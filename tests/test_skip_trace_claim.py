"""One active skip-trace claim per lead, and a conflict that costs only itself.

Migration 100 adds a partial unique index on pending_skip_trace_rows(result_id)
for active rows. Phase 1b's "look up contacts" action is a SECOND writer of that
queue, so two writers can now decide the same lead is eligible at the same
moment; without the index the customer is charged twice for one lead.

The index alone would have introduced a worse bug than it fixed, which is what
most of this file is about (Codex round 15, finding 15-1). The scrape enqueue
used to build rows with db.add() and flush them at ONE commit() guarded by
`except Exception: db.rollback(); db.commit()`. Under the index, a single
conflicting row would raise IntegrityError at that commit, roll back the WHOLE
job's enqueue -- every pending row and every results status update -- and then
commit an empty transaction, silently, with the leads left un-traced and nothing
logged. `test_one_conflicting_lead_does_not_lose_the_rest_of_the_batch` is that
regression: it fails against the pre-100 enqueue and passes against the shared
claim.

Real DB (conftest `db` fixture). Tracerfy is never reached: these tests stop at
the queue.
"""
import uuid

import psycopg2
import pytest
from sqlalchemy import func, select, text

from src.config import settings
from src.db.models import Job, PendingSkipTraceRow, Result, ScraperConfig, User
from src.scrapers.enrichment.skip_trace import build_pending_row_payload
from src.workers.skip_trace_claim import claim_skip_trace_rows, lock_job_for_claim
from src.workers.tasks_helpers.enrich import _enqueue_skip_trace_rows

# A traceable owner and a complete, parseable situs: exactly the row the claim
# is meant to accept, so a test that sees no pending row is proving a rule and
# not tripping over an ineligible fixture.
_PARTY = "SAARENAS AVELINO G"
_SITUS = {"property_city": "VANCOUVER", "property_state": "WA", "property_zip": "98661"}


@pytest.fixture
def _skip_trace_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


async def _config(db, user: User) -> ScraperConfig:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="claim",
        county="clark", state="WA", record_type="probate",
        fields=[], enrichment=[], schedule={"frequency": "manual"},
        deliver={"formats": ["csv"], "emails": []}, skip_trace_enabled=True,
    )
    db.add(cfg)
    await db.commit()
    return cfg


async def _job(db, user: User, cfg: ScraperConfig) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id,
               status="enriching", trigger="manual"))
    await db.commit()
    return job_id


async def _row(db, job_id, user_id, address="1400 MAIN ST", **kw) -> str:
    rid = str(uuid.uuid4())
    kw.setdefault("is_duplicate", False)
    kw.setdefault("party_name", _PARTY)
    db.add(Result(id=rid, job_id=job_id, user_id=user_id,
                  property_address=address, **_SITUS, **kw))
    await db.commit()
    return rid


async def _payload(db, result_id) -> dict:
    payload = build_pending_row_payload(await db.get(Result, result_id))
    assert payload is not None, "fixture row must be traceable or the test is vacuous"
    return payload


async def _pending(db, result_id, active_only=False) -> int:
    q = select(func.count()).select_from(PendingSkipTraceRow).where(
        PendingSkipTraceRow.result_id == result_id
    )
    if active_only:
        q = q.where(PendingSkipTraceRow.status.in_(("queued", "submitting", "submitted")))
    return (await db.execute(q)).scalar_one()


async def _status(db, result_id) -> str:
    r = await db.get(Result, result_id)
    await db.refresh(r)
    return r.skip_trace_status


def _locked_claim(s, payloads, **kw):
    """Claim the way production does: hold the job lock first.

    `claim_skip_trace_rows` ASSERTS the lock is held, so a test that skipped it
    would be testing a path production never takes. `lock_job_for_claim` is
    idempotent within a transaction, so calling it per claim is safe.
    """
    lock_job_for_claim(s, str(payloads[0]["job_id"]))
    return claim_skip_trace_rows(s, payloads, **kw)


def _sync_enqueue(job_id, cfg_id, redis_client):
    def _inner(s):
        _enqueue_skip_trace_rows(s, s.get(Job, job_id), redis_client, job_id,
                                 s.get(ScraperConfig, cfg_id))
    return _inner


async def test_claim_returns_the_ids_it_won_and_advances_only_those(
    db, business_user: User,
):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    a = await _row(db, job_id, business_user.id, address="1 A ST")
    b = await _row(db, job_id, business_user.id, address="2 B ST")
    untouched = await _row(db, job_id, business_user.id, address="3 C ST")

    payloads = [await _payload(db, a), await _payload(db, b)]
    won = await db.run_sync(lambda s: _locked_claim(s, payloads))
    await db.commit()

    assert sorted(won) == sorted([a, b])
    assert await _status(db, a) == "queued"
    assert await _status(db, b) == "queued"
    # The claim advances EXACTLY what it won and nothing else.
    assert await _status(db, untouched) == "not_attempted"
    assert await _pending(db, untouched) == 0


