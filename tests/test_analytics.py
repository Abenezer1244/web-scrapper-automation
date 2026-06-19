"""Integration tests for GET /analytics/summary (Phase 3).

DB-backed — CI-arbitrated (prod-only DB). Do NOT run locally.
Uses real conftest fixtures: `client` (httpx AsyncClient, no auth),
`db` (AsyncSession), `starter_user` + `starter_token`, `business_user` +
`business_token`.

Seeding uses system_sync_session (the worker pathway) so the read path is
exercised from write to API exactly as in production.
"""
import uuid

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session

# ─── Auth helper ──────────────────────────────────────────────────────────────

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─── Seeding helpers ──────────────────────────────────────────────────────────

def _seed_result(
    user_id: str,
    *,
    county: str = "king",
    state: str = "WA",
    record_type: str = "probate",
    is_duplicate: bool = False,
    skip_trace_status: str = "not_attempted",
    phone: str | None = None,
    email: str | None = None,
) -> str:
    """Insert the full FK chain (scraper_config → job → result) for one result.

    Returns the result id. Uses system_sync_session so the worker write-path is
    exercised end-to-end.
    """
    sc_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    result_id = str(uuid.uuid4())
    with system_sync_session() as db:
        db.execute(
            text("""
                INSERT INTO scraper_configs
                    (id, user_id, name, county, state, record_type,
                     fields, enrichment, schedule, deliver,
                     skip_trace_enabled, active)
                VALUES
                    (:sc_id, :user_id, :name, :county, :state, :record_type,
                     '[]'::json, '[]'::json,
                     '{"frequency":"manual"}'::json,
                     '{"format":"csv","emails":[]}'::json,
                     false, true)
            """),
            {
                "sc_id": sc_id,
                "user_id": user_id,
                "name": f"Test {county} {record_type}",
                "county": county,
                "state": state,
                "record_type": record_type,
            },
        )
        db.execute(
            text("""
                INSERT INTO jobs
                    (id, user_id, scraper_config_id, status, trigger,
                     page_current, page_total, record_count, retry_count)
                VALUES
                    (:job_id, :user_id, :sc_id, 'done', 'manual',
                     0, 0, 0, 0)
            """),
            {"job_id": job_id, "user_id": user_id, "sc_id": sc_id},
        )
        db.execute(
            text("""
                INSERT INTO results
                    (id, job_id, user_id, is_duplicate,
                     skip_trace_status, phone, email, created_at)
                VALUES
                    (:result_id, :job_id, :user_id, :is_duplicate,
                     :skip_trace_status, :phone, :email, now())
            """),
            {
                "result_id": result_id,
                "job_id": job_id,
                "user_id": user_id,
                "is_duplicate": is_duplicate,
                "skip_trace_status": skip_trace_status,
                "phone": phone,
                "email": email,
            },
        )
        db.commit()
    return result_id


# ─── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_requires_auth(client):
    """No bearer token → 401 or 403."""
    r = await client.get("/analytics/summary")
    assert r.status_code in (401, 403)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_rejects_bad_window(client, starter_user, starter_token):
    """window=7 is not in Literal[30,90] → 422 Unprocessable Entity."""
    r = await client.get("/analytics/summary?window=7", headers=_auth(starter_token))
    assert r.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_empty_account_all_zeros(client, starter_user, starter_token):
    """Account with no results → 30-point zero-filled trend + empty lists + zero skip_trace."""
    r = await client.get("/analytics/summary?window=30", headers=_auth(starter_token))
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 30
    assert body["timezone"]  # non-empty string
    assert len(body["trend"]) == 30  # dense, zero-filled, incl today
    assert all(p["leads"] == 0 for p in body["trend"])
    assert body["by_record_type"] == []
    assert body["by_county"] == []
    assert body["skip_trace"] == {
        "total": 0,
        "enriched": 0,
        "phone_pct": 0,
        "email_pct": 0,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_90_day_window(client, starter_user, starter_token):
    """window=90 returns a 90-point trend."""
    r = await client.get("/analytics/summary?window=90", headers=_auth(starter_token))
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 90
    assert len(body["trend"]) == 90


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_tenant_isolation(
    client, starter_user, starter_token, business_user
):
    """Results seeded under business_user must never appear for starter_user."""
    # Seed one result for the OTHER tenant (business_user).
    _seed_result(business_user.id, county="pierce", state="WA", record_type="probate")

    r = await client.get("/analytics/summary?window=30", headers=_auth(starter_token))
    body = r.json()
    # starter sees zero leads — the other tenant's row is completely hidden.
    assert sum(p["leads"] for p in body["trend"]) == 0
    assert body["skip_trace"]["total"] == 0
    assert body["by_record_type"] == []
    assert body["by_county"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_counts_own_leads(
    client, starter_user, starter_token
):
    """3 non-duplicate King WA probate results + 1 duplicate → only 3 counted."""
    for _ in range(3):
        _seed_result(
            starter_user.id,
            county="king",
            state="WA",
            record_type="probate",
            is_duplicate=False,
        )
    # This duplicate must be excluded from all counts.
    _seed_result(
        starter_user.id,
        county="king",
        state="WA",
        record_type="probate",
        is_duplicate=True,
    )

    r = await client.get("/analytics/summary?window=30", headers=_auth(starter_token))
    assert r.status_code == 200
    body = r.json()

    # Trend total = 3 (duplicate excluded).
    assert sum(p["leads"] for p in body["trend"]) == 3

    # by_record_type: probate → 3
    rt = {x["record_type"]: x["leads"] for x in body["by_record_type"]}
    assert rt.get("probate") == 3

    # by_county: (king, WA) → 3
    counties = {(c["county"], c["state"]): c["leads"] for c in body["by_county"]}
    assert counties.get(("king", "WA")) == 3

    # skip_trace total = 3 (all not_attempted; no phone/email)
    st = body["skip_trace"]
    assert st["total"] == 3
    assert st["enriched"] == 0
    assert st["phone_pct"] == 0
    assert st["email_pct"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_state_collision_king_wa_vs_king_tx(
    client, starter_user, starter_token
):
    """King WA and King TX must appear as separate county+state buckets."""
    _seed_result(
        starter_user.id, county="king", state="WA", record_type="probate"
    )
    _seed_result(
        starter_user.id, county="king", state="TX", record_type="probate"
    )

    r = await client.get("/analytics/summary?window=30", headers=_auth(starter_token))
    assert r.status_code == 200
    body = r.json()

    counties = {(c["county"], c["state"]): c["leads"] for c in body["by_county"]}
    assert counties.get(("king", "WA")) == 1
    assert counties.get(("king", "TX")) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_skip_trace_hit_counts(client, starter_user, starter_token):
    """enriched counts 'hit' (not the never-written 'done'); pct = contact presence."""
    _seed_result(starter_user.id, skip_trace_status="hit", phone="x", email="x")
    _seed_result(starter_user.id, skip_trace_status="miss")  # traced, no contact
    r = await client.get("/analytics/summary?window=30", headers=_auth(starter_token))
    st = r.json()["skip_trace"]
    assert st["total"] == 2
    assert st["enriched"] == 1   # only the hit, NOT the miss
    assert st["phone_pct"] == 50
    assert st["email_pct"] == 50


def test_analytics_route_registered_in_openapi():
    """Smoke test: /analytics/summary appears in the OpenAPI spec."""
    from main import app

    paths = app.openapi()["paths"]
    assert "/analytics/summary" in paths
