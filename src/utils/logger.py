import hashlib
import hmac
import logging
import re
from datetime import datetime

import colorlog

from src.config import settings


def email_fingerprint(email: str | None) -> str:
    """Return a stable, non-reversible fingerprint of an email for logs.

    PII (M2): lets ops correlate repeated activity on the same account
    (identical fingerprint) without putting an enumerable plaintext address —
    or a dictionary-checkable bare hash — in the logs. Keyed HMAC with the
    server SECRET_KEY so an attacker with the logs can't brute a known email
    list against it.
    """
    if not email:
        return "none"
    key = (settings.SECRET_KEY or "bridgeleads-log-pepper").encode()
    msg = email.strip().lower().encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:12]

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
    # PII (M2): mask the local-part of any email so logs can't be used to
    # enumerate accounts or harvest contacts; keep the first char + domain so
    # delivery/ops logs stay debuggable (e.g. "j***@gmail.com").
    (re.compile(r"(?i)([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*(@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"), r"\1***\2"),
    # PII (M2): redact a phone number ONLY when it follows a `phone`-ish label
    # (phone=, "phone":, primary_phone=, phone_number=, 'phone': '...'). A bare
    # 10-digit redaction is unsafe here — county parcel IDs are 10 digits and
    # appear all over scraper logs, so the label is required. The value matcher
    # allows internal spaces/punctuation ("+1 (253) 261-1057") but stops at a
    # non-numeric token so it never eats the following field.
    (re.compile(
        r"(?i)((?:primary_|mobile_|cell_|home_|work_|landline_)?phone(?:_number)?[\"']?\s*[:=]\s*[\"']?)"
        r"\+?[\d().\-]+(?:[ \t][\d().\-]+){0,4}"
    ), r"\1[REDACTED_PHONE]"),
]


def install_global_redaction() -> None:
    """Best-effort: attach the redaction filter to the root + uvicorn loggers.

    setup_logger() already filters its own handlers, but modules that use
    ``logging.getLogger(...)`` directly (e.g. middleware) bypass that. Adding
    the filter at the logger level here scrubs records emitted directly to
    those loggers; the per-source fingerprinting remains the primary defense
    for PII. Idempotent — safe to call more than once.
    """
    names = [None, "uvicorn", "uvicorn.error", "uvicorn.access", "security"]
    for name in names:
        lg = logging.getLogger(name) if name else logging.getLogger()
        if _redaction_filter not in lg.filters:
            lg.addFilter(_redaction_filter)
        for handler in lg.handlers:
            if _redaction_filter not in handler.filters:
                handler.addFilter(_redaction_filter)


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