async def test_a_second_active_claim_for_one_lead_is_refused_by_the_index(
    db, business_user: User,
):
    """The index doing its job: this is the double charge Phase 1b makes possible.

    The result is put BACK to 'not_attempted' before the second attempt. Without
    that the insert's own join filters the row out and the test passes whether or
    not the index exists, proving nothing about database arbitration -- which is
    the only thing standing between two writers and one lead charged twice.
    """
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    first = await db.run_sync(lambda s: _locked_claim(s, [payload]))
    await db.commit()
    assert first == [rid]

    (await db.get(Result, rid)).skip_trace_status = "not_attempted"
    await db.commit()

    second = await db.run_sync(lambda s: _locked_claim(s, [payload]))
    await db.commit()
    assert second == [], "a lead with an active claim must not be claimed again"
    assert await _pending(db, rid, active_only=True) == 1


async def test_one_conflicting_lead_does_not_lose_the_rest_of_the_batch(
    db, business_user: User, redis_client, _skip_trace_on,
):
    """THE 15-1 regression.

    Lead A already has an active claim (as the contact-lookup action would have
    left it). The job's OTHER leads must still be queued. The pre-100 enqueue
    would have raised IntegrityError at its single commit, rolled the whole
    batch back and committed an empty transaction -- losing B and C silently.
    """
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    a = await _row(db, job_id, business_user.id, address="1 A ST")
    b = await _row(db, job_id, business_user.id, address="2 B ST")
    c = await _row(db, job_id, business_user.id, address="3 C ST")

    # Someone else claims A first, exactly as the action worker would.
    payload_a = await _payload(db, a)
    assert await db.run_sync(lambda s: _locked_claim(s, [payload_a])) == [a]
    await db.commit()
    # Put A back to not_attempted so the enqueue still considers it eligible and
    # genuinely collides, rather than filtering it out before the insert.
    (await db.get(Result, a)).skip_trace_status = "not_attempted"
    await db.commit()

    await db.run_sync(_sync_enqueue(job_id, cfg.id, redis_client))

    assert await _status(db, b) == "queued", "B was lost to A's conflict"
    assert await _status(db, c) == "queued", "C was lost to A's conflict"
    assert await _pending(db, b, active_only=True) == 1
    assert await _pending(db, c, active_only=True) == 1
    # A keeps its single original claim; no second row, no double charge.
    assert await _pending(db, a, active_only=True) == 1


async def test_a_duplicate_result_id_inside_one_batch_claims_once(
    db, business_user: User,
):
    """ON CONFLICT cannot arbitrate two conflicting rows inside ONE statement,
    so the helper dedupes first. A job listing one lead twice is not
    hypothetical: the trustee_sale collapse produces sibling rows."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    won = await db.run_sync(lambda s: _locked_claim(s, [payload, dict(payload)]))
    await db.commit()

    assert won == [rid]
    assert await _pending(db, rid, active_only=True) == 1


async def test_the_claim_does_not_commit(db, business_user: User):
    """The caller owns the transaction. Phase 1b-2 writes the action's
    dispositions and its audit event in this same transaction; a helper that
    committed underneath would make that atomicity impossible."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    def _claim_then_abandon(s):
        won = _locked_claim(s, [payload])
        assert won == [rid]
        s.rollback()

    await db.run_sync(_claim_then_abandon)

    assert await _pending(db, rid) == 0, "the claim committed itself"
    assert await _status(db, rid) == "not_attempted"


async def test_a_mixed_tenant_batch_is_refused(db, business_user: User, starter_user: User):
    """One claim is one tenant's work: the single tenant-scoped UPDATE that
    advances `results` would otherwise be too broad or silently partial."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)
    foreign = dict(payload, result_id=str(uuid.uuid4()), user_id=starter_user.id)

    with pytest.raises(ValueError, match="one user_id"):
        await db.run_sync(lambda s: _locked_claim(s, [payload, foreign]))


async def test_a_long_last_name_is_truncated_to_the_column_width(
    db, business_user: User,
):
    """Truncation lives beside the insert because the Phase 1a subject key
    hashes against these same widths: if the two diverge the enqueue's cache
    read can never match its own write and every repeat trace is re-paid."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = dict(await _payload(db, rid), first_name="A", last_name="Z" * 400)

    assert await db.run_sync(lambda s: _locked_claim(s, [payload])) == [rid]
    await db.commit()

    stored = (await db.execute(
        select(PendingSkipTraceRow.last_name).where(PendingSkipTraceRow.result_id == rid)
    )).scalar_one()
    assert len(stored) == 128
    assert stored == "Z" * 128


async def test_a_lead_that_settled_before_the_claim_is_not_claimed_at_all(
    db, business_user: User,
):
    """A lead answered between the eligibility read and the claim must keep its
    answer AND get no pending row.

    Asserting only that 'hit' survives is not enough: the earlier version of this
    helper inserted the active pending row first and then found the guarded
    UPDATE matched nothing, leaving an active queued row that nothing would ever
    settle while the lead read 'hit' -- stranded work, invisible. The insert now
    joins through `results`, so the row is never created.
    """
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    settled = await db.get(Result, rid)
    settled.skip_trace_status = "hit"
    await db.commit()

    assert await db.run_sync(lambda s: _locked_claim(s, [payload])) == []
    await db.commit()

    assert await _status(db, rid) == "hit", "a settled answer was overwritten"
    assert await _pending(db, rid) == 0, "a stranded active row was created"


