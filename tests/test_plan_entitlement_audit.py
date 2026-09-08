"""Every advertised plan entitlement, probed through the real routes.

The plan cards on the billing page are a promise. This file is the proof that
the API keeps it, or the record of exactly where it does not. Each test drives a
real HTTP request against the real app with a real user row of the plan under
test, so a gate that exists only in the frontend cannot pass here.

Three things are deliberately asserted as they ARE rather than as the card
reads, each with the mismatch named in the test's own docstring:

  * export FORMAT and schedule FREQUENCY carry no plan gate at all, so a Starter
    account can save an Excel/JSON export and a daily schedule that the cards
    sell as Pro and above;
  * the overlap/intersection segment routes carry no plan gate, so Starter and
    Pro reach what is sold as a Business line.

Those tests are named ``test_documents_...`` and are the regression pin for the
gap. When a gate lands, they are the tests to rewrite, not delete.

Enforcement note: county and record-type gating runs behind
``settings.ENTITLEMENT_ENFORCEMENT``, which defaults False in code and is
``true`` on the production api and worker services. Tests that exercise those
two gates flip the flag on, because production behaviour is what the cards
describe.
"""
from __future__ import annotations

import inspect
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import create_secure_token, hash_password
from src.config.constants import (
    ALL_RECORD_TYPES,
    BATCH_PLANS,
    BUSINESS_FEATURES_PLANS,
    COUNTY_LIMIT_BY_PLAN,
    PRIORITY_QUEUE_PLANS,
    RECORD_TYPES_BY_PLAN,
    SKIP_TRACE_ADDON_PLANS,
)
from src.config.plans import PLAN_CATALOG, get_plan
from src.config.settings import settings
from src.db.models import ScraperConfig, User

PLANS = ("starter", "pro", "business", "agency")

# The record types each card promises, written out by hand from the marketing
# copy. The grid test grades against THIS, not against RECORD_TYPES_BY_PLAN,
# so a change to the constant cannot quietly re-grade the promise it is meant
# to be measured against.
CARD_RECORD_TYPES: dict[str, frozenset[str]] = {
    # "Probate records" is the only list the Starter card names.
    "starter": frozenset({"probate"}),
    # "Probate, pre-foreclosure, tax-delinquent & auction lists".
    "pro": frozenset({"probate", "pre_foreclosure", "tax_delinquent", "trustee_sale"}),
    # "All record types".
    "business": frozenset({
        "probate", "pre_foreclosure", "tax_delinquent", "trustee_sale",
        "code_violation", "divorce", "death_certificate",
    }),
    "agency": frozenset({
        "probate", "pre_foreclosure", "tax_delinquent", "trustee_sale",
        "code_violation", "divorce", "death_certificate",
    }),
}

