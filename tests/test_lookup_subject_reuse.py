"""Reuse is keyed on the OWNER, not just the address (migration 098).

The bug: within the 90-day window a lead could inherit the previous owner's
phone, because every reuse path keyed on the address alone. Probate is the
common case, not the exotic one -- the deceased owner is traced, an heir is
scraped later, and the heir's lead is served the dead owner's contacts.

Each test here defends a specific finding. Codex round 13 made four of them a
condition of its PASS; round 14 (consult before code) found two P1s that the
first thirteen rounds missed. The round-14 names say which.

Real DB (conftest). Tracerfy is never reached.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from src.config import settings
from src.db.models import (
    Job,
    PendingSkipTraceRow,
    Result,
    ScraperConfig,
    SkipTraceCache,
)
from src.db.session import system_sync_session
from src.scrapers.enrichment.skip_trace import (
    address_cache_key,
    lookup_subject_key,
)
from src.workers.property_identity import legacy_strong_signature

# "LAST FIRST M" is the WA recorder convention select_traceable_owner expects.
DECEASED = "SAARENAS AVELINO G"        # -> AVELINO SAARENAS
HEIR = "JONES ROBERT L"                # -> ROBERT JONES
CITY, STATE = "VANCOUVER", "WA"


@pytest.fixture
def _skip_trace_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


def _address(n: int) -> str:
    return f"{2200 + n} CEDAR ST"


def _parcel(n: int) -> str:
    return f"77123{n:05d}"


def _hash(n: int) -> str:
    return legacy_strong_signature(_parcel(n), _address(n))


def _subject(user_id, n: int, first, last, trace_type="normal") -> str:
    return lookup_subject_key(
        user_id, _address(n), CITY, STATE, trace_type, first, last,
    )


def _run(user_id, *, skip_on=True, status="enriching") -> str:
    sc_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    with system_sync_session() as db:
        db.add(ScraperConfig(
            id=sc_id, user_id=user_id, name="subject reuse", county="clark",
            state="WA", record_type="probate", fields=[], enrichment=[],
            schedule={"frequency": "manual"}, deliver={"formats": ["csv"], "emails": []},
            skip_trace_enabled=skip_on,
        ))
        db.flush()
        db.add(Job(id=job_id, user_id=user_id, scraper_config_id=sc_id,
                   status=status, trigger="manual"))
        db.commit()
    return job_id


def _lead(user_id, job_id, n, *, party=DECEASED, dup=False, status="not_attempted",
          phone=None, subject_hash=None, traced_days_ago=None) -> str:
    rid = str(uuid.uuid4())
    attempted = (datetime.now(UTC) - timedelta(days=traced_days_ago)
                 if traced_days_ago is not None else None)
    with system_sync_session() as db:
        db.add(Result(
            id=rid, job_id=job_id, user_id=user_id, party_name=party,
            parcel_id=_parcel(n), property_address=_address(n),
            property_city=CITY, property_state=STATE, property_zip="98661",
            dedup_hash=_hash(n), is_duplicate=dup,
            duplicate_reason="prior_run" if dup else None,
            skip_trace_status=status, skip_trace_attempted_at=attempted,
            skip_trace_source="lookup" if status in ("hit", "miss") else None,
            skip_trace_subject_hash=subject_hash,
            phone=phone, phones=[{"number": phone, "type": "Mobile"}] if phone else None,
        ))
        db.flush()
        if not dup:
            db.execute(text(
                "INSERT INTO delivered_records (id, user_id, dedup_hash, first_result_id, "
                "first_job_id, parcel_id, property_address) "
                "VALUES (:id, :u, :h, :r, :j, :p, :a) "
                "ON CONFLICT (user_id, dedup_hash) DO NOTHING"),
                {"id": str(uuid.uuid4()), "u": user_id, "h": _hash(n), "r": rid,
                 "j": job_id, "p": _parcel(n), "a": _address(n)})
        db.commit()
    return rid


def _cache_row(key: str, phone: str, *, days_old: float = 1) -> None:
    with system_sync_session() as db:
        db.add(SkipTraceCache(
            address_hash=key, phone=phone, phone_type="Mobile",
            phones=[{"number": phone, "type": "Mobile"}],
            fetched_at=datetime.now(UTC) - timedelta(days=days_old),
        ))
        db.commit()


def _pending_row(user_id, job_id, result_id, n, *, first, last,
                 trace_type="normal", status="queued") -> str:
    pid = str(uuid.uuid4())
    with system_sync_session() as db:
        db.add(PendingSkipTraceRow(
            id=pid, job_id=job_id, result_id=result_id, user_id=user_id,
            property_address=_address(n), city=CITY, state=STATE, zip="98661",
            first_name=first, last_name=last, trace_type=trace_type, status=status,
        ))
        if status == "queued":
            db.execute(text("UPDATE results SET skip_trace_status='queued' WHERE id=:r"),
                       {"r": result_id})
        db.commit()
    return pid


def _enqueue(job_id, redis_client) -> None:
    from src.workers.tasks_helpers.enrich import (
        _enqueue_skip_trace_rows,
        _reuse_enrichment_for_duplicates,
    )

    with system_sync_session() as db:
        job = db.get(Job, job_id)
        cfg = db.get(ScraperConfig, job.scraper_config_id)
        _reuse_enrichment_for_duplicates(db, job, job_id)
        _enqueue_skip_trace_rows(db, job, redis_client, job_id, cfg)


def _pending_count(result_id) -> int:
    with system_sync_session() as db:
        return db.execute(
            select(func.count()).select_from(PendingSkipTraceRow)
            .where(PendingSkipTraceRow.result_id == result_id)
        ).scalar_one()


def _row(result_id) -> Result:
    with system_sync_session() as db:
        r = db.get(Result, result_id)
        db.expunge(r)
        return r


# ─── The bug itself, at the enqueue cache read (path 1) ───────────────────────

async def test_an_heir_does_not_inherit_the_deceased_owners_phone(
    business_user, redis_client, _skip_trace_on,
):
    """The reason this phase exists. The account paid for the deceased owner's
    contacts at this address; the heir's lead must not be served them."""
    _cache_row(_subject(business_user.id, 1, "AVELINO", "SAARENAS"), "2065550101")
    job = _run(business_user.id)
    heir = _lead(business_user.id, job, 1, party=HEIR)

    _enqueue(job, redis_client)

    assert _pending_count(heir) == 1, "the heir's lead must be looked up, not inherited"
    r = _row(heir)
    assert r.phone is None
    assert r.skip_trace_status == "queued"


