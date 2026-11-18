"""Общий конвейер для CLI и GUI: ссылки -> разделение -> текст.

О ходе работы сообщает через emit(event, **data). События:
    device(gpu, name)                   — годится ли видеокарта и её описание
    download_start(index) / download_progress(index, value) / downloaded(index, path)
    model                               — загружается модель разделения
    start(index, stem)                  — началось разделение файла; stem — начало имён результатов
    split(index, outputs, seconds)      — дорожки готовы
    lyrics_model / lyrics_start(index) / lyrics(index, txt, lrc, lines, source: lrclib|whisper)
    lyrics_online(index, found, note)   — результат поиска готового текста в LRCLIB
    done(index)                         — по файлу сделано всё
    error(index, message)               — файл пропущен
"""

import time
from pathlib import Path

from .core import Options, Splitter, device_description, gpu_available, unique_stems
from .download import download, is_url
from .errors import SplitterError


def run(inputs: list[str], out: Path, options: Options, emit) -> int:
    """Обрабатывает входы (пути или ссылки). Возвращает число неудач."""
    options.validate()
    emit("device", gpu=gpu_available(), name=device_description())
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

    stems = dict(zip(sources, unique_stems(list(sources.values()))))
    vocals: dict[int, Path] = {}
    emit("model")
    with Splitter(options) as splitter:
        for index, src in sources.items():
            emit("start", index=index, stem=stems[index])
            started = time.monotonic()
            try:
                result = splitter.split(src, out, keep_vocals=options.lyrics, stem=stems[index])
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

        if vocals and options.lyrics_online:
            from .lrclib import lookup
            from .lyrics import format_lrc, save_lyrics

            for index in list(vocals):
                emit("lyrics_start", index=index)
                lines, note = lookup(sources[index])
                emit("lyrics_online", index=index, found=lines is not None, note=note)
                if lines is None:
                    continue
                try:
                    txt, lrc = save_lyrics(format_lrc(lines), out, stems[index])
                except OSError as exc:
                    fail(index, SplitterError(f"Не удалось сохранить текст: {exc}"))
                    del vocals[index]
                    continue
                emit("lyrics", index=index, txt=str(txt), lrc=str(lrc), lines=len(lines), source="lrclib")
                emit("done", index=index)
                del vocals[index]

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
                    txt, lrc = save_lyrics(format_lrc(lines), out, stems[index])
                except SplitterError as exc:
                    fail(index, exc)
                    continue
                emit("lyrics", index=index, txt=str(txt), lrc=str(lrc), lines=len(lines), source="whisper")
                emit("done", index=index)
    return len(failed)
