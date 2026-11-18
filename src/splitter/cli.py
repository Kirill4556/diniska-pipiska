"""CLI: splitter song.mp3 --out ./result --format mp3 --bitrate 320 --quality high"""

import argparse
import sys
from pathlib import Path

from . import __version__
from .errors import SplitterError
from .ffmpeg import BITRATES, FORMATS
from .presets import BUILTIN, builtin

MODE_CHOICES = ("vocals", "stems", "backing")
MIX_CHOICES = ("drums", "bass", "other")
TRACK_TITLES = {
    "instrumental": "минус",
    "instrumental_backing": "минус с бэк-вокалом",
    "vocals": "вокал",
    "lead": "основной голос",
    "backing": "бэк-вокал",
    "drums": "ударные",
    "bass": "бас",
    "other": "остальное",
}


def parse_mix(value: str) -> tuple[str, ...]:
    if value.strip().lower() in ("", "none"):
        return ()
    names = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    unknown = set(names) - set(MIX_CHOICES)
    if unknown:
        raise argparse.ArgumentTypeError(f"неизвестные дорожки: {', '.join(sorted(unknown))} (доступно: {', '.join(MIX_CHOICES)})")
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="splitter",
        description="Делит песню на минус (инструментал) и вокал. Умеет 4 дорожки, бэк-вокал, текст песни и ссылки.",
    )
    parser.add_argument("inputs", nargs="+", help="аудиофайлы (mp3, wav, flac, m4a), папки или ссылки на трек/видео")
    parser.add_argument("-o", "--out", type=Path, default=Path("result"), help="папка для результата (по умолчанию ./result)")
    parser.add_argument("-f", "--format", choices=FORMATS, default="mp3", help="формат выхода (по умолчанию mp3)")
    parser.add_argument("-b", "--bitrate", type=int, choices=BITRATES, default=320, help="битрейт mp3, кбит/с (по умолчанию 320)")
    parser.add_argument("-q", "--quality", choices=("fast", "high"), default="high", help="fast — быстрее, high — качественнее (по умолчанию)")
    parser.add_argument(
        "-p",
        "--preset",
        choices=tuple(BUILTIN),
        help="готовый набор: "
        + "; ".join(f"{key} — {title.lower()}" for key, (title, _) in BUILTIN.items())
        + ". Явные --mode и --mix важнее пресета",
    )
    parser.add_argument(
        "-m",
        "--mode",
        choices=MODE_CHOICES,
        help="vocals — минус и вокал (по умолчанию); stems — вокал, ударные, бас, остальное; backing — основной голос и бэк-вокал отдельно",
    )
    parser.add_argument(
        "--mix",
        type=parse_mix,
        metavar="СПИСОК",
        help="для --mode stems: что смешать в минус, через запятую из drums,bass,other (по умолчанию все; none — не делать минус)",
    )
    parser.add_argument("--lyrics", action="store_true", help="распознать текст песни и сохранить .txt и .lrc")
    parser.add_argument("--language", choices=("auto", "ru", "en"), default="auto", help="язык текста (по умолчанию определяется автоматически)")
    parser.add_argument("--batch", action="store_true", help="обрабатывать папки целиком (включается автоматически, если передана папка)")
    parser.add_argument("--version", action="version", version=f"splitter {__version__}")
    return parser


def resolve_mode(args: argparse.Namespace) -> tuple[str, tuple[str, ...]]:
    """Режим и состав минуса: явные параметры, иначе пресет, иначе по умолчанию."""
    preset = builtin(args.preset) if args.preset else {}
    mode = args.mode or preset.get("mode", "vocals")
    mix = args.mix if args.mix is not None else tuple(preset.get("mix", MIX_CHOICES))
    return mode, mix


def collect_inputs(inputs: list[str]) -> list[str]:
    """Разворачивает папки; ссылки оставляет как есть."""
    from .core import AUDIO_EXTENSIONS
    from .download import is_url

    items: list[str] = []
    for raw in inputs:
        if is_url(raw):
            items.append(raw)
            continue
        path = Path(raw)
        if path.is_dir():
            found = sorted(p for p in path.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS)
            if not found:
                print(f"! В папке {path} нет аудиофайлов.", file=sys.stderr)
            items += [str(p) for p in found]
        elif path.is_file():
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                raise SplitterError(f"Неподдерживаемый формат: {path.name}. Нужен mp3, wav, flac или m4a.")
            items.append(str(path))
        else:
            raise SplitterError(f"Файл или папка не найдены: {path}")
    return items


def run(args: argparse.Namespace) -> int:
    items = collect_inputs(args.inputs)
    if not items:
        raise SplitterError("Нечего обрабатывать.")

    from . import pipeline
    from .core import Options
    from .download import COPYRIGHT_NOTICE, is_url
    from .lyrics import LANGUAGES
    from .report import RunLog

    if any(is_url(item) for item in items):
        print(f"! {COPYRIGHT_NOTICE}", file=sys.stderr)

    mode, mix = resolve_mode(args)
    options = Options(
        fmt=args.format,
        bitrate=args.bitrate,
        quality=args.quality,
        mode=mode,
        mix=mix,
        lyrics=args.lyrics,
        language=LANGUAGES[args.language],
    )
    total = len(items)
    log = RunLog("cli")
    log.write(f"задание: {total} шт., {options}")

    def label(index: int) -> str:
        name = items[index] if is_url(items[index]) else Path(items[index]).name
        return f"[{index + 1}/{total}] {name}"

    def emit(event: str, **data) -> None:
        if event != "download_progress":
            log.write(f"{event} {data}")
        if event == "device" and not data["gpu"]:
            print("! Подходящий GPU (CUDA) не найден — работаю на CPU, трек может занять до 10 минут.", file=sys.stderr)
        elif event == "download_start":
            print(f"{label(data['index'])}: скачиваю…")
        elif event == "downloaded":
            print(f"  скачано: {data['path']}")
        elif event == "model":
            print("Загружаю модель (при первом запуске она скачивается)...")
        elif event == "start":
            print(label(data["index"]))
        elif event == "split":
            for name, path in data["outputs"].items():
                print(f"  {TRACK_TITLES.get(name, name)}: {path}")
            print(f"  готово за {data['seconds']} с")
        elif event == "lyrics_model":
            print("Загружаю модель распознавания текста (при первом запуске она скачивается)...")
        elif event == "lyrics_start":
            print(f"{label(data['index'])}: распознаю текст…")
        elif event == "lyrics":
            print(f"  текст ({data['lines']} строк): {data['txt']}")
            print(f"  текст с таймкодами: {data['lrc']}")
        elif event == "error":
            print(f"{label(data['index'])}\n  ! {data['message']}", file=sys.stderr)

    try:
        failures = pipeline.run(items, args.out, options, emit)
    except Exception as exc:
        log.write(f"ошибка: {type(exc).__name__}: {exc}")
        print(f"Журнал: {log.path}", file=sys.stderr)
        raise
    if failures:
        print(f"Не обработано: {failures} из {total}. Журнал: {log.path}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    try:
        code = run(args)
    except SplitterError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("\nОтменено.", file=sys.stderr)
        code = 130
    sys.exit(code)
