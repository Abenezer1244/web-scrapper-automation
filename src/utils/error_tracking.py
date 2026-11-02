"""P0-03: error tracking (Sentry) that exports stack traces and nothing else.

An unhandled error in the API, a Celery worker or beat used to reach a log line at
best and nobody at worst. This sends it to Sentry with a stack trace, under one
hard rule: nothing that could be PII, a secret or a signed URL leaves the box.

How (Codex consult, P0-03):
- Allowlist, not regex. ``_sanitize_event`` builds a NEW event from a fixed set of
  fields: exception type, module and mechanism, stack frames reduced to
  module/function/file/line (no local variables, no source lines), the service,
  environment and release, a validated request ref and a static transaction name
  (a route template or a task name, never a raw URL). Exception message text is
  replaced by a constant because no scrubber can promise a message is clean.
  Request data, user, extra, breadcrumbs, log records and every other context are
  dropped. A sanitiser error drops the event; it never falls back to the original.
- Only the integrations this file names are enabled (no auto-enabling, no logging,
  stdlib, httpx, redis or SQLAlchemy hooks). Tracing, profiling, sessions, logs and
  metrics are all off, so only error events exist to send.
- Empty ``SENTRY_DSN`` = the SDK is never initialised. A DSN that is not a plain
  https Sentry ingest URL is refused with a static log line and also stays off;
  the value is never logged or echoed.
- Each process role initialises itself (API at startup, the worker parent before
  it forks, beat at start). Initialising twice in one process is a no-op.
"""
import json
import logging
import math
import os
import re
import sys
import types
import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("error_tracking")

SERVICES = frozenset({"api", "worker", "beat"})

# job_outcome tag values (P0-03 5d): a handled path or the watchdog failed a job.
JOB_OUTCOMES = frozenset({"failed", "watchdog_failed"})

# What the exported exception carries instead of its message text.
REDACTED_VALUE = "[redacted]"

# Sentry's ingest hosts: o<org>.ingest.sentry.io, or a regional .us/.de variant.
_INGEST_HOST = re.compile(r"^o\d{1,20}\.ingest(?:\.(?:us|de))?\.sentry\.io$")
_PUBLIC_KEY = re.compile(r"^[0-9a-f]{32}$")
_PROJECT_PATH = re.compile(r"^/\d{1,20}$")

_REF = re.compile(r"^[0-9a-f]{12}$")
_RELEASE = re.compile(r"^[0-9a-f]{7,40}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Route templates ("/jobs/{job_id}") and Celery task names ("src.workers.tasks.x").
_TRANSACTION = re.compile(r"^[A-Za-z0-9_./{}-]{1,200}$")
# Transaction sources that are a template or a code name, never a raw request path.
_STATIC_TRANSACTION_SOURCES = frozenset({"route", "task", "component", "view"})
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,199}$")
_FILENAME = re.compile(r"^[A-Za-z0-9_./<>-]{1,300}$")
_MECHANISM_TYPE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_LEVELS = frozenset({"fatal", "error", "warning"})
_EVENT_ID = re.compile(r"^[0-9a-f]{32}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$")

_MAX_EXCEPTIONS = 10
_MAX_FRAMES = 100

# (pid, service) of the client this process initialised, or None.
_initialised: tuple[int, str] | None = None


def _valid_dsn(raw: str) -> str | None:
    """The DSN when it is a plain https Sentry ingest URL, else None. Never raises.

    ``sentry_sdk.utils.BadDsn`` messages quote parts of the input, so the SDK's own
    parser is not used for the refusal path.
    """
    if not raw or "﻿" in raw:
        return None
    dsn = raw.strip()
    try:
        parts = urlsplit(dsn)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or not host:
        return None
    if parts.password is not None or parts.query or parts.fragment:
        return None
    if not _INGEST_HOST.fullmatch(host) or not _PUBLIC_KEY.fullmatch(parts.username or ""):
        return None
    if not _PROJECT_PATH.fullmatch(parts.path):
        return None
    return dsn


