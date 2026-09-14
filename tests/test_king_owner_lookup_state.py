"""King tax owner lookups must say what happened, per lead.

Production job b2f2ecd5 (King tax_delinquent, 2026-09-13) delivered 840 leads with
0 owner names although eRealProperty publishes an owner for every parcel we
checked. Three defects made that invisible and unrecoverable:

  1. The owner-only pass required a mailing address, so a lead the bulk extract
     could not mail (216 of the 840) never had its owner looked up at all.
  2. A denied source lease returned the same empty dict as "no owner found", so
     the job logged "Resolved 0 owner names from 0/16576 parcels" and left no
     marker on any row. Nothing could tell a busy source from a missing owner.
  3. The user log counted every deferred parcel as "mailing still being looked
     up" (16,859) although 16,576 of them already had a mailing address.

Real DB, real enrichment code. Only the county boundary is substituted: the
eRealProperty HTTP response, the shared source lease, the GIS layer, and the
assessor bulk file.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment import king_county_assessor as kca
from src.scrapers.enrichment.source_health import KING_EREALPROPERTY

pytestmark = pytest.mark.asyncio

# Live eRealProperty markup shape (see tests/test_king_assessor_owner.py).
_NAMED = "3418000010"
_NO_OWNER = "0007200015"   # leading zero on purpose: must survive every step
_NO_ECHO = "5379801941"


def _page(pid: str, owner: str | None) -> str:
    major_minor = f"{pid[:6]}-{pid[6:]}"
    name_cell = owner if owner is not None else "&nbsp;"
    return (
        f'<tr><td style="font-weight:bold;">Parcel Number</td><td>{major_minor}</td></tr>'
        f'<tr><td style="font-weight:bold;">Name</td><td>{name_cell}</td></tr>'
    )


class _Resp:
    def __init__(self, status: int, body: str):
        self.status_code = status
        self.text = body
        self.url = "https://blue.kingcounty.com/x"
        self.headers: dict = {}


def _lease(monkeypatch, *, admitted: bool) -> None:
    class _Lease:
        def __init__(self, *a, **k):
            self.admitted = admitted

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def still_held(self):
            return admitted

    monkeypatch.setattr("src.scrapers.enrichment.source_admission.SourceAdmission", _Lease)


@pytest.fixture(autouse=True)
def _county_offline(monkeypatch):
    """No GIS layer and no bulk file: this file is about the eRealProperty passes."""
    from src.scrapers.enrichment import county_gis, king_rpacct

    monkeypatch.setattr(county_gis, "batch_enrich_parcels_gis",
                        lambda parcel_ids, county, state, stats=None: {})
    monkeypatch.setattr(king_rpacct, "resolve_pins", lambda pins: None)


@pytest.fixture(autouse=True)
def _clean_source_health():
    from src.db.session import SyncSessionLocal

    def _wipe():
        with SyncSessionLocal() as sdb:
            sdb.execute(text("DELETE FROM external_source_health WHERE source_key = :k"),
                        {"k": KING_EREALPROPERTY})
            sdb.commit()

    _wipe()
    yield
    _wipe()


# ── the owner-lookup outcome ledger (pure fetch loop) ────────────────────────

async def test_a_denied_lease_reports_not_admitted_instead_of_zero_owners(monkeypatch):
    _lease(monkeypatch, admitted=False)
    calls: list[str] = []
    monkeypatch.setattr(kca, "safe_get", lambda url, **k: calls.append(url))
    stats: dict = {}

    owners = await kca.batch_extract_king_owners([_NAMED, _NO_OWNER], delay=0, stats=stats)

    assert owners == {}
    assert calls == []
    assert stats["outcome"] == "not_admitted"
    assert stats["attempted"] == [] and stats["no_owner_on_record"] == []


async def test_only_a_page_that_names_the_parcel_can_prove_there_is_no_owner(monkeypatch):
    _lease(monkeypatch, admitted=True)
    pages = {
        _NAMED: _Resp(200, _page(_NAMED, "SHAN HOMES2 LLC")),
        _NO_OWNER: _Resp(200, _page(_NO_OWNER, None)),
        # An interstitial with no parcel cell proves nothing about any parcel: a
        # failed lookup, retryable, never "no owner on record".
        _NO_ECHO: _Resp(200, "<html><body>Please wait</body></html>"),
    }
    monkeypatch.setattr(kca, "safe_get", lambda url, **k: pages[url[-10:]])
    stats: dict = {}

    owners = await kca.batch_extract_king_owners(
        [_NAMED, _NO_OWNER, _NO_ECHO], delay=0, max_unresolved_rate=1.0, stats=stats)

    assert owners == {_NAMED: "SHAN HOMES2 LLC"}
    assert stats["outcome"] == "complete"
    assert stats["attempted"] == [_NAMED, _NO_OWNER]
    assert stats["no_owner_on_record"] == [_NO_OWNER]
    assert stats["transient"] == [_NO_ECHO]
    assert stats["parcel_mismatch"] == []


async def test_a_tripped_breaker_is_recorded_before_it_raises(monkeypatch):
    _lease(monkeypatch, admitted=True)
    monkeypatch.setattr(kca, "safe_get", lambda url, **k: _Resp(503, ""))
    monkeypatch.setattr(kca, "record_source_blocked", lambda *a, **k: None)
    pids = [f"12345{i:05d}" for i in range(10)]
    stats: dict = {}

    with pytest.raises(kca.KingOwnerLookupBlockedError):
        await kca.batch_extract_king_owners(pids, delay=0, circuit_window=5, stats=stats)

    assert stats["outcome"] == "breaker_tripped"
    assert stats["transient"] == pids[:5]
    assert set(stats["attempted"]).isdisjoint(stats["transient"])


# ── the job's enrichment pass, end to end ────────────────────────────────────

async def _tax_job(db, user: User) -> tuple[str, dict[str, str]]:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="King tax owner state",
        county="king", state="WA", record_type="tax_delinquent",
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status="enriching",
               trigger="manual", record_count=3, billed_count=3))
    await db.commit()
    rows = {
        # Has mailing (the only shape the old owner pass would look at).
        "mailed": (_NAMED, "3420 S 164TH ST, SEATAC, WA 98188"),
        # No mailing: the bulk file could not answer it. Old pass skipped it.
        "unmailed": (_NO_OWNER, None),
        # A second lead on the same parcel: one lookup, same outcome.
        "sibling": (_NO_OWNER, "5140 S 172ND LANE, SEATAC, WA 98188"),
    }
    ids: dict[str, str] = {}
    for key, (parcel, mailing) in rows.items():
        rid = str(uuid.uuid4())
        db.add(Result(
            id=rid, user_id=user.id, job_id=job_id, party_name=None, parcel_id=parcel,
            legal_description=parcel, mailing_address=mailing, is_duplicate=False,
            skip_trace_status="not_attempted",
            enrichment_data={"source": "king_county_delinquent_taxes",
                             "delinquent_amount": "7066.18"},
        ))
        ids[key] = rid
    await db.commit()
    return job_id, ids


def _enrich(job_id: str, redis_client) -> dict:
    from src.db.session import system_sync_session
    from src.workers.tasks_helpers.enrich import _run_inline_enrichment

    with system_sync_session() as sdb:
        job = sdb.get(Job, job_id)
        config = sdb.get(ScraperConfig, job.scraper_config_id)
        summary: dict = {}
        _run_inline_enrichment(sdb, job, redis_client, job_id, config, summary=summary)
        return summary


async def _rows(db, ids: dict[str, str]) -> dict:
    got = (await db.execute(text(
        "SELECT id, party_name, parcel_id, mailing_address, enrichment_data, "
        "skip_trace_status FROM results WHERE id = ANY(:ids)"),
        {"ids": list(ids.values())})).all()
    by_id = {str(r.id): r for r in got}
    return {key: by_id[rid] for key, rid in ids.items()}


async def _logs(db, job_id: str) -> list[str]:
    return list((await db.execute(
        text("SELECT message FROM job_logs WHERE job_id = :j ORDER BY created_at"), {"j": job_id}
    )).scalars())


async def test_a_busy_source_marks_every_unnamed_lead_retryable_and_says_so(
    db, business_user, redis_client, monkeypatch,
):
    _lease(monkeypatch, admitted=False)
    job_id, ids = await _tax_job(db, business_user)
    billed_before = (await db.execute(text(
        "SELECT billed_count, billing_applied_at FROM jobs WHERE id = :j"), {"j": job_id})).first()

    summary = await asyncio.to_thread(_enrich, job_id, redis_client)

    rows = await _rows(db, ids)
    for key in ("mailed", "unmailed", "sibling"):
        ed = rows[key].enrichment_data
        assert rows[key].party_name is None
        assert ed["owner_lookup_deferred"] is True, key
        assert ed["owner_lookup_deferred_reason"] == "not_admitted", key
        assert "owner_lookup_outcome" not in ed
    # Mailing is deferred only where it is actually missing.
    assert rows["unmailed"].enrichment_data["mailing_lookup_deferred"] is True
    assert "mailing_lookup_deferred" not in rows["mailed"].enrichment_data
    assert "mailing_lookup_deferred" not in rows["sibling"].enrichment_data
    # Parcel ids are strings end to end.
    assert rows["unmailed"].parcel_id == _NO_OWNER

    assert summary["mailing_deferred"] == 1          # parcels still missing mailing
    assert summary["owner_deferred"] == 3            # leads still missing an owner
    logs = await _logs(db, job_id)
    assert any(m.startswith("Mailing addresses are still being looked up for 1 of 2 ")
               for m in logs), logs
    assert "Owner names could not be looked up for 3 leads during this run. " \
           "Names already found are saved." in logs
    assert not any("Resolved 0 owner names" in m for m in logs), logs
    assert not any("—" in m for m in logs if "owner" in m.lower())
    assert tuple((await db.execute(text(
        "SELECT billed_count, billing_applied_at FROM jobs WHERE id = :j"),
        {"j": job_id})).first()) == tuple(billed_before)
    assert {r.skip_trace_status for r in rows.values()} == {"not_attempted"}


async def test_an_open_source_names_leads_without_mailing_and_records_a_true_absence(
    db, business_user, redis_client, monkeypatch,
):
    _lease(monkeypatch, admitted=True)
    job_id, ids = await _tax_job(db, business_user)
    fetched: list[str] = []
    pages = {
        _NAMED: _page(_NAMED, "SHAN HOMES2 LLC"),
        _NO_OWNER: _page(_NO_OWNER, None),
    }

    def _get(url, **k):
        pid = url.rsplit("=", 1)[-1]
        fetched.append(pid)
        return _Resp(200, pages[pid])

    monkeypatch.setattr(kca, "safe_get", _get)
    # Phase 1 (property + owner + tax-bill links) is covered by its own tests; make
    # it reach nothing here so the owner-only pass is what decides these rows.
    async def _phase1_found_nothing(parcel_ids, **kw):
        return {}

    monkeypatch.setattr(kca, "_batch_enrich_king_county", _phase1_found_nothing)

    summary = await asyncio.to_thread(_enrich, job_id, redis_client)

    rows = await _rows(db, ids)
    assert rows["mailed"].party_name == "SHAN HOMES2 LLC"
    assert "owner_lookup_deferred" not in rows["mailed"].enrichment_data
    for key in ("unmailed", "sibling"):
        ed = rows[key].enrichment_data
        assert rows[key].party_name is None
        assert ed["owner_lookup_outcome"] == "not_on_record", key
        assert ed["owner_lookup_deferred"] is False, key
    assert sorted(fetched) == sorted([_NAMED, _NO_OWNER])   # one lookup per parcel
    assert not summary.get("owner_deferred")
    logs = await _logs(db, job_id)
    assert "Resolved 1 owner names from 1/2 parcels" in logs
    assert "1 parcel has no owner name on the county record." in logs

    # Idempotent: a second pass does not ask King again about a parcel whose own
    # page already said it has no owner, and changes nothing.
    fetched.clear()
    await asyncio.to_thread(_enrich, job_id, redis_client)
    assert _NO_OWNER not in fetched
    again = await _rows(db, ids)
    assert again["unmailed"].enrichment_data == rows["unmailed"].enrichment_data


async def test_a_name_found_later_clears_the_retry_marker(
    db, business_user, redis_client, monkeypatch,
):
    job_id, ids = await _tax_job(db, business_user)
    _lease(monkeypatch, admitted=False)
    await asyncio.to_thread(_enrich, job_id, redis_client)
    assert (await _rows(db, ids))["mailed"].enrichment_data["owner_lookup_deferred"] is True

    _lease(monkeypatch, admitted=True)
    monkeypatch.setattr(kca, "safe_get",
                        lambda url, **k: _Resp(200, _page(url[-10:], "SHAN HOMES2 LLC")))

    async def _phase1_found_nothing(parcel_ids, **kw):
        return {}

    monkeypatch.setattr(kca, "_batch_enrich_king_county", _phase1_found_nothing)
    await asyncio.to_thread(_enrich, job_id, redis_client)

    ed = (await _rows(db, ids))["mailed"].enrichment_data
    assert ed["owner_lookup_deferred"] is False
    assert "owner_lookup_deferred_reason" not in ed


def test_completion_line_counts_each_field_and_claims_nothing_it_did_not_do():
    from src.workers.tasks_helpers.enrich import enrichment_completion_log

    assert enrichment_completion_log({}) == ("success", "Enrichment complete: addresses added")
    assert enrichment_completion_log({"mailing_deferred": 12}) == (
        "info", "Address enrichment partly complete. "
                "12 mailing address lookups are still pending.")
    assert enrichment_completion_log({"mailing_deferred": 0, "owner_deferred": 16576}) == (
        "info", "Address enrichment partly complete. "
                "16,576 owner names could not be looked up during this run.")
    level, msg = enrichment_completion_log({"mailing_deferred": 1, "owner_deferred": 1})
    assert msg == ("Address enrichment partly complete. 1 mailing address lookup is still "
                   "pending. 1 owner name could not be looked up during this run.")
    assert "—" not in msg and "Property addresses were added" not in msg


async def test_a_parcel_id_that_can_never_be_requested_is_not_promised_a_lookup(
    db, business_user, redis_client, monkeypatch,
):
    _lease(monkeypatch, admitted=True)
    job_id, ids = await _tax_job(db, business_user)
    await db.execute(text("UPDATE results SET parcel_id = 'PENDING-ASSR' WHERE id = :i"),
                     {"i": ids["mailed"]})
    await db.commit()
    monkeypatch.setattr(kca, "safe_get",
                        lambda url, **k: _Resp(200, _page(url[-10:], "SAPOV GEORGE")))

    async def _phase1_found_nothing(parcel_ids, **kw):
        return {}

    monkeypatch.setattr(kca, "_batch_enrich_king_county", _phase1_found_nothing)
    summary = await asyncio.to_thread(_enrich, job_id, redis_client)

    ed = (await _rows(db, ids))["mailed"].enrichment_data
    assert "owner_lookup_deferred" not in ed and "owner_lookup_outcome" not in ed
    assert not summary.get("owner_deferred")


async def test_both_king_passes_ask_about_the_largest_balances_first(
    db, business_user, redis_client, monkeypatch,
):
    """The lookup budget reaches a few hundred parcels; it must be spent on the
    leads the tax plan cap delivers (largest balance first), not in set order."""
    _lease(monkeypatch, admitted=True)
    job_id, ids = await _tax_job(db, business_user)
    for key, amount in (("mailed", "31729.74"), ("unmailed", "14.71"), ("sibling", "14.71")):
        await db.execute(text("UPDATE results SET delinquent_amount = :a WHERE id = :i"),
                         {"a": amount, "i": ids[key]})
    await db.commit()
    phase1_order: list[list[str]] = []
    owner_order: list[str] = []

    async def _phase1_records_order(parcel_ids, **kw):
        phase1_order.append(list(parcel_ids))
        return {}

    def _get(url, **k):
        owner_order.append(url.rsplit("=", 1)[-1])
        return _Resp(200, _page(url[-10:], None))

    monkeypatch.setattr(kca, "_batch_enrich_king_county", _phase1_records_order)
    monkeypatch.setattr(kca, "safe_get", _get)

    await asyncio.to_thread(_enrich, job_id, redis_client)

    # The reverse of parcel-id order, so a plain sort cannot pass this.
    assert phase1_order == [[_NAMED, _NO_OWNER]]
    assert owner_order == [_NAMED, _NO_OWNER]
