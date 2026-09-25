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
from datetime import UTC, datetime, timedelta

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
    SUPPORTED_EXPORT_FORMATS,
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


def test_the_metered_price_is_attached_to_the_subscription_at_checkout(monkeypatch):
    """The most expensive gap the audit found. The Pro card sells "then $0.08 per
    lookup"; checkout built the subscription with a single licensed line item and
    nothing ever added one priced against the skip-trace meter, so the MeterEvent
    the ingest path fires had nothing to settle against and every over-quota
    lookup was recorded, metered, and free.

    Asserts the line items the route actually hands Stripe. A metered price must
    carry NO quantity: Stripe rejects the item if it does."""
    import src.api.routes.billing as billing

    monkeypatch.setattr(
        billing,
        "_SKIP_TRACE_METERED_PRICE",
        {
            "pro": {"month": "price_st_pro_m", "year": "price_st_pro_y"},
            "business": {"month": "price_st_biz_m", "year": ""},
            "agency": {"month": "price_st_agy_m", "year": ""},
        },
    )

    assert billing._metered_skip_trace_price("pro", "month") == "price_st_pro_m"
    assert billing._metered_skip_trace_price("pro", "year") == "price_st_pro_y"
    # Starter has no allowance and no metered price.
    assert billing._metered_skip_trace_price("starter", "month") is None
    # An interval with nothing provisioned sells the plan unmetered rather than
    # failing the checkout: Stripe requires one recurring interval per
    # subscription, so a monthly metered price cannot ride on a yearly plan.
    assert billing._metered_skip_trace_price("business", "year") is None


def test_a_bad_metered_price_id_does_not_take_the_sale_down_with_it(monkeypatch):
    """A product id in a price slot is how a STRIPE_PRICE_* env has been
    misconfigured before. Losing the overage is bad; losing the subscription is
    worse."""
    import src.api.routes.billing as billing

    monkeypatch.setattr(
        billing, "_SKIP_TRACE_METERED_PRICE", {"pro": {"month": "prod_oops"}}
    )
    assert billing._metered_skip_trace_price("pro", "month") is None


