"""Per-scraper run eligibility (UX audit Q6 / F-035, item 2b-ii Phase A).

``config_run_eligibility`` answers "may this scraper start a run, and if not,
why" for a whole list at once. ``POST /jobs`` makes its decision through it and
``GET /scrapers`` reports it, so the Run now button cannot disagree with the
gate. Every row here is real, in the test database; connectors are created
under throwaway county names through the conftest ``connectors`` fixture, which
removes them afterwards (the shared ``db`` teardown does not).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import event

import src.db.session as _db_session
from src.api.auth import create_secure_token, hash_password
from src.config import settings
from src.db.models import Job, ScraperConfig, User

FROZEN_MSG = (
    "Your subscription payment could not be completed, so new scrapes are "
    "paused. Update your payment method to resume. Your data and past exports "
    "are untouched."
)


def _now() -> datetime:
    return datetime.now(UTC)


def _next_month_start(now: datetime) -> datetime:
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(
        month=first.month + 1
    )


def _county() -> str:
    return f"elig{uuid.uuid4().hex[:8]}"


async def _user(db, **kw) -> User:
    start = _now() - timedelta(days=10)
    fields = {
        "id": str(uuid.uuid4()),
        "email": f"test_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        "password_hash": hash_password("TestPass123!"),
        "plan": "business",
        "records_limit": 5000,
        "records_used": 0,
        "subscription_status": "active",
        "quota_anchor_at": start,
        "quota_period_start": start,
        "quota_period_end": start + timedelta(days=30),
        "records_period_start": start,
    }
    fields.update(kw)
    user = User(**fields)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _config(db, user, county, record_type="probate", **kw) -> ScraperConfig:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"Elig {county} {record_type}",
        county=county, state="WA", record_type=record_type,
        fields=["party_name", "parcel_id"], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
        **kw,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


async def _job(db, user, config, status="done", **kw) -> Job:
    job = Job(
        id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=config.id,
        status=status, trigger="manual", **kw,
    )
    db.add(job)
    await db.commit()
    return job


async def _eligibility(db, user, configs, now=None):
    from src.api.config_eligibility import config_run_eligibility

    return await config_run_eligibility(db, user, configs, now or _now())


@pytest.fixture
def enforce(monkeypatch):
    """Production runs with ENTITLEMENT_ENFORCEMENT=true (read 2026-09-27)."""
    monkeypatch.setattr(settings, "ENTITLEMENT_ENFORCEMENT", True)


# ─── The codes ────────────────────────────────────────────────────────────────

async def test_an_ordinary_scraper_can_run(db, connectors):
    county = _county()
    await connectors(county, ["probate"], "manual")
    user = await _user(db)
    config = await _config(db, user, county)
    e = (await _eligibility(db, user, [config]))[config.id]
    assert (e.can_run, e.code, e.message, e.resumes_at, e.job_id, e.violation_code) == (
        True, None, None, None, None, None,
    )


async def test_a_running_scraper_names_its_job(db, connectors):
    county = _county()
    await connectors(county, ["probate"], "manual")
    user = await _user(db)
    config = await _config(db, user, county)
    job = await _job(db, user, config, status="scraping", started_at=_now())
    e = (await _eligibility(db, user, [config]))[config.id]
    assert (e.can_run, e.code, e.job_id) == (False, "run_in_flight", job.id)
    assert e.message == "This scraper is already running."


async def test_the_newest_slot_holder_wins_and_a_cancelled_one_is_still_stopping(db, connectors):
    county = _county()
    await connectors(county, ["probate"], "manual")
    user = await _user(db)
    config = await _config(db, user, county)
    now = _now()
    # Both workers claimed their run (the claim stamps last_heartbeat_at) and
    # neither has acknowledged its exit yet, so both still hold the slot.
    await _job(
        db, user, config, status="cancelled", started_at=now - timedelta(minutes=3),
        last_heartbeat_at=now - timedelta(minutes=3),
        finished_at=now - timedelta(minutes=2), created_at=now - timedelta(minutes=4),
    )
    newest = await _job(
        db, user, config, status="cancelled", started_at=now - timedelta(minutes=1),
        last_heartbeat_at=now - timedelta(minutes=1),
        finished_at=now - timedelta(seconds=30), created_at=now - timedelta(minutes=1),
    )
    e = (await _eligibility(db, user, [config], now))[config.id]
    assert (e.code, e.job_id) == ("run_in_flight", newest.id)
    assert e.message == "This scraper is still stopping. Try again in a few minutes."


async def test_a_job_whose_owner_does_not_match_its_config_is_nobodys_run(db, connectors):
    """The database permits a job whose user_id and config belong to different
    users. Neither account's page may treat it as its own run."""
    county = _county()
    await connectors(county, ["probate"], "manual")
    owner = await _user(db)
    other = await _user(db)
    config = await _config(db, owner, county)
    await _job(db, other, config, status="scraping", started_at=_now())
    assert (await _eligibility(db, owner, [config]))[config.id].can_run is True


