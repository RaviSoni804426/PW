"""Logging for a process nobody is watching.

The service runs headless from boot, so the log file is the only account of
what happened. It rotates by size (not by day) because a laptop that is off
for a week would otherwise keep stale daily files while a chatty device-retry
loop fills the current one.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def configure(log_dir: Path | None = None, level: str = "INFO", console: bool = True) -> None:
    """Install handlers on the root logger. Safe to call more than once."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "ongoingrec.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # These libraries log every poll cycle at INFO, which would bury the
    # recorder's own messages in a file that must stay readable during support.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
