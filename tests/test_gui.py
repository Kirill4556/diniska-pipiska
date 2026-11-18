"""Тесты окна без нейросети (Qt в режиме offscreen)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from splitter.gui import (
    DONE,
    ERROR_ROLE,
    FAILED,
    FILE_ROLE,
    QUEUED,
    STATE_ROLE,
    MainWindow,
    expand_paths,
    format_duration,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    win = MainWindow()
    win._save_settings = lambda: None  # не трогаем настройки пользователя
    win._settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    win._fill_presets()
    yield win
    win._process = None  # иначе закрытие спросит «Прервать обработку?» и тест повиснет
    win.close()


def fake_run(window, tmp_path, names, lyrics=False):
    """Готовит окно так, будто обработчик запущен на этих файлах."""
    songs = []
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"")
        songs.append(tmp_path / name)
    window.add_paths(songs)
    window._batch = window._items()
    window._job = {"format": "mp3", "mode": "vocals", "mix": [], "out": str(tmp_path / "out"), "lyrics": lyrics}
    window._process = object()  # «идёт обработка»
    return window._batch


def split_event(tmp_path, index, stem):
    outputs = {}
    for name in ("instrumental", "vocals"):
        path = tmp_path / "out" / f"{stem}_{name}.mp3"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"x")
        outputs[name] = str(path)
    return {"event": "split", "index": index, "outputs": outputs, "seconds": 5}


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


def test_presets_follow_settings(window):
    window.preset.setCurrentIndex(window.preset.findData("builtin:no-drums"))
    assert window.mode.currentData() == "stems" and window._mix() == ["bass", "other"]
    window.mix_boxes["drums"].setChecked(True)  # поменяли руками — пресет больше не совпадает
    assert window.preset.currentData() is None
    window.mode.setCurrentIndex(window.mode.findData("backing"))
    assert window.preset.currentData() == "builtin:backing"
    # свой пресет запоминает и качество с форматом
    window._store_user_presets({"Мой": {"mode": "vocals", "mix": [], "quality": "fast", "format": "flac"}})
    window._fill_presets()
    window.preset.setCurrentIndex(window.preset.findData("user:Мой"))
    assert (window.mode.currentData(), window.quality.currentData(), window.format.currentText()) == ("vocals", "fast", "flac")
    assert window.delete_preset_button.isEnabled()


def test_cancel_keeps_finished_tracks(window, tmp_path):
    first, second = fake_run(window, tmp_path, ["a/Song.mp3", "b/Song.mp3"], lyrics=True)
    window._on_event({"event": "start", "index": 0, "stem": "Song"})
    window._on_event(split_event(tmp_path, 0, "Song"))
    window._on_event({"event": "start", "index": 1, "stem": "Song (2)"})
    window._cancelled = True
    window._on_exit(1)
    # у первой песни дорожки уже записаны — они остаются, хоть текст и не распознан
    assert first.data(0, STATE_ROLE) == DONE and first.childCount() == 2
    assert second.data(0, STATE_ROLE) == QUEUED and second.text(1) == "Отменено"


def test_file_rows_can_be_dragged_out(window, tmp_path):
    (song,) = fake_run(window, tmp_path, ["Song.mp3"])
    window._on_event({"event": "start", "index": 0, "stem": "Song"})
    window._on_event(split_event(tmp_path, 0, "Song"))
    window._on_event({"event": "done", "index": 0})
    rows = [song.child(i) for i in range(song.childCount())]
    assert [row.text(0) for row in rows] == ["минус — Song_instrumental.mp3", "вокал — Song_vocals.mp3"]
    assert len(window.queue.mimeData([song]).urls()) == 2  # песня целиком — все её дорожки
    assert window.queue.mimeData([rows[1]]).urls()[0].toLocalFile().endswith("Song_vocals.mp3")
    assert rows[0].data(0, FILE_ROLE).endswith("Song_instrumental.mp3")


def test_error_offers_report(window, tmp_path):
    window.show()
    (song,) = fake_run(window, tmp_path, ["Song.mp3"])
    window._on_event({"event": "device", "gpu": False, "name": "GTX 650"})
    window._on_event({"event": "error", "index": 0, "message": "CUDA error: no kernel image"})
    assert song.data(0, STATE_ROLE) == FAILED and song.data(0, ERROR_ROLE)
    song.setSelected(True)
    assert window.report_button.isVisible()
    window._copy_selected_report()
    report = QApplication.clipboard().text()
    assert "no kernel image" in report and "GTX 650" in report


def test_format_duration():
    assert format_duration(20) == "меньше минуты"
    assert format_duration(150) == "≈ 2 мин"
    assert format_duration(3900) == "≈ 1 ч 05 мин"
