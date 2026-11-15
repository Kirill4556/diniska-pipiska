"""Окно приложения (PySide6): очередь файлов, настройки, прогресс, отмена."""

import json
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core import AUDIO_EXTENSIONS, MIX_STEMS, Options, output_names, part_path
from .download import COPYRIGHT_NOTICE, is_url
from .ffmpeg import BITRATES, FORMATS
from .lyrics import save_lyrics

QUALITIES = (("high", "Качественно (дольше)"), ("fast", "Быстро"))
MODE_TITLES = (
    ("vocals", "Минус и вокал"),
    ("stems", "4 дорожки: вокал, ударные, бас, остальное"),
    ("backing", "Бэк-вокал отдельно: основной голос, подпевки, два минуса"),
)
MIX_TITLES = {"drums": "ударные", "bass": "бас", "other": "остальное"}
LANGUAGE_TITLES = ((None, "Язык: авто"), ("ru", "Русский"), ("en", "English"))

PATH_ROLE = Qt.ItemDataRole.UserRole  # путь к файлу или ссылка
STATE_ROLE = Qt.ItemDataRole.UserRole + 1
LRC_ROLE = Qt.ItemDataRole.UserRole + 2  # путь к .lrc, когда текст распознан
QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        # в собранном приложении обработчик — соседний консольный файл (у оконного нет stdout)
        name = "splitter-cli.exe" if sys.platform == "win32" else "splitter-cli"
        return [str(Path(sys.executable).with_name(name)), "--worker"]
    python = Path(sys.executable)
    console = python.with_name("python.exe")
    if python.name.lower() == "pythonw.exe" and console.exists():
        python = console
    return [str(python), "-m", "splitter.worker"]