def _str_or_none(value: Any, pattern: re.Pattern) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _sanitize_frame(frame: Any) -> dict:
    if not isinstance(frame, dict):
        return {}
    out: dict = {}
    for key, pattern in (("module", _IDENTIFIER), ("function", _IDENTIFIER), ("filename", _FILENAME)):
        value = _str_or_none(frame.get(key), pattern)
        if value is not None:
            out[key] = value
    lineno = frame.get("lineno")
    if isinstance(lineno, int) and not isinstance(lineno, bool):
        out["lineno"] = lineno
    if isinstance(frame.get("in_app"), bool):
        out["in_app"] = frame["in_app"]
    return out


def _sanitize_mechanism(mechanism: Any) -> dict | None:
    if not isinstance(mechanism, dict):
        return None
    out: dict = {}
    kind = _str_or_none(mechanism.get("type"), _MECHANISM_TYPE)
    if kind is not None:
        out["type"] = kind
    for key in ("handled", "is_exception_group"):
        if isinstance(mechanism.get(key), bool):
            out[key] = mechanism[key]
    for key in ("exception_id", "parent_id"):
        value = mechanism.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
    source = _str_or_none(mechanism.get("source"), _IDENTIFIER)
    if source is not None:
        out["source"] = source
    return out or None


def _sanitize_exception(exc: Any) -> dict | None:
    if not isinstance(exc, dict):
        return None
    kind = _str_or_none(exc.get("type"), _IDENTIFIER)
    if kind is None:
        return None
    out: dict = {"type": kind, "value": REDACTED_VALUE}
    module = _str_or_none(exc.get("module"), _IDENTIFIER)
    if module is not None:
        out["module"] = module
    mechanism = _sanitize_mechanism(exc.get("mechanism"))
    if mechanism is not None:
        out["mechanism"] = mechanism
    stacktrace = exc.get("stacktrace")
    frames = stacktrace.get("frames") if isinstance(stacktrace, dict) else None
    if isinstance(frames, list):
        # Sentry orders frames oldest first; keep the newest (the failing end).
        kept = [f for f in (_sanitize_frame(fr) for fr in frames[-_MAX_FRAMES:]) if f]
        if kept:
            out["stacktrace"] = {"frames": kept}
    return out


def _sanitize_tags(tags: Any) -> dict:
    out: dict = {}
    if not isinstance(tags, dict):
        return out
    if tags.get("service") in SERVICES:
        out["service"] = tags["service"]
    ref = _str_or_none(tags.get("ref"), _REF)
    if ref is not None:
        out["ref"] = ref
    if tags.get("job_outcome") in JOB_OUTCOMES:
        out["job_outcome"] = tags["job_outcome"]
    return out


def _sanitize_runtime(contexts: Any) -> dict | None:
    runtime = contexts.get("runtime") if isinstance(contexts, dict) else None
    if not isinstance(runtime, dict):
        return None
    out = {
        key: runtime[key]
        for key in ("name", "version")
        if isinstance(runtime.get(key), str) and re.fullmatch(r"^[A-Za-z0-9_.+-]{1,64}$", runtime[key])
    }
    return out or None