@pytest.mark.usefixtures("enforce")
async def test_a_record_type_outside_the_plan_is_not_entitled(db, connectors):
    county = _county()
    await connectors(county, ["tax_delinquent"], "manual")
    user = await _user(db, plan="starter", records_limit=50)
    config = await _config(db, user, county, record_type="tax_delinquent")
    e = (await _eligibility(db, user, [config]))[config.id]
    assert (e.can_run, e.code, e.violation_code) == (False, "not_entitled", "record_type")
    assert e.message.endswith("Upgrade your plan to continue.")


@pytest.mark.usefixtures("enforce")
async def test_a_county_outside_the_plans_slots_is_not_entitled(db, connectors):
    first, second = _county(), _county()
    await connectors(first, ["probate"], "manual")
    await connectors(second, ["probate"], "manual")
    user = await _user(db, plan="starter", records_limit=50)
    kept = await _config(db, user, first)
    extra = await _config(db, user, second)
    result = await _eligibility(db, user, [kept, extra])
    assert result[kept.id].can_run is True
    assert (result[extra.id].code, result[extra.id].violation_code) == (
        "not_entitled", "county_limit",
    )


async def test_not_entitled_does_not_block_while_enforcement_is_off(db, connectors, monkeypatch):
    monkeypatch.setattr(settings, "ENTITLEMENT_ENFORCEMENT", False)
    county = _county()
    await connectors(county, ["tax_delinquent"], "manual")
    user = await _user(db, plan="starter", records_limit=50)
    config = await _config(db, user, county, record_type="tax_delinquent")
    assert (await _eligibility(db, user, [config]))[config.id].can_run is True


async def test_the_ai_monthly_limit_resumes_next_utc_month(db, connectors):
    county = _county()
    await connectors(county, ["probate"], "ai")
    user = await _user(db, plan="starter", records_limit=50)
    config = await _config(db, user, county)
    for _ in range(settings.AI_JOB_LIMITS["starter"]):
        await _job(db, user, config)
    now = _now()
    e = (await _eligibility(db, user, [config], now))[config.id]
    assert (e.can_run, e.code, e.resumes_at) == (False, "ai_limit", _next_month_start(now))
    assert e.message == (
        "Monthly AI scrape limit reached (5/5). "
        "Upgrade your plan for more AI-powered scrapes."
    )


async def test_the_account_rule_applies_to_every_scraper(db, connectors):
    county = _county()
    await connectors(county, ["probate"], "manual")
    user = await _user(db, subscription_status="unpaid")
    config = await _config(db, user, county)
    e = (await _eligibility(db, user, [config]))[config.id]
    assert (e.code, e.message, e.resumes_at) == ("frozen", FROZEN_MSG, None)


async def test_over_limit_on_a_cancelled_term_has_no_resume_date(db, connectors):
    county = _county()
    await connectors(county, ["probate"], "manual")
    start = _now() - timedelta(days=10)
    end = start + timedelta(days=30)
    user = await _user(
        db, records_used=5000, entitlement_ends_at=end,
        quota_anchor_at=start, quota_period_start=start, quota_period_end=end,
    )
    config = await _config(db, user, county)
    e = (await _eligibility(db, user, [config]))[config.id]
    assert (e.code, e.resumes_at) == ("over_limit", None)


async def test_an_entitlement_paused_scraper_is_config_inactive(db, connectors):
    county = _county()
    await connectors(county, ["probate"], "manual")
    user = await _user(db)
    config = await _config(db, user, county, active=False, paused_reason="entitlement")
    e = (await _eligibility(db, user, [config]))[config.id]
    assert (e.can_run, e.code) == (False, "config_inactive")
    assert "paused" in e.message


# ─── Precedence: the reason shown is the refusal POST /jobs gives first ───────

