#!/usr/bin/env bash
# Сборка для Linux: папка packaging/dist/Splitter и архив Splitter-linux-x86_64.tar.gz.
# Нужен uv (https://docs.astral.sh/uv/). Запуск из корня проекта: bash packaging/build_linux.sh
set -euo pipefail
cd "$(dirname "$0")/.."

uv sync
uv run pyinstaller packaging/splitter.spec --noconfirm --distpath packaging/dist --workpath packaging/build --log-level WARN
tar -C packaging/dist -czf packaging/dist/Splitter-linux-x86_64.tar.gz Splitter
echo "Готово: packaging/dist/Splitter-linux-x86_64.tar.gz (внутри Splitter/Splitter и Splitter/splitter-cli)"
