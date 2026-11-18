"""Кэш результатов и восстановление очереди (FR-30)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from splitter.cache import ResultCache, file_hash, options_key
from splitter.core import Options
from splitter.gui import DONE, STATE_ROLE, MainWindow


def make_outputs(folder, stem):
    folder.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for name in ("instrumental", "vocals"):
        outputs[name] = folder / f"{stem}_{name}.mp3"
        outputs[name].write_bytes(name.encode() * 100)
    return outputs


def test_key_depends_on_what_changes_tracks():
    base = options_key(Options())
    assert options_key(Options(lyrics=True, language="ru")) == base  # текст на дорожки не влияет
    assert options_key(Options(quality="fast")) != base
    assert options_key(Options(fmt="flac", bitrate=192)) == options_key(Options(fmt="flac"))  # битрейт только для mp3
    assert options_key(Options(bitrate=192)) != base
    assert options_key(Options(mode="stems", mix=("bass", "drums"))) == options_key(Options(mode="stems", mix=("drums", "bass")))


def test_finds_reuses_and_copies(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"audio")
    digest = file_hash(song)
    options = Options()
    cache = ResultCache(tmp_path / "results.json")
    assert cache.find_tracks(digest, options, tmp_path / "out", "song") is None

    outputs = make_outputs(tmp_path / "out", "song")
    cache.store_tracks(digest, options, outputs)
    cache = ResultCache(tmp_path / "results.json")  # переживает перезапуск
    found = cache.find_tracks(digest, options, tmp_path / "out", "song")
    assert {name: path.resolve() for name, path in found.items()} == {name: path.resolve() for name, path in outputs.items()}

    # другая папка и другое имя — файлы копируются
    copied = cache.find_tracks(digest, options, tmp_path / "other", "song (2)")
    assert copied["vocals"].name == "song (2)_vocals.mp3" and copied["vocals"].read_bytes() == outputs["vocals"].read_bytes()
    assert cache.find_tracks(digest, Options(quality="fast"), tmp_path / "out", "song") is None


def test_changed_or_deleted_outputs_are_not_reused(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"audio")
    cache = ResultCache(tmp_path / "results.json")
    outputs = make_outputs(tmp_path / "out", "song")
    cache.store_tracks(file_hash(song), Options(), outputs)
    outputs["vocals"].write_bytes(b"edited by user, different size")
    assert cache.find_tracks(file_hash(song), Options(), tmp_path / "out", "song") is None
    outputs["vocals"].unlink()
    assert cache.find_tracks(file_hash(song), Options(), tmp_path / "out", "song") is None


def test_lyrics_cached_per_language(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"audio")
    digest = file_hash(song)
    txt, lrc = tmp_path / "out" / "song.txt", tmp_path / "out" / "song.lrc"
    txt.parent.mkdir()
    txt.write_text("раз\n", encoding="utf-8")
    lrc.write_text("[00:01.00]раз\n", encoding="utf-8")
    cache = ResultCache(tmp_path / "results.json")
    cache.store_lyrics(digest, Options(lyrics=True, language="ru"), txt, lrc)
    assert cache.find_lyrics(digest, Options(lyrics=True, language="ru"), tmp_path / "copy", "song")[1].read_text(encoding="utf-8") == "[00:01.00]раз\n"
    assert cache.find_lyrics(digest, Options(lyrics=True, language="en"), tmp_path / "copy", "song") is None


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_unfinished_queue_comes_back(app, tmp_path):
    settings_file = str(tmp_path / "settings.ini")
    songs = []
    for name in ("a.mp3", "b.mp3"):
        (tmp_path / name).write_bytes(b"")
        songs.append(tmp_path / name)

    first = MainWindow()
    first._settings = QSettings(settings_file, QSettings.Format.IniFormat)
    first.restore_queue()
    first.add_paths(songs)
    first._items()[0].setData(0, STATE_ROLE, DONE)  # первая обработана
    first._refresh()
    first._process = None
    first._save_settings = lambda: None
    first.close()

    second = MainWindow()
    second._settings = QSettings(settings_file, QSettings.Format.IniFormat)
    second._save_settings = lambda: None
    assert second.restore_queue() == 1
    assert [item.text(0) for item in second._items()] == ["b.mp3"]
    second.close()
