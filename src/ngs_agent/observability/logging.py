from __future__ import annotations

import logging
import sys
from pathlib import Path


def configure_logging(logs_dir: str, verbose: bool = False) -> None:
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))

    file_handler = logging.FileHandler(Path(logs_dir) / "ngs.log", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))

    root.addHandler(console_handler)
    root.addHandler(file_handler)