async def test_another_tenants_lead_is_never_claimed(
    db, business_user: User, starter_user: User,
):
    """A payload naming another tenant's result must claim nothing.

    If it inserted, the unique index would then block the RIGHTFUL owner from
    ever claiming that lead, while the dispatcher's tenant-pinned joins ignored
    the row forever. The claim's user_id is the one asserted; the insert joins
    `results` on both id AND user_id.
    """
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    victim = await _row(db, job_id, business_user.id)
    payload = await _payload(db, victim)
    # Same lead, claimed under the WRONG tenant.
    stolen = dict(payload, user_id=starter_user.id)

    assert await db.run_sync(lambda s: _locked_claim(s, [stolen])) == []
    await db.commit()

    assert await _pending(db, victim) == 0
    assert await _status(db, victim) == "not_attempted"
    # And the real owner can still claim it.
    assert await db.run_sync(lambda s: _locked_claim(s, [payload])) == [victim]
    await db.commit()
    assert await _status(db, victim) == "queued"


async def test_a_vanished_lead_does_not_kill_the_batch(db, business_user: User):
    """A payload naming a result that no longer exists must cost only itself.

    Without the join it is a foreign-key violation, which fails the whole
    multi-row insert and takes every other lead in the batch with it -- the same
    class of silent batch loss 15-1 was about.
    """
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    alive = await _row(db, job_id, business_user.id, address="1 A ST")
    payload = await _payload(db, alive)
    ghost = dict(payload, result_id=str(uuid.uuid4()))

    won = await db.run_sync(lambda s: _locked_claim(s, [payload, ghost]))
    await db.commit()

    assert won == [alive]
    assert await _status(db, alive) == "queued"


@pytest.mark.parametrize("field", ["state", "mail_state"])
async def test_an_oversized_state_is_refused_not_truncated(
    db, business_user: User, field: str,
):
    """`state` is String(2) but lookup_subject_key hashes it at 128, so silently
    cutting a longer value to two characters would STORE one value and HASH
    another: the cache read could never match its own write and every repeat
    trace would be re-paid. The payload is refused instead.

    Includes a POSITIVE CONTROL, because an implementation that refused every
    payload would otherwise pass this.
    """
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    bad_id = await _row(db, job_id, business_user.id, address="1 BAD ST")
    good_id = await _row(db, job_id, business_user.id, address="2 GOOD ST")
    bad = dict(await _payload(db, bad_id), **{field: "WASHINGTON"})
    good = await _payload(db, good_id)

    won = await db.run_sync(lambda s: _locked_claim(s, [bad, good]))
    await db.commit()

    assert won == [good_id], "the refusal must cost only the payload it names"
    assert await _pending(db, bad_id) == 0
    assert await _pending(db, good_id) == 1