async def test_the_same_owner_still_reuses_and_buys_nothing(
    business_user, redis_client, _skip_trace_on,
):
    """The other half: owner isolation must not cost the account its own reuse."""
    _cache_row(_subject(business_user.id, 2, "AVELINO", "SAARENAS"), "2065550102")
    job = _run(business_user.id)
    same = _lead(business_user.id, job, 2, party=DECEASED)

    _enqueue(job, redis_client)

    assert _pending_count(same) == 0, "the same owner's answer must still be reused"
    r = _row(same)
    assert r.phone == "2065550102"
    assert r.skip_trace_source == "reused"
    assert r.skip_trace_subject_hash == _subject(
        business_user.id, 2, "AVELINO", "SAARENAS")


async def test_an_advanced_answer_is_reused_across_owners_at_one_address(
    business_user, redis_client, _skip_trace_on,
):
    """Owner decision D1: an advanced trace sends NO name, so owner isolation
    cannot apply to what came back. It is reused per address, and documented."""
    _cache_row(
        _subject(business_user.id, 3, None, None, "advanced"), "2065550103")
    job = _run(business_user.id)
    # An entity name routes to an advanced trace.
    entity = _lead(business_user.id, job, 3, party="CEDAR HOLDINGS LLC")

    _enqueue(job, redis_client)

    assert _pending_count(entity) == 0
    assert _row(entity).phone == "2065550103"


async def test_a_normal_trace_never_reuses_an_advanced_answer(
    business_user, redis_client, _skip_trace_on,
):
    """trace_type is in the key, so a 1-credit answer and a 2-credit answer are
    never merged onto one another."""
    _cache_row(
        _subject(business_user.id, 4, None, None, "advanced"), "2065550104")
    job = _run(business_user.id)
    named = _lead(business_user.id, job, 4, party=DECEASED)

    _enqueue(job, redis_client)

    assert _pending_count(named) == 1
    assert _row(named).phone is None


