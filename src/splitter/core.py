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
from .i18n import tr

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a"}

# Режимы: минус + вокал; 4 дорожки (FR-12); основной голос и бэк-вокал отдельно (FR-13).
MODES = ("vocals", "stems", "backing")
MIX_STEMS = ("drums", "bass", "other")

QUALITIES = ("fast", "high", "max")
BS_ROFORMER = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"  # лучший минус (SDR 16,45)
KIM_ROFORMER = "vocals_mel_band_roformer.ckpt"  # лучший вокал (SDR 12,60)
# FR-4: «быстро» — Demucs htdemucs, «качественно» — BS-Roformer / htdemucs_ft.
# FR-20: «максимум» — несколько моделей, результаты усредняются.
MODELS = {
    "vocals": {"fast": ("htdemucs.yaml",), "high": (BS_ROFORMER,), "max": (BS_ROFORMER, KIM_ROFORMER)},
    "stems": {"fast": ("htdemucs.yaml",), "high": ("htdemucs_ft.yaml",), "max": ("htdemucs_ft.yaml",)},
}
MODELS["backing"] = MODELS["vocals"]
# сколько раз Demucs прогоняет трек со случайным сдвигом и усредняет (больше — чище, но дольше)
DEMUCS_SHIFTS = {"fast": 1, "high": 2, "max": 4}
# Делит вокальную дорожку на основной голос и подпевки.
KARAOKE_MODEL = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"
# FR-16: очистка вокала — сначала реверберация и эхо, потом шум.
CLEAN_MODELS = ("dereverb_echo_mbr_fused.ckpt", "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt")

_STEM_RE = re.compile(r"_\(([^)]+)\)_")

# Скорость моделей Roformer (замер на RTX 4060, трек 4 минуты, BS-Roformer):
#   по умолчанию (overlap из модели = 4)       264 с
#   половинная точность (autocast)             130 с, отличие от эталона −73 дБ — тот же результат
#   autocast + overlap 2                        69 с, отличие −43 дБ — ниже шума mp3, на слух не отличить
# «Качественно» — autocast + overlap 2; «максимум» — только autocast (полное перекрытие).
MDXC_OVERLAP = {"fast": 2, "high": 2, "max": None}  # None — как в настройках модели


def speed_settings(quality: str) -> dict:
    """Параметры Separator для CUDA. На процессоре и DirectML половинная точность не используется."""
    return {
        "use_autocast": True,
        "mdxc_params": {
            "segment_size": 256,
            "override_model_segment_size": False,
            "batch_size": None,
            "overlap": MDXC_OVERLAP[quality],
            "pitch_shift": 0,
        },
    }


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
    clean_vocals: bool = False  # дополнительно очищенный от эха и шума вокал (FR-16)

    def validate(self) -> None:
        if self.mode not in MODES:
            raise SplitterError(tr("Неизвестный режим: {0}. Доступно: {1}.").format(self.mode, ', '.join(MODES)))
        if self.quality not in QUALITIES:
            raise SplitterError(tr("Неизвестное качество: {0}. Доступно: {1}.").format(self.quality, ', '.join(QUALITIES)))
        unknown = set(self.mix) - set(MIX_STEMS)
        if unknown:
            raise SplitterError(tr("Неизвестные дорожки для минуса: {0}. Доступно: {1}.").format(', '.join(sorted(unknown)), ', '.join(MIX_STEMS)))


@dataclass
class SplitResult:
    outputs: dict[str, Path] = field(default_factory=dict)  # имя дорожки -> файл
    vocals_wav: Path | None = None  # вокал для распознавания текста (если просили сохранить)


def part_path(dst: Path) -> Path:
    return dst.with_name(dst.name + ".part")


def output_names(options: Options) -> list[str]:
    """Какие дорожки появятся на выходе при данных настройках."""
    if options.mode == "stems":
        names = ["vocals", "drums", "bass", "other"] + (["instrumental"] if options.mix else [])
    elif options.mode == "backing":
        names = ["instrumental", "instrumental_backing", "lead", "backing"]
    else:
        names = ["instrumental", "vocals"]
    if options.clean_vocals:
        names.append(f"{clean_target(options)}_clean")
    return names


def clean_target(options: Options) -> str:
    """Какой голос очищать: в режиме бэк-вокала — основной, иначе весь вокал."""
    return "lead" if options.mode == "backing" else "vocals"


