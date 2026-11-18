"""Разделение трека на дорожки поверх audio-separator."""

import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SplitterError
from .ffmpeg import encode, ensure_ffmpeg
from .paths import models_dir

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a"}

# Режимы: минус + вокал; 4 дорожки (FR-12); основной голос и бэк-вокал отдельно (FR-13).
MODES = ("vocals", "stems", "backing")
MIX_STEMS = ("drums", "bass", "other")

# FR-4: «быстро» — Demucs htdemucs, «качественно» — BS-Roformer (SDR ~12.9) / htdemucs_ft.
MODELS = {
    "vocals": {"fast": "htdemucs.yaml", "high": "model_bs_roformer_ep_317_sdr_12.9755.ckpt"},
    "stems": {"fast": "htdemucs.yaml", "high": "htdemucs_ft.yaml"},
}
MODELS["backing"] = MODELS["vocals"]
# Делит вокальную дорожку на основной голос и подпевки.
KARAOKE_MODEL = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"

_STEM_RE = re.compile(r"_\(([^)]+)\)_")


@dataclass
class Options:
    fmt: str = "mp3"
    bitrate: int = 320
    quality: str = "high"
    mode: str = "vocals"
    mix: tuple[str, ...] = MIX_STEMS  # какие дорожки смешать в минус в режиме stems
    lyrics: bool = False
    language: str | None = None  # None — определить автоматически
    lyrics_online: bool = False  # сначала искать готовый текст в LRCLIB (FR-17)

    def validate(self) -> None:
        if self.mode not in MODES:
            raise SplitterError(f"Неизвестный режим: {self.mode}. Доступно: {', '.join(MODES)}.")
        if self.quality not in ("fast", "high"):
            raise SplitterError(f"Неизвестное качество: {self.quality}. Доступно: fast, high.")
        unknown = set(self.mix) - set(MIX_STEMS)
        if unknown:
            raise SplitterError(f"Неизвестные дорожки для минуса: {', '.join(sorted(unknown))}. Доступно: {', '.join(MIX_STEMS)}.")


@dataclass
class SplitResult:
    outputs: dict[str, Path] = field(default_factory=dict)  # имя дорожки -> файл
    vocals_wav: Path | None = None  # вокал для распознавания текста (если просили сохранить)


def part_path(dst: Path) -> Path:
    return dst.with_name(dst.name + ".part")


def output_names(options: Options) -> list[str]:
    """Какие дорожки появятся на выходе при данных настройках."""
    if options.mode == "stems":
        names = ["vocals", "drums", "bass", "other"]
        return names + ["instrumental"] if options.mix else names
    if options.mode == "backing":
        return ["instrumental", "instrumental_backing", "lead", "backing"]
    return ["instrumental", "vocals"]


_gpu_usable: bool | None = None


def gpu_available() -> bool:
    """Годится ли видеокарта для расчётов. Если нет — переводит torch на процессор.

    Видеокарта может определяться, но не поддерживаться сборкой PyTorch (старые GTX, слишком новые модели):
    тогда расчёт падает с «CUDA error: no kernel image is available». Проверяем настоящим вычислением.
    """
    global _gpu_usable
    if _gpu_usable is not None:
        return _gpu_usable
    import torch

    usable = False
    if torch.cuda.is_available():
        try:
            probe = torch.ones(1, 1, 8, 8, device="cuda")
            torch.nn.functional.conv2d(probe, probe[:, :, :3, :3]).sum().item()
            usable = True
        except Exception:
            usable = False
    if not usable:
        # audio-separator и Whisper выбирают устройство по этой функции
        torch.cuda.is_available = lambda: False
    _gpu_usable = usable
    return usable


