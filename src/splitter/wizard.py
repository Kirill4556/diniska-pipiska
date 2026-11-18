"""Мастер первого запуска (FR-25): видеокарта, место на диске и заранее скачанные модели."""

import json
import shutil
import subprocess
import sys
import threading

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .core import MODEL_PACKS
from .paths import cache_dir, models_dir

PACK_TITLES = {
    "fast": "Минус и вокал — быстро",
    "high": "Минус и вокал — качественно (основной режим)",
    "stems": "4 дорожки: вокал, ударные, бас, остальное",
    "backing": "Бэк-вокал отдельно",
    "max": "Качество «максимум» (вторая модель)",
    "clean": "Очистка вокала от эха и шума",
}
DEFAULT_PACKS = ("fast", "high")
WHISPER_FILES = {"turbo": ("large-v3-turbo.pt", 1543), "small": ("small.pt", 461)}


def pack_downloaded(pack: str) -> bool:
    return all((models_dir() / model).exists() for model in MODEL_PACKS[pack][0])


def whisper_downloaded() -> bool:
    folder = cache_dir() / "whisper"
    return any((folder / name).exists() for name, _ in WHISPER_FILES.values())


def size_text(megabytes: float) -> str:
    return f"{megabytes / 1024:.1f} ГБ".replace(".", ",") if megabytes >= 1000 else f"{megabytes:.0f} МБ"


class FirstRunWizard(QDialog):
    event = Signal(dict)
    exited = Signal(int)

    def __init__(self, command: list[str], parent=None):
        super().__init__(parent)
        self._command = command
        self._process: subprocess.Popen | None = None
        self._gpu: bool | None = None
        self._error: str | None = None
        self.setWindowTitle("Splitter — первый запуск")
        self.resize(560, 470)

        intro = QLabel(
            "<b>Добро пожаловать!</b><br>Splitter делит песни на дорожки нейросетями прямо на этом компьютере. "
            "Модели скачиваются один раз, дальше интернет не нужен. Можно скачать их сейчас, чтобы первое "
            "разделение не ждало загрузки."
        )
        intro.setWordWrap(True)
        self.device = QLabel("Видеокарта: проверяю…")
        self.device.setWordWrap(True)
        free = shutil.disk_usage(cache_dir().anchor or cache_dir()).free / 1024**3
        self.disk = QLabel(f"Свободно на диске с моделями: {free:.0f} ГБ".replace(".", ","))
        self._free_mb = free * 1024

        self.boxes: dict[str, QCheckBox] = {}
        packs_layout = QVBoxLayout()
        for pack, title in PACK_TITLES.items():
            box = QCheckBox()
            if pack_downloaded(pack):
                box.setText(f"{title} — уже скачано")
                box.setChecked(True)
                box.setEnabled(False)
            else:
                box.setText(f"{title} — ≈ {size_text(MODEL_PACKS[pack][1])}")
                box.setChecked(pack in DEFAULT_PACKS)
            box.toggled.connect(self._update_total)
            self.boxes[pack] = box
            packs_layout.addWidget(box)
        self.lyrics_box = QCheckBox()
        if whisper_downloaded():
            self.lyrics_box.setText("Распознавание текста песни — уже скачано")
            self.lyrics_box.setChecked(True)
            self.lyrics_box.setEnabled(False)
        else:
            self.lyrics_box.setText("Распознавание текста песни — ≈ 1,5 ГБ (без видеокарты ≈ 460 МБ)")
        self.lyrics_box.toggled.connect(self._update_total)
        packs_layout.addWidget(self.lyrics_box)

        self.total = QLabel()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.download_button = QPushButton("Скачать выбранное")
        self.download_button.setDefault(True)
        self.download_button.clicked.connect(self._download)
        self.close_button = QPushButton("Пропустить")
        self.close_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.download_button)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.device)
        layout.addWidget(self.disk)
        layout.addSpacing(6)
        layout.addWidget(QLabel("Что скачать:"))
        layout.addLayout(packs_layout)
        layout.addWidget(self.total)
        layout.addStretch()
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

        self.event.connect(self._on_event)
        self.exited.connect(self._on_exit)
        self._update_total()
        self._run({"action": "prefetch", "packs": [], "lyrics": False})  # только проверка видеокарты

    def selected(self) -> tuple[list[str], bool]:
        packs = [pack for pack, box in self.boxes.items() if box.isChecked() and box.isEnabled()]
        return packs, self.lyrics_box.isChecked() and self.lyrics_box.isEnabled()

    def _update_total(self, *_) -> None:
        packs, lyrics = self.selected()
        size = sum(MODEL_PACKS[pack][1] for pack in packs) + (1543 if lyrics and self._gpu is not False else 461 if lyrics else 0)
        text = f"Скачать: ≈ {size_text(size)}" if size else "Ничего скачивать не нужно."
        if size and size > self._free_mb * 0.9:
            text += " — места на диске может не хватить!"
        self.total.setText(text)
        self.download_button.setEnabled(bool(size) and self._process is None)

    # ---------- обработчик ----------

    def _run(self, job: dict) -> None:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            process = subprocess.Popen(
                self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags
            )
        except OSError as exc:
            self.device.setText(f"Видеокарта: не удалось проверить ({exc})")
            return
        self._process = process
        process.stdin.write(json.dumps(job).encode("utf-8"))
        process.stdin.close()

        def read():
            try:
                for line in process.stdout:
                    try:
                        self.event.emit(json.loads(line.decode("utf-8")))
                    except ValueError:
                        continue
                self.exited.emit(process.wait())
            except RuntimeError:
                pass  # окно мастера уже закрыто

        threading.Thread(target=read, daemon=True).start()
        self._update_total()

    def _on_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "device":
            self._gpu = event["gpu"]
            if event["gpu"]:
                self.device.setText(f"Видеокарта: {event['name']}. Разделение будет быстрым.")
            else:
                self.device.setText(
                    f"Видеокарта: {event['name']}.<br>Обработка пойдёт на процессоре — это в несколько раз медленнее; "
                    "лучше выбирать «Быстро»."
                )
            self._update_total()
        elif kind == "prefetch_start":
            title = PACK_TITLES.get(event["pack"], "Распознавание текста песни")
            self.status.setText(f"Скачиваю {event['index'] + 1} из {event['total']}: {title}…")
            self.progress.setValue(int(1000 * event["index"] / event["total"]))
            self._step = (event["index"], event["total"])
        elif kind == "model_download":
            index, total = getattr(self, "_step", (0, 1))
            self.progress.setValue(int(1000 * (index + event["value"]) / total))
        elif kind == "fatal":
            self._error = event["message"]

    def _download(self) -> None:
        packs, lyrics = self.selected()
        if self._process is not None or not (packs or lyrics):
            return
        for box in (*self.boxes.values(), self.lyrics_box):
            box.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.status.setText("Начинаю…")
        self._downloading = True
        self._run({"action": "prefetch", "packs": packs, "lyrics": lyrics})

    def _on_exit(self, code: int) -> None:
        self._process = None
        if getattr(self, "_downloading", False):
            self._downloading = False
            if self._error or code != 0:
                self.status.setText(f"Скачать не получилось: {self._error or 'обработчик завершился с ошибкой'}. "
                                    "Модели скачаются при первом разделении.")
            else:
                self.progress.setValue(1000)
                self.status.setText("Готово: модели скачаны, можно работать без интернета.")
                self.close_button.setText("Готово")
            self.download_button.setEnabled(False)
        self._update_total()

    def done(self, result: int) -> None:
        if self._process is not None:
            self._process.kill()
            self._process = None
        super().done(result)