# ─── Round 13 condition: nothing reads a legacy key any more ──────────────────

async def test_a_legacy_address_only_cache_row_is_reused_by_nobody(
    business_user, redis_client, _skip_trace_on,
):
    """Codex round 13 made this a condition of its PASS. A pre-098 row cannot say
    whose answer it holds, so reading one is the leak the cutover removes. It must
    be invisible to the enqueue cache read AND to the dispatcher's sweep, and the
    legacy mailing-locality fallback must be gone with it."""
    from src.workers.skip_trace_dispatcher import _settle_queued_from_known_answers

    _cache_row(address_cache_key(business_user.id, _address(5), CITY, STATE),
               "2065550105")
    job = _run(business_user.id)
    lead = _lead(business_user.id, job, 5, party=DECEASED)

    _enqueue(job, redis_client)
    assert _pending_count(lead) == 1, "the enqueue read a legacy key"
    assert _row(lead).phone is None

    with system_sync_session() as db:
        _settle_queued_from_known_answers(db)
    r = _row(lead)
    assert r.phone is None, "the dispatcher sweep read a legacy key"
    assert r.skip_trace_status == "queued"


# ─── Round 13 condition: a row queued before the cutover settles on v2 ────────

async def test_a_pending_row_queued_before_the_cutover_settles_on_its_own_subject(
    business_user, _skip_trace_on,
):
    """Pending rows outlive the deploy. The dispatcher keys them from the row's
    OWN stored fields, so a row queued before the cutover is settled against the
    v2 subject it was actually submitted with, and a different owner's answer at
    that address does not settle it."""
    from src.workers.skip_trace_dispatcher import _settle_queued_from_known_answers

    job = _run(business_user.id)
    mine = _lead(business_user.id, job, 6, party=DECEASED)
    _pending_row(business_user.id, job, mine, 6, first="AVELINO", last="SAARENAS")

    # Someone else's answer at the same address: must not settle this row.
    _cache_row(_subject(business_user.id, 6, "ROBERT", "JONES"), "2065550199")
    with system_sync_session() as db:
        _settle_queued_from_known_answers(db)
    assert _row(mine).skip_trace_status == "queued"

    # Its own subject's answer does settle it, at no charge.
    _cache_row(_subject(business_user.id, 6, "AVELINO", "SAARENAS"), "2065550106")
    with system_sync_session() as db:
        _settle_queued_from_known_answers(db)
    r = _row(mine)
    assert (r.skip_trace_status, r.phone) == ("hit", "2065550106")
    assert r.skip_trace_source == "reused"
    assert r.skip_trace_subject_hash == _subject(
        business_user.id, 6, "AVELINO", "SAARENAS")


# ─── Round 14-A: the subject key alone would double-charge ────────────────────

async def test_two_owners_at_one_address_do_not_go_out_in_one_batch(
    business_user, _skip_trace_on,
):
    """Round 14-A, a P1 the first thirteen rounds missed.

    Provider attribution is address-only: `_attribution_is_safe` refuses the whole
    group when two answers come back for one address. Under the old key the
    in-flight hold deduped on the address, so this never happened. Distinct v2
    subjects would send both, get both refused, and charge for both while
    answering nobody. The submission key keeps one address per batch.
    """
    from src.workers.skip_trace_dispatcher import _hold_answers_in_flight

    job = _run(business_user.id)
    a = _lead(business_user.id, job, 7, party=DECEASED)
    b_job = _run(business_user.id)
    b = _lead(business_user.id, b_job, 7, party=HEIR, dup=True)
    pa = _pending_row(business_user.id, job, a, 7, first="AVELINO", last="SAARENAS")
    pb = _pending_row(business_user.id, b_job, b, 7, first="ROBERT", last="JONES")

    with system_sync_session() as db:
        rows = db.execute(
            select(PendingSkipTraceRow)
            .where(PendingSkipTraceRow.id.in_([pa, pb]))
            .order_by(PendingSkipTraceRow.property_address)
        ).scalars().all()
        assert len(rows) == 2
        kept, held = _hold_answers_in_flight(db, rows)

    assert (len(kept), held) == (1, 1), (
        "two owners at one address went out together: the provider returns two "
        "rows for that address, attribution refuses both, and both are charged"
    )