def device_description() -> str:
    """Видеокарта и драйвер одной строкой — для журнала и отчёта об ошибке."""
    import subprocess

    import torch

    usable = gpu_available()
    try:
        if not torch.cuda.device_count():
            return "видеокарта NVIDIA не найдена, расчёт на процессоре"
        name = torch.cuda.get_device_name(0)
        major, minor = torch.cuda.get_device_capability(0)
    except Exception as exc:
        return f"видеокарта не опознана ({type(exc).__name__}), расчёт на процессоре"
    driver = ""
    try:
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        query = ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]
        driver = subprocess.run(query, capture_output=True, text=True, timeout=5, creationflags=flags).stdout.strip()
    except Exception:
        pass
    status = "используется" if usable else "не поддерживается сборкой PyTorch, расчёт на процессоре"
    details = f"sm_{major}{minor}, CUDA {torch.version.cuda}" + (f", драйвер {driver.splitlines()[0]}" if driver else "")
    return f"{name} ({details}) — {status}"


def unique_stems(paths: list[Path]) -> list[str]:
    """Имена для результатов: одинаковые имена из разных папок получают суффикс « (2)», « (3)»…"""
    taken: set[str] = set()
    stems = []
    for path in paths:
        stem, number = path.stem, 2
        candidate = stem
        while candidate.lower() in taken:
            candidate = f"{stem} ({number})"
            number += 1
        taken.add(candidate.lower())
        stems.append(candidate)
    return stems