_gpu_usable: bool | None = None


def cuda_available() -> bool:
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


def gpu_available() -> bool:
    """Есть ли чем ускорить разделение.

    DirectML (видеокарты AMD/Intel) проверялся и отклонён: на моделях разделения он не быстрее процессора
    (BS-Roformer, 20 с звука: 158 с на DirectML против 156 с на процессоре) — см. ТЗ, FR-31.
    """
    return cuda_available()


def accelerator() -> str:
    """Чем считать разделение: cuda или cpu."""
    return "cuda" if cuda_available() else "cpu"


def device_description() -> str:
    """Видеокарта и драйвер одной строкой — для журнала и отчёта об ошибке."""
    import subprocess

    import torch

    usable = cuda_available()
    try:
        if not torch.cuda.device_count():
            return tr("видеокарта NVIDIA не найдена, расчёт на процессоре")
        name = torch.cuda.get_device_name(0)
        major, minor = torch.cuda.get_device_capability(0)
    except Exception as exc:
        return tr("видеокарта не опознана ({0}), расчёт на процессоре").format(type(exc).__name__)
    driver = ""
    try:
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        query = ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]
        driver = subprocess.run(query, capture_output=True, text=True, timeout=5, creationflags=flags).stdout.strip()
    except Exception:
        pass
    status = tr("используется") if usable else tr("не поддерживается сборкой PyTorch, расчёт на процессоре")
    details = f"sm_{major}{minor}, CUDA {torch.version.cuda}" + (tr(", драйвер {0}").format(driver.splitlines()[0]) if driver else "")
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


# FR-25: наборы моделей для предзагрузки в мастере первого запуска: ключ -> (модели, примерный размер, МБ)
MODEL_PACKS = {
    "fast": (("htdemucs.yaml",), 80),
    "high": ((BS_ROFORMER,), 610),
    "stems": (("htdemucs_ft.yaml",), 320),
    "backing": ((KARAOKE_MODEL,), 871),
    "max": ((KIM_ROFORMER,), 871),
    "clean": (CLEAN_MODELS, 1306),
}


