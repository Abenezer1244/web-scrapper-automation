"""Central log-redaction filter tests (security review Low: no central redaction).

Defense-in-depth: a stray log call must not leak a credential to console/file/Loki.
P0-04 adds: bound SQL values, Postgres DETAIL values, tracebacks, stack info,
presigned links, search terms, and Celery's own handlers. Real DB errors, real
handlers and formatters, the real Celery signal. No mocks.
"""
import io
import logging
import sys

import colorlog
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.utils.logger import _redaction_filter

_MARKER = "Jane Q Marker 1234 Elm St"
_SIGNED = ("https://acct.r2.cloudflarestorage.com/exports/a.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256"
           "&X-Amz-Credential=AKIAEXAMPLE%2F20261010%2Fauto%2Fs3%2Faws4_request"
           "&X-Amz-Signature=deadbeefcafe0123456789&X-Amz-Expires=3600")


def _render(record: logging.LogRecord, formatter: logging.Formatter) -> str:
    """Emit through a real StreamHandler carrying the filter, return the output."""
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(formatter)
    handler.addFilter(_redaction_filter)
    handler.handle(record)
    return out.getvalue()


def _exc_record(exc: BaseException, msg: str = "boom") -> logging.LogRecord:
    try:
        raise exc
    except BaseException:
        return logging.LogRecord("t", logging.ERROR, "f", 1, msg, (), sys.exc_info())


def _redact(msg: str, *args) -> str:
    record = logging.LogRecord("t", logging.INFO, "f", 1, msg, args, None)
    _redaction_filter.filter(record)
    return record.getMessage()


@pytest.mark.parametrize(
    "msg",
    [
        "Authorization: Bearer abc123.def456-tok",
        'login body {"password": "hunter2secret"}',
        "config api_key=sk-abcdef1234567890abcdef",
        "x-api-key: deadbeefdeadbeef1234",
        "download token=eyJhdr.payloadpart123.sigpart45678",
        "jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.sigpart_abc123",
        "stripe key sk-live-abcdef1234567890ABCDEF",
        "Set-Cookie: sb-access-token=eyJhdr.body.sig; HttpOnly; Secure",
        "Cookie: session=abc123; csrf=def456",
        "fetching https://admin:s3cretpass@internal.host/path",
    ],
)
def test_secrets_are_redacted(msg):
    out = _redact(msg)
    assert "REDACTED" in out, f"not redacted: {out}"


@pytest.mark.parametrize(
    "msg",
    [
        "record saved for 123 Main St, Tacoma WA",
        "Scrape complete: 152 new leads (8 duplicates)",
        "Navigated to https://recordsearch.kingcounty.gov/search",
    ],
)
def test_normal_messages_unchanged(msg):
    assert _redact(msg) == msg


@pytest.mark.parametrize(
    "msg, secret",
    [
        (f"export ready {_SIGNED}", "deadbeefcafe0123456789"),
        (f"export ready {_SIGNED}", "AKIAEXAMPLE"),
        ('127.0.0.1 - "GET /results?q=Jane+Doe HTTP/1.1" 200', "Jane"),
        ('"GET /results?page=2&search=Jane%20Doe%20Tacoma HTTP/1.1" 200', "Jane"),
        ('DETAIL:  Key (email_idx)=(Jane Doe) already exists.', "Jane"),
        ('DETAIL:  Key (a)=(MARKER1) conflicts with existing key (a)=(MARKER2).', "MARKER1"),
        ('DETAIL:  Key (a)=(MARKER1) conflicts with existing key (a)=(MARKER2).', "MARKER2"),
        ('DETAIL:  Key (user_id)=(Jane (x) Doe) is not present in table "users".', "Jane"),
        ('DETAIL:  Failing row contains (7, Jane Doe, 12 Elm St, null).', "Jane"),
    ],
)
def test_p0_04_patterns(msg, secret):
    out = _redact(msg)
    assert secret not in out and "REDACTED" in out, out


def test_p0_04_detail_patterns_stay_on_their_line():
    msg = "DETAIL:  Key (a)=(MARKER1) already exists.\nnext line (kept) stays"
    assert _redact(msg).endswith("next line (kept) stays")


def test_hide_parameters_keeps_bound_values_out_of_sql_errors():
    from src.db.session import sync_engine

    with sync_engine.connect() as conn:
        with pytest.raises(DBAPIError) as caught:
            conn.execute(text("SELECT CAST(:v AS text), 1/0"), {"v": _MARKER})
        conn.rollback()
    assert "division by zero" in str(caught.value)
    assert _MARKER not in str(caught.value)
    # The value really was bound: hide_parameters only changed the text.
    assert caught.value.params == {"v": _MARKER}


