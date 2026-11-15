"""Быстрые тесты без нейросети: разбор аргументов, сбор файлов, кодирование."""

import subprocess

import numpy as np
import pytest
import soundfile as sf

from pathlib import Path

from splitter.cli import build_parser, collect_inputs
from splitter.core import Options, output_names
from splitter.errors import SplitterError
from splitter.ffmpeg import encode, ensure_ffmpeg
from splitter.lyrics import format_lrc, lrc_to_text, save_lyrics


def test_defaults():
    args = build_parser().parse_args(["song.mp3"])
    assert (args.format, args.bitrate, args.quality, args.mode) == ("mp3", 320, "high", "vocals")
    assert args.mix == ("drums", "bass", "other") and not args.lyrics and args.language == "auto"


def test_mix_argument():
    parse = build_parser().parse_args
    assert parse(["s.mp3", "-m", "stems", "--mix", "drums, other"]).mix == ("drums", "other")
    assert parse(["s.mp3", "--mix", "none"]).mix == ()
    with pytest.raises(SystemExit):
        parse(["s.mp3", "--mix", "guitar"])


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
