"""Logging setup shared by the CLI and the daemon."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Configure root logging once. Safe to call repeatedly."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    root.addHandler(stream)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3)
        rotating.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
        root.addHandler(rotating)

    # Third-party libraries are chatty at DEBUG; keep them at WARNING.
    for noisy in ("urllib3", "requests", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        setup_logging(os.getenv("LOG_LEVEL", "INFO"), os.getenv("LOG_FILE"))
    return logging.getLogger(name)


def redact(value: str | None, keep: int = 4) -> str:
    """Render a secret safely for logs: 'abcd...wxyz' -> 'abcd***'."""
    if not value:
        return "<unset>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 6}"
