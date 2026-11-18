"""Пресеты (FR-19): готовые наборы настроек.

Встроенный пресет задаёт только то, что отличает его от других (режим и состав минуса);
качество, формат и папку он не трогает. Пользовательские пресеты окно хранит в QSettings
и запоминает в них все настройки.
"""

from .errors import SplitterError

# ключ для CLI -> (название, настройки)
BUILTIN: dict[str, tuple[str, dict]] = {
    "vocals": ("Минус + вокал", {"mode": "vocals"}),
    "no-drums": ("Без ударных", {"mode": "stems", "mix": ["bass", "other"]}),
    "no-bass": ("Без баса", {"mode": "stems", "mix": ["drums", "other"]}),
    "backing": ("Отдельно бэк-вокал", {"mode": "backing"}),
}

# настройки, которые запоминает пользовательский пресет
SETTING_KEYS = ("mode", "mix", "quality", "format", "bitrate", "lyrics", "language", "lyrics_online", "clean_vocals")


def builtin(key: str) -> dict:
    if key not in BUILTIN:
        raise SplitterError(f"Неизвестный пресет: {key}. Доступно: {', '.join(BUILTIN)}.")
    return dict(BUILTIN[key][1])


def matches(preset: dict, current: dict) -> bool:
    """Совпадают ли текущие настройки с пресетом (по тем полям, что в нём заданы)."""
    for key, value in preset.items():
        if key == "mix":
            if current.get("mode") == "stems" and sorted(current.get("mix", [])) != sorted(value):
                return False
        elif current.get(key) != value:
            return False
    return True