async def test_two_tenants_at_one_address_do_not_go_out_in_one_batch(
    business_user, starter_user, db, _skip_trace_on,
):
    """The submission key takes no user_id on purpose. A batch spans tenants and
    provider attribution carries no tenant identifier either, so a cross-tenant
    pair at one address is refused exactly the same way."""
    from src.db.models import User
    from src.workers.skip_trace_dispatcher import _hold_answers_in_flight

    other = await db.get(User, starter_user.id)
    other.plan = "business"
    await db.commit()

    mine_job = _run(business_user.id)
    mine = _lead(business_user.id, mine_job, 8, party=DECEASED)
    theirs_job = _run(other.id)
    theirs = _lead(other.id, theirs_job, 8, party=HEIR)
    p1 = _pending_row(business_user.id, mine_job, mine, 8,
                      first="AVELINO", last="SAARENAS")
    p2 = _pending_row(other.id, theirs_job, theirs, 8, first="ROBERT", last="JONES")

    with system_sync_session() as s:
        rows = s.execute(
            select(PendingSkipTraceRow).where(PendingSkipTraceRow.id.in_([p1, p2]))
        ).scalars().all()
        kept, held = _hold_answers_in_flight(s, rows)

    assert (len(kept), held) == (1, 1)


async def test_the_held_row_is_not_stranded(business_user, _skip_trace_on):
    """Holding is not losing. The second subject stays 'queued' and goes out on a
    later tick; nothing is cancelled and nothing is charged."""
    from src.workers.skip_trace_dispatcher import _hold_answers_in_flight

    job = _run(business_user.id)
    a = _lead(business_user.id, job, 9, party=DECEASED)
    b_job = _run(business_user.id)
    b = _lead(business_user.id, b_job, 9, party=HEIR, dup=True)
    _pending_row(business_user.id, job, a, 9, first="AVELINO", last="SAARENAS")
    pb = _pending_row(business_user.id, b_job, b, 9, first="ROBERT", last="JONES")

    with system_sync_session() as db:
        rows = db.execute(
            select(PendingSkipTraceRow).where(
                PendingSkipTraceRow.result_id.in_([a, b]))
        ).scalars().all()
        _hold_answers_in_flight(db, rows)
        still = db.get(PendingSkipTraceRow, pb)
        assert still.status == "queued"


# ─── Round 14-B: the dedup_hash passes need durable evidence ──────────────────

async def test_a_source_whose_party_name_was_rewritten_does_not_donate(
    business_user, redis_client, _skip_trace_on,
):
    """Round 14-B, the finding that forced migration 098.

    Owner recovery rewrites `party_name` AFTER a lookup settles. Recomputing the
    source's subject then yields the CURRENT owner, which matches the target, while
    the phone stored beside it still belongs to the PREVIOUS one. A recomputation
    check passes here and copies exactly the leak it was added to stop. The durable
    subject hash says who the answer was really bought for, so this refuses.
    """
    first = _run(business_user.id, skip_on=False)
    # Bought for the deceased owner...
    source = _lead(business_user.id, first, 10, party=DECEASED, status="hit",
                   phone="2065550110", traced_days_ago=1,
                   subject_hash=_subject(business_user.id, 10, "AVELINO", "SAARENAS"))
    # ...then owner recovery rewrote the name on that same row to the heir.
    with system_sync_session() as db:
        db.execute(text("UPDATE results SET party_name = :p WHERE id = :i"),
                   {"p": HEIR, "i": source})
        db.commit()

    again = _run(business_user.id)
    target = _lead(business_user.id, again, 10, party=HEIR, dup=True)

    _enqueue(again, redis_client)

    r = _row(target)
    assert r.phone is None, (
        "the heir's lead received the deceased owner's phone: the source's subject "
        "was recomputed from a party_name that had been rewritten"
    )
    assert _pending_count(target) == 1


