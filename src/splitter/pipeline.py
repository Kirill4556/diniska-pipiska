"""Общий конвейер для CLI и GUI: ссылки -> разделение -> текст.

О ходе работы сообщает через emit(event, **data). События:
    device(gpu)                         — есть ли видеокарта
    download_start(index) / download_progress(index, value) / downloaded(index, path)
    model                               — загружается модель разделения
    start(index)                        — началось разделение файла
    split(index, outputs, seconds)      — дорожки готовы
    lyrics_model / lyrics_start(index) / lyrics(index, txt, lrc)
    done(index)                         — по файлу сделано всё
    error(index, message)               — файл пропущен
"""

import time
from pathlib import Path

from .core import Options, Splitter, gpu_available
from .download import download, is_url
from .errors import SplitterError


def run(inputs: list[str], out: Path, options: Options, emit) -> int:
    """Обрабатывает входы (пути или ссылки). Возвращает число неудач."""
    options.validate()
    emit("device", gpu=gpu_available())
    failed: set[int] = set()

    def fail(index: int, exc: Exception) -> None:
        failed.add(index)
        emit("error", index=index, message=str(exc))

    sources: dict[int, Path] = {}
    for index, item in enumerate(inputs):
        if not is_url(item):
            sources[index] = Path(item)
            continue
        emit("download_start", index=index)
        try:
            sources[index] = download(item, out, lambda value, i=index: emit("download_progress", index=i, value=value))
        except SplitterError as exc:
            fail(index, exc)
            continue
        emit("downloaded", index=index, path=str(sources[index]))

    if not sources:
        return len(failed)

    vocals: dict[int, Path] = {}
    emit("model")
    with Splitter(options) as splitter:
        for index, src in sources.items():
            emit("start", index=index)
            started = time.monotonic()
            try:
                result = splitter.split(src, out, keep_vocals=options.lyrics)
            except SplitterError as exc:
                fail(index, exc)
                continue
            emit(
                "split",
                index=index,
                outputs={name: str(path) for name, path in result.outputs.items()},
                seconds=round(time.monotonic() - started),
            )
            if options.lyrics:
                vocals[index] = result.vocals_wav
            else:
                emit("done", index=index)

        if vocals:
            from .lyrics import Transcriber, format_lrc, save_lyrics

            splitter.release_models()  # видеопамять нужна Whisper
            emit("lyrics_model")
            try:
                transcriber = Transcriber()
            except SplitterError as exc:
                for index in vocals:
                    fail(index, exc)
                return len(failed)
            for index, wav in vocals.items():
                emit("lyrics_start", index=index)
                try:
                    lines = transcriber.transcribe(wav, options.language)
                    txt, lrc = save_lyrics(format_lrc(lines), out, sources[index].stem)
                except SplitterError as exc:
                    fail(index, exc)
                    continue
                emit("lyrics", index=index, txt=str(txt), lrc=str(lrc), lines=len(lines))
                emit("done", index=index)
    return len(failed)