@pytest.mark.usefixtures("enforce")
async def test_precedence_follows_the_gate(db, connectors):
    county = _county()
    await connectors(county, ["tax_delinquent"], "ai")
    # frozen AND over its AI limit AND not entitled AND running.
    user = await _user(db, plan="starter", records_limit=50, subscription_status="unpaid")
    config = await _config(db, user, county, record_type="tax_delinquent")
    for _ in range(settings.AI_JOB_LIMITS["starter"]):
        await _job(db, user, config)
    running = await _job(db, user, config, status="scraping", started_at=_now())
    assert (await _eligibility(db, user, [config]))[config.id].code == "run_in_flight"

    running.status = "done"
    await db.commit()
    assert (await _eligibility(db, user, [config]))[config.id].code == "not_entitled"

    # Pro allows tax_delinquent; exhaust Pro's AI limit too. Still frozen.
    user.plan = "pro"
    await db.commit()
    for _ in range(settings.AI_JOB_LIMITS["pro"] - settings.AI_JOB_LIMITS["starter"] - 1):
        await _job(db, user, config)
    # starter's 5 + the finished "running" job + the rest = exactly Pro's 50.
    assert (await _eligibility(db, user, [config]))[config.id].code == "ai_limit"

    # Room for one more AI run: only the account rule is left.
    user.plan = "business"
    await db.commit()
    assert (await _eligibility(db, user, [config]))[config.id].code == "frozen"


# ─── AI identity: the connector the worker would run ──────────────────────────

async def test_a_mixed_county_classifies_each_record_type(db, connectors):
    county = _county()
    await connectors(county, ["probate"], "manual")
    await connectors(county, ["tax_delinquent"], "ai")
    user = await _user(db, plan="starter", records_limit=50)
    probate = await _config(db, user, county)
    for _ in range(settings.AI_JOB_LIMITS["starter"]):
        await _job(db, user, probate)
    # Five jobs, all on the MANUAL probate connector: no AI usage at all.
    assert (await _eligibility(db, user, [probate]))[probate.id].can_run is True


async def test_manual_jobs_do_not_count_against_the_ai_limit(db, connectors, client: AsyncClient):
    """The old count joined on (state, county, mode='ai') only, so manual jobs
    in a county that also has an AI connector counted as AI. The AI connector
    is inserted FIRST, which is also the row an unordered ``.first()`` returns."""
    county = _county()
    await connectors(county, ["tax_delinquent"], "ai")
    await connectors(county, ["probate"], "manual")
    user = await _user(db, plan="pro", records_limit=1000)
    probate = await _config(db, user, county)
    tax = await _config(db, user, county, record_type="tax_delinquent")
    for _ in range(settings.AI_JOB_LIMITS["pro"]):
        await _job(db, user, probate)
    r = await client.post(
        "/jobs", json={"scraper_config_id": tax.id, "trigger": "manual"},
        headers={"Authorization": f"Bearer {create_secure_token(user.id)}"},
    )
    # Needs nothing new from this change: it proves the OLD gate's bug directly.
    assert r.status_code == 201, r.text


async def test_the_evaluator_agrees_manual_jobs_are_not_ai_usage(db, connectors):
    county = _county()
    await connectors(county, ["tax_delinquent"], "ai")
    await connectors(county, ["probate"], "manual")
    user = await _user(db, plan="pro", records_limit=1000)
    probate = await _config(db, user, county)
    tax = await _config(db, user, county, record_type="tax_delinquent")
    for _ in range(settings.AI_JOB_LIMITS["pro"]):
        await _job(db, user, probate)
    assert (await _eligibility(db, user, [tax]))[tax.id].can_run is True


async def test_ai_jobs_in_a_county_the_page_does_not_show_still_count(db, connectors):
    shown, elsewhere = _county(), _county()
    await connectors(shown, ["probate"], "ai")
    await connectors(elsewhere, ["probate"], "ai")
    user = await _user(db, plan="pro", records_limit=1000)
    visible = await _config(db, user, shown)
    other = await _config(db, user, elsewhere)
    for _ in range(settings.AI_JOB_LIMITS["pro"]):
        await _job(db, user, other)
    assert (await _eligibility(db, user, [visible]))[visible.id].code == "ai_limit"


async def test_another_accounts_jobs_never_spend_this_accounts_ai_limit(db, connectors):
    """The AI count is scoped by Job.user_id AND the config's owner. Only a job
    whose owner does not match its config tells the two apart: it must count for
    nobody. Another account's ordinary jobs never count either."""
    county = _county()
    await connectors(county, ["probate"], "ai")
    user = await _user(db, plan="starter", records_limit=50)
    other = await _user(db, plan="starter", records_limit=50)
    config = await _config(db, user, county)
    other_config = await _config(db, other, county)
    for _ in range(settings.AI_JOB_LIMITS["starter"] - 1):
        await _job(db, user, config)
    await _job(db, other, config)          # mismatched owner: other's job, user's config
    await _job(db, other, other_config)    # other's own AI run
    assert (await _eligibility(db, user, [config]))[config.id].can_run is True


