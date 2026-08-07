from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(base_dir: Path, template_name: str) -> None:
    logs_dir = base_dir / "logs" / template_name
    logs_dir.mkdir(parents=True, exist_ok=True)

    log_file = logs_dir / "pipeline.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Avoid duplicate handlers if setup_logging is called twice.
    if root.handlers:
        return

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=500_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    root.addHandler(console)
    root.addHandler(file_handler)

    # Silence noisy libraries a bit.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)
