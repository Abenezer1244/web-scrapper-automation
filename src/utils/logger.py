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

# Everything up to the next line that is not database error text (see below).
_UNTIL_NOT_DB = r"(?:(?!\n\[SQL|\n\(Background|\n\nThe above exception|\n\nDuring handling)[\s\S])*"

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
    # P0-04: presigned R2/S3 links are bearer credentials until they expire.
    (re.compile(r"(?i)(X-Amz-(?:Signature|Credential|Security-Token)=)[^&\s\"'<>]+"), r"\1[REDACTED]"),
    # P0-04: search terms in access-log URLs are owner names and addresses.
    (re.compile(r"(?i)([?&](?:q|search)=)[^&\s\"'<>]+"), r"\1[REDACTED]"),
    # P0-04: hide_parameters drops SQLAlchemy's "[parameters: ...]", but Postgres
    # echoes values in too many message shapes to list one by one, so these two
    # rules key on WHERE the values sit and fail closed up to the next line that
    # is not database text: SQLAlchemy's "[SQL" / "(Background" lines or
    # Python's chained-exception separators (tracebacks after it stay readable).
    #  1. Diagnostic blocks: DETAIL / CONTEXT, and bare "Key (" / "Failing row
    #     contains (" (asyncpg may drop the DETAIL: prefix).
    #  2. A psycopg2/asyncpg primary message's quoted value: invalid input,
    #     malformed array literal, date/time out of range, enum, and the rest.
    (re.compile(r"(DETAIL:|CONTEXT:|Key \(|Failing row contains \()" + _UNTIL_NOT_DB), r"\1[REDACTED]"),
    (re.compile(r'((?:psycopg2|asyncpg)[^\n]*?: )"' + _UNTIL_NOT_DB), r'\1"[REDACTED]'),
]


def _scrub(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


_exc_formatter = logging.Formatter()


def install_global_redaction(*extra_loggers: logging.Logger) -> None:
    """Best-effort: attach the redaction filter to the root + uvicorn loggers.

    setup_logger() already filters its own handlers, but modules that use
    ``logging.getLogger(...)`` directly (e.g. middleware) bypass that. Adding
    the filter at the logger level here scrubs records emitted directly to
    those loggers; the per-source fingerprinting remains the primary defense
    for PII. Idempotent — safe to call more than once. ``extra_loggers`` get the
    same treatment (Celery passes its root, task and multiprocessing loggers).

    Known limit: filters are per sink. A handler added after this runs, on a
    logger not set up by setup_logger(), is not covered; give it the filter.
    """
    names = [None, "uvicorn", "uvicorn.error", "uvicorn.access", "security"]
    loggers = [logging.getLogger(name) if name else logging.getLogger() for name in names]
    for lg in [*loggers, *extra_loggers]:
        if _redaction_filter not in lg.filters:
            lg.addFilter(_redaction_filter)
        for handler in lg.handlers:
            if _redaction_filter not in handler.filters:
                handler.addFilter(_redaction_filter)
    # P0-04: the API never configures the root logger, so a bare
    # logging.getLogger() record (e.g. api.unhandled's 500 traceback) falls
    # through to logging.lastResort, which no logger-level filter reaches.
    last = logging.lastResort
    if last is not None and _redaction_filter not in last.filters:
        last.addFilter(_redaction_filter)


class _RedactionFilter(logging.Filter):
    """Rewrite log records to scrub secret-shaped substrings before output."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Each part is scrubbed on its own: a failure in one must neither drop
        # the record nor skip the others.
        try:
            msg = record.getMessage()
            redacted = _scrub(msg)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        # P0-04: handlers format exc_info AFTER filters run and then reuse
        # record.exc_text, so the traceback is scrubbed here. It may already be
        # cached unscrubbed by an earlier handler, so always scrub what is there.
        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = _exc_formatter.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = _scrub(record.exc_text)
        except Exception:
            pass
        try:
            if record.stack_info:
                record.stack_info = _scrub(record.stack_info)
        except Exception:
            pass
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
