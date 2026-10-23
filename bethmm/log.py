"""Logging setup: one readable log file in ``~/.local/share/beth-mod-manager/logs``.

Every file operation is logged by the module performing it through the normal
``logging`` loggers (``bethmm.*``), so the log reads as a history of what the
app did to the disk.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from bethmm import paths

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_handler: logging.Handler | None = None

#: Control characters a terminal acts on -- ESC starts the sequences that move
#: the cursor, retitle the window or set the clipboard -- written as visible
#: ``\xNN`` instead; tabs and newlines stay. Names from archives and plugin
#: headers can hold them, and the log is printed with ``cat`` as often as not.
VISIBLE_CONTROLS = {c: f"\\x{c:02x}" for c in (*range(0x20), 0x7F, *range(0x80, 0xA0)) if c not in (0x09, 0x0A)}


def visible_controls(text: str) -> str:
    return text.translate(VISIBLE_CONTROLS)


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return visible_controls(super().format(record))


def setup_logging(level: int = logging.INFO, log_file: Path | None = None) -> Path:
    """Attach a rotating file handler to the ``bethmm`` logger (idempotent)."""
    global _handler
    log_file = log_file or paths.log_path()
    logger = logging.getLogger("bethmm")
    if _handler is not None:
        if getattr(_handler, "baseFilename", None) == str(log_file.resolve()):
            return log_file
        logger.removeHandler(_handler)
        _handler.close()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8", errors="backslashreplace"
    )
    _handler.setFormatter(_Formatter(FORMAT, DATE_FORMAT))
    logger.addHandler(_handler)
    logger.setLevel(level)
    return log_file
