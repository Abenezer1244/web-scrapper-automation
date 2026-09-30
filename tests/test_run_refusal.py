"""A machine-readable code on the run-refusal 402s (UX audit Q6 / F-035, 2b-ii Phase B).

The AI-limit and account-rule refusals on ``POST /jobs`` and ``POST /batches``
keep ``detail`` as the same sentence, byte for byte, and gain two top-level
keys beside it: ``code`` (the evaluator's code) and ``resumes_at``. Additive on
purpose: a client that reads ``detail`` as a string keeps working. Every row is
real, in the test database.
"""

from __future__ import annotations

import json
import typing
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from src.config import settings
from tests.test_config_eligibility import _auth, _config, _county, _job, _user

RUN_KEYS = {"detail", "code", "resumes_at"}
ALLOWED_ORIGIN = "https://bridgeleads.io"
SECURITY_HEADERS = {
    "x-content-type-options", "x-frame-options", "referrer-policy",
    "permissions-policy", "content-security-policy",
    "cross-origin-opener-policy", "cross-origin-resource-policy",
}


async def _post_job(client: AsyncClient, user, config, headers=None):
    return await client.post(
        "/jobs", json={"scraper_config_id": config.id, "trigger": "manual"},
        headers={**_auth(user), **(headers or {})},
    )


async def _post_batch(client: AsyncClient, user):
    # Past the plan and size gates, the account rule is the first refusal, and it
    # runs before any connector is looked up.
    return await client.post(
        "/batches",
        json={"state": "WA", "counties": ["king"], "record_types": ["probate"]},
        headers=_auth(user),
    )


async def _page_eligibility(client: AsyncClient, user, config) -> dict:
    r = await client.get(f"/scrapers/{config.id}", headers=_auth(user))
    assert r.status_code == 200, r.text
    return r.json()["run_eligibility"]


def _raw(response) -> dict:
    """The body as emitted: json.loads keeps datetimes as the exact strings sent."""
    return json.loads(response.text)


def _window(**kw):
    start = datetime.now(UTC) - timedelta(days=10)
    return {
        "quota_anchor_at": start, "quota_period_start": start,
        "quota_period_end": start + timedelta(days=30), **kw,
    }


# ─── POST /jobs ───────────────────────────────────────────────────────────────

async def _assert_run_refusal(client, user, config, code):
    """402 with today's sentence in detail, plus the code and the exact
    resumes_at string GET /scrapers reports for the same scraper."""
    page = await _page_eligibility(client, user, config)
    r = await _post_job(client, user, config)
    body = _raw(r)
    assert r.status_code == 402, r.text
    assert set(body) == RUN_KEYS
    assert body["code"] == code == page["code"]
    assert body["detail"] == page["message"]
    assert body["resumes_at"] == page["resumes_at"]
    assert r.headers["content-type"].startswith("application/json")
    return body


@pytest.mark.parametrize(
    ("code", "fields"),
    [
        ("frozen", {"subscription_status": "unpaid"}),
        ("ended", {"entitlement_ends_at": datetime.now(UTC) - timedelta(hours=1)}),
        ("over_limit", {"records_used": 5000}),
    ],
)
async def test_account_402_names_its_code(db, connectors, client: AsyncClient, code, fields):
    county = _county()
    await connectors(county, ["probate"], "manual")
    user = await _user(db, **fields)
    config = await _config(db, user, county)
    body = await _assert_run_refusal(client, user, config, code)
    if code == "over_limit":
        assert body["resumes_at"] is not None
    else:
        assert body["resumes_at"] is None


async def test_over_limit_402_on_a_cancelled_term_has_no_resume(db, connectors, client: AsyncClient):
    county = _county()
    await connectors(county, ["probate"], "manual")
    window = _window()
    user = await _user(
        db, records_used=5000, entitlement_ends_at=window["quota_period_end"], **window,
    )
    config = await _config(db, user, county)
    body = await _assert_run_refusal(client, user, config, "over_limit")
    assert body["resumes_at"] is None


async def test_other_job_refusals_keep_their_shape(db, connectors, client: AsyncClient, monkeypatch):
    """The 409 and the entitlement 402 are already structured in ``detail`` and
    gain nothing at the top level."""
    monkeypatch.setattr(settings, "ENTITLEMENT_ENFORCEMENT", True)
    county = _county()
    await connectors(county, ["probate", "tax_delinquent"], "manual")

    busy = await _user(db)
    busy_config = await _config(db, busy, county)
    running = await _job(db, busy, busy_config, status="scraping", started_at=datetime.now(UTC))
    r = await _post_job(client, busy, busy_config)
    assert r.status_code == 409
    assert _raw(r) == {"detail": {
        "code": "run_in_flight", "job_id": running.id,
        "message": "This scraper is already running.",
    }}

    starter = await _user(db, plan="starter", records_limit=50)
    r = await _post_job(client, starter, await _config(db, starter, county, "tax_delinquent"))
    assert r.status_code == 402
    assert set(_raw(r)) == {"detail"}
    assert set(_raw(r)["detail"]) == {"code", "title", "message"}


