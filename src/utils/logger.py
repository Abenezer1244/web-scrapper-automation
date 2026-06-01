import logging
import re
from datetime import datetime

import colorlog

from src.config import settings

# ─── Secret redaction (defense-in-depth) ─────────────────────────────────────
# Last-line scrub so a stray log call can't leak a credential to console/file/
# Loki. Patterns target common secret shapes; the per-record cost is small and
# only the matching records are rewritten.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._\-]+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(password\"?\s*[:=]\s*\"?)[^\s\"',}]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[^\s\"',}]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\btoken\s*[:=]\s*\"?[A-Za-z0-9._\-]+"), "token=[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\bsk[_-][A-Za-z0-9_\-]{16,}"), "[REDACTED_KEY]"),  # sk-..., sk_live_..., sk_test_...
    (re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*).+"), r"\1[REDACTED]"),  # Cookie / Set-Cookie header value
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s:@]+@"), r"\1[REDACTED]@"),  # basic-auth creds in URL
]


class _RedactionFilter(logging.Filter):
    """Rewrite log records to scrub secret-shaped substrings before output."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for pattern, repl in _SECRET_PATTERNS:
            redacted = pattern.sub(repl, redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


_redaction_filter = _RedactionFilter()


def setup_logger(name: str | None = None, log_file: bool = True) -> logging.Logger:
    """Set up a logger with colored console output and optional file logging."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))

    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger

    # Console handler with colors
    console_handler = colorlog.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_formatter = colorlog.ColoredFormatter(
        "%(log_color)s%(levelname)-8s%(reset)s %(blue)s%(name)s%(reset)s - %(message)s",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "red,bg_white",
        },
    )
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(_redaction_filter)
    logger.addHandler(console_handler)

    # File handler
    if log_file:
        settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_filename = f"bridgeleads_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(
            settings.LOGS_DIR / log_filename,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        file_handler.addFilter(_redaction_filter)
        logger.addHandler(file_handler)

    return logger
