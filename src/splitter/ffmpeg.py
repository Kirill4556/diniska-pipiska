"""Поиск ffmpeg и кодирование результата с переносом тегов."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import SplitterError
from .paths import cache_dir
from .i18n import tr

FORMATS = ("mp3", "wav", "flac")
BITRATES = (192, 256, 320)


def ensure_ffmpeg() -> str:
    """Возвращает путь к ffmpeg и гарантирует, что он виден в PATH.

    Сначала берём системный ffmpeg, иначе — бинарник из пакета imageio-ffmpeg.
    В PATH он нужен под именем `ffmpeg`: его ищут audio-separator и librosa.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:
        raise SplitterError(
            tr("Не найден ffmpeg. Установите его (https://ffmpeg.org) и добавьте в PATH.")
        ) from exc

    bin_dir = cache_dir() / "bin"
    bin_dir.mkdir(exist_ok=True)
    target = bin_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if not target.exists() or target.stat().st_size != bundled.stat().st_size:
        shutil.copy2(bundled, target)
    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    return str(target)


def encode(wav: Path, dst: Path, fmt: str, bitrate: int, tags_from: Path | None = None) -> None:
    """Кодирует wav в dst; теги и обложку берёт из tags_from (FR-9)."""
    cmd = [ensure_ffmpeg(), "-y", "-v", "error", "-i", str(wav)]
    if tags_from is not None:
        cmd += ["-i", str(tags_from), "-map", "0:a", "-map_metadata", "1"]
        if fmt in ("mp3", "flac"):
            # обложка — необязательный видеопоток исходника
            cmd += ["-map", "1:v?", "-c:v", "copy", "-disposition:v", "attached_pic"]
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-id3v2_version", "3"]
    elif fmt == "flac":
        cmd += ["-c:a", "flac"]
    else:
        cmd += ["-c:a", "pcm_s16le"]
    # формат задаём явно: dst может быть временным файлом с расширением .part
    cmd += ["-f", fmt, str(dst)]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        dst.unlink(missing_ok=True)
        detail = result.stderr.strip()
        if "No space left" in detail:
            raise SplitterError(tr("Недостаточно места на диске для {0}.").format(dst.name))
        raise SplitterError(tr("ffmpeg не смог записать {0}: {1}").format(dst.name, detail[-400:]))
