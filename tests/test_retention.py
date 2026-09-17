"""The §7 retention sweep: what it purges, and everything it must NOT.

Privacy Policy §7 promises 365-day deletion of lead records. The sweep keeps the
lead row (county public record) and deletes the vendor-sourced contact PII inside
it. Both halves are load-bearing, so both are tested: that aged PII really goes,
and that fresh rows, never-traced rows, and IN-FLIGHT rows are left alone.

The in-flight case is the one worth reading. A row traced long ago and since
re-queued carries old, past-retention PII while sitting in queued/submitted.
Purging it flips its status, and tracerfy_ingest only applies a provider result to
a row still IN ('queued','submitted') -- so purging it mid-flight silently
discards a Tracerfy lookup that was PAID for.

Real DB (SyncSessionLocal) -- no mocks.

EXPORT_RETENTION_DAYS is pinned absurdly high wherever the sweep runs in enforce
mode. The export leg does real network I/O to Cloudflare R2, and this suite shares
a test database, so a leaked job row carrying an export_key would otherwise send
these tests out to the internet. The R2 leg's own guarantee (the conditional
clear) is asserted at the SQL level instead, where it needs no network.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, text

from src.config import settings
from src.db.models import (
    Job,
    Result,
    ScraperConfig,
    SkipTraceCache,
    SkipTraceQueue,
    User,
)
from src.db.session import SyncSessionLocal
from src.workers.scheduler_helpers.retention import (
    _CLEAR_EXPORT,
    _purge_skip_trace_pii_impl,
)

_PII = ("phone", "phone_type", "phone_dnc_flag", "email", "phones", "emails")


def _now() -> datetime:
    return datetime.now(UTC)


# ─── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def made():
    """Track what a test creates, then delete it.

    Results/jobs/configs/queues all cascade from the user. skip_trace_cache does
    NOT -- it has no user_id (its key is a hash OF one) -- so those rows are
    deleted by hand or they outlive the test and skew the next run's counts.
    """
    created = {"users": [], "cache": []}
    yield created
    with SyncSessionLocal() as db:
        if created["cache"]:
            db.execute(
                delete(SkipTraceCache).where(
                    SkipTraceCache.address_hash.in_(created["cache"])
                )
            )
        if created["users"]:
            db.execute(delete(User).where(User.id.in_(created["users"])))
        db.commit()


@pytest.fixture
def enforce(monkeypatch):
    """Run the sweep for real, with the R2 leg held out (see module docstring)."""
    monkeypatch.setattr(settings, "RETENTION_PURGE_ENABLED", True)
    monkeypatch.setattr(settings, "RETENTION_PURGE_DRY_RUN", False)
    monkeypatch.setattr(settings, "EXPORT_RETENTION_DAYS", 36500)


# ─── builders ─────────────────────────────────────────────────────────────────

def _user(db, made) -> User:
    u = User(
        id=str(uuid.uuid4()),
        email=f"ret-{uuid.uuid4().hex[:10]}@test.local",
        password_hash="x" * 60,
        plan="pro",
        records_used=0,
        records_limit=-1,
    )
    db.add(u)
    db.flush()
    made["users"].append(u.id)
    return u


def _job(db, user_id: str) -> Job:
    c = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user_id, name="ret", county="pierce",
        state="WA", record_type="probate", fields=[], enrichment=[],
        schedule={}, deliver={},
    )
    db.add(c)
    db.flush()
    j = Job(id=str(uuid.uuid4()), user_id=user_id, scraper_config_id=c.id,
            status="done", trigger="manual")
    db.add(j)
    db.flush()
    return j


def _result(db, user_id, job_id, *, age_days, status="hit", with_pii=True) -> Result:
    r = Result(
        id=str(uuid.uuid4()), user_id=user_id, job_id=job_id,
        date_recorded="06/01/2026", party_name="DOE, JANE",
        property_address="100 MAIN ST",
        property_key=f"WA|pierce|{uuid.uuid4().hex[:10]}",
        skip_trace_status=status,
        skip_trace_attempted_at=_now() - timedelta(days=age_days),
    )
    if with_pii:
        r.phone = "+12535550123"
        r.phone_type = "Mobile"
        r.phone_dnc_flag = False
        r.email = "jane@example.com"
        r.phones = [{"number": "+12535550123", "type": "Mobile"}]
        r.emails = ["jane@example.com"]
    db.add(r)
    db.flush()
    return r


def _cache(db, made, *, age_days) -> str:
    h = uuid.uuid4().hex * 2  # 64 chars
    db.add(SkipTraceCache(
        address_hash=h, phone="+12535550199", email="x@example.com",
        raw_response={"phones": ["+12535550199"]},
        fetched_at=_now() - timedelta(days=age_days),
    ))
    db.flush()
    made["cache"].append(h)
    return h


def _queue(db, user_id, *, status, age_days) -> SkipTraceQueue:
    q = SkipTraceQueue(
        id=str(uuid.uuid4()),
        tracerfy_queue_id=int(uuid.uuid4().int % 2_000_000_000),
        user_id=user_id, trace_type="normal", status=status,
        download_url="https://tracerfy.nyc3.cdn.digitaloceanspaces.com/x/f.csv",
        submitted_at=_now() - timedelta(days=age_days),
        completed_at=_now() - timedelta(days=age_days) if status == "completed" else None,
    )
    db.add(q)
    db.flush()
    return q


def _reload(rid: str) -> Result:
    with SyncSessionLocal() as db:
        return db.get(Result, rid)


def _has_no_pii(r: Result) -> bool:
    return all(getattr(r, c) is None for c in _PII)


# ─── the sweep is off by default ──────────────────────────────────────────────

def test_disabled_by_default_touches_nothing(made):
    """Shipping off is a feature: the deletion is irreversible."""
    with SyncSessionLocal() as db:
        u = _user(db, made)
        j = _job(db, u.id)
        r = _result(db, u.id, j.id, age_days=400)
        db.commit()
        rid = r.id

    assert settings.RETENTION_PURGE_ENABLED is False
    _purge_skip_trace_pii_impl()

    after = _reload(rid)
    assert after.phone is not None
    assert after.skip_trace_status == "hit"


def test_dry_run_writes_nothing(made, monkeypatch):
    monkeypatch.setattr(settings, "RETENTION_PURGE_ENABLED", True)
    monkeypatch.setattr(settings, "RETENTION_PURGE_DRY_RUN", True)
    with SyncSessionLocal() as db:
        u = _user(db, made)
        j = _job(db, u.id)
        r = _result(db, u.id, j.id, age_days=400)
        h = _cache(db, made, age_days=400)
        db.commit()
        rid = r.id

    _purge_skip_trace_pii_impl()

    after = _reload(rid)
    assert after.phone is not None, "dry run must not clear PII"
    assert after.skip_trace_status == "hit"
    with SyncSessionLocal() as db:
        assert db.get(SkipTraceCache, h) is not None, "dry run must not delete cache"


# ─── what it purges ───────────────────────────────────────────────────────────

def test_purges_aged_pii_and_marks_the_row_purged(made, enforce):
    with SyncSessionLocal() as db:
        u = _user(db, made)
        j = _job(db, u.id)
        r = _result(db, u.id, j.id, age_days=400)
        db.commit()
        rid, pk = r.id, r.property_key

    _purge_skip_trace_pii_impl()

    after = _reload(rid)
    assert _has_no_pii(after), "every vendor contact column must be cleared"
    assert after.skip_trace_status == "purged"
    # The lead itself SURVIVES -- that is the whole point of option (c).
    assert after.property_key == pk
    assert after.party_name == "DOE, JANE"
    assert after.property_address == "100 MAIN ST"
    # The attempt history is kept; only the contact data goes.
    assert after.skip_trace_attempted_at is not None


def test_leaves_rows_inside_the_window_alone(made, enforce):
    with SyncSessionLocal() as db:
        u = _user(db, made)
        j = _job(db, u.id)
        r = _result(db, u.id, j.id, age_days=10)
        db.commit()
        rid = r.id

    _purge_skip_trace_pii_impl()

    after = _reload(rid)
    assert after.phone is not None
    assert after.skip_trace_status == "hit"


def test_is_idempotent(made, enforce):
    """It runs daily forever; a second pass must find nothing left to do."""
    with SyncSessionLocal() as db:
        u = _user(db, made)
        j = _job(db, u.id)
        r = _result(db, u.id, j.id, age_days=400)
        db.commit()
        rid = r.id

    _purge_skip_trace_pii_impl()
    first = _reload(rid)
    _purge_skip_trace_pii_impl()
    second = _reload(rid)

    assert _has_no_pii(second)
    assert second.skip_trace_status == "purged"
    # Nothing re-written on the second pass: same row, still terminal.
    assert second.skip_trace_attempted_at == first.skip_trace_attempted_at


def test_a_miss_row_is_never_touched(made, enforce):
    """Attempted, nothing returned. No PII to purge, so it keeps its history."""
    with SyncSessionLocal() as db:
        u = _user(db, made)
        j = _job(db, u.id)
        r = _result(db, u.id, j.id, age_days=400, status="miss", with_pii=False)
        db.commit()
        rid = r.id

    _purge_skip_trace_pii_impl()

    after = _reload(rid)
    assert after.skip_trace_status == "miss", (
        "a miss must not be relabelled 'purged' -- 'we asked and got nothing' and "
        "'we had it and deleted it' are different facts"
    )


def test_a_never_traced_row_is_never_touched(made, enforce):
    with SyncSessionLocal() as db:
        u = _user(db, made)
        j = _job(db, u.id)
        r = Result(
            id=str(uuid.uuid4()), user_id=u.id, job_id=j.id,
            date_recorded="06/01/2026", party_name="DOE, JOHN",
            property_address="200 MAIN ST",
            property_key=f"WA|pierce|{uuid.uuid4().hex[:10]}",
        )
        db.add(r)
        db.commit()
        rid = r.id

    _purge_skip_trace_pii_impl()

    after = _reload(rid)
    assert after.skip_trace_status == "not_attempted"
    assert after.skip_trace_attempted_at is None


# ─── the race guard ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["queued", "submitted"])
def test_does_not_purge_an_in_flight_row(made, enforce, status):
    """A re-queued row's OLD PII is past retention, but purging it now would make
    tracerfy_ingest discard the paid result that is still coming."""
    with SyncSessionLocal() as db:
        u = _user(db, made)
        j = _job(db, u.id)
        r = _result(db, u.id, j.id, age_days=400, status=status)
        db.commit()
        rid = r.id

    _purge_skip_trace_pii_impl()

    after = _reload(rid)
    assert after.skip_trace_status == status, (
        "an in-flight row must keep its status, or the provider callback "
        "(which matches on queued/submitted) will drop a lookup we paid for"
    )
    assert after.phone is not None


# ─── the cache ────────────────────────────────────────────────────────────────

def test_cache_deletes_past_the_reuse_window_only(made, enforce):
    with SyncSessionLocal() as db:
        old = _cache(db, made, age_days=200)   # past the 90d reuse window
        fresh = _cache(db, made, age_days=5)
        db.commit()

    _purge_skip_trace_pii_impl()

    with SyncSessionLocal() as db:
        assert db.get(SkipTraceCache, old) is None, (
            "a cache row past the reuse window can never be used again and holds "
            "the full provider payload"
        )
        assert db.get(SkipTraceCache, fresh) is not None


# ─── the provider download links ──────────────────────────────────────────────

def test_completed_link_is_cleared_past_the_short_window(made, enforce):
    with SyncSessionLocal() as db:
        u = _user(db, made)
        q = _queue(db, u.id, status="completed", age_days=60)
        db.commit()
        qid = q.id

    _purge_skip_trace_pii_impl()

    with SyncSessionLocal() as db:
        assert db.get(SkipTraceQueue, qid).download_url is None, (
            "an ingested queue has nothing left to recover, and the CDN link "
            "needs no auth"
        )


def test_recent_completed_link_survives(made, enforce):
    with SyncSessionLocal() as db:
        u = _user(db, made)
        q = _queue(db, u.id, status="completed", age_days=2)
        db.commit()
        qid = q.id

    _purge_skip_trace_pii_impl()

    with SyncSessionLocal() as db:
        assert db.get(SkipTraceQueue, qid).download_url is not None


def test_errored_link_survives_the_short_window_but_not_the_pii_window(made, enforce):
    """An errored queue is recovered BY HAND from its link, so it is not taken
    early -- but past the PII window there is nothing left to recover into."""
    with SyncSessionLocal() as db:
        u = _user(db, made)
        recoverable = _queue(db, u.id, status="errored", age_days=60)
        expired = _queue(db, u.id, status="errored", age_days=400)
        db.commit()
        rec_id, exp_id = recoverable.id, expired.id

    _purge_skip_trace_pii_impl()

    with SyncSessionLocal() as db:
        assert db.get(SkipTraceQueue, rec_id).download_url is not None
        assert db.get(SkipTraceQueue, exp_id).download_url is None


# ─── the export-key clear (SQL level, no network) ─────────────────────────────

def test_export_key_clear_is_conditional_on_the_key_deleted(made):
    """The Codex High. A commit and an R2 round trip sit between selecting an aged
    export and clearing its key. If the job is re-exported in that gap, an
    unconditional clear wipes the NEW key while its file is still in R2 -- an
    orphan nothing can ever find."""
    with SyncSessionLocal() as db:
        u = _user(db, made)
        j = _job(db, u.id)
        j.export_key = "exports/u/j/new.csv"   # re-exported while we were deleting
        db.commit()
        jid = j.id

        stale = db.execute(
            _CLEAR_EXPORT, {"id": jid, "key": "exports/u/j/old.csv"}
        )
        db.commit()
        assert stale.rowcount == 0, "a stale key must match nothing"

    with SyncSessionLocal() as db:
        assert db.get(Job, jid).export_key == "exports/u/j/new.csv", (
            "the freshly written key must survive"
        )

    with SyncSessionLocal() as db:
        hit = db.execute(_CLEAR_EXPORT, {"id": jid, "key": "exports/u/j/new.csv"})
        db.commit()
        assert hit.rowcount == 1, "the key we actually deleted must clear"
        assert db.get(Job, jid).export_key is None


# ─── the grant the sweep depends on ───────────────────────────────────────────

def test_cache_delete_grant_is_provisioned():
    """The sweep DELETEs skip_trace_cache. Losing that grant is exactly how this
    repo stranded 16,761 dedup claims on delivered_records -- it failed silently."""
    with SyncSessionLocal() as db:
        granted = db.execute(
            text("SELECT has_table_privilege(current_user, 'skip_trace_cache', 'DELETE')")
        ).scalar()
    assert granted, (
        "the worker role cannot DELETE skip_trace_cache; run "
        "scripts/provision_rls_roles.sql"
    )