def test_postgres_detail_value_is_scrubbed_from_a_logged_unique_violation():
    from src.db.session import sync_engine

    with sync_engine.connect() as conn:
        conn.execute(text("CREATE TEMP TABLE p0_redact (k text PRIMARY KEY) ON COMMIT DROP"))
        conn.execute(text("INSERT INTO p0_redact VALUES (:k)"), {"k": _MARKER})
        with pytest.raises(IntegrityError) as caught:
            conn.execute(text("INSERT INTO p0_redact VALUES (:k)"), {"k": _MARKER})
        conn.rollback()
    assert _MARKER in str(caught.value)  # Postgres's DETAIL echoes the key
    out = _render(_exc_record(caught.value, "insert failed"), logging.Formatter())
    assert "already exists" in out and _MARKER not in out


@pytest.mark.parametrize(
    "formatter",
    [logging.Formatter("%(levelname)s %(message)s"),
     colorlog.ColoredFormatter("%(log_color)s%(levelname)s%(reset)s %(message)s")],
    ids=["plain", "colorlog"],
)
def test_traceback_is_scrubbed_in_rendered_output(formatter):
    out = _render(_exc_record(RuntimeError(f"upload failed for {_SIGNED}")), formatter)
    assert "Traceback" in out and "RuntimeError" in out
    assert "deadbeefcafe0123456789" not in out and "AKIAEXAMPLE" not in out


def test_traceback_cached_by_an_earlier_unfiltered_handler_is_still_scrubbed():
    record = _exc_record(RuntimeError(f"upload failed for {_SIGNED}"))
    logging.Formatter().format(record)  # an unfiltered handler formats first
    assert "deadbeefcafe0123456789" in record.exc_text
    out = _render(record, logging.Formatter())
    assert "deadbeefcafe0123456789" not in out


def test_stack_info_is_scrubbed():
    record = logging.LogRecord("t", logging.INFO, "f", 1, "here", (), None,
                               sinfo=f'Stack (most recent call last):\n  url = "{_SIGNED}"')
    out = _render(record, logging.Formatter())
    assert "Stack (most recent call last)" in out and "deadbeefcafe0123456789" not in out


def test_unformattable_message_still_has_its_traceback_scrubbed():
    record = _exc_record(RuntimeError(f"upload failed for {_SIGNED}"), "%d")
    record.args = ("not a number",)
    _redaction_filter.filter(record)
    assert "deadbeefcafe0123456789" not in record.exc_text


def test_celery_logger_setup_attaches_the_filter():
    # The real Celery signal worker and beat fire after building their handlers,
    # sent to a test-only logger so global logging is untouched.
    from celery.signals import after_setup_logger, after_setup_task_logger

    import src.workers  # noqa: F401  registers the receiver

    for signal in (after_setup_logger, after_setup_task_logger):
        lg = logging.getLogger(f"p0_04_celery_{signal.name}")
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        lg.addHandler(handler)
        lg.propagate = False
        try:
            signal.send(sender=None, logger=lg, loglevel=logging.INFO, logfile=None,
                        format="%(message)s", colorize=False)
            assert _redaction_filter in handler.filters
            lg.warning("download %s", _SIGNED)
            assert "deadbeefcafe0123456789" not in out.getvalue()
        finally:
            lg.removeHandler(handler)


@pytest.mark.parametrize(
    "msg",
    [
        "DETAIL:  Key (k)=(" + "Jane Doe " * 80 + ") already exists.",  # past the 500 bound
        "DETAIL:  Key (k)=(Jane Doe, no closing paren",
        "DETAIL:  Failing row contains (7, Jane Doe, no closing paren",
        "DETAIL:  Key (k)=(Jane Doe)\r\nnext",
    ],
    ids=["long-key", "unclosed-key", "unclosed-row", "crlf"],
)
def test_detail_patterns_fail_closed(msg):
    assert "Jane" not in _redact(msg)


def test_last_resort_handler_is_filtered(capsys):
    # The API never configures the root logger: a handler-less logger's record
    # goes to logging.lastResort (stderr). api.unhandled's 500 traceback did.
    from src.utils.logger import install_global_redaction

    install_global_redaction()
    lg = logging.getLogger("p0_04_no_handlers")
    lg.propagate = False
    capsys.readouterr()
    try:
        raise RuntimeError(f"upload failed for {_SIGNED}")
    except RuntimeError:
        lg.exception("Unhandled error ref=abc")
    err = capsys.readouterr().err
    assert "Unhandled error ref=abc" in err and "Traceback" in err
    assert "deadbeefcafe0123456789" not in err
