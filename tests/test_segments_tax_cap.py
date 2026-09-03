"""Hard 18-month tax-delinquent cap on the SEGMENT + BATCH-EXPORT lead queries.

The standing product rule that hides stale tax leads from the per-job results
surface (`tests/test_tax_cap_jobs.py`) and the dialer (`tests/test_dialer_tax_cap.py`)
must ALSO keep them out of the cross-list lead products:

- `src/api/routes/segments.py` — the `candidates` CTE of EVERY raw-SQL lead query
  (`_INTERSECTION_SQL`, `_UNION_SQL`, `_INTERSECTION_DATED_SQL`) and the
  `_EXCLUDED_NO_DATE_SQL` count ANDs in `tax_cap_sql('r')`, bound via
  `TAX_CAP_BIND`. An out-of-window tax Result never reaches preview/export.
- `src/workers/batch_export.py` — `_COMBINED_SQL` (the combined batch CSV) ANDs in
  the same fragment with the same bind.

Two layers, matching the existing tax-cap tests:
  1. LIVE DB (conftest async `db`/token/config fixtures, real Postgres in CI):
     seed in-window tax / out-of-window tax / non-tax rows and assert which
     survive each query.
  2. STRING/SOURCE GUARDS (no DB): assert the cap fragment + ``:tax_cap_min_year``
     placeholder live in each SQL constant, and that EVERY execute() call site
     binds ``TAX_CAP_BIND`` — a fragment that is never bound raises at runtime.

Real DB + real predicates — no mocks.
"""
import inspect
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import src.api.routes.segments as segments
import src.workers.batch_export as batch_export
from src.api.tax_filters import TAX_CAP_BIND, tax_cap_min_year, tax_cap_sql
from src.db.models import Job, PropertyListMembership, Result, ScraperConfig, User
from src.workers.batch_export import _COMBINED_SQL

# Frozen "today" — tax_cap_min_year() is the same math the queries bind, so the
# boundary year is whatever the cap resolves to right now. Pick rows clearly inside
# (the cap year) and clearly outside (2010) the 18-month window.
_TODAY = datetime.now(UTC).date()
_IN_WINDOW_YEAR = tax_cap_min_year(_TODAY)   # oldest year still visible
_OUT_OF_WINDOW_YEAR = 2010                    # decades stale -> always dropped
_CAP_BIND_VALUE = tax_cap_min_year(_TODAY)


# ─── Live-DB helpers ────────────────────────────────────────────────────────────

async def _tax_config(db: AsyncSession, user: User) -> ScraperConfig:
    """A tax_delinquent config owned by `user` (the segment queries join configs
    and filter by record_type)."""
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name="Tax Cap Segment Config",
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
    return config


async def _done_job(db: AsyncSession, user: User, config: ScraperConfig) -> str:
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


async def _seed_three(db: AsyncSession, job_id: str, user_id: str) -> dict[str, str]:
    """Three tax_delinquent-job rows: in-window tax, out-of-window tax, non-tax.

    All carry a distinct property_key so each is its OWN union bucket (the bucket
    ranking can't merge them away — the only thing that may exclude a row here is
    the tax cap). The non-tax row has NULL bill_year (every probate/etc row).
    """
    ids = {
        "in_window": str(uuid.uuid4()),
        "out_of_window": str(uuid.uuid4()),
        "non_tax": str(uuid.uuid4()),
    }
    db.add(Result(
        id=ids["in_window"], job_id=job_id, user_id=user_id,
        party_name="IN WINDOW OWNER", property_key=f"pk-in-{job_id}",
        # Leads carry an address (lead_actionability, 2026-09-02): address-less
        # rows are quarantined from every segment/export, so fixtures have one.
        property_address="1 IN WINDOW ST",
        delinquent_amount=5000, delinquent_bill_year=_IN_WINDOW_YEAR,
    ))
    db.add(Result(
        id=ids["out_of_window"], job_id=job_id, user_id=user_id,
        party_name="STALE DEBT OWNER", property_key=f"pk-out-{job_id}",
        property_address="2 STALE DEBT ST",
        delinquent_amount=5000, delinquent_bill_year=_OUT_OF_WINDOW_YEAR,
    ))
    db.add(Result(
        id=ids["non_tax"], job_id=job_id, user_id=user_id,
        party_name="PROBATE OWNER", property_key=f"pk-nontax-{job_id}",
        property_address="3 PROBATE LN",
        delinquent_amount=None, delinquent_bill_year=None,
    ))
    await db.commit()
    return ids


