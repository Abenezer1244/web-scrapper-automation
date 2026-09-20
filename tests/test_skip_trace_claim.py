"""One active skip-trace claim per lead, and a conflict that costs only itself.

Migration 099 adds a partial unique index on pending_skip_trace_rows(result_id)
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
regression: it fails against the pre-099 enqueue and passes against the shared
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
from src.workers.skip_trace_claim import claim_skip_trace_rows
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
    won = await db.run_sync(lambda s: claim_skip_trace_rows(s, payloads))
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

    first = await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload]))
    await db.commit()
    assert first == [rid]

    (await db.get(Result, rid)).skip_trace_status = "not_attempted"
    await db.commit()

    second = await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload]))
    await db.commit()
    assert second == [], "a lead with an active claim must not be claimed again"
    assert await _pending(db, rid, active_only=True) == 1


async def test_one_conflicting_lead_does_not_lose_the_rest_of_the_batch(
    db, business_user: User, redis_client, _skip_trace_on,
):
    """THE 15-1 regression.

    Lead A already has an active claim (as the contact-lookup action would have
    left it). The job's OTHER leads must still be queued. The pre-099 enqueue
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
    assert await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload_a])) == [a]
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

    won = await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload, dict(payload)]))
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
        won = claim_skip_trace_rows(s, [payload])
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
        await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload, foreign]))


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

    assert await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload])) == [rid]
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

    assert await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload])) == []
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

    assert await db.run_sync(lambda s: claim_skip_trace_rows(s, [stolen])) == []
    await db.commit()

    assert await _pending(db, victim) == 0
    assert await _status(db, victim) == "not_attempted"
    # And the real owner can still claim it.
    assert await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload])) == [victim]
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

    won = await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload, ghost]))
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

    won = await db.run_sync(lambda s: claim_skip_trace_rows(s, [bad, good]))
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

    assert await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload])) == []
    await db.commit()
    assert await _pending(db, rid) == 0


async def test_without_the_index_the_scrape_degrades_but_the_action_refuses(
    db, business_user: User,
):
    """What a failed migration 099 costs, and to whom.

    start.sh starts the WORKER even when migrations fail and tasks.py only logs
    an enqueue failure, so a targeted `ON CONFLICT (result_id) WHERE ...` would
    turn a failed 099 into every skip-trace enqueue silently ceasing. The bare
    conflict clause degrades instead. But degrading is only safe for the SCRAPE,
    which is the single writer; the Phase 1b-2 action is a second writer and
    must refuse, because without the index two writers really can buy one lead
    twice. The second claim below proves the protection is genuinely gone, which
    is what makes `require_enforcement=True` load-bearing rather than decorative.
    """
    from src.workers.skip_trace_claim import INDEX_NAME, ClaimUnenforcedError, warn_if_unenforced

    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
    try:
        assert await db.run_sync(warn_if_unenforced) is False

        # The action worker refuses outright: nothing claimed, nothing charged.
        with pytest.raises(ClaimUnenforcedError):
            await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload]))

        # The scrape degrades, rather than raising "no unique or exclusion
        # constraint matching the ON CONFLICT specification".
        assert await db.run_sync(
            lambda s: claim_skip_trace_rows(s, [payload], require_enforcement=False)
        ) == [rid]
        # And the protection really is absent: put the lead back and claim again.
        await db.execute(text(
            "UPDATE results SET skip_trace_status = 'not_attempted' WHERE id = :i"
        ), {"i": rid})
        assert await db.run_sync(
            lambda s: claim_skip_trace_rows(s, [payload], require_enforcement=False)
        ) == [rid], "without the index a duplicate active claim is possible"
    finally:
        await db.rollback()

    assert await db.run_sync(warn_if_unenforced) is True


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
    """ACTIVE_PENDING_STATUSES must equal migration 099's index predicate. If
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
    assert row is not None, "migration 099 did not create the index"
    assert row.indisunique, "the index is not UNIQUE, so it enforces nothing"
    assert row.indisvalid, "the index is INVALID, so the planner ignores it"
    assert "(result_id)" in row.definition, "the index is not keyed on result_id"
    for status in ACTIVE_PENDING_STATUSES:
        assert f"'{status}'" in row.predicate, f"{status} missing from the predicate"
    # And nothing EXTRA: a widened predicate would refuse legitimate re-claims.
    assert row.predicate.count("::character varying") == len(ACTIVE_PENDING_STATUSES)
