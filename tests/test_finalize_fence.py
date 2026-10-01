"""2c-bis: finalization is fenced to the attempt that owns the job.

A stalled attempt A that resumes after the watchdog re-queued its job must not bill,
complete, fail or release anything that the replacement attempt B now owns. Real DB,
real Redis, production helpers throughout (claim_attempt, the watchdog's own
_recovery_cas, finalize_billing_and_done, _fail_job, release_quota_reservation). Where
a race must land between two statements inside finalization, the test wraps the
helper's OWN function so the real code runs and B acts at exactly that point.
"""
import inspect
import threading
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.scheduler_helpers.health import _Candidate, _recovery_cas
from src.workers.tasks_helpers import finalize as fin
from src.workers.tasks_helpers.finalize import FinalizeKind, finalize_billing_and_done
from src.workers.tasks_helpers.status import (
    _TERMINAL_STATUSES,
    AttemptToken,
    _fail_job,
    _retry_scrape_job,
    _set_progress,
    _set_stage,
    _set_status,
    _write_heartbeat,
    attempt_state,
    claim_attempt,
    finalize_exit,
    release_quota_reservation,
)

ADDR = "5006 61ST STREET CT E"
WINDOW = datetime(2026, 9, 1, tzinfo=UTC)


# ── fixtures and helpers ─────────────────────────────────────────────────────

@pytest.fixture
def run(starter_user, scraper_config):
    """A job attempt A has claimed and is finalizing: 2 new leads, a reservation of
    2 already charged to the user by A's plan cap (as the cap does), a dedup claim."""
    user_id, config_id = starter_user.id, scraper_config.id
    job_id = str(uuid.uuid4())
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE users SET records_used = 2, quota_period_start = :w "
                       "WHERE id = :u"), {"w": WINDOW, "u": user_id})
        s.add(Job(id=job_id, user_id=user_id, scraper_config_id=config_id,
                  trigger="manual", status="pending", records_found=2,
                  reserved_at=datetime.now(UTC), reserved_count=2,
                  quota_period_start=WINDOW))
        s.flush()
        for _ in range(2):
            s.add(Result(id=str(uuid.uuid4()), job_id=job_id, user_id=user_id,
                         property_address=ADDR, is_duplicate=False,
                         dedup_hash=uuid.uuid4().hex))
        s.commit()
        a = claim_attempt(s, job_id)
        s.execute(text("UPDATE jobs SET status = 'enriching' WHERE id = :j"), {"j": job_id})
        s.execute(text(
            "INSERT INTO delivered_records (id, user_id, dedup_hash, first_result_id, "
            "first_job_id, first_delivered_at) SELECT gen_random_uuid(), user_id, "
            "dedup_hash, id, job_id, now() FROM results WHERE job_id = :j"), {"j": job_id})
        s.commit()
    return {"job_id": job_id, "user_id": user_id, "config_id": config_id, "a": a}


def _requeue(run, observed: AttemptToken, observed_status="enriching") -> bool:
    """The watchdog's own guarded re-queue of the attempt it observed."""
    with SyncSessionLocal() as b:
        seen = _Candidate(id=run["job_id"], status=observed_status,
                          retry_count=observed.retry_count, started_at=observed.started_at,
                          user_id=str(run["user_id"]), scraper_config_id=str(run["config_id"]))
        return _recovery_cas(b, seen, status="pending", started_at=None,
                             retry_count=observed.retry_count + 1, records_found=None,
                             last_heartbeat_at=None)


def _requeue_and_claim(run, observed: AttemptToken) -> AttemptToken | None:
    if not _requeue(run, observed):
        return None
    with SyncSessionLocal() as b:
        token = claim_attempt(b, run["job_id"])
        b.execute(text("UPDATE jobs SET status = 'enriching' WHERE id = :j AND status = 'queued'"),
                  {"j": run["job_id"]})
        b.commit()
        return token


def _finalize(run, token, redis_client):
    with SyncSessionLocal() as db:
        job = db.get(Job, run["job_id"])
        return finalize_billing_and_done(
            db, redis_client, job=job, user=db.get(User, job.user_id),
            config=db.get(ScraperConfig, job.scraper_config_id), job_id=run["job_id"],
            attempt_token=token, object_key=f"exports/{job.user_id}/{run['job_id']}/leads.csv",
            boot_user_id=str(job.user_id),
        )


def _snapshot(run) -> dict:
    """Everything finalization could touch: the job row, the user's counter, the
    reservation, the claims, notifications and job logs."""
    with SyncSessionLocal() as s:
        j = s.execute(text(
            "SELECT status, started_at, retry_count, billing_applied_at, billed_count, "
            "record_count, reserved_at, reserved_count, export_key, error_message "
            "FROM jobs WHERE id = :j"), {"j": run["job_id"]}).one()
        return {
            "job": tuple(j),
            "records_used": s.execute(text("SELECT records_used FROM users WHERE id = :u"),
                                      {"u": run["user_id"]}).scalar_one(),
            "claims": s.execute(text("SELECT count(*) FROM delivered_records "
                                     "WHERE first_job_id = :j"), {"j": run["job_id"]}).scalar_one(),
            "notifications": s.execute(text("SELECT count(*) FROM notifications "
                                            "WHERE job_id = :j"), {"j": run["job_id"]}).scalar_one(),
            "job_logs": s.execute(text("SELECT count(*) FROM job_logs WHERE job_id = :j"),
                                  {"j": run["job_id"]}).scalar_one(),
        }


