"""One property, one charge, even when a run scrapes it twice.

`dedup_hash` (parcel|address) is the app-wide BILLING key, but the cross-job
dedup only records that a hash was CLAIMED once. It leaves same-JOB rows sharing
a hash all is_duplicate=false, and billing counts ROWS -- so a run that scraped
two filings on one property charged for both.

trustee_sale has collapsed its own siblings since 2026-07-03. Nothing else did.
An audit on 2026-09-08 found 8 completed probate and pre_foreclosure jobs that
had charged 50 records for properties already billed in the same run, including
a 122-record job covering 120 properties.

Real DB (conftest `db` fixture) -- no mocks.
"""
import uuid

from src.db.models import Job, Result, ScraperConfig, User
from src.workers.tasks_helpers.dedup import collapse_same_run_siblings


async def _job(db, user: User, config: ScraperConfig) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status="done", trigger="manual"))
    await db.commit()
    return job_id


async def _row(db, job_id, user_id, dedup_hash, **kw) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, job_id=job_id, user_id=user_id, dedup_hash=dedup_hash,
                  is_duplicate=False, **kw))
    await db.commit()
    return rid


async def _collapse(db, job_id, user_id) -> int:
    n = await db.run_sync(lambda s: collapse_same_run_siblings(s, job_id, user_id))
    await db.commit()
    return n


async def _state(db, ids):
    out = {}
    for rid in ids:
        r = await db.get(Result, rid)
        await db.refresh(r)
        out[rid] = (r.is_duplicate, r.duplicate_reason)
    return out


async def test_two_filings_on_one_property_bill_once(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """The defect, reduced. Both rows survived the cross-job dedup and both billed."""
    job_id = await _job(db, starter_user, scraper_config)
    h = uuid.uuid4().hex
    a = await _row(db, job_id, starter_user.id, h, party_name="A",
                   property_address="1 MAIN ST")
    b = await _row(db, job_id, starter_user.id, h, party_name="B",
                   property_address="1 MAIN ST")

    assert await _collapse(db, job_id, starter_user.id) == 1

    st = await _state(db, [a, b])
    kept = [r for r, (dup, _) in st.items() if not dup]
    gone = [r for r, (dup, _) in st.items() if dup]
    assert len(kept) == 1 and len(gone) == 1
    # Never "already delivered" — the customer is seeing these for the first time.
    assert st[gone[0]][1] == "same_run"


async def test_distinct_properties_are_never_collapsed(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    job_id = await _job(db, starter_user, scraper_config)
    ids = [await _row(db, job_id, starter_user.id, uuid.uuid4().hex,
                      party_name=f"O{i}", property_address=f"{i} MAIN ST")
           for i in range(3)]

    assert await _collapse(db, job_id, starter_user.id) == 0
    assert all(not dup for dup, _ in (await _state(db, ids)).values())


async def test_the_most_complete_row_survives(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """The losers stop being delivered, so whatever they alone carried is lost.
    Keep the row a customer can actually act on."""
    job_id = await _job(db, starter_user, scraper_config)
    h = uuid.uuid4().hex
    bare = await _row(db, job_id, starter_user.id, h, party_name="BARE")
    full = await _row(db, job_id, starter_user.id, h, party_name="FULL",
                      property_address="9 MAIN ST", mailing_address="PO BOX 1",
                      parcel_id="0123456")

    assert await _collapse(db, job_id, starter_user.id) == 1
    st = await _state(db, [bare, full])
    assert st[full][0] is False, "the row with address + parcel must survive"
    assert st[bare][0] is True


async def test_collapse_is_idempotent(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """A watchdog re-run must not shrink the delivered set every pass."""
    job_id = await _job(db, starter_user, scraper_config)
    h = uuid.uuid4().hex
    for i in range(3):
        await _row(db, job_id, starter_user.id, h, party_name=f"P{i}",
                   property_address="7 MAIN ST")

    assert await _collapse(db, job_id, starter_user.id) == 2
    assert await _collapse(db, job_id, starter_user.id) == 0


async def test_collapse_never_touches_another_account(
    db, starter_user: User, business_user: User, scraper_config: ScraperConfig,
):
    """The same parcel is routinely scraped by many accounts in the same window."""
    other = ScraperConfig(id=str(uuid.uuid4()), user_id=business_user.id,
                          name="theirs", county="Pierce", state="WA",
                          record_type="probate")
    db.add(other)
    await db.commit()
    theirs_job = await _job(db, business_user, other)
    mine_job = await _job(db, starter_user, scraper_config)
    h = uuid.uuid4().hex
    t1 = await _row(db, theirs_job, business_user.id, h, party_name="T1",
                    property_address="3 MAIN ST")
    t2 = await _row(db, theirs_job, business_user.id, h, party_name="T2",
                    property_address="3 MAIN ST")
    await _row(db, mine_job, starter_user.id, h, party_name="M",
               property_address="3 MAIN ST")

    assert await _collapse(db, mine_job, starter_user.id) == 0
    assert all(not dup for dup, _ in (await _state(db, [t1, t2])).values())
