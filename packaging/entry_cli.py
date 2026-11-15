"""Точка входа собранного приложения: командная строка и фоновый обработчик для окна."""

import sys

if "--worker" in sys.argv[1:]:
    from splitter.worker import main
else:
    from splitter.cli import main

main()
