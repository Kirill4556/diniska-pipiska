"""Прослушивание, микс, редактор текста и готовые тексты — без звуковой карты и сети."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtWidgets import QApplication

from splitter.audio import RATE, MixPlayer, load, mixer_tracks, render, save_mix
from splitter.lrclib import clean_title, normalize, track_info
from splitter.lyrics import format_lrc, parse_lrc
from splitter.player_ui import LyricsEditor, MixerDialog, parse_clock


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def tone(freq: float, seconds: float = 1.0, level: float = 0.2) -> np.ndarray:
    wave = (level * np.sin(2 * np.pi * freq * np.arange(int(RATE * seconds)) / RATE)).astype(np.float32)
    return np.stack([wave, wave], axis=1)


def test_mixer_skips_tracks_made_of_others():
    assert mixer_tracks(["vocals", "drums", "bass", "other", "instrumental"]) == ["vocals", "drums", "bass", "other"]
    assert mixer_tracks(["instrumental", "vocals"]) == ["instrumental", "vocals"]
    assert mixer_tracks(["instrumental", "instrumental_backing", "lead", "backing"]) == ["instrumental", "lead", "backing"]


def test_player_mixes_with_gains_and_switches_to_original():
    a, b = tone(220), tone(330)
    player = MixPlayer({"a": a, "b": b}, original=a + b)
    player.gains = {"a": 1.0, "b": 0.0}
    out = np.zeros((512, 2), dtype=np.float32)
    player._playing = True
    player._fill(out, 512, None, None)
    assert np.allclose(out, a[:512]) and player._position == 512
    player.solo_original = True
    player._fill(out, 512, None, None)
    assert np.allclose(out, (a + b)[512:1024])
    # остановка в заданной точке (прослушивание одной строки текста)
    player.solo_original = False
    player._stop_at = 1100
    player._fill(out, 512, None, None)
    assert not player.playing and player._position == 1100 and not out[100:].any()


def test_full_mix_equals_sum_and_saves(tmp_path):
    a, b = tone(220), tone(330)
    assert np.allclose(render({"a": a, "b": b}, {"a": 1.0, "b": 1.0}), a + b)
    loud = render({"a": a * 4, "b": b * 4}, {"a": 1.0, "b": 1.0})
    path = save_mix(loud, tmp_path / "song_mix.flac", "flac", 320)
    saved, rate = sf.read(path, dtype="float32")
    assert rate == RATE and np.abs(saved).max() <= 1.0 and abs(len(saved) - len(loud)) < 10
    assert np.allclose(load(path)[:1000], saved[:1000], atol=1e-4)


def test_lrc_parsing_and_offset():
    lrc = "[ar:Кто-то]\n[offset:500]\n[00:10.00]Первая\n[00:05.50]Вторая\nбез времени\n"
    assert parse_lrc(lrc) == [(5.5, "Вторая"), (10.0, "Первая")]
    assert normalize(lrc) == [(5.0, "Вторая"), (9.5, "Первая")]  # offset +500 мс — текст на полсекунды раньше


def test_titles_and_names(tmp_path):
    assert clean_title("Песня (Official Video) [4K]") == "Песня"
    assert clean_title("Song (Live at Home)") == "Song (Live at Home)"
    wav = tmp_path / "Исполнитель - Песня (Lyric Video).wav"
    sf.write(wav, tone(220, 2.5), RATE)
    info = track_info(wav)
    assert (info.artist, info.title) == ("Исполнитель", "Песня") and abs(info.duration - 2.5) < 0.05


def test_parse_clock():
    assert parse_clock("1:05.30") == pytest.approx(65.3)
    assert parse_clock("12,5") == pytest.approx(12.5)
    assert parse_clock("abc") is None


def test_lyrics_editor_saves_edited_lines(app, tmp_path):
    lrc = tmp_path / "song.lrc"
    lrc.write_text(format_lrc([(1.0, "раз"), (3.0, "два")]), encoding="utf-8")
    editor = LyricsEditor(lrc, {})
    editor.table.selectRow(1)
    editor._shift(0.5)
    editor.table.item(0, 1).setText("один")
    editor.accept()
    assert parse_lrc(lrc.read_text(encoding="utf-8")) == [(1.0, "один"), (3.5, "два")]
    assert (tmp_path / "song.txt").read_text(encoding="utf-8") == "один\nдва\n"
    # неверное время не сохраняется
    editor = LyricsEditor(lrc, {})
    editor.table.item(0, 0).setText("ой")
    assert editor.lines() is None


def test_mixer_dialog_saves_mix(app, tmp_path):
    tracks = {}
    for name, freq in (("instrumental", 220), ("vocals", 440)):
        tracks[name] = tmp_path / f"song_{name}.flac"
        sf.write(tracks[name], tone(freq), RATE)
    dialog = MixerDialog("song", tracks, {}, None, tmp_path / "song", 320)
    arrays = {name: load(path) for name, path in tracks.items()}
    dialog._on_loaded(arrays)  # вместо фоновой загрузки
    dialog.mutes["vocals"].setChecked(True)
    dialog._save()
    saved, _ = sf.read(tmp_path / "song_mix.flac", dtype="float32")
    assert np.allclose(saved, arrays["instrumental"][: len(saved)], atol=1e-3)
    dialog.close()
