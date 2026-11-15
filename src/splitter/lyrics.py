"""Текст песни: распознавание по вокальной дорожке (Whisper), вывод .txt и .lrc (FR-11)."""

import re
from pathlib import Path

from .errors import SplitterError
from .paths import cache_dir

LANGUAGES = {"auto": None, "ru": "ru", "en": "en"}

_TIMECODE_RE = re.compile(r"^\s*(\[\d+:\d+(?:[.:]\d+)?\])+\s*")


def format_lrc(lines: list[tuple[float, str]]) -> str:
    """[(секунды, строка)] -> текст .lrc с таймкодами [мм:сс.сс]."""
    out = []
    for start, text in lines:
        minutes, seconds = divmod(max(start, 0.0), 60)
        out.append(f"[{int(minutes):02d}:{seconds:05.2f}]{text.strip()}")
    return "\n".join(out) + "\n"


def lrc_to_text(lrc: str) -> str:
    """Убирает таймкоды: из .lrc получается обычный текст."""
    lines = (_TIMECODE_RE.sub("", line).rstrip() for line in lrc.splitlines())
    return "\n".join(lines).strip() + "\n"


def lyrics_paths(out_dir: Path, stem: str) -> tuple[Path, Path]:
    return out_dir / f"{stem}.txt", out_dir / f"{stem}.lrc"


def save_lyrics(lrc: str, out_dir: Path, stem: str) -> tuple[Path, Path]:
    """Пишет .lrc и соответствующий ему .txt; возвращает (txt, lrc)."""
    txt_path, lrc_path = lyrics_paths(out_dir, stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    lrc_path.write_text(lrc, encoding="utf-8")
    txt_path.write_text(lrc_to_text(lrc), encoding="utf-8")
    return txt_path, lrc_path


class Transcriber:
    """Загруженная модель Whisper."""

    def __init__(self):
        import torch
        import whisper

        self._gpu = torch.cuda.is_available()
        # на процессоре большая модель слишком медленная
        name = "turbo" if self._gpu else "small"
        models = cache_dir() / "whisper"
        models.mkdir(exist_ok=True)
        try:
            self._model = whisper.load_model(name, download_root=str(models))
        except Exception as exc:
            raise SplitterError(
                f"Не удалось загрузить модель распознавания текста: {exc}\n"
                "При первом запуске нужен интернет — модель скачивается один раз."
            ) from exc

    def transcribe(self, vocals_wav: Path, language: str | None = None) -> list[tuple[float, str]]:
        """Возвращает строки текста с временем начала."""
        try:
            result = self._model.transcribe(
                str(vocals_wav),
                language=language,
                fp16=self._gpu,
                verbose=False,  # прогресс через tqdm, без печати текста
                condition_on_previous_text=False,  # меньше зацикливаний на проигрышах
            )
        except Exception as exc:
            raise SplitterError(f"Не удалось распознать текст: {str(exc) or type(exc).__name__}") from exc
        return [(seg["start"], seg["text"].strip()) for seg in result["segments"] if seg["text"].strip()]
