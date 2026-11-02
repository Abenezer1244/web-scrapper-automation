"""P0-03 error tracking: what leaves the box is a stack trace and nothing else.

Every scenario that initialises the SDK runs in a fresh interpreter: sentry_sdk.init
and its integrations patch process-wide state, which must not leak into the rest of
the suite. The real SDK runs end to end; the only thing swapped is the transport,
for one that keeps each serialised envelope (the exact bytes that would go on the
wire) in memory instead of posting it. Canaries planted in every place a request,
a task or a log could put data must appear nowhere in those bytes.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.utils.error_tracking import REDACTED_VALUE, _sanitize_event, _valid_dsn

_ROOT = Path(__file__).resolve().parents[1]
_GOOD_DSN = "https://0123456789abcdef0123456789abcdef@o12345.ingest.us.sentry.io/678"
_SHA = "9b4ca49c0123456789abcdef0123456789abcdef"

_CANARY_EMAIL = "canary.owner@example.com"
_CANARY_PHONE = "+1-206-555-0147"
_CANARY_JWT = "eyJhdr.canaryjwtpayload.sigcanary"  # JWT-shaped, but not a real token (gitleaks)
_CANARY_SIGNED = "X-Amz-Signature=deadbeefcanary"
_CANARY_PATH_SECRET = "pathsecretcanary0p003"
_CANARY_SQL = "1234 CANARY STREET"
_CANARY_ARG = "taskargcanary"
_CANARY_LOCAL = "localvarcanary"
_CANARIES = (
    _CANARY_EMAIL, _CANARY_PHONE, _CANARY_JWT, _CANARY_SIGNED, _CANARY_PATH_SECRET,
    _CANARY_SQL, _CANARY_ARG, _CANARY_LOCAL,
)

# Prepended to every scenario: an in-memory transport and a JSON report on stdout.
_PRELUDE = textwrap.dedent(
    """
    import json, sys
    import sentry_sdk
    from sentry_sdk.transport import Transport

    class MemoryTransport(Transport):
        def __init__(self, options=None):
            super().__init__(options)
            self.envelopes = []

        def capture_envelope(self, envelope):
            self.envelopes.append(envelope.serialize().decode("utf-8", "replace"))

    transport = MemoryTransport()

    def report(**data):
        data["envelopes"] = transport.envelopes
        data["item_types"] = [
            json.loads(line).get("type")
            for env in transport.envelopes
            for line in env.splitlines()[1:]
            if line.startswith("{") and '"type"' in line and '"length"' in line
        ]
        print("REPORT " + json.dumps(data))
    """
)


def _run(scenario: str, *, dsn: str | None = _GOOD_DSN) -> dict:
    env = dict(os.environ)
    env.pop("SENTRY_DSN", None)
    if dsn is not None:
        env["SENTRY_DSN"] = dsn
    env["RAILWAY_GIT_COMMIT_SHA"] = _SHA
    code = _PRELUDE + textwrap.dedent(scenario)
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=_ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("REPORT ")]
    assert done.returncode == 0 and lines, done.stderr[-3000:]
    out = json.loads(lines[-1][len("REPORT "):])
    out["stderr"] = done.stderr
    return out


def _assert_clean(text: str) -> None:
    for canary in _CANARIES:
        assert canary not in text, canary


_LEAKY_ERROR = textwrap.dedent(
    f"""
    from src.utils.error_tracking import init_error_tracking

    assert init_error_tracking("worker", transport=transport)
    sentry_sdk.set_user({{"email": "{_CANARY_EMAIL}", "id": "{_CANARY_PHONE}"}})
    sentry_sdk.set_extra("args", ["{_CANARY_ARG}"])
    sentry_sdk.set_context("celery_job", {{"args": ["{_CANARY_ARG}"], "kwargs": {{"url": "{_CANARY_SIGNED}"}}}})
    sentry_sdk.set_tag("path", "/hooks/{_CANARY_PATH_SECRET}")
    sentry_sdk.add_breadcrumb(category="query", message="SELECT '{_CANARY_SQL}'")

    def lookup_owner(row):
        {_CANARY_LOCAL} = row
        raise RuntimeError("owner {_CANARY_EMAIL} at {_CANARY_SQL} token {_CANARY_JWT} {_CANARY_SIGNED}")

    try:
        try:
            lookup_owner("{_CANARY_PHONE}")
        except RuntimeError as inner:
            raise ValueError("wrapped {_CANARY_EMAIL}") from inner
    except ValueError:
        sentry_sdk.capture_exception()
    sentry_sdk.flush()
    report(active=sentry_sdk.get_client().is_active())
    """
)


def test_exported_event_is_type_and_frames_only():
    out = _run(_LEAKY_ERROR)
    assert out["active"] is True
    assert len(out["envelopes"]) == 1
    assert out["item_types"] == ["event"]  # no transaction, session, log, check-in or attachment
    envelope = out["envelopes"][0]
    _assert_clean(envelope)
    event = json.loads(envelope.splitlines()[-1])
    types = [v["type"] for v in event["exception"]["values"]]
    assert types == ["RuntimeError", "ValueError"]  # the chain survives
    assert {v["value"] for v in event["exception"]["values"]} == {REDACTED_VALUE}
    functions = [f.get("function") for v in event["exception"]["values"] for f in v["stacktrace"]["frames"]]
    assert "lookup_owner" in functions
    frame_keys = {k for v in event["exception"]["values"] for f in v["stacktrace"]["frames"] for k in f}
    assert frame_keys <= {"module", "function", "filename", "lineno", "in_app"}
    assert set(event) <= {
        "exception", "platform", "event_id", "timestamp", "level", "environment", "release",
        "tags", "transaction", "transaction_info", "contexts", "sdk",
    }
    assert event["tags"] == {"service": "worker"}
    assert event["release"] == _SHA
    assert "user" not in event and "extra" not in event and "breadcrumbs" not in event
    assert set(event.get("contexts", {})) <= {"runtime"}


def test_empty_dsn_never_initialises_the_sdk():
    out = _run(
        """
        from src.utils.error_tracking import init_error_tracking

        ok = init_error_tracking("api", transport=transport)
        try:
            raise RuntimeError("never sent")
        except RuntimeError:
            sentry_sdk.capture_exception()
        report(ok=ok, active=sentry_sdk.get_client().is_active())
        """,
        dsn=None,
    )
    assert out == {**out, "ok": False, "active": False, "envelopes": []}


@pytest.mark.parametrize(
    "bad",
    [
        "http://0123456789abcdef0123456789abcdef@o12345.ingest.us.sentry.io/678",
        "﻿" + _GOOD_DSN,
        "https://0123456789abcdef0123456789abcdef@evil.example.com/678",
        "https://0123456789abcdef0123456789abcdef@o12345.ingest.us.sentry.io.evil.com/678",
        "https://0123456789abcdef0123456789abcdef:secret@o12345.ingest.us.sentry.io/678",
        "https://0123456789abcdef0123456789abcdef@o12345.ingest.us.sentry.io:8443/678",
        "https://0123456789abcdef0123456789abcdef@o12345.ingest.us.sentry.io/678?x=1",
        "https://o12345.ingest.us.sentry.io/678",
        "https://0123456789abcdef0123456789abcdef@o12345.ingest.us.sentry.io/api/678",
        "https://[::1/678",
        "not a dsn",
    ],
)
def test_refused_dsns(bad):
    assert _valid_dsn(bad) is None


def test_dsn_whitespace_is_trimmed_and_regions_accepted():
    assert _valid_dsn("  " + _GOOD_DSN + "\n") == _GOOD_DSN
    for host in ("o1.ingest.sentry.io", "o1.ingest.de.sentry.io"):
        assert _valid_dsn(f"https://0123456789abcdef0123456789abcdef@{host}/9")


def test_refused_dsn_stays_off_and_is_never_echoed():
    marker = "evilhostcanary"
    out = _run(
        """
        from src.utils.error_tracking import init_error_tracking

        report(ok=init_error_tracking("beat", transport=transport), active=sentry_sdk.get_client().is_active())
        """,
        dsn=f"https://0123456789abcdef0123456789abcdef@{marker}.example.com/678",
    )
    assert out["ok"] is False and out["active"] is False and out["envelopes"] == []
    assert "not an https Sentry ingest DSN" in out["stderr"]
    assert marker not in out["stderr"]


def test_second_init_is_idempotent_and_another_service_is_refused():
    out = _run(
        """
        from src.utils.error_tracking import init_error_tracking

        first = init_error_tracking("worker", transport=transport)
        client = sentry_sdk.get_client()
        again = init_error_tracking("worker", transport=transport)
        other = init_error_tracking("api", transport=transport)
        report(first=first, again=again, other=other, same=sentry_sdk.get_client() is client)
        """
    )
    assert (out["first"], out["again"], out["other"], out["same"]) == (True, True, False, True)


def test_message_events_are_not_exported():
    out = _run(
        f"""
        from src.utils.error_tracking import init_error_tracking

        init_error_tracking("worker", transport=transport)
        sentry_sdk.capture_message("owner {_CANARY_EMAIL}")
        sentry_sdk.flush()
        report()
        """
    )
    assert out["envelopes"] == []


def test_unknown_service_is_a_programming_error():
    from src.utils.error_tracking import init_error_tracking

    with pytest.raises(ValueError):
        init_error_tracking("scheduler")


class _ExplodingEvent(dict):
    def get(self, *_args, **_kwargs):
        raise RuntimeError(_CANARY_EMAIL)


def test_sanitiser_failure_drops_the_event(caplog):
    event = _ExplodingEvent(exception={"values": [{"type": "RuntimeError", "value": _CANARY_EMAIL}]})
    assert _sanitize_event(event) is None
    assert _CANARY_EMAIL not in caplog.text


def _event(**extra):
    return {
        "exception": {"values": [{"type": "KeyError", "value": _CANARY_EMAIL, "stacktrace": {"frames": [
            {"module": "src.api.x", "function": "f", "filename": "src/api/x.py", "lineno": 3,
             "abs_path": f"/app/{_CANARY_PATH_SECRET}.py", "vars": {"a": _CANARY_ARG},
             "context_line": _CANARY_SQL, "pre_context": [_CANARY_SQL]},
        ]}}]},
        **extra,
    }


def test_route_template_is_kept_raw_url_is_dropped():
    kept = _sanitize_event(_event(transaction="/jobs/{job_id}", transaction_info={"source": "route"}))
    assert kept["transaction"] == "/jobs/{job_id}"
    raw = _sanitize_event(_event(
        transaction=f"/webhooks/tracerfy/{_CANARY_PATH_SECRET}", transaction_info={"source": "url"}
    ))
    assert "transaction" not in raw
    _assert_clean(json.dumps(raw))


def test_frames_lose_paths_locals_and_source():
    out = _sanitize_event(_event(request={"url": _CANARY_SIGNED}, server_name=_CANARY_PHONE,
                                 tags={"service": "api", "ref": "0123456789ab", "user": _CANARY_EMAIL},
                                 modules={"x": _CANARY_ARG}, fingerprint=[_CANARY_EMAIL]))
    assert out["exception"]["values"][0]["stacktrace"]["frames"] == [
        {"module": "src.api.x", "function": "f", "filename": "src/api/x.py", "lineno": 3}
    ]
    assert out["tags"] == {"service": "api", "ref": "0123456789ab"}
    _assert_clean(json.dumps(out))


def test_invalid_ref_tag_is_dropped():
    out = _sanitize_event(_event(tags={"ref": _CANARY_PATH_SECRET}))
    assert "tags" not in out


def test_non_event_telemetry_never_leaves(tmp_path):
    attachment = tmp_path / "canary.txt"
    attachment.write_text(_CANARY_EMAIL, encoding="utf-8")
    out = _run(
        f"""
        from sentry_sdk.crons import capture_checkin
        from src.utils.error_tracking import init_error_tracking

        init_error_tracking("worker", transport=transport)
        scope = sentry_sdk.get_isolation_scope()
        sentry_sdk.set_user({{"email": "{_CANARY_EMAIL}", "ip_address": "203.0.113.7"}})
        scope.start_session()
        scope.end_session()
        capture_checkin(monitor_slug="{_CANARY_PATH_SECRET}", status="ok")
        sentry_sdk.get_current_scope().add_attachment(bytes=b"{_CANARY_SQL}", filename="{_CANARY_ARG}.txt")
        sentry_sdk.get_current_scope().add_attachment(path=r"{attachment}")
        try:
            raise KeyError("{_CANARY_PHONE}")
        except KeyError:
            sentry_sdk.capture_exception()
        sentry_sdk.get_client().flush()
        report()
        """
    )
    assert out["item_types"] == ["event"]  # the session, check-in and attachments were all refused
    _assert_clean("".join(out["envelopes"]))
    assert "203.0.113.7" not in "".join(out["envelopes"])


def test_forged_event_id_timestamp_and_headers_are_replaced():
    out = _run(
        f"""
        from src.utils.error_tracking import init_error_tracking

        init_error_tracking("worker", transport=transport)
        sentry_sdk.capture_event({{
            "event_id": "{_CANARY_PATH_SECRET}",
            "timestamp": "{_CANARY_EMAIL}",
            "exception": {{"values": [{{"type": "RuntimeError", "value": "{_CANARY_SQL}"}}]}},
        }})
        sentry_sdk.flush()
        report()
        """
    )
    assert out["item_types"] == ["event"]
    envelope = out["envelopes"][0]
    _assert_clean(envelope)
    header = json.loads(envelope.splitlines()[0])
    assert set(header) <= {"event_id", "sdk", "sent_at"}  # no dsn, no trace context
    assert len(header["event_id"]) == 32
    assert "timestamp" not in json.loads(envelope.splitlines()[-1])


def test_real_fastapi_route_error_exports_the_template_only():
    out = _run(
        f"""
        from src.utils.error_tracking import init_error_tracking

        assert init_error_tracking("api", transport=transport)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/owners/{{owner_id}}")
        def owner(owner_id: str):
            raise LookupError("no owner " + owner_id)

        client = TestClient(app, raise_server_exceptions=False)
        status = client.get(
            "/owners/{_CANARY_PATH_SECRET}?email={_CANARY_EMAIL}",
            headers={{"Authorization": "Bearer {_CANARY_JWT}", "Cookie": "s={_CANARY_SIGNED}"}},
        ).status_code
        sentry_sdk.flush()
        report(status=status)
        """
    )
    assert out["status"] == 500
    assert out["item_types"] == ["event"]
    envelope = out["envelopes"][0]
    _assert_clean(envelope)
    event = json.loads(envelope.splitlines()[-1])
    assert event["transaction"] == "/owners/{owner_id}"
    assert event["tags"] == {"service": "api"}
    assert event["exception"]["values"][-1]["type"] == "LookupError"


def test_real_celery_task_error_exports_the_task_name_not_its_args():
    out = _run(
        f"""
        from src.utils.error_tracking import init_error_tracking

        assert init_error_tracking("worker", transport=transport)
        from celery import Celery

        app = Celery("p003", broker="memory://", backend="cache+memory://")

        @app.task(name="p003.enrich_owner")
        def enrich_owner(address, email=None):
            raise ConnectionError("lookup failed for " + address)

        result = enrich_owner.apply(args=["{_CANARY_SQL}"], kwargs={{"email": "{_CANARY_EMAIL}"}})
        sentry_sdk.flush()
        report(failed=result.failed())
        """
    )
    assert out["failed"] is True
    assert out["item_types"] == ["event"]
    envelope = out["envelopes"][0]
    _assert_clean(envelope)
    event = json.loads(envelope.splitlines()[-1])
    assert event["transaction"] == "p003.enrich_owner"
    assert event["exception"]["values"][-1]["type"] == "ConnectionError"


def test_trailing_newline_does_not_pass_a_pattern():
    assert _valid_dsn(_GOOD_DSN.replace("/678", "/678\n") + "x") is None
    out = _sanitize_event(_event(tags={"ref": "0123456789ab\n"}))
    assert "tags" not in out


def test_filter_rebuilds_headers_and_refuses_extra_items():
    from sentry_sdk.envelope import Envelope, Item, PayloadRef

    from src.utils.error_tracking import _filter_envelope

    hostile = {
        "event_id": "0" * 32,
        "dsn": f"https://key@{_CANARY_PATH_SECRET}.example.com/1",
        "trace": {"transaction": f"/hooks/{_CANARY_PATH_SECRET}", "user": _CANARY_EMAIL},
        "sdk": {"name": "sentry.python", "version": "2.72.0", "integrations": [_CANARY_ARG]},
    }
    clean = _filter_envelope(Envelope(headers=hostile, items=[Item(payload=PayloadRef(json=_event()), type="event")]))
    assert set(clean.headers) == {"event_id", "sdk"}
    assert clean.headers["sdk"] == {"name": "sentry.python", "version": "2.72.0"}
    _assert_clean(clean.serialize().decode())
    two = Envelope(headers=hostile, items=[
        Item(payload=PayloadRef(json=_event()), type="event"),
        Item(payload=_CANARY_SQL.encode(), type="attachment", filename="a.txt"),
    ])
    assert _filter_envelope(two) is None
    session = Envelope(items=[Item(payload=PayloadRef(json={"did": _CANARY_EMAIL}), type="session")])
    assert _filter_envelope(session) is None
