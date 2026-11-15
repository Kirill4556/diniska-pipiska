"""Тесты окна без нейросети (Qt в режиме offscreen)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from splitter.gui import QUEUED, STATE_ROLE, MainWindow, expand_paths


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    win = MainWindow()
    win._save_settings = lambda: None  # не трогаем настройки пользователя
    yield win
    win.close()


def test_expand_paths_skips_non_audio(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("a.mp3", "sub/b.m4a", "cover.png"):
        (tmp_path / name).write_bytes(b"")
    assert [p.name for p in expand_paths([tmp_path, tmp_path / "cover.png"])] == ["a.mp3", "b.m4a"]


def test_queue_ignores_duplicates_and_enables_start(window, tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    assert not window.start_button.isEnabled()
    window.add_paths([song])
    window.add_paths([song, tmp_path])
    items = window._items()
    assert len(items) == 1 and items[0].data(0, STATE_ROLE) == QUEUED
    assert window.start_button.isEnabled() and not window.cancel_button.isEnabled()


def test_urls_and_mode_controls(window):
    assert not window.add_url("просто текст")
    assert window.add_url("https://example.com/track") and not window.add_url("https://example.com/track")
    window.show()
    window.mode.setCurrentIndex(window.mode.findData("stems"))
    assert window.mix_row.isVisible()
    window.mode.setCurrentIndex(window.mode.findData("vocals"))
    assert not window.mix_row.isVisible()
    window.lyrics_box.setChecked(False)
    assert not window.language.isEnabled()
    window.lyrics_box.setChecked(True)
    assert window.language.isEnabled()


def test_bitrate_only_for_mp3(window):
    window.format.setCurrentText("flac")
    assert not window.bitrate.isEnabled()
    window.format.setCurrentText("mp3")
    assert window.bitrate.isEnabled()
