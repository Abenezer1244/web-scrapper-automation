"""M7 durable audit trail: the never-fail-the-request contract (pure).

The durable insert is best-effort by design: these lock that audit_log and
_persist_audit_event can NEVER raise into a request path — not without an
event loop, not with a broken DB, not with oversized input. (The actual row
landing in audit_events is exercised post-deploy; the log line is the
always-present fallback.)
"""
import asyncio

from starlette.requests import Request

from src.api.middleware.security import _persist_audit_event, audit_log


def _fake_request(path: str = "/auth/login") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "query_string": b"",
        "server": ("test", 80),
        "scheme": "http",
        "client": ("127.0.0.1", 1234),
    }
    return Request(scope)


def test_audit_log_without_running_loop_does_not_raise():
    # Sync/test context: no event loop -> the durable write is skipped, the
    # log line stands, nothing raises.
    audit_log(_fake_request(), "login_success", "00000000-0000-0000-0000-000000000001")
    audit_log(_fake_request(), "x" * 500, None, "detail" * 200)  # oversized input


def test_persist_swallows_db_failure():
    # Even a hard DB failure (e.g. table missing pre-migration, DB down) must
    # resolve quietly — never propagate into the caller's task group.
    asyncio.run(
        _persist_audit_event("login_success", None, "127.0.0.1", "/auth/login", "")
    )
    asyncio.run(
        _persist_audit_event("e" * 999, "not-a-uuid", "ip" * 99, "/p" * 999, "d" * 9999)
    )
