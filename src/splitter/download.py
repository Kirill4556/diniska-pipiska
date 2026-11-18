"""Вход по ссылке: скачиваем звук через yt-dlp и превращаем в обычный mp3 (FR-14)."""

import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import SplitterError
from .ffmpeg import ensure_ffmpeg
from .i18n import tr

COPYRIGHT_NOTICE = (
    "Скачивайте только то, на что у вас есть права: свои записи, свободные лицензии "
    "или разрешение правообладателя. Результат — для личного использования."
)


def is_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


def download(url: str, dest_dir: Path, on_progress=None) -> Path:
    """Скачивает звук по ссылке и возвращает путь к mp3 в dest_dir."""
    import yt_dlp

    ffmpeg = ensure_ffmpeg()
    tmp = Path(tempfile.mkdtemp(prefix="splitter_dl_"))

    def hook(status: dict) -> None:
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        if on_progress and total and status.get("status") == "downloading":
            on_progress(min(status.get("downloaded_bytes", 0) / total, 1.0))

    params = {
        "format": "bestaudio/best",
        "outtmpl": str(tmp / "audio.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": ffmpeg,
        "progress_hooks": [hook],
    }
    try:
        try:
            with yt_dlp.YoutubeDL(params) as ydl:
                info = ydl.extract_info(url.strip(), download=True)
        except Exception as exc:
            detail = str(exc).replace("ERROR: ", "").strip() or type(exc).__name__
            raise SplitterError(tr("Не удалось скачать {0}: {1}").format(url, detail)) from exc

        downloaded = [p for p in tmp.iterdir() if p.is_file() and not p.name.endswith(".part")]
        if not downloaded:
            raise SplitterError(tr("По ссылке {0} не нашлось звука.").format(url))

        title = info.get("track") or info.get("title") or "track"
        artist = info.get("artist") or info.get("uploader") or ""
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{_safe_name(title)}.mp3"
        cmd = [ffmpeg, "-y", "-v", "error", "-i", str(downloaded[0]), "-vn", "-c:a", "libmp3lame", "-b:a", "320k"]
        cmd += ["-metadata", f"title={title}", "-metadata", f"artist={artist}", "-id3v2_version", "3", str(target)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            target.unlink(missing_ok=True)
            raise SplitterError(tr("Не удалось преобразовать скачанный файл в mp3: {0}").format(result.stderr.strip()[-300:]))
        return target
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _safe_name(title: str) -> str:
    cleaned = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in title).strip(" .")
    return cleaned[:120] or "track"