async def test_a_pre_098_answer_donates_nothing(
    business_user, redis_client, _skip_trace_on,
):
    """A row settled before 098 has a NULL subject hash. It cannot say whose answer
    it is, so it fails closed rather than guessing."""
    first = _run(business_user.id, skip_on=False)
    _lead(business_user.id, first, 11, party=DECEASED, status="hit",
          phone="2065550111", traced_days_ago=1, subject_hash=None)
    again = _run(business_user.id)
    target = _lead(business_user.id, again, 11, party=DECEASED, dup=True)

    _enqueue(again, redis_client)

    assert _row(target).phone is None
    assert _pending_count(target) == 1


async def test_an_exact_subject_match_still_donates(
    business_user, redis_client, _skip_trace_on,
):
    """Failing closed must not cost the account the reuse it already paid for:
    the same owner at the same property still copies, and buys nothing."""
    first = _run(business_user.id, skip_on=False)
    _lead(business_user.id, first, 12, party=DECEASED, status="hit",
          phone="2065550112", traced_days_ago=1,
          subject_hash=_subject(business_user.id, 12, "AVELINO", "SAARENAS"))
    again = _run(business_user.id)
    target = _lead(business_user.id, again, 12, party=DECEASED, dup=True)

    _enqueue(again, redis_client)

    r = _row(target)
    assert (r.phone, r.skip_trace_status) == ("2065550112", "hit")
    assert r.skip_trace_source == "reused"
    assert _pending_count(target) == 0


async def test_the_later_pass_picks_the_newest_per_subject_not_per_property(
    business_user, redis_client, _skip_trace_on,
):
    """`later_sql` used to take the newest settled answer per dedup_hash. One
    property can now hold several owners' answers, so picking by property alone
    would hand whichever owner was traced LAST to every lead at that address.

    This has to reach `later_sql` specifically, so the FIRST delivery is left
    untraced: the first pass joins `delivered_records.first_result_id` and finds
    nothing to copy, exactly as it does when run 1 ran with skip trace off and a
    later run traced the already-delivered row.
    """
    first = _run(business_user.id, skip_on=False)
    _lead(business_user.id, first, 13, party=DECEASED)  # delivered, never traced

    older = _run(business_user.id, skip_on=False)
    _lead(business_user.id, older, 13, party=DECEASED, status="hit", dup=True,
          phone="2065550113", traced_days_ago=5,
          subject_hash=_subject(business_user.id, 13, "AVELINO", "SAARENAS"))
    # A NEWER answer at the same property, for a DIFFERENT owner.
    newer = _run(business_user.id, skip_on=False)
    _lead(business_user.id, newer, 13, party=HEIR, status="hit", dup=True,
          phone="2065550999", traced_days_ago=1,
          subject_hash=_subject(business_user.id, 13, "ROBERT", "JONES"))

    again = _run(business_user.id)
    target = _lead(business_user.id, again, 13, party=DECEASED, dup=True)

    _enqueue(again, redis_client)

    assert _row(target).phone == "2065550113", (
        "the newest answer for the property won instead of the right owner's"
    )


# ─── Round 14 P2: one tenant can hold two subjects in one ingest group ────────

async def test_one_tenant_with_two_subjects_gets_two_cache_rows(business_user):
    """The write used to dedup by user_id, which under 098 drops the second
    subject for a tenant and re-pays for it on the next run."""
    from src.scrapers.enrichment.skip_trace import pending_row_subject_key

    class _Pend:
        def __init__(self, first, last):
            self.user_id = business_user.id
            self.property_address = _address(14)
            self.city, self.state = CITY, STATE
            self.trace_type = "normal"
            self.first_name, self.last_name = first, last

    rows = [_Pend("AVELINO", "SAARENAS"), _Pend("ROBERT", "JONES")]
    keys = {pending_row_subject_key(p) for p in rows}
    assert len(keys) == 2, "one tenant's two subjects must be two cache rows"

    seen: set[str] = set()
    written = [k for k in (pending_row_subject_key(p) for p in rows)
               if not (k in seen or seen.add(k))]
    assert len(written) == 2
