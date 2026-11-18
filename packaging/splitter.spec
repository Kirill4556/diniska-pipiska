# PyInstaller: одна папка, два запускаемых файла — Splitter (окно) и splitter-cli (консоль + обработчик).
# Сборка: uv run pyinstaller packaging/splitter.spec --noconfirm

from PyInstaller.utils.hooks import collect_all, copy_metadata

datas, binaries, hiddenimports = [], [], []
# пакеты с файлами данных и модулями, которые подгружаются по имени
for package in ("audio_separator", "whisper", "yt_dlp_ejs", "imageio_ffmpeg", "librosa", "lazy_loader"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
# audio-separator узнаёт свою версию через метаданные пакета
for dist in ("audio-separator", "torch", "onnxruntime", "tqdm", "requests", "packaging", "numpy", "regex"):
    datas += copy_metadata(dist)
# иконка окна (packaging/make_icon.py)
datas += [("../src/splitter/assets", "splitter/assets")]

a = Analysis(
    ["entry_gui.py", "entry_cli.py"],
    pathex=["../src"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "IPython", "matplotlib"],
    noarchive=False,
)
# У torch в метаданных лежит дерево лицензий сторонних библиотек с очень длинными путями:
# при установке в глубокую папку Windows они упираются в лимит 260 символов и установка срывается.
# Убираем только их (dist-info/licenses/third_party); основные лицензии остаются.
MAX_RELATIVE_PATH = 110
a.datas = [
    entry
    for entry in a.datas
    if not (".dist-info" in entry[0] and "third_party" in entry[0] and len(entry[0]) + len("_internal/") > MAX_RELATIVE_PATH)
]
pyz = PYZ(a.pure)


def scripts_without(entry):
    return [item for item in a.scripts if item[0] != entry]


gui = EXE(
    pyz,
    scripts_without("entry_cli"),
    exclude_binaries=True,
    name="Splitter",
    console=False,
    icon="icon.ico",
    upx=False,
)
cli = EXE(
    pyz,
    scripts_without("entry_gui"),
    exclude_binaries=True,
    name="splitter-cli",
    console=True,
    icon="icon.ico",
    upx=False,
)
COLLECT(gui, cli, a.binaries, a.datas, name="Splitter", upx=False)
