"""P0-03 5b: an unhandled API error reaches Sentry tagged with the ref the client saw.

Each scenario runs the REAL application (main.app, its middleware, its exception
handler) in a fresh interpreter, with the SDK initialised first on the in-memory
transport from test_error_tracking (main.py's own init call is then a no-op for the
same service). A route that raises is added to that app instance, since no
production route fails on demand.
"""
import json

from tests.test_error_tracking import (
    _CANARY_EMAIL,
    _CANARY_JWT,
    _CANARY_PATH_SECRET,
    _assert_clean,
    _run,
)

_APP = """
from src.utils.error_tracking import init_error_tracking

assert init_error_tracking("api", transport=transport)
import main

def explode(item_id: str):
    raise LookupError("no item " + item_id)

main.app.add_api_route("/p003/items/{item_id}", explode, methods=["GET"])
"""


def _events(out: dict) -> list[dict]:
    return [json.loads(env.splitlines()[-1]) for env in out["envelopes"]]


def test_unhandled_error_keeps_the_500_contract_and_tags_the_ref():
    out = _run(
        _APP
        + f"""
from fastapi.testclient import TestClient

client = TestClient(main.app, raise_server_exceptions=False)
res = client.get(
    "/p003/items/{_CANARY_PATH_SECRET}?email={_CANARY_EMAIL}",
    headers={{"Authorization": "Bearer {_CANARY_JWT}"}},
)
sentry_sdk.flush()
report(status=res.status_code, body=res.json())
"""
    )
    assert out["status"] == 500
    assert set(out["body"]) == {"detail", "ref"} and out["body"]["detail"] == "Internal error"
    events = _events(out)
    assert len(events) == 1  # captured once, not once per layer
    assert out["item_types"] == ["event"]
    event = events[0]
    assert event["tags"] == {"service": "api", "ref": out["body"]["ref"]}
    assert event["transaction"] == "/p003/items/{item_id}"
    assert event["exception"]["values"][-1]["type"] == "LookupError"
    _assert_clean("".join(out["envelopes"]))


def test_the_server_log_names_the_route_template_not_the_raw_path():
    # The message is constant here, so the path value can only reach the log
    # through the handler's own line (the traceback prints the message).
    out = _run(
        _APP
        + f"""
from fastapi.testclient import TestClient

def quiet(token: str):
    raise LookupError("no item")

main.app.add_api_route("/p003/quiet/{{token}}", quiet, methods=["GET"])
res = TestClient(main.app, raise_server_exceptions=False).get("/p003/quiet/{_CANARY_PATH_SECRET}")
report(ref=res.json()["ref"])
"""
    )
    line = next(ln for ln in out["stderr"].splitlines() if "Unhandled error ref=" in ln)
    assert f"ref={out['ref']}" in line and "route=/p003/quiet/{token}" in line
    assert _CANARY_PATH_SECRET not in out["stderr"]


def test_concurrent_failures_each_carry_their_own_ref():
    out = _run(
        _APP
        + """
import asyncio
import httpx

async def main_():
    transport_ = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport_, base_url="http://test") as client:
        responses = await asyncio.gather(*(client.get(f"/p003/items/n{i}") for i in range(8)))
    return [r.json()["ref"] for r in responses]

refs = asyncio.run(main_())
sentry_sdk.flush()
report(refs=refs)
"""
    )
    tagged = sorted(e["tags"]["ref"] for e in _events(out))
    assert len(set(out["refs"])) == 8
    assert tagged == sorted(out["refs"])  # one event per request, none crossed over


def test_tracking_off_leaves_the_500_unchanged():
    out = _run(
        """
import main
from fastapi.testclient import TestClient

def explode():
    raise LookupError("boom")

main.app.add_api_route("/p003/boom", explode, methods=["GET"])
res = TestClient(main.app, raise_server_exceptions=False).get("/p003/boom")
report(status=res.status_code, body=res.json(), active=sentry_sdk.get_client().is_active())
""",
        dsn=None,
    )
    assert out["status"] == 500 and out["active"] is False and out["envelopes"] == []
    assert set(out["body"]) == {"detail", "ref"}


def test_a_requests_ref_does_not_leak_into_later_events():
    out = _run(
        _APP
        + """
from fastapi.testclient import TestClient

ref = TestClient(main.app, raise_server_exceptions=False).get("/p003/items/x").json()["ref"]
try:
    raise RuntimeError("outside any request")
except RuntimeError:
    sentry_sdk.capture_exception()
sentry_sdk.flush()
report(ref=ref)
"""
    )
    first, later = _events(out)
    assert first["tags"]["ref"] == out["ref"]
    assert "ref" not in later.get("tags", {})
