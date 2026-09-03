"""Hard 18-month tax-delinquent cap on the DIALER delivery paths.

The same standing product rule that hides stale tax leads from the results
list/export (`tests/test_tax_cap_jobs.py`) must also keep them OUT of the dialer:
- `src/workers/dialer_outbox.py` — the per-contact outbox transport selects the
  Result it is about to POST with `tax_cap_condition(today)` ANDed on, so an
  out-of-window tax lead resolves to None and is dropped (never POSTed).
- `src/workers/scheduler_helpers/dialer.py` — the push sweep adds
  `tax_cap_condition(today)` to its `conds`, so a stale tax lead is never swept
  into the count/fetch that feeds the dialer.

These run under a sync system session in prod. Here we exercise the EXACT WHERE
clauses each path builds, against real seeded rows on the conftest async `db`
session (real Postgres, no mocks), and assert which Result ids survive. We also
assert the predicate object is actually wired into the source query / conds list
so a future edit that drops it fails this test.

Real DB + real predicates — no mocks.
"""
import inspect
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dialer_filters import dialer_ready_conditions
from src.api.lead_actionability import actionable_condition
from src.api.tax_filters import tax_cap_condition, tax_cap_min_year
from src.db.models import Job, Result, ScraperConfig, User

# Frozen "today" — tax_cap_min_year() is the same math the delivery paths use, so
# the boundary is whatever the cap resolves to right now. Pick rows clearly inside
# (the cap year) and clearly outside (2010) the 18-month window.
_TODAY = datetime.now(UTC).date()
_IN_WINDOW_YEAR = tax_cap_min_year(_TODAY)   # oldest year still deliverable
_OUT_OF_WINDOW_YEAR = 2010                    # decades stale -> always dropped


async def _seed_three(db: AsyncSession, job_id: str, user_id: str) -> dict[str, str]:
    """Three dialer-ready rows: in-window tax, out-of-window tax, non-tax (NULL).

    All three carry a phone + are non-duplicate so they satisfy the dialer-ready
    filter — the ONLY thing that may exclude a row in these tests is the tax cap.
    """
    ids = {
        "in_window": str(uuid.uuid4()),
        "out_of_window": str(uuid.uuid4()),
        "non_tax": str(uuid.uuid4()),
    }
    common = {
        "job_id": job_id,
        "user_id": user_id,
        "phone": "2065550100",
        "is_duplicate": False,
        # Leads carry an address (lead_actionability, 2026-09-02): the sweep and
        # the outbox never dial an address-less row, so the fixtures have one.
        "property_address": "100 MAIN ST",
    }
    db.add(Result(
        id=ids["in_window"], party_name="IN WINDOW OWNER",
        delinquent_amount=5000, delinquent_bill_year=_IN_WINDOW_YEAR, **common,
    ))
    db.add(Result(
        id=ids["out_of_window"], party_name="STALE DEBT OWNER",
        delinquent_amount=5000, delinquent_bill_year=_OUT_OF_WINDOW_YEAR, **common,
    ))
    db.add(Result(
        id=ids["non_tax"], party_name="PROBATE OWNER",
        delinquent_amount=None, delinquent_bill_year=None, **common,
    ))
    await db.commit()
    return ids


async def _make_done_job(db: AsyncSession, user: User, config: ScraperConfig) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(
        id=job_id,
        user_id=user.id,
        scraper_config_id=config.id,
        status="done",
        trigger="manual",
    ))
    await db.commit()
    return job_id


# ─── Outbox transport: the per-contact Result re-read ───────────────────────────

async def test_outbox_result_reread_excludes_out_of_window(
    db: AsyncSession,
    starter_user: User,
    scraper_config: ScraperConfig,
):
    """process_dialer_outbox re-reads each Result with tax_cap_condition before
    POSTing. An out-of-window tax row resolves to None (skipped); the in-window
    tax row and the non-tax row resolve normally."""
    job_id = await _make_done_job(db, starter_user, scraper_config)
    ids = await _seed_three(db, job_id, starter_user.id)

    async def reread(result_id: str):
        # Byte-identical WHERE to dialer_outbox.py's per-row select.
        return (await db.execute(
            select(Result).where(
                Result.id == result_id,
                Result.user_id == starter_user.id,
                tax_cap_condition(_TODAY),
                actionable_condition(),
            )
        )).scalar_one_or_none()

    assert await reread(ids["in_window"]) is not None
    assert await reread(ids["non_tax"]) is not None
    # The stale tax lead is hidden -> the transport drops it instead of POSTing.
    assert await reread(ids["out_of_window"]) is None


def test_outbox_query_wires_in_tax_cap():
    """Guard against a future edit silently dropping the cap from the outbox."""
    from src.workers import dialer_outbox

    src = inspect.getsource(dialer_outbox.process_dialer_outbox)
    assert "tax_cap_condition(today)" in src
    assert "from src.api.tax_filters import tax_cap_condition" in src
    # Standing actionability rule (lead_actionability) must stay wired too.
    assert "actionable_condition()" in src


# ─── Push sweep: the conds the sweep selects on ─────────────────────────────────

async def test_push_sweep_count_and_fetch_exclude_out_of_window(
    db: AsyncSession,
    business_user: User,
):
    """The sweep's `conds` (dialer-ready + not-duplicate + tax cap) must drop the
    stale tax row from BOTH the count and the fetched leads, while keeping the
    in-window tax row and the non-tax row."""
    # business plan: dialer push is a Business+ feature; use a config owned by them.
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=business_user.id,
        name="Sweep Tax Cap Config",
        county="king",
        state="WA",
        record_type="tax_delinquent",
        fields=["party_name"],
        enrichment=[],
        schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()

    job_id = await _make_done_job(db, business_user, config)
    ids = await _seed_three(db, job_id, business_user.id)

    # Reconstruct the EXACT conds the sweep assembles (see _dialer_push_sweep_impl).
    conds = dialer_ready_conditions(include_unknown_dnc=True)
    conds = [*conds, Result.is_duplicate.is_(False)]
    conds = [*conds, tax_cap_condition(_TODAY)]
    conds = [*conds, actionable_condition()]

    total = (await db.execute(
        select(func.count()).select_from(Result).where(
            Result.job_id == job_id, Result.user_id == business_user.id, *conds
        )
    )).scalar_one()
    rows = (await db.execute(
        select(Result).where(
            Result.job_id == job_id, Result.user_id == business_user.id, *conds
        ).order_by(Result.id)
    )).scalars().all()
    selected = {r.id for r in rows}

    assert ids["in_window"] in selected
    assert ids["non_tax"] in selected
    assert ids["out_of_window"] not in selected
    # Count is off the same WHERE, so it must agree with the fetched leads.
    assert total == 2
    assert len(rows) == 2


def test_push_sweep_wires_in_tax_cap():
    """Guard against a future edit silently dropping the cap from the sweep."""
    from src.workers.scheduler_helpers import dialer

    src = inspect.getsource(dialer._dialer_push_sweep_impl)
    assert "tax_cap_condition(_today)" in src
    assert "from src.api.tax_filters import tax_cap_condition" in src