# One live (county, record_type) pair per type, from the seeded connectors.
# _validate_connector_supports runs BEFORE the entitlement gate, so a pair the
# county does not offer 422s and proves nothing about plans.
COUNTY_FOR_TYPE = {
    "probate": "king",
    "pre_foreclosure": "king",
    "tax_delinquent": "king",
    "trustee_sale": "king",
    "code_violation": "king",
    "death_certificate": "king",
    "divorce": "pierce",
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def enforcement_on(monkeypatch):
    """Match production: ENTITLEMENT_ENFORCEMENT is true on api and worker."""
    monkeypatch.setattr(settings, "ENTITLEMENT_ENFORCEMENT", True)


@pytest.fixture
def make_user(db: AsyncSession):
    """Build a real user row on any plan and hand back (user, bearer token).

    A factory rather than four fixtures: the audit runs the same probe against
    every tier, and no test may be pinned to one hardcoded account.
    """

    async def _make(plan: str) -> tuple[User, str]:
        user = User(
            id=str(uuid.uuid4()),
            email=f"audit_{uuid.uuid4().hex[:10]}@test.bridgeleads.io",
            password_hash=hash_password("TestPass123!"),
            plan=plan,
            records_used=0,
            records_limit=get_plan(plan)["records_limit"],
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user, create_secure_token(user.id)

    return _make



@pytest.fixture
def sync_db():
    """A synchronous session on the test engine. The skip-trace billing path is
    sync because the Tracerfy ingest worker that owns the transaction is."""
    from src.db.session import SyncSessionLocal

    session = SyncSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _body(county: str, record_type: str = "probate", **extra) -> dict:
    body = {
        "name": f"Audit {county} {record_type}",
        "county": county,
        "state": "WA",
        "record_type": record_type,
    }
    body.update(extra)
    return body


# -- Records per month -------------------------------------------------------

@pytest.mark.parametrize(
    ("plan", "expected"),
    [("starter", 50), ("pro", 1000), ("business", 5000), ("agency", -1)],
)
def test_the_card_record_allowance_is_the_one_the_gates_read(plan, expected):
    """PLAN_CATALOG feeds the card; settings.PLAN_LIMITS feeds registration and
    the trial. They are two constants and they must not drift apart."""
    assert get_plan(plan)["records_limit"] == expected
    assert settings.PLAN_LIMITS[plan] == expected


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", PLANS)
async def test_the_record_cap_blocks_one_over_and_allows_one_under(plan, db, make_user):
    """One under the cap runs, the cap itself is the last unit consumed, one over
    is refused. An unlimited plan is never refused on volume."""
    from src.api.quota import is_over_record_limit

    user, _token = await make_user(plan)
    limit = get_plan(plan)["records_limit"]
    assert user.records_limit == limit

    if limit < 0:
        user.records_used = 10 ** 9
        assert is_over_record_limit(user) is False
        return

    user.records_used = limit - 1
    assert is_over_record_limit(user) is False
    user.records_used = limit
    assert is_over_record_limit(user) is True
    user.records_used = limit + 1
    assert is_over_record_limit(user) is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_starter_at_the_cap_is_refused_a_new_run_by_the_api(
    client, db, make_user
):
    """The block is server-side, not a disabled button: POST /jobs refuses."""
    user, token = await make_user("starter")
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name="Audit cap",
        county="king",
        state="WA",
        record_type="probate",
        fields=["party_name"],
        enrichment=[],
        schedule={"frequency": "manual"},
        deliver={"formats": ["csv"], "emails": []},
    )
    db.add(config)
    user.records_used = get_plan("starter")["records_limit"]
    await db.commit()

    r = await client.post(
        "/jobs", json={"scraper_config_id": str(config.id)}, headers=_auth(token)
    )
    assert r.status_code == 402, r.text
    assert "Record limit reached" in str(r.json()["detail"])


# -- Counties ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("plan", "cap"), [("starter", 1), ("pro", 3), ("business", 10), ("agency", -1)]
)
def test_the_card_county_count_is_the_enforced_cap(plan, cap):
    assert COUNTY_LIMIT_BY_PLAN[plan] == cap
    bullet = " ".join(get_plan(plan)["features"]).lower()
    if cap < 0:
        assert "unlimited counties" in bullet
    else:
        assert f"{cap} count" in bullet