async def test_the_worker_runs_the_connector_eligibility_judged(db, connectors):
    """The registry (worker) and the evaluator share pick_connector: with two
    active connectors for one record type, the worker resolves the oldest."""
    from src.scrapers.base_scraper import BridgeScraper
    from src.scrapers.registry import UnsupportedCountyError, get_scraper_class

    old = datetime(2026, 1, 1, tzinfo=UTC)
    # The two connectors are told apart by what the worker does with them: the
    # manual one resolves to its class; the ai one would look for a recorder
    # template matching its example.gov base_url, find none, and raise.
    manual_older = _county()
    await connectors(manual_older, ["probate"], "ai", created_at=old + timedelta(days=1))
    await connectors(manual_older, ["probate"], "manual", created_at=old)
    factory, record_type = get_scraper_class(manual_older, "WA", "probate")
    assert (factory, record_type) == (BridgeScraper, "probate")

    ai_older = _county()
    await connectors(ai_older, ["probate"], "manual", created_at=old + timedelta(days=1))
    await connectors(ai_older, ["probate"], "ai", created_at=old)
    with pytest.raises(UnsupportedCountyError, match="template"):
        get_scraper_class(ai_older, "WA", "probate")


async def test_duplicate_connectors_resolve_to_the_oldest(db, connectors):
    from src.scrapers.registry import pick_connector

    county = _county()
    old = datetime(2026, 1, 1, tzinfo=UTC)
    manual = await connectors(county, ["probate"], "manual", created_at=old)
    ai = await connectors(county, ["probate"], "ai", created_at=old + timedelta(days=1))
    assert pick_connector([ai, manual], "PROBATE") is manual
    assert pick_connector([manual, ai], "probate") is manual
    assert pick_connector([manual, ai], "divorce") is None

    user = await _user(db, plan="starter", records_limit=50)
    config = await _config(db, user, county)
    for _ in range(settings.AI_JOB_LIMITS["starter"]):
        await _job(db, user, config)
    # The worker runs the older MANUAL connector, so none of this is AI usage.
    assert (await _eligibility(db, user, [config]))[config.id].can_run is True


# ─── Batched: a fixed number of queries, and all-tenant slot math ─────────────

async def test_the_query_count_does_not_grow_with_the_list(db, connectors):
    """Worst case: AI scrapers in several counties, plus AI history in a county
    only that history reaches (forcing the second connector load)."""
    counties = [_county() for _ in range(3)]
    history_only = _county()
    for county in [*counties, history_only]:
        await connectors(county, ["probate"], "ai")
    user = await _user(db)
    configs = [await _config(db, user, counties[i % 3]) for i in range(6)]
    await _job(db, user, await _config(db, user, history_only))
    statements: list[str] = []

    def _count(conn, cursor, statement, *args):
        statements.append(statement)

    engine = _db_session.async_engine.sync_engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        await _eligibility(db, user, configs[:1])
        one = len(statements)
        statements.clear()
        await _eligibility(db, user, configs)
        six = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert six == one
    assert six <= 6, statements


@pytest.mark.usefixtures("enforce")
async def test_slot_math_uses_every_active_scraper_not_only_the_listed_ones(db, connectors):
    first, second = _county(), _county()
    await connectors(first, ["probate"], "manual")
    await connectors(second, ["probate"], "manual")
    user = await _user(db, plan="starter", records_limit=50)
    await _config(db, user, first)
    extra = await _config(db, user, second)
    # Only the second scraper is asked about; the first still owns the one slot.
    assert (await _eligibility(db, user, [extra]))[extra.id].code == "not_entitled"


# ─── The response model refuses impossible states ─────────────────────────────

@pytest.mark.parametrize(
    "payload",
    [
        {"can_run": True, "code": "run_in_flight"},
        {"can_run": True, "message": "x"},
        {"can_run": False, "message": "x"},
        {"can_run": False, "code": "frozen"},
        {"can_run": False, "code": "frozen", "message": "x", "resumes_at": datetime.now(UTC)},
        {"can_run": False, "code": "ended", "message": "x", "job_id": "j"},
        {"can_run": False, "code": "not_entitled", "message": "x"},
        {"can_run": False, "code": "frozen", "message": "x", "violation_code": "record_type"},
        {"can_run": False, "code": "bogus", "message": "x"},
    ],
)
def test_config_run_eligibility_response_rejects_impossible_states(payload):
    from src.api.schemas import ConfigRunEligibilityResponse

    with pytest.raises(ValidationError):
        ConfigRunEligibilityResponse(**payload)


