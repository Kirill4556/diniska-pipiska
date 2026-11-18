"""Кэш результатов (FR-30): тот же файл с теми же настройками повторно не разделяется.

Сами дорожки не копируются в кэш — хранится только указатель на уже сохранённые файлы
(путь, размер, время изменения). Если файлы на месте и не менялись, ими и пользуемся:
при другой папке или другом имени результата они копируются; если их удалили — считаем заново.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path

from .core import CLEAN_MODELS, KARAOKE_MODEL, MODELS, Options
from .paths import cache_dir

CACHE_VERSION = 2  # поднимать, когда меняется сама обработка (модели, сведение уровней, настройки скорости)
MAX_ENTRIES = 2000


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def options_key(options: Options) -> str:
    """Всё, что влияет на дорожки. Текст сюда не входит — у него свой ключ."""
    fields = {
        "v": CACHE_VERSION,
        "mode": options.mode,
        "quality": options.quality,
        "model": MODELS[options.mode][options.quality],
        "fmt": options.fmt,
    }
    if options.fmt == "mp3":
        fields["bitrate"] = options.bitrate
    if options.mode == "stems":
        fields["mix"] = sorted(options.mix)
    if options.mode == "backing":
        fields["karaoke"] = KARAOKE_MODEL
    if options.clean_vocals:
        fields["clean"] = list(CLEAN_MODELS)
    return json.dumps(fields, sort_keys=True)


def lyrics_key(options: Options) -> str:
    return json.dumps({"language": options.language, "online": options.lyrics_online}, sort_keys=True)


def _stamp(path: Path) -> list | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return [info.st_size, int(info.st_mtime)]


class ResultCache:
    def __init__(self, path: Path | None = None):
        self.path = path or cache_dir() / "results.json"
        try:
            self._entries: dict = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._entries = {}

    def _save(self) -> None:
        if len(self._entries) > MAX_ENTRIES:  # самые старые записи — первые
            for key in list(self._entries)[: len(self._entries) - MAX_ENTRIES]:
                del self._entries[key]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".part")
            tmp.write_text(json.dumps(self._entries, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # без кэша приложение просто работает медленнее

    @staticmethod
    def _valid(files: dict) -> dict[str, Path] | None:
        """{имя: [путь, размер, время]} -> {имя: путь}, если все файлы на месте и не менялись."""
        paths = {}
        for name, (path, size, mtime) in files.items():
            if _stamp(Path(path)) != [size, mtime]:
                return None
            paths[name] = Path(path)
        return paths

    @staticmethod
    def _place(paths: dict[str, Path], out_dir: Path, rename) -> dict[str, Path]:
        """Кладёт файлы под нужными именами в нужную папку (если они уже там — ничего не делает)."""
        placed = {}
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, path in paths.items():
            target = out_dir / rename(name, path)
            if target.resolve() != path.resolve():
                part = target.with_name(target.name + ".part")
                shutil.copyfile(path, part)
                os.replace(part, target)
            placed[name] = target
        return placed

    def find_tracks(self, digest: str, options: Options, out_dir: Path, stem: str) -> dict[str, Path] | None:
        entry = self._entries.get(digest, {}).get(options_key(options))
        paths = self._valid(entry) if entry else None
        if paths is None:
            return None
        try:
            return self._place(paths, out_dir, lambda name, path: f"{stem}_{name}{path.suffix}")
        except OSError:
            return None

    def find_lyrics(self, digest: str, options: Options, out_dir: Path, stem: str) -> tuple[Path, Path] | None:
        entry = self._entries.get(digest, {}).get("lyrics", {}).get(lyrics_key(options))
        paths = self._valid(entry) if entry else None
        if paths is None or set(paths) != {"txt", "lrc"}:
            return None
        try:
            placed = self._place(paths, out_dir, lambda name, path: f"{stem}.{name}")
        except OSError:
            return None
        return placed["txt"], placed["lrc"]

    def store_tracks(self, digest: str, options: Options, outputs: dict[str, Path]) -> None:
        files = {name: [str(Path(path).resolve()), *(_stamp(Path(path)) or [0, 0])] for name, path in outputs.items()}
        entry = self._entries.pop(digest, {})  # перемещаем в конец: свежие записи живут дольше
        entry[options_key(options)] = files
        self._entries[digest] = entry
        self._save()

    def store_lyrics(self, digest: str, options: Options, txt: Path, lrc: Path) -> None:
        entry = self._entries.pop(digest, {})
        files = {name: [str(Path(path).resolve()), *(_stamp(Path(path)) or [0, 0])] for name, path in (("txt", txt), ("lrc", lrc))}
        entry.setdefault("lyrics", {})[lyrics_key(options)] = files
        self._entries[digest] = entry
        self._save()