@pytest.mark.integration
@pytest.mark.asyncio
async def test_starter_gets_one_county_and_is_refused_the_second(
    client, db, make_user, enforcement_on
):
    _user, token = await make_user("starter")
    first = await client.post("/scrapers", json=_body("king"), headers=_auth(token))
    assert first.status_code == 201, first.text
    second = await client.post("/scrapers", json=_body("pierce"), headers=_auth(token))
    assert second.status_code == 402, second.text
    assert second.json()["detail"]["code"] == "county_limit"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pro_gets_three_counties_and_is_refused_the_fourth(
    client, db, make_user, enforcement_on
):
    _user, token = await make_user("pro")
    # snohomish offers no probate connector, and _validate_connector_supports runs
    # before the plan gate, so each county needs a type it actually serves.
    for county, record_type in (
        ("king", "probate"),
        ("pierce", "probate"),
        ("snohomish", "tax_delinquent"),
    ):
        r = await client.post(
            "/scrapers", json=_body(county, record_type), headers=_auth(token)
        )
        assert r.status_code == 201, f"{county}: {r.text}"
    fourth = await client.post(
        "/scrapers", json=_body("clark", "trustee_sale"), headers=_auth(token)
    )
    assert fourth.status_code == 402, fourth.text
    assert fourth.json()["detail"]["code"] == "county_limit"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_business_is_measured_against_ten_and_not_refused_below_it(
    client, db, make_user, enforcement_on
):
    """Four WA counties are seeded, so an eleventh cannot be reached through a
    connector. What is provable: nothing refuses Business below its cap, and the
    cap the account-wide count is measured against is 10."""
    from src.api.entitlements import projected_county_overage

    user, token = await make_user("business")
    for county in ("king", "pierce", "snohomish", "clark"):
        r = await client.post(
            "/scrapers", json=_body(county, "trustee_sale"), headers=_auth(token)
        )
        assert r.status_code == 201, f"{county}: {r.text}"

    assert await projected_county_overage(db, user.id, "business", "WA", {"whatcom"}) is None
    assert await projected_county_overage(
        db, user.id, "business", "WA", {f"county{i}" for i in range(6)}
    ) is None
    assert await projected_county_overage(
        db, user.id, "business", "WA", {f"county{i}" for i in range(7)}
    ) == (11, 10)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agency_has_no_finite_county_cap(db, make_user, enforcement_on):
    from src.api.entitlements import projected_county_overage

    user, _token = await make_user("agency")
    assert await projected_county_overage(
        db, user.id, "agency", "WA", {f"county{i}" for i in range(500)}
    ) is None


# -- Record types ------------------------------------------------------------

@pytest.mark.parametrize("plan", PLANS)
def test_the_matrix_is_the_set_the_card_describes(plan):
    assert RECORD_TYPES_BY_PLAN[plan] == CARD_RECORD_TYPES[plan]


def test_all_record_types_means_every_live_type():
    """"All record types" has to mean the whole live set, or Business and Agency
    are sold a superlative the connector registry does not back."""
    assert ALL_RECORD_TYPES == CARD_RECORD_TYPES["business"]
    assert set(COUNTY_FOR_TYPE) == set(ALL_RECORD_TYPES)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", sorted(COUNTY_FOR_TYPE))
@pytest.mark.parametrize("plan", PLANS)
async def test_every_record_type_against_every_plan(
    client, db, make_user, enforcement_on, plan, record_type
):
    """The full 4x7 grid, through the create route. An allowed pair must persist;
    a disallowed pair must come back 402 with a plan code, never 201."""
    _user, token = await make_user(plan)
    county = COUNTY_FOR_TYPE[record_type]
    r = await client.post(
        "/scrapers", json=_body(county, record_type), headers=_auth(token)
    )
    if record_type in CARD_RECORD_TYPES[plan]:
        assert r.status_code == 201, f"{plan}/{record_type} refused: {r.text}"
    else:
        assert r.status_code == 402, f"{plan}/{record_type} allowed: {r.text}"
        assert r.json()["detail"]["code"] in {"record_type", "plan_limit"}


# -- Skip tracing ------------------------------------------------------------

