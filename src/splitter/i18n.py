"""Язык интерфейса (FR-26): русский — исходный, английский — по словарю.

tr("строка") возвращает перевод для текущего языка. Строки с подстановками пишутся как шаблоны:
tr("Готово: {0} из {1}.").format(done, total) — так один и тот же шаблон переводится целиком.
"""

LANGUAGES = (("ru", "Русский"), ("en", "English"))
_language = "ru"


def set_language(language: str) -> None:
    global _language
    _language = language if language in dict(LANGUAGES) else "ru"


def language() -> str:
    return _language


def system_language() -> str:
    """Язык системы: русский для русской системы, иначе английский."""
    try:
        from PySide6.QtCore import QLocale

        return "ru" if QLocale.system().language() == QLocale.Language.Russian else "en"
    except Exception:
        import locale

        return "ru" if (locale.getlocale()[0] or "").lower().startswith("ru") else "en"


def tr(text: str) -> str:
    if _language == "ru":
        return text
    from .translations import EN

    return EN.get(text, text)
