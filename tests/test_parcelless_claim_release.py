"""A parcel-less filing a run could not address must not keep its claim.

Island probate (owner, 2026-10-04): job 6b1f3445's 71 no-address, parcel-less rows held
66 WEAK (name|date) claims. transfer_undelivered_claims never moves a weak claim, so a
re-run would have flagged the same filings "already delivered" and hidden them though
nobody was ever given them. release_parcelless_no_address_claims ends that at the end of
every run; the name pass now asks never-answered names first and paces Island at 10 s.

Real DB through the conftest fixtures; the county portal is the only thing scripted.
"""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.db.models import DeliveredRecord, Job, Result, ScraperConfig
from src.scrapers.enrichment import pacs

NOW = datetime.now(UTC)


async def _config(db, user) -> ScraperConfig:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="island probate claims",
        county="island", state="WA", record_type="probate", fields=["party_name"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(cfg)
    await db.commit()
    return cfg


async def _job(db, user, cfg, *, created_at=NOW, status="enriching") -> str:
    jid = str(uuid.uuid4())
    db.add(Job(id=jid, user_id=user.id, scraper_config_id=cfg.id, status=status,
               trigger="manual", record_count=0, billed_count=0, created_at=created_at))
    await db.commit()
    return jid


async def _row(db, user, jid, *, h, parcel=None, prop=None, mail=None, claim=True,
               claim_parcel=None, enrichment=None, name=None) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=jid, party_name=name or f"OWNER {h[:6]}",
                  parcel_id=parcel, property_address=prop, mailing_address=mail,
                  dedup_hash=h, is_duplicate=False, doc_type="Transfer on Death Deed",
                  enrichment_data=enrichment or {"instrument_number": "1"},
                  skip_trace_status="not_attempted"))
    await db.commit()
    if claim:
        db.add(DeliveredRecord(id=str(uuid.uuid4()), user_id=user.id, dedup_hash=h,
                               first_result_id=rid, first_job_id=jid,
                               parcel_id=claim_parcel, property_address=None))
        await db.commit()
    return rid


def _release(jid, uid) -> int:
    from src.db.session import system_sync_session
    from src.workers.tasks_helpers.dedup import release_parcelless_no_address_claims

    with system_sync_session() as sdb:
        return release_parcelless_no_address_claims(sdb, jid, uid)


async def _claim_count(db, h) -> int:
    return (await db.execute(text("SELECT count(*) FROM delivered_records WHERE dedup_hash = :h"),
                             {"h": h})).scalar_one()


async def _reason(db, rid):
    return (await db.execute(text("SELECT is_duplicate, duplicate_reason FROM results WHERE id = :i"),
                             {"i": rid})).one()


async def test_only_parcelless_unaddressed_weak_claims_of_this_run_are_released(db, business_user):
    cfg = await _config(db, business_user)
    jid = await _job(db, business_user, cfg)
    other = await _job(db, business_user, cfg, created_at=NOW - timedelta(days=3), status="done")
    h = {k: uuid.uuid4().hex for k in ("trap", "trap2", "parcel", "addr", "mixed", "strong", "foreign")}

    trap = await _row(db, business_user, jid, h=h["trap"])
    trap_sibling = await _row(db, business_user, jid, h=h["trap"], claim=False)  # same filing, same run
    await _row(db, business_user, jid, h=h["trap2"])
    # Kept: a parcel (mailing recovery may still address it), an address, a filing
    # another row of this run addressed, a strong claim, a claim anchored elsewhere.
    parcel = await _row(db, business_user, jid, h=h["parcel"], parcel="R123456")
    await _row(db, business_user, jid, h=h["addr"], prop="1 FIR LN")
    await _row(db, business_user, jid, h=h["mixed"])
    await _row(db, business_user, jid, h=h["mixed"], claim=False, mail="PO BOX 1")
    await _row(db, business_user, jid, h=h["strong"], claim_parcel="R999")
    await _row(db, business_user, other, h=h["foreign"])
    await _row(db, business_user, jid, h=h["foreign"], claim=False)

    released = await asyncio.to_thread(_release, jid, business_user.id)

    assert released == 2
    assert await _claim_count(db, h["trap"]) == 0 and await _claim_count(db, h["trap2"]) == 0
    for k in ("parcel", "addr", "mixed", "strong", "foreign"):
        assert await _claim_count(db, h[k]) == 1, k
    # Both rows of the released filing are hidden for good, as a transfer hides an anchor.
    assert tuple(await _reason(db, trap)) == (True, "superseded")
    assert tuple(await _reason(db, trap_sibling)) == (True, "superseded")
    assert tuple(await _reason(db, parcel)) == (False, None)