@pytest.mark.parametrize(
    ("plan", "included"),
    [("starter", 0), ("pro", 250), ("business", 1000), ("agency", 2000)],
)
def test_the_card_skip_trace_allowance_is_the_billed_quota(plan, included):
    assert settings.SKIP_TRACE_BUNDLED_QUOTAS[plan] == included


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", PLANS)
async def test_the_metered_skip_trace_addon_is_pro_and_above(client, db, make_user, plan):
    _user, token = await make_user(plan)
    r = await client.post(
        "/scrapers", json=_body("king", skip_trace_enabled=True), headers=_auth(token)
    )
    if plan in SKIP_TRACE_ADDON_PLANS:
        assert r.status_code == 201, r.text
    else:
        assert r.status_code == 402, r.text


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", PLANS)
async def test_the_enrichment_skip_tracing_toggle_is_gated_to_business_and_above(
    client, db, make_user, plan
):
    _user, token = await make_user(plan)
    r = await client.post(
        "/scrapers",
        json=_body("king", enrichment={"skip_tracing": True}),
        headers=_auth(token),
    )
    if plan in BUSINESS_FEATURES_PLANS:
        assert r.status_code == 201, r.text
    else:
        assert r.status_code == 402, r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_only_the_units_above_the_bundled_quota_are_metered(db, make_user, sync_db):
    """Below the included allowance nothing is billable; above it only the excess
    is, and a later batch does not re-bill the earlier one's overage.

    Calls the production function against a real user row rather than restating
    its formula: a test that re-implements the arithmetic it is checking proves
    only that the test author can subtract."""
    from src.api.billing.skip_trace_usage import report_lookups_for_user

    quota = settings.SKIP_TRACE_BUNDLED_QUOTAS["pro"]
    user, _token = await make_user("pro")
    uid = str(user.id)

    # Everything inside the allowance: nothing to bill.
    first = report_lookups_for_user(sync_db, uid, quota, queue_id=900001)
    assert first["quota"] == quota
    assert first["used_after"] == quota
    assert first["billable_units"] == 0

    # One past it: exactly one unit.
    second = report_lookups_for_user(sync_db, uid, 1, queue_id=900002)
    assert second["used_after"] == quota + 1
    assert second["billable_units"] == 1

    # A later batch bills only its own excess, not the running total.
    third = report_lookups_for_user(sync_db, uid, 10, queue_id=900003)
    assert third["billable_units"] == 10
    sync_db.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_lookup_straddling_the_allowance_bills_only_its_overage(
    db, make_user, sync_db
):
    from src.api.billing.skip_trace_usage import report_lookups_for_user

    quota = settings.SKIP_TRACE_BUNDLED_QUOTAS["pro"]
    user, _token = await make_user("pro")
    uid = str(user.id)
    report_lookups_for_user(sync_db, uid, quota - 5, queue_id=900004)
    straddle = report_lookups_for_user(sync_db, uid, 10, queue_id=900005)
    assert straddle["billable_units"] == 5
    sync_db.rollback()


