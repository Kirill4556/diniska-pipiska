"""Быстрые тесты без нейросети: разбор аргументов, сбор файлов, кодирование."""

import subprocess

import numpy as np
import pytest
import soundfile as sf

from pathlib import Path

from splitter.cli import build_parser, collect_inputs, resolve_mode
from splitter.core import Options, output_names, unique_stems
from splitter.errors import SplitterError
from splitter.ffmpeg import encode, ensure_ffmpeg
from splitter.lyrics import format_lrc, lrc_to_text, save_lyrics
from splitter.presets import builtin, matches
from splitter.report import KEEP_LOGS, RunLog, build_report


def test_defaults():
    args = build_parser().parse_args(["song.mp3"])
    assert (args.format, args.bitrate, args.quality) == ("mp3", 320, "high")
    assert resolve_mode(args) == ("vocals", ("drums", "bass", "other"))
    assert not args.lyrics and args.language == "auto"


def test_mix_argument():
    parse = build_parser().parse_args
    assert parse(["s.mp3", "-m", "stems", "--mix", "drums, other"]).mix == ("drums", "other")
    assert parse(["s.mp3", "--mix", "none"]).mix == ()
    with pytest.raises(SystemExit):
        parse(["s.mp3", "--mix", "guitar"])


def test_presets_and_explicit_arguments():
    parse = build_parser().parse_args
    assert resolve_mode(parse(["s.mp3", "--preset", "no-drums"])) == ("stems", ("bass", "other"))
    assert resolve_mode(parse(["s.mp3", "-p", "backing"]))[0] == "backing"
    # явные параметры важнее пресета
    assert resolve_mode(parse(["s.mp3", "-p", "no-drums", "--mix", "drums"])) == ("stems", ("drums",))
    assert resolve_mode(parse(["s.mp3", "-p", "no-bass", "-m", "vocals"]))[0] == "vocals"
    with pytest.raises(SystemExit):
        parse(["s.mp3", "--preset", "karaoke"])


def test_preset_matching():
    current = {"mode": "stems", "mix": ["other", "bass"], "quality": "fast"}
    assert matches(builtin("no-drums"), current)
    assert not matches(builtin("no-bass"), current)
    assert matches(builtin("vocals"), {"mode": "vocals", "mix": []})  # состав минуса вне stems не важен


def test_unique_stems_for_same_names(tmp_path):
    paths = [tmp_path / "a" / "Song.mp3", tmp_path / "b" / "song.flac", tmp_path / "c" / "Song.mp3", tmp_path / "Other.mp3"]
    assert unique_stems(paths) == ["Song", "song (2)", "Song (3)", "Other"]


def test_report_hides_folders():
    message = r"Не удалось обработать C:\Users\Кирилл\Music\Альбом\песня.mp3: ошибка в /home/user/music/x.wav"
    report = build_report(message, {"mode": "vocals"}, "RTX 4060", [r"start C:\Users\Кирилл\a b\c.mp3"])
    assert "Кирилл" not in report and "user" not in report
    assert "песня.mp3" in report and "x.wav" in report and "c.mp3" in report
    assert "RTX 4060" in report and "mode=vocals" in report


def test_run_log_keeps_last_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLITTER_CACHE", str(tmp_path))
    for number in range(KEEP_LOGS + 2):
        log = RunLog("test")
        log.write(f"строка {number}")
        log.write(" 45%|████▌     | 9/20 [00:01<00:01]")  # прогресс tqdm в журнал не попадает
    assert len(list((tmp_path / "logs").glob("*.log"))) == KEEP_LOGS
    assert log.tail()[-1].endswith(f"строка {KEEP_LOGS + 1}") and len(log.tail()) == 2