# ─── Live DB: union query (the simplest lead path — no membership rollup) ────────

async def test_union_sql_excludes_out_of_window(
    db: AsyncSession,
    starter_user: User,
):
    """`_UNION_SQL` keeps the in-window tax row + the non-tax row and drops the
    out-of-window tax row, bound exactly as `_fetch_union` binds it."""
    config = await _tax_config(db, starter_user)
    job_id = await _done_job(db, starter_user, config)
    ids = await _seed_three(db, job_id, starter_user.id)

    sql = text(segments._UNION_SQL.format(county_clause=""))
    result = await db.execute(sql, {
        "uid": starter_user.id,
        "types": ["tax_delinquent"],
        "limit": 1000,
        "filing_from": None,
        "filing_to": None,
        "require_date": False,
        TAX_CAP_BIND: _CAP_BIND_VALUE,
    })
    returned = {str(r._mapping["id"]) for r in result.fetchall()}

    assert ids["in_window"] in returned
    assert ids["non_tax"] in returned
    assert ids["out_of_window"] not in returned


# ─── Live DB: intersection query (membership-backed all-time path) ──────────────

async def test_intersection_sql_excludes_out_of_window(
    db: AsyncSession,
    starter_user: User,
):
    """`_INTERSECTION_SQL` (two-list overlap) drops the out-of-window tax row even
    when it IS on both lists, while the in-window overlapping property survives.

    Each property must appear on TWO record types to overlap, so we seed a probate
    config + job and a probate Result re-using the SAME property_key as each tax
    row, plus the property_list_membership rows the all-time query reads.
    """
    tax_cfg = await _tax_config(db, starter_user)
    tax_job = await _done_job(db, starter_user, tax_cfg)
    await _seed_three(db, tax_job, starter_user.id)  # seeds the in/out/non-tax tax rows

    probate_cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=starter_user.id, name="Probate Cfg",
        county="king", state="WA", record_type="probate", fields=["party_name"],
        enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(probate_cfg)
    await db.commit()
    probate_job = await _done_job(db, starter_user, probate_cfg)

    # A probate Result for the in-window and out-of-window properties (NULL
    # bill_year — probate rows are non-tax) so each property is on BOTH lists.
    for key, pk in (("in_window", f"pk-in-{tax_job}"), ("out_of_window", f"pk-out-{tax_job}")):
        db.add(Result(
            id=str(uuid.uuid4()), job_id=probate_job, user_id=starter_user.id,
            party_name=f"PROBATE {key}", property_key=pk,
            property_address=f"9 {key.upper()} WAY",
            delinquent_amount=None, delinquent_bill_year=None,
        ))
    # property_list_membership rollup the all-time intersection reads.
    for pk in (f"pk-in-{tax_job}", f"pk-out-{tax_job}"):
        for rt in ("tax_delinquent", "probate"):
            db.add(PropertyListMembership(
                user_id=starter_user.id, property_key=pk, record_type=rt,
            ))
    await db.commit()

    sql = text(segments._INTERSECTION_SQL.format(county_clause=""))
    result = await db.execute(sql, {
        "uid": starter_user.id,
        "types": ["tax_delinquent", "probate"],
        "n": 2,
        "limit": 1000,
        TAX_CAP_BIND: _CAP_BIND_VALUE,
    })
    # _INTERSECTION_SQL does NOT expose property_key in its final SELECT, so
    # identify each property by party_name (which IS returned). One ranked row is
    # emitted per overlapping property bucket; for pk-in that row is EITHER the tax
    # or the probate owner, so test membership with a set intersection.
    returned_names = {r._mapping["party_name"] for r in result.fetchall()}

    # The in-window property is on both lists AND within the cap -> its bucket
    # survives (the ranked row is one of its two owners).
    assert returned_names & {"IN WINDOW OWNER", "PROBATE in_window"}
    # The out-of-window property's tax row is capped out of the candidates CTE, so
    # only its probate row remains -> overlap_count 1 < n=2 -> bucket excluded.
    assert not (returned_names & {"STALE DEBT OWNER", "PROBATE out_of_window"})
    # The non-tax (single-list) property never overlaps -> excluded regardless.
    assert "PROBATE OWNER" not in returned_names