async def test_a_bad_trace_type_is_refused_not_truncated(db, business_user: User):
    """trace_type is validated, never truncated to its 16-char column. The
    dispatcher batches strictly by 'normal'/'advanced', so any other value would
    sit queued forever: counted as in progress, never submitted, never settled."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = dict(await _payload(db, rid), trace_type="super-advanced-deluxe")

    assert await db.run_sync(lambda s: _locked_claim(s, [payload])) == []
    await db.commit()
    assert await _pending(db, rid) == 0


async def test_without_the_index_nothing_is_claimed_and_nothing_is_lost(
    db, business_user: User,
):
    """A failed migration 100 PAUSES lookups; it does not risk a double charge.

    This replaces a version where the scrape deliberately degraded and claimed
    anyway. The Security Master Review rejected that as a Critical, and the
    premise behind it was wrong: refusing does not "strand every lookup in the
    product". The lead stays 'not_attempted', so the next run claims it once the
    migration lands, and meanwhile scrapes still run and leads are still
    delivered. Only the paid add-on waits. Proceeding unenforced, by contrast,
    risks charging a customer twice for one lead, which retrying cannot undo.
    """
    from src.workers.skip_trace_claim import (
        INDEX_NAME,
        ClaimUnenforcedError,
        warn_if_unenforced,
    )

    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
    try:
        assert await db.run_sync(warn_if_unenforced) is False
        with pytest.raises(ClaimUnenforcedError):
            await db.run_sync(lambda s: _locked_claim(s, [payload]))
    finally:
        await db.rollback()

    assert await db.run_sync(warn_if_unenforced) is True
    # Nothing was claimed and nothing was consumed: the lead is still waiting.
    assert await _pending(db, rid) == 0
    assert await _status(db, rid) == "not_attempted"
    # And once the index is back, the very same payload claims normally.
    assert await db.run_sync(lambda s: _locked_claim(s, [payload])) == [rid]
    await db.commit()
    assert await _status(db, rid) == "queued"


async def test_a_lead_charged_and_unmatched_mid_enqueue_is_not_bought_again(
    db, business_user: User, redis_client, _skip_trace_on,
):
    """The charged-unanswered rule is re-checked under the job lock.

    An 'unmatched' pending row means Tracerfy ACCEPTED that lookup and charged a
    credit for it but we could not attribute the answer. Buying it again is a
    second charge for a question the vendor already failed to answer. The first
    check runs before the lock and has to commit (its log line commits), and the
    dispatcher and ingest do not take that lock, so a lead can become 'unmatched'
    in between. The race is injected exactly there, by having the lock
    acquisition itself mark the lead from a SEPARATE committed connection.
    """
    from src.workers.tasks_helpers import enrich as enrich_mod

    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id,
                     is_duplicate=True, duplicate_reason="prior_run",
                     dedup_hash="hash-charged-unanswered")

    def _mark_unmatched_elsewhere() -> None:
        dsn = settings.DATABASE_URL_SYNC.replace("postgresql+psycopg2://", "postgresql://")
        other = psycopg2.connect(dsn)
        try:
            cur = other.cursor()
            cur.execute(
                "INSERT INTO pending_skip_trace_rows "
                "(id, job_id, result_id, user_id, property_address, city, state, "
                " trace_type, status, submitted_at) "
                "VALUES (gen_random_uuid(), %s, %s, %s, '1400 MAIN ST', 'VANCOUVER', "
                "        'WA', 'normal', 'unmatched', now())",
                (job_id, rid, business_user.id),
            )
            other.commit()
        finally:
            other.close()

    import src.workers.skip_trace_claim as claim_mod

    # Captured BEFORE patching: resolving it inside the patch would resolve the
    # patch itself and recurse forever.
    original = claim_mod.lock_job_for_claim
    fired = {"n": 0}

    def _lock_then_race(s, jid):
        original(s, jid)
        if fired["n"] == 0:
            fired["n"] = 1
            _mark_unmatched_elsewhere()

    claim_mod.lock_job_for_claim = _lock_then_race
    try:
        await db.run_sync(_sync_enqueue(job_id, cfg.id, redis_client))
    finally:
        claim_mod.lock_job_for_claim = original
    assert enrich_mod is not None  # the enqueue under test imports the patched name

    assert fired["n"] == 1, "the race never fired; the test proves nothing"
    assert await _pending(db, rid, active_only=True) == 0, (
        "a lead already charged and unmatched was queued again"
    )
    assert await _status(db, rid) == "errored"


async def test_a_batch_larger_than_the_parameter_limit_claims(
    db, business_user: User,
):
    """A job bigger than one INSERT statement can hold must still claim.

    Postgres caps a statement at 65535 bind parameters and each row costs
    len(_COLUMNS) + 1, so a single VALUES list breaks at about 4,368 leads.
    Production holds 100,548 claimable leads, so a job past that ceiling is
    ordinary. Before chunking, such a job raised and enqueued NOTHING -- the
    exact "one problem costs the whole batch" failure this module exists to
    prevent, just with a different trigger.

    Sized deliberately above the ceiling, not above the chunk size, so it keeps
    testing the real limit if _INSERT_CHUNK_ROWS is ever tuned.
    """
    from src.workers.skip_trace_claim import _COLUMNS

    ceiling = (65535 - 2) // (len(_COLUMNS) + 1)
    n = ceiling + 50

    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    # Built with one bulk INSERT: creating them one at a time is far too slow.
    ids = [str(uuid.uuid4()) for _ in range(n)]
    await db.execute(text(
        "INSERT INTO results (id, job_id, user_id, party_name, property_address, "
        " property_city, property_state, property_zip, is_duplicate, "
        " skip_trace_status) "
        "SELECT x.id, CAST(:j AS uuid), CAST(:u AS uuid), :party, "
        "       x.n || ' BULK ST', 'VANCOUVER', 'WA', '98661', false, "
        "       'not_attempted' "
        "FROM unnest(CAST(:ids AS uuid[])) WITH ORDINALITY AS x(id, n)"
    ), {"j": job_id, "u": business_user.id, "party": _PARTY, "ids": ids})
    await db.commit()

    payloads = [await _payload(db, rid) for rid in ids[:1]]  # prove one is valid
    assert payloads[0] is not None

    def _claim_all(s):
        rows = s.execute(
            select(Result).where(Result.job_id == job_id)
        ).scalars().all()
        built = [build_pending_row_payload(r) for r in rows]
        assert all(b is not None for b in built), "fixture rows must be traceable"
        lock_job_for_claim(s, job_id)
        return claim_skip_trace_rows(s, built)

    won = await db.run_sync(_claim_all)
    await db.commit()

    assert len(won) == n, f"expected all {n} leads claimed, got {len(won)}"
    queued = (await db.execute(
        select(func.count()).select_from(PendingSkipTraceRow)
        .where(PendingSkipTraceRow.job_id == job_id,
               PendingSkipTraceRow.status == "queued")
    )).scalar_one()
    assert queued == n


async def test_a_stranded_lead_outside_the_final_chunk_withdraws_the_right_row(
    db, business_user: User,
):
    """The withdrawal deletes OUR row for that lead, whichever chunk made it.

    The chunked insert rebuilds its bind parameters per chunk, so a withdrawal
    that indexed those parameters by a lead's position in the whole batch would
    read the wrong chunk's id -- deleting a legitimately claimed row and leaving
    its Result 'queued' with nothing behind it -- or KeyError past the last
    chunk's length. The stranded lead here sits in the FIRST chunk while later
    chunks follow, which is precisely the case that mis-mapped.
    """
    from src.workers.skip_trace_claim import _INSERT_CHUNK_ROWS

    n = _INSERT_CHUNK_ROWS + 25
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    ids = [str(uuid.uuid4()) for _ in range(n)]
    await db.execute(text(
        "INSERT INTO results (id, job_id, user_id, party_name, property_address, "
        " property_city, property_state, property_zip, is_duplicate, "
        " skip_trace_status) "
        "SELECT x.id, CAST(:j AS uuid), CAST(:u AS uuid), :party, "
        "       x.n || ' CHUNK ST', 'VANCOUVER', 'WA', '98661', false, "
        "       'not_attempted' "
        "FROM unnest(CAST(:ids AS uuid[])) WITH ORDINALITY AS x(id, n)"
    ), {"j": job_id, "u": business_user.id, "party": _PARTY, "ids": ids})
    await db.commit()

    # Pick a lead the claim will place in the FIRST chunk (the claim sorts by
    # result_id), and settle it from another connection mid-claim so its UPDATE
    # matches nothing and it becomes the stranded one.
    victim = sorted(ids)[0]

    def _settle_victim() -> None:
        dsn = settings.DATABASE_URL_SYNC.replace("postgresql+psycopg2://", "postgresql://")
        other = psycopg2.connect(dsn)
        try:
            cur = other.cursor()
            cur.execute(
                "UPDATE results SET skip_trace_status = 'hit' WHERE id = %s", (victim,)
            )
            other.commit()
        finally:
            other.close()

    def _claim_with_a_settle_after_the_inserts(s):
        rows = s.execute(select(Result).where(Result.job_id == job_id)).scalars().all()
        built = [build_pending_row_payload(r) for r in rows]
        lock_job_for_claim(s, job_id)
        original = s.execute
        state = {"inserts": 0, "fired": False}

        def _execute(statement, *args, **kwargs):
            # BEFORE the results UPDATE, not after: the victim must be inserted
            # (so it is in inserted_ids) and only then settled, so its UPDATE
            # matches nothing and it becomes the stranded row the withdrawal has
            # to find. Settling any earlier and the INSERT's own join would
            # exclude it, the withdrawal would never run, and this test would
            # pass against a broken mapping.
            if (state["inserts"] and not state["fired"]
                    and "UPDATE results SET skip_trace_status" in str(statement)):
                state["fired"] = True
                _settle_victim()
            result = original(statement, *args, **kwargs)
            if "INSERT INTO pending_skip_trace_rows" in str(statement):
                state["inserts"] += 1
            return result

        s.execute = _execute
        try:
            won = claim_skip_trace_rows(s, built)
        finally:
            s.execute = original
        assert state["inserts"] > 1, "the batch did not actually chunk"
        assert state["fired"], "the settle never fired; the test proves nothing"
        return won

    won = await db.run_sync(_claim_with_a_settle_after_the_inserts)
    await db.commit()

    assert victim not in won
    assert len(won) == n - 1
    # The victim's row is gone, and every OTHER lead kept its own queued row.
    assert await _pending(db, victim) == 0
    queued = (await db.execute(
        select(func.count()).select_from(PendingSkipTraceRow)
        .where(PendingSkipTraceRow.job_id == job_id,
               PendingSkipTraceRow.status == "queued")
    )).scalar_one()
    assert queued == n - 1, "the withdrawal deleted the wrong row"


async def test_claiming_without_the_job_lock_is_refused(db, business_user: User):
    """The lock is a precondition, not a convention.

    It is what serializes the cache-hit write -- an ORM write on encrypted
    columns, which no SQL in the claim can substitute for and which cannot see
    another writer's uncommitted pending row. A caller that forgets it would
    silently reintroduce the race where a settled Result sits against an active
    paid queue row, so the claim refuses instead of trusting the docstring.
    """
    from src.workers.skip_trace_claim import ClaimLockNotHeldError

    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    with pytest.raises(ClaimLockNotHeldError):
        await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload]))
    await db.rollback()
    assert await _pending(db, rid) == 0

    # The identical payload claims once the lock is taken, so the refusal is
    # about the lock and not about the payload.
    assert await db.run_sync(lambda s: _locked_claim(s, [payload])) == [rid]
    await db.commit()


async def test_the_lock_check_detects_the_lock_for_negative_hashes_too(db):
    """The lock assertion reconstructs the advisory key from pg_locks.

    pg_advisory_xact_lock(bigint) splits the key across classid (high 32 bits)
    and objid (low 32), and hashtext() returns a SIGNED int4, so roughly half of
    all job ids hash negative and the shift is arithmetic. Get that wrong in the
    strict direction and every claim raises ClaimLockNotHeldError; get it wrong
    in the loose direction and the assertion is worthless. Both signs are
    exercised here, along with the two ways it could be wrong: matching before
    the lock is taken, and matching another job's key.
    """
    from src.workers.skip_trace_claim import _lock_key, job_claim_lock_held

    def _exercise(s):
        seen_negative = seen_positive = 0
        for _ in range(60):
            job_id, other_id = str(uuid.uuid4()), str(uuid.uuid4())
            h = s.execute(
                text("SELECT hashtext(:k)::bigint"), {"k": _lock_key(job_id)}
            ).scalar()
            assert not job_claim_lock_held(s, job_id), "matched before locking"
            lock_job_for_claim(s, job_id)
            assert job_claim_lock_held(s, job_id), f"missed its own lock (hash {h})"
            assert not job_claim_lock_held(s, other_id), "matched another job"
            if h < 0:
                seen_negative += 1
            else:
                seen_positive += 1
        return seen_negative, seen_positive

    negative, positive = await db.run_sync(_exercise)
    await db.rollback()
    # If a run drew only one sign the assertions above proved half as much.
    assert negative > 0 and positive > 0, (
        f"needed both signs to be meaningful (negative={negative}, positive={positive})"
    )


async def test_a_mixed_job_batch_is_refused(db, business_user: User):
    """One job per claim: a single job lock is meaningless over a mixed batch."""
    cfg = await _config(db, business_user)
    job_a = await _job(db, business_user, cfg)
    # Its own scraper: two ACTIVE runs of one scraper are impossible since
    # migration 104, and the claim rules under test are per job, not per scraper.
    job_b = await _job(db, business_user, await _config(db, business_user))
    a = await _row(db, job_a, business_user.id, address="1 A ST")
    b = await _row(db, job_b, business_user.id, address="2 B ST")

    payloads = [await _payload(db, a), await _payload(db, b)]
    with pytest.raises(ValueError, match="one job_id"):
        await db.run_sync(lambda s: _locked_claim(s, payloads))


async def test_a_payload_naming_the_wrong_job_claims_nothing(
    db, business_user: User,
):
    """The claim pins the lead to its tenant AND to its job.

    A malformed payload carrying another job's id would otherwise write a queue
    row tied to a job the lead does not belong to. The dispatcher's tenant-pinned
    joins would ignore that row forever, while the unique index blocked the
    legitimate claim: a lead that could never be looked up again.
    """
    cfg = await _config(db, business_user)
    job_a = await _job(db, business_user, cfg)
    # Its own scraper: two ACTIVE runs of one scraper are impossible since
    # migration 104, and the claim rules under test are per job, not per scraper.
    job_b = await _job(db, business_user, await _config(db, business_user))
    rid = await _row(db, job_a, business_user.id)
    wrong_job = dict(await _payload(db, rid), job_id=job_b)

    assert await db.run_sync(lambda s: _locked_claim(s, [wrong_job])) == []
    await db.commit()

    assert await _pending(db, rid) == 0
    assert await _status(db, rid) == "not_attempted"


async def test_enforcement_check_rejects_a_same_named_wrong_index(
    db, business_user: User,
):
    """`CREATE INDEX IF NOT EXISTS` would accept a same-named index on the wrong
    table or with a wider predicate, and either enforces something other than
    "one active claim per lead" while looking applied. The check asserts
    identity, not just the name."""
    from src.workers.skip_trace_claim import INDEX_NAME, claim_enforcement_ok

    await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
    try:
        # Same name, but not unique.
        await db.execute(text(
            f"CREATE INDEX {INDEX_NAME} ON pending_skip_trace_rows (result_id) "
            "WHERE status IN ('queued','submitting','submitted')"
        ))
        assert await db.run_sync(claim_enforcement_ok) is False
        await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
        # Unique, but a WIDER predicate: would refuse a legitimate re-claim.
        await db.execute(text(
            f"CREATE UNIQUE INDEX {INDEX_NAME} ON pending_skip_trace_rows (result_id) "
            "WHERE status IN ('queued','submitting','submitted','cancelled')"
        ))
        assert await db.run_sync(claim_enforcement_ok) is False
        await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
        # The bypass Codex constructed: right table, unique, valid, one column,
        # and EXACTLY three ::character varying occurrences, so the old
        # cast-counting check passed it -- yet the extra conjunct lets that one
        # lead hold duplicate active rows.
        await db.execute(text(
            f"CREATE UNIQUE INDEX {INDEX_NAME} ON pending_skip_trace_rows (result_id) "
            "WHERE status IN ('queued','submitting','submitted') "
            "  AND result_id <> '00000000-0000-0000-0000-000000000000'::uuid"
        ))
        assert await db.run_sync(claim_enforcement_ok) is False, (
            "an extra predicate conjunct must not pass as the expected index"
        )
        await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
        # A COMPOSITE unique index also permits duplicate active rows per lead.
        await db.execute(text(
            f"CREATE UNIQUE INDEX {INDEX_NAME} ON pending_skip_trace_rows "
            "(result_id, status) "
            "WHERE status IN ('queued','submitting','submitted')"
        ))
        assert await db.run_sync(claim_enforcement_ok) is False
    finally:
        await db.rollback()

    assert await db.run_sync(claim_enforcement_ok) is True


async def test_the_arbiter_itself_fails_closed_without_the_index(
    db, business_user: User,
):
    """Enforcement does not rest on this module's idea of how a predicate renders.

    With `require_enforcement=True` the statement names its arbiter, so Postgres
    resolves it against a real index at planning time and raises "no unique or
    exclusion constraint matching the ON CONFLICT specification" when 100 is
    missing. This bypasses the catalog pre-check to prove the SQL alone fails
    closed -- which is what removes the window between checking the index and
    relying on it.
    """
    from sqlalchemy.exc import ProgrammingError

    from src.workers.skip_trace_claim import INDEX_NAME

    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
    try:
        def _claim_past_the_precheck(s):
            import src.workers.skip_trace_claim as mod
            real = mod.claim_enforcement_ok
            mod.claim_enforcement_ok = lambda _db: True  # pretend the check passed
            lock_job_for_claim(s, job_id)
            try:
                return mod.claim_skip_trace_rows(s, [payload])
            finally:
                mod.claim_enforcement_ok = real

        with pytest.raises(ProgrammingError):
            await db.run_sync(_claim_past_the_precheck)
    finally:
        await db.rollback()


async def test_enforcement_check_ignores_a_same_named_index_in_another_schema(db):
    """The catalog query is schema-qualified on both the index and its table. A
    same-named index on another schema's pending_skip_trace_rows would otherwise
    satisfy the check while the table the application writes stays unenforced."""
    from src.workers.skip_trace_claim import INDEX_NAME, claim_enforcement_ok

    await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
    try:
        await db.execute(text("CREATE SCHEMA IF NOT EXISTS decoy"))
        await db.execute(text(
            "CREATE TABLE decoy.pending_skip_trace_rows "
            "(result_id uuid, status varchar(16))"
        ))
        await db.execute(text(
            f"CREATE UNIQUE INDEX {INDEX_NAME} ON decoy.pending_skip_trace_rows "
            "(result_id) WHERE status IN ('queued','submitting','submitted')"
        ))
        assert await db.run_sync(claim_enforcement_ok) is False
    finally:
        await db.rollback()

    assert await db.run_sync(claim_enforcement_ok) is True


async def test_a_lead_settled_between_insert_and_update_leaves_no_row_behind(
    db, business_user: User,
):
    """The claim is atomic, not eventually-consistent.

    A concurrent writer can settle a lead BETWEEN the claim's insert and its
    results update, so the update matches nothing. Trusting the dispatcher's
    cancel sweep to collect the leftover row was not enough: the sweep runs ONCE
    per tick and BEFORE the submit loop, so a row stranded after the sweep could
    still be submitted and charged for a lead that already had its answer. The
    claim now withdraws its own row inside the same uncommitted transaction.

    The settle is done through a SEPARATE committed connection, so it really is
    concurrent rather than a same-session edit the claim could have seen.
    """
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    def _settle_from_another_connection() -> None:
        dsn = settings.DATABASE_URL_SYNC.replace("postgresql+psycopg2://", "postgresql://")
        other = psycopg2.connect(dsn)
        try:
            cur = other.cursor()
            cur.execute(
                "UPDATE results SET skip_trace_status = 'hit' WHERE id = %s", (rid,)
            )
            other.commit()
        finally:
            other.close()

    def _claim_with_a_settle_in_the_middle(s):
        original = s.execute
        state = {"done": False}

        def _execute(statement, *args, **kwargs):
            result = original(statement, *args, **kwargs)
            if ("INSERT INTO pending_skip_trace_rows" in str(statement)
                    and not state["done"]):
                state["done"] = True
                _settle_from_another_connection()
            return result

        lock_job_for_claim(s, job_id)
        s.execute = _execute
        try:
            won = claim_skip_trace_rows(s, [payload])
        finally:
            s.execute = original
        # Without this the test would be vacuous: no settle means no race, and
        # the claim would simply have succeeded.
        assert state["done"], "the concurrent settle never fired"
        return won

    won = await db.run_sync(_claim_with_a_settle_in_the_middle)
    await db.commit()

    assert won == [], "a lead settled mid-claim must not be reported as claimed"
    assert await _pending(db, rid) == 0, "the claim left a row behind"
    assert await _status(db, rid) == "hit"


async def test_the_active_predicate_matches_the_index(db):
    """ACTIVE_PENDING_STATUSES must equal migration 100's index predicate. If
    they drift, either a second claim slips through (double charge) or a
    legitimate one is refused forever. Checks the index is UNIQUE and VALID and
    on the right column too: a non-unique or invalid index enforces nothing
    while still matching on status strings alone."""
    from src.workers.skip_trace_claim import ACTIVE_PENDING_STATUSES, INDEX_NAME

    row = (await db.execute(text(
        "SELECT i.indisunique, i.indisvalid, "
        "       pg_get_expr(i.indpred, i.indrelid) AS predicate, "
        "       pg_get_indexdef(i.indexrelid) AS definition "
        "FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
        "WHERE c.relname = :n"
    ), {"n": INDEX_NAME})).first()
    assert row is not None, "migration 100 did not create the index"
    assert row.indisunique, "the index is not UNIQUE, so it enforces nothing"
    assert row.indisvalid, "the index is INVALID, so the planner ignores it"
    assert "(result_id)" in row.definition, "the index is not keyed on result_id"
    for status in ACTIVE_PENDING_STATUSES:
        assert f"'{status}'" in row.predicate, f"{status} missing from the predicate"
    # And nothing EXTRA: a widened predicate would refuse legitimate re-claims.
    assert row.predicate.count("::character varying") == len(ACTIVE_PENDING_STATUSES)


def _sync_enqueue_recording(job_id, cfg_id, redis_client, calls: list):
    """Enqueue exactly as production does, recording the stage announcement.

    `on_begin` is #348's callback: the caller enters the `queuing_contacts`
    stage from it. 1b-0 added gates AFTER the point where it fires (the job
    lock, the re-read, the re-applied filters and the per-row payload gates),
    so it stopped meaning what its docstring said.
    """
    def _inner(s):
        _enqueue_skip_trace_rows(
            s, s.get(Job, job_id), redis_client, job_id,
            s.get(ScraperConfig, cfg_id),
            on_begin=lambda: calls.append("queuing_contacts"),
        )
    return _inner


async def test_no_stage_announcement_when_no_lead_can_produce_a_payload(
    db, business_user: User, redis_client, _skip_trace_on,
):
    """A run that will queue nothing must not announce that it is queuing.

    `on_begin` has to fire BEFORE the advisory lock, because the caller's
    `_set_stage` commits and a transaction-scoped lock does not survive a
    commit. That puts it above the per-row gates, so without a pre-check a run
    whose every party_name is a case DESCRIPTION rather than a person -- the
    shape code-violation scrapers write, and exactly the fixture here --
    announces "queuing contact lookups" on every single run and queues
    nothing. That is the same false label Codex round 7 removed from the call
    site.

    Precisely which gate rejects these rows, because the distinction was got
    wrong twice while writing this: they are refused by the generic
    `looks_like_non_personal_party_name` check inside
    `build_pending_row_payload`, which applies to EVERY record type, not by
    `code_violation_owner_is_known`. That gate does RUN -- it is called for
    every row -- but the config here is `probate` and the rows carry no
    code-violation source metadata, so it returns True and passes them
    straight through. Verified against both functions directly, not reasoned
    about: owner_is_known True, non_personal True, payload None.

    A lead with NO party_name is deliberately NOT used here: it still queues,
    as an address-only advanced trace. An earlier version of this test used
    one, and it queued two rows and failed, which is how that was learned.
    """
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    a = await _row(db, job_id, business_user.id, address="1 A ST",
                   party_name="Weeds ? 1819 HARVARD AVE")
    b = await _row(db, job_id, business_user.id, address="2 B ST",
                   party_name="LandLord/Tenant ? 419 21ST AVE")

    # Not vacuous: these rows really are the payload-less case, and they really
    # did reach the gate -- a fixture that failed an EARLIER gate would make the
    # assertion below pass for the wrong reason.
    assert build_pending_row_payload(await db.get(Result, a)) is None
    assert build_pending_row_payload(await db.get(Result, b)) is None

    calls: list = []
    await db.run_sync(_sync_enqueue_recording(job_id, cfg.id, redis_client, calls))

    assert calls == [], "announced the queuing stage for a run that queued nothing"
    assert await _pending(db, a) == 0
    assert await _pending(db, b) == 0
    assert await _status(db, a) == "not_attempted"


async def test_stage_is_announced_once_when_any_lead_would_be_queued(
    db, business_user: User, redis_client, _skip_trace_on,
):
    """The pre-check may only ever SUPPRESS a false announcement.

    One traceable lead among untraceable ones still has work to do, so the
    stage must still be announced -- exactly once. This is the half that stops
    the fix above from being a silent regression of #348.
    """
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    untraceable = await _row(db, job_id, business_user.id, address="1 A ST",
                             party_name="Weeds ? 1819 HARVARD AVE")
    traceable = await _row(db, job_id, business_user.id, address="2 B ST")
    assert build_pending_row_payload(await db.get(Result, traceable)) is not None

    calls: list = []
    await db.run_sync(_sync_enqueue_recording(job_id, cfg.id, redis_client, calls))

    assert calls == ["queuing_contacts"], "the stage must be announced exactly once"
    # And the announcement was truthful: the traceable lead really was queued.
    assert await _pending(db, traceable, active_only=True) == 1
    assert await _status(db, traceable) == "queued"
    assert await _pending(db, untraceable) == 0
