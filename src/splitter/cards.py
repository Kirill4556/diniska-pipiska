"""Карточка песни в очереди (FR-21): обложка, название, длительность, волна и состояние."""

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem

ROW_HEIGHT = 56
COVER = 44
# цвет волны — бирюзовый, как правая половина иконки; для светлой темы темнее
WAVE_DARK, WAVE_LIGHT = "#38D5C4", "#0F8F82"


class SongDelegate(QStyledItemDelegate):
    """Рисует строки песен (верхний уровень). Вложенные строки с файлами — как обычно."""

    def __init__(self, roles: dict, placeholder: QPixmap | None = None, parent=None):
        super().__init__(parent)
        self._roles = roles  # {"cover": роль, "peaks": роль, "info": роль, "state": роль}
        self._placeholder = placeholder

    def sizeHint(self, option, index) -> QSize:
        size = super().sizeHint(option, index)
        if not index.parent().isValid() and index.column() == 0:
            size.setHeight(ROW_HEIGHT)
        return size

    def paint(self, painter: QPainter, option, index) -> None:
        if index.parent().isValid() or index.column() != 0:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, opt.widget)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = opt.rect
        # стиль Windows 11 выделяет строку светлой подложкой, поэтому цвет текста не меняем
        text_color = opt.palette.color(QPalette.ColorRole.Text)
        dark = opt.palette.color(QPalette.ColorRole.Base).lightness() < 128
        muted = QColor(text_color)
        muted.setAlphaF(0.6)

        # обложка (или значок приложения)
        cover_rect = QRectF(rect.left() + 6, rect.top() + (rect.height() - COVER) / 2, COVER, COVER)
        clip = QPainterPath()
        clip.addRoundedRect(cover_rect, 6, 6)
        painter.setClipPath(clip)
        pixmap = index.data(self._roles["cover"]) or self._placeholder
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            painter.drawPixmap(cover_rect.toRect(), pixmap)
        else:
            painter.fillRect(cover_rect, opt.palette.color(QPalette.ColorRole.Mid))
        painter.setClipping(False)

        # волна справа
        peaks = index.data(self._roles["peaks"]) or []
        wave_width = min(220.0, rect.width() * 0.35)
        wave_rect = QRectF(rect.right() - wave_width - 8, rect.top() + 12, wave_width, rect.height() - 24)
        if peaks and wave_width > 60:
            accent = QColor(WAVE_DARK if dark else WAVE_LIGHT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            step = wave_rect.width() / len(peaks)
            bar = max(step * 0.6, 1.0)
            middle = wave_rect.center().y()
            for i, peak in enumerate(peaks):
                height = max(peak * wave_rect.height(), 1.5)
                painter.drawRoundedRect(QRectF(wave_rect.left() + i * step, middle - height / 2, bar, height), bar / 2, bar / 2)

        # название и подпись
        text_left = cover_rect.right() + 10
        text_right = (wave_rect.left() - 10) if peaks and wave_width > 60 else rect.right() - 8
        title_font = QFont(opt.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(text_color)
        title_rect = QRectF(text_left, rect.top() + 8, text_right - text_left, rect.height() / 2 - 8)
        title = painter.fontMetrics().elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "", Qt.TextElideMode.ElideMiddle, int(title_rect.width()))
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, title)
        painter.setFont(opt.font)
        painter.setPen(muted)
        info_rect = QRectF(text_left, rect.top() + rect.height() / 2 + 2, text_right - text_left, rect.height() / 2 - 8)
        info = painter.fontMetrics().elidedText(index.data(self._roles["info"]) or "", Qt.TextElideMode.ElideRight, int(info_rect.width()))
        painter.drawText(info_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, info)
        painter.restore()
