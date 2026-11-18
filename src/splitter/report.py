"""Журнал работы и отчёт об ошибке (FR-27).

Журнал — по файлу на запуск в <кэш>/logs, хранятся последние KEEP_LOGS.
Отчёт — текст для пересылки разработчику: версия, ОС, видеокарта, настройки, ошибка и конец журнала.
Пути к личным папкам из отчёта вырезаются, остаётся только имя файла.
"""

import platform
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .paths import cache_dir

KEEP_LOGS = 5

# строки прогресса tqdm в журнале не нужны
_PROGRESS_RE = re.compile(r"\d+%\|")
# «C:\Users\имя\Music\» или «/home/имя/music/» — всё до последней части пути
_WIN_DIRS_RE = re.compile(r"[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\r\n]+[\\/])+")
_POSIX_DIRS_RE = re.compile(r"(?<![\w.])/(?:home|Users|root|tmp|mnt|media|var)/(?:[^/\r\n]+/)+")


def logs_dir() -> Path:
    path = cache_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


class RunLog:
    """Файл журнала текущего запуска. Ошибки записи не мешают работе."""

    def __init__(self, kind: str):
        self._lock = threading.Lock()  # окно пишет из основного потока и из потока чтения stderr
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.path = logs_dir() / f"{kind}-{stamp}.log"
        self._prune()
        self.write(f"splitter {__version__}, {platform.platform()}, Python {platform.python_version()}")

    def _prune(self) -> None:
        logs = sorted(logs_dir().glob("*.log"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        for old in logs[KEEP_LOGS - 1 :]:
            try:
                old.unlink()
            except OSError:
                pass

    def write(self, line: str) -> None:
        line = line.rstrip()
        if not line or _PROGRESS_RE.search(line):
            return
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as file:
                file.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        except OSError:
            pass

    def tail(self, lines: int = 40) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()[-lines:]
        except OSError:
            return []


def anonymize(text: str) -> str:
    """Убирает папки из путей, оставляя имена файлов."""
    text = _WIN_DIRS_RE.sub("…\\\\", text)
    return _POSIX_DIRS_RE.sub("…/", text)


def build_report(message: str, settings: dict, device: str | None, log_tail: list[str]) -> str:
    rows = [
        "Отчёт об ошибке Splitter",
        f"Версия: {__version__}",
        f"Система: {platform.platform()}",
        f"Видеокарта: {device or 'не определялась'}",
        "Настройки: " + ", ".join(f"{key}={value}" for key, value in settings.items()),
        "",
        "Ошибка:",
        message.strip(),
    ]
    if log_tail:
        rows += ["", "Конец журнала:", *log_tail]
    return anonymize("\n".join(rows)) + "\n"