class _Channel:
    """Everything published on the job's live-log channel while it is open."""

    def __init__(self, redis_client, job_id):
        self._ps = redis_client.pubsub(ignore_subscribe_messages=True)
        self._ps.subscribe(f"job_logs:{job_id}")
        self._ps.get_message(timeout=1)

    def drain(self) -> list:
        out = []
        while (m := self._ps.get_message(timeout=0.5)) is not None:
            out.append(m)
        self._ps.close()
        return out


# ── B1 attempt_state / finalize_exit ─────────────────────────────────────────

def test_attempt_state_owned_requeued_reclaimed(run):
    job_id, uid, a = run["job_id"], run["user_id"], run["a"]
    with SyncSessionLocal() as s:
        assert finalize_exit(attempt_state(s, job_id, uid, a)) is None
        s.rollback()
    assert _requeue(run, a)
    with SyncSessionLocal() as s:
        assert finalize_exit(attempt_state(s, job_id, uid, a)) == "lost_ownership"
        s.rollback()
        b = claim_attempt(s, job_id)
        assert finalize_exit(attempt_state(s, job_id, uid, a)) == "lost_ownership"
        assert finalize_exit(attempt_state(s, job_id, uid, b)) is None
        s.rollback()


@pytest.mark.parametrize("terminal", _TERMINAL_STATUSES)
def test_a_terminal_row_with_the_same_token_is_terminalized(run, terminal):
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET status = :t WHERE id = :j"),
                  {"t": terminal, "j": run["job_id"]})
        s.commit()
        state = attempt_state(s, run["job_id"], run["user_id"], run["a"])
        s.rollback()
    assert (state.owned, finalize_exit(state)) == (False, "terminalized")


def test_attempt_state_takes_a_row_lock(run):
    with SyncSessionLocal() as holder:
        attempt_state(holder, run["job_id"], run["user_id"], run["a"])
        done = threading.Event()

        def _write():
            with SyncSessionLocal() as other:
                other.execute(text("UPDATE jobs SET error_message = 'x' WHERE id = :j"),
                              {"j": run["job_id"]})
                other.commit()
            done.set()

        t = threading.Thread(target=_write)
        t.start()
        assert not done.wait(1.5), "attempt_state did not lock the row"
        holder.rollback()
        t.join(10)
    assert done.is_set()


def test_another_tenant_never_owns_the_job(run, business_user):
    with SyncSessionLocal() as s:
        state = attempt_state(s, run["job_id"], business_user.id, run["a"])
        s.rollback()
    assert state == (False, None)


# ── B2 the token is unique per claim ─────────────────────────────────────────

def test_every_re_pend_increments_retry_count_so_tokens_never_repeat(run):
    a = run["a"]
    b = _requeue_and_claim(run, a)
    with SyncSessionLocal() as s:
        job = s.get(Job, run["job_id"])
        assert _retry_scrape_job(s, job, run["job_id"], b, max_retries=3, backoffs=(1,)) is not None
        c = claim_attempt(s, run["job_id"])
    assert len({a.retry_count, b.retry_count, c.retry_count}) == 3


# ── B4 races: stale A vs replacement B ───────────────────────────────────────

def _assert_a_did_nothing(before, after, published):
    assert after == before, "the stale attempt wrote after it lost ownership"
    assert published == [], "the stale attempt published on the live channel"


def test_b_reclaims_before_a_stage_write(run, redis_client):
    b = _requeue_and_claim(run, run["a"])
    before, ch = _snapshot(run), _Channel(redis_client, run["job_id"])

    outcome = _finalize(run, run["a"], redis_client)

    assert outcome.kind is FinalizeKind.LOST_OWNERSHIP
    _assert_a_did_nothing(before, _snapshot(run), ch.drain())
    with SyncSessionLocal() as s:
        assert s.get(Job, run["job_id"]).started_at == b.started_at


def test_b_reclaims_between_stage_and_billing(run, redis_client, monkeypatch):
    real_stage, state = fin._set_stage, {}

    def stage_then_b_reclaims(*args, **kwargs):
        landed = real_stage(*args, **kwargs)
        state["b"] = _requeue_and_claim(run, run["a"])
        state["before"] = _snapshot(run)
        return landed

    monkeypatch.setattr(fin, "_set_stage", stage_then_b_reclaims)
    ch = _Channel(redis_client, run["job_id"])

    outcome = _finalize(run, run["a"], redis_client)

    assert state["b"] is not None
    assert outcome.kind is FinalizeKind.LOST_OWNERSHIP
    _assert_a_did_nothing(state["before"], _snapshot(run), ch.drain())


