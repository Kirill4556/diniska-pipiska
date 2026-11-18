"""Окна прослушивания: микшер дорожек (FR-15) и редактор текста с таймкодами (FR-18)."""

import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .audio import MixPlayer, load, mixer_tracks, save_mix
from .errors import SplitterError
from .lyrics import format_lrc, parse_lrc, save_lyrics


def clock(seconds: float, fraction: bool = False) -> str:
    minutes, rest = divmod(max(seconds, 0.0), 60)
    return f"{int(minutes)}:{rest:05.2f}" if fraction else f"{int(minutes)}:{int(rest):02d}"


def parse_clock(text: str) -> float | None:
    """«1:05.30» или «65.3» -> секунды."""
    text = text.strip().replace(",", ".")
    try:
        if ":" in text:
            minutes, seconds = text.split(":", 1)
            return int(minutes) * 60 + float(seconds)
        return float(text)
    except ValueError:
        return None


class Transport(QWidget):
    """Кнопка «Играть/Пауза», ползунок позиции и время. Работает с любым MixPlayer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player: MixPlayer | None = None
        self.play_button = QPushButton("Играть")
        self.play_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.setMinimumWidth(110)
        self.play_button.clicked.connect(self.toggle)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderReleased.connect(self._seek)
        self.slider.sliderMoved.connect(lambda value: self.time.setText(self._time_text(value / 10)))
        self.time = QLabel("0:00 / 0:00")
        self.time.setMinimumWidth(90)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.play_button)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.time)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self.update_view)
        self._timer.start()
        self.setEnabled(False)

    def attach(self, player: MixPlayer) -> None:
        self.player = player
        self.slider.setRange(0, int(player.duration * 10))
        self.setEnabled(True)
        self.update_view()

    def _time_text(self, position: float) -> str:
        return f"{clock(position)} / {clock(self.player.duration)}" if self.player else "0:00 / 0:00"

    def toggle(self) -> None:
        if self.player is None:
            return
        try:
            if self.player.playing:
                self.player.pause()
            else:
                self.player.play()
        except SplitterError as exc:
            QMessageBox.warning(self, "Звук", str(exc))
        self.update_view()

    def _seek(self) -> None:
        if self.player is not None:
            self.player.seek(self.slider.value() / 10)

    def update_view(self) -> None:
        if self.player is None:
            return
        playing = self.player.playing
        self.play_button.setText("Пауза" if playing else "Играть")
        self.play_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay))
        if not self.slider.isSliderDown():
            self.slider.setValue(int(self.player.position * 10))
            self.time.setText(self._time_text(self.player.position))


class _Loader:
    """Декодирует файлы в фоне, чтобы окно открывалось сразу."""

    def __init__(self, files: dict[str, Path], done, failed):
        def work():
            try:
                arrays = {name: load(path) for name, path in files.items()}
            except SplitterError as exc:
                failed.emit(str(exc))
                return
            done.emit(arrays)

        threading.Thread(target=work, daemon=True).start()


class MixerDialog(QDialog):
    """Прослушивание дорожек с громкостью каждой, сравнение с оригиналом, сохранение микса."""

    loaded = Signal(dict)
    load_failed = Signal(str)
    ORIGINAL = "__original__"

    def __init__(self, title: str, tracks: dict[str, Path], titles: dict[str, str], source: Path | None,
                 out_stem: Path, bitrate: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Слушать и смешать — {title}")
        self.resize(620, 360)
        self._names = mixer_tracks(list(tracks))
        self._titles = titles
        self._source = source if source is not None and source.is_file() else None
        self._out_stem = out_stem  # папка/начало имени: <stem>_mix.<fmt>
        self._fmt = next(iter(tracks.values())).suffix.lstrip(".").lower()
        self._bitrate = bitrate
        self.player: MixPlayer | None = None
        self.saved: Path | None = None

        self.note = QLabel("Загружаю дорожки…")
        self.note.setWordWrap(True)
        self.sliders: dict[str, QSlider] = {}
        self.mutes: dict[str, QCheckBox] = {}
        self.values: dict[str, QLabel] = {}
        grid = QGridLayout()
        for row, name in enumerate(self._names):
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 150)
            slider.setValue(100)
            slider.setPageStep(10)
            value = QLabel("100 %")
            value.setMinimumWidth(48)
            mute = QCheckBox("выкл.")
            slider.valueChanged.connect(self._update_gains)
            mute.toggled.connect(self._update_gains)
            grid.addWidget(QLabel(titles.get(name, name)), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(value, row, 2)
            grid.addWidget(mute, row, 3)
            self.sliders[name], self.values[name], self.mutes[name] = slider, value, mute
        grid.setColumnStretch(1, 1)
        self.tracks_box = QWidget()
        self.tracks_box.setLayout(grid)

        self.original = QCheckBox("Слушать оригинал (для сравнения)")
        self.original.setEnabled(False)
        self.original.toggled.connect(self._update_gains)
        self.transport = Transport()
        self.reset_button = QPushButton("Сбросить")
        self.reset_button.clicked.connect(self._reset)
        self.save_button = QPushButton("Сохранить микс")
        self.save_button.setToolTip(f"Сохранить то, что звучит сейчас, в {self._out_name().name}")
        self.save_button.clicked.connect(self._save)
        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.reset_button)
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addWidget(self.tracks_box)
        layout.addWidget(self.original)
        layout.addWidget(self.transport)
        layout.addStretch()
        layout.addLayout(buttons)
        for widget in (self.tracks_box, self.reset_button, self.save_button):
            widget.setEnabled(False)

        self.loaded.connect(self._on_loaded)
        self.load_failed.connect(self._on_failed)
        files = {name: tracks[name] for name in self._names}
        if self._source is not None:
            files[self.ORIGINAL] = self._source
        _Loader(files, self.loaded, self.load_failed)

    def _out_name(self) -> Path:
        return self._out_stem.with_name(f"{self._out_stem.name}_mix.{self._fmt}")

    def _on_loaded(self, arrays: dict) -> None:
        original = arrays.pop(self.ORIGINAL, None)
        self.player = MixPlayer(arrays, original)
        self.transport.attach(self.player)
        self.original.setEnabled(self.player.has_original)
        for widget in (self.tracks_box, self.reset_button, self.save_button):
            widget.setEnabled(True)
        self.note.setText("Двигайте ползунки во время прослушивания. Пробел — играть/пауза.")
        self._update_gains()

    def _on_failed(self, message: str) -> None:
        self.note.setText(f"Не удалось загрузить дорожки: {message}")

    def _update_gains(self, *_) -> None:
        for name, slider in self.sliders.items():
            self.values[name].setText(f"{slider.value()} %")
        if self.player is None:
            return
        self.player.gains = {
            name: 0.0 if self.mutes[name].isChecked() else slider.value() / 100 for name, slider in self.sliders.items()
        }
        self.player.solo_original = self.original.isChecked()
        self.tracks_box.setEnabled(not self.original.isChecked())

    def _reset(self) -> None:
        for name in self.sliders:
            self.sliders[name].setValue(100)
            self.mutes[name].setChecked(False)
        self.original.setChecked(False)

    def _save(self) -> None:
        if self.player is None:
            return
        target = self._out_name()
        if target.exists():
            answer = QMessageBox.question(self, "Сохранить микс", f"Файл {target.name} уже есть. Заменить?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            save_mix(self.player.mix(), target, self._fmt, self._bitrate, tags_from=self._source)
        except (SplitterError, OSError) as exc:
            QMessageBox.warning(self, "Сохранить микс", f"Не удалось сохранить: {exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.saved = target
        self.note.setText(f"Микс сохранён: {target.name}")

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self.transport.toggle()
        else:
            super().keyPressEvent(event)

    def done(self, result: int) -> None:
        if self.player is not None:
            self.player.close()
        super().done(result)

    def closeEvent(self, event) -> None:
        if self.player is not None:
            self.player.close()
        super().closeEvent(event)


class LyricsEditor(QDialog):
    """Текст по строкам с таймкодами: прослушать строку, сдвинуть время, поставить по текущей позиции."""

    loaded = Signal(dict)
    load_failed = Signal(str)

    def __init__(self, lrc_path: Path, audio: dict[str, Path], parent=None):
        super().__init__(parent)
        self._lrc_path = lrc_path
        self.setWindowTitle(f"Текст песни — {lrc_path.stem}")
        self.resize(700, 600)
        self.player: MixPlayer | None = None
        self._audio_names = list(audio)

        self.note = QLabel("Загружаю звук…" if audio else "Звук для прослушивания не найден — текст можно править без него.")
        self.note.setWordWrap(True)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Время", "Строка"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        for start, text in parse_lrc(lrc_path.read_text(encoding="utf-8")):
            self._append(start, text)

        self.listen = QComboBox()
        for name in self._audio_names:
            self.listen.addItem(f"Слушать: {name}", name)
        self.listen.currentIndexChanged.connect(self._choose_track)
        self.play_line_button = QPushButton("Строку")
        self.play_line_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_line_button.setToolTip("Проиграть выбранную строку (двойной щелчок по времени — то же)")
        self.earlier_button = QPushButton("−0,1 с")
        self.later_button = QPushButton("+0,1 с")
        self.stamp_button = QPushButton("Поставить сюда")
        self.stamp_button.setToolTip("Записать текущую позицию в выбранную строку и перейти к следующей (клавиша Enter)")
        self.add_button = QPushButton("Добавить строку")
        self.delete_button = QPushButton("Удалить строку")
        self.play_line_button.clicked.connect(self._play_line)
        self.earlier_button.clicked.connect(lambda: self._shift(-0.1))
        self.later_button.clicked.connect(lambda: self._shift(0.1))
        self.stamp_button.clicked.connect(self._stamp)
        self.add_button.clicked.connect(self._add_line)
        self.delete_button.clicked.connect(self._delete_line)
        self.table.cellDoubleClicked.connect(lambda row, column: self._play_line() if column == 0 else None)
        line_tools = QHBoxLayout()
        for widget in (self.play_line_button, self.earlier_button, self.later_button, self.stamp_button):
            line_tools.addWidget(widget)
        line_tools.addStretch()
        line_tools.addWidget(self.add_button)
        line_tools.addWidget(self.delete_button)

        self.transport = Transport()
        listen_row = QHBoxLayout()
        listen_row.addWidget(self.listen)
        listen_row.addWidget(self.transport, 1)

        self.save_button = QPushButton("Сохранить")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.accept)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(QLabel("Сохраняются оба файла: .lrc с таймкодами и .txt."))
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        buttons.addWidget(cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addLayout(listen_row)
        layout.addWidget(self.table, 1)
        layout.addLayout(line_tools)
        layout.addLayout(buttons)

        self._arrays: dict = {}
        self._sound_tools(False)
        self.loaded.connect(self._on_loaded)
        self.load_failed.connect(lambda message: self.note.setText(f"Звук не загрузился ({message}) — текст можно править без него."))
        if audio:
            _Loader(audio, self.loaded, self.load_failed)
        if self.table.rowCount():
            self.table.selectRow(0)

    # ---------- звук ----------

    def _sound_tools(self, enabled: bool) -> None:
        for widget in (self.listen, self.play_line_button, self.stamp_button):
            widget.setEnabled(enabled)

    def _on_loaded(self, arrays: dict) -> None:
        self._arrays = arrays
        self._choose_track()
        self._sound_tools(True)
        self.note.setText(
            "Выберите строку и нажмите «Строку» (или дважды щёлкните по времени), чтобы её прослушать. Чтобы расставить время заново: "
            "включите песню и нажимайте Enter в начале каждой строки."
        )

    def _choose_track(self, *_) -> None:
        name = self.listen.currentData()
        if name not in self._arrays:
            return
        position, playing = (self.player.position, self.player.playing) if self.player else (0.0, False)
        if self.player is not None:
            self.player.close()
        self.player = MixPlayer({name: self._arrays[name]})
        self.player.seek(position)
        self.transport.attach(self.player)
        if playing:
            self.player.play()

    # ---------- строки ----------

    def _append(self, start: float, text: str, row: int | None = None) -> None:
        row = self.table.rowCount() if row is None else row
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(clock(start, fraction=True)))
        self.table.setItem(row, 1, QTableWidgetItem(text))

    def _row_time(self, row: int) -> float | None:
        item = self.table.item(row, 0)
        return parse_clock(item.text()) if item else None

    def _set_row_time(self, row: int, seconds: float) -> None:
        self.table.item(row, 0).setText(clock(max(seconds, 0.0), fraction=True))

    def _current_row(self) -> int:
        return self.table.currentRow()

    def _play_line(self) -> None:
        row = self._current_row()
        if self.player is None or row < 0:
            return
        start = self._row_time(row)
        if start is None:
            self.note.setText(f"Строка {row + 1}: время записано неверно — нужно вида 1:05.30.")
            return
        stop = self._row_time(row + 1) if row + 1 < self.table.rowCount() else None
        try:
            self.player.play(start=start, stop=stop if stop and stop > start else None)
        except SplitterError as exc:
            QMessageBox.warning(self, "Звук", str(exc))

    def _shift(self, delta: float) -> None:
        row = self._current_row()
        start = self._row_time(row) if row >= 0 else None
        if start is not None:
            self._set_row_time(row, start + delta)

    def _stamp(self) -> None:
        row = self._current_row()
        if self.player is None or row < 0:
            return
        self._set_row_time(row, self.player.position)
        if row + 1 < self.table.rowCount():
            self.table.selectRow(row + 1)

    def _add_line(self) -> None:
        row = self._current_row()
        start = self.player.position if self.player else (self._row_time(row) or 0.0)
        self._append(start, "", row + 1 if row >= 0 else None)
        target = row + 1 if row >= 0 else self.table.rowCount() - 1
        self.table.selectRow(target)
        self.table.editItem(self.table.item(target, 1))

    def _delete_line(self) -> None:
        row = self._current_row()
        if row >= 0:
            self.table.removeRow(row)

    def lines(self) -> list[tuple[float, str]] | None:
        result = []
        for row in range(self.table.rowCount()):
            start = self._row_time(row)
            if start is None:
                self.table.selectRow(row)
                self.note.setText(f"Строка {row + 1}: время записано неверно — нужно вида 1:05.30.")
                return None
            text = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
            result.append((start, text))
        return sorted(result, key=lambda pair: pair[0])

    def keyPressEvent(self, event) -> None:
        editing = self.table.state() == QAbstractItemView.State.EditingState
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not editing and self.player is not None:
            self._stamp()
        elif event.key() == Qt.Key.Key_Space and not editing:
            self.transport.toggle()
        else:
            super().keyPressEvent(event)

    def accept(self) -> None:
        lines = self.lines()
        if lines is None:
            return
        save_lyrics(format_lrc(lines), self._lrc_path.parent, self._lrc_path.stem)
        super().accept()

    def done(self, result: int) -> None:
        if self.player is not None:
            self.player.close()
        super().done(result)