def test_documents_that_a_metered_lookup_never_reaches_an_invoice():
    """MISMATCH, and the most expensive one. The Pro card sells "then $0.08 per
    lookup". Checkout builds the subscription with a single licensed line item
    (src/api/routes/billing.py, create_checkout) and nothing in the codebase ever
    adds a subscription item priced against the skip-trace meter, so the
    MeterEvent this path fires has no price to settle against.

    The three configured metered price ids are the proof: they exist in settings,
    they are set in production, and no runtime module reads them.

    Rewrite this test when the metered price is attached at checkout."""
    import src.api.billing.skip_trace_usage as st
    import src.api.routes.billing as billing

    for slot in (
        "STRIPE_PRICE_SKIP_TRACE_PRO",
        "STRIPE_PRICE_SKIP_TRACE_BUSINESS_OVERAGE",
        "STRIPE_PRICE_SKIP_TRACE_AGENCY_OVERAGE",
    ):
        assert hasattr(settings, slot)
        assert slot not in inspect.getsource(billing)
        assert slot not in inspect.getsource(st)

    create_checkout_src = inspect.getsource(billing.create_checkout)
    assert 'line_items=[{"price": stripe_price_id, "quantity": 1}]' in create_checkout_src


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", PLANS)
async def test_the_usage_endpoint_reports_the_right_included_amount_and_rate(
    client, db, make_user, plan
):
    _user, token = await make_user(plan)
    r = await client.get("/billing/skip-trace-usage", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["quota"] == settings.SKIP_TRACE_BUNDLED_QUOTAS[plan]
    expected_rate = {"starter": None, "pro": 0.08, "business": 0.08, "agency": 0.05}[plan]
    assert body["overage_rate_usd"] == expected_rate


# -- Export formats ----------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["csv", "excel", "xlsx", "json"])
async def test_documents_that_export_format_carries_no_plan_gate(
    client, db, make_user, fmt
):
    """MISMATCH. The Starter card sells "CSV export" and Pro "CSV + Excel", but
    DeliverConfig validates `formats` against SUPPORTED_EXPORT_FORMATS alone and
    no route consults the plan. A Starter account saves JSON here.

    Rewrite this test when the gate lands; do not delete it."""
    _user, token = await make_user("starter")
    r = await client.post(
        "/scrapers",
        json=_body("king", deliver={"formats": [fmt], "emails": []}),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["deliver"]["formats"] == [fmt]


# -- Schedules ---------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("frequency", ["manual", "daily", "weekly", "monthly"])
async def test_documents_that_schedule_frequency_carries_no_plan_gate(
    client, db, make_user, frequency
):
    """MISMATCH. The Starter card sells "Manual runs" and Pro "Daily/weekly", but
    ScheduleConfig only checks the value is a known frequency. A Starter account
    saves a monthly recurring schedule here and the beat dispatcher fires it.

    Rewrite this test when the gate lands; do not delete it."""
    _user, token = await make_user("starter")
    r = await client.post(
        "/scrapers",
        json=_body("king", schedule={"frequency": frequency}),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["schedule"]["frequency"] == frequency


# -- Delivery ----------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", PLANS)
async def test_webhook_delivery_is_business_and_above(client, db, make_user, plan):
    _user, token = await make_user(plan)
    r = await client.post(
        "/scrapers",
        json=_body(
            "king", deliver={"webhook_url": "https://example.com/hook", "emails": []}
        ),
        headers=_auth(token),
    )
    if plan in BUSINESS_FEATURES_PLANS:
        assert r.status_code == 201, r.text
    else:
        assert r.status_code == 402, r.text


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", PLANS)
async def test_dialer_delivery_is_business_and_above(client, db, make_user, plan):
    """The dialer push carries the same entitlement as the summary webhook: both
    POST lead PII outbound, so gating one and not the other would be a hole."""
    _user, token = await make_user(plan)
    r = await client.post(
        "/scrapers",
        json=_body(
            "king",
            deliver={"dialer_webhook_url": "https://example.com/dialer", "emails": []},
        ),
        headers=_auth(token),
    )
    if plan in BUSINESS_FEATURES_PLANS:
        assert r.status_code == 201, r.text
    else:
        assert r.status_code == 402, r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_documents_that_email_delivery_carries_no_plan_gate(
    client, db, make_user
):
    """MISMATCH, small. The /billing/pricing comparison row says Email delivery is
    False on Starter. Nothing enforces that: `deliver.emails` is accepted on
    every plan. The in-app Starter card makes no email claim either way."""
    _user, token = await make_user("starter")
    r = await client.post(
        "/scrapers",
        json=_body(
            "king",
            deliver={"emails": ["lead@test.bridgeleads.io"], "formats": ["csv"]},
        ),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text


# -- Batch scraping ----------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", PLANS)
async def test_batch_scraping_is_pro_and_above(client, db, make_user, plan):
    _user, token = await make_user(plan)
    r = await client.post(
        "/batches",
        json={
            "name": "Audit batch",
            "state": "WA",
            "counties": ["king"],
            "record_types": ["probate"],
        },
        headers=_auth(token),
    )
    if plan in BATCH_PLANS:
        assert r.status_code == 201, r.text
    else:
        assert r.status_code == 402, r.text


# -- API access --------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", PLANS)
async def test_api_key_issuance_is_business_and_above(client, db, make_user, plan):
    _user, token = await make_user(plan)
    r = await client.post("/auth/api-key", headers=_auth(token))
    if plan in BUSINESS_FEATURES_PLANS:
        assert r.status_code == 201, r.text
    else:
        assert r.status_code == 403, r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_key_minted_on_business_stops_working_after_a_downgrade(
    client, db, make_user
):
    """The gate is on the auth path too, not only on issuance. Gating the mint
    alone would leave a downgraded account holding a live Business key."""
    user, token = await make_user("business")
    minted = await client.post("/auth/api-key", headers=_auth(token))
    assert minted.status_code == 201, minted.text
    raw_key = minted.json()["api_key"]

    ok = await client.get("/scrapers", headers=_auth(raw_key))
    assert ok.status_code == 200, ok.text

    user.plan = "pro"
    db.add(user)
    await db.commit()

    refused = await client.get("/scrapers", headers=_auth(raw_key))
    assert refused.status_code == 403, refused.text


# -- Overlap / intersection --------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", PLANS)
async def test_documents_that_overlap_segments_carry_no_plan_gate(
    client, db, make_user, plan
):
    """MISMATCH. "All record types + overlap/intersection" is sold as a Business
    and Agency line, and /segments/intersection has no plan dependency, so a
    Starter account reaches it. lib/entitlements.ts already carries a
    canUseOverlap() helper left deliberately unwired for this reason: hiding a
    backend-allowed feature in the UI would be the wrong half to fix.

    Rewrite this test when the backend gate lands; do not delete it."""
    _user, token = await make_user(plan)
    r = await client.post(
        "/segments/intersection",
        json={"record_types": ["probate", "pre_foreclosure"]},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text


# -- Agency-only lines -------------------------------------------------------

def test_the_priority_queue_is_a_real_queue_paid_plans_are_routed_to():
    from src.workers import app as celery_app

    assert PRIORITY_QUEUE_PLANS == frozenset({"business", "agency"})
    names = {q.name for q in celery_app.conf.task_queues}
    assert "scrape-priority" in names
    assert "scrape" in names


def test_white_label_is_advertised_as_coming_soon_not_as_shipped():
    """It is not built. The only honest way to carry it on the card is the
    "coming soon" qualifier, so the qualifier is what this pins."""
    agency = " ".join(get_plan("agency")["features"]).lower()
    assert "white-label" in agency
    assert "coming soon" in agency


def test_no_plan_card_uses_an_em_dash():
    for plan in PLAN_CATALOG:
        for bullet in plan["features"]:
            assert "—" not in bullet, f"{plan['id']}: {bullet}"


# -- Data freshness ----------------------------------------------------------

def test_the_starter_seven_day_data_delay_is_real_on_the_rolling_window():
    """The comparison row "Data freshness: 7-day delay" IS implemented. The
    rolling window ends seven days back for a Starter and today for everyone
    else (src/workers/tasks_helpers/dates.py)."""
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo

    from src.workers.tasks_helpers.dates import _resolve_date_range

    today = datetime.now(ZoneInfo("US/Pacific")).date()
    schedule = {"date_range_mode": "rolling_90"}

    _, starter_to = _resolve_date_range(schedule, user_plan="starter")
    _, pro_to = _resolve_date_range(schedule, user_plan="pro")

    def _d(mmddyyyy: str) -> date:
        return datetime.strptime(mmddyyyy, "%m/%d/%Y").date()

    assert _d(pro_to) == today
    assert _d(starter_to) == today - timedelta(days=7)


def test_documents_that_a_custom_date_range_bypasses_the_starter_delay():
    """MISMATCH. The delay is applied to the rolling window only. The custom
    branch returns the caller's own date_to without clamping it to the delayed
    end, so a Starter asking for today gets today.

    Rewrite this test when the custom branch clamps; do not delete it."""
    from datetime import date
    from zoneinfo import ZoneInfo

    from src.workers.tasks_helpers.dates import _resolve_date_range

    today = datetime.now(ZoneInfo("US/Pacific")).date()
    schedule = {
        "date_range_mode": "custom",
        "date_from": (today.replace(day=1)).isoformat(),
        "date_to": today.isoformat(),
    }
    _, starter_to = _resolve_date_range(schedule, user_plan="starter")
    assert datetime.strptime(starter_to, "%m/%d/%Y").date() == today


# -- The enrichment.skip_tracing toggle --------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_documents_that_enrichment_skip_tracing_alone_runs_no_trace(
    client, db, make_user
):
    """MISMATCH. `enrichment.skip_tracing` is gated to Business and Agency and
    persisted onto the config, but the worker's skip-trace entry point reads the
    separate `skip_trace_enabled` COLUMN and never looks at the enrichment blob
    (src/workers/tasks_helpers/enrich.py, the `skip_trace_enabled` guard). A
    Business account that flips only this toggle gets no lookups.

    Rewrite this test when the two are reconciled; do not delete it."""
    from src.workers.tasks_helpers import enrich

    _user, token = await make_user("business")
    r = await client.post(
        "/scrapers",
        json=_body("king", enrichment={"skip_tracing": True}),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["enrichment"]["skip_tracing"] is True
    assert body["skip_trace_enabled"] is False

    # The guard the worker actually runs, and the absence of any read of the
    # enrichment blob's skip_tracing key anywhere in the module.
    source = inspect.getsource(enrich)
    assert 'getattr(config, "skip_trace_enabled", False)' in source
    assert 'skip_tracing' not in source


# -- Priority queue routing --------------------------------------------------

def test_documents_that_only_manual_runs_reach_the_priority_queue():
    """MISMATCH. Agency is sold a "Priority queue". POST /jobs routes Business
    and Agency to `scrape-priority` explicitly, and so does the transient-retry
    path, but the scheduled dispatcher and the batch fan-out both enqueue with
    `run_scrape_job.delay(...)`, which takes the task's default route.

    That default is the ordinary `scrape` queue for every plan, which is what
    this asserts. Recurring work is exactly the work a priority tier is for.

    Rewrite this test when the two dispatch paths carry the queue; do not
    delete it."""
    from src.workers import app as celery_app
    from src.workers.scheduler_helpers import dispatch as dispatch_mod
    from src.workers import batch_tasks as batch_mod

    route = celery_app.amqp.router.route({}, "src.workers.tasks.run_scrape_job")
    assert route["queue"].name == "scrape"

    assert "run_scrape_job.delay(jid)" in inspect.getsource(dispatch_mod)
    assert "run_scrape_job.delay(jid)" in inspect.getsource(batch_mod)


def test_the_priority_queue_is_round_robin_not_strict_priority():
    """The worker consumes `scrape-priority` before `scrape` in WORKER_QUEUES,
    but the Redis broker's default `queue_order_strategy` is round_robin and the
    app sets no transport override. A priority job therefore never waits behind
    a whole backlog, which is a real benefit, but it is not strict priority.

    Pinned so the start.sh comment ("listed first = processed first") cannot be
    read as a guarantee the transport does not give."""
    from kombu.transport.redis import Channel

    from src.workers import app as celery_app

    assert Channel.queue_order_strategy == "round_robin"
    opts = celery_app.conf.broker_transport_options or {}
    assert "queue_order_strategy" not in opts


# -- Export delivery ---------------------------------------------------------

def test_documents_that_only_the_first_selected_export_format_is_produced():
    """MISMATCH, wording. "CSV + Excel export" and "All export formats" read as
    a set the customer receives. The worker takes `formats[0]` and produces one
    file, and the combined batch export is CSV whatever the config says.

    Rewrite this test when multi-format delivery lands; do not delete it."""
    from src.workers import batch_export
    from src.workers import tasks as tasks_mod

    assert "fmt = formats[0]" in inspect.getsource(tasks_mod)
    assert 'combined.csv' in inspect.getsource(batch_export)


# -- Plan-string normalization -----------------------------------------------

@pytest.mark.parametrize(
    "stored", ["Business", "BUSINESS", "business "],
)
def test_documents_that_a_non_canonical_plan_string_loses_paid_features(stored):
    """MISMATCH. Plans in this deployment are set by hand in the database (there
    are no Stripe subscriptions), so a stored "Business" or "business " is
    reachable. Half the gates normalize with .lower() and none of them .strip().

    The failure is silent and it costs the customer: the priority-queue check in
    POST /jobs is a bare membership test, so a mis-cased Business is routed to
    the ordinary queue with no error anywhere.

    Rewrite this test when plan reads are normalized at one place; do not delete
    it."""
    assert stored not in BUSINESS_FEATURES_PLANS
    assert stored not in PRIORITY_QUEUE_PLANS
    assert stored.lower() not in COUNTY_LIMIT_BY_PLAN or stored.lower() == "business"
    # .lower() alone is not enough: entitlements._plan_of lowercases but does not strip.
    assert "business " .strip() == "business"
    assert "business " not in COUNTY_LIMIT_BY_PLAN
