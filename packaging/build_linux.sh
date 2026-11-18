#!/usr/bin/env bash
# Сборка для Linux: папка packaging/dist/Splitter и архив Splitter-linux-x86_64.tar.gz.
# Нужны uv (https://docs.astral.sh/uv/) и компилятор C: пакет diffq не выкладывает готовую сборку под Linux
# (Ubuntu: sudo apt install build-essential). Для прослушивания в окне нужна libportaudio2.
# Запуск из корня проекта: bash packaging/build_linux.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v cc >/dev/null && [ -z "${CC:-}" ]; then
  echo "Нужен компилятор C для пакета diffq: sudo apt install build-essential (или задайте CC)" >&2
  exit 1
fi

uv sync
uv run pyinstaller packaging/splitter.spec --noconfirm --distpath packaging/dist --workpath packaging/build --log-level WARN
tar -C packaging/dist -czf packaging/dist/Splitter-linux-x86_64.tar.gz Splitter
echo "Готово: packaging/dist/Splitter-linux-x86_64.tar.gz (внутри Splitter/Splitter и Splitter/splitter-cli)"