def _race_b_against_a_billing_lock(run, monkeypatch):
    """B's re-queue fires right after A's billing fence took the row lock."""
    real, result = fin._fenced_exit, {}

    def fence_then_b(db, job_id, uid, token, where):
        stop = real(db, job_id, uid, token, where)
        if where == "billing" and stop is None:
            t = threading.Thread(target=lambda: result.setdefault(
                "b", _requeue_and_claim(run, run["a"])))
            t.start()
            t.join(1.5)
            result["waited"] = t.is_alive()
            result["thread"] = t
        return stop

    monkeypatch.setattr(fin, "_fenced_exit", fence_then_b)
    return result


def test_a_holds_billing_lock_so_a_wins_and_b_requeue_noops(run, redis_client, monkeypatch):
    race = _race_b_against_a_billing_lock(run, monkeypatch)

    outcome = _finalize(run, run["a"], redis_client)
    race["thread"].join(15)

    assert race["waited"], "B's re-queue did not wait on A's billing lock"
    assert (outcome.kind, outcome.display_count) == (FinalizeKind.DONE, 2)
    assert race["b"] is None
    snap = _snapshot(run)
    status, started_at, retry, billed_at, billed, *_ = snap["job"]
    assert (status, started_at, retry, billed) == ("done", run["a"].started_at, 0, 2)
    assert billed_at is not None and snap["records_used"] == 2


def test_b_cannot_get_between_a_billing_and_done_on_the_already_billed_path(
    run, redis_client, monkeypatch,
):
    """Precondition: billed by an earlier attempt, so A's billing CAS does not fire.
    The billing fence's row lock is taken on this path too, so B still waits."""
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET billing_applied_at = now() - interval '1 hour', "
                       "billed_count = 2 WHERE id = :j"), {"j": run["job_id"]})
        s.commit()
    race = _race_b_against_a_billing_lock(run, monkeypatch)

    outcome = _finalize(run, run["a"], redis_client)
    race["thread"].join(15)

    assert race["waited"] and race["b"] is None
    assert (outcome.kind, outcome.display_count) == (FinalizeKind.DONE, 2)
    assert _snapshot(run)["records_used"] == 2


@pytest.mark.parametrize("terminal", _TERMINAL_STATUSES)
def test_a_terminal_job_with_the_same_token_bills_nothing(run, redis_client, terminal):
    """Not billed, and the cleanup runs right away (Codex diff r1 P1): a cancelled or
    failed run hands its reservation back; a done run (which billed) keeps its charge."""
    billed = terminal == "done"
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET status = :t, billing_applied_at = "
                       "CASE WHEN :b THEN now() END WHERE id = :j"),
                  {"t": terminal, "b": billed, "j": run["job_id"]})
        s.commit()
    billed_at_before = _snapshot(run)["job"][3]

    outcome = _finalize(run, run["a"], redis_client)

    assert outcome.kind is FinalizeKind.ALREADY_TERMINAL
    snap = _snapshot(run)
    assert snap["job"][3] == billed_at_before                      # A billed nothing
    assert snap["records_used"] == (2 if billed else 0)
    assert snap["job"][7] == (2 if billed else 0)                  # reserved_count


# ── B5 after A lost, the replacement settles or cleans up exactly once ───────

def _a_loses(run, redis_client) -> AttemptToken:
    b = _requeue_and_claim(run, run["a"])
    assert _finalize(run, run["a"], redis_client).kind is FinalizeKind.LOST_OWNERSHIP
    return b


def test_after_a_lost_b_succeeds_billed_once(run, redis_client):
    b = _a_loses(run, redis_client)

    outcome = _finalize(run, b, redis_client)

    assert (outcome.kind, outcome.display_count) == (FinalizeKind.DONE, 2)
    snap = _snapshot(run)
    status, started_at, _, billed_at, billed, *_ = snap["job"]
    assert (status, started_at, billed) == ("done", b.started_at, 2)
    # Charged once: the reservation A's cap took (2) is what B settles against.
    assert billed_at is not None and snap["records_used"] == 2


def test_after_a_lost_b_is_cancelled_releases_once(run, redis_client):
    b = _a_loses(run, redis_client)
    with SyncSessionLocal() as s:   # the user cancels B's run (status only, as the API does)
        s.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :j"), {"j": run["job_id"]})
        s.commit()

    assert _finalize(run, b, redis_client).kind is FinalizeKind.ALREADY_TERMINAL
    with SyncSessionLocal() as s:   # already handed back by B's terminal cleanup
        assert release_quota_reservation(s, run["job_id"]) == 0
    snap = _snapshot(run)
    assert (snap["records_used"], snap["claims"]) == (0, 0)