# ─── POST /batches ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("code", "fields"),
    [
        ("frozen", {"subscription_status": "unpaid"}),
        ("ended", {"entitlement_ends_at": datetime.now(UTC) - timedelta(hours=1)}),
        ("over_limit", {"records_used": 5000}),
    ],
)
async def test_batch_402_names_its_code(db, client: AsyncClient, code, fields):
    user = await _user(db, plan="business", **fields)
    usage = (await client.get("/billing/usage", headers=_auth(user))).json()["run_eligibility"]
    r = await _post_batch(client, user)
    body = _raw(r)
    assert r.status_code == 402, r.text
    assert set(body) == RUN_KEYS
    assert (body["code"], body["detail"], body["resumes_at"]) == (
        code, usage["message"], usage["resumes_at"],
    )


async def test_batch_402_on_a_cancelled_term_has_no_resume(db, client: AsyncClient):
    window = _window()
    user = await _user(
        db, plan="business", records_used=5000,
        entitlement_ends_at=window["quota_period_end"], **window,
    )
    body = _raw(await _post_batch(client, user))
    assert (body["code"], body["resumes_at"]) == ("over_limit", None)


async def test_the_batch_plan_gate_is_still_a_bare_sentence(db, client: AsyncClient):
    user = await _user(db, plan="starter", records_limit=50)
    r = await _post_batch(client, user)
    assert (r.status_code, _raw(r)) == (
        402, {"detail": "Batch scrape requires a Pro plan or higher."},
    )


# ─── The handler ──────────────────────────────────────────────────────────────

async def test_a_run_refusal_keeps_cors_and_security_headers(db, connectors, client: AsyncClient):
    county = _county()
    await connectors(county, ["probate"], "manual")
    user = await _user(db, subscription_status="unpaid")
    config = await _config(db, user, county)
    origin = {"Origin": ALLOWED_ORIGIN}
    ok = await client.get("/scrapers", headers={**_auth(user), **origin})
    r = await _post_job(client, user, config, headers=origin)
    assert (ok.status_code, r.status_code) == (200, 402)
    assert r.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "origin" in r.headers.get("vary", "").lower()
    assert "retry-after" in r.headers["access-control-expose-headers"].lower()
    present = {h for h in SECURITY_HEADERS if h in ok.headers}
    assert present, "the security middleware sets its headers on a 200"
    assert present <= set(r.headers)


async def test_the_handler_keeps_headers_and_the_body_shape():
    from starlette.requests import Request

    from src.api.errors import RunRefusedHTTPException, run_refused_handler

    exc = RunRefusedHTTPException(
        code="over_limit", message="Record limit reached (5/5).",
        resumes_at=datetime(2026, 10, 1, tzinfo=UTC), headers={"Retry-After": "60"},
    )
    request = Request({"type": "http", "method": "POST", "path": "/jobs", "headers": []})
    response = await run_refused_handler(request, exc)
    assert response.status_code == 402
    assert response.headers["retry-after"] == "60"
    assert json.loads(response.body) == {
        "detail": "Record limit reached (5/5).", "code": "over_limit",
        "resumes_at": "2026-10-01T00:00:00Z",
    }


def test_an_unexpected_code_stays_a_bare_sentence():
    from fastapi import HTTPException

    from src.api.errors import RunRefusedHTTPException, run_refusal_http

    exc = run_refusal_http("config_inactive", "This scraper has been deleted.", None)
    assert type(exc) is HTTPException
    assert not isinstance(exc, RunRefusedHTTPException)
    assert (exc.status_code, exc.detail) == (402, "This scraper has been deleted.")
    known = run_refusal_http("frozen", "paused", None)
    assert isinstance(known, RunRefusedHTTPException)
    assert (known.code, known.detail, known.resumes_at) == ("frozen", "paused", None)


def test_the_code_list_and_the_schema_agree():
    from src.api.errors import RUN_REFUSAL_CODES
    from src.api.schemas import RunRefusalResponse

    literal = RunRefusalResponse.model_fields["code"].annotation
    assert tuple(typing.get_args(literal)) == RUN_REFUSAL_CODES


@pytest.mark.parametrize("path", ["/jobs", "/batches"])
def test_openapi_declares_every_402_shape(path):
    from main import app

    schema = app.openapi()
    content = schema["paths"][path]["post"]["responses"]["402"]["content"]["application/json"]
    refs = {s["$ref"].rsplit("/", 1)[-1] for s in content["schema"]["anyOf"]}
    assert refs == {"RunRefusalResponse", "EntitlementRefusalResponse", "PlainRefusalResponse"}
    components = schema["components"]["schemas"]
    assert set(components["RunRefusalResponse"]["required"]) == RUN_KEYS
    assert components["EntitlementRefusalResponse"]["required"] == ["detail"]
    assert components["PlainRefusalResponse"]["required"] == ["detail"]
    assert set(components["EntitlementRefusalDetail"]["required"]) == {"code", "title", "message"}
