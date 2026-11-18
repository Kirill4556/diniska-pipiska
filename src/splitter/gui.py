"""Окно приложения (PySide6): очередь файлов, пресеты, настройки, прогресс, отмена, готовые дорожки."""

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from pathlib import Path

from PySide6.QtCore import QMimeData, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QActionGroup, QDesktopServices, QIcon, QPixmap
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
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSystemTrayIcon,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import i18n, presets, taskbar
from .cards import SongDelegate
from .cli import TRACK_TITLES
from .core import AUDIO_EXTENSIONS, MIX_STEMS, Options, output_names, part_path
from .download import COPYRIGHT_NOTICE, is_url
from .ffmpeg import BITRATES, FORMATS
from .media import format_length, song_info
from .player_ui import LyricsEditor, MixerDialog
from . import __version__
from .report import RunLog, build_report, logs_dir
from .i18n import tr

ICON_PATH = Path(__file__).with_name("assets") / "icon.png"

QUALITIES = (("high", "Качественно"), ("fast", "Быстро"), ("max", "Максимум: две модели, вдвое дольше"))
MODE_TITLES = (
    ("vocals", "Минус и вокал"),
    ("stems", "4 дорожки: вокал, ударные, бас, остальное"),
    ("backing", "Бэк-вокал отдельно: основной голос, подпевки, два минуса"),
)
MIX_TITLES = {"drums": "ударные", "bass": "бас", "other": "остальное"}
LANGUAGE_TITLES = ((None, "Язык: авто"), ("ru", "Русский"), ("en", "English"))
CUSTOM_PRESET = "Свои настройки"
THEMES = (("system", "Как в системе"), ("light", "Светлая"), ("dark", "Тёмная"))
TRACK_ORDER = (
    "instrumental", "instrumental_backing", "vocals", "vocals_clean", "lead", "lead_clean", "backing", "drums", "bass", "other",
)

PATH_ROLE = Qt.ItemDataRole.UserRole  # путь к исходному файлу или ссылка
STATE_ROLE = Qt.ItemDataRole.UserRole + 1
LRC_ROLE = Qt.ItemDataRole.UserRole + 2  # путь к .lrc, когда текст распознан
STEM_ROLE = Qt.ItemDataRole.UserRole + 3  # начало имён результатов (с учётом « (2)»)
ERROR_ROLE = Qt.ItemDataRole.UserRole + 4  # текст ошибки для отчёта
FILE_ROLE = Qt.ItemDataRole.UserRole + 5  # у вложенных строк: путь к готовому файлу
OUTPUTS_ROLE = Qt.ItemDataRole.UserRole + 6  # JSON {дорожка: путь} готовых дорожек песни
LYRICS_SOURCE_ROLE = Qt.ItemDataRole.UserRole + 7  # откуда текст: lrclib или whisper
CACHED_ROLE = Qt.ItemDataRole.UserRole + 8  # дорожки взяты из кэша
COVER_ROLE = Qt.ItemDataRole.UserRole + 9  # QPixmap обложки
PEAKS_ROLE = Qt.ItemDataRole.UserRole + 10  # форма волны для карточки
INFO_ROLE = Qt.ItemDataRole.UserRole + 11  # подпись под названием: длительность, формат, папка
# SPLIT — дорожки уже записаны, ждём или распознаём текст: при отмене такие файлы остаются
QUEUED, RUNNING, SPLIT, DONE, FAILED = "queued", "running", "split", "done", "failed"
# частые события, которые не нужны в журнале
NOISY_EVENTS = {"progress", "lyrics_progress", "download_progress", "model_download"}


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


def reveal(path: Path) -> None:
    """Открывает папку с выделенным файлом."""
    if sys.platform == "win32" and path.exists():
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def format_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return tr("меньше минуты")
    if minutes < 60:
        return tr("≈ {0} мин").format(minutes)
    return tr("≈ {0} ч {1:02d} мин").format(minutes // 60, minutes % 60)


class QueueTree(QTreeWidget):
    """Очередь. Готовые дорожки можно перетащить в проводник или другую программу."""

    def mimeTypes(self) -> list[str]:
        return ["text/uri-list"]

    def mimeData(self, items) -> QMimeData:
        files: list[str] = []
        for item in items:
            rows = [item] if item.parent() is not None else [item.child(i) for i in range(item.childCount())]
            files += [row.data(0, FILE_ROLE) for row in rows if row.data(0, FILE_ROLE)]
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(path) for path in dict.fromkeys(files) if Path(path).is_file()])
        return data