def expand_paths(paths: list[Path]) -> list[Path]:
    """Файлы берём как есть, папки разворачиваем; всё не-аудио пропускаем."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files += sorted(p for p in path.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS)
        elif path.suffix.lower() in AUDIO_EXTENSIONS:
            files.append(path)
    return files


class LyricsDialog(QDialog):
    """Правка распознанного текста: редактируется .lrc, .txt пересобирается из него."""

    def __init__(self, lrc_path: Path, parent=None):
        super().__init__(parent)
        self._lrc_path = lrc_path
        self.setWindowTitle(f"Текст песни — {lrc_path.stem}")
        self.resize(560, 520)
        note = QLabel("Поправьте слова, не трогая таймкоды в квадратных скобках. Сохранятся оба файла: .lrc и .txt.")
        note.setWordWrap(True)
        self.editor = QPlainTextEdit(lrc_path.read_text(encoding="utf-8"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.editor, 1)
        layout.addWidget(buttons)

    def accept(self) -> None:
        text = self.editor.toPlainText().rstrip() + "\n"
        save_lyrics(text, self._lrc_path.parent, self._lrc_path.stem)
        super().accept()


class MainWindow(QMainWindow):
    worker_event = Signal(dict)
    worker_exited = Signal(int)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Splitter — минус и вокал")
        self.resize(820, 620)
        self.setAcceptDrops(True)
        self._settings = QSettings("splitter", "splitter")
        self._process: subprocess.Popen | None = None
        self._batch: list[QTreeWidgetItem] = []
        self._job: dict = {}
        self._units: dict[int, int] = {}  # сколько этапов пройдено по каждому файлу
        self._current: QTreeWidgetItem | None = None
        self._cancelled = False
        self._fatal: str | None = None
        self._stderr_tail: deque[str] = deque(maxlen=15)

        self._build_ui()
        self._load_settings()
        self.worker_event.connect(self._on_event)
        self.worker_exited.connect(self._on_exit)
        self._refresh()

    # ---------- интерфейс ----------

    def _build_ui(self) -> None:
        self.queue = QTreeWidget()
        self.queue.setHeaderLabels(["Файл", "Состояние"])
        self.queue.setRootIsDecorated(False)
        self.queue.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.queue.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.queue.setColumnWidth(1, 210)
        self.queue.header().setStretchLastSection(False)
        self.queue.itemSelectionChanged.connect(self._refresh)
        self.queue.itemDoubleClicked.connect(self._on_double_click)

        self.hint = QLabel("Перетащите сюда песни или папки\n(mp3, wav, flac, m4a)")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setStyleSheet("color: gray; font-size: 15px; border: 2px dashed gray; border-radius: 8px; padding: 30px;")

        self.add_files_button = QPushButton("Добавить файлы…")
        self.add_folder_button = QPushButton("Добавить папку…")
        self.add_url_button = QPushButton("Добавить ссылку…")
        self.remove_button = QPushButton("Убрать")
        self.clear_button = QPushButton("Очистить")
        self.lyrics_button = QPushButton("Текст…")
        self.lyrics_button.setToolTip("Посмотреть и поправить распознанный текст выбранной песни")
        self.add_files_button.clicked.connect(self._choose_files)
        self.add_folder_button.clicked.connect(self._choose_folder)
        self.add_url_button.clicked.connect(self._ask_url)
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button.clicked.connect(self._clear)
        self.lyrics_button.clicked.connect(self._edit_lyrics)
        queue_buttons = QHBoxLayout()
        for button in (self.add_files_button, self.add_folder_button, self.add_url_button, self.remove_button, self.clear_button):
            queue_buttons.addWidget(button)
        queue_buttons.addStretch()
        queue_buttons.addWidget(self.lyrics_button)

        self.mode = QComboBox()
        for key, title in MODE_TITLES:
            self.mode.addItem(title, key)
        self.mode.currentIndexChanged.connect(self._refresh)
        self.mix_boxes = {name: QCheckBox(title) for name, title in MIX_TITLES.items()}
        self.mix_row = QWidget()
        mix_layout = QHBoxLayout(self.mix_row)
        mix_layout.setContentsMargins(0, 0, 0, 0)
        for box in self.mix_boxes.values():
            box.setChecked(True)
            mix_layout.addWidget(box)
        mix_layout.addStretch()
        self.mix_label = QLabel("Смешать в минус:")

        self.quality = QComboBox()
        for key, title in QUALITIES:
            self.quality.addItem(title, key)
        self.format = QComboBox()
        self.format.addItems(FORMATS)
        self.format.currentTextChanged.connect(self._refresh)
        self.bitrate = QComboBox()
        for value in sorted(BITRATES, reverse=True):
            self.bitrate.addItem(f"{value} кбит/с", value)
        format_row = QHBoxLayout()
        format_row.addWidget(self.format)
        format_row.addWidget(self.bitrate)
        format_row.addStretch()

        self.lyrics_box = QCheckBox("Распознать текст песни (.txt и .lrc с таймкодами)")
        self.lyrics_box.toggled.connect(self._refresh)
        self.language = QComboBox()
        for key, title in LANGUAGE_TITLES:
            self.language.addItem(title, key)
        lyrics_row = QHBoxLayout()
        lyrics_row.addWidget(self.lyrics_box)
        lyrics_row.addWidget(self.language)
        lyrics_row.addStretch()

        self.out_dir = QLineEdit()
        self.browse_button = QPushButton("Обзор…")
        self.browse_button.clicked.connect(self._choose_out_dir)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_dir)
        out_row.addWidget(self.browse_button)

        form = QFormLayout()
        form.addRow("Режим:", self.mode)
        form.addRow(self.mix_label, self.mix_row)
        form.addRow("Качество:", self.quality)
        form.addRow("Формат:", format_row)
        form.addRow("Текст:", lyrics_row)
        form.addRow("Папка результата:", out_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        self.start_button = QPushButton("Разделить")
        self.start_button.setDefault(True)
        self.cancel_button = QPushButton("Отмена")
        self.open_button = QPushButton("Открыть папку результата")
        for button in (self.start_button, self.cancel_button, self.open_button):
            button.setMinimumHeight(34)
        self.start_button.clicked.connect(self.start)
        self.cancel_button.clicked.connect(self.cancel)
        self.open_button.clicked.connect(self._open_output)
        actions = QHBoxLayout()
        actions.addWidget(self.open_button)
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.start_button)

        layout = QVBoxLayout()
        layout.addWidget(self.hint, 1)
        layout.addWidget(self.queue, 1)
        layout.addLayout(queue_buttons)
        layout.addLayout(form)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addLayout(actions)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

    def _load_settings(self) -> None:
        value = self._settings.value
        self.out_dir.setText(value("out_dir", str(Path.home() / "Music" / "Splitter")))
        self.mode.setCurrentIndex(max(0, self.mode.findData(value("mode", "vocals"))))
        self.quality.setCurrentIndex(max(0, self.quality.findData(value("quality", "high"))))
        self.format.setCurrentText(value("format", "mp3"))
        self.bitrate.setCurrentIndex(max(0, self.bitrate.findData(int(value("bitrate", 320)))))
        mix = str(value("mix", ",".join(MIX_STEMS))).split(",")
        for name, box in self.mix_boxes.items():
            box.setChecked(name in mix)
        self.lyrics_box.setChecked(str(value("lyrics", "false")).lower() == "true")
        self.language.setCurrentIndex(max(0, self.language.findData(value("language", "") or None)))

    def _save_settings(self) -> None:
        store = self._settings.setValue
        store("out_dir", self.out_dir.text())
        store("mode", self.mode.currentData())
        store("quality", self.quality.currentData())
        store("format", self.format.currentText())
        store("bitrate", self.bitrate.currentData())
        store("mix", ",".join(self._mix()))
        store("lyrics", "true" if self.lyrics_box.isChecked() else "false")
        store("language", self.language.currentData() or "")

    def _mix(self) -> list[str]:
        return [name for name, box in self.mix_boxes.items() if box.isChecked()]

    @property
    def running(self) -> bool:
        return self._process is not None

    def _items(self) -> list[QTreeWidgetItem]:
        return [self.queue.topLevelItem(i) for i in range(self.queue.topLevelItemCount())]

    def _selected_lrc(self) -> Path | None:
        selected = self.queue.selectedItems()
        if len(selected) == 1 and selected[0].data(0, LRC_ROLE):
            path = Path(selected[0].data(0, LRC_ROLE))
            return path if path.is_file() else None
        return None

    def _refresh(self, *_) -> None:
        """Приводит доступность кнопок в соответствие с состоянием."""
        items = self._items()
        pending = any(item.data(0, STATE_ROLE) != DONE for item in items)
        busy = self.running
        stems_mode = self.mode.currentData() == "stems"
        self.hint.setVisible(not items)
        self.queue.setVisible(bool(items))
        self.mix_label.setVisible(stems_mode)
        self.mix_row.setVisible(stems_mode)
        settings = (self.mode, self.mix_row, self.quality, self.format, self.lyrics_box, self.out_dir, self.browse_button)
        for widget in (self.add_files_button, self.add_folder_button, self.add_url_button, *settings):
            widget.setEnabled(not busy)
        self.bitrate.setEnabled(not busy and self.format.currentText() == "mp3")
        self.language.setEnabled(not busy and self.lyrics_box.isChecked())
        self.remove_button.setEnabled(not busy and bool(self.queue.selectedItems()))
        self.clear_button.setEnabled(not busy and bool(items))
        self.lyrics_button.setEnabled(self._selected_lrc() is not None)
        self.start_button.setEnabled(not busy and pending)
        self.cancel_button.setEnabled(busy)

    # ---------- очередь ----------

    def _add_item(self, key: str, title: str) -> bool:
        if key in {item.data(0, PATH_ROLE) for item in self._items()}:
            return False
        item = QTreeWidgetItem([title, ""])
        item.setData(0, PATH_ROLE, key)
        item.setToolTip(0, key)
        self.queue.addTopLevelItem(item)
        self._set_state(item, QUEUED, "В очереди")
        return True

    def add_paths(self, paths: list[Path]) -> None:
        added = sum(self._add_item(str(file.resolve()), file.name) for file in expand_paths(paths))
        if paths and not added:
            self.status.setText("Новых аудиофайлов не найдено (нужны mp3, wav, flac или m4a).")
        self._refresh()

    def add_url(self, url: str) -> bool:
        url = url.strip()
        if not is_url(url):
            self.status.setText("Это не похоже на ссылку: она должна начинаться с http:// или https://.")
            return False
        added = self._add_item(url, url)
        self._refresh()
        return added

    def _ask_url(self) -> None:
        url, ok = QInputDialog.getText(self, "Добавить ссылку", "Ссылка на трек или видео:")
        if not ok or not url.strip():
            return
        answer = QMessageBox.question(self, "Авторские права", f"{COPYRIGHT_NOTICE}\n\nДобавить ссылку в очередь?")
        if answer == QMessageBox.StandardButton.Yes:
            self.add_url(url)

    def _set_state(self, item: QTreeWidgetItem, state: str, text: str, tooltip: str | None = None) -> None:
        item.setData(0, STATE_ROLE, state)
        item.setText(1, text)
        if tooltip is not None:
            item.setToolTip(1, tooltip)

    def _choose_files(self) -> None:
        pattern = " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS))
        names, _ = QFileDialog.getOpenFileNames(self, "Выберите песни", self._settings.value("last_dir", ""), f"Аудио ({pattern})")
        if names:
            self._settings.setValue("last_dir", str(Path(names[0]).parent))
            self.add_paths([Path(n) for n in names])

    def _choose_folder(self) -> None:
        name = QFileDialog.getExistingDirectory(self, "Выберите папку с песнями", self._settings.value("last_dir", ""))
        if name:
            self._settings.setValue("last_dir", name)
            self.add_paths([Path(name)])

    def _choose_out_dir(self) -> None:
        name = QFileDialog.getExistingDirectory(self, "Папка для результата", self.out_dir.text())
        if name:
            self.out_dir.setText(name)

    def _remove_selected(self) -> None:
        for item in self.queue.selectedItems():
            self.queue.takeTopLevelItem(self.queue.indexOfTopLevelItem(item))
        self._refresh()

    def _clear(self) -> None:
        self.queue.clear()
        self.progress.setValue(0)
        self.status.setText("")
        self._refresh()

    def _open_output(self) -> None:
        folder = Path(self.out_dir.text())
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            self.status.setText("Папка результата пока не создана.")

    def _edit_lyrics(self) -> None:
        lrc = self._selected_lrc()
        if lrc is not None and LyricsDialog(lrc, self).exec():
            self.status.setText(f"Текст сохранён: {lrc.with_suffix('.txt').name} и {lrc.name}.")

    def _on_double_click(self, *_) -> None:
        if self._selected_lrc() is not None:
            self._edit_lyrics()
        else:
            self._open_output()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and not self.running:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        self.add_paths([Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()])

    # ---------- обработка ----------

    def start(self) -> None:
        if self.running:
            return
        out = self.out_dir.text().strip()
        if not out:
            self.status.setText("Укажите папку результата.")
            return
        self._batch = [item for item in self._items() if item.data(0, STATE_ROLE) != DONE]
        if not self._batch:
            return
        for item in self._batch:
            self._set_state(item, QUEUED, "В очереди", "")
            item.setData(0, LRC_ROLE, None)
        self._save_settings()
        self._units = {}
        self._current = None
        self._cancelled = False
        self._fatal = None
        self._stderr_tail.clear()
        self.progress.setValue(0)
        self.status.setText("Запуск…")

        self._job = {
            "inputs": [item.data(0, PATH_ROLE) for item in self._batch],
            "out": out,
            "format": self.format.currentText(),
            "bitrate": self.bitrate.currentData(),
            "quality": self.quality.currentData(),
            "mode": self.mode.currentData(),
            "mix": self._mix(),
            "lyrics": self.lyrics_box.isChecked(),
            "language": self.language.currentData(),
        }
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(
            worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
        )
        self._process = process
        process.stdin.write(json.dumps(self._job).encode("utf-8"))
        process.stdin.close()
        threading.Thread(target=self._read_events, args=(process,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(process,), daemon=True).start()
        self._refresh()

    def _read_events(self, process: subprocess.Popen) -> None:
        for line in process.stdout:
            try:
                self.worker_event.emit(json.loads(line.decode("utf-8")))
            except ValueError:
                continue
        self.worker_exited.emit(process.wait())

    def _read_stderr(self, process: subprocess.Popen) -> None:
        for line in process.stderr:
            self._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())

    def cancel(self) -> None:
        if self._process is not None:
            self._cancelled = True
            self.status.setText("Отмена…")
            self._process.kill()

    def _units_per_file(self) -> int:
        return 2 if self._job.get("lyrics") else 1

    def _overall(self, fraction: float = 0.0) -> None:
        total = len(self._batch) * self._units_per_file()
        self.progress.setValue(int(1000 * min(sum(self._units.values()) + fraction, total) / total))

    def _on_event(self, event: dict) -> None:
        kind = event.get("event")
        index = event.get("index")
        item = self._batch[index] if index is not None else None
        position = f"{index + 1} из {len(self._batch)}" if index is not None else ""

        if kind == "device" and not event["gpu"]:
            self.status.setText("Видеокарта (CUDA) не найдена — считаю на процессоре, один трек может занять до 10 минут.")
        elif kind == "model":
            self.status.setText("Загружаю модель (при первом запуске она скачивается)…")
        elif kind == "lyrics_model":
            self.status.setText("Загружаю модель распознавания текста (при первом запуске она скачивается)…")
        elif kind == "model_download":
            self.status.setText(f"Скачиваю модель: {event['value']:.0%}")
        elif kind == "download_start":
            self._current = item
            self._set_state(item, RUNNING, "Скачивание…")
            self.status.setText(f"Скачиваю {position}: {item.text(0)}")
        elif kind == "download_progress":
            self._set_state(item, RUNNING, f"Скачивание… {event['value']:.0%}")
        elif kind == "downloaded":
            # дальше это обычный файл: повторный запуск не будет качать заново
            item.setData(0, PATH_ROLE, event["path"])
            item.setText(0, Path(event["path"]).name)
            item.setToolTip(0, event["path"])
            self._set_state(item, QUEUED, "Скачано, в очереди")
            self._current = None
        elif kind == "start":
            self._current = item
            self._set_state(item, RUNNING, "Обработка… 0%")
            self.queue.scrollToItem(item)
            self.status.setText(f"Обрабатываю {position}: {item.text(0)}")
        elif kind == "progress":
            self._set_state(item, RUNNING, f"Обработка… {event['value']:.0%}")
            self._overall(event["value"])
        elif kind == "split":
            self._units[index] = 1
            self._set_state(item, RUNNING, f"Дорожки готовы ({event['seconds']} с)", "\n".join(event["outputs"].values()))
            self._current = None
            self._overall()
        elif kind == "lyrics_start":
            self._current = item
            self._set_state(item, RUNNING, "Распознаю текст… 0%")
            self.queue.scrollToItem(item)
            self.status.setText(f"Распознаю текст {position}: {item.text(0)}")
        elif kind == "lyrics_progress":
            self._set_state(item, RUNNING, f"Распознаю текст… {event['value']:.0%}")
            self._overall(event["value"])
        elif kind == "lyrics":
            item.setData(0, LRC_ROLE, event["lrc"])
            item.setToolTip(1, f"{item.toolTip(1)}\n{event['txt']}\n{event['lrc']}")
        elif kind == "done":
            self._units[index] = self._units_per_file()
            self._set_state(item, DONE, "Готово + текст" if item.data(0, LRC_ROLE) else "Готово")
            self._current = None
            self._overall()
        elif kind == "error":
            self._units[index] = self._units_per_file()
            self._set_state(item, FAILED, "Ошибка", event["message"])
            self._current = None
            self._overall()
        elif kind == "fatal":
            self._fatal = event["message"]

    def _on_exit(self, code: int) -> None:
        self._process = None
        interrupted = self._current
        self._current = None
        if interrupted is not None:
            self._remove_partial(interrupted)
        unfinished = [item for item in self._batch if item.data(0, STATE_ROLE) not in (DONE, FAILED)]
        failed = [item for item in self._batch if item.data(0, STATE_ROLE) == FAILED]
        done = [item for item in self._batch if item.data(0, STATE_ROLE) == DONE]
        summary = f"Готово: {len(done)} из {len(self._batch)}."

        if self._cancelled:
            for item in unfinished:
                if item.data(0, STATE_ROLE) == RUNNING:  # начатые; нетронутые остаются «В очереди»
                    self._set_state(item, QUEUED, "Отменено")
            self.status.setText(f"Отменено. {summary}")
        elif self._fatal or code != 0 or unfinished:
            message = self._fatal or "Обработчик неожиданно завершился.\n" + "\n".join(self._stderr_tail)
            for item in unfinished:
                self._set_state(item, FAILED, "Ошибка", message)
            self.status.setText("Обработка прервана из-за ошибки.")
            QMessageBox.critical(self, "Ошибка", message)
        else:
            self.progress.setValue(1000)
            if failed:
                summary += f" С ошибкой: {len(failed)} — наведите курсор на «Ошибка», чтобы увидеть причину."
            elif self._job.get("lyrics"):
                summary += " Чтобы поправить текст, выберите песню и нажмите «Текст…»."
            else:
                summary += " Файлы в папке результата."
            self.status.setText(summary)
        self._refresh()

    def _remove_partial(self, item: QTreeWidgetItem) -> None:
        """Убирает недописанные .part после отмены или сбоя."""
        source = item.data(0, PATH_ROLE)
        if is_url(source) or not self._job:
            return
        options = Options(fmt=self._job["format"], mode=self._job["mode"], mix=tuple(self._job["mix"]))
        out = Path(self._job["out"])
        for name in output_names(options):
            part_path(out / f"{Path(source).stem}_{name}.{options.fmt}").unlink(missing_ok=True)

    def closeEvent(self, event) -> None:
        if self.running:
            answer = QMessageBox.question(self, "Идёт обработка", "Прервать обработку и выйти?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            process, interrupted = self._process, self._current
            self.cancel()
            process.wait()
            if interrupted is not None:
                self._remove_partial(interrupted)
        self._save_settings()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.add_paths([Path(arg) for arg in sys.argv[1:]])
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