def test_after_a_lost_b_fails_releases_once(run, redis_client):
    b = _a_loses(run, redis_client)
    with SyncSessionLocal() as s:
        job = s.get(Job, run["job_id"])
        assert _fail_job(s, job, redis_client, run["job_id"], "boom", expected_started_at=b)
        assert release_quota_reservation(s, run["job_id"]) == 0   # already given back
    snap = _snapshot(run)
    assert snap["job"][0] == "failed" and snap["records_used"] == 0


# ── B6 the billing-failed branch (the user counter did not move) ─────────────
# That failure (a deleted user, an RLS scope) cannot be produced by real data, so
# the settle statement is replaced by one that moved nothing. Everything else is the
# production path, and B acts at the branch's own ownership check.

def _counter_does_not_move(monkeypatch):
    monkeypatch.setattr(fin, "_settle_user_charge", lambda *a, **k: None)


def _before_billing_failed_check(monkeypatch, action):
    real = fin._fenced_exit

    def wrapped(db, job_id, uid, token, where):
        if where == "billing_failed":
            action()
        return real(db, job_id, uid, token, where)

    monkeypatch.setattr(fin, "_fenced_exit", wrapped)


def test_billing_failed_owned_fails_the_job_once(run, redis_client, monkeypatch):
    _counter_does_not_move(monkeypatch)

    outcome = _finalize(run, run["a"], redis_client)

    assert outcome.kind is FinalizeKind.BILLING_FAILED
    snap = _snapshot(run)
    assert snap["job"][0] == "failed" and snap["job"][3] is None   # failed, not billed
    assert (snap["records_used"], snap["job"][7]) == (0, 0)       # reservation back
    assert snap["notifications"] == 1


def test_billing_failed_lost_does_nothing(run, redis_client, monkeypatch):
    _counter_does_not_move(monkeypatch)
    state = {}

    def b_reclaims():
        state["b"] = _requeue_and_claim(run, run["a"])
        state["before"] = _snapshot(run)

    _before_billing_failed_check(monkeypatch, b_reclaims)
    ch = _Channel(redis_client, run["job_id"])

    outcome = _finalize(run, run["a"], redis_client)

    assert state["b"] is not None
    assert outcome.kind is FinalizeKind.LOST_OWNERSHIP
    _assert_a_did_nothing(state["before"], _snapshot(run), ch.drain())


def test_billing_failed_terminal_cleans_up_once(run, redis_client, monkeypatch):
    _counter_does_not_move(monkeypatch)

    def cancelled():
        with SyncSessionLocal() as s:
            s.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :j"),
                      {"j": run["job_id"]})
            s.commit()

    _before_billing_failed_check(monkeypatch, cancelled)

    outcome = _finalize(run, run["a"], redis_client)

    assert outcome.kind is FinalizeKind.ALREADY_TERMINAL
    snap = _snapshot(run)
    assert (snap["job"][0], snap["job"][3]) == ("cancelled", None)
    assert (snap["records_used"], snap["job"][7], snap["claims"]) == (0, 0, 0)
    assert snap["notifications"] == 0


def test_billing_failed_cancel_between_check_and_fail_cleans_up(run, redis_client, monkeypatch):
    """Codex diff r2 P1-2: the job is cancelled after the branch's ownership check but
    before _fail_job's CAS, so the fail does not land. The reservation and claims must
    still come back, and the outcome says the job is terminal."""
    _counter_does_not_move(monkeypatch)
    real_fail = fin._fail_job

    def cancel_then_fail(*args, **kwargs):
        with SyncSessionLocal() as s:
            s.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :j"),
                      {"j": run["job_id"]})
            s.commit()
        return real_fail(*args, **kwargs)

    monkeypatch.setattr(fin, "_fail_job", cancel_then_fail)

    outcome = _finalize(run, run["a"], redis_client)

    assert outcome.kind is FinalizeKind.ALREADY_TERMINAL
    snap = _snapshot(run)
    assert (snap["job"][0], snap["job"][3]) == ("cancelled", None)
    assert (snap["records_used"], snap["job"][7], snap["claims"]) == (0, 0, 0)
    assert snap["notifications"] == 0


def test_billing_failed_that_cannot_fail_the_job_raises(run, redis_client, monkeypatch):
    """Codex diff r3 P2: _fail_job returning False while the job is still ours means it
    swallowed an error; never report BILLING_FAILED for a failure that did not happen."""
    _counter_does_not_move(monkeypatch)
    monkeypatch.setattr(fin, "_fail_job", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="could not be failed"):
        _finalize(run, run["a"], redis_client)


def _claims(run) -> int:
    return _snapshot(run)["claims"]


def test_run_claims_are_released_only_by_the_owner(run):
    """Codex diff r3 P1-2: the failure paths' claim release is keyed by job, which a
    replacement shares, so a stale attempt must not strip them."""
    assert _claims(run) == 2
    b = _requeue_and_claim(run, run["a"])
    with SyncSessionLocal() as s:
        assert fin.release_run_claims_if_owned(s, run["job_id"], run["user_id"], run["a"]) is False
        s.commit()
    assert _claims(run) == 2                       # the stale attempt stripped nothing
    with SyncSessionLocal() as s:
        assert fin.release_run_claims_if_owned(s, run["job_id"], run["user_id"], b) is True
        s.commit()
    assert _claims(run) == 0                       # the owner can