class MainWindow(QMainWindow):
    worker_event = Signal(dict)
    worker_exited = Signal(int)
    media_ready = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr("Splitter — минус и вокал"))
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(860, 660)
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
        self._log: RunLog | None = None
        self._device: str | None = None
        self._clock: tuple[float, float] | None = None  # (время, доля) в начале расчёта — для оценки остатка
        self._taskbar: taskbar.TaskbarProgress | None = None
        self._tray: QSystemTrayIcon | None = None
        self._applying_preset = False
        # очередь запоминается только в настоящем окне (restore_queue), не в тестах
        self._remember_queue = False
        self._media = ThreadPoolExecutor(max_workers=2, thread_name_prefix="splitter-media")
        self._stored_queue: str | None = None

        self._build_ui()
        self._build_menu()
        self._load_settings()
        self._fill_presets()
        self.worker_event.connect(self._on_event)
        self.worker_exited.connect(self._on_exit)
        self.media_ready.connect(self._on_media)
        self._refresh()

    # ---------- интерфейс ----------

    def _build_ui(self) -> None:
        self.queue = QueueTree()
        self.queue.setHeaderLabels([tr("Файл"), tr("Состояние")])
        self.queue.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.queue.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.queue.setColumnWidth(1, 230)
        self.queue.header().setStretchLastSection(False)
        self.queue.setExpandsOnDoubleClick(False)
        self.queue.setDragEnabled(True)
        self.queue.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.queue.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.queue.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue.customContextMenuRequested.connect(self._show_menu)
        self.queue.itemSelectionChanged.connect(self._refresh)
        self.queue.itemDoubleClicked.connect(self._on_double_click)
        placeholder = QPixmap(str(ICON_PATH)) if ICON_PATH.is_file() else None
        self.queue.setItemDelegateForColumn(
            0, SongDelegate({"cover": COVER_ROLE, "peaks": PEAKS_ROLE, "info": INFO_ROLE}, placeholder, self.queue)
        )

        self.hint = QLabel(tr("Перетащите сюда песни или папки\n(mp3, wav, flac, m4a)"))
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setStyleSheet("color: gray; font-size: 15px; border: 2px dashed gray; border-radius: 8px; padding: 30px;")

        self.add_files_button = QPushButton(tr("Добавить файлы…"))
        self.add_folder_button = QPushButton(tr("Добавить папку…"))
        self.add_url_button = QPushButton(tr("Добавить ссылку…"))
        self.remove_button = QPushButton(tr("Убрать"))
        self.clear_button = QPushButton(tr("Очистить"))
        self.report_button = QPushButton(tr("Скопировать отчёт"))
        self.report_button.setToolTip(tr("Скопировать описание ошибки, чтобы отправить разработчику"))
        self.lyrics_button = QPushButton(tr("Текст…"))
        self.lyrics_button.setToolTip(tr("Посмотреть и поправить текст выбранной песни: со звуком и таймкодами"))
        self.listen_button = QPushButton(tr("Слушать…"))
        self.listen_button.setToolTip(tr("Прослушать дорожки, сравнить с оригиналом, свести и сохранить свой микс"))
        self.listen_button.clicked.connect(self._open_mixer)
        self.add_files_button.clicked.connect(self._choose_files)
        self.add_folder_button.clicked.connect(self._choose_folder)
        self.add_url_button.clicked.connect(self._ask_url)
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button.clicked.connect(self._clear)
        self.report_button.clicked.connect(self._copy_selected_report)
        self.lyrics_button.clicked.connect(self._edit_lyrics)
        queue_buttons = QHBoxLayout()
        for button in (self.add_files_button, self.add_folder_button, self.add_url_button, self.remove_button, self.clear_button):
            queue_buttons.addWidget(button)
        queue_buttons.addStretch()
        queue_buttons.addWidget(self.report_button)
        queue_buttons.addWidget(self.listen_button)
        queue_buttons.addWidget(self.lyrics_button)

        self.preset = QComboBox()
        self.preset.setMinimumWidth(220)
        self.preset.currentIndexChanged.connect(self._on_preset_chosen)
        self.save_preset_button = QPushButton(tr("Сохранить как…"))
        self.save_preset_button.setToolTip(tr("Сохранить текущие настройки как свой пресет"))
        self.delete_preset_button = QPushButton(tr("Удалить"))
        self.save_preset_button.clicked.connect(self._save_preset)
        self.delete_preset_button.clicked.connect(self._delete_preset)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.preset)
        preset_row.addWidget(self.save_preset_button)
        preset_row.addWidget(self.delete_preset_button)
        preset_row.addStretch()

        self.mode = QComboBox()
        for key, title in MODE_TITLES:
            self.mode.addItem(tr(title), key)
        self.mode.currentIndexChanged.connect(self._settings_changed)
        self.mix_boxes = {name: QCheckBox(tr(title)) for name, title in MIX_TITLES.items()}
        self.mix_row = QWidget()
        mix_layout = QHBoxLayout(self.mix_row)
        mix_layout.setContentsMargins(0, 0, 0, 0)
        for box in self.mix_boxes.values():
            box.setChecked(True)
            box.toggled.connect(self._settings_changed)
            mix_layout.addWidget(box)
        mix_layout.addStretch()
        self.mix_label = QLabel(tr("Смешать в минус:"))

        self.quality = QComboBox()
        for key, title in QUALITIES:
            self.quality.addItem(tr(title), key)
        self.quality.currentIndexChanged.connect(self._settings_changed)
        self.format = QComboBox()
        self.format.addItems(FORMATS)
        self.format.currentTextChanged.connect(self._settings_changed)
        self.bitrate = QComboBox()
        for value in sorted(BITRATES, reverse=True):
            self.bitrate.addItem(tr("{0} кбит/с").format(value), value)
        self.bitrate.currentIndexChanged.connect(self._settings_changed)
        self.clean_box = QCheckBox(tr("Очистить вокал от эха и шума (отдельный файл)"))
        self.clean_box.setToolTip(tr("Дополнительно сохраняет голос без реверберации, эха и шума: <имя>_vocals_clean.\nДобавляет время на обработку."))
        self.clean_box.toggled.connect(self._settings_changed)
        quality_row = QHBoxLayout()
        quality_row.addWidget(self.quality, 1)
        quality_row.addWidget(self.clean_box)
        format_row = QHBoxLayout()
        format_row.addWidget(self.format)
        format_row.addWidget(self.bitrate)
        format_row.addStretch()

        self.lyrics_box = QCheckBox(tr("Распознать текст песни (.txt и .lrc с таймкодами)"))
        self.lyrics_box.toggled.connect(self._settings_changed)
        self.language = QComboBox()
        for key, title in LANGUAGE_TITLES:
            self.language.addItem(tr(title), key)
        self.language.currentIndexChanged.connect(self._settings_changed)
        self.online_box = QCheckBox(tr("сначала искать готовый текст в интернете (LRCLIB)"))
        self.online_box.setToolTip(
            tr("Ищет текст с таймкодами по исполнителю, названию и длительности — сам звук никуда не отправляется.\nЕсли не найдено, текст распознаётся как обычно.")
        )
        self.online_box.toggled.connect(self._settings_changed)
        lyrics_row = QHBoxLayout()
        lyrics_row.addWidget(self.lyrics_box)
        lyrics_row.addWidget(self.language)
        lyrics_row.addWidget(self.online_box)
        lyrics_row.addStretch()

        self.out_dir = QLineEdit()
        self.browse_button = QPushButton(tr("Обзор…"))
        self.browse_button.clicked.connect(self._choose_out_dir)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_dir)
        out_row.addWidget(self.browse_button)

        form = QFormLayout()
        form.addRow(tr("Пресет:"), preset_row)
        form.addRow(tr("Режим:"), self.mode)
        form.addRow(self.mix_label, self.mix_row)
        form.addRow(tr("Качество:"), quality_row)
        form.addRow(tr("Формат:"), format_row)
        form.addRow(tr("Текст:"), lyrics_row)
        form.addRow(tr("Папка результата:"), out_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        self.start_button = QPushButton(tr("Разделить"))
        self.start_button.setDefault(True)
        self.cancel_button = QPushButton(tr("Отмена"))
        self.open_button = QPushButton(tr("Открыть папку результата"))
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

    def _build_menu(self) -> None:
        settings_menu = self.menuBar().addMenu(tr("Настройки"))
        theme_menu = settings_menu.addMenu(tr("Тема"))
        self.theme_group = QActionGroup(self)
        for key, title in THEMES:
            action = theme_menu.addAction(tr(title))
            action.setCheckable(True)
            action.setData(key)
            self.theme_group.addAction(action)
        self.theme_group.triggered.connect(lambda action: self.set_theme(action.data()))
        language_menu = settings_menu.addMenu(tr("Язык"))
        self.language_group = QActionGroup(self)
        for key, title in i18n.LANGUAGES:
            action = language_menu.addAction(title)
            action.setCheckable(True)
            action.setData(key)
            action.setChecked(key == i18n.language())
            self.language_group.addAction(action)
        self.language_group.triggered.connect(lambda action: self._choose_language(action.data()))
        settings_menu.addSeparator()
        settings_menu.addAction(tr("Скачать модели заранее…"), self.show_wizard)
        settings_menu.addAction(tr("Папка журналов"),lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir()))))
        help_menu = self.menuBar().addMenu(tr("Справка"))
        help_menu.addAction(tr("О программе"), self._about)

    def _choose_language(self, key: str) -> None:
        """Язык интерфейса (FR-26) меняется после перезапуска: так не нужно перестраивать окно на ходу."""
        if key == self._settings.value("ui_language", i18n.language()):
            return
        self._settings.setValue("ui_language", key)
        previous = i18n.language()
        i18n.set_language(key)  # сообщение — уже на выбранном языке
        QMessageBox.information(self, tr("Язык интерфейса"), tr("Язык интерфейса сменится после перезапуска Splitter."))
        i18n.set_language(previous)

    def show_wizard(self) -> None:
        """Мастер: видеокарта, место на диске, предзагрузка моделей (FR-25)."""
        from .wizard import FirstRunWizard

        if self.running:
            self.status.setText(tr("Дождитесь конца обработки — потом можно скачать модели."))
            return
        FirstRunWizard(worker_command(), self).exec()
        self._settings.setValue("wizard_done", "true")

    def maybe_first_run(self) -> None:
        if str(self._settings.value("wizard_done", "false")).lower() != "true":
            self.show_wizard()

    def set_theme(self, key: str) -> None:
        """Тема: как в системе, светлая или тёмная (FR-21)."""
        schemes = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}
        app = QApplication.instance()
        if app is not None and hasattr(app.styleHints(), "setColorScheme"):
            app.styleHints().setColorScheme(schemes.get(key, Qt.ColorScheme.Unknown))
        for action in self.theme_group.actions():
            action.setChecked(action.data() == key)
        self._settings.setValue("theme", key)

    def _about(self) -> None:
        QMessageBox.about(
            self,
            tr("О программе"),
            tr("<b>Splitter {0}</b><br>Делит песню на минус, вокал и другие дорожки. Вся обработка — на этом компьютере.<br><br>Видеокарта: {1}").format(__version__, self._device or 'определится при первом разделении'),
        )

    def _load_settings(self) -> None:
        value = self._settings.value
        self.set_theme(str(value("theme", "system")))
        self.out_dir.setText(value("out_dir", str(Path.home() / "Music" / "Splitter")))
        self._applying_preset = True
        try:
            self.mode.setCurrentIndex(max(0, self.mode.findData(value("mode", "vocals"))))
            self.quality.setCurrentIndex(max(0, self.quality.findData(value("quality", "high"))))
            self.format.setCurrentText(value("format", "mp3"))
            self.bitrate.setCurrentIndex(max(0, self.bitrate.findData(int(value("bitrate", 320)))))
            mix = str(value("mix", ",".join(MIX_STEMS))).split(",")
            for name, box in self.mix_boxes.items():
                box.setChecked(name in mix)
            self.lyrics_box.setChecked(str(value("lyrics", "false")).lower() == "true")
            self.language.setCurrentIndex(max(0, self.language.findData(value("language", "") or None)))
            self.online_box.setChecked(str(value("lyrics_online", "false")).lower() == "true")
            self.clean_box.setChecked(str(value("clean_vocals", "false")).lower() == "true")
        finally:
            self._applying_preset = False

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
        store("lyrics_online", "true" if self.online_box.isChecked() else "false")
        store("clean_vocals", "true" if self.clean_box.isChecked() else "false")

    def _mix(self) -> list[str]:
        return [name for name, box in self.mix_boxes.items() if box.isChecked()]

    @property
    def running(self) -> bool:
        return self._process is not None

    def _items(self) -> list[QTreeWidgetItem]:
        return [self.queue.topLevelItem(i) for i in range(self.queue.topLevelItemCount())]

    def _selected_songs(self) -> list[QTreeWidgetItem]:
        """Выбранные песни (строка готовой дорожки считается своей песней)."""
        songs = [item.parent() or item for item in self.queue.selectedItems()]
        return list(dict.fromkeys(songs))

    def _selected_lrc(self) -> Path | None:
        songs = self._selected_songs()
        if len(songs) == 1 and songs[0].data(0, LRC_ROLE):
            path = Path(songs[0].data(0, LRC_ROLE))
            return path if path.is_file() else None
        return None

    def _song_outputs(self, item: QTreeWidgetItem) -> dict[str, Path]:
        """Готовые дорожки песни, которые ещё лежат на диске."""
        try:
            outputs = json.loads(item.data(0, OUTPUTS_ROLE) or "{}")
        except ValueError:
            return {}
        return {name: Path(path) for name, path in outputs.items() if Path(path).is_file()}

    def _selected_outputs(self) -> QTreeWidgetItem | None:
        songs = self._selected_songs()
        return songs[0] if len(songs) == 1 and self._song_outputs(songs[0]) else None

    def _source_path(self, item: QTreeWidgetItem) -> Path | None:
        source = item.data(0, PATH_ROLE)
        return None if not source or is_url(source) else Path(source)

    def _open_mixer(self, item: QTreeWidgetItem | None = None) -> None:
        item = item or self._selected_outputs()
        outputs = self._song_outputs(item) if item is not None else {}
        if not outputs:
            return
        first = next(iter(outputs.values()))
        stem = item.data(0, STEM_ROLE) or first.stem.rsplit("_", 1)[0]
        dialog = MixerDialog(
            item.text(0), outputs, {name: tr(title) for name, title in TRACK_TITLES.items()}, self._source_path(item), first.with_name(stem), self.bitrate.currentData(), self
        )
        dialog.exec()
        saved = dialog.saved
        if saved is not None and str(saved) not in {item.child(i).data(0, FILE_ROLE) for i in range(item.childCount())}:
            self._add_file_rows(item, [(tr("микс"), str(saved))])
            self.status.setText(tr("Микс сохранён: {0}").format(saved.name))

    def _selected_error(self) -> QTreeWidgetItem | None:
        songs = self._selected_songs()
        return songs[0] if len(songs) == 1 and songs[0].data(0, ERROR_ROLE) else None

    def _settings_changed(self, *_) -> None:
        self._sync_preset()
        self._refresh()

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
        settings = (
            self.preset,
            self.save_preset_button,
            self.mode,
            self.mix_row,
            self.quality,
            self.clean_box,
            self.format,
            self.lyrics_box,
            self.out_dir,
            self.browse_button,
        )
        for widget in (self.add_files_button, self.add_folder_button, self.add_url_button, *settings):
            widget.setEnabled(not busy)
        self.delete_preset_button.setEnabled(not busy and self._user_preset_selected() is not None)
        self.bitrate.setEnabled(not busy and self.format.currentText() == "mp3")
        self.language.setEnabled(not busy and self.lyrics_box.isChecked())
        self.online_box.setEnabled(not busy and self.lyrics_box.isChecked())
        self.listen_button.setEnabled(self._selected_outputs() is not None)
        self.remove_button.setEnabled(not busy and bool(self.queue.selectedItems()))
        self.clear_button.setEnabled(not busy and bool(items))
        self.lyrics_button.setEnabled(self._selected_lrc() is not None)
        self.report_button.setVisible(self._selected_error() is not None)
        self.start_button.setEnabled(not busy and pending)
        self.cancel_button.setEnabled(busy)
        self._store_queue()

    # ---------- незавершённая очередь (FR-30) ----------

    def _store_queue(self) -> None:
        """Запоминает необработанные песни, чтобы предложить их при следующем запуске."""
        if not self._remember_queue:
            return
        unfinished = [item.data(0, PATH_ROLE) for item in self._items() if item.data(0, STATE_ROLE) != DONE]
        value = json.dumps(unfinished, ensure_ascii=False)
        if value != self._stored_queue:
            self._stored_queue = value
            self._settings.setValue("queue", value)

    def restore_queue(self) -> int:
        """Возвращает в очередь то, что не успели обработать в прошлый раз. Вызывается при запуске окна."""
        try:
            saved = json.loads(str(self._settings.value("queue", "[]")))
        except ValueError:
            saved = []
        self._remember_queue = True
        restored = 0
        for key in saved if isinstance(saved, list) else []:
            if not isinstance(key, str):
                continue
            if is_url(key):
                restored += self._add_item(key, key)
            elif Path(key).is_file():
                restored += self._add_item(key, Path(key).name)
        if restored:
            self.status.setText(tr("Возвращена незаконченная очередь с прошлого раза: {0} шт. Нажмите «Разделить» или «Очистить».").format(restored))
        self._refresh()
        return restored

    # ---------- пресеты ----------

    def _user_presets(self) -> dict[str, dict]:
        try:
            stored = json.loads(str(self._settings.value("presets", "{}")))
            return stored if isinstance(stored, dict) else {}
        except ValueError:
            return {}

    def _store_user_presets(self, stored: dict[str, dict]) -> None:
        self._settings.setValue("presets", json.dumps(stored, ensure_ascii=False))

    def _fill_presets(self) -> None:
        self.preset.blockSignals(True)
        self.preset.clear()
        self.preset.addItem(tr(CUSTOM_PRESET), None)
        for key, (title, _) in presets.BUILTIN.items():
            self.preset.addItem(tr(title), f"builtin:{key}")
        for name in self._user_presets():
            self.preset.addItem(f"★ {name}", f"user:{name}")
        self.preset.blockSignals(False)
        self._sync_preset()

    def _preset_values(self, data) -> dict | None:
        if not data:
            return None
        kind, _, key = data.partition(":")
        if kind == "builtin":
            return presets.builtin(key)
        return self._user_presets().get(key)

    def _user_preset_selected(self) -> str | None:
        data = self.preset.currentData()
        return data.partition(":")[2] if data and data.startswith("user:") else None

    def current_settings(self) -> dict:
        return {
            "mode": self.mode.currentData(),
            "mix": sorted(self._mix()),
            "quality": self.quality.currentData(),
            "format": self.format.currentText(),
            "bitrate": self.bitrate.currentData(),
            "lyrics": self.lyrics_box.isChecked(),
            "language": self.language.currentData(),
            "lyrics_online": self.online_box.isChecked(),
            "clean_vocals": self.clean_box.isChecked(),
        }

    def apply_settings(self, values: dict) -> None:
        self._applying_preset = True
        try:
            if "mode" in values:
                self.mode.setCurrentIndex(max(0, self.mode.findData(values["mode"])))
            if "mix" in values:
                for name, box in self.mix_boxes.items():
                    box.setChecked(name in values["mix"])
            if "quality" in values:
                self.quality.setCurrentIndex(max(0, self.quality.findData(values["quality"])))
            if "format" in values:
                self.format.setCurrentText(values["format"])
            if "bitrate" in values:
                self.bitrate.setCurrentIndex(max(0, self.bitrate.findData(values["bitrate"])))
            if "lyrics" in values:
                self.lyrics_box.setChecked(bool(values["lyrics"]))
            if "language" in values:
                self.language.setCurrentIndex(max(0, self.language.findData(values["language"])))
            if "lyrics_online" in values:
                self.online_box.setChecked(bool(values["lyrics_online"]))
            if "clean_vocals" in values:
                self.clean_box.setChecked(bool(values["clean_vocals"]))
        finally:
            self._applying_preset = False
        self._refresh()

    def _sync_preset(self) -> None:
        """Показывает в списке пресет, совпадающий с текущими настройками, или «Свои настройки»."""
        if self._applying_preset:
            return
        current = self.current_settings()
        chosen = self._preset_values(self.preset.currentData())
        if chosen is not None and presets.matches(chosen, current):
            return
        index = 0
        for i in range(1, self.preset.count()):
            values = self._preset_values(self.preset.itemData(i))
            if values is not None and presets.matches(values, current):
                index = i
                break
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(index)
        self.preset.blockSignals(False)

    def _on_preset_chosen(self, *_) -> None:
        values = self._preset_values(self.preset.currentData())
        if values is not None:
            self.apply_settings(values)
        self._refresh()

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, tr("Сохранить пресет"), tr("Название пресета:"), text=self._user_preset_selected() or "")
        name = name.strip()
        if not ok or not name:
            return
        reserved = {CUSTOM_PRESET, tr(CUSTOM_PRESET)} | {t for title, _ in presets.BUILTIN.values() for t in (title, tr(title))}
        if name in reserved:
            QMessageBox.warning(self, tr("Сохранить пресет"), tr("Так называется встроенный пресет — выберите другое название."))
            return
        stored = self._user_presets()
        if name in stored:
            answer = QMessageBox.question(self, tr("Сохранить пресет"), tr("Пресет «{0}» уже есть. Заменить?").format(name))
            if answer != QMessageBox.StandardButton.Yes:
                return
        values = self.current_settings()
        stored[name] = {key: values[key] for key in presets.SETTING_KEYS}
        self._store_user_presets(stored)
        self._fill_presets()
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(self.preset.findData(f"user:{name}"))
        self.preset.blockSignals(False)
        self.status.setText(tr("Пресет «{0}» сохранён.").format(name))
        self._refresh()

    def _delete_preset(self) -> None:
        name = self._user_preset_selected()
        if name is None:
            return
        if QMessageBox.question(self, tr("Удалить пресет"), tr("Удалить пресет «{0}»?").format(name)) != QMessageBox.StandardButton.Yes:
            return
        stored = self._user_presets()
        stored.pop(name, None)
        self._store_user_presets(stored)
        self._fill_presets()
        self._refresh()

    # ---------- очередь ----------

    def _add_item(self, key: str, title: str) -> bool:
        if key in {item.data(0, PATH_ROLE) for item in self._items()}:
            return False
        item = QTreeWidgetItem([title, ""])
        item.setData(0, PATH_ROLE, key)
        item.setToolTip(0, key)
        self.queue.addTopLevelItem(item)
        self._set_state(item, QUEUED, tr("В очереди"))
        self._describe(item)
        return True

    def _describe(self, item: QTreeWidgetItem, duration: float | None = None) -> None:
        """Подпись карточки; длительность и волна дозагружаются в фоне."""
        key = item.data(0, PATH_ROLE)
        if is_url(key):
            item.setData(0, INFO_ROLE, tr("ссылка — скачается при обработке"))
            return
        path = Path(key)
        parts = [format_length(duration)] if duration else []
        parts += [path.suffix.lstrip(".").lower(), path.parent.name]
        item.setData(0, INFO_ROLE, " · ".join(part for part in parts if part))
        if duration is None:
            try:
                self._media.submit(lambda: self.media_ready.emit(key, song_info(path)))
            except RuntimeError:
                pass  # окно закрывается

    def _on_media(self, key: str, info) -> None:
        for item in self._items():
            if item.data(0, PATH_ROLE) == key:
                item.setData(0, PEAKS_ROLE, info.peaks)
                if info.cover:
                    pixmap = QPixmap()
                    if pixmap.loadFromData(info.cover):
                        item.setData(0, COVER_ROLE, pixmap)
                self._describe(item, info.duration or 0.0)

    def add_paths(self, paths: list[Path]) -> None:
        added = sum(self._add_item(str(file.resolve()), file.name) for file in expand_paths(paths))
        if paths and not added:
            self.status.setText(tr("Новых аудиофайлов не найдено (нужны mp3, wav, flac или m4a)."))
        self._refresh()

    def add_url(self, url: str) -> bool:
        url = url.strip()
        if not is_url(url):
            self.status.setText(tr("Это не похоже на ссылку: она должна начинаться с http:// или https://."))
            return False
        added = self._add_item(url, url)
        self._refresh()
        return added

    def _ask_url(self) -> None:
        url, ok = QInputDialog.getText(self, tr("Добавить ссылку"), tr("Ссылка на трек или видео:"))
        if not ok or not url.strip():
            return
        answer = QMessageBox.question(self, tr("Авторские права"), tr("{0}\n\nДобавить ссылку в очередь?").format(tr(COPYRIGHT_NOTICE)))
        if answer == QMessageBox.StandardButton.Yes:
            self.add_url(url)

    def _set_state(self, item: QTreeWidgetItem, state: str, text: str, tooltip: str | None = None) -> None:
        item.setData(0, STATE_ROLE, state)
        item.setText(1, text)
        if tooltip is not None:
            item.setToolTip(1, tooltip)

    def _add_file_rows(self, item: QTreeWidgetItem, files: list[tuple[str, str]]) -> None:
        """Вложенные строки с готовыми файлами: двойной щелчок открывает, можно перетащить наружу."""
        for title, path in files:
            row = QTreeWidgetItem([f"{tr(title)} — {Path(path).name}", ""])
            row.setData(0, FILE_ROLE, path)
            row.setToolTip(0, path)
            item.addChild(row)

    def _choose_files(self) -> None:
        pattern = " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS))
        names, _ = QFileDialog.getOpenFileNames(self, tr("Выберите песни"), self._settings.value("last_dir", ""), tr("Аудио ({0})").format(pattern))
        if names:
            self._settings.setValue("last_dir", str(Path(names[0]).parent))
            self.add_paths([Path(n) for n in names])

    def _choose_folder(self) -> None:
        name = QFileDialog.getExistingDirectory(self, tr("Выберите папку с песнями"), self._settings.value("last_dir", ""))
        if name:
            self._settings.setValue("last_dir", name)
            self.add_paths([Path(name)])

    def _choose_out_dir(self) -> None:
        name = QFileDialog.getExistingDirectory(self, tr("Папка для результата"), self.out_dir.text())
        if name:
            self.out_dir.setText(name)

    def _remove_selected(self) -> None:
        for item in self._selected_songs():
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
            self.status.setText(tr("Папка результата пока не создана."))

    def _open_file(self, path: Path) -> None:
        if path.suffix.lower() in (".lrc", ".txt") and path.with_suffix(".lrc").is_file():
            self._edit_lyrics(path.with_suffix(".lrc"))
        elif path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.status.setText(tr("Файл не найден: {0} — возможно, его переместили или удалили.").format(path.name))

    def _edit_lyrics(self, lrc: Path | None = None) -> None:
        lrc = lrc or self._selected_lrc()
        if lrc is None:
            return
        # звук для сверки: выделенный голос, затем оригинал
        song = next((item for item in self._items() if item.data(0, LRC_ROLE) == str(lrc)), None)
        audio: dict[str, Path] = {}
        if song is not None:
            outputs = self._song_outputs(song)
            for name in ("vocals", "lead"):
                if name in outputs:
                    audio[tr(TRACK_TITLES[name])] = outputs[name]
                    break
            source = self._source_path(song)
            if source is not None and source.is_file():
                audio[tr("оригинал")] = source
        if LyricsEditor(lrc, audio, self).exec():
            self.status.setText(tr("Текст сохранён: {0} и {1}.").format(lrc.with_suffix('.txt').name, lrc.name))

    def _on_double_click(self, item: QTreeWidgetItem, *_) -> None:
        if item.data(0, FILE_ROLE):
            self._open_file(Path(item.data(0, FILE_ROLE)))
        elif item.childCount():
            item.setExpanded(not item.isExpanded())
        else:
            self._open_output()

    def _show_menu(self, position) -> None:
        item = self.queue.itemAt(position)
        if item is None:
            return
        menu = QMenu(self)
        file = item.data(0, FILE_ROLE)
        if file:
            path = Path(file)
            menu.addAction(tr("Открыть"), lambda: self._open_file(path))
            menu.addAction(tr("Показать в папке"), lambda: reveal(path))
            menu.addAction(tr("Скопировать путь"), lambda: QApplication.clipboard().setText(str(path)))
        else:
            first = item.child(0).data(0, FILE_ROLE) if item.childCount() else None
            if self._song_outputs(item):
                menu.addAction(tr("Слушать и смешать…"), lambda: self._open_mixer(item))
            if first:
                menu.addAction(tr("Показать в папке"), lambda: reveal(Path(first)))
            if item.data(0, LRC_ROLE):
                menu.addAction(tr("Текст…"), lambda: self._edit_lyrics(Path(item.data(0, LRC_ROLE))))
            if item.data(0, ERROR_ROLE):
                menu.addAction(tr("Скопировать отчёт об ошибке"), lambda: self._copy_report(item.data(0, ERROR_ROLE)))
            if self._log is not None:
                log_path = self._log.path
                menu.addAction(tr("Открыть журнал"), lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_path))))
            if not self.running:
                menu.addSeparator()
                menu.addAction(tr("Убрать из очереди"), self._remove_selected)
        if menu.actions():
            menu.exec(self.queue.viewport().mapToGlobal(position))

    def dragEnterEvent(self, event) -> None:
        # свои строки (готовые дорожки) обратно в очередь не принимаем
        if event.mimeData().hasUrls() and event.source() is None and not self.running:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if event.source() is None:
            self.add_paths([Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()])

    # ---------- отчёт об ошибке ----------

    def _copy_report(self, message: str) -> None:
        settings = {key: value for key, value in self._job.items() if key not in ("inputs", "out")}
        report = build_report(message, settings, self._device, self._log.tail() if self._log else [])
        QApplication.clipboard().setText(report)
        self.status.setText(tr("Отчёт скопирован — вставьте его в сообщение разработчику."))

    def _copy_selected_report(self) -> None:
        item = self._selected_error()
        if item is not None:
            self._copy_report(item.data(0, ERROR_ROLE))

    # ---------- обработка ----------

    def start(self) -> None:
        if self.running:
            return
        out = self.out_dir.text().strip()
        if not out:
            self.status.setText(tr("Укажите папку результата."))
            return
        self._batch = [item for item in self._items() if item.data(0, STATE_ROLE) != DONE]
        if not self._batch:
            return
        for item in self._batch:
            self._set_state(item, QUEUED, tr("В очереди"), "")
            item.setData(0, LRC_ROLE, None)
            item.setData(0, LYRICS_SOURCE_ROLE, None)
            item.setData(0, ERROR_ROLE, None)
            item.setData(0, OUTPUTS_ROLE, None)
            item.setData(0, CACHED_ROLE, None)
            item.takeChildren()
        self._save_settings()
        self._units = {}
        self._current = None
        self._cancelled = False
        self._fatal = None
        self._clock = None
        self._stderr_tail.clear()
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.status.setText(tr("Запуск…"))

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
            "lyrics_online": self.lyrics_box.isChecked() and self.online_box.isChecked(),
            "clean_vocals": self.clean_box.isChecked(),
            "ui_language": i18n.language(),  # ошибки из обработчика — на языке окна
        }
        if self._log is None:
            self._log = RunLog("gui")
        self._log.write(f"задание: {json.dumps(self._job, ensure_ascii=False)}")
        if self._taskbar is None:
            self._taskbar = taskbar.TaskbarProgress(int(self.winId()))
        self._taskbar.set_state(taskbar.NORMAL)
        self._taskbar.set_value(0, 1000)

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
        threading.Thread(target=self._read_stderr, args=(process, self._log), daemon=True).start()
        self._refresh()

    def _read_events(self, process: subprocess.Popen) -> None:
        for line in process.stdout:
            try:
                self.worker_event.emit(json.loads(line.decode("utf-8")))
            except ValueError:
                continue
        self.worker_exited.emit(process.wait())

    def _read_stderr(self, process: subprocess.Popen, log: RunLog) -> None:
        for line in process.stderr:
            text = line.decode("utf-8", errors="replace").rstrip()
            self._stderr_tail.append(text)
            log.write(f"| {text}")

    def cancel(self) -> None:
        if self._process is not None:
            self._cancelled = True
            self.status.setText(tr("Отмена…"))
            self._process.kill()

    def _units_per_file(self) -> int:
        return 2 if self._job.get("lyrics") else 1

    def _overall(self, fraction: float = 0.0) -> None:
        total = len(self._batch) * self._units_per_file()
        share = min(sum(self._units.values()) + fraction, total) / total
        self.progress.setValue(int(1000 * share))
        if self._taskbar is not None:
            self._taskbar.set_value(int(1000 * share), 1000)
        # оставшееся время — по скорости с начала расчёта (загрузку моделей не считаем)
        now = time.monotonic()
        if self._clock is None:
            self._clock = (now, share)
            return
        started, base = self._clock
        elapsed, gained = now - started, share - base
        if elapsed > 15 and gained > 0.02 and share < 1:
            self.progress.setFormat(tr("%p%  ·  осталось {0}").format(format_duration(elapsed * (1 - share) / gained)))

    def _on_event(self, event: dict) -> None:
        kind = event.get("event")
        index = event.get("index")
        item = self._batch[index] if index is not None else None
        position = tr("{0} из {1}").format(index + 1, len(self._batch)) if index is not None else ""
        if self._log is not None and kind not in NOISY_EVENTS:
            self._log.write(f"{kind} {json.dumps({k: v for k, v in event.items() if k != 'event'}, ensure_ascii=False)}")

        if kind == "device":
            self._device = event.get("name")
            if not event["gpu"]:
                self.status.setText(tr("Подходящая видеокарта NVIDIA не найдена — считаю на процессоре, один трек может занять до 10 минут."))
        elif kind == "model":
            self.status.setText(tr("Загружаю модель (при первом запуске она скачивается)…"))
        elif kind == "lyrics_model":
            self.status.setText(tr("Загружаю модель распознавания текста (при первом запуске она скачивается)…"))
        elif kind == "model_download":
            self.status.setText(tr("Скачиваю модель: {0:.0%}").format(event['value']))
        elif kind == "download_start":
            self._current = item
            self._set_state(item, RUNNING, tr("Скачивание…"))
            self.status.setText(tr("Скачиваю {0}: {1}").format(position, item.text(0)))
        elif kind == "download_progress":
            self._set_state(item, RUNNING, tr("Скачивание… {0:.0%}").format(event['value']))
        elif kind == "downloaded":
            # дальше это обычный файл: повторный запуск не будет качать заново
            item.setData(0, PATH_ROLE, event["path"])
            item.setText(0, Path(event["path"]).name)
            item.setToolTip(0, event["path"])
            self._describe(item)
            self._set_state(item, QUEUED, tr("Скачано, в очереди"))
            self._current = None
        elif kind == "start":
            self._current = item
            item.setData(0, STEM_ROLE, event.get("stem"))
            self._set_state(item, RUNNING, tr("Обработка… 0%"))
            self.queue.scrollToItem(item)
            self.status.setText(tr("Обрабатываю {0}: {1}").format(position, item.text(0)))
            self._overall()
        elif kind == "progress":
            self._set_state(item, RUNNING, tr("Обработка… {0:.0%}").format(event['value']))
            self._overall(event["value"])
        elif kind == "split":
            self._units[index] = 1
            # Qt при передаче события сортирует ключи — возвращаем порядок: минус и вокал первыми
            outputs = dict(sorted(event["outputs"].items(), key=lambda pair: TRACK_ORDER.index(pair[0]) if pair[0] in TRACK_ORDER else 99))
            self._add_file_rows(item, [(TRACK_TITLES.get(name, name), path) for name, path in outputs.items()])
            item.setData(0, OUTPUTS_ROLE, json.dumps(outputs, ensure_ascii=False))
            took = tr("из кэша") if event.get("cached") else tr("{0} с").format(event['seconds'])
            waiting = tr("Дорожки готовы, текст — в очереди") if self._job.get("lyrics") else tr("Дорожки готовы ({0})").format(took)
            if event.get("cached"):
                item.setData(0, CACHED_ROLE, True)
            self._set_state(item, SPLIT, waiting, "\n".join(outputs.values()))
            if len(self._batch) == 1:
                item.setExpanded(True)
            self._current = None
            self._overall()
        elif kind == "lyrics_start":
            self._current = item
            # при поиске в LRCLIB первый lyrics_start — поиск; если не нашлось, придёт второй — распознавание
            searching = self._job.get("lyrics_online") and item.data(0, LYRICS_SOURCE_ROLE) != "searched"
            self._set_state(item, SPLIT, tr("Ищу готовый текст…") if searching else tr("Распознаю текст… 0%"))
            self.queue.scrollToItem(item)
            action = tr("Ищу готовый текст") if searching else tr("Распознаю текст")
            self.status.setText(f"{action} {position}: {item.text(0)}")
        elif kind == "lyrics_online":
            item.setData(0, LYRICS_SOURCE_ROLE, "searched")
            if not event["found"]:
                self._set_state(item, SPLIT, tr("Готового текста нет — распознаю"), event.get("note", ""))
        elif kind == "lyrics_progress":
            self._set_state(item, SPLIT, tr("Распознаю текст… {0:.0%}").format(event['value']))
            self._overall(event["value"])
        elif kind == "lyrics":
            item.setData(0, LRC_ROLE, event["lrc"])
            item.setData(0, LYRICS_SOURCE_ROLE, event.get("source"))
            item.setToolTip(1, f"{item.toolTip(1)}\n{event['txt']}\n{event['lrc']}")
            self._add_file_rows(item, [(tr("текст"), event["txt"]), (tr("текст с таймкодами"), event["lrc"])])
        elif kind == "done":
            self._units[index] = self._units_per_file()
            lyrics = ""
            if item.data(0, LRC_ROLE):
                lyrics = tr(" + текст из LRCLIB") if item.data(0, LYRICS_SOURCE_ROLE) == "lrclib" else tr(" + текст")
            cached = tr(" (из кэша)") if item.data(0, CACHED_ROLE) else ""
            self._set_state(item, DONE, tr("Готово{0}{1}").format(lyrics, cached))
            self._current = None
            self._overall()
        elif kind == "error":
            self._units[index] = self._units_per_file()
            item.setData(0, ERROR_ROLE, event["message"])
            if item.data(0, STATE_ROLE) == SPLIT:  # дорожки есть, не вышел только текст
                self._set_state(item, DONE, tr("Готово, текст не распознан"), event["message"])
            else:
                self._set_state(item, FAILED, tr("Ошибка"), event["message"])
            self._current = None
            self._overall()
        elif kind == "fatal":
            self._fatal = event["message"]
        self._refresh()

    def _on_exit(self, code: int) -> None:
        self._process = None
        interrupted = self._current
        self._current = None
        if interrupted is not None and interrupted.data(0, STATE_ROLE) == RUNNING:
            self._remove_partial(interrupted)
        self.progress.setFormat("%p%")
        if self._log is not None:
            self._log.write(f"обработчик завершился, код {code}{', отменено' if self._cancelled else ''}")

        # у файлов, где дорожки уже записаны, результат сохраняется — не вышел только текст
        reason = tr("отменено") if self._cancelled else tr("прервано")
        for item in self._batch:
            if item.data(0, STATE_ROLE) == SPLIT:
                note = tr("без текста") if self._job.get("lyrics") else ""
                self._set_state(item, DONE, tr("Готово{0} ({1})").format(', ' + note if note else '', reason))

        unfinished = [item for item in self._batch if item.data(0, STATE_ROLE) not in (DONE, FAILED)]
        failed = [item for item in self._batch if item.data(0, STATE_ROLE) == FAILED]
        done = [item for item in self._batch if item.data(0, STATE_ROLE) == DONE]
        summary = tr("Готово: {0} из {1}.").format(len(done), len(self._batch))

        if self._cancelled:
            for item in unfinished:
                if item.data(0, STATE_ROLE) == RUNNING:  # начатые; нетронутые остаются «В очереди»
                    self._set_state(item, QUEUED, tr("Отменено"))
            self.status.setText(tr("Отменено. {0}").format(summary))
            self._taskbar_state(taskbar.NONE)
        elif self._fatal or code != 0 or unfinished:
            message = self._fatal or tr("Обработчик неожиданно завершился.\n") + "\n".join(self._stderr_tail)
            for item in unfinished:
                item.setData(0, ERROR_ROLE, message)
                self._set_state(item, FAILED, tr("Ошибка"), message)
            self.status.setText(tr("Обработка прервана из-за ошибки."))
            self._taskbar_state(taskbar.ERROR)
            self._show_fatal(message)
        else:
            self.progress.setValue(1000)
            if failed:
                summary += tr(" С ошибкой: {0} — выберите песню и нажмите «Скопировать отчёт», чтобы отправить разработчику.").format(len(failed))
            elif self._job.get("lyrics"):
                summary += tr(" Чтобы послушать дорожки или поправить текст, выберите песню: «Слушать…» и «Текст…».")
            else:
                summary += tr(" Дорожки — под каждой песней; «Слушать…» — прослушать и свести свой микс.")
            self.status.setText(summary)
            self._taskbar_state(taskbar.ERROR if failed else taskbar.NONE)
            self._notify(tr("Готово: {0} из {1}").format(len(done), len(self._batch)) + (tr(", с ошибкой: {0}").format(len(failed)) if failed else ""))
        self._refresh()

    def _taskbar_state(self, state: int) -> None:
        if self._taskbar is not None:
            self._taskbar.set_state(state)

    def _notify(self, text: str) -> None:
        """Сообщает об окончании, если пользователь ушёл в другое окно."""
        if self.isActiveWindow():
            return
        QApplication.alert(self)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self._tray is None:
            self._tray = QSystemTrayIcon(self.windowIcon(), self)
            self._tray.messageClicked.connect(self._bring_to_front)
            self._tray.activated.connect(self._bring_to_front)
        self._tray.show()
        self._tray.showMessage("Splitter", text, self.windowIcon(), 10000)
        QTimer.singleShot(30000, self._tray.hide)

    def _bring_to_front(self, *_) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _show_fatal(self, message: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Critical, tr("Ошибка"), message, parent=self)
        copy = box.addButton(tr("Скопировать отчёт"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is copy:
            self._copy_report(message)

    def _remove_partial(self, item: QTreeWidgetItem) -> None:
        """Убирает недописанные .part после отмены или сбоя."""
        source = item.data(0, PATH_ROLE)
        if is_url(source) or not self._job:
            return
        stem = item.data(0, STEM_ROLE) or Path(source).stem
        options = Options(
            fmt=self._job["format"], mode=self._job["mode"], mix=tuple(self._job["mix"]), clean_vocals=self._job.get("clean_vocals", False)
        )
        out = Path(self._job["out"])
        for name in output_names(options):
            part_path(out / f"{stem}_{name}.{options.fmt}").unlink(missing_ok=True)

    def closeEvent(self, event) -> None:
        if self.running:
            answer = QMessageBox.question(self, tr("Идёт обработка"), tr("Прервать обработку и выйти?"))
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            process, interrupted = self._process, self._current
            self.cancel()
            process.wait()
            if interrupted is not None and interrupted.data(0, STATE_ROLE) == RUNNING:
                self._remove_partial(interrupted)
        self._save_settings()
        if self._tray is not None:
            self._tray.hide()
        self._media.shutdown(wait=False, cancel_futures=True)
        event.accept()


def install_qt_translations(app: QApplication, language: str) -> None:
    """Стандартные кнопки Qt («Yes/No», «Cancel») — на языке интерфейса."""
    from PySide6.QtCore import QLibraryInfo, QTranslator

    translator = QTranslator(app)
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load(f"qtbase_{language}", folder):
        app.installTranslator(translator)


def main() -> None:
    if sys.platform == "win32":
        # своя группа на панели задач — с нашей иконкой, а не python.exe
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Splitter.Splitter")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("Splitter")
    language = str(QSettings("splitter", "splitter").value("ui_language", "") or i18n.system_language())
    i18n.set_language(language)
    install_qt_translations(app, language)
    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = MainWindow()
    window.restore_queue()
    window.add_paths([Path(arg) for arg in sys.argv[1:]])
    window.show()
    QTimer.singleShot(400, window.maybe_first_run)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
