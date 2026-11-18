"""Готовый текст с таймкодами из открытой базы LRCLIB (FR-17).

Отправляются только исполнитель, название и длительность — сам звук никуда не уходит.
Функция включается пользователем; при любой сетевой ошибке просто возвращаем None.
"""

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .ffmpeg import ensure_ffmpeg
from .i18n import tr

API = "https://lrclib.net/api"
USER_AGENT = f"splitter/{__version__} (desktop app)"
DURATION_TOLERANCE = 3.0  # секунд: другая длительность — скорее всего другая версия песни

# приписки из названий роликов, мешающие поиску
_NOISE_RE = re.compile(
    r"\s*[\(\[][^)\]]*(official|video|audio|lyric|lyrics|clip|клип|премьера|hd|4k|remaster)[^)\]]*[\)\]]",
    re.IGNORECASE,
)


@dataclass
class TrackInfo:
    artist: str
    title: str
    duration: float


def track_info(path: Path) -> TrackInfo | None:
    """Исполнитель, название и длительность: из тегов, иначе из имени файла «Исполнитель - Название»."""
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    result = subprocess.run(
        [ensure_ffmpeg(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    tags: dict[str, str] = {}
    duration = 0.0
    for line in result.stderr.splitlines():
        key, _, value = line.strip().partition(":")
        key = key.strip().lower()
        if key in ("title", "artist", "album_artist") and key not in tags and value.strip():
            tags[key] = value.strip()
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", line)
        if match:
            hours, minutes, seconds = match.groups()
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    artist = tags.get("artist") or tags.get("album_artist") or ""
    title = tags.get("title") or ""
    if not title:
        name = Path(path).stem
        if " - " in name:
            artist, title = (part.strip() for part in name.split(" - ", 1))
        else:
            title = name
    title = clean_title(title)
    if artist and title.lower().startswith(artist.lower() + " - "):
        title = title[len(artist) + 3 :]  # «Исполнитель - Название» в теге названия
    return TrackInfo(artist.strip(), title.strip(), duration) if title.strip() else None


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", _NOISE_RE.sub("", title)).strip()


def _request(endpoint: str, params: dict, timeout: float) -> object | None:
    url = f"{API}/{endpoint}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def find_synced(info: TrackInfo, timeout: float = 10.0) -> str | None:
    """Синхронизированный текст (.lrc) или None. Сетевые ошибки пробрасываются — решает вызывающий."""
    if info.artist:
        params = {"artist_name": info.artist, "track_name": info.title}
        if info.duration:
            params["duration"] = round(info.duration)
        found = _request("get", params, timeout)
        if isinstance(found, dict) and found.get("syncedLyrics") and _close(found, info):
            return found["syncedLyrics"]
        candidates = _request("search", {"artist_name": info.artist, "track_name": info.title}, timeout)
    else:
        candidates = _request("search", {"q": info.title}, timeout)
    for item in candidates or []:
        if isinstance(item, dict) and item.get("syncedLyrics") and _close(item, info):
            return item["syncedLyrics"]
    return None


def normalize(lrc: str) -> list[tuple[float, str]]:
    """Строки с таймкодами без служебных тегов; учитывает [offset:мс] (положительный — текст раньше)."""
    from .lyrics import parse_lrc

    match = re.search(r"^\s*\[offset:\s*([+-]?\d+)\s*\]", lrc, re.IGNORECASE | re.MULTILINE)
    shift = int(match.group(1)) / 1000 if match else 0.0
    return [(max(start - shift, 0.0), text) for start, text in parse_lrc(lrc)]


def lookup(path: Path, attempts: int = 2) -> tuple[list[tuple[float, str]] | None, str]:
    """Ищет текст для файла. Возвращает (строки или None, пояснение для журнала). Не бросает исключений."""
    info = track_info(path)
    if info is None:
        return None, tr("нет названия ни в тегах, ни в имени файла")
    described = tr("{0}{1}, {2:.0f} с").format(info.artist + ' — ' if info.artist else '', info.title, info.duration)
    for attempt in range(attempts):
        try:
            lrc = find_synced(info)
            break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if attempt == attempts - 1:
                return None, tr("{0}: сервис недоступен ({1})").format(described, exc)
    if not lrc:
        return None, tr("{0}: не найден").format(described)
    lines = [line for line in normalize(lrc) if line[1]]
    return (lines, tr("{0}: найден").format(described)) if lines else (None, tr("{0}: пустой текст").format(described))


def _close(item: dict, info: TrackInfo) -> bool:
    if not info.duration or not item.get("duration"):
        return True
    return abs(float(item["duration"]) - info.duration) <= DURATION_TOLERANCE
