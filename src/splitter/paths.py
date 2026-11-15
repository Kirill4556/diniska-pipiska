import os
import sys
from pathlib import Path


def cache_dir() -> Path:
    """Локальный кэш приложения (модели, ffmpeg). Переопределяется SPLITTER_CACHE."""
    override = os.environ.get("SPLITTER_CACHE")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "splitter"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "splitter"
    base.mkdir(parents=True, exist_ok=True)
    return base


def models_dir() -> Path:
    path = cache_dir() / "models"
    path.mkdir(exist_ok=True)
    return path