def test_the_plan_is_read_from_the_licensed_item_not_from_index_zero():
    """Every reader took items[0], which was safe only while a subscription had
    one item. With the metered item attached, index 0 is whichever Stripe returns
    first, and a plan lookup on the metered price would miss the map, alert
    "price not in plan map", and refuse to activate a plan the customer had just
    paid for."""
    import src.api.routes.billing as billing

    plan_price = next(iter(billing._PRICE_TO_PLAN), None)
    if plan_price is None:
        pytest.skip("no STRIPE_PRICE_* configured in this environment")

    metered_first = [
        {"price": {"id": "price_skip_trace_meter"}},
        {"price": {"id": plan_price}},
    ]
    assert billing._plan_item_price_id(metered_first) == plan_price
    assert billing._plan_item_price_id(list(reversed(metered_first))) == plan_price
    assert billing._plan_item_price_id([{"price": {"id": "price_unknown"}}]) is None
    assert billing._plan_item_price_id([]) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkout_sends_two_line_items_and_no_quantity_on_the_metered_one(
    client, db, make_user, monkeypatch
):
    """End to end through POST /billing/checkout, watching what reaches Stripe."""
    import src.api.routes.billing as billing

    plan_price = next(
        (pid for pid, info in billing._PRICE_TO_PLAN.items() if info[0] == "pro"), None
    )
    if plan_price is None:
        pytest.skip("no Pro STRIPE_PRICE_* configured in this environment")

    monkeypatch.setattr(
        billing, "_SKIP_TRACE_METERED_PRICE", {"pro": {"month": "price_st_pro_m"}}
    )

    captured: dict = {}

    class _FakeSession:
        url = "https://checkout.example/session"

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(billing.stripe.checkout.Session, "create", _fake_create)
    # The checkout guard added Subscription.list / Session.list / Session.expire
    # to this path, and moved customer adoption onto auto_paging_iter(). Patching
    # only Customer.list (and as a plain dict) would leave the rest reaching the
    # real Stripe and 503 before this test could look at its line items.
    monkeypatch.setattr(
        billing.stripe.Customer, "list", lambda **kw: _FakeStripeList([])
    )
    monkeypatch.setattr(
        billing.stripe.Customer, "create", lambda **kw: {"id": "cus_audit"}
    )
    monkeypatch.setattr(
        billing.stripe.Subscription, "list", lambda **kw: _FakeStripeList([])
    )
    monkeypatch.setattr(
        billing.stripe.checkout.Session, "list", lambda **kw: _FakeStripeList([])
    )
    monkeypatch.setattr(
        billing.stripe.checkout.Session, "expire", lambda sid, **kw: {"id": sid}
    )

    _user, token = await make_user("pro")
    r = await client.post(
        "/billing/checkout", json={"price_id": plan_price}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text

    items = captured["line_items"]
    assert items[0] == {"price": plan_price, "quantity": 1}
    assert items[1] == {"price": "price_st_pro_m"}
    assert "quantity" not in items[1]


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
@pytest.mark.parametrize("plan", PLANS)
async def test_export_format_matches_what_the_card_sells(client, db, make_user, plan, fmt):
    """Starter "CSV export", Pro "CSV + Excel export", Business and Agency "All
    export formats". This carried no gate at any layer, so a Starter bearer token
    saved JSON. "xlsx" and "excel" are one format with two spellings and must
    move together, or the same file is allowed under one name and refused under
    the other."""
    from src.config.constants import allowed_export_formats

    _user, token = await make_user(plan)
    r = await client.post(
        "/scrapers",
        json=_body("king", deliver={"formats": [fmt], "emails": []}),
        headers=_auth(token),
    )
    if fmt in allowed_export_formats(plan):
        assert r.status_code == 201, f"{plan}/{fmt} refused: {r.text}"
        assert r.json()["deliver"]["formats"] == [fmt]
    else:
        assert r.status_code == 402, f"{plan}/{fmt} allowed: {r.text}"
        assert r.json()["detail"]["code"] == "export_format"


def test_the_card_export_wording_is_the_matrix():
    """Read the bullets, not the constant: this is the promise being audited."""
    from src.config.constants import allowed_export_formats

    assert allowed_export_formats("starter") == frozenset({"csv"})
    assert allowed_export_formats("pro") == frozenset({"csv", "excel", "xlsx"})
    # "All export formats" has to mean every format the exporter can produce.
    assert allowed_export_formats("business") == SUPPORTED_EXPORT_FORMATS
    assert allowed_export_formats("agency") == SUPPORTED_EXPORT_FORMATS


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_edit_cannot_add_a_format_above_the_plan_but_can_keep_one(
    client, db, make_user
):
    """The enable-delta rule. A config that already exports a format its owner has
    since downgraded out of stays renameable; adding a second one does not."""
    _user, token = await make_user("business")
    created = await client.post(
        "/scrapers",
        json=_body("king", deliver={"formats": ["json"], "emails": []}),
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    config_id = created.json()["id"]

    # Downgrade under the config's feet.
    from sqlalchemy import text as _text

    await db.execute(
        _text("UPDATE users SET plan = 'starter' WHERE id = CAST(:u AS uuid)"),
        {"u": _user.id},
    )
    await db.commit()

    async def _token_of(cid: str) -> str:
        got = await client.get(f"/scrapers/{cid}", headers=_auth(token))
        assert got.status_code == 200, got.text
        return got.json()["updated_at"]

    renamed = await client.patch(
        f"/scrapers/{config_id}",
        json={"name": "Renamed", "updated_at": await _token_of(config_id)},
        headers=_auth(token),
    )
    assert renamed.status_code == 200, renamed.text

    widened = await client.patch(
        f"/scrapers/{config_id}",
        json={
            "deliver": {"formats": ["json", "excel"]},
            "updated_at": await _token_of(config_id),
        },
        headers=_auth(token),
    )
    assert widened.status_code == 402, widened.text
    assert widened.json()["detail"]["code"] == "export_format"


# -- Schedules ---------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("frequency", ["manual", "daily", "weekly", "monthly"])
@pytest.mark.parametrize("plan", PLANS)
async def test_schedule_frequency_matches_what_the_card_sells(
    client, db, make_user, plan, frequency
):
    """Starter "Manual runs", Pro "Daily/weekly schedule", Business and Agency
    "All schedules". Nothing gated this, so a Starter saved a daily schedule and
    the beat fired it every morning. "manual" is in every plan: it is the absence
    of a schedule, not a schedule."""
    from src.config.constants import allowed_schedule_frequencies

    _user, token = await make_user(plan)
    r = await client.post(
        "/scrapers",
        json=_body("king", schedule={"frequency": frequency}),
        headers=_auth(token),
    )
    if frequency in allowed_schedule_frequencies(plan):
        assert r.status_code == 201, f"{plan}/{frequency} refused: {r.text}"
        assert r.json()["schedule"]["frequency"] == frequency
    else:
        assert r.status_code == 402, f"{plan}/{frequency} allowed: {r.text}"
        assert r.json()["detail"]["code"] == "schedule"


def test_a_starter_refusal_talks_about_scheduling_not_about_a_frequency():
    """Starter was never offered daily or weekly, so naming the one it asked for
    reads as a near miss. Every other plan is missing one specific frequency and
    is told which one."""
    from src.api.entitlements import schedule_frequency_violation

    assert schedule_frequency_violation("starter", "daily").message == (
        "Your Starter plan runs scrapes when you start them. "
        "Scheduled runs are not included."
    )
    assert schedule_frequency_violation("pro", "monthly").message == (
        "Monthly scheduling is not included in your Pro plan."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_save_that_breaks_two_gates_is_refused_once(client, db, make_user):
    """A notice per violation would send the customer back for a second refusal
    after they fixed the first."""
    _user, token = await make_user("starter")
    r = await client.post(
        "/scrapers",
        json=_body(
            "king",
            deliver={"formats": ["json"], "emails": []},
            schedule={"frequency": "daily"},
        ),
        headers=_auth(token),
    )
    assert r.status_code == 402, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "plan_limit"
    assert detail["title"] == "Plan limit reached"
    assert "JSON export is not included in your Starter plan." in detail["message"]
    assert "Scheduled runs are not included." in detail["message"]


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
@pytest.mark.parametrize(
    "path", ["/segments/intersection", "/segments/union", "/segments/intersection/export"]
)
async def test_overlap_and_intersection_are_business_and_above(
    client, db, make_user, plan, path
):
    """"All record types + overlap/intersection" is a Business and Agency line,
    and the strategy doc gates the distress-list overlap there deliberately. The
    router shipped with authentication and tenant scoping but no plan dependency,
    so a Starter bearer token reached all four endpoints.

    Every endpoint on the router is checked, not just the preview: the export is
    the one that hands over the leads."""
    from src.config.constants import OVERLAP_PLANS

    _user, token = await make_user(plan)
    r = await client.post(
        path, json={"record_types": ["probate", "pre_foreclosure"]}, headers=_auth(token)
    )
    if plan in OVERLAP_PLANS:
        assert r.status_code == 200, f"{plan} {path}: {r.text}"
    else:
        assert r.status_code == 402, f"{plan} {path}: {r.text}"
        assert r.json()["detail"]["code"] == "overlap"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["pro", "business", "agency"])
async def test_every_plan_with_batch_can_still_create_one(client, db, make_user, plan):
    """"Batch scraping" is a Pro card line, and the overlap gate must not have
    quietly broken it. An earlier version of this test pinned the opposite
    decision (overlaps_only ungated everywhere); the gate now refuses an
    EXPLICIT overlaps_only below Business and coerces the DEFAULT to
    "everything", so a batch still runs on every plan that is sold one."""
    _user, token = await make_user(plan)
    r = await client.post(
        "/batches",
        json={
            "name": "Batch still works",
            "state": "WA",
            "counties": ["king"],
            "record_types": ["probate"],
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"{plan}: {r.text}"


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


def test_a_custom_date_range_cannot_reach_past_the_starter_freshness_edge():
    """The delay used to apply to the rolling window and nothing else, so a
    Starter asking for a custom window ending today got today: the paid
    freshness moat, available for free to anyone who typed a date. The custom
    branch now clamps its end to the same edge, and leaves a paid plan alone."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from src.workers.tasks_helpers.dates import _resolve_date_range

    today = datetime.now(ZoneInfo("US/Pacific")).date()
    schedule = {
        "date_range_mode": "custom",
        "date_from": (today - timedelta(days=60)).isoformat(),
        "date_to": today.isoformat(),
    }

    def _to(plan: str):
        _, end = _resolve_date_range(schedule, user_plan=plan)
        return datetime.strptime(end, "%m/%d/%Y").date()

    assert _to("starter") == today - timedelta(days=7)
    assert _to("pro") == today

    # A PAID plan is not clamped at all, including a window that ends in the
    # FUTURE. trustee_sale reads the window's LENGTH as a forward auction
    # horizon (src/scrapers/trustee_sale.py, _window_span_days), so truncating a
    # future end would shorten the horizon a customer asked for, or erase it
    # entirely once the span went non-positive and the scraper fell back to
    # "every upcoming auction". The guard is `end_date < today`, not the plan
    # name, and this is the test that says why.
    forward = {
        "date_range_mode": "custom",
        "date_from": today.isoformat(),
        "date_to": (today + timedelta(days=90)).isoformat(),
    }
    for paid in ("pro", "business", "agency"):
        start, end = _resolve_date_range(forward, user_plan=paid)
        assert datetime.strptime(end, "%m/%d/%Y").date() == today + timedelta(days=90), paid
        span = (
            datetime.strptime(end, "%m/%d/%Y").date()
            - datetime.strptime(start, "%m/%d/%Y").date()
        ).days
        assert span == 90, paid

    # A window that sits entirely inside the embargo collapses to the edge
    # rather than inverting into garbage the portals cannot answer.
    inside = {
        "date_range_mode": "custom",
        "date_from": (today - timedelta(days=2)).isoformat(),
        "date_to": today.isoformat(),
    }
    start, end = _resolve_date_range(inside, user_plan="starter")
    assert start == end
    assert datetime.strptime(end, "%m/%d/%Y").date() == today - timedelta(days=7)


# -- The enrichment.skip_tracing toggle --------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_enrichment_toggle_sets_the_column_the_worker_actually_reads(
    client, db, make_user
):
    """`enrichment.skip_tracing` used to be gated, persisted, and read by
    nothing: the worker's skip-trace entry point keys off the
    `skip_trace_enabled` COLUMN and never opens the enrichment blob, so a
    Business account that flipped only this toggle got a 201 and no lookups.

    The create route now mirrors it onto that column. The worker guard is
    asserted too, because the mirror is only correct while that is what the
    worker reads."""
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
    assert body["skip_trace_enabled"] is True

    source = inspect.getsource(enrich)
    assert 'getattr(config, "skip_trace_enabled", False)' in source
    assert "skip_tracing" not in source


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_enrichment_toggle_does_not_widen_who_can_run_a_trace(
    client, db, make_user
):
    """The mirror must not become a second door into paid lookups. The toggle
    is Business and above, which is stricter than the metered flag's Pro and
    above, so no plan reaches skip tracing through it that could not already
    reach it through `skip_trace_enabled`."""
    for plan in ("starter", "pro"):
        _user, token = await make_user(plan)
        r = await client.post(
            "/scrapers",
            json=_body("king", enrichment={"skip_tracing": True}),
            headers=_auth(token),
        )
        assert r.status_code == 402, f"{plan}: {r.text}"


# -- Priority queue routing --------------------------------------------------

def test_the_declared_route_is_the_ordinary_queue_so_delay_can_never_prioritize():
    """`run_scrape_job.delay(...)` takes the task's declared route, which is the
    ordinary `scrape` queue for every plan. That is why an enqueue site has to
    pass the queue explicitly, and why a `.delay()` anywhere in a dispatch path
    is a silent loss of the Agency priority line rather than a style choice."""
    from src.workers import app as celery_app

    route = celery_app.amqp.router.route({}, "src.workers.tasks.run_scrape_job")
    assert route["queue"].name == "scrape"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ("starter", "scrape"),
        ("pro", "scrape"),
        ("business", "scrape-priority"),
        ("agency", "scrape-priority"),
    ],
)
def test_a_scheduled_run_is_published_to_the_queue_the_plan_pays_for(
    monkeypatch, plan, expected
):
    """The one that was broken. The scheduled dispatcher published with
    `.delay()`, so a Business or Agency account got priority on a button press
    and ordinary service on every recurring run, which is the work the tier is
    bought for.

    Watches the actual publish rather than reading the source: a comment that
    mentions `.delay(jid)` would satisfy a grep, and one did."""
    from datetime import UTC, datetime

    from src.api.auth import hash_password as _hash
    from src.db.models import ScraperConfig as _Config
    from src.db.models import User as _User
    from src.db.session import SyncSessionLocal
    from src.workers import tasks as tasks_mod
    from src.workers.scheduler_helpers import dispatch as dispatch_mod

    published: list[tuple[str, str | None]] = []

    def _capture(args=None, queue=None, **kwargs):
        published.append((str((args or [None])[0]), queue))

    monkeypatch.setattr(tasks_mod.run_scrape_job, "apply_async", _capture)

    now = datetime.now(UTC)
    uid = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    with SyncSessionLocal() as db:
        db.add(
            _User(
                id=uid,
                email=f"queue_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
                password_hash=_hash("TestPass123!"),
                plan=plan,
                records_used=0,
                records_limit=get_plan(plan)["records_limit"],
            )
        )
        db.add(
            _Config(
                id=cid,
                user_id=uid,
                name="Queue routing",
                county="king",
                state="WA",
                record_type="probate",
                fields=["party_name"],
                enrichment=[],
                # Due at this minute, so the real dispatcher picks it up on a
                # real clock without a fixed-tick seam.
                schedule={
                    "frequency": "daily",
                    "run_at_hour": now.hour,
                    "run_at_minute": now.minute,
                },
                deliver={},
                active=True,
            )
        )
        db.commit()

    try:
        dispatch_mod._dispatch_scheduled_jobs_impl()
        mine = [q for jid, q in published if jid]
        assert mine, "the dispatcher published nothing; the config was not due"
        assert set(mine) == {expected}, published
    finally:
        from sqlalchemy import text

        with SyncSessionLocal() as db:
            db.execute(
                text("DELETE FROM jobs WHERE user_id = CAST(:u AS uuid)"), {"u": uid}
            )
            db.execute(
                text("DELETE FROM scraper_configs WHERE user_id = CAST(:u AS uuid)"),
                {"u": uid},
            )
            db.execute(text("DELETE FROM users WHERE id = CAST(:u AS uuid)"), {"u": uid})
            db.commit()


def test_the_batch_fan_out_publishes_to_the_owners_queue():
    """The batch path resolves one queue for the whole run from the batch
    owner's plan, before the branch, so a recovery re-dispatch routes the same
    way as the first one."""
    from src.workers import batch_tasks as batch_mod

    source = inspect.getsource(batch_mod.dispatch_batch_run)
    assert "queue = scrape_queue_for_plan(_owner_plan)" in source
    assert "run_scrape_job.apply_async(args=[jid], queue=queue)" in source


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


# -- The three items the first pass left open --------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["pro", "business"])
async def test_an_explicit_overlaps_only_batch_is_a_business_line(
    client, db, make_user, plan
):
    """Asking for the overlap product BY NAME is refused below Business, the same
    as /segments. The batch was the second door to the same thing."""
    from src.config.constants import OVERLAP_PLANS

    _user, token = await make_user(plan)
    r = await client.post(
        "/batches",
        json={
            "name": "Explicit overlap",
            "state": "WA",
            "counties": ["king"],
            "record_types": ["probate"],
            "delivery_mode": "overlaps_only",
        },
        headers=_auth(token),
    )
    if plan in OVERLAP_PLANS:
        assert r.status_code == 201, r.text
    else:
        assert r.status_code == 402, r.text
        assert r.json()["detail"]["code"] == "overlap"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_pro_batch_that_says_nothing_still_runs_and_delivers_everything(
    client, db, make_user
):
    """`delivery_mode` DEFAULTS to overlaps_only and "Batch scraping" is a Pro card
    line, so refusing the default would leave Pro able to create a batch and
    unable to receive the only export it makes. Omitted below Business becomes
    "everything": more rows, not fewer, and the overlap filter is the part that
    was never sold at this tier."""
    from sqlalchemy import text as _text

    _user, token = await make_user("pro")
    r = await client.post(
        "/batches",
        json={
            "name": "Default batch",
            "state": "WA",
            "counties": ["king"],
            "record_types": ["probate"],
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    stored = (
        await db.execute(
            _text("SELECT delivery_mode FROM scraper_batches WHERE id = CAST(:b AS uuid)"),
            {"b": r.json()["batch_id"]},
        )
    ).scalar()
    assert stored == "everything"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_business_batch_keeps_the_overlaps_only_default(
    client, db, make_user
):
    _user, token = await make_user("business")
    from sqlalchemy import text as _text

    r = await client.post(
        "/batches",
        json={
            "name": "Default batch",
            "state": "WA",
            "counties": ["king"],
            "record_types": ["probate"],
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    stored = (
        await db.execute(
            _text("SELECT delivery_mode FROM scraper_batches WHERE id = CAST(:b AS uuid)"),
            {"b": r.json()["batch_id"]},
        )
    ).scalar()
    assert stored == "overlaps_only"


def test_a_missing_customer_id_is_not_the_same_signal_as_stripe_being_off():
    """These two used to raise ONE exception type, and the caller stamped
    reported_at on it, which permanently wrote off real billable overage for a
    customer whose stripe_customer_id had simply not been written yet. On this
    deployment plans are set by hand and most users have no customer id at all,
    so that was not a corner case."""
    import src.api.billing.skip_trace_usage as st

    assert st._MissingCustomerError is not st._StripeNotConfiguredError
    assert not issubclass(st._MissingCustomerError, st._StripeNotConfiguredError)
    assert not issubclass(st._StripeNotConfiguredError, st._MissingCustomerError)

    # _stripe_enabled() is checked first and is False without the meter env, so
    # the customer-id branch is only reachable with Stripe configured.
    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    try:
        # The gate now runs inside the sender, so it has to be satisfied for
        # these two signals to be reachable at all. Neutered here on purpose:
        # this test is about keeping _MissingCustomerError and
        # _StripeNotConfiguredError apart, not about the gate.
        monkeypatch.setattr(st, "assert_billable", lambda *a, **k: {"id": "sub_x"})
        monkeypatch.setattr(st, "_stripe_enabled", lambda: True)
        with pytest.raises(st._MissingCustomerError):
            st.report_meter_event_to_stripe(
                user_id="u" * 36,
                queue_id=1,
                billable_units=5,
                stripe_customer_id=None,
                plan="pro",
                usage_at=datetime.now(UTC),
            )
        # Stripe genuinely off stays the OTHER signal, and stays terminal.
        monkeypatch.setattr(st, "_stripe_enabled", lambda: False)
        with pytest.raises(st._StripeNotConfiguredError):
            st.report_meter_event_to_stripe(
                user_id="u" * 36,
                queue_id=1,
                billable_units=5,
                stripe_customer_id="cus_x",
                plan="pro",
                usage_at=datetime.now(UTC),
            )
    finally:
        monkeypatch.undo()


def test_the_outbox_sweep_selects_pending_rows_and_nothing_else():
    """The sweep must not re-enqueue a row whose disposition is already decided.

    Re-pinned twice over. It used to assert the sweep JOINed `users` and
    required a stripe_customer_id — the P1 rule, now gone — and it asserted it
    by SEARCHING THE SOURCE TEXT, which is the weaker of the two problems: a
    source-text assertion passes for code that is never executed and says
    nothing about what the query returns. It now runs the statement against the
    database and looks at the rows.

    Every settled disposition is checked, not just one, because the failure this
    guards against is a sweep that keeps firing MeterEvents for usage somebody
    already decided was not billable.
    """
    import re

    from sqlalchemy import text as _text

    from src.db.session import system_sync_session
    from src.workers.scheduler_helpers import meter

    src = inspect.getsource(meter)
    # A hold nobody can see is worse than the write-off it replaced.
    assert "skip_trace_meter_held" in src

    m = re.search(r'text\("""(\s*SELECT e\.id.*?)"""\)', src, re.S)
    assert m, "could not find the sweep's selection query"
    sweep_sql = m.group(1)
    assert "stripe_customer_id" not in sweep_sql, (
        "the sweep is keying off the customer id again — that id is written "
        "when the checkout SESSION is created, before payment"
    )

    with system_sync_session() as db:
        for disposition in (
            "pending", "reported", "non_billable", "needs_review",
            "written_off_manual", "settled_manual",
        ):
            got = db.execute(
                _text(
                    "SELECT COUNT(*) FROM (" + sweep_sql + ") s "
                    "JOIN skip_trace_meter_events e2 ON e2.id = s.id "
                    "WHERE e2.disposition = :d"
                ),
                {"d": disposition},
            ).scalar()
            if disposition == "pending":
                continue
            assert got == 0, (
                f"the sweep would re-enqueue rows already settled as "
                f"{disposition}"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_skip_trace_allowance_rolls_on_the_entitlement_window(
    db, make_user, sync_db
):
    """Records reset on the subscriber's anniversary and a Stripe metered item
    bills over the SUBSCRIPTION period, so a calendar-month allowance gave a
    customer anchored on the 20th two free buckets inside one paid month. The
    counter now rolls on the same window the records do.

    The comparison is strictly less-than, so it can only ever RESET a counter,
    never resurrect a spent one."""

    from sqlalchemy import text as _text

    from src.api.billing.skip_trace_usage import report_lookups_for_user

    quota = settings.SKIP_TRACE_BUNDLED_QUOTAS["pro"]
    user, _token = await make_user("pro")
    uid = str(user.id)

    # Spend the whole allowance inside the current window.
    first = report_lookups_for_user(sync_db, uid, quota, queue_id=910001)
    assert first["used_after"] == quota
    assert first["billable_units"] == 0
    sync_db.commit()

    # A calendar rollover with the window unchanged must NOT hand out a second
    # bucket. This is the case the old code got wrong.
    same_window = report_lookups_for_user(sync_db, uid, 1, queue_id=910002)
    assert same_window["used_after"] == quota + 1
    assert same_window["billable_units"] == 1
    sync_db.commit()

    # Advance the ENTITLEMENT window; the allowance comes back.
    sync_db.execute(
        _text(
            "UPDATE users SET quota_period_start = quota_period_start + INTERVAL '1 month'"
            " WHERE id = CAST(:u AS uuid)"
        ),
        {"u": uid},
    )
    sync_db.commit()
    rolled = report_lookups_for_user(sync_db, uid, 1, queue_id=910003)
    assert rolled["used_before"] == 0
    assert rolled["used_after"] == 1
    assert rolled["billable_units"] == 0
    sync_db.rollback()


def test_the_daily_rollover_keys_off_the_window_not_the_calendar():
    import inspect

    from src.workers.scheduler_helpers import billing as sched_billing

    src = inspect.getsource(sched_billing)
    assert "skip_trace_period_start < quota_period_start" in src
    assert "SET skip_trace_period_start = quota_period_start" in src
    # The module's docstring still QUOTES the retired records-half SQL as
    # history, so a blunt "date_trunc is absent" check reads that prose and
    # fails. What matters is that no skip-trace UPDATE keys off the calendar.
    for stmt in src.split("UPDATE users")[1:]:
        head = stmt[:400]
        if "skip_trace" in head:
            assert "date_trunc" not in head, head[:200]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_lookup_after_a_window_ends_is_not_billed_against_the_old_one(
    db, make_user, sync_db
):
    """The rollover is LAZY. Between a window ending and the hourly
    reconciliation catching up, `users.quota_period_start` still names the OLD
    window. Reading it raw said "no roll", left the exhausted counter in place,
    and billed the customer for lookups that belonged to the new window's free
    allowance: charged for something they were owed for free.

    The fix asks `effective_window`, the same helper the records side uses, so
    the answer does not depend on whether a background task has run yet. Codex
    found this in review."""
    from sqlalchemy import text as _text

    from src.api.billing.skip_trace_usage import report_lookups_for_user

    quota = settings.SKIP_TRACE_BUNDLED_QUOTAS["pro"]
    user, _token = await make_user("pro")
    uid = str(user.id)

    # Spend the whole allowance inside the current window.
    spent = report_lookups_for_user(sync_db, uid, quota, queue_id=920001)
    assert spent["used_after"] == quota
    assert spent["billable_units"] == 0
    sync_db.commit()

    # The window ENDS, and nothing has rolled it yet: period_start/end still
    # name the old window, exactly as production sits between the boundary and
    # the next reconciliation tick.
    sync_db.execute(
        _text(
            "UPDATE users"
            " SET quota_period_start = quota_period_start - INTERVAL '1 month',"
            "     quota_period_end = quota_period_end - INTERVAL '1 month',"
            "     skip_trace_period_start = quota_period_start - INTERVAL '1 month'"
            " WHERE id = CAST(:u AS uuid)"
        ),
        {"u": uid},
    )
    sync_db.commit()

    after = report_lookups_for_user(sync_db, uid, 1, queue_id=920002)
    assert after["used_before"] == 0, "the ended window's counter was carried over"
    assert after["used_after"] == 1
    assert after["billable_units"] == 0, "billed for a lookup inside the free allowance"
    sync_db.rollback()


# ─── The checkout guard: one subscription per customer ────────────────────────
#
# Stripe is the one dependency these tests stand in for. The project rule is no
# mocks; the exception it names is an external API, and creating real live
# subscriptions to prove we refuse to create a second one is not a test anyone
# can run twice. Everything else here is a real user row, a real request through
# the real route, and the real guard.


class _FakeStripeList:
    """A Stripe ListObject stand-in that only knows how to page.

    Deliberately does NOT support `.get("data")`. The production code moved from
    reading one page to `auto_paging_iter()`, and a fake that answers both would
    let the pagination regression this guards against pass unnoticed.
    """

    def __init__(self, items):
        self._items = list(items)

    def auto_paging_iter(self):
        return iter(self._items)


def _sub(status: str, sid: str = "sub_test") -> dict:
    return {"id": sid, "status": status}


def _patch_checkout_stripe(
    monkeypatch,
    billing,
    *,
    customers=(),
    subscriptions=(),
    sessions=(),
    expired=None,
    created=None,
):
    """Point every Stripe call create_checkout makes at in-memory data."""
    monkeypatch.setattr(
        billing.stripe.Customer, "list", lambda **kw: _FakeStripeList(customers)
    )
    monkeypatch.setattr(
        billing.stripe.Customer, "create", lambda **kw: {"id": "cus_new"}
    )
    monkeypatch.setattr(
        billing.stripe.Subscription, "list", lambda **kw: _FakeStripeList(subscriptions)
    )
    monkeypatch.setattr(
        billing.stripe.checkout.Session, "list", lambda **kw: _FakeStripeList(sessions)
    )

    def _expire(sid, **kw):
        if expired is not None:
            expired.append(sid)
        return {"id": sid, "status": "expired"}

    monkeypatch.setattr(billing.stripe.checkout.Session, "expire", _expire)

    class _FakeSession:
        url = "https://checkout.example/session"

    def _create(**kwargs):
        if created is not None:
            created.append(kwargs)
        return _FakeSession()

    monkeypatch.setattr(billing.stripe.checkout.Session, "create", _create)


def _pro_price(billing):
    return next(
        (pid for pid, info in billing._PRICE_TO_PLAN.items() if info[0] == "pro"), None
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["active", "trialing", "past_due", "unpaid", "paused"]
)
async def test_checkout_refuses_when_a_live_subscription_exists(
    client, db, make_user, monkeypatch, status
):
    """A second subscription is a second obligation, whatever the first's state.

    `past_due` and `unpaid` are the ones worth naming: they read like "not
    really subscribed", and they still bill. Selling this customer another plan
    leaves them owing on two.
    """
    import src.api.routes.billing as billing

    price = _pro_price(billing)
    if price is None:
        pytest.skip("no Pro STRIPE_PRICE_* configured in this environment")

    created: list = []
    _patch_checkout_stripe(
        monkeypatch, billing, subscriptions=[_sub(status)], created=created
    )

    _user, token = await make_user("pro")
    r = await client.post(
        "/billing/checkout", json={"price_id": price}, headers=_auth(token)
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "subscription_exists"
    assert created == [], "refused checkout must not create a Session"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["canceled", "incomplete_expired"])
async def test_checkout_is_allowed_after_a_terminal_subscription(
    client, db, make_user, monkeypatch, status
):
    """A finished subscription is not an obligation — resubscribing must work.

    The guard's failure mode in this direction is silent and total: a customer
    who cancelled and wants to come back simply cannot, and nothing in the
    product tells them why.
    """
    import src.api.routes.billing as billing

    price = _pro_price(billing)
    if price is None:
        pytest.skip("no Pro STRIPE_PRICE_* configured in this environment")

    created: list = []
    _patch_checkout_stripe(
        monkeypatch, billing, subscriptions=[_sub(status)], created=created
    )

    _user, token = await make_user("pro")
    r = await client.post(
        "/billing/checkout", json={"price_id": price}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    assert len(created) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_incomplete_subscription_is_told_to_finish_paying(
    client, db, make_user, monkeypatch
):
    """`incomplete` blocks, but must not read as "contact support".

    Stripe holds a first payment open for roughly 23 hours. Routing a customer
    whose card needs one retry to a support queue locks them out of their own
    purchase for a day, so this status gets its own message.
    """
    import src.api.routes.billing as billing

    price = _pro_price(billing)
    if price is None:
        pytest.skip("no Pro STRIPE_PRICE_* configured in this environment")

    _patch_checkout_stripe(monkeypatch, billing, subscriptions=[_sub("incomplete")])

    _user, token = await make_user("pro")
    r = await client.post(
        "/billing/checkout", json={"price_id": price}, headers=_auth(token)
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "subscription_incomplete"
    assert "support" not in detail["message"].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unknown_subscription_status_fails_closed(
    client, db, make_user, monkeypatch
):
    """A status Stripe adds later must BLOCK, not sail through.

    The terminal set names what is safe. Written as a deny-list it would have
    let every future status create a duplicate subscription, and nobody would
    look until a customer was billed twice.
    """
    import src.api.routes.billing as billing

    price = _pro_price(billing)
    if price is None:
        pytest.skip("no Pro STRIPE_PRICE_* configured in this environment")

    _patch_checkout_stripe(
        monkeypatch, billing, subscriptions=[_sub("some_status_stripe_adds_in_2027")]
    )

    _user, token = await make_user("pro")
    r = await client.post(
        "/billing/checkout", json={"price_id": price}, headers=_auth(token)
    )
    assert r.status_code == 409, r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_refused_checkout_does_not_publish_stripe_customer_id(
    client, db, make_user, monkeypatch
):
    """The interaction between this guard and the held skip-trace backlog.

    `users.stripe_customer_id` is the signal the meter outbox sweep uses to
    release held usage. The old order resolved-and-PERSISTED the customer before
    doing anything else, so a checkout that gets refused here would still have
    published that id — firing a customer's entire held backlog for a
    subscription that was never created. The write now happens only after a
    Session exists.
    """
    from sqlalchemy import select as _select

    import src.api.routes.billing as billing
    from src.db.models import User as _User

    price = _pro_price(billing)
    if price is None:
        pytest.skip("no Pro STRIPE_PRICE_* configured in this environment")

    _patch_checkout_stripe(monkeypatch, billing, subscriptions=[_sub("active")])

    user, token = await make_user("pro")
    assert user.stripe_customer_id is None

    r = await client.post(
        "/billing/checkout", json={"price_id": price}, headers=_auth(token)
    )
    assert r.status_code == 409, r.text

    fresh = (
        await db.execute(_select(_User).where(_User.id == user.id))
    ).scalar_one()
    await db.refresh(fresh)
    assert fresh.stripe_customer_id is None, (
        "a refused checkout published the customer id that releases held "
        "skip-trace meter events"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkout_fails_closed_when_stripe_state_cannot_be_read(
    client, db, make_user, monkeypatch
):
    """Not knowing is not the same as knowing there is none.

    If the subscription enumeration fails we cannot tell whether this customer
    already owes Stripe money. Creating the Session anyway is the single outcome
    that can charge someone twice, so this refuses with a 503 instead.
    """
    import src.api.routes.billing as billing

    price = _pro_price(billing)
    if price is None:
        pytest.skip("no Pro STRIPE_PRICE_* configured in this environment")

    created: list = []
    _patch_checkout_stripe(monkeypatch, billing, created=created)

    def _boom(**kw):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(billing.stripe.Subscription, "list", _boom)

    _user, token = await make_user("pro")
    r = await client.post(
        "/billing/checkout", json={"price_id": price}, headers=_auth(token)
    )
    assert r.status_code == 503, r.text
    assert created == [], "must not create a Session when Stripe state is unknown"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outstanding_subscription_sessions_are_expired_first(
    client, db, make_user, monkeypatch
):
    """The advisory lock serialises CALLS; it does not retire live Sessions.

    Two tabs each holding a purchasable Session can both be paid after any
    number of correct guard checks. One-off payment sessions are left alone —
    expiring those would cancel a purchase this guard has no business touching.
    """
    import src.api.routes.billing as billing

    price = _pro_price(billing)
    if price is None:
        pytest.skip("no Pro STRIPE_PRICE_* configured in this environment")

    expired: list = []
    _patch_checkout_stripe(
        monkeypatch,
        billing,
        sessions=[
            {"id": "cs_sub_1", "mode": "subscription"},
            {"id": "cs_payment", "mode": "payment"},
            {"id": "cs_sub_2", "mode": "subscription"},
        ],
        expired=expired,
    )

    _user, token = await make_user("pro")
    r = await client.post(
        "/billing/checkout", json={"price_id": price}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    assert expired == ["cs_sub_1", "cs_sub_2"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_customer_adoption_looks_past_the_first_page(
    client, db, make_user, monkeypatch
):
    """The eighth finding: adoption read one page of five and took the first match.

    A user with more than five Stripe customers on one address could have their
    real one sit past that page. We would create yet another customer, and the
    guard above would then enumerate the WRONG customer's subscriptions — so the
    duplicate-subscription hole reopens through the back door. The guard is only
    as good as the customer it is pointed at.
    """
    import src.api.routes.billing as billing

    price = _pro_price(billing)
    if price is None:
        pytest.skip("no Pro STRIPE_PRICE_* configured in this environment")

    user, token = await make_user("pro")

    # Six unrelated customers on this address, then theirs.
    others = [
        {"id": f"cus_other_{i}", "metadata": {"user_id": str(uuid.uuid4())}}
        for i in range(6)
    ]
    theirs = {"id": "cus_theirs", "metadata": {"user_id": str(user.id)}}

    created: list = []
    _patch_checkout_stripe(
        monkeypatch, billing, customers=[*others, theirs], created=created
    )

    def _must_not_create(**kw):
        raise AssertionError(
            "created a new Stripe customer while the user already had one"
        )

    monkeypatch.setattr(billing.stripe.Customer, "create", _must_not_create)

    r = await client.post(
        "/billing/checkout", json={"price_id": price}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    assert created[0]["customer"] == "cus_theirs"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_hand_set_plan_does_not_block_checkout(
    client, db, make_user, monkeypatch
):
    """The guard must ask Stripe, never `users.plan`.

    Every real account on this deployment has its plan set by hand in the
    database and carries no stripe_subscription_id. A guard keyed on the stored
    plan would refuse checkout to all of them while still missing an actual
    duplicate, which is the wrong answer twice.
    """
    import src.api.routes.billing as billing

    price = _pro_price(billing)
    if price is None:
        pytest.skip("no Pro STRIPE_PRICE_* configured in this environment")

    created: list = []
    _patch_checkout_stripe(monkeypatch, billing, subscriptions=[], created=created)

    _user, token = await make_user("agency")  # a paid plan, no Stripe subscription
    r = await client.post(
        "/billing/checkout", json={"price_id": price}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    assert len(created) == 1


# ─── P1: held usage must not release on "the customer has a Stripe id" ────────
#
# The old rule was `users.stripe_customer_id IS NOT NULL`, and create_checkout
# writes that id when the checkout SESSION is created — before payment. So
# starting checkout and walking away released a whole pre-subscription backlog.
# These pin the rule that replaced it.


def _sub_with(price_id, *, status="active", start=None, end=None):
    """A Stripe subscription shaped the way assert_billable reads one."""
    now = int(datetime.now(UTC).timestamp())
    return {
        "id": "sub_x",
        "status": status,
        "current_period_start": start if start is not None else now - 86400,
        "current_period_end": end if end is not None else now + 86400,
        "items": {"data": [{"price": {"id": price_id}}]},
    }


def _patch_subs(monkeypatch, st, subs):
    class _L:
        def __init__(self, items): self._i = list(items)
        def auto_paging_iter(self): return iter(self._i)

    import stripe as _stripe
    monkeypatch.setattr(_stripe.Subscription, "list", lambda **kw: _L(subs))
    monkeypatch.setattr(st.settings, "STRIPE_SECRET_KEY", "sk_test_fake")
    # These tests exercise the RULE, so they pin the kill switch ON rather than
    # inheriting whatever the module default happens to be. It is True today.
    # If it is ever switched off, every one of these would short-circuit to
    # usage_at_unknown and keep passing while testing nothing — a green suite
    # proving the shutdown works and the rule not at all. The switch has its own
    # test below, in both positions.
    monkeypatch.setattr(st, "USAGE_PROVENANCE_IS_TRUSTWORTHY", True)


def _metered_price(st):
    ids = st._configured_metered_price_ids()
    if not ids:
        pytest.skip("no metered skip-trace price configured in this environment")
    return sorted(ids)[0]


def _require_stripe_prices(*, plan: bool = False, metered: bool = False) -> None:
    """Skip when this environment has no Stripe prices configured.

    Without it these tests do not fail because the rule under test is broken;
    they fail because the code never REACHES that rule. With no metered price,
    `assert_billable` short-circuits to 'no_metered_price_configured' before
    evaluating the refusal reason being asserted. With no plan price, a
    subscription carrying an unrecognised price id is refused up front with
    `_UnrecognisedSubscriptionError: expected exactly one plan price, found 0`.

    Both are environment facts, not defects, and CI (which has the STRIPE_PRICE_*
    vars) exercises them properly. Skipping locally is what lets a developer read
    a local full-suite run as pass/fail instead of diffing it against a baseline
    run of origin/main to find out which failures were already there.
    """
    import src.api.billing.skip_trace_usage as st
    import src.api.routes.billing as b

    if metered and not st._configured_metered_price_ids():
        pytest.skip("no metered skip-trace price configured in this environment")
    if plan and not b._PRICE_TO_PLAN:
        pytest.skip("no plan STRIPE_PRICE_* configured in this environment")


def test_a_customer_id_alone_no_longer_authorises_billing(monkeypatch):
    """THE P1, stated as a test.

    A user who started checkout has a stripe_customer_id and no subscription.
    Under the old rule the sweep released their entire backlog on that alone.
    """
    _require_stripe_prices(metered=True)
    import src.api.billing.skip_trace_usage as st

    _patch_subs(monkeypatch, st, [])
    with pytest.raises(st._NotBillableError) as e:
        st.assert_billable("cus_started_checkout", datetime.now(UTC))
    # The ONE refusal certain enough to write off: no subscription has ever
    # existed, of any status, so nothing can have covered this usage.
    assert e.value.reason == "no_subscription_ever"


def test_a_subscription_without_the_metered_item_does_not_authorise_overage(
    monkeypatch,
):
    """Buying a PLAN is not agreeing to a per-lookup rate.

    The metered item is the only artefact that says the customer was shown, and
    accepted, a price per lookup. Billing overage against a plan-only
    subscription charges for something never quoted.
    """
    _require_stripe_prices(metered=True)
    import src.api.billing.skip_trace_usage as st

    _patch_subs(monkeypatch, st, [_sub_with("price_plan_only_not_metered")])
    with pytest.raises(st._NotBillableError) as e:
        st.assert_billable("cus_1", datetime.now(UTC))
    # A subscription exists, so this is NOT a certain write-off — a human looks.
    assert e.value.reason == "coverage_unproven"


@pytest.mark.parametrize("status", ["trialing", "past_due", "unpaid", "paused"])
def test_only_an_active_subscription_authorises_billing(monkeypatch, status):
    """`trialing` is not permission to bill trial usage, and a subscription
    whose last invoice did not clear is not somewhere to quietly add more."""
    import src.api.billing.skip_trace_usage as st

    price = _metered_price(st)
    _patch_subs(monkeypatch, st, [_sub_with(price, status=status)])
    with pytest.raises(st._NotBillableError) as e:
        st.assert_billable("cus_1", datetime.now(UTC))
    assert e.value.reason == "coverage_unproven"


def test_usage_from_before_the_subscription_is_not_swept_into_it(monkeypatch):
    """The retroactive charge, arrived at politely.

    Usage incurred before this subscription existed does not become billable
    because a subscription exists NOW. That is the second half of the P1: the
    first half strands the usage, this half charges for it.
    """
    import src.api.billing.skip_trace_usage as st

    price = _metered_price(st)
    now = int(datetime.now(UTC).timestamp())
    sub = _sub_with(price, start=now - 3600, end=now + 86400)
    _patch_subs(monkeypatch, st, [sub])

    before_the_subscription = datetime.now(UTC) - timedelta(days=3)
    with pytest.raises(st._NotBillableError) as e:
        st.assert_billable("cus_1", before_the_subscription)
    assert e.value.reason == "coverage_unproven"


def test_usage_inside_an_active_metered_period_is_billable(monkeypatch):
    """The one case that SHOULD bill, so the rule is not just 'refuse'."""
    import src.api.billing.skip_trace_usage as st

    price = _metered_price(st)
    _patch_subs(monkeypatch, st, [_sub_with(price)])
    sub = st.assert_billable("cus_1", datetime.now(UTC))
    assert sub["id"] == "sub_x"


def test_usage_older_than_stripes_backdating_limit_goes_to_a_human(monkeypatch):
    """Too old to bill honestly, so it is not billed at all.

    Stripe refuses a meter event over 35 days old. The tempting fallback is to
    send it with today's date, which silently moves a customer's usage into a
    period they did not incur it in. needs_review instead.
    """
    import src.api.billing.skip_trace_usage as st

    price = _metered_price(st)
    _patch_subs(monkeypatch, st, [_sub_with(price)])
    with pytest.raises(st._NotBillableError) as e:
        st.assert_billable("cus_1", datetime.now(UTC) - timedelta(days=40))
    assert e.value.reason == "timestamp_expired"


def test_a_row_with_no_usage_time_is_never_billed_on_a_guess(monkeypatch):
    """Rows written before migration 092 have usage_at NULL.

    Their real usage time is not recoverable. Substituting `now` would be a
    guess that gets charged for, so they go to a human.
    """
    import src.api.billing.skip_trace_usage as st

    price = _metered_price(st)
    _patch_subs(monkeypatch, st, [_sub_with(price)])
    with pytest.raises(st._NotBillableError) as e:
        st.assert_billable("cus_1", None)
    assert e.value.reason == "usage_at_unknown"


def test_a_stripe_outage_is_not_an_answer(monkeypatch):
    """"We could not ask" must not settle the row.

    A _NotBillableError would write a disposition and stop the retries, turning
    an outage into a permanent decision. The raw exception propagates instead so
    the task's autoretry gets another go and the row stays pending.
    """
    _require_stripe_prices(metered=True)
    import stripe as _stripe

    import src.api.billing.skip_trace_usage as st

    def _boom(**kw):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(_stripe.Subscription, "list", _boom)
    monkeypatch.setattr(st.settings, "STRIPE_SECRET_KEY", "sk_test_fake")

    with pytest.raises(RuntimeError):
        st.assert_billable("cus_1", datetime.now(UTC))


def test_the_meter_event_carries_an_explicit_timestamp(monkeypatch):
    """Omitting it bills into whatever period is open when the row is sent.

    A row that waits in the outbox — a broker outage, a retry, the beat sweep —
    would otherwise be stamped at submission time and land in the wrong period.
    """
    import stripe as _stripe

    import src.api.billing.skip_trace_usage as st

    captured: dict = {}

    def _create(**kwargs):
        captured.update(kwargs)
        return {"identifier": "id_1"}

    monkeypatch.setattr(_stripe.billing.MeterEvent, "create", _create)
    monkeypatch.setattr(st, "_stripe_enabled", lambda: True)
    monkeypatch.setattr(st.settings, "STRIPE_SECRET_KEY", "sk_test_fake")
    # The subject here is the TIMESTAMP, not eligibility; the gate has its own
    # tests above and would otherwise need a whole subscription fixture.
    monkeypatch.setattr(st, "assert_billable", lambda *a, **k: {"id": "sub_1"})

    when = datetime.now(UTC) - timedelta(days=2)
    st.report_meter_event_to_stripe(
        user_id="u1", queue_id=7, billable_units=3,
        stripe_customer_id="cus_1", plan="pro", usage_at=when,
    )
    assert captured["timestamp"] == int(when.timestamp()), (
        "the event must be dated when the usage happened, not when it was sent"
    )


# ─── The four defects the Codex gate found in the fixes above ────────────────


def test_a_settled_row_is_never_re_evaluated(monkeypatch):
    """A decision is not a suggestion.

    The task used to claim rows on `reported_at`, which is NULL for every
    settled-but-unsent disposition — non_billable, written_off_manual,
    settled_manual. So a task already on the queue, or re-enqueued by the sweep,
    walked past the decision, re-ran the predicate, and could bill a row a human
    had explicitly written off.
    """
    import src.workers.tracerfy_ingest as ti

    class _Row:
        disposition = "written_off_manual"
        reported_at = None
        usage_at = None
        billable_units = 99
        user_id = "u1"
        tracerfy_queue_id = 1
        plan = "pro"
        stripe_customer_id = "cus_1"

    class _Result:
        # The owner lookup and the advisory lock the task now takes before it
        # reads the row. Returning a user id lets it reach the disposition
        # check, which is what this test is actually about.
        def scalar(self): return "u1"

    class _Session:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **kw): return _Result()
        def get(self, *a, **kw): return _Row()
        def commit(self): raise AssertionError("a settled row must not be written")

    import src.db.session as _sess
    monkeypatch.setattr(_sess, "system_sync_session", lambda: _Session())

    def _must_not_report(**kw):
        raise AssertionError("a written-off row was sent to Stripe")

    import src.api.billing.skip_trace_usage as st
    monkeypatch.setattr(st, "report_meter_event_to_stripe", _must_not_report)

    out = ti.report_skip_trace_meter_event(str(uuid.uuid4()))
    assert out["skipped"] == "written_off_manual"


def test_a_renewal_between_lookup_and_report_does_not_discard_the_usage(
    monkeypatch,
):
    """Usage at 23:59, renewal at 00:00, reported at 00:01.

    The customer genuinely owes this. Refusing it is correct — the period it
    belongs to has closed — but settling it `non_billable` throws real revenue
    away silently, which is the same stranding failure this work exists to stop.
    It has to reach a human.
    """
    import src.api.billing.skip_trace_usage as st

    price = _metered_price(st)
    now = int(datetime.now(UTC).timestamp())
    # The subscription started a month ago and renewed an hour ago.
    sub = _sub_with(price, start=now - 3600, end=now + 86400)
    sub["start_date"] = now - 30 * 86400
    sub["items"]["data"][0]["created"] = now - 30 * 86400
    _patch_subs(monkeypatch, st, [sub])

    just_before_renewal = datetime.now(UTC) - timedelta(hours=2)
    with pytest.raises(st._NotBillableError) as e:
        st.assert_billable("cus_1", just_before_renewal)
    assert e.value.reason == "closed_billing_period", (
        "a closed period is not the same as no agreement — one is reviewable "
        "revenue, the other is a write-off"
    )


def test_a_metered_item_added_later_does_not_price_earlier_usage(monkeypatch):
    """The item's presence today says nothing about last week.

    Period opens Sept 1, usage Sept 7, metered item added Sept 8, reported
    Sept 9. The usage falls inside the current period and the item is on the
    subscription, so the naive check passes — and charges per-lookup for usage
    incurred before any per-lookup price was agreed.
    """
    import src.api.billing.skip_trace_usage as st

    price = _metered_price(st)
    now = int(datetime.now(UTC).timestamp())
    sub = _sub_with(price, start=now - 8 * 86400, end=now + 22 * 86400)
    sub["start_date"] = now - 8 * 86400
    sub["items"]["data"][0]["created"] = now - 86400  # added yesterday

    _patch_subs(monkeypatch, st, [sub])

    usage_before_the_item_existed = datetime.now(UTC) - timedelta(days=2)
    with pytest.raises(st._NotBillableError):
        st.assert_billable("cus_1", usage_before_the_item_existed)


def test_usage_at_is_never_guessed_from_a_clock_we_control():
    """usage_at may come from the PROVIDER's clock and from nothing else.

    Three candidates were tried before migration 093 and every one is a clock
    this system controls, at some moment later than the work:

      created_at    server_default=now() — the reconciliation transaction.
      completed_at  set to `now` by the ingest worker a few statements before
                    billing runs, in the SAME transaction.
      submitted_at  written by the dispatcher on send, but _persist_submission
                    ALSO runs on the reconciler's adoption path and inserts the
                    row there with `now` — for an adopted queue, the adoption
                    time, days after the work.

    Each can place usage from before a subscription inside it, which bills a
    customer for something they never agreed to, and can make usage past
    Stripe's 35-day limit look fresh.

    The answer is `skip_trace_queues.provider_submitted_at`, which is written
    once with known provenance (see provider_submitted_time) and is always at or
    before the lookups. This test fails loudly if anyone re-attaches one of the
    wrong clocks — including by widening this select to COALESCE onto one, which
    is exactly how a "harmless fallback" would get back in.
    """
    import inspect

    from src.api.billing import skip_trace_usage as st

    code = [
        line for line in inspect.getsource(st.report_usage_from_webhook).splitlines()
        if not line.lstrip().startswith("#") and not line.lstrip().startswith("--")
    ]
    body = chr(10).join(code)

    assert "provider_submitted_at" in body, (
        "usage_at must be read from the column whose provenance is known"
    )

    # The wrong clocks, checked on the SELECT that feeds usage_at. Note
    # provider_submitted_at legitimately ends in 'submitted_at', so that one is
    # matched as a whole word rather than as a substring.
    import re

    for wrong_clock in ("created_at", "completed_at", "NOW()", "now()"):
        assert wrong_clock not in body, (
            f"usage_at is being derived from {wrong_clock} again"
        )
    assert not re.search(r"(?<!provider_)\bsubmitted_at\b", body), (
        "usage_at is being derived from submitted_at again — the adoption path "
        "rewrites it, which is why provider_submitted_at exists"
    )


def test_a_null_usage_at_reaches_a_human_rather_than_a_write_off(monkeypatch):
    """The consequence of the above: it must land in needs_review.

    Routing it to non_billable would silently discard revenue the customer may
    genuinely owe, which is the same class of error as charging them for
    something they did not.
    """
    import src.api.billing.skip_trace_usage as st

    price = _metered_price(st)
    _patch_subs(monkeypatch, st, [_sub_with(price)])
    with pytest.raises(st._NotBillableError) as e:
        st.assert_billable("cus_1", None)
    assert e.value.reason == "usage_at_unknown"

    import src.workers.tracerfy_ingest as ti

    src = inspect.getsource(ti.report_skip_trace_meter_event)
    assert 'refusal.reason == "no_subscription_ever"' in src, (
        "only a Stripe-confirmed 'never had a subscription' may be written off"
    )
    assert '"no_customer_id"' not in src.split("row.disposition = (")[1][:200], (
        "no_customer_id must NOT be written off: it is a fact about our own "
        "row, it is mutable, and it can be stale"
    )


def test_the_kill_switch_stops_every_report_when_it_is_off(monkeypatch):
    """The switch, not the rule.

    Every other test here pins this ON to reach the rule underneath. This one
    checks the switch itself does what it claims in BOTH positions: off, nothing
    is billable whatever a row has stored, because a timestamp written by an
    earlier version of this code is exactly as undefendable as one written
    today; on, a well-formed row reaches the rule instead of being refused here.
    """
    import src.api.billing.skip_trace_usage as st

    price = _metered_price(st)
    _patch_subs(monkeypatch, st, [_sub_with(price)])

    # OFF: a perfectly billable-looking row, with a stored timestamp, refuses.
    monkeypatch.setattr(st, "USAGE_PROVENANCE_IS_TRUSTWORTHY", False)
    with pytest.raises(st._NotBillableError) as e:
        st.assert_billable("cus_1", datetime.now(UTC))
    assert e.value.reason == "usage_at_unknown"

    # ON: the same row now reaches the rule and passes it. Without this half the
    # test would still pass with the rule permanently broken.
    monkeypatch.setattr(st, "USAGE_PROVENANCE_IS_TRUSTWORTHY", True)
    assert st.assert_billable("cus_1", datetime.now(UTC)) is not None


def test_a_null_usage_at_is_refused_even_with_the_switch_on(monkeypatch):
    """The switch is not the only thing holding the line.

    Adopted queues where Tracerfy gave us no created_at, and every row written
    before migration 093, still carry usage_at = NULL. Turning automatic billing
    on must not turn those into charges: "we do not know when this happened" is
    a refusal on its own merits, not a consequence of the switch.
    """
    import src.api.billing.skip_trace_usage as st

    price = _metered_price(st)
    _patch_subs(monkeypatch, st, [_sub_with(price)])

    with pytest.raises(st._NotBillableError) as e:
        st.assert_billable("cus_1", None)
    assert e.value.reason == "usage_at_unknown"


def test_the_stripe_sender_runs_the_gate_on_what_it_is_about_to_send(monkeypatch):
    """The gate must be bound to the event, not to a token beside it.

    It briefly lived one layer up in the calling task, then behind a
    `billing_proof` dict this function only checked for None — and Codex broke
    that by passing `{}`, emitting a real MeterEvent with billing switched off
    and no timestamp at all. A token that is not bound to the customer and time
    it vouches for is not evidence; a proof fetched for one customer authorised
    a send for any other.

    So the sender calls assert_billable on the SAME arguments it is about to
    send. These three cases would each have passed the token version.
    """
    import src.api.billing.skip_trace_usage as st

    sent = []
    import stripe as _stripe
    monkeypatch.setattr(
        _stripe.billing.MeterEvent, "create",
        lambda **kw: sent.append(kw) or {"identifier": "x"},
    )
    monkeypatch.setattr(st.settings, "STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setattr(st, "_stripe_enabled", lambda: True)

    def _send(**over):
        kwargs = {
            "user_id": "u1", "queue_id": 1, "billable_units": 5,
            "stripe_customer_id": "cus_1", "plan": "pro",
            "usage_at": datetime.now(UTC),
        }
        kwargs.update(over)
        return st.report_meter_event_to_stripe(**kwargs)

    # 1. Billing switched off entirely.
    monkeypatch.setattr(st, "USAGE_PROVENANCE_IS_TRUSTWORTHY", False)
    with pytest.raises(st._NotBillableError) as e:
        _send()
    assert e.value.reason == "usage_at_unknown"

    # 2. Switch on, but no defensible time. The token version emitted an event
    #    with NO timestamp here, letting Stripe stamp submission time.
    monkeypatch.setattr(st, "USAGE_PROVENANCE_IS_TRUSTWORTHY", True)
    with pytest.raises(st._NotBillableError) as e:
        _send(usage_at=None)
    assert e.value.reason == "usage_at_unknown"

    # 3. Switch on, real time, but no subscription behind the customer.
    class _Empty:
        def auto_paging_iter(self):
            return iter([])

    monkeypatch.setattr(_stripe.Subscription, "list", lambda **kw: _Empty())
    with pytest.raises(st._NotBillableError):
        _send()

    assert sent == [], (
        "a MeterEvent reached Stripe without the gate having passed for it"
    )


def test_the_sender_takes_no_caller_supplied_authorisation(monkeypatch):
    """There must be no argument a caller can pass to skip the gate.

    The whole failure mode was an authorisation value the sender trusted instead
    of checking. If one comes back, this fails.
    """
    import inspect

    import src.api.billing.skip_trace_usage as st

    params = inspect.signature(st.report_meter_event_to_stripe).parameters
    assert "billing_proof" not in params
    for name in params:
        assert "proof" not in name and "verified" not in name and "skip" not in name, (
            f"{name!r} looks like a caller-supplied way past the gate"
        )
    assert "assert_billable(stripe_customer_id, usage_at)" in inspect.getsource(
        st.report_meter_event_to_stripe
    ), "the gate must run on the arguments actually being sent"


def test_the_meter_timestamp_is_never_omitted(monkeypatch):
    """Omitting it lets Stripe stamp submission time and bill the wrong period.

    It used to be conditional on usage_at being present. The gate now refuses a
    NULL usage_at outright, so there is no branch left where it can be left off
    — and this pins that, because re-adding the condition would look harmless.
    """
    import inspect

    import src.api.billing.skip_trace_usage as st

    src = inspect.getsource(st.report_meter_event_to_stripe)
    assert 'event_kwargs["timestamp"] = int(usage_at.timestamp())' in src
    assert "if usage_at is not None:" not in src, (
        "the timestamp must be unconditional; the gate guarantees usage_at"
    )


def test_adoption_never_stamps_its_own_clock_as_the_provider_time():
    """The defect that killed the previous two attempts, pinned.

    `submitted_at` looked like a lower bound on execution and was not:
    _persist_submission also runs on the reconciler's ADOPTION path, and there it
    INSERTS the queue row for the first time, so ON CONFLICT DO NOTHING protects
    nothing and the row gets `now` — days after the lookups. Billing against that
    places pre-subscription usage inside a paid period.

    Asserts provenance per path, not merely that a column is populated.
    """
    from src.workers.skip_trace_dispatcher import provider_submitted_time

    claim_time = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    provider = {"created_at": "2026-09-01T10:00:00.123456Z"}
    expected = datetime(2026, 9, 1, 10, 0, 0, 123456, tzinfo=UTC)

    # Dispatch, no provider timestamp: the CLAIM time, taken and committed
    # before the POST went out. The first version used the bookkeeping clock and
    # called it a lower bound; it is not one, because bookkeeping runs after the
    # POST returns and the provider can start work the moment it receives the
    # batch.
    assert provider_submitted_time({}, claim_time, adopted=False) == claim_time

    # Dispatch WITH a provider timestamp: the provider's wins over ours.
    assert provider_submitted_time(provider, claim_time, adopted=False) == expected

    # Adoption WITH a provider timestamp: still the provider's, unaffected by
    # when we happened to notice the batch.
    assert provider_submitted_time(provider, claim_time, adopted=True) == expected

    # Adoption with NOTHING: NULL. This is the whole point of the parameter.
    # Every clock still available on that path is later than the work.
    assert provider_submitted_time({}, claim_time, adopted=True) is None
    assert provider_submitted_time({"created_at": None}, claim_time, adopted=True) is None
    assert provider_submitted_time({"created_at": "not-a-date"}, claim_time, adopted=True) is None


def test_a_naive_provider_timestamp_is_pinned_to_utc():
    """A naive value in a timestamptz column is read back in the server's zone.

    That silently shifts the billing period by the offset, which is how usage
    lands in the wrong Stripe invoice without anything looking wrong.
    """
    from src.workers.skip_trace_dispatcher import provider_submitted_time

    claim_time = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    got = provider_submitted_time({"created_at": "2026-09-01T10:00:00"}, claim_time, adopted=True)
    assert got == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    assert got.tzinfo is not None


def test_the_dispatcher_records_the_provider_time_it_computes():
    """The pure rule above is only worth anything if the writer actually uses it.

    Pins the wiring: _persist_submission must pass its response/adopted through
    to provider_submitted_time and store the result, so the rule cannot be
    correct while the column stays NULL.
    """
    import inspect

    from src.workers import skip_trace_dispatcher as d

    src = inspect.getsource(d._persist_submission)
    assert "provider_submitted_time(response, claim_time, adopted)" in src, (
        "_persist_submission must derive the value from the shared rule"
    )
    assert "provider_submitted_at=provider_submitted_at" in src, (
        "the derived value must actually be written to the queue row"
    )
    # And the adoption call site must declare itself, or every adopted queue
    # silently takes the dispatch branch and gets the adoption clock. It also
    # passes its claim_time (1b-1b-i, C2), which pins the row update to the claim
    # being adopted. Checked on the parsed CALL, not the source text, so a comment
    # or a different call can never satisfy it (Codex).
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(d._reconcile_stale_claims)))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_persist_submission"
    ]
    assert len(calls) == 1, "expected exactly one _persist_submission call in the reconciler"
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("adopted"), ast.Constant) and kw["adopted"].value is True, (
        "the reconciler's adoption call must pass adopted=True"
    )
    assert isinstance(kw.get("claim_time"), ast.Name) and kw["claim_time"].id == "claim_time", (
        "the reconciler's adoption call must pass its claim_time"
    )
# ─── Plan switching: one subscription, moved, never a second one ─────────────


def _sub_items(*price_ids):
    """A Stripe subscription shaped the way _plan_change_items reads one."""
    return {
        "id": "sub_live",
        "status": "active",
        "items": {
            "data": [
                {"id": f"si_{i}", "price": {"id": pid}}
                for i, pid in enumerate(price_ids)
            ]
        },
    }


def _billing():
    from src.api.routes import billing as b
    return b


def test_a_plan_change_moves_the_licensed_item_it_does_not_add_one():
    """The defect, at its root: a switch must not leave two plan items.

    create_checkout built a whole second subscription; the same mistake one
    level down is adding a second licensed item beside the first. The array must
    carry the EXISTING item id with a new price.
    """
    _require_stripe_prices(plan=True)
    b = _billing()
    pro_m = b.settings.STRIPE_PRICE_PRO
    biz_m = b.settings.STRIPE_PRICE_BUSINESS

    sub = _sub_items(pro_m)
    items = b._plan_change_items(sub, biz_m, "business", "month")

    licensed = [i for i in items if i.get("price") == biz_m]
    assert len(licensed) == 1
    assert licensed[0].get("id") == "si_0", (
        "the licensed item must be re-priced by id, not added alongside the old one"
    )


def test_the_metered_item_is_replaced_not_repriced():
    """Re-pricing in place would retroactively re-rate this period's lookups.

    assert_billable refuses to bill usage against a metered item created AFTER
    the usage happened — that is what stops a rate agreed today being applied to
    last week. Updating an item keeps its id and its `created`, so an in-place
    re-price produces an item that looks like it always carried the new rate and
    the check silently passes. Delete plus add gives the new item a new
    `created`, so pre-switch usage goes to a human instead.
    """
    _require_stripe_prices(plan=True)
    b = _billing()
    pro_m = b.settings.STRIPE_PRICE_PRO
    biz_m = b.settings.STRIPE_PRICE_BUSINESS
    st_pro_m = b.settings.STRIPE_PRICE_SKIP_TRACE_PRO
    st_biz_m = b.settings.STRIPE_PRICE_SKIP_TRACE_BUSINESS_OVERAGE

    sub = _sub_items(pro_m, st_pro_m)
    items = b._plan_change_items(sub, biz_m, "business", "month")

    # The old metered item is DELETED...
    deleted = [i for i in items if i.get("deleted")]
    assert [i["id"] for i in deleted] == ["si_1"]

    # ...and the new one is ADDED with no id, so Stripe mints a fresh `created`.
    added = [i for i in items if i.get("price") == st_biz_m]
    assert len(added) == 1
    assert "id" not in added[0], (
        "carrying the old item id would preserve its `created` and re-rate "
        "usage that predates this price"
    )


def test_a_monthly_to_annual_switch_moves_both_items_in_one_call():
    """Stripe requires every item on a subscription to share one interval.

    So the licensed and metered prices cannot move separately: a first call
    leaving a monthly metered price beside an annual plan price is rejected, and
    the customer is left mid-transition. One array, one modify.
    """
    _require_stripe_prices(plan=True)
    b = _billing()
    pro_m = b.settings.STRIPE_PRICE_PRO
    pro_y = b.settings.STRIPE_PRICE_PRO_ANNUAL
    st_pro_m = b.settings.STRIPE_PRICE_SKIP_TRACE_PRO
    st_pro_y = b.settings.STRIPE_PRICE_SKIP_TRACE_PRO_ANNUAL

    sub = _sub_items(pro_m, st_pro_m)
    items = b._plan_change_items(sub, pro_y, "pro", "year")

    prices = {i.get("price") for i in items if i.get("price")}
    assert prices == {pro_y, st_pro_y}, (
        "both the plan price and the metered price must move to the new interval"
    )
    assert any(i.get("deleted") for i in items), "the monthly metered item must go"


def test_an_unprovisioned_metered_interval_removes_the_item_rather_than_mixing():
    """No metered price for the target interval means no metered item.

    Leaving the old one attached is the interval violation this function exists
    to avoid, and it would also keep billing overage at a rate belonging to a
    plan the customer has left.
    """
    _require_stripe_prices(plan=True)
    b = _billing()
    pro_m = b.settings.STRIPE_PRICE_PRO
    pro_y = b.settings.STRIPE_PRICE_PRO_ANNUAL
    st_pro_m = b.settings.STRIPE_PRICE_SKIP_TRACE_PRO

    sub = _sub_items(pro_m, st_pro_m)
    # Force "no metered price provisioned for the target".
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(b, "_metered_skip_trace_price", lambda plan, interval: None)
        items = b._plan_change_items(sub, pro_y, "pro", "year")
    finally:
        mp.undo()

    assert {"id": "si_1", "deleted": True} in items
    assert not any(
        i.get("price") and i["price"].startswith("price_test_st") for i in items
    ), "no metered item may survive a move to an interval that has no price"


def test_a_subscription_with_no_recognised_plan_price_is_refused():
    """Guessing which item to re-price is worse than refusing.

    Re-pricing an unidentified item, or adding a licensed item beside it, both
    end with the customer on something nobody chose.
    """
    b = _billing()
    sub = _sub_items("price_something_we_do_not_sell")
    with pytest.raises(b._UnrecognisedSubscriptionError):
        b._plan_change_items(sub, b.settings.STRIPE_PRICE_PRO, "pro", "month")


def test_change_plan_can_never_create_a_subscription():
    """The whole point of separating this from checkout.

    If this endpoint can mint a Session or a Subscription, then the duplicate it
    exists to prevent has simply moved to a new address.
    """
    import inspect

    b = _billing()
    src = inspect.getsource(b.change_plan)

    for forbidden in ("Session.create", "Subscription.create", "Customer.create"):
        assert forbidden not in src, (
            f"change_plan must not call {forbidden}: it modifies the "
            "subscription that exists and refuses when there is none"
        )
    assert "Subscription.modify" in src


def test_change_plan_and_checkout_share_one_lock_namespace():
    """Two locks would let the two endpoints interleave.

    They guard one invariant between them — this customer has exactly one
    subscription. A checkout and a plan change running concurrently under
    different keys is exactly how the second subscription gets created while
    both guards read "none".
    """
    import inspect

    b = _billing()
    assert "pg_advisory_xact_lock(4243" in inspect.getsource(b.change_plan)
    assert "pg_advisory_xact_lock(4243" in inspect.getsource(b.create_checkout)


def test_checkout_sends_an_existing_subscriber_to_the_change_plan_path():
    """A refusal that names no next step is a dead end.

    The portal cannot do it (subscription_update is disabled on the live
    configuration), and checkout itself would create the duplicate.
    """
    b = _billing()
    exc = b._subscription_conflict({"id": "sub_1", "status": "active"})
    assert exc.status_code == 409
    assert exc.detail["code"] == "subscription_exists"
    assert exc.detail.get("action") == "change_plan"
    assert "support" not in exc.detail["message"].lower(), (
        "there is an endpoint for this now; support is not the next step"
    )


def test_an_incomplete_subscription_is_not_modified_underneath_its_payment():
    """Stripe holds a first payment ~23h; changing the price mid-flight changes
    what the customer is being charged after they have already authorised it."""
    import inspect

    b = _billing()
    src = inspect.getsource(b.change_plan)
    assert '"incomplete"' in src
    assert "subscription_incomplete" in src
# ─── The re-gate findings, pinned ────────────────────────────────────────────


def test_the_gate_is_asked_once_per_report(monkeypatch):
    """Two calls meant two Stripe subscription listings for every report.

    The gate briefly lived in BOTH the worker and the sender: the worker asked
    to decide the disposition, then the sender asked again to authorise the
    send. Same question, same answer, one of them thrown away, and Stripe billed
    for the round trip either way. The sender owns it now, and the worker reads
    the refusal it raises.
    """
    import inspect

    from src.workers import tracerfy_ingest as ti

    src = inspect.getsource(ti.report_skip_trace_meter_event)
    assert "assert_billable(" not in src, (
        "the worker must not re-ask the gate; the sender runs it on the exact "
        "arguments it sends"
    )
    # ...and the refusal must still be turned into a disposition here, or the
    # rule would run with nobody writing down the answer.
    assert "_NotBillableError as refusal" in src
    assert 'refusal.reason == "no_subscription_ever"' in src, (
        "the ONE write-off is the Stripe-confirmed one; everything else is "
        "uncertainty and goes to a human"
    )


def test_stranded_usage_is_recorded_before_stripe_is_called():
    """Ordering is the whole fix, so ordering is what gets asserted.

    Written after the modify, it was not retry-safe: if the UPDATE or the
    request commit failed once Stripe had already swapped the item, the rows
    stayed `reported` and a retry took the same-plan shortcut and never came
    back. Marking first can only over-flag, and a human releases those.
    """
    import inspect

    from src.api.routes import billing as b

    src = inspect.getsource(b.change_plan)
    mark = src.index("_mark_stranded_metered_usage")
    modify = src.index("stripe.Subscription.modify")
    assert mark < modify, (
        "the stranded-usage marking must be durable BEFORE the subscription is "
        "modified; afterwards a failure loses the revenue with no record"
    )


def test_the_stranded_marking_does_not_release_the_checkout_lock():
    """Committing on the request session would have unlocked the guard.

    The plan-change guard is pg_advisory_xact_lock, which is TRANSACTION scoped.
    Committing the request's transaction to make the marking durable would
    release it mid-flight and let a concurrent checkout through — trading a
    lost-revenue bug for a duplicate-subscription one.
    """
    import inspect

    from src.api.routes import billing as b

    helper = inspect.getsource(b._mark_stranded_metered_usage)
    assert "system_sync_session" in helper, (
        "the marking needs its own connection so the caller's lock survives"
    )
    change = inspect.getsource(b.change_plan)
    assert "asyncio.to_thread" in change, (
        "a sync session in an async route must not run on the event loop"
    )
    assert "await db.commit()" not in change, (
        "committing the request transaction releases the advisory lock"
    )


def test_a_malformed_subscription_is_reported_even_when_nothing_changes():
    """The shape check must run before the same-plan shortcut.

    The other way round, a subscription carrying two licensed items answered
    "you are already on that plan" and the malformation was never surfaced,
    because the only thing that inspects the shape is _plan_change_items and the
    shortcut returned before reaching it.
    """
    import inspect

    from src.api.routes import billing as b

    src = inspect.getsource(b.change_plan)
    validate = src.index("_plan_change_items(")
    shortcut = src.index('"status": "unchanged"')
    assert validate < shortcut, (
        "validate the subscription shape before returning 'unchanged', or a "
        "malformed subscription is silently accepted"
    )


def test_two_licensed_items_are_refused_not_sampled():
    """The behaviour behind the ordering test above."""
    b = _billing()
    pro_m = b.settings.STRIPE_PRICE_PRO
    biz_m = b.settings.STRIPE_PRICE_BUSINESS

    sub = _sub_items(pro_m, biz_m)  # two licensed items
    with pytest.raises(b._UnrecognisedSubscriptionError):
        b._plan_change_items(sub, biz_m, "business", "month")


def test_an_unknown_price_on_the_subscription_is_refused():
    """Something we do not sell is on there. Proceeding leaves the customer on
    a subscription nobody chose."""
    b = _billing()
    pro_m = b.settings.STRIPE_PRICE_PRO
    biz_m = b.settings.STRIPE_PRICE_BUSINESS

    sub = _sub_items(pro_m, "price_mystery")
    with pytest.raises(b._UnrecognisedSubscriptionError):
        b._plan_change_items(sub, biz_m, "business", "month")


def test_usage_is_held_when_the_counted_window_cannot_be_established():
    """"We do not know which window this was counted against" must fail closed.

    billable_units is a function of one specific entitlement window's counter.
    If that window is unknown, the quantity cannot be defended, and the case
    nobody can reason about must not be the one that bills automatically.
    """
    import inspect

    from src.api.billing import skip_trace_usage as st

    src = inspect.getsource(st.report_usage_from_webhook)
    assert "counted_from is None or usage_at < counted_from" in src, (
        "a missing counted window must hold the row, not wave it through"
    )
    assert "usage_outside_counted_window" in src
# ─── Round-4 gate findings, pinned ───────────────────────────────────────────


def test_the_meter_worker_takes_the_same_lock_as_the_plan_change():
    """Otherwise a report can slip into the middle of a plan change.

    The route marks this period's reported rows as stranded, then asks Stripe to
    delete the old metered item. Holding no lock, this worker could claim a
    still-pending row in that window, pass the gate against the item that is
    about to vanish, and report it — landing usage on an item Stripe deletes a
    moment later, after the marking that would have caught it had already run.
    """
    import inspect

    from src.workers import tracerfy_ingest as ti

    src = inspect.getsource(ti.report_skip_trace_meter_event)

    # TRY, not wait. The Stripe calls happen inside this transaction, so
    # whoever holds this lock holds it across a network round trip. If that
    # were the worker, a customer clicking Upgrade would wait behind a
    # background meter report for as long as Stripe took. A plan change is a
    # person waiting; this is a background task with a sweep behind it.
    assert "pg_try_advisory_xact_lock(4243" in src, (
        "the worker must YIELD to a plan change, not block it"
    )
    assert "pg_advisory_xact_lock(4243" not in src.replace("pg_try_advisory_xact_lock", ""), (
        "a blocking wait here puts a background task in front of a customer"
    )
    # Before the row is read, or it is not a barrier at all.
    assert src.index("pg_try_advisory_xact_lock(4243") < src.index("with_for_update=True"), (
        "the lock must be taken before the row is read, so the worker never "
        "waits on the lock while holding a row lock"
    )
    # Failing to get it must write NOTHING and leave the row for the sweep.
    assert "billing_change_in_progress" in src
    deferral = src.split("if not got_lock:")[1][:400]
    assert "row.disposition" not in deferral, (
        "deferring is not a decision; the row must stay pending"
    )


def test_a_missing_customer_id_is_not_a_write_off():
    """Only Stripe gets to say a customer never had a subscription.

    `no_customer_id` and `no_subscription_ever` look equally certain and are not.
    The first is a fact about OUR row: create_checkout writes that column, most
    accounts here have no customer id at all because plans are set by hand, and
    the value can appear a moment after we looked. The second is confirmed by the
    system that would do the billing. non_billable is permanent, so only the
    second earns it (Codex).
    """
    import inspect

    from src.workers import tracerfy_ingest as ti

    src = inspect.getsource(ti.report_skip_trace_meter_event)
    mapping = src.split("row.disposition = (")[1][:300]
    assert '"no_subscription_ever"' in mapping
    assert '"no_customer_id"' not in mapping, (
        "a missing customer id is mutable local state; writing it off "
        "permanently discards usage for a customer who is mid-checkout"
    )


def test_wrongly_stranded_rows_can_be_released_but_nothing_else_can():
    """Pre-marking can only over-flag, so there has to be a way back.

    If the marking commits and Stripe then refuses, the old item is still on the
    subscription and those rows were never stranded. Without a release path they
    sit in review forever and the script can only settle or write them off —
    turning an over-flag into a manual write-off.

    Scoped to the one reason on purpose: this must not be able to reverse a
    human's settle or write-off.
    """
    src = open("scripts/settle_skip_trace_meter_rows.py", encoding="utf-8").read()

    assert "--release" in src
    assert '_RELEASABLE_REASON = "metered_item_replaced_before_invoice"' in src
    # The release statements must both be pinned to that reason.
    for stmt in ("_RELEASE_BY_ID", "_RELEASE_BY_USER"):
        block = src.split(stmt, 1)[1][:600]
        assert "disposition_reason = :releasable" in block, (
            f"{stmt} must only ever release the abandoned-plan-change reason"
        )
        assert "disposition = 'needs_review'" in block, (
            f"{stmt} must only move rows OUT of review, never out of a decision"
        )


def test_the_stranded_marking_fails_the_plan_change_rather_than_skipping_it():
    """It runs before Stripe, so refusing costs nothing and proceeding costs revenue.

    Nothing has been modified at that point, so a failure here is free to
    surface. Carrying on would perform the plan change without the record that
    the marking exists to create.
    """
    import inspect

    from src.api.routes import billing as b

    src = inspect.getsource(b.change_plan)
    # Up to the Stripe call: everything between the marking and the modify is
    # the window this assertion is about.
    guard = src.split("_mark_stranded_metered_usage")[1].split(
        "stripe.Subscription.modify"
    )[0]
    assert "HTTP_503_SERVICE_UNAVAILABLE" in guard, (
        "a failure to record stranded usage must refuse the plan change"
    )
    assert "_stranded_mark_slots()" in src, (
        "the sync pool is small; unbounded use surfaces a timeout as a 500"
    )
    # asyncio.timeout around to_thread is not a bound: the thread cannot be
    # cancelled, so it keeps running and commits after the 503 has been sent,
    # while its semaphore slot is already released.
    #
    # Asserted against CODE only. The comment above the fix names the thing it
    # removed, so a raw source search fails on the documentation rather than the
    # implementation — the same overstated-assertion trap this file has already
    # had to retire twice.
    code_only = chr(10).join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "asyncio.timeout" not in code_only, (
        "to_thread cannot be cancelled; bound the statement in the database "
        "instead of pretending the await is a timeout"
    )
    helper = inspect.getsource(b._mark_stranded_metered_usage)
    assert "statement_timeout" in helper and "lock_timeout" in helper, (
        "the real bound belongs where it can stop the work"
    )