# ── B6/B8 a forced timestamp collision: A's token matches B's started_at ──────

@pytest.fixture
def collided(run):
    """B re-claimed with a higher retry_count but EXACTLY A's started_at."""
    b = _requeue_and_claim(run, run["a"])
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET started_at = :sa WHERE id = :j"),
                  {"sa": run["a"].started_at, "j": run["job_id"]})
        s.commit()
    return run, AttemptToken(run["a"].started_at, b.retry_count)


def test_no_attempt_scoped_write_lands_for_a_colliding_stale_token(collided, redis_client):
    run, b = collided
    a, job_id = run["a"], run["job_id"]
    before = _snapshot(run)
    with SyncSessionLocal() as s:
        job = s.get(Job, job_id)
        assert _set_progress(s, job, expected_started_at=a, units_done=5) is False
        assert _set_stage(s, job, "finalizing", expected_started_at=a) is False
        assert _set_status(s, job, "done", expected_started_at=a) is False
        assert _fail_job(s, job, redis_client, job_id, "stale", expected_started_at=a) is False
        assert _retry_scrape_job(s, job, job_id, a, max_retries=9, backoffs=(1,)) is None
        assert attempt_state(s, job_id, run["user_id"], a).owned is False
        s.rollback()
    assert _write_heartbeat(job_id, a) == 0
    assert _finalize(run, a, redis_client).kind is FinalizeKind.LOST_OWNERSHIP
    after = _snapshot(run)
    assert after["records_used"] == before["records_used"] == 2   # B's reservation intact
    assert after["job"][0] == "enriching" and after["job"][2] == b.retry_count


def test_the_legacy_datetime_form_is_unchanged(collided):
    """The bare-datetime token keeps today's timestamp-only behavior (legacy test
    callers only; production passes AttemptToken): under a collision it DOES match."""
    run, _b = collided
    with SyncSessionLocal() as s:
        job = s.get(Job, run["job_id"])
        assert _set_progress(s, job, expected_started_at=run["a"].started_at, units_done=5)


# ── B7 wiring (supplement) ───────────────────────────────────────────────────

def _live(source: str) -> str:
    return "\n".join(ln for ln in source.splitlines() if not ln.strip().startswith("#"))


def test_run_scrape_job_passes_the_whole_token_everywhere():
    from src.workers.tasks import run_scrape_job

    body = _live(inspect.getsource(run_scrape_job.__wrapped__))
    assert "attempt_token = claim_attempt(db, job_id)" in body
    assert "claim_job_for_attempt(" not in body
    assert "attempt_token.started_at" not in body and "attempt_started_at" not in body
    assert "finalize_billing_and_done(" in body
    assert "if _outcome.kind is not FinalizeKind.DONE:" in body
    # Everything after DONE (email, webhook, dialer) is below that return.
    tail = body[body.index("if _outcome.kind is not FinalizeKind.DONE:"):]
    for effect in ("deliver_job_email", "emit_job_completed(", "r.publish("):
        assert effect in tail
    # The completed notification goes out once, through the helper (UX 2d), never inline.
    assert tail.count("emit_job_completed(") == 1
    assert '"job_completed"' not in body and "'job_completed'" not in body


def test_finalize_fences_every_money_write():
    body = _live(inspect.getsource(finalize_billing_and_done))
    assert "*_attempt_clauses(attempt_token)" in body              # billing CAS
    assert "expected_started_at=attempt_token," in body           # done-CAS
    assert "reason, expected_started_at=attempt_token" in body    # billing-failed _fail_job
    assert body.count("_fenced_exit(") >= 4                       # stage, billing, failed, done
    # the billing-failed branch asks before it fails or releases anything
    failed = body[body.index("Billing failed: user record-usage"):]
    assert failed.index("_fenced_exit(") < failed.index("_fail_job(")
    # no terminal status literal of its own
    assert "'cancelled'" not in body and '"cancelled"' not in body


def test_retry_count_changes_only_on_the_two_re_pends():
    """The uniqueness argument, pinned: the watchdog and _retry_scrape_job both bump it."""
    src = inspect.getsource(_retry_scrape_job)
    assert "retry_count=retry_count+1" in src
    from src.workers.scheduler_helpers import health
    assert "retry_count=job.retry_count + 1" in inspect.getsource(health)


def _calls(body: str, name: str) -> list[str]:
    """The full argument text of every `name(` call, parentheses balanced."""
    out, start = [], 0
    while (i := body.find(name + "(", start)) != -1:
        depth, j = 0, i + len(name)
        while True:
            ch = body[j]
            depth += ch == "("
            depth -= ch == ")"
            j += 1
            if depth == 0:
                break
        out.append(body[i:j])
        start = j
    return out


