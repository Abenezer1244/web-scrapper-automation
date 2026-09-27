"""Account-level run eligibility (UX audit Q6 / F-035, item 2b-i).

``run_eligibility`` is the one statement of "may this account start billable
work, and if not, why and until when". ``quota_block_reason`` (every enqueue
gate) and ``GET /billing/usage`` (what the UI reads) are both built on it, so
the page can no longer disagree with the gate.

Every user here is a real row in the test database. The function-level tests
pin ``now`` so boundaries are exact; the route tests use windows placed days
away from the real clock so they cannot flake at an edge.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import create_secure_token, hash_password
from src.db.models import ScraperConfig, User

# Today's exact prose, spelled out rather than imported: a test that compares
# the function with its own constant would pass whatever the constant said.
FROZEN_MSG = (
    "Your subscription payment could not be completed, so new scrapes are "
    "paused. Update your payment method to resume. Your data and past exports "
    "are untouched."
)
ENDED_MSG = (
    "Your subscription has ended, so new scrapes are paused. Resubscribe to "
    "continue. Your data and past exports are untouched."
)


def _over_msg(used: int, limit: int, reset: datetime) -> str:
    return (
        f"Record limit reached ({used}/{limit}). Your quota resets "
        f"{reset.date().isoformat()} (UTC). Upgrade your plan to continue now."
    )


# A fixed clock for the function-level tests, and a window around it.
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
ANCHOR = datetime(2026, 3, 20, tzinfo=UTC)
LIVE_START = datetime(2026, 9, 20, tzinfo=UTC)
LIVE_END = datetime(2026, 10, 20, tzinfo=UTC)
# The window that precedes the live one: a user still sitting on it at NOW has
# crossed a boundary the lazy rollover has not caught up with yet.
PREV_START = datetime(2026, 8, 20, tzinfo=UTC)


async def _user(db: AsyncSession, **kw) -> User:
    fields = {
        "id": str(uuid.uuid4()),
        "email": f"test_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        "password_hash": hash_password("TestPass123!"),
        "plan": "pro",
        "records_limit": 1000,
        "records_used": 0,
        "subscription_status": "active",
        "quota_anchor_at": ANCHOR,
        "quota_period_start": LIVE_START,
        "quota_period_end": LIVE_END,
        "records_period_start": LIVE_START,
    }
    fields.update(kw)
    user = User(**fields)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _stale(**kw) -> dict:
    """A user whose stored window ended at LIVE_START (rollover not yet run)."""
    return {"quota_period_start": PREV_START, "quota_period_end": LIVE_START, **kw}


# ─── run_eligibility: the codes ───────────────────────────────────────────────

async def test_ok_user_can_run_with_nothing_else_set(db):
    from src.api.quota import run_eligibility

    user = await _user(db, records_used=10)
    e = run_eligibility(user, NOW)
    assert (e.can_run, e.code, e.message, e.resumes_at) == (True, None, None, None)


@pytest.mark.parametrize("status", ["unpaid", "incomplete", "incomplete_expired", "paused"])
async def test_frozen_status_is_frozen_with_no_resume_date(db, status):
    from src.api.quota import run_eligibility

    user = await _user(db, subscription_status=status)
    e = run_eligibility(user, NOW)
    assert (e.can_run, e.code, e.message, e.resumes_at) == (False, "frozen", FROZEN_MSG, None)


async def test_past_due_is_frozen_only_after_its_grace(db):
    from src.api.quota import run_eligibility

    in_grace = await _user(
        db, subscription_status="past_due", entitlement_grace_ends_at=NOW + timedelta(days=1)
    )
    expired = await _user(
        db, subscription_status="past_due", entitlement_grace_ends_at=NOW - timedelta(days=1)
    )
    assert run_eligibility(in_grace, NOW).can_run is True
    assert run_eligibility(expired, NOW).code == "frozen"


async def test_ended_is_ended_with_no_resume_date(db):
    from src.api.quota import run_eligibility

    user = await _user(db, entitlement_ends_at=NOW - timedelta(hours=1))
    e = run_eligibility(user, NOW)
    assert (e.can_run, e.code, e.message, e.resumes_at) == (False, "ended", ENDED_MSG, None)


async def test_ended_starts_exactly_at_entitlement_ends_at(db):
    from src.api.quota import run_eligibility

    user = await _user(db, entitlement_ends_at=NOW)
    assert run_eligibility(user, NOW).code == "ended"
    assert run_eligibility(user, NOW - timedelta(microseconds=1)).can_run is True


async def test_frozen_wins_over_ended_and_over_limit(db):
    from src.api.quota import run_eligibility

    user = await _user(
        db,
        subscription_status="unpaid",
        entitlement_ends_at=NOW - timedelta(hours=1),
        records_used=1000,
    )
    assert run_eligibility(user, NOW).code == "frozen"


async def test_over_limit_resumes_at_the_window_end(db):
    from src.api.quota import run_eligibility

    user = await _user(db, records_used=1000)
    e = run_eligibility(user, NOW)
    assert (e.can_run, e.code, e.resumes_at) == (False, "over_limit", LIVE_END)
    assert e.message == _over_msg(1000, 1000, LIVE_END)


@pytest.mark.parametrize("ends_at", [LIVE_END, LIVE_END - timedelta(days=5)])
async def test_over_limit_on_a_cancelled_term_promises_no_reset(db, ends_at):
    """Paid access stops at or before the window end, so the window does not
    reset there (``should_roll`` refuses: no entitlement left). Promising a
    reset date sent the customer to wait for a quota that never came back."""
    from src.api.quota import run_eligibility

    user = await _user(db, records_used=1000, entitlement_ends_at=ends_at)
    e = run_eligibility(user, NOW)
    assert (e.can_run, e.code, e.resumes_at) == (False, "over_limit", None)
    assert "resets" not in e.message
    assert e.message.startswith("Record limit reached (1000/1000).")
    assert ends_at.date().isoformat() in e.message


async def test_over_limit_on_a_term_ending_after_the_window_still_resets(db):
    from src.api.quota import run_eligibility

    user = await _user(
        db, records_used=1000, entitlement_ends_at=LIVE_END + timedelta(days=1)
    )
    e = run_eligibility(user, NOW)
    assert (e.code, e.resumes_at) == ("over_limit", LIVE_END)
    assert e.message == _over_msg(1000, 1000, LIVE_END)


async def test_unlimited_is_never_over(db):
    from src.api.quota import run_eligibility

    user = await _user(db, plan="agency", records_limit=-1, records_used=10**7)
    assert run_eligibility(user, NOW).can_run is True


async def test_window_boundary_belongs_to_the_new_window(db):
    """At exactly the stored window end the user is in the NEW window: the old
    usage is no longer theirs, so they may run."""
    from src.api.quota import run_eligibility

    user = await _user(db, records_used=1000)
    assert run_eligibility(user, LIVE_END - timedelta(microseconds=1)).code == "over_limit"
    assert run_eligibility(user, LIVE_END).can_run is True


async def test_naive_datetimes_are_read_as_utc(db):
    """Some drivers hand back naive values from timestamptz columns."""
    from src.api.quota import run_eligibility

    user = await _user(db, records_used=1000)
    user.quota_period_start = LIVE_START.replace(tzinfo=None)
    user.quota_period_end = LIVE_END.replace(tzinfo=None)
    user.quota_anchor_at = ANCHOR.replace(tzinfo=None)
    e = run_eligibility(user, NOW)
    assert e.resumes_at == LIVE_END
    assert e.resumes_at.tzinfo is not None


# ─── quota_block_reason: the enqueue gates see exactly the same answer ────────

async def test_quota_block_reason_keeps_todays_prose(db):
    from src.api.quota import quota_block_reason

    assert quota_block_reason(await _user(db), NOW) is None
    assert quota_block_reason(await _user(db, subscription_status="unpaid"), NOW) == FROZEN_MSG
    ended = await _user(db, entitlement_ends_at=NOW - timedelta(hours=1))
    assert quota_block_reason(ended, NOW) == ENDED_MSG
    over = await _user(db, records_used=1200)
    assert quota_block_reason(over, NOW) == _over_msg(1200, 1000, LIVE_END)


async def test_quota_block_reason_on_a_cancelled_term_promises_no_reset(db):
    from src.api.quota import quota_block_reason

    user = await _user(db, records_used=1000, entitlement_ends_at=LIVE_END)
    reason = quota_block_reason(user, NOW)
    assert reason is not None
    assert "resets" not in reason


# ─── RunEligibilityResponse: impossible combinations are refused ──────────────

@pytest.mark.parametrize(
    "payload",
    [
        {"can_run": True, "code": "over_limit", "message": None, "resumes_at": None},
        {"can_run": True, "code": None, "message": "x", "resumes_at": None},
        {"can_run": True, "code": None, "message": None, "resumes_at": LIVE_END},
        {"can_run": False, "code": None, "message": "x", "resumes_at": None},
        {"can_run": False, "code": "frozen", "message": None, "resumes_at": None},
        {"can_run": False, "code": "frozen", "message": "x", "resumes_at": LIVE_END},
        {"can_run": False, "code": "ended", "message": "x", "resumes_at": LIVE_END},
        {"can_run": False, "code": "ai_limit", "message": "x", "resumes_at": None},
    ],
)
def test_run_eligibility_response_rejects_impossible_states(payload):
    from src.api.schemas import RunEligibilityResponse

    with pytest.raises(ValidationError):
        RunEligibilityResponse(**payload)


def test_run_eligibility_response_accepts_the_real_states():
    from src.api.schemas import RunEligibilityResponse

    RunEligibilityResponse(can_run=True, code=None, message=None, resumes_at=None)
    RunEligibilityResponse(can_run=False, code="frozen", message="x", resumes_at=None)
    RunEligibilityResponse(can_run=False, code="ended", message="x", resumes_at=None)
    RunEligibilityResponse(can_run=False, code="over_limit", message="x", resumes_at=LIVE_END)
    RunEligibilityResponse(can_run=False, code="over_limit", message="x", resumes_at=None)


# ─── usage_view: what /billing/usage reports, on a fixed clock ────────────────

async def test_usage_view_reports_eligibility_and_the_window(db):
    from src.api.routes.billing import usage_view

    view = usage_view(await _user(db, records_used=1000), NOW)
    assert view["run_eligibility"].code == "over_limit"
    assert view["run_eligibility"].resumes_at == LIVE_END
    assert (view["period_start"], view["next_reset_at"]) == (LIVE_START, LIVE_END)
    assert (view["records_used"], view["records_limit"], view["records_remaining"]) == (
        1000, 1000, 0,
    )


@pytest.mark.parametrize(
    "ends_at, expected_reset",
    [
        (LIVE_END - timedelta(days=5), None),
        (LIVE_END, None),
        (LIVE_END + timedelta(days=1), LIVE_END),
    ],
)
async def test_usage_view_never_advertises_a_reset_that_ends_access(db, ends_at, expected_reset):
    from src.api.routes.billing import usage_view

    view = usage_view(await _user(db, entitlement_ends_at=ends_at), NOW)
    assert view["next_reset_at"] == expected_reset


async def test_usage_view_applies_a_pending_downgrade_the_rollover_will_apply(db):
    """Business with a pending Pro downgrade, window ended, rollover not yet
    run. The gate and the next charge already treat them as Pro at 0/1000; the
    page used to say Business 0/5000 with a downgrade still "upcoming"."""
    from src.api.routes.billing import usage_view

    user = await _user(
        db,
        **_stale(
            plan="business", records_limit=5000, records_used=3000,
            pending_plan="pro", pending_records_limit=1000,
        ),
    )
    view = usage_view(user, NOW)
    assert (view["plan"], view["records_limit"], view["records_used"]) == ("pro", 1000, 0)
    assert (view["records_remaining"], view["percent_used"]) == (1000, 0)
    assert (view["pending_plan"], view["pending_records_limit"]) == (None, None)
    assert (view["period_start"], view["next_reset_at"]) == (LIVE_START, LIVE_END)
    assert view["run_eligibility"].can_run is True


async def test_usage_view_agency_downgrade_is_no_longer_unlimited(db):
    from src.api.routes.billing import usage_view

    user = await _user(
        db,
        **_stale(
            plan="agency", records_limit=-1, records_used=9000,
            pending_plan="pro", pending_records_limit=1000,
        ),
    )
    view = usage_view(user, NOW)
    assert (view["plan"], view["records_limit"], view["records_remaining"]) == ("pro", 1000, 1000)


@pytest.mark.parametrize("current_plan, current_limit", [("pro", 1000), ("agency", -1)])
async def test_usage_view_downgrade_to_starter(db, current_plan, current_limit):
    from src.api.routes.billing import usage_view

    user = await _user(
        db,
        **_stale(
            plan=current_plan, records_limit=current_limit, records_used=40,
            pending_plan="starter", pending_records_limit=50,
        ),
    )
    view = usage_view(user, NOW)
    assert (view["plan"], view["records_limit"], view["records_remaining"]) == ("starter", 50, 50)
    assert (view["pending_plan"], view["pending_records_limit"]) == (None, None)


async def test_usage_view_keeps_a_pending_downgrade_upcoming_inside_the_window(db):
    from src.api.routes.billing import usage_view

    user = await _user(
        db, plan="business", records_limit=5000, records_used=3000,
        pending_plan="pro", pending_records_limit=1000,
    )
    view = usage_view(user, NOW)
    assert (view["plan"], view["records_limit"], view["records_used"]) == ("business", 5000, 3000)
    assert (view["pending_plan"], view["pending_records_limit"]) == ("pro", 1000)


async def test_usage_view_rolls_at_exactly_the_window_end(db):
    from src.api.routes.billing import usage_view

    user = await _user(db, records_used=1000)
    view = usage_view(user, LIVE_END)
    assert view["records_used"] == 0
    assert view["period_start"] == LIVE_END
    assert view["run_eligibility"].can_run is True


async def test_usage_view_publishes_utc_instants_for_naive_rows(db):
    from src.api.routes.billing import usage_view

    user = await _user(db, entitlement_ends_at=LIVE_END + timedelta(days=40))
    user.quota_period_start = LIVE_START.replace(tzinfo=None)
    user.quota_period_end = LIVE_END.replace(tzinfo=None)
    user.quota_anchor_at = ANCHOR.replace(tzinfo=None)
    user.entitlement_ends_at = user.entitlement_ends_at.replace(tzinfo=None)
    view = usage_view(user, NOW)
    for key in ("period_start", "next_reset_at", "entitlement_ends_at"):
        assert view[key].tzinfo is not None, key
    assert view["entitlement_ends_at"] == LIVE_END + timedelta(days=40)


# ─── The HTTP contract ────────────────────────────────────────────────────────

def _live_window() -> dict:
    now = datetime.now(UTC)
    start = now - timedelta(days=10)
    return {
        "quota_anchor_at": start,
        "quota_period_start": start,
        "quota_period_end": start + timedelta(days=30),
        "records_period_start": start,
    }


async def _get_usage(client: AsyncClient, user: User) -> dict:
    r = await client.get(
        "/billing/usage",
        headers={"Authorization": f"Bearer {create_secure_token(user.id)}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_usage_route_reports_run_eligibility_for_each_state(client, db):
    now = datetime.now(UTC)
    ok = await _user(db, **_live_window())
    frozen = await _user(db, subscription_status="unpaid", **_live_window())
    ended = await _user(db, entitlement_ends_at=now - timedelta(hours=1), **_live_window())
    over = await _user(db, records_used=1000, **_live_window())

    assert (await _get_usage(client, ok))["run_eligibility"] == {
        "can_run": True, "code": None, "message": None, "resumes_at": None,
    }
    assert (await _get_usage(client, frozen))["run_eligibility"] == {
        "can_run": False, "code": "frozen", "message": FROZEN_MSG, "resumes_at": None,
    }
    assert (await _get_usage(client, ended))["run_eligibility"] == {
        "can_run": False, "code": "ended", "message": ENDED_MSG, "resumes_at": None,
    }
    body = await _get_usage(client, over)
    elig = body["run_eligibility"]
    assert (elig["can_run"], elig["code"]) == (False, "over_limit")
    assert elig["resumes_at"] is not None
    assert datetime.fromisoformat(elig["resumes_at"]) == datetime.fromisoformat(
        body["next_reset_at"]
    )
    assert datetime.fromisoformat(elig["resumes_at"]).tzinfo is not None


async def test_usage_route_cancelled_term_has_no_reset_date(client, db):
    window = _live_window()
    user = await _user(db, entitlement_ends_at=window["quota_period_end"], **window)
    body = await _get_usage(client, user)
    assert body["next_reset_at"] is None
    assert datetime.fromisoformat(body["entitlement_ends_at"]) == window["quota_period_end"]


async def test_usage_route_reports_the_limit_the_gate_enforces(client, db):
    now = datetime.now(UTC)
    start = now - timedelta(days=40)
    user = await _user(
        db,
        plan="business", records_limit=5000, records_used=3000,
        pending_plan="pro", pending_records_limit=1000,
        quota_anchor_at=start, quota_period_start=start,
        quota_period_end=start + timedelta(days=30), records_period_start=start,
    )
    body = await _get_usage(client, user)
    assert (body["plan"], body["records_limit"], body["records_used"]) == ("pro", 1000, 0)
    assert (body["pending_plan"], body["pending_records_limit"]) == (None, None)


# ─── POST /jobs: the 402 text is today's, for every account-level refusal ─────

async def _config_for(db: AsyncSession, user: User) -> ScraperConfig:
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name="Eligibility Pierce Probate",
        county="pierce",
        state="WA",
        record_type="probate",
        fields=["party_name", "parcel_id"],
        enrichment=[],
        schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    return config


async def _post_job(client: AsyncClient, db: AsyncSession, user: User):
    config = await _config_for(db, user)
    return await client.post(
        "/jobs",
        json={"scraper_config_id": config.id, "trigger": "manual"},
        headers={"Authorization": f"Bearer {create_secure_token(user.id)}"},
    )


async def test_post_jobs_402_carries_todays_prose(client, db):
    now = datetime.now(UTC)
    frozen = await _user(db, subscription_status="unpaid", **_live_window())
    ended = await _user(db, entitlement_ends_at=now - timedelta(hours=1), **_live_window())
    window = _live_window()
    over = await _user(db, records_used=1000, **window)

    r = await _post_job(client, db, frozen)
    assert (r.status_code, r.json()["detail"]) == (402, FROZEN_MSG)
    r = await _post_job(client, db, ended)
    assert (r.status_code, r.json()["detail"]) == (402, ENDED_MSG)
    r = await _post_job(client, db, over)
    assert (r.status_code, r.json()["detail"]) == (
        402, _over_msg(1000, 1000, window["quota_period_end"]),
    )


@pytest.mark.parametrize("days_before_end", [0, 5])
async def test_post_jobs_402_on_a_cancelled_term_promises_no_reset(client, db, days_before_end):
    window = _live_window()
    user = await _user(
        db,
        records_used=1000,
        entitlement_ends_at=window["quota_period_end"] - timedelta(days=days_before_end),
        **window,
    )
    r = await _post_job(client, db, user)
    assert r.status_code == 402
    assert r.json()["detail"].startswith("Record limit reached (1000/1000).")
    assert "resets" not in r.json()["detail"]
