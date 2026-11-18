"""Английский интерфейс (FR-26): у каждой строки есть перевод, подстановки совпадают."""

import ast
import re
import string
from pathlib import Path

import pytest

from splitter import i18n
from splitter.translations import EN

SOURCES = sorted((Path(__file__).parent.parent / "src" / "splitter").glob("*.py"))


def tr_literals() -> list[str]:
    found = []
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr":
                if node.args and isinstance(node.args[0], ast.Constant):
                    found.append(node.args[0].value)
    return found


def fields(template: str) -> list[str]:
    return sorted(f"{name}:{spec}" for _, name, spec, _ in string.Formatter().parse(template) if name is not None)


def test_every_tr_string_is_translated():
    missing = [text for text in tr_literals() if text not in EN]
    assert not missing, "нет перевода:\n" + "\n".join(missing)


def test_module_level_labels_are_translated():
    from splitter import cli, download, gui, presets, wizard

    labels = [title for seq in (gui.QUALITIES, gui.MODE_TITLES, gui.LANGUAGE_TITLES, gui.THEMES) for _, title in seq]
    labels += list(gui.MIX_TITLES.values()) + list(cli.TRACK_TITLES.values()) + list(wizard.PACK_TITLES.values())
    labels += [title for title, _ in presets.BUILTIN.values()] + [gui.CUSTOM_PRESET, download.COPYRIGHT_NOTICE]
    missing = [text for text in labels if re.search("[А-Яа-яЁё]", text) and text not in EN]
    assert not missing, missing


@pytest.mark.parametrize("source", sorted(EN))
def test_translation_keeps_placeholders(source):
    assert fields(source) == fields(EN[source])
    assert not re.search("[А-Яа-яЁё]", EN[source]), "в переводе остался русский текст"


def test_switching_language():
    try:
        i18n.set_language("en")
        assert i18n.tr("Разделить") == "Split"
        assert i18n.tr("Готово: {0} из {1}.").format(2, 3) == "Done: 2 of 3."
        i18n.set_language("de")  # неизвестный — русский
        assert i18n.tr("Разделить") == "Разделить"
    finally:
        i18n.set_language("ru")