async def test_a_second_run_is_not_told_the_filing_was_already_delivered(db, business_user):
    """The whole point: after release, the next run claims the filing itself."""
    cfg = await _config(db, business_user)
    first = await _job(db, business_user, cfg, created_at=NOW - timedelta(days=1))
    h = uuid.uuid4().hex
    await _row(db, business_user, first, h=h)
    assert await asyncio.to_thread(_release, first, business_user.id) == 1
    assert await _claim_count(db, h) == 0  # nothing left to collide with


async def test_releasing_twice_is_harmless(db, business_user):
    cfg = await _config(db, business_user)
    jid = await _job(db, business_user, cfg)
    await _row(db, business_user, jid, h=uuid.uuid4().hex)
    assert await asyncio.to_thread(_release, jid, business_user.id) == 1
    assert await asyncio.to_thread(_release, jid, business_user.id) == 0


def test_island_is_paced_slower(monkeypatch):
    sleeps: list = []
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(round(s, 1)))
    monkeypatch.setattr(pacs, "NAME_JITTER_S", 0.0)
    monkeypatch.setattr(pacs, "lookup_pacs_by_name", lambda url, n: (pacs.LOOKUP_NO_MATCH, None))
    pacs.batch_lookup_pacs_by_name("https://assessor.islandcountywa.gov/propertyaccess/PropertySearch.aspx?cid=0", ["A", "B"])
    pacs.batch_lookup_pacs_by_name("https://propertysearch.co.benton.wa.us/propertyaccess/PropertySearch.aspx?cid=0", ["A", "B"])
    assert sleeps == [10.0, pacs.NAME_PACE_S]


async def test_never_answered_names_are_asked_first(db, business_user, redis_client, monkeypatch):
    """Without this, a pass that stops early re-asks the same first names every run."""
    cfg = await _config(db, business_user)
    earlier = await _job(db, business_user, cfg, created_at=NOW - timedelta(days=1), status="done")
    h_answered, h_fresh = uuid.uuid4().hex, uuid.uuid4().hex
    await _row(db, business_user, earlier, h=h_answered, claim=False, name="AARON ANSWERED",
               enrichment={"pacs_name_lookup": "no_match"})
    jid = await _job(db, business_user, cfg)
    await _row(db, business_user, jid, h=h_answered, claim=False, name="AARON ANSWERED")
    await _row(db, business_user, jid, h=h_fresh, claim=False, name="ZED FRESH")

    asked: list = []

    def scripted(url, names, source_key=None):
        asked.extend(names)
        return [(pacs.LOOKUP_NO_MATCH, None)] * len(names)

    monkeypatch.setattr(pacs, "batch_lookup_pacs_by_name", scripted)

    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _run_inline_enrichment

        with system_sync_session() as sdb:
            job = sdb.get(Job, jid)
            _run_inline_enrichment(sdb, job, redis_client, jid, sdb.get(ScraperConfig, cfg.id), summary={})

    await asyncio.to_thread(_go)
    assert asked == ["ZED FRESH", "AARON ANSWERED"]


async def test_a_claim_another_run_is_flagged_against_is_kept(db, business_user):
    """Releasing it would leave that run's rows marked delivered by nobody (Codex P1)."""
    cfg = await _config(db, business_user)
    jid = await _job(db, business_user, cfg)
    other = await _job(db, business_user, cfg, created_at=NOW + timedelta(minutes=1), status="done")
    h = uuid.uuid4().hex
    await _row(db, business_user, jid, h=h)
    flagged = await _row(db, business_user, other, h=h, claim=False)
    await db.execute(text("UPDATE results SET is_duplicate = true, duplicate_reason = 'prior_run' "
                          "WHERE id = :i"), {"i": flagged})
    await db.commit()
    assert await asyncio.to_thread(_release, jid, business_user.id) == 0
    assert await _claim_count(db, h) == 1


async def test_blank_values_on_the_claim_count_as_none(db, business_user):
    cfg = await _config(db, business_user)
    jid = await _job(db, business_user, cfg)
    h = uuid.uuid4().hex
    await _row(db, business_user, jid, h=h, claim_parcel="  ")
    assert await asyncio.to_thread(_release, jid, business_user.id) == 1


def test_no_lease_means_nothing_is_asked(monkeypatch):
    """Another run (or the parcel adapter) holds the portal: skip, don't pile on."""
    from src.scrapers.enrichment import source_admission

    class Busy:
        admitted = False

        def __init__(self, key, **kw):
            assert key == "pacs_island"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(source_admission, "SourceAdmission", Busy)
    asked: list = []
    monkeypatch.setattr(pacs, "lookup_pacs_by_name", lambda url, n: asked.append(n) or (pacs.LOOKUP_FOUND, {}))
    out = pacs.batch_lookup_pacs_by_name("https://assessor.islandcountywa.gov/x?cid=0", ["A", "B"],
                                         source_key="pacs_island")
    assert asked == [] and [o for o, _ in out] == [pacs.LOOKUP_SKIPPED] * 2