def test_collect_inputs_scans_folders_and_keeps_urls(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("a.mp3", "sub/b.FLAC", "notes.txt"):
        (tmp_path / name).write_bytes(b"")
    url = "https://example.com/track"
    items = collect_inputs([str(tmp_path), url])
    assert [Path(i).name for i in items[:2]] == ["a.mp3", "b.FLAC"] and items[2] == url


def test_collect_inputs_rejects_missing_and_unsupported(tmp_path):
    with pytest.raises(SplitterError):
        collect_inputs([str(tmp_path / "nope.mp3")])
    (tmp_path / "doc.txt").write_bytes(b"")
    with pytest.raises(SplitterError):
        collect_inputs([str(tmp_path / "doc.txt")])


def test_output_names_per_mode():
    assert output_names(Options()) == ["instrumental", "vocals"]
    assert output_names(Options(mode="stems", mix=())) == ["vocals", "drums", "bass", "other"]
    assert output_names(Options(mode="stems")) == ["vocals", "drums", "bass", "other", "instrumental"]
    assert output_names(Options(mode="backing")) == ["instrumental", "instrumental_backing", "lead", "backing"]
    with pytest.raises(SplitterError):
        Options(mode="karaoke").validate()


def test_clean_part_choice():
    from splitter.core import _clean_part

    assert _clean_part({"noreverb": 1, "reverb": 2}) == 1
    assert _clean_part({"no echo": 1, "echo": 2}) == 1
    assert _clean_part({"dry": 1, "noise": 2}) == 1
    assert _clean_part({"vocals": 1, "noise": 2}) == 1
    assert _clean_part({"a": 1, "b": 2}) is None  # непонятно — не угадываем


def test_average_of_models():
    from splitter.core import _average

    first = {"vocals": np.ones((10, 2)), "instrumental": np.zeros((10, 2))}
    second = {"vocals": np.zeros((8, 2)), "instrumental": np.ones((8, 2))}
    mean = _average([first, second])
    assert mean["vocals"].shape == (8, 2) and np.allclose(mean["vocals"], 0.5) and np.allclose(mean["instrumental"], 0.5)
    assert _average([first]) is first


def test_clean_and_max_options():
    assert output_names(Options(clean_vocals=True)) == ["instrumental", "vocals", "vocals_clean"]
    assert output_names(Options(mode="backing", clean_vocals=True))[-1] == "lead_clean"
    Options(quality="max").validate()
    assert build_parser().parse_args(["s.mp3", "-q", "max", "--clean-vocals"]).clean_vocals


def test_lyrics_files(tmp_path):
    lrc = format_lrc([(0.0, " Первая строка "), (75.5, "Second line")])
    assert lrc == "[00:00.00]Первая строка\n[01:15.50]Second line\n"
    assert lrc_to_text(lrc) == "Первая строка\nSecond line\n"
    txt_path, lrc_path = save_lyrics(lrc, tmp_path, "song")
    assert (txt_path.name, lrc_path.name) == ("song.txt", "song.lrc")
    assert txt_path.read_text(encoding="utf-8") == "Первая строка\nSecond line\n"


@pytest.mark.parametrize("fmt", ["mp3", "flac", "wav"])
def test_encode_keeps_duration_and_tags(tmp_path, fmt):
    ffmpeg = ensure_ffmpeg()
    tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(44100 * 2) / 44100)).astype(np.float32)
    wav = tmp_path / "stem.wav"
    sf.write(wav, np.stack([tone, tone], axis=1), 44100)
    tagged = tmp_path / "source.mp3"
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(wav), "-metadata", "title=Песня", "-metadata", "artist=Автор", str(tagged)],
        check=True,
    )

    out = tmp_path / f"out.{fmt}"
    encode(wav, out.with_name(out.name + ".part"), fmt, 192, tags_from=tagged)  # как в core: через .part
    out.with_name(out.name + ".part").replace(out)

    info = subprocess.run([ffmpeg, "-i", str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    assert "Песня" in info and "Автор" in info
    assert abs(sf.info(out).duration - 2.0) < 0.05