def _timestamp(value: Any) -> Any:
    """A datetime, a finite epoch number or a strict ISO-8601 string; else None."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) and value > 0 else None
    if isinstance(value, str) and _TIMESTAMP.fullmatch(value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return value
    return None


def _sanitize_event(event: Any, hint: Any = None) -> dict | None:
    """Build the exported event from an allowlist. None = do not send.

    Wired as ``before_send``. Any failure drops the event: the original is never
    returned, because it is the thing that may carry PII.
    """
    try:
        if not isinstance(event, dict):
            return None
        values = (event.get("exception") or {}).get("values")
        if not isinstance(values, list):
            # Only exception events are exported; bare messages carry free text.
            return None
        exceptions = [e for e in (_sanitize_exception(v) for v in values[-_MAX_EXCEPTIONS:]) if e]
        if not exceptions:
            return None
        # The SDK appends hint["attachments"] AFTER before_send; drop them here (and
        # the transport filter refuses attachment items anyway).
        if isinstance(hint, dict):
            hint.pop("attachments", None)
        out: dict = {"exception": {"values": exceptions}, "platform": "python"}
        out["event_id"] = _str_or_none(event.get("event_id"), _EVENT_ID) or uuid.uuid4().hex
        timestamp = _timestamp(event.get("timestamp"))
        if timestamp is not None:
            out["timestamp"] = timestamp
        if event.get("level") in _LEVELS:
            out["level"] = event["level"]
        environment = _str_or_none(event.get("environment"), _ENVIRONMENT)
        if environment is not None:
            out["environment"] = environment
        release = _str_or_none(event.get("release"), _RELEASE)
        if release is not None:
            out["release"] = release
        tags = _sanitize_tags(event.get("tags"))
        if tags:
            out["tags"] = tags
        info = event.get("transaction_info")
        source = info.get("source") if isinstance(info, dict) else None
        transaction = _str_or_none(event.get("transaction"), _TRANSACTION)
        if transaction is not None and source in _STATIC_TRANSACTION_SOURCES:
            out["transaction"] = transaction
            out["transaction_info"] = {"source": source}
        runtime = _sanitize_runtime(event.get("contexts"))
        if runtime is not None:
            out["contexts"] = {"runtime": runtime}
        sdk = event.get("sdk")
        if isinstance(sdk, dict):
            out["sdk"] = {
                key: sdk[key]
                for key in ("name", "version")
                if isinstance(sdk.get(key), str) and re.fullmatch(r"^[A-Za-z0-9_.+-]{1,64}$", sdk[key])
            }
        return out
    except Exception:  # noqa: BLE001 — must never fall back to the unsanitised event
        _logger.error("error_tracking: event dropped, sanitiser failed")
        return None


def _filter_envelope(envelope: Any) -> Any:
    """The envelope reduced to one re-sanitised event item, or None. Never raises.

    The last gate before the network. ``before_send`` only sees error events;
    sessions, check-ins, attachments, client reports, logs, metrics and user
    feedback travel in their own envelope items and would skip it. Only an
    ``event`` item leaves, rebuilt from the sanitiser's output, under headers
    reduced to the event id and the SDK name/version (no DSN, no trace context).
    """
    from sentry_sdk.envelope import Envelope, Item, PayloadRef

    try:
        events = [item for item in envelope.items if item.type == "event"]
        if len(events) != 1 or len(envelope.items) != 1:
            return None
        payload = events[0].payload
        data = payload.json if payload.json is not None else json.loads(payload.get_bytes())
        event = _sanitize_event(data)
        if event is None:
            return None
        headers: dict = {"event_id": event["event_id"]}
        sdk = envelope.headers.get("sdk")
        if isinstance(sdk, dict):
            headers["sdk"] = {
                key: sdk[key]
                for key in ("name", "version")
                if isinstance(sdk.get(key), str) and re.fullmatch(r"^[A-Za-z0-9_.+-]{1,64}$", sdk[key])
            }
        return Envelope(headers=headers, items=[Item(payload=PayloadRef(json=event), type="event")])
    except Exception:  # noqa: BLE001 — fail closed: nothing leaves
        _logger.error("error_tracking: envelope dropped, filter failed")
        return None


def _event_only(transport_cls: type) -> type:
    """``transport_cls`` with every outgoing envelope passed through _filter_envelope."""

    class EventOnly(transport_cls):  # type: ignore[valid-type,misc]
        def capture_envelope(self, envelope: Any) -> None:
            filtered = _filter_envelope(envelope)
            if filtered is not None:
                super().capture_envelope(filtered)

    EventOnly.__name__ = f"EventOnly{transport_cls.__name__}"
    return EventOnly


def _event_only_delegate(inner: Any) -> Any:
    """An injected transport instance behind the same envelope filter."""
    from sentry_sdk.transport import Transport

    class EventOnlyDelegate(Transport):
        def capture_envelope(self, envelope: Any) -> None:
            filtered = _filter_envelope(envelope)
            if filtered is not None:
                inner.capture_envelope(filtered)

        def flush(self, timeout: float, callback: Any = None) -> None:
            inner.flush(timeout, callback)

        def kill(self) -> None:
            inner.kill()

    return EventOnlyDelegate()


def _drop(*_args: Any, **_kwargs: Any) -> None:
    """before_breadcrumb / before_send_transaction / _log / _metric: send nothing."""
    return None


def _integrations(service: str) -> Iterable[Any]:
    from sentry_sdk.integrations.atexit import AtexitIntegration
    from sentry_sdk.integrations.dedupe import DedupeIntegration
    from sentry_sdk.integrations.excepthook import ExcepthookIntegration

    base: list[Any] = [AtexitIntegration(), DedupeIntegration(), ExcepthookIntegration()]
    if service == "api":
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        # "url" = the matched route template; an unmatched path is source "url"
        # and is dropped by the sanitiser.
        return [*base, StarletteIntegration(transaction_style="url"), FastApiIntegration(transaction_style="url")]
    from sentry_sdk.integrations.celery import CeleryIntegration

    return [*base, CeleryIntegration(propagate_traces=False, monitor_beat_tasks=False)]


class _BeatErrorCapture(logging.Handler):
    """Sends ERROR records that carry an exception to Sentry (beat only).

    Beat catches a failed publish and logs it (celery/beat.py apply_entry,
    ``exc_info=True``) instead of raising, so no integration ever sees it. Records
    without an exception (connection-retry chatter) are ignored; the message text
    is never sent, only the exception, through the same sanitiser.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        exc_info = record.exc_info
        if not exc_info or exc_info[1] is None:
            return
        import sentry_sdk

        sentry_sdk.capture_exception(exc_info)


