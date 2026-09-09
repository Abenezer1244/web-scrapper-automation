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
from src.workers.property_identity import legacy_strong_signature
from src.workers.tasks_helpers.dedup import collapse_same_run_siblings


def _strong(parcel: str, address: str) -> str:
    """The REAL hash the worker would store for these values.

    Tests must not invent a dedup_hash: the collapse only groups rows whose
    stored hash still equals the strong signature of their current parcel and
    address, which is what stops an enriched weak-hash row collapsing under
    its old NAME|DATE key.
    """
    h = legacy_strong_signature(parcel, address)
    assert h is not None, "test inputs must form a strong identity"
    return h


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
    h = _strong("0123456", "1 MAIN ST")
    a = await _row(db, job_id, starter_user.id, h, party_name="A",
                   parcel_id="0123456", property_address="1 MAIN ST")
    b = await _row(db, job_id, starter_user.id, h, party_name="B",
                   parcel_id="0123456", property_address="1 MAIN ST")

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
    Keep the row a customer can act on.

    Both rows necessarily carry the same parcel and property address -- that is
    what produced the shared strong hash -- so completeness is decided by what
    else they have.
    """
    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", "9 MAIN ST")
    thin = await _row(db, job_id, starter_user.id, h, party_name="",
                      parcel_id="0123456", property_address="9 MAIN ST",
                      date_recorded="2026-01-01")
    full = await _row(db, job_id, starter_user.id, h, party_name="FULL",
                      parcel_id="0123456", property_address="9 MAIN ST",
                      mailing_address="PO BOX 1", date_recorded="2026-06-01")

    assert await _collapse(db, job_id, starter_user.id) == 1
    st = await _state(db, [thin, full])
    assert st[full][0] is False, "the row with a mailing address must survive"
    assert st[thin][0] is True


async def test_collapse_is_idempotent(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """A watchdog re-run must not shrink the delivered set every pass."""
    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", "7 MAIN ST")
    for i in range(3):
        await _row(db, job_id, starter_user.id, h, party_name=f"P{i}",
                   parcel_id="0123456", property_address="7 MAIN ST")

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
    h = _strong("0123456", "3 MAIN ST")
    t1 = await _row(db, theirs_job, business_user.id, h, party_name="T1",
                    parcel_id="0123456", property_address="3 MAIN ST")
    t2 = await _row(db, theirs_job, business_user.id, h, party_name="T2",
                    parcel_id="0123456", property_address="3 MAIN ST")
    await _row(db, mine_job, starter_user.id, h, party_name="M",
               parcel_id="0123456", property_address="3 MAIN ST")

    assert await _collapse(db, mine_job, starter_user.id) == 0
    assert all(not dup for dup, _ in (await _state(db, [t1, t2])).values())


# ─── Codex P1s: what the collapse must NOT do ──────────────────────────────────

async def test_weak_name_date_hashes_are_never_collapsed(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """A dedup_hash is only a PROPERTY when it came from parcel|address. Without a
    usable parcel and address the worker falls back to a NAME|DATE hash, which
    identifies a FILING: two addressless filings by the same party on the same day
    share it without being the same lead. Collapsing them would silently stop
    delivering one."""
    job_id = await _job(db, starter_user, scraper_config)
    weak = uuid.uuid4().hex  # same hash, but neither row has property identity
    a = await _row(db, job_id, starter_user.id, weak, party_name="SMITH",
                   date_recorded="2026-01-02", mailing_address="PO BOX 1")
    b = await _row(db, job_id, starter_user.id, weak, party_name="SMITH",
                   date_recorded="2026-01-02", mailing_address="PO BOX 2")

    assert await _collapse(db, job_id, starter_user.id) == 0
    assert all(not dup for dup, _ in (await _state(db, [a, b])).values())


async def test_an_undeliverable_row_never_wins_over_a_usable_one(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """'(enrichment unavailable)' is a populated string but is not an address
    anywhere the customer looks. Letting it win would hide the usable sibling
    while the property stayed claimed."""
    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", "5 MAIN ST")
    placeholder = await _row(db, job_id, starter_user.id, h, party_name="A",
                             parcel_id="0123456",
                             property_address="5 MAIN ST",
                             mailing_address="(enrichment unavailable)",
                             date_recorded="2026-01-01")
    usable = await _row(db, job_id, starter_user.id, h, party_name="B",
                        parcel_id="0123456", property_address="5 MAIN ST",
                        mailing_address="PO BOX 7",
                        date_recorded="2026-06-01")

    assert await _collapse(db, job_id, starter_user.id) == 1
    st = await _state(db, [placeholder, usable])
    assert st[usable][0] is False, "the deliverable row must survive"
    assert st[placeholder][0] is True


async def test_enrichment_filling_an_address_cannot_retro_collapse_a_weak_hash(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """The hash is computed at INSERT time; enrichment mutates property_address
    afterwards. On a watchdog retry, two rows that hashed weakly as NAME|DATE and
    have since had addresses filled in must NOT collapse under that old hash --
    they were never proven to be the same property.

    Simulated the way it actually happens: rows share a weak hash, and the
    address they now carry would hash strongly to something else entirely.
    """
    job_id = await _job(db, starter_user, scraper_config)
    weak = uuid.uuid4().hex
    a = await _row(db, job_id, starter_user.id, weak, party_name="SMITH",
                   date_recorded="2026-01-02", parcel_id="0123456",
                   property_address="11 MAIN ST")   # enrichment filled this in
    b = await _row(db, job_id, starter_user.id, weak, party_name="SMITH",
                   date_recorded="2026-01-02", parcel_id="0123456",
                   property_address="11 MAIN ST")

    assert await _collapse(db, job_id, starter_user.id) == 0
    assert all(not dup for dup, _ in (await _state(db, [a, b])).values())