def prefetch_model(model: str) -> None:
    """Скачивает модель в кэш (если её ещё нет), не оставляя её в памяти."""
    import gc

    from audio_separator.separator import Separator

    gpu_available()
    ensure_ffmpeg()
    with tempfile.TemporaryDirectory(prefix="splitter_prefetch_") as folder:
        separator = Separator(log_level=logging.ERROR, model_file_dir=str(models_dir()), output_dir=folder)
        try:
            separator.load_model(model_filename=model)
        except Exception as exc:
            raise SplitterError(tr("Не удалось скачать модель {0}: {1}").format(model, exc)) from exc
        del separator
        gc.collect()


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
        self._main = [self._load(model) for model in MODELS[options.mode][options.quality]]
        self._karaoke = self._load(KARAOKE_MODEL) if options.mode == "backing" else None
        self._cleaners = [self._load(model) for model in CLEAN_MODELS] if options.clean_vocals else []

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
            **(speed_settings(self.options.quality) if accelerator() == "cuda" else {}),
            demucs_params={"segment_size": "Default", "shifts": DEMUCS_SHIFTS[self.options.quality], "overlap": 0.25, "segments_enabled": True},
        )
        try:
            separator.load_model(model_filename=model)
        except Exception as exc:
            raise SplitterError(
                tr("Не удалось загрузить модель {0}: {1}\nПри первом запуске нужен интернет — модель скачивается один раз.").format(model, exc)
            ) from exc
        return separator

    def release_models(self) -> None:
        """Освобождает видеопамять (перед распознаванием текста)."""
        import gc

        import torch

        self._main, self._karaoke, self._cleaners = [], None, []
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def close(self) -> None:
        self._main, self._karaoke, self._cleaners = [], None, []
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
            raise SplitterError(tr("Файл не найден: {0}").format(src))
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            results, rate = [], 44100
            for separator in self._main:
                stems, rate = self._separate(separator, src)
                if "vocals" not in stems or len(stems) < 2:
                    raise SplitterError(tr("Модель не вернула нужные дорожки для {0} (получено: {1}).").format(src.name, ', '.join(stems) or 'ничего'))
                if len(stems) == 2:  # у двухдорожечных моделей вторая дорожка — минус, как бы она ни называлась
                    stems = {"vocals": stems["vocals"], "instrumental": next(a for n, a in stems.items() if n != "vocals")}
                results.append(stems)

            original, _ = librosa.load(src, sr=rate, mono=False)
            original = np.atleast_2d(original).T
            if original.shape[1] == 1:
                original = np.repeat(original, 2, axis=1)
            # FR-20: у каждой модели свои уровни — выравниваем по оригиналу, потом усредняем
            stems = _average([_restore_levels(original, result) for result in results])
            vocals = stems["vocals"]
            rest = [audio for name, audio in stems.items() if name != "vocals"]

            if options.mode == "stems":
                tracks = {name: stems[name] for name in ("vocals", "drums", "bass", "other") if name in stems}
                if options.mix:
                    tracks["instrumental"] = sum(stems[name] for name in options.mix if name in stems)
            elif options.mode == "backing":
                vocals_wav = self._scratch / "vocals_for_karaoke.wav"
                sf.write(vocals_wav, vocals, rate, subtype="FLOAT")
                parts, _ = self._separate(self._karaoke, vocals_wav)
                if set(parts) != {"vocals", "instrumental"}:
                    raise SplitterError(tr("Модель бэк-вокала вернула неожиданные дорожки: {0}.").format(', '.join(parts)))
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

            if self._cleaners:
                target = clean_target(options)
                tracks[f"{target}_clean"] = self._clean(tracks[target], rate, src.name)

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

    def _clean(self, voice, rate: int, name: str):
        """FR-16: прогоняет голос через модели очистки (эхо/реверберация, затем шум)."""
        import numpy as np
        import soundfile as sf

        audio = voice
        for step, separator in enumerate(self._cleaners):
            wav = self._scratch / f"clean_{step}.wav"
            sf.write(wav, audio, rate, subtype="FLOAT")
            parts, _ = self._separate(separator, wav)
            kept = _clean_part(parts)
            if kept is None:
                raise SplitterError(tr("Модель очистки вернула неожиданные дорожки для {0}: {1}.").format(name, ', '.join(parts)))
            audio = kept
        # длина как у исходного голоса, чтобы дорожки совпадали по времени
        if len(audio) >= len(voice):
            return audio[: len(voice)]
        return np.concatenate([audio, np.zeros((len(voice) - len(audio), audio.shape[1]), dtype=audio.dtype)])

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
                hint = tr("не хватило видеопамяти — закройте игры и другие программы, занимающие видеокарту")
            elif "CUDA" in detail:
                hint = tr("сбой видеокарты")
            else:
                hint = tr("файл повреждён или не аудио?")
            raise SplitterError(tr("Не удалось обработать {0} ({1}): {2}").format(src.name, hint, detail)) from exc

        stems, rate = {}, 44100
        for name in produced:
            path = Path(name)
            if not path.is_absolute():
                path = self._scratch / path
            match = _STEM_RE.search(path.name)
            if match:
                stems[match.group(1).lower()], rate = sf.read(path, dtype="float32", always_2d=True)
        return stems, rate


_DIRTY_WORDS = ("reverb", "echo", "noise", "crowd")


def _clean_part(parts: dict):
    """Из дорожек модели очистки выбирает «чистую»: «без реверберации», «сухую», «без шума»."""
    for name, audio in parts.items():
        compact = name.replace(" ", "").replace("-", "").replace("_", "")
        # «noreverb», «no echo», «no_noise» — но не само слово «noise»
        if compact.startswith("no") and any(compact[2:].startswith(word) for word in _DIRTY_WORDS):
            return audio
    for preferred in ("dry", "vocals", "clean"):
        if preferred in parts:
            return parts[preferred]
    clean = [audio for name, audio in parts.items() if not any(word in name for word in _DIRTY_WORDS)]
    return clean[0] if len(clean) == 1 else None


def _average(results: list[dict]) -> dict:
    """Среднее нескольких разделений с одинаковыми дорожками."""
    if len(results) == 1:
        return results[0]
    names = [name for name in results[0] if all(name in result for result in results)]
    length = min(len(result[name]) for result in results for name in names)
    return {name: sum(result[name][:length] for result in results) / len(results) for name in names}


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
