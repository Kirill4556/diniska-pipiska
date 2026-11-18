"""Сведения для карточки песни в очереди (FR-21): обложка, длительность и форма волны."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .ffmpeg import ensure_ffmpeg

PEAK_BARS = 120  # столбиков в мини-волне
_PEAK_RATE = 4000  # для формы волны хватает низкой частоты — декодирование в разы быстрее


@dataclass
class SongInfo:
    duration: float = 0.0
    peaks: list[float] = field(default_factory=list)  # 0..1, PEAK_BARS штук
    cover: bytes | None = None  # PNG вложенной обложки


def _run(args: list[str]) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    return subprocess.run([ensure_ffmpeg(), "-v", "error", *args], capture_output=True, creationflags=flags, timeout=120)


def song_info(path: Path) -> SongInfo:
    info = SongInfo()
    try:
        result = _run(["-i", str(path), "-vn", "-ac", "1", "-ar", str(_PEAK_RATE), "-f", "f32le", "-"])
    except (OSError, subprocess.TimeoutExpired):
        return info
    samples = np.frombuffer(result.stdout, dtype=np.float32)
    if samples.size:
        info.duration = samples.size / _PEAK_RATE
        bins = np.array_split(np.abs(samples), PEAK_BARS)
        peaks = np.array([chunk.max() if chunk.size else 0.0 for chunk in bins])
        top = float(peaks.max()) or 1.0
        info.peaks = [round(float(value) / top, 3) for value in peaks]
    try:
        cover = _run(["-i", str(path), "-an", "-frames:v", "1", "-vf", "scale=96:96:force_original_aspect_ratio=increase,crop=96:96",
                      "-c:v", "png", "-f", "image2pipe", "-"])
        if cover.returncode == 0 and cover.stdout.startswith(b"\x89PNG"):
            info.cover = cover.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    return info


def format_length(seconds: float) -> str:
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}:{rest:02d}"
