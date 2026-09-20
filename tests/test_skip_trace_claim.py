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


async def test_a_second_active_claim_for_one_lead_is_refused(db, business_user: User):
    """The index doing its job: this is the double charge Phase 1b makes possible."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    first = await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload]))
    await db.commit()
    assert first == [rid]

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


async def test_results_is_advanced_only_from_not_attempted(db, business_user: User):
    """A lead that settled between the eligibility read and the claim keeps its
    answer; the claim must not stamp 'queued' over a real result."""
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id)
    payload = await _payload(db, rid)

    settled = await db.get(Result, rid)
    settled.skip_trace_status = "hit"
    await db.commit()

    assert await db.run_sync(lambda s: claim_skip_trace_rows(s, [payload])) == [rid]
    await db.commit()

    assert await _status(db, rid) == "hit", "a settled answer was overwritten"


async def test_the_active_predicate_matches_the_index(db):
    """The helper's ACTIVE_PENDING_STATUSES is the ON CONFLICT arbiter
    predicate. If it and migration 099 drift, either a second claim slips
    through (double charge) or a legitimate one is refused forever."""
    from src.workers.skip_trace_claim import ACTIVE_PENDING_STATUSES

    predicate = (await db.execute(text(
        "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_class c "
        "JOIN pg_index i ON i.indexrelid = c.oid "
        "WHERE c.relname = 'uq_pending_skip_trace_active_result'"
    ))).scalar_one_or_none()
    assert predicate is not None, "migration 099 did not create the index"
    for status in ACTIVE_PENDING_STATUSES:
        assert f"'{status}'" in predicate, f"{status} missing from the index predicate"