# ─── Parity: POST /jobs refuses exactly as the evaluator predicted ────────────

async def _post(client: AsyncClient, user: User, config: ScraperConfig):
    return await client.post(
        "/jobs", json={"scraper_config_id": config.id, "trigger": "manual"},
        headers={"Authorization": f"Bearer {create_secure_token(user.id)}"},
    )


async def _assert_parity(db, client, user, config):
    e = (await _eligibility(db, user, [config]))[config.id]
    r = await _post(client, user, config)
    if e.can_run:
        assert r.status_code == 201, r.text
        return
    body = r.json()["detail"]
    if e.code == "run_in_flight":
        assert r.status_code == 409
        assert body == {"code": "run_in_flight", "job_id": e.job_id, "message": e.message}
    elif e.code == "not_entitled":
        from src.api.entitlements import plan_limit_http

        assert r.status_code == 402
        # The WHOLE body, title included, not just the fields the page shows.
        assert body == plan_limit_http(e.violation).detail
        assert (body["code"], body["message"]) == (e.violation_code, e.message)
    else:
        assert (r.status_code, body) == (402, e.message)


@pytest.mark.usefixtures("enforce")
async def test_post_jobs_parity_for_every_code(db, connectors, client: AsyncClient):
    manual, ai = _county(), _county()
    await connectors(manual, ["probate", "tax_delinquent"], "manual")
    await connectors(ai, ["probate"], "ai")

    ok = await _user(db)
    await _assert_parity(db, client, ok, await _config(db, ok, manual))

    busy = await _user(db)
    busy_config = await _config(db, busy, manual)
    await _job(db, busy, busy_config, status="scraping", started_at=_now())
    await _assert_parity(db, client, busy, busy_config)

    starter = await _user(db, plan="starter", records_limit=50)
    await _assert_parity(db, client, starter, await _config(db, starter, manual, "tax_delinquent"))

    ai_user = await _user(db, plan="starter", records_limit=50)
    ai_config = await _config(db, ai_user, ai)
    for _ in range(settings.AI_JOB_LIMITS["starter"]):
        await _job(db, ai_user, ai_config)
    await _assert_parity(db, client, ai_user, ai_config)

    frozen = await _user(db, subscription_status="unpaid")
    await _assert_parity(db, client, frozen, await _config(db, frozen, manual))

    over = await _user(db, records_used=5000)
    await _assert_parity(db, client, over, await _config(db, over, manual))


# ─── The HTTP contract on the scraper routes ──────────────────────────────────

def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_secure_token(user.id)}"}


async def test_list_and_get_carry_run_eligibility(db, connectors, client: AsyncClient):
    county = _county()
    await connectors(county, ["probate"], "manual")
    user = await _user(db)
    idle = await _config(db, user, county)
    busy = await _config(db, user, county)
    job = await _job(db, user, busy, status="scraping", started_at=_now())

    r = await client.get("/scrapers", headers=_auth(user))
    assert r.status_code == 200, r.text
    by_id = {s["id"]: s["run_eligibility"] for s in r.json()}
    assert by_id[idle.id] == {
        "can_run": True, "code": None, "message": None, "resumes_at": None,
        "job_id": None, "violation_code": None,
    }
    assert (by_id[busy.id]["code"], by_id[busy.id]["job_id"]) == ("run_in_flight", job.id)

    r = await client.get(f"/scrapers/{busy.id}", headers=_auth(user))
    assert r.json()["run_eligibility"]["code"] == "run_in_flight"


async def test_get_one_reports_an_entitlement_paused_scraper(db, connectors, client: AsyncClient):
    county = _county()
    await connectors(county, ["probate"], "manual")
    user = await _user(db)
    paused = await _config(db, user, county, active=False, paused_reason="entitlement")
    r = await client.get(f"/scrapers/{paused.id}", headers=_auth(user))
    assert r.status_code == 200
    assert r.json()["run_eligibility"]["code"] == "config_inactive"


async def test_other_routes_leave_run_eligibility_uncomputed(db, connectors, client: AsyncClient):
    county = _county()
    await connectors(county, ["probate"], "manual")
    user = await _user(db)
    config = await _config(db, user, county)
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={"name": "Renamed", "updated_at": config.updated_at.isoformat()},
        headers=_auth(user),
    )
    assert r.status_code == 200, r.text
    assert r.json()["run_eligibility"] is None