class Splitter:
    """Держит загруженные модели, чтобы в пакетном режиме не грузить их на каждый файл."""

    def __init__(self, options: Options):
        options.validate()
        self.options = options
        gpu_available()  # до загрузки моделей: при негодной видеокарте считаем на процессоре
        ensure_ffmpeg()
        self._workdir = Path(tempfile.mkdtemp(prefix="splitter_"))
        self._scratch = self._workdir / "scratch"
        self._keep = self._workdir / "keep"
        self._scratch.mkdir()
        self._keep.mkdir()
        self._separators = [self._load(MODELS[options.mode][options.quality])]
        if options.mode == "backing":
            self._separators.append(self._load(KARAOKE_MODEL))

    def _load(self, model: str):
        from audio_separator.separator import Separator

        separator = Separator(
            # WARNING-и библиотеки про onnxruntime к нашим torch-моделям не относятся
            log_level=logging.ERROR,
            model_file_dir=str(models_dir()),
            output_dir=str(self._scratch),
            output_format="WAV",
            # без нормализации: дорожки должны в сумме давать оригинал
            normalization_threshold=1.0,
        )
        try:
            separator.load_model(model_filename=model)
        except Exception as exc:
            raise SplitterError(
                f"Не удалось загрузить модель {model}: {exc}\n"
                "При первом запуске нужен интернет — модель скачивается один раз."
            ) from exc
        return separator

    def release_models(self) -> None:
        """Освобождает видеопамять (перед распознаванием текста)."""
        import gc

        import torch

        self._separators.clear()
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def close(self) -> None:
        self._separators.clear()
        shutil.rmtree(self._workdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def split(self, src: Path, out_dir: Path, keep_vocals: bool = False, stem: str | None = None) -> SplitResult:
        """stem — начало имён выходных файлов (по умолчанию имя исходного файла)."""
        import librosa
        import numpy as np
        import soundfile as sf

        src = Path(src)
        stem = stem or src.stem
        options = self.options
        if not src.is_file():
            raise SplitterError(f"Файл не найден: {src}")
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            stems, rate = self._separate(self._separators[0], src)
            if "vocals" not in stems or len(stems) < 2:
                raise SplitterError(f"Модель не вернула нужные дорожки для {src.name} (получено: {', '.join(stems) or 'ничего'}).")

            original, _ = librosa.load(src, sr=rate, mono=False)
            original = np.atleast_2d(original).T
            if original.shape[1] == 1:
                original = np.repeat(original, 2, axis=1)
            stems = _restore_levels(original, stems)
            vocals = stems["vocals"]
            rest = [audio for name, audio in stems.items() if name != "vocals"]

            if options.mode == "stems":
                tracks = {name: stems[name] for name in ("vocals", "drums", "bass", "other") if name in stems}
                if options.mix:
                    tracks["instrumental"] = sum(stems[name] for name in options.mix if name in stems)
            elif options.mode == "backing":
                vocals_wav = self._scratch / "vocals_for_karaoke.wav"
                sf.write(vocals_wav, vocals, rate, subtype="FLOAT")
                parts, _ = self._separate(self._separators[1], vocals_wav)
                if set(parts) != {"vocals", "instrumental"}:
                    raise SplitterError(f"Модель бэк-вокала вернула неожиданные дорожки: {', '.join(parts)}.")
                parts = _restore_levels(vocals, parts)
                instrumental = sum(rest)
                shared = min(len(instrumental), len(parts["instrumental"]))
                tracks = {
                    "instrumental": instrumental,
                    "instrumental_backing": instrumental[:shared] + parts["instrumental"][:shared],
                    "lead": parts["vocals"],
                    "backing": parts["instrumental"],
                }
            else:
                tracks = {"instrumental": sum(rest), "vocals": vocals}

            # одинаково приглушаем все дорожки, если хоть одна клиппирует
            peak = max(float(np.abs(audio).max()) for audio in tracks.values())
            common = min(1.0, 0.999 / peak) if peak > 0 else 1.0

            result = SplitResult()
            if keep_vocals:
                result.vocals_wav = self._keep / f"{len(list(self._keep.iterdir()))}.wav"
                sf.write(result.vocals_wav, vocals * common, rate, subtype="PCM_16")

            # пишем во временные .part и подменяем разом: отмена не оставит половину результата
            targets = {name: out_dir / f"{stem}_{name}.{options.fmt}" for name in tracks}
            try:
                for name, audio in tracks.items():
                    wav = self._scratch / f"final_{name}.wav"
                    sf.write(wav, (audio * common).astype(np.float32), rate, subtype="FLOAT")
                    encode(wav, part_path(targets[name]), options.fmt, options.bitrate, tags_from=src)
                for dst in targets.values():
                    part_path(dst).replace(dst)
            finally:
                for dst in targets.values():
                    part_path(dst).unlink(missing_ok=True)
            result.outputs = targets
            return result
        finally:
            for leftover in self._scratch.iterdir():
                leftover.unlink(missing_ok=True)

    def _separate(self, separator, src: Path) -> tuple[dict, int]:
        """Запускает модель и читает дорожки: {имя: массив (n, 2)}, частота."""
        import soundfile as sf

        try:
            produced = separator.separate(str(src))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            if "out of memory" in detail.lower():
                hint = "не хватило видеопамяти — закройте игры и другие программы, занимающие видеокарту"
            elif "CUDA" in detail:
                hint = "сбой видеокарты"
            else:
                hint = "файл повреждён или не аудио?"
            raise SplitterError(f"Не удалось обработать {src.name} ({hint}): {detail}") from exc

        stems, rate = {}, 44100
        for name in produced:
            path = Path(name)
            if not path.is_absolute():
                path = self._scratch / path
            match = _STEM_RE.search(path.name)
            if match:
                stems[match.group(1).lower()], rate = sf.read(path, dtype="float32", always_2d=True)
        return stems, rate


def _restore_levels(reference, stems: dict) -> dict:
    """Возвращает дорожкам исходные уровни громкости.

    audio-separator нормализует каждую дорожку по отдельности, из-за чего их сумма
    перестаёт совпадать с оригиналом. Подбираем множитель для каждой (МНК по оригиналу).
    """
    import numpy as np

    names = list(stems)
    length = min(len(reference), *(len(stems[name]) for name in names))
    matrix = np.stack([stems[name][:length].ravel() for name in names], axis=1).astype(np.float64)
    gains, *_ = np.linalg.lstsq(matrix, reference[:length].ravel().astype(np.float64), rcond=None)
    if not all(0.5 <= gain <= 4.0 for gain in gains):
        gains = np.ones(len(names))  # подбор не удался — оставляем как есть
    return {name: stems[name][:length] * np.float32(gain) for name, gain in zip(names, gains)}