# ─── Live DB: batch combined export query ───────────────────────────────────────

async def test_combined_sql_excludes_out_of_window(
    db: AsyncSession,
    starter_user: User,
):
    """`_COMBINED_SQL` (the batch combined CSV source) keeps the in-window tax row
    + the non-tax row and drops the out-of-window tax row, bound exactly as
    `_combined_pairs` binds it."""
    config = await _tax_config(db, starter_user)
    job_id = await _done_job(db, starter_user, config)
    ids = await _seed_three(db, job_id, starter_user.id)

    result = await db.execute(text(_COMBINED_SQL), {
        "uid": starter_user.id,
        "job_ids": [job_id],
        "limit": 1000,
        "offset": 0,
        "overlaps_only": False,
        TAX_CAP_BIND: _CAP_BIND_VALUE,
    })
    returned = {str(r._mapping["id"]) for r in result.fetchall()}

    assert ids["in_window"] in returned
    assert ids["non_tax"] in returned
    assert ids["out_of_window"] not in returned


# ─── String guards (no DB): the cap fragment lives in every SQL constant ─────────

_FRAGMENT = tax_cap_sql("r")  # "(r.delinquent_bill_year IS NULL OR ... >= :tax_cap_min_year)"


def test_fragment_self_scopes_on_null_bill_year():
    # The shared helper is the source of truth; lock its self-scoping shape so a
    # non-tax (NULL bill_year) row always passes the cap.
    assert "r.delinquent_bill_year IS NULL" in _FRAGMENT
    assert f":{TAX_CAP_BIND}" in _FRAGMENT


def test_every_segment_sql_constant_carries_the_cap():
    for name in (
        "_INTERSECTION_SQL",
        "_UNION_SQL",
        "_INTERSECTION_DATED_SQL",
        "_EXCLUDED_NO_DATE_SQL",
    ):
        sql = getattr(segments, name)
        assert _FRAGMENT in sql, f"{name} missing tax_cap_sql('r') fragment"
        assert f":{TAX_CAP_BIND}" in sql, f"{name} missing :{TAX_CAP_BIND} placeholder"


def test_combined_sql_constant_carries_the_cap():
    assert _FRAGMENT in _COMBINED_SQL
    assert f":{TAX_CAP_BIND}" in _COMBINED_SQL


def test_segment_format_placeholders_survived_fstring():
    # The constants are now f-strings; the .format() placeholders must still be
    # single-brace tokens so the existing county/pk_clause formatting keeps working.
    assert "{county_clause}" in segments._INTERSECTION_SQL
    assert "{county_clause}" in segments._UNION_SQL
    assert "{county_clause}" in segments._INTERSECTION_DATED_SQL
    assert "{county_clause}" in segments._EXCLUDED_NO_DATE_SQL
    assert "{pk_clause}" in segments._EXCLUDED_NO_DATE_SQL
    # And .format() still produces a bound, cap-carrying query.
    formatted = segments._INTERSECTION_SQL.format(county_clause="")
    assert f":{TAX_CAP_BIND}" in formatted
    assert "{" not in formatted  # no stray unfilled placeholders


# ─── Source guards (no DB): every execute() site binds the cap param ─────────────

def test_fetch_helpers_bind_the_cap_param():
    """A fragment that is never bound raises 'bind parameter required' at runtime.
    Guard that each fetch helper builds TAX_CAP_BIND into its params dict."""
    for fn in (segments._fetch_intersection, segments._fetch_union,
               segments._count_excluded_no_date):
        src = inspect.getsource(fn)
        assert "TAX_CAP_BIND: tax_cap_min_year(" in src, (
            f"{fn.__name__} does not bind the tax cap param"
        )


def test_combined_pairs_binds_the_cap_param():
    src = inspect.getsource(batch_export._combined_pairs)
    assert "TAX_CAP_BIND: tax_cap_min_year(" in src


def test_segments_imports_the_cap_helpers():
    src = inspect.getsource(segments)
    assert "from src.api.tax_filters import TAX_CAP_BIND, tax_cap_min_year, tax_cap_sql" in src


def test_batch_export_imports_the_cap_helpers():
    src = inspect.getsource(batch_export)
    assert "from src.api.tax_filters import TAX_CAP_BIND, tax_cap_min_year, tax_cap_sql" in src
