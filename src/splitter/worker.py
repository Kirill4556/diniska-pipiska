"""Фоновый процесс для GUI.

Задание приходит в stdin одним JSON:
    {"inputs": [...], "out": "...", "format": "mp3", "bitrate": 320, "quality": "high",
     "mode": "vocals", "mix": ["drums", "bass", "other"], "lyrics": false, "language": null}
События уходят в stdout, по одному JSON в строке (поле "event"): всё из pipeline.run плюс
    model_download(value), progress(index, value), lyrics_progress(index, value), fatal(message), finished.
Отдельный процесс нужен, чтобы окно не подвисало, а отмена была мгновенной (процесс убивают).
"""

import json
import os
import sys
from pathlib import Path

from .errors import SplitterError

# сколько прогресс-баров tqdm библиотека показывает на один файл: (режим, качество)
_BARS_PER_FILE = {
    ("vocals", "fast"): 2,
    ("vocals", "high"): 1,
    ("stems", "fast"): 2,
    ("stems", "high"): 8,
    ("backing", "fast"): 3,
    ("backing", "high"): 2,
}


class _Progress:
    """Перехватывает tqdm внутри библиотек и превращает его в события."""

    def __init__(self, emit, bars_per_file: int):
        self._emit = emit
        self._bars = bars_per_file
        self._index: int | None = None
        self._event = "progress"
        self._finished: set[int] = set()
        self._last = -1.0

    def begin(self, index: int, event: str) -> None:
        self._index, self._event = index, event
        self._finished.clear()
        self._last = -1.0

    def end(self) -> None:
        self._index = None

    def report(self, bar, done: int, total: int | None) -> None:
        if not total or id(bar) in self._finished:
            return
        fraction = min(done / total, 1.0)
        if getattr(bar, "unit", "") == "iB":  # скачивание модели
            self._send("model_download", fraction)
            return
        if self._index is None:
            return
        if self._event == "lyrics_progress":
            self._send(self._event, fraction, index=self._index)
            return
        value = min((len(self._finished) + fraction) / self._bars, 1.0)
        if done >= total:
            self._finished.add(id(bar))
        # последние 10% — сведение и кодирование
        self._send(self._event, value * 0.9, index=self._index)

    def _send(self, event: str, value: float, **extra) -> None:
        if abs(value - self._last) >= 0.01:
            self._last = value
            self._emit(event, value=round(value, 3), **extra)

    def install(self) -> None:
        from tqdm.std import tqdm

        progress = self
        original_iter, original_update = tqdm.__iter__, tqdm.update

        def patched_iter(bar):
            for count, item in enumerate(original_iter(bar), 1):
                yield item
                progress.report(bar, count, bar.total)

        def patched_update(bar, n=1):
            result = original_update(bar, n)
            progress.report(bar, bar.n, bar.total)
            return result

        tqdm.__iter__, tqdm.update = patched_iter, patched_update


def main() -> None:
    # события — в исходный stdout; всё, что печатают библиотеки, уводим в stderr
    events = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def send(event: str, **data) -> None:
        events.write(json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")
        events.flush()

    try:
        job = json.loads(sys.stdin.buffer.read().decode("utf-8"))

        from . import pipeline
        from .core import Options

        options = Options(
            fmt=job["format"],
            bitrate=job["bitrate"],
            quality=job["quality"],
            mode=job.get("mode", "vocals"),
            mix=tuple(job.get("mix", ())),
            lyrics=job.get("lyrics", False),
            language=job.get("language"),
            lyrics_online=job.get("lyrics_online", False),
        )
        progress = _Progress(send, _BARS_PER_FILE.get((options.mode, options.quality), 1))
        progress.install()

        def emit(event: str, **data) -> None:
            if event == "start":
                progress.begin(data["index"], "progress")
            elif event == "lyrics_start":
                progress.begin(data["index"], "lyrics_progress")
            elif event in ("split", "lyrics", "lyrics_online", "error"):
                progress.end()
            send(event, **data)

        pipeline.run(job["inputs"], Path(job["out"]), options, emit)
        send("finished")
    except SplitterError as exc:
        send("fatal", message=str(exc))
    except Exception as exc:  # всё неожиданное показываем в окне, а не теряем
        send("fatal", message=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