def capture_beat_errors() -> None:
    """beat_init: attach _BeatErrorCapture to ``celery.beat`` once; beat client only."""
    if _initialised is None or _initialised != (os.getpid(), "beat"):
        return
    beat_logger = logging.getLogger("celery.beat")
    if not any(isinstance(h, _BeatErrorCapture) for h in beat_logger.handlers):
        beat_logger.addHandler(_BeatErrorCapture())


class JobFailedError(Exception):
    """A scrape job reached FAILED through a handled path (5d). Never raised."""


class WatchdogFailedError(JobFailedError):
    """The watchdog failed a job whose retries ran out (5d). Never raised."""


_MAX_CALLER_FRAMES = 30


def _caller_traceback(skip: int) -> types.TracebackType | None:
    """A traceback over the caller's stack, oldest frame first, capped.

    Lets a synthetic exception point at the line that failed the job. Only frame
    positions are exported (locals and source are off, and the sanitiser keeps
    module/function/file/line).
    """
    frames: list[types.FrameType] = []
    frame: types.FrameType | None = sys._getframe(skip)
    try:
        while frame is not None and len(frames) < _MAX_CALLER_FRAMES:
            frames.append(frame)
            frame = frame.f_back
        tb = None
        for caller in frames:  # nearest first, so the last one built is the oldest
            tb = types.TracebackType(tb, caller, caller.f_lasti, caller.f_lineno)
        return tb
    finally:
        del frame, frames