def test_every_status_and_fail_write_in_run_scrape_job_carries_the_token():
    """Codex diff r3 P1-1/P1-2: no status transition, fail or claim DELETE in
    run_scrape_job may be status-only or job-keyed; each is pinned to the attempt."""
    from src.workers.tasks import run_scrape_job

    body = _live(inspect.getsource(run_scrape_job.__wrapped__))
    fails = _calls(body, "_fail_job")
    statuses = [c for c in _calls(body, "_set_status") if c.startswith('_set_status(db, job, "')]
    # One _fail_job call (inside _fail_attempt, which every fail path uses: B10).
    assert len(fails) == 1 and len(statuses) >= 3
    for call in fails + statuses:
        assert "expected_started_at=attempt_token" in call, call
    assert "DELETE FROM delivered_records" not in body
    assert body.count("release_run_claims_if_owned(db, job_id, _boot_user_id, attempt_token)") == 4
    cap = body[body.index("release_capped_dedup_claims(") - 400:body.index("release_capped_dedup_claims(")]
    assert "attempt_state(db, job_id, _boot_user_id, attempt_token).owned" in cap


def test_an_unexplained_done_cas_miss_raises_and_releases_nothing(run, redis_client, monkeypatch):
    """Codex diff r4 P2: the job is still ours and live, yet the done-CAS missed.
    Terminal cleanup would refund a live job's reservation, so it must raise instead."""
    monkeypatch.setattr(fin, "_set_status", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="done transition did not land"):
        _finalize(run, run["a"], redis_client)
    snap = _snapshot(run)
    assert (snap["records_used"], snap["job"][7], snap["claims"]) == (2, 2, 2)


def test_every_stage_write_in_run_scrape_job_stops_a_lost_attempt():
    """Codex diff r4 P2: a stage write that did not land means the attempt may have
    lost the job; each one goes through _still_ours before anything is published."""
    from src.workers.tasks import run_scrape_job

    body = _live(inspect.getsource(run_scrape_job.__wrapped__))
    stages = [c for c in _calls(body, "_set_stage") if c.startswith('_set_stage(db, job, "')
              or c.startswith("_set_stage(\n")]
    # The one exception: skip trace's on_begin callback. It only writes the stage
    # (token-scoped, so a stale attempt's write does not land) and publishes
    # nothing. It runs in enrichment, before finalization; what it announces is
    # fenced inside the enqueue itself (B9 below).
    callback = [c for c in stages if '"queuing_contacts"' in c]
    assert len(callback) == 1 and "expected_started_at=attempt_token" in callback[0]
    direct = [c for c in stages if c not in callback]
    assert len(direct) >= 6
    for call in direct:
        i = body.index(call)
        assert body[max(0, i - 30):i].rstrip().endswith("_still_ours("), call


def test_the_date_window_is_written_only_onto_this_attempts_row():
    """Codex diff r5 P2: the resolved window used to be an ORM assignment flushed by
    primary key, so a stale attempt committed it onto the replacement's row."""
    from src.workers.tasks import run_scrape_job

    body = _live(inspect.getsource(run_scrape_job.__wrapped__))
    assert "job.date_from = " not in body and "job.date_to = " not in body
    write = body[body.index(".values(date_from=date_from, date_to=date_to)") - 300:]
    assert "*_attempt_clauses(attempt_token)" in write[:300]
    assert "if not _still_ours(_dated == 1):" in write


# ── B9 paid skip-trace enqueue (Codex diff r6 P1) ────────────────────────────
# The enqueue runs in enrichment, BEFORE finalization, and commits queued PAID
# lookups plus cache-hit copies. Only the attempt that owns the job may do that.

_PARTY = "SAARENAS AVELINO G"
_SITUS = {"property_city": "VANCOUVER", "property_state": "WA", "property_zip": "98661"}


@pytest.fixture
def st_run(business_user, monkeypatch):
    """Attempt A holds a skip-trace-enabled job with two traceable leads: one the
    enqueue would buy a lookup for, one it would serve from a seeded cache entry."""
    from src.config import settings
    from src.db.models import SkipTraceCache
    from src.scrapers.enrichment.skip_trace import build_pending_row_payload, payload_subject_key

    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    user_id, job_id, config_id = business_user.id, str(uuid.uuid4()), str(uuid.uuid4())
    buy, cached = str(uuid.uuid4()), str(uuid.uuid4())
    with SyncSessionLocal() as s:
        s.add(ScraperConfig(id=config_id, user_id=user_id, name="fence", county="clark",
                            state="WA", record_type="probate", fields=[], enrichment=[],
                            schedule={"frequency": "manual"},
                            deliver={"formats": ["csv"], "emails": []},
                            skip_trace_enabled=True))
        s.flush()
        s.add(Job(id=job_id, user_id=user_id, scraper_config_id=config_id,
                  trigger="manual", status="pending"))
        s.flush()
        for rid, addr in ((buy, "1400 MAIN ST"), (cached, "1402 MAIN ST")):
            s.add(Result(id=rid, job_id=job_id, user_id=user_id, property_address=addr,
                         party_name=_PARTY, is_duplicate=False, **_SITUS))
        s.flush()
        payload = build_pending_row_payload(s.get(Result, cached))
        assert payload is not None, "fixture lead must be traceable or the test is vacuous"
        s.add(SkipTraceCache(address_hash=payload_subject_key(user_id, payload),
                             phone="2065550100", phone_type="mobile"))
        s.commit()
        a = claim_attempt(s, job_id)
        s.execute(text("UPDATE jobs SET status = 'enriching' WHERE id = :j"), {"j": job_id})
        s.commit()
    return {"job_id": job_id, "user_id": user_id, "config_id": config_id, "a": a,
            "buy": buy, "cached": cached}


