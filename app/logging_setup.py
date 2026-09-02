"""Logging configuration with secret redaction.

Console output stays short and readable; the full log goes to
``data/logs/archiver.log`` with rotation. A filter scrubs known secrets
(API hash, session strings) from every record so they can never leak into a
log file that might later be shared.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Iterable

REDACTED = "***redacted***"


class SecretRedactingFilter(logging.Filter):
    """Replace configured secret values anywhere in the formatted message."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Only redact values long enough to be a real secret.
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        if not any(secret in message for secret in self._secrets):
            return True
        for secret in self._secrets:
            message = message.replace(secret, REDACTED)
        record.msg = message
        record.args = ()
        return True


class ConsoleFormatter(logging.Formatter):
    """Compact, human readable console lines."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


def setup_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
    secrets: Iterable[str] = (),
) -> None:
    """Configure the root logger. Safe to call once at startup."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    redactor = SecretRedactingFilter(secrets)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(ConsoleFormatter())
    console.addFilter(redactor)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "archiver.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    # Telethon and SQLAlchemy are extremely chatty at DEBUG level.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
