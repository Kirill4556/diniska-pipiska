"""Прослушивание и сведение готовых дорожек (FR-15).

Дорожки декодируются ffmpeg в память (float32, 44,1 кГц, стерео) и смешиваются на лету в одном потоке
вывода — поэтому они не расходятся по времени, а громкость и переключение «оригинал/микс» работают сразу.
"""

import subprocess
import threading
from pathlib import Path

import numpy as np

from .errors import SplitterError
from .ffmpeg import encode, ensure_ffmpeg

RATE = 44100
# эти дорожки — сами суммы других, в микшер их не берём, чтобы не удвоить звук
_SUMS = {"instrumental": ("drums", "bass", "other"), "instrumental_backing": ("instrumental", "backing")}


def load(path: Path) -> np.ndarray:
    """Декодирует любой поддерживаемый ffmpeg файл в массив (кадры, 2)."""
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    command = [ensure_ffmpeg(), "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(RATE), "-"]
    result = subprocess.run(command, capture_output=True, creationflags=flags)
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SplitterError(f"Не удалось прочитать {Path(path).name}: {detail or 'пустой файл'}")
    return np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2)


def mixer_tracks(names: list[str]) -> list[str]:
    """Какие дорожки показать в микшере: без тех, что сами составлены из других."""
    chosen = []
    for name in names:
        if name.endswith("_clean"):  # очищенный голос — другой вариант того же голоса
            continue
        parts = _SUMS.get(name)
        if parts and any(part in names for part in parts):
            continue
        chosen.append(name)
    return chosen


def render(tracks: dict[str, np.ndarray], gains: dict[str, float]) -> np.ndarray:
    length = min(len(audio) for audio in tracks.values())
    mix = np.zeros((length, 2), dtype=np.float32)
    for name, audio in tracks.items():
        gain = gains.get(name, 1.0)
        if gain:
            mix += audio[:length] * np.float32(gain)
    return mix


def save_mix(mix: np.ndarray, dst: Path, fmt: str, bitrate: int, tags_from: Path | None = None) -> Path:
    """Сохраняет микс; если громко до клиппинга — приглушает целиком."""
    import tempfile

    import soundfile as sf

    peak = float(np.abs(mix).max()) if len(mix) else 0.0
    if peak > 0.999:
        mix = mix * np.float32(0.999 / peak)
    with tempfile.TemporaryDirectory(prefix="splitter_mix_") as folder:
        wav = Path(folder) / "mix.wav"
        sf.write(wav, mix, RATE, subtype="FLOAT")
        part = dst.with_name(dst.name + ".part")
        try:
            encode(wav, part, fmt, bitrate, tags_from=tags_from)
            part.replace(dst)
        finally:
            part.unlink(missing_ok=True)
    return dst


class MixPlayer:
    """Плеер нескольких синхронных дорожек и (по желанию) оригинала. Потокобезопасен."""

    def __init__(self, tracks: dict[str, np.ndarray], original: np.ndarray | None = None):
        if not tracks:
            raise SplitterError("Нет дорожек для прослушивания.")
        self.length = min(len(audio) for audio in tracks.values())
        self._tracks = {name: audio[: self.length] for name, audio in tracks.items()}
        self._original = None
        if original is not None:
            padded = np.zeros((self.length, 2), dtype=np.float32)
            shared = min(self.length, len(original))
            padded[:shared] = original[:shared]
            self._original = padded
        self.gains = {name: 1.0 for name in tracks}
        self.solo_original = False
        self._position = 0
        self._stop_at: int | None = None
        self._playing = False
        self._lock = threading.Lock()
        self._stream = None

    @property
    def has_original(self) -> bool:
        return self._original is not None

    @property
    def duration(self) -> float:
        return self.length / RATE

    @property
    def position(self) -> float:
        return self._position / RATE

    @property
    def playing(self) -> bool:
        return self._playing

    def _ensure_stream(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice

            self._stream = sounddevice.OutputStream(samplerate=RATE, channels=2, dtype="float32", callback=self._fill, latency="low")
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise SplitterError(f"Не удалось открыть звуковое устройство: {exc}") from exc

    def _fill(self, out, frames, _time, _status) -> None:
        with self._lock:
            out.fill(0)
            if not self._playing:
                return
            start = self._position
            end = min(start + frames, self.length if self._stop_at is None else min(self._stop_at, self.length))
            count = max(end - start, 0)
            if count:
                if self.solo_original and self._original is not None:
                    out[:count] = self._original[start:end]
                else:
                    for name, audio in self._tracks.items():
                        gain = self.gains.get(name, 0.0)
                        if gain:
                            out[:count] += audio[start:end] * np.float32(gain)
                np.clip(out, -1.0, 1.0, out=out)
            self._position = end
            if count < frames:  # дошли до конца или до точки остановки
                self._playing = False
                self._stop_at = None

    def play(self, start: float | None = None, stop: float | None = None) -> None:
        self._ensure_stream()
        with self._lock:
            if start is not None:
                self._position = int(min(max(start, 0.0), self.duration) * RATE)
            if self._position >= self.length:
                self._position = 0
            self._stop_at = int(stop * RATE) if stop is not None else None
            self._playing = True

    def pause(self) -> None:
        with self._lock:
            self._playing = False
            self._stop_at = None

    def seek(self, seconds: float) -> None:
        with self._lock:
            self._position = int(min(max(seconds, 0.0), self.duration) * RATE)

    def mix(self) -> np.ndarray:
        return render(self._tracks, self.gains)

    def close(self) -> None:
        with self._lock:
            self._playing = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