def _enqueue(st_run, redis_client, token, on_begin=None):
    from src.workers.tasks_helpers.enrich import _enqueue_skip_trace_rows

    with SyncSessionLocal() as db:
        job = db.get(Job, st_run["job_id"])
        _enqueue_skip_trace_rows(db, job, redis_client, st_run["job_id"],
                                 db.get(ScraperConfig, job.scraper_config_id),
                                 on_begin=on_begin, attempt_token=token)


def _lookups(st_run) -> dict:
    with SyncSessionLocal() as s:
        return {
            "pending": s.execute(text("SELECT count(*) FROM pending_skip_trace_rows "
                                      "WHERE job_id = :j"), {"j": st_run["job_id"]}).scalar_one(),
            "statuses": {str(rid): status for rid, status in s.execute(text(
                "SELECT id, skip_trace_status FROM results WHERE job_id = :j"),
                {"j": st_run["job_id"]}).all()},
            "cached_phone": s.get(Result, st_run["cached"]).phone,
        }


_UNTOUCHED = "not_attempted"


def test_the_owner_queues_and_copies_as_before(st_run, redis_client):
    _enqueue(st_run, redis_client, st_run["a"])

    got = _lookups(st_run)
    assert got["pending"] == 1
    assert got["statuses"] == {st_run["buy"]: "queued", st_run["cached"]: "hit"}
    assert got["cached_phone"] == "2065550100"


def test_b_reclaims_just_before_a_takes_the_claim_lock(st_run, redis_client):
    """The watchdog re-queues and B claims in the instant before A's lock: A has
    already read its leads as eligible, and must now buy and copy nothing."""
    b = {}
    _enqueue(st_run, redis_client, st_run["a"],
             on_begin=lambda: b.setdefault("token", _requeue_and_claim(st_run, st_run["a"])))
    assert b["token"] is not None

    got = _lookups(st_run)
    assert got["pending"] == 0
    assert set(got["statuses"].values()) == {_UNTOUCHED}
    assert got["cached_phone"] is None
    # Not vacuous: the owner B queues and copies exactly what A was refused.
    _enqueue(st_run, redis_client, b["token"])
    got = _lookups(st_run)
    assert got["pending"] == 1 and got["cached_phone"] == "2065550100"


@pytest.mark.parametrize("terminal", sorted(_TERMINAL_STATUSES))
def test_a_terminal_job_queues_no_lookup(st_run, redis_client, terminal):
    """Same token, but the job was cancelled, failed or finished: nothing is bought."""
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET status = :t WHERE id = :j"),
                  {"t": terminal, "j": st_run["job_id"]})
        s.commit()

    _enqueue(st_run, redis_client, st_run["a"])

    got = _lookups(st_run)
    assert got["pending"] == 0 and set(got["statuses"].values()) == {_UNTOUCHED}


def test_the_job_row_stays_locked_until_the_lookups_commit(st_run, redis_client, monkeypatch):
    """The ownership answer must still hold when the claim commits: from the check to
    the commit the jobs row is locked, so no re-queue can land in between."""
    from src.workers import skip_trace_claim

    real, seen = skip_trace_claim.claim_skip_trace_rows, {}

    def _claim_while_b_tries_to_requeue(db, payloads, **kw):
        with SyncSessionLocal() as b:
            try:
                b.execute(text("SELECT 1 FROM jobs WHERE id = :j FOR UPDATE NOWAIT"),
                          {"j": st_run["job_id"]})
                seen["locked"] = False
            except Exception as exc:  # noqa: BLE001 - LockNotAvailable is the expected answer
                seen["locked"] = "could not obtain lock" in str(exc)
            b.rollback()
        return real(db, payloads, **kw)

    monkeypatch.setattr(skip_trace_claim, "claim_skip_trace_rows", _claim_while_b_tries_to_requeue)
    _enqueue(st_run, redis_client, st_run["a"])

    assert seen == {"locked": True}
    assert _lookups(st_run)["pending"] == 1