def report_job_failure(outcome: str) -> None:
    """One Sentry event for a job that just became FAILED. Never raises.

    Call it once, right after the FAILED write is known to have committed (the
    CAS that landed it), so a job is reported once however many workers race.
    Inside an ``except`` the active exception becomes the cause; otherwise the
    event carries the caller's stack. The tag is set on a scope local to this
    capture. Best effort: a crash between commit and here loses the report.
    """
    if _initialised is None:
        return
    if outcome not in JOB_OUTCOMES:
        _logger.error("error_tracking: unknown job outcome; not reported")
        return
    error: JobFailedError | None = None
    try:
        import sentry_sdk

        error = (WatchdogFailedError if outcome == "watchdog_failed" else JobFailedError)("job failed")
        cause = sys.exc_info()[1]
        if cause is not None:
            error.__cause__ = cause
            error.__suppress_context__ = True
        error.__traceback__ = _caller_traceback(2)
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("job_outcome", outcome)
            sentry_sdk.capture_exception(error)
    except Exception:  # noqa: BLE001 — reporting must never change how a job fails
        _logger.error("error_tracking: job failure not reported")
    finally:
        if error is not None:
            error.__traceback__ = None
        del error


def tag_request_ref(ref: str) -> None:
    """Put an API 500's ref on the request's isolation scope; no-op when tracking is off.

    The Starlette integration captures the re-raised exception after the handler
    returns, inside the same request scope, so the event carries the ref the
    client was shown. The sanitiser keeps it only as 12 hex characters.
    """
    if _initialised is None:
        return
    import sentry_sdk

    sentry_sdk.get_isolation_scope().set_tag("ref", ref)


def init_error_tracking(service: str, *, transport: Any = None) -> bool:
    """Initialise Sentry for this process role. True when a client is active.

    No-op (False) when SENTRY_DSN is empty or refused. A second call for the same
    service in the same process is a no-op (True); a different service is refused.
    ``transport`` replaces the network transport's sender (the tests pass one that
    keeps the serialised envelopes in memory); either way every envelope goes
    through _filter_envelope first.
    """
    global _initialised
    if service not in SERVICES:
        raise ValueError("unknown error-tracking service")
    pid = os.getpid()
    if _initialised is not None and _initialised[0] == pid:
        if _initialised[1] == service:
            return True
        _logger.error("error_tracking: already initialised for another service; refused")
        return False
    raw = settings.SENTRY_DSN
    if not raw:
        return False
    dsn = _valid_dsn(raw)
    if dsn is None:
        _logger.error("error_tracking: SENTRY_DSN is not an https Sentry ingest DSN; error tracking off")
        return False
    if os.environ.get("SENTRY_PRINT_ENVELOPES", "").strip():
        # The SDK would wrap the transport in a printer that writes every raw
        # envelope (sessions, check-ins included) to stderr BEFORE the filter.
        _logger.error("error_tracking: SENTRY_PRINT_ENVELOPES is set; error tracking off")
        return False

    import sentry_sdk
    from sentry_sdk.transport import HttpTransport

    sender: Any = _event_only(HttpTransport) if transport is None else _event_only_delegate(transport)
    environment = _str_or_none(settings.ENVIRONMENT.strip().lower(), _ENVIRONMENT)
    release = _str_or_none(os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip().lower(), _RELEASE)
    sentry_sdk.init(
        dsn=dsn,
        transport=sender,
        environment=environment,
        release=release,
        server_name="",
        default_integrations=False,
        auto_enabling_integrations=False,
        integrations=list(_integrations(service)),
        send_default_pii=False,
        include_local_variables=False,
        include_source_context=False,
        max_request_body_size="never",
        max_breadcrumbs=0,
        attach_stacktrace=False,
        sample_rate=1.0,
        traces_sample_rate=None,
        trace_propagation_targets=[],
        propagate_traces=False,
        profiles_sample_rate=None,
        profile_session_sample_rate=None,
        auto_session_tracking=False,
        enable_logs=False,
        enable_metrics=False,
        send_client_reports=False,
        # Spotlight is a second sender that runs BEFORE the transport (and its
        # filter); False here beats a SENTRY_SPOTLIGHT env var.
        spotlight=False,
        debug=False,
        before_send=_sanitize_event,
        before_breadcrumb=_drop,
        before_send_transaction=_drop,
        before_send_log=_drop,
        before_send_metric=_drop,
    )
    sentry_sdk.set_tag("service", service)
    _initialised = (pid, service)
    _logger.info("error_tracking: enabled for %s", service)
    return True
