"""Общий конвейер для CLI и GUI: ссылки -> разделение -> текст.

О ходе работы сообщает через emit(event, **data). События:
    device(gpu, name)                   — годится ли видеокарта и её описание
    download_start(index) / download_progress(index, value) / downloaded(index, path)
    model                               — загружается модель разделения
    start(index, stem)                  — началось разделение файла; stem — начало имён результатов
    split(index, outputs, seconds, cached) — дорожки готовы (cached — взяты из кэша, FR-30)
    lyrics_model / lyrics_start(index) / lyrics(index, txt, lrc, lines, source: lrclib|whisper)
    lyrics_online(index, found, note)   — результат поиска готового текста в LRCLIB
    done(index)                         — по файлу сделано всё
    error(index, message)               — файл пропущен
"""

import shutil
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from .cache import ResultCache, file_hash
from .core import Options, Splitter, device_description, gpu_available, unique_stems
from .download import download, is_url
from .errors import SplitterError
from .i18n import tr


def _vocals_from_outputs(outputs: dict[str, Path], folder: Path, name: str) -> Path:
    """Вокал для распознавания текста из уже сохранённых дорожек (когда разделение взято из кэша)."""
    import soundfile as sf

    from .audio import RATE, load

    if "vocals" in outputs:
        audio = load(outputs["vocals"])
    elif "lead" in outputs and "backing" in outputs:
        lead, backing = load(outputs["lead"]), load(outputs["backing"])
        shared = min(len(lead), len(backing))
        audio = lead[:shared] + backing[:shared]
    else:
        raise SplitterError(tr("В сохранённых дорожках нет вокала для распознавания текста."))
    wav = folder / f"{name}.wav"
    sf.write(wav, audio, RATE, subtype="PCM_16")
    return wav


def run(inputs: list[str], out: Path, options: Options, emit, use_cache: bool = True) -> int:
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
    cache = ResultCache() if use_cache else None
    digests: dict[int, str] = {}
    vocals: dict[int, Path] = {}  # вокал (wav) для распознавания текста
    pending: dict[int, Path] = {}  # что придётся разделять
    scratch = Path(tempfile.mkdtemp(prefix="splitter_lyrics_"))

    def lyrics_ready(index: int, txt: Path, lrc: Path, lines: int, source: str) -> None:
        if cache is not None and index in digests:
            cache.store_lyrics(digests[index], options, txt, lrc)
        emit("lyrics", index=index, txt=str(txt), lrc=str(lrc), lines=lines, source=source)
        emit("done", index=index)

    try:
        with ExitStack() as stack:
            # 1. что уже есть в кэше
            for index, src in sources.items():
                if cache is None or not src.is_file():
                    pending[index] = src
                    continue
                digests[index] = file_hash(src)
                outputs = cache.find_tracks(digests[index], options, out, stems[index])
                if outputs is None:
                    pending[index] = src
                    continue
                emit("start", index=index, stem=stems[index])
                emit("split", index=index, outputs={name: str(path) for name, path in outputs.items()}, seconds=0, cached=True)
                if not options.lyrics:
                    emit("done", index=index)
                    continue
                cached = cache.find_lyrics(digests[index], options, out, stems[index])
                if cached is not None:
                    from .lyrics import parse_lrc

                    txt, lrc = cached
                    emit("lyrics", index=index, txt=str(txt), lrc=str(lrc), lines=len(parse_lrc(lrc.read_text(encoding="utf-8"))), source="cache")
                    emit("done", index=index)
                    continue
                try:
                    vocals[index] = _vocals_from_outputs(outputs, scratch, str(index))
                except SplitterError as exc:
                    fail(index, exc)

            # 2. разделение остального
            splitter = None
            if pending:
                emit("model")
                splitter = stack.enter_context(Splitter(options))
                for index, src in pending.items():
                    emit("start", index=index, stem=stems[index])
                    started = time.monotonic()
                    try:
                        result = splitter.split(src, out, keep_vocals=options.lyrics, stem=stems[index])
                    except SplitterError as exc:
                        fail(index, exc)
                        continue
                    if cache is not None and index in digests:
                        cache.store_tracks(digests[index], options, result.outputs)
                    emit(
                        "split",
                        index=index,
                        outputs={name: str(path) for name, path in result.outputs.items()},
                        seconds=round(time.monotonic() - started),
                        cached=False,
                    )
                    if options.lyrics:
                        vocals[index] = result.vocals_wav
                    else:
                        emit("done", index=index)

            # 3. текст: сначала готовый из LRCLIB (если разрешено), потом распознавание
            vocals = dict(sorted(vocals.items()))
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
                        fail(index, SplitterError(tr("Не удалось сохранить текст: {0}").format(exc)))
                        del vocals[index]
                        continue
                    lyrics_ready(index, txt, lrc, len(lines), "lrclib")
                    del vocals[index]

            if vocals:
                from .lyrics import Transcriber, format_lrc, save_lyrics

                if splitter is not None:
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
                    lyrics_ready(index, txt, lrc, len(lines), "whisper")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return len(failed)
