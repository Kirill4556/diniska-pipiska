"""Рисует иконку приложения (FR-22): звуковая волна, разделённая на две половины — голос и музыка.

Запуск из корня проекта:  uv run python packaging/make_icon.py
Результат: src/splitter/assets/icon.png (для окна) и packaging/icon.ico (для exe и установщика).
"""

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath

ROOT = Path(__file__).resolve().parent.parent
SIZES = (16, 24, 32, 48, 64, 128, 256)

BACKGROUND = ("#2D2560", "#151230")
VOICE = QColor("#FF7A59")  # левая половина — вокал
MUSIC = QColor("#38D5C4")  # правая половина — минус
HEIGHTS = (0.30, 0.55, 0.85, 0.62, 0.40, 0.70, 0.95, 0.58, 0.32)  # высота столбиков волны


def draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scale = size / 256

    plate = QPainterPath()
    margin = 8 * scale
    plate.addRoundedRect(QRectF(margin, margin, size - 2 * margin, size - 2 * margin), 56 * scale, 56 * scale)
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, QColor(BACKGROUND[0]))
    gradient.setColorAt(1, QColor(BACKGROUND[1]))
    painter.fillPath(plate, gradient)

    # на маленьких размерах столбиков меньше, иначе они сливаются
    heights = HEIGHTS if size >= 48 else HEIGHTS[1::2] if size < 24 else HEIGHTS[::2]
    count = len(heights)
    area_left, area_width = 44 * scale, 168 * scale
    gap_middle = 12 * scale  # щель по центру — «разрез»
    step = (area_width - gap_middle) / count
    bar = step * 0.62
    center_y = size / 2
    painter.setPen(Qt.PenStyle.NoPen)
    for i, height in enumerate(heights):
        left_half = i < count / 2
        x = area_left + i * step + (0 if left_half else gap_middle) + (step - bar) / 2
        h = max(height * 150 * scale, bar)
        painter.setBrush(VOICE if left_half else MUSIC)
        painter.drawRoundedRect(QRectF(x, center_y - h / 2, bar, h), bar / 2, bar / 2)
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def write_ico(path: Path, images: list[bytes]) -> None:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, payload = b"", b""
    for size, data in zip(SIZES, images):
        side = 0 if size >= 256 else size  # 0 означает 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset + len(payload))
        payload += data
    path.write_bytes(header + entries + payload)


def main() -> None:
    QGuiApplication(sys.argv)
    images = [draw(size) for size in SIZES]
    assets = ROOT / "src" / "splitter" / "assets"
    assets.mkdir(exist_ok=True)
    images[-1].save(str(assets / "icon.png"))
    write_ico(ROOT / "packaging" / "icon.ico", [png_bytes(image) for image in images])
    print("готово:", assets / "icon.png", ROOT / "packaging" / "icon.ico")


if __name__ == "__main__":
    main()