def test_run_scrape_job_fences_the_skip_trace_enqueue():
    """Production passes the whole token, and the check sits between the claim lock
    and the commit with nothing that commits (a log publish) in between."""
    from src.workers.tasks import run_scrape_job
    from src.workers.tasks_helpers.enrich import _enqueue_skip_trace_rows

    body = _live(inspect.getsource(run_scrape_job.__wrapped__))
    (call,) = _calls(body, "_enqueue_skip_trace_rows")
    assert "attempt_token=attempt_token" in call
    enq = _live(inspect.getsource(_enqueue_skip_trace_rows))
    locked = enq[enq.index("lock_job_for_claim(db, job_id)"):]
    fence = locked.index("attempt_state(db, job_id, job.user_id, attempt_token)")
    assert "_publish_log(" not in locked[:fence] and "commit()" not in locked[:fence]
    assert fence < locked.index("claim_skip_trace_rows(db, to_claim")  # args may follow


# ── B10 early returns: terminal is cleaned up, lost releases nothing (Codex diff r6 P2) ──

@pytest.fixture
def rerun(run):
    """A watchdog re-run of `run`: back to pending with its reservation and claims
    still held, its scraper since paused, so the run stops at its first early return
    (a `_fail_job` that must land on this attempt's row)."""
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET status = 'pending', started_at = NULL, "
                       "retry_count = retry_count + 1 WHERE id = :j"), {"j": run["job_id"]})
        s.execute(text("UPDATE scraper_configs SET active = false WHERE id = :c"),
                  {"c": run["config_id"]})
        s.commit()
    return run


def _run_scrape_job_racing(rerun, monkeypatch, race):
    """Run the real task; `race` lands between its claim and its first early return."""
    from src.workers import tasks

    real = tasks.skip_reason_for_config

    def _race_then_decide(active, paused_reason):
        race()
        return real(active, paused_reason)

    monkeypatch.setattr(tasks, "skip_reason_for_config", _race_then_decide)
    tasks.run_scrape_job(rerun["job_id"])


def _cancel(rerun):
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :j"), {"j": rerun["job_id"]})
        s.commit()


def _requeue_live_claim(rerun, box):
    with SyncSessionLocal() as s:
        row = s.execute(text("SELECT started_at, retry_count, status FROM jobs WHERE id = :j"),
                        {"j": rerun["job_id"]}).one()
    assert _requeue(rerun, AttemptToken(row.started_at, row.retry_count), row.status)
    with SyncSessionLocal() as b:
        box["b"] = claim_attempt(b, rerun["job_id"])
        b.execute(text("UPDATE jobs SET status = 'enriching' WHERE id = :j"), {"j": rerun["job_id"]})
        b.commit()


def test_an_early_return_on_a_cancelled_job_hands_back_its_reservation_and_claims(
    rerun, monkeypatch,
):
    _run_scrape_job_racing(rerun, monkeypatch, lambda: _cancel(rerun))

    snap = _snapshot(rerun)
    assert snap["job"][0] == "cancelled"
    assert snap["records_used"] == 0 and snap["job"][6] is None  # reservation handed back
    assert snap["claims"] == 0                                     # claims released
    assert snap["notifications"] == 0


def test_an_early_return_on_a_lost_job_releases_nothing(rerun, monkeypatch):
    box = {}
    _run_scrape_job_racing(rerun, monkeypatch, lambda: _requeue_live_claim(rerun, box))

    assert box["b"] is not None
    snap = _snapshot(rerun)
    assert (snap["job"][0], snap["job"][1], snap["job"][2]) == (
        "enriching", box["b"].started_at, box["b"].retry_count)    # B's run, untouched
    assert snap["records_used"] == 2 and snap["job"][7] == 2       # B's reservation kept
    assert snap["job"][6] is not None
    assert snap["claims"] == 2 and snap["notifications"] == 0


def test_every_early_return_in_run_scrape_job_tells_terminal_from_lost():
    """One decision for every missed write: stage writes through _still_ours, fails
    through _fail_attempt, status aborts through _after_missed_write."""
    from src.workers.tasks import run_scrape_job

    body = _live(inspect.getsource(run_scrape_job.__wrapped__))
    decide = body[body.index("def _after_missed_write"):body.index("def _still_ours")]
    assert "finalize_exit(attempt_state(db, job_id, _boot_user_id, attempt_token))" in decide
    assert decide.index("db.rollback()") < decide.index("_terminal_cleanup(")
    assert 'if decision == "terminalized":' in decide
    assert "return landed or _after_missed_write()" in body
    # _fail_job is called in exactly one place, which falls back to the decision.
    (fail,) = _calls(body, "_fail_job")
    wrapper = body[body.index("def _fail_attempt"):]
    assert "if not _fail_job(" in wrapper and wrapper.index("_after_missed_write()") < \
        wrapper.index("create_notification(")
    assert len(_calls(body, "_fail_attempt")) >= 11   # def + 10 early returns
    # every status abort asks too
    for status in ('"probing"', '"scraping"', '"enriching"'):
        at = body.index(f"_set_status(db, job, {status}")
        tail = body[at:body.index("return", at)]
        assert "_after_missed_write()" in tail, status
