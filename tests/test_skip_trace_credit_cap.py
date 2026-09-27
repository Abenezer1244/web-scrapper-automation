"""The credit-weighted daily spend caps (Phase 1b-1b-ii-b).

Every Tracerfy lookup costs the operator real money: a normal one 1 credit, an
advanced one 2. Two caps over a rolling 24h window of `submitted_at`, one across
all tenants and one per account, enforced inside the dispatcher's claim lock.

DB-backed, no network. The dispatcher tests point Tracerfy at a non-HTTPS URL, so
`submit_batch` raises a definite configuration rejection before any socket opens
and the claim is released to 'errored'. A row the pass CLAIMED therefore ends
'errored'; a row it left alone stays 'queued'. That is what these tests read.
Spend that should already count is seeded as rows with `submitted_at` set.

Absorbs the old tests/test_skip_trace_daily_cap.py (Codex R11).
"""
import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.api.auth import hash_password
from src.config import settings
from src.db.models import User
from src.db.session import sync_engine, system_sync_session
from src.workers import skip_trace_capacity as cap
from src.workers import skip_trace_dispatcher
from src.workers.skip_trace_dispatcher import affordable_row_count, dispatch_pending_skip_trace

# ── Pure arithmetic ───────────────────────────────────────────────────────────


def test_credits_are_the_tracerfy_price_and_an_unknown_type_is_an_error():
    assert cap.credits_for("normal") == 1
    assert cap.credits_for("advanced") == 2
    with pytest.raises(ValueError):
        cap.credits_for("premium")


def test_the_402_path_does_not_price_an_unknown_type_at_one_credit():
    # Even when the 402 message is unparseable, the type is checked first (S4).
    with pytest.raises(ValueError):
        affordable_row_count("need 3 more credits", 10, "premium")
    with pytest.raises(ValueError):
        affordable_row_count("", 10, "premium")
    assert affordable_row_count("need 15 more credits", 10, "advanced") == 2


@pytest.mark.parametrize("cost", [1, 2])
@pytest.mark.parametrize("cap_", [0, 1, 2, 3])
@pytest.mark.parametrize("spent", [0, 1, 2, 3])
def test_row_allowance_boundaries(cap_, spent, cost):
    got = cap.row_allowance(cap_, spent, cost)
    if cap_ == 0:
        assert got is None  # disabled, not "nothing allowed"
    else:
        assert got == max(0, (cap_ - spent) // cost)
        assert got * cost + spent <= max(cap_, spent)  # never past the cap


@pytest.mark.parametrize("cost", [1, 2])
@pytest.mark.parametrize("cap_", [0, 1, 2, 3])
@pytest.mark.parametrize("spent", [0, 1, 2, 3])
def test_batch_rows_is_never_unbounded(cap_, spent, cost):
    got = cap.batch_rows(cap_, spent, cost)
    assert isinstance(got, int)
    assert 0 <= got <= cap.BATCH_ROW_LIMIT
    if cap_ == 0:
        assert got == cap.BATCH_ROW_LIMIT


def test_batch_rows_keeps_the_5000_bound_under_a_huge_cap():
    assert cap.batch_rows(10**9, 0, 1) == cap.BATCH_ROW_LIMIT


# ── Settings ──────────────────────────────────────────────────────────────────


def test_every_cap_defaults_to_unset_so_deploying_changes_nothing():
    from src.config.settings import Settings

    fields = Settings.model_fields
    for name in ("SKIP_TRACE_DAILY_CREDIT_CAP", "SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP",
                 "SKIP_TRACE_DAILY_ROW_CAP"):
        assert fields[name].default is None, name


def test_a_negative_cap_is_refused_at_boot():
    from src.config.settings import Settings

    with pytest.raises(ValueError):
        Settings.spend_caps_are_not_negative(-1)
    assert Settings.spend_caps_are_not_negative(0) == 0
    assert Settings.spend_caps_are_not_negative(None) is None


def test_the_credit_cap_wins_and_zero_disables_it_without_the_legacy_value(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_DAILY_ROW_CAP", 1000)
    monkeypatch.setattr(settings, "SKIP_TRACE_DAILY_CREDIT_CAP", 0)
    monkeypatch.setattr(settings, "SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP", None)
    assert cap.resolve_caps() == cap.Caps(0, 0, "SKIP_TRACE_DAILY_CREDIT_CAP")
    monkeypatch.setattr(settings, "SKIP_TRACE_DAILY_CREDIT_CAP", 300)
    monkeypatch.setattr(settings, "SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP", 40)
    assert cap.resolve_caps() == cap.Caps(300, 40, "SKIP_TRACE_DAILY_CREDIT_CAP")


def test_the_legacy_row_cap_is_read_as_credits_and_warned_about_once(monkeypatch, caplog):
    monkeypatch.setattr(settings, "SKIP_TRACE_DAILY_CREDIT_CAP", None)
    monkeypatch.setattr(settings, "SKIP_TRACE_DAILY_ROW_CAP", 1000)
    monkeypatch.setattr(cap, "_legacy_warned", False)
    import logging

    with caplog.at_level(logging.WARNING):
        assert cap.resolve_caps() == cap.Caps(1000, 0, "SKIP_TRACE_DAILY_ROW_CAP")
        assert cap.resolve_caps().global_cap == 1000
    warnings = [r for r in caplog.records if "deprecated" in r.getMessage()]
    assert len(warnings) == 1
    assert "500 advanced" in warnings[0].getMessage()


def test_no_cap_set_anywhere_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_DAILY_CREDIT_CAP", None)
    monkeypatch.setattr(settings, "SKIP_TRACE_DAILY_ROW_CAP", None)
    monkeypatch.setattr(settings, "SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP", None)
    assert cap.resolve_caps() == cap.Caps(0, 0, "unset")


def test_the_dispatcher_does_not_shadow_module_scope_datetime():
    """Regression: a function-local `from datetime import datetime` once rebound
    `datetime` as LOCAL for the whole dispatcher, so every later
    `datetime.now(UTC)` raised UnboundLocalError on the path where it did not run."""
    src = inspect.getsource(skip_trace_dispatcher.dispatch_pending_skip_trace.run)
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert "from datetime import" not in code


# ── Seeding ───────────────────────────────────────────────────────────────────


@pytest.fixture
def make_account(db):
    """Real users, created through the same session the suite tears down."""
    async def _make() -> str:
        user = User(
            id=str(uuid.uuid4()), email=f"test_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
            password_hash=hash_password("TestPass123!"), plan="business",
            records_used=0, records_limit=5000,
        )
        db.add(user)
        await db.commit()
        return user.id
    return _make


_seq = iter(range(1, 10**6))


def _seed(user_id: str, *, address: str | None = None, trace_type: str = "normal",
          status: str = "queued", submitted_at=None, enqueued_at=None,
          tracerfy_queue_id=None) -> str:
    """scraper_config -> job (done) -> result (queued) -> one pending row. Every row
    gets its own address unless one is given, so the in-flight hold never links
    two rows by accident. Returns the pending id."""
    n = next(_seq)
    address = address or f"{n} CAP TEST ST"
    sc_id, job_id, rid, pid = (str(uuid.uuid4()) for _ in range(4))
    enqueued_at = enqueued_at or datetime.now(UTC) - timedelta(hours=1) + timedelta(
        milliseconds=n)
    with system_sync_session() as db:
        db.execute(text("""
            INSERT INTO scraper_configs (id, user_id, name, county, state, record_type,
                fields, enrichment, schedule, deliver, skip_trace_enabled, active)
            VALUES (:sc, :u, 'cap test', 'pierce', 'WA', 'probate', '[]'::json,
                    '[]'::json, '{"frequency":"manual"}'::json,
                    '{"format":"csv","emails":[]}'::json, true, true)
        """), {"sc": sc_id, "u": user_id})
        db.execute(text("""
            INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger, page_current,
                              page_total, record_count, retry_count)
            VALUES (:j, :u, :sc, 'done', 'manual', 0, 0, 0, 0)
        """), {"j": job_id, "u": user_id, "sc": sc_id})
        db.execute(text("""
            INSERT INTO results (id, job_id, user_id, is_duplicate, skip_trace_status,
                                 party_name, property_address, enrichment_data, created_at)
            VALUES (:r, :j, :u, false, :rs, 'CAP TEST OWNER', :a, '{}'::json, now())
        """), {"r": rid, "j": job_id, "u": user_id, "a": address,
               "rs": "queued" if status == "queued" else "submitted"})
        db.execute(text("""
            INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id,
                property_address, city, state, trace_type, status, enqueued_at,
                submitted_at, tracerfy_queue_id)
            VALUES (:p, :j, :r, :u, :a, 'TACOMA', 'WA', :tt, :st, :eq, :sub, :q)
        """), {"p": pid, "j": job_id, "r": rid, "u": user_id, "a": address, "tt": trace_type,
               "st": status, "eq": enqueued_at, "sub": submitted_at, "q": tracerfy_queue_id})
        db.commit()
    return pid


def _spent(user_id: str, credits: int, *, trace_type: str = "normal", ago=timedelta(hours=1)):
    """Spend already inside the window: accepted rows, submitted `ago`."""
    per = cap.credits_for(trace_type)
    assert credits % per == 0
    for _ in range(credits // per):
        _seed(user_id, trace_type=trace_type, status="submitted",
              submitted_at=datetime.now(UTC) - ago, tracerfy_queue_id=900000)


def _claimed(pids) -> set[str]:
    """Which of these rows the pass claimed (definite rejection -> 'errored')."""
    with system_sync_session() as db:
        return {str(p) for p in db.execute(text(
            "SELECT id FROM pending_skip_trace_rows "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND status = 'errored'"
        ), {"ids": list(pids)}).scalars()}


@pytest.fixture
def dispatcher(monkeypatch):
    """Dispatcher on, Tracerfy unreachable by construction, no caps unless a test sets one."""
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(settings, "TRACERFY_API_BASE_URL", "http://tracerfy.invalid")
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
    monkeypatch.setattr(settings, "SKIP_TRACE_MAX_BATCHES_PER_TICK", 1)
    for name in ("SKIP_TRACE_DAILY_CREDIT_CAP", "SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP",
                 "SKIP_TRACE_DAILY_ROW_CAP"):
        monkeypatch.setattr(settings, name, None)

    def _caps(global_cap=None, account_cap=None):
        monkeypatch.setattr(settings, "SKIP_TRACE_DAILY_CREDIT_CAP", global_cap)
        monkeypatch.setattr(settings, "SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP", account_cap)
    return _caps


# ── What counts as spent ──────────────────────────────────────────────────────


async def test_spend_counts_claims_and_unknown_outcomes_but_not_releases_or_old_rows(
        make_account):
    u = await make_account()
    now = datetime.now(UTC)
    _seed(u, trace_type="advanced", status="submitting", submitted_at=now)  # unknown: 2
    _seed(u, status="submitted", submitted_at=now, tracerfy_queue_id=1)     # accepted: 1
    _seed(u, status="errored", submitted_at=None)                            # released: 0
    _seed(u, status="queued")                                                # never sent: 0
    _seed(u, status="done", submitted_at=now - timedelta(days=1, seconds=1))  # expired: 0
    with system_sync_session() as db:
        total, by_user = cap.spent_credits(db, now - timedelta(days=1))
    assert by_user.get(u) == 3
    assert total >= 3


# ── The caps inside the claim ─────────────────────────────────────────────────


async def test_an_account_at_its_cap_waits_while_others_are_served(make_account, dispatcher):
    full, fresh = await make_account(), await make_account()
    dispatcher(account_cap=4)
    _spent(full, 4)
    waits = [_seed(full) for _ in range(3)]
    served = [_seed(fresh) for _ in range(3)]

    dispatch_pending_skip_trace()

    assert _claimed(waits) == set()
    assert _claimed(served) == set(served)


async def test_an_account_below_its_cap_gets_exactly_what_fits(make_account, dispatcher):
    u = await make_account()
    dispatcher(account_cap=7)
    _spent(u, 2)  # 5 credits left
    advanced = [_seed(u, trace_type="advanced") for _ in range(4)]

    dispatch_pending_skip_trace()

    assert len(_claimed(advanced)) == 2  # floor(5 / 2)


async def test_one_credit_left_buys_a_normal_lookup_but_not_an_advanced_one(
        make_account, dispatcher):
    n_acct, a_acct = await make_account(), await make_account()
    dispatcher(account_cap=5)
    _spent(n_acct, 4)
    _spent(a_acct, 4)
    normal = [_seed(n_acct) for _ in range(2)]
    advanced = [_seed(a_acct, trace_type="advanced") for _ in range(2)]

    dispatch_pending_skip_trace()

    assert len(_claimed(normal)) == 1
    assert _claimed(advanced) == set()


async def test_the_global_cap_binds_every_account_together(make_account, dispatcher):
    a, b = await make_account(), await make_account()
    dispatcher(global_cap=10)
    _spent(a, 7)
    rows = [_seed(a) for _ in range(3)] + [_seed(b) for _ in range(3)]

    dispatch_pending_skip_trace()

    assert len(_claimed(rows)) == 3


async def test_spend_committed_by_another_tick_after_the_early_check_is_honoured(
        make_account, dispatcher, monkeypatch):
    """The cap is hard because spend is read INSIDE the claim lock. Here another
    tick's claim commits after this tick's early check but before its lock (the
    settle step runs in between): the pass must see it."""
    u = await make_account()
    dispatcher(account_cap=5)
    rows = [_seed(u) for _ in range(5)]
    real_settle = skip_trace_dispatcher._settle_queued_from_known_answers

    def settle_then_another_tick_claims(db):
        out = real_settle(db)
        _spent(u, 3, ago=timedelta(seconds=1))
        return out
    monkeypatch.setattr(skip_trace_dispatcher, "_settle_queued_from_known_answers",
                        settle_then_another_tick_claims)

    dispatch_pending_skip_trace()

    assert len(_claimed(rows)) == 2


# ── Fairness ──────────────────────────────────────────────────────────────────


async def test_one_backlog_does_not_starve_the_accounts_behind_it(make_account, dispatcher):
    big = await make_account()
    others = [await make_account() for _ in range(4)]
    dispatcher(global_cap=5)
    base = datetime.now(UTC) - timedelta(hours=3)
    backlog = [_seed(big, enqueued_at=base + timedelta(seconds=i)) for i in range(20)]
    later = [_seed(o, enqueued_at=base + timedelta(hours=1)) for o in others]

    dispatch_pending_skip_trace()

    assert _claimed(later) == set(later)
    assert _claimed(backlog) == {backlog[0]}


async def test_with_less_headroom_than_accounts_the_earliest_first_rows_win(
        make_account, dispatcher):
    # R5, documented behaviour: fairness needs room for one row per account.
    accts = [await make_account() for _ in range(3)]
    dispatcher(global_cap=2)
    base = datetime.now(UTC) - timedelta(hours=2)
    firsts = [_seed(a, enqueued_at=base + timedelta(minutes=i)) for i, a in enumerate(accts)]

    dispatch_pending_skip_trace()

    assert _claimed(firsts) == set(firsts[:2])


async def test_with_no_cap_everything_eligible_goes_out(make_account, dispatcher):
    a, b = await make_account(), await make_account()
    dispatcher()
    rows = [_seed(a) for _ in range(4)] + [_seed(b) for _ in range(4)]

    dispatch_pending_skip_trace()

    assert _claimed(rows) == set(rows)


# ── Refill ────────────────────────────────────────────────────────────────────


def _in_flight_twin_of(user_id: str, address: str):
    """The same account's lookup for this address, already at Tracerfy: the queued
    twin is held by _hold_answers_in_flight."""
    _seed(user_id, address=address, status="submitted",
          submitted_at=datetime.now(UTC) - timedelta(days=2), tracerfy_queue_id=900001)


async def test_a_held_head_is_replaced_instead_of_costing_its_account_a_turn(
        make_account, dispatcher):
    u = await make_account()
    dispatcher(account_cap=1)
    base = datetime.now(UTC) - timedelta(hours=2)
    _in_flight_twin_of(u, "1 HELD HEAD RD")
    head = _seed(u, address="1 HELD HEAD RD", enqueued_at=base)
    second = _seed(u, enqueued_at=base + timedelta(minutes=1))

    dispatch_pending_skip_trace()

    assert _claimed([head, second]) == {second}


async def test_a_run_of_held_heads_longer_than_one_round_is_passed(make_account, dispatcher):
    u = await make_account()
    dispatcher(account_cap=1)
    base = datetime.now(UTC) - timedelta(hours=2)
    heads = []
    for i in range(6):  # more than rounds 0 and 1 allocate at room 1 (1 + 2)
        _in_flight_twin_of(u, f"{i} HELD RUN RD")
        heads.append(_seed(u, address=f"{i} HELD RUN RD", enqueued_at=base + timedelta(seconds=i)))
    good = _seed(u, enqueued_at=base + timedelta(minutes=5))

    dispatch_pending_skip_trace()

    assert _claimed(heads + [good]) == {good}


async def test_a_locked_head_is_replaced(make_account, dispatcher):
    u = await make_account()
    dispatcher(account_cap=1)
    base = datetime.now(UTC) - timedelta(hours=2)
    head = _seed(u, enqueued_at=base)
    second = _seed(u, enqueued_at=base + timedelta(minutes=1))

    with sync_engine.connect() as other_tick:
        other_tick.execute(text(
            "SELECT 1 FROM pending_skip_trace_rows WHERE id = :p FOR UPDATE"), {"p": head})
        dispatch_pending_skip_trace()
        other_tick.rollback()

    assert _claimed([head, second]) == {second}


async def test_the_duplicate_hold_spans_refill_rounds(make_account, dispatcher):
    """R2: a row that only appears in a refill round must still be held when it
    shares an address with a survivor from an earlier round, or both go out in one
    batch, are paid twice, and are refused together at ingest."""
    x, y = await make_account(), await make_account()
    dispatcher(account_cap=1)
    base = datetime.now(UTC) - timedelta(hours=2)
    _in_flight_twin_of(x, "7 X HELD RD")
    _seed(x, address="7 X HELD RD", enqueued_at=base)                  # round 0: held
    y1 = _seed(y, address="9 SHARED ST", enqueued_at=base + timedelta(seconds=1))
    x2 = _seed(x, address="9 SHARED ST", enqueued_at=base + timedelta(seconds=2))

    dispatch_pending_skip_trace()

    assert _claimed([y1, x2]) == {y1}


async def test_a_row_committed_mid_pass_never_takes_an_account_past_its_cap(
        make_account, dispatcher, monkeypatch):
    u = await make_account()
    dispatcher(account_cap=2)
    base = datetime.now(UTC) - timedelta(hours=2)
    _in_flight_twin_of(u, "3 MID PASS RD")
    rows = [_seed(u, address="3 MID PASS RD", enqueued_at=base)]
    real_allocate = skip_trace_dispatcher.allocate
    fired = []

    def allocate_while_a_row_commits(*a, **k):
        out = real_allocate(*a, **k)
        if not fired:
            fired.append(1)
            # Inserted before the pass began (enqueued_at earlier), committed now.
            rows.extend(_seed(u, enqueued_at=base - timedelta(minutes=5)) for _ in range(3))
        return out
    monkeypatch.setattr(skip_trace_dispatcher, "allocate", allocate_while_a_row_commits)

    dispatch_pending_skip_trace()

    assert len(_claimed(rows)) <= 2


# ── Bounded refill (U2, V2-V4) ────────────────────────────────────────────────


async def test_the_round_limit_claims_what_it_found_and_says_so(
        make_account, dispatcher, monkeypatch, caplog):
    blocked, ok = await make_account(), await make_account()
    dispatcher(account_cap=1)
    monkeypatch.setattr(skip_trace_dispatcher, "_REFILL_MAX_ROUNDS", 2)  # inspects 1 + 2
    base = datetime.now(UTC) - timedelta(hours=2)
    heads = []
    for i in range(3):
        _in_flight_twin_of(blocked, f"{i} CUTOFF RD")
        heads.append(_seed(blocked, address=f"{i} CUTOFF RD",
                           enqueued_at=base + timedelta(seconds=i)))
    beyond = _seed(blocked, enqueued_at=base + timedelta(minutes=5))
    served = _seed(ok, enqueued_at=base)

    import logging
    with caplog.at_level(logging.WARNING):
        dispatch_pending_skip_trace()

    assert _claimed(heads + [beyond, served]) == {served}
    lines = [r.getMessage() for r in caplog.records if "refill_truncated" in r.getMessage()]
    assert len(lines) == 1 and "reason=round_limit" in lines[0]
    # V3: the operator can see who is still waiting (the blocked account).
    assert "accounts_with_room=1" in lines[0]
    assert "rounds=2" in lines[0] and "survivors=1" in lines[0]


async def test_equal_timestamps_are_ordered_by_id_and_none_is_skipped(
        make_account, dispatcher):
    """F4: the frontier is (enqueued_at, id). Two rows sharing enqueued_at must both
    be reachable: the held one first by id, then its twin in the next round."""
    u = await make_account()
    dispatcher(account_cap=1)
    same = datetime.now(UTC) - timedelta(hours=2)
    a = _seed(u, address="1 TIE ST", enqueued_at=same)
    b = _seed(u, address="2 TIE ST", enqueued_at=same)
    first, second = sorted([a, b])
    held_address = "1 TIE ST" if first == a else "2 TIE ST"
    _in_flight_twin_of(u, held_address)

    dispatch_pending_skip_trace()

    assert _claimed([a, b]) == {second}


async def test_an_account_cut_by_the_rounds_limit_comes_back_next_round(
        make_account, dispatcher):
    """F4: rows the round's global LIMIT cuts are not returned, not passed, and the
    account is NOT retired: it is served in the next round."""
    accts = [await make_account() for _ in range(4)]
    dispatcher(global_cap=3, account_cap=1)
    base = datetime.now(UTC) - timedelta(hours=2)
    _in_flight_twin_of(accts[0], "0 CUT ST")
    held = _seed(accts[0], address="0 CUT ST", enqueued_at=base)  # its only row
    b = _seed(accts[1], enqueued_at=base + timedelta(seconds=1))
    c = _seed(accts[2], enqueued_at=base + timedelta(seconds=2))
    d = _seed(accts[3], enqueued_at=base + timedelta(seconds=3))    # cut in round 0

    dispatch_pending_skip_trace()

    assert _claimed([held, b, c, d]) == {b, c, d}


async def test_a_short_result_does_not_retire_an_account(make_account, dispatcher, monkeypatch):
    """F3: under READ COMMITTED a row can become available mid-pass AHEAD of its
    account's frontier. An account whose walk came back short keeps its turn; only
    a walk that returns nothing retires it."""
    u = await make_account()
    dispatcher(account_cap=2)
    base = datetime.now(UTC) - timedelta(hours=2)
    _in_flight_twin_of(u, "4 SHORT RD")
    held = _seed(u, address="4 SHORT RD", enqueued_at=base)  # round 0: 1 row of 2, held
    real_allocate = skip_trace_dispatcher.allocate
    late: list[str] = []

    def allocate_then_commit_a_row(*a, **k):
        out = real_allocate(*a, **k)
        if not late:
            # Enqueued before the pass began and after the frontier; committed now.
            late.append(_seed(u, enqueued_at=base + timedelta(minutes=1)))
        return out
    monkeypatch.setattr(skip_trace_dispatcher, "allocate", allocate_then_commit_a_row)

    dispatch_pending_skip_trace()

    assert _claimed([held, *late]) == set(late)


async def test_an_account_first_held_in_a_later_round_is_still_read_for_in_flight_twins(
        make_account, dispatcher):
    """F5: the pass's in-flight cache records which accounts it READ, apart from the
    keys. An account whose rows first reach the hold in a later round (its head was
    locked by another tick) must still be read, though other accounts' keys are
    already cached, or its twin of an in-flight lookup is bought twice."""
    x, y = await make_account(), await make_account()
    dispatcher(account_cap=1)
    base = datetime.now(UTC) - timedelta(hours=2)
    _in_flight_twin_of(x, "8 X TWIN RD")
    _seed(x, address="8 X TWIN RD", enqueued_at=base)            # round 0: x read and cached
    y_head = _seed(y, enqueued_at=base)                            # locked by another tick
    _in_flight_twin_of(y, "8 Y TWIN RD")
    y_twin = _seed(y, address="8 Y TWIN RD", enqueued_at=base + timedelta(minutes=1))

    with sync_engine.connect() as other_tick:
        other_tick.execute(text(
            "SELECT 1 FROM pending_skip_trace_rows WHERE id = :p FOR UPDATE"), {"p": y_head})
        dispatch_pending_skip_trace()
        other_tick.rollback()

    assert _claimed([y_head, y_twin]) == set()


def test_round_limits_keep_the_documented_cutoff_at_any_headroom():
    """H2: at room 1 a pass inspects 1 + 2 + ... + 2**11 = 4095 rows of a blocked
    account, even with ONE row of global headroom left."""
    total = 0
    for r in range(12):
        limits, round_limit = cap.round_limits(
            ["u"], room_left={"u": 1}, default_rows=1, global_left=1, round_no=r)
        total += limits["u"]
        assert round_limit == min(cap.BATCH_ROW_LIMIT, 2 ** r)
    assert total == 4095


def test_round_limits_bound_a_rounds_work_whatever_the_tenant_count():
    many = [str(i) for i in range(15000)]
    limits, round_limit = cap.round_limits(
        many, room_left=None, default_rows=None, global_left=5000, round_no=0)
    assert round_limit == 5000
    assert set(limits.values()) == {1}                 # ceil(5000 / 15000)
    limits, _ = cap.round_limits(
        many, room_left=None, default_rows=None, global_left=5000, round_no=3)
    assert sum(limits.values()) <= 15000 * 8           # O((accounts + left) * 2**r)


def test_round_limits_leave_out_accounts_without_room():
    limits, _ = cap.round_limits(
        ["full", "fresh", "known"], room_left={"full": 0, "known": 2}, default_rows=1,
        global_left=10, round_no=1)
    assert "full" not in limits
    assert limits["fresh"] == 2          # default room 1 * 2**1
    assert limits["known"] == 4          # room 2 * 2**1


async def test_discovery_lists_only_accounts_with_queued_rows_of_the_type(make_account):
    a, b, c, d = [await make_account() for _ in range(4)]
    now = datetime.now(UTC)
    _seed(a)                                             # queued normal: found
    _seed(b, trace_type="advanced")                      # other type
    _seed(c, status="submitted", submitted_at=now, tracerfy_queue_id=1)  # not queued
    _seed(d, enqueued_at=now + timedelta(hours=1))       # after the watermark
    with system_sync_session() as db:
        found = set(cap.discover_accounts(db, "normal", now))
    assert a in found
    assert not found & {b, c, d}


async def test_a_row_enqueued_after_the_pass_began_is_not_claimed(make_account, dispatcher):
    """V1's watermark, now applied by allocate() AFTER the walk (inside it, it misled
    the planner). No cap at all, so only the watermark keeps the later row out."""
    u = await make_account()
    dispatcher()
    old = _seed(u)
    later = _seed(u, enqueued_at=datetime.now(UTC) + timedelta(hours=1))

    dispatch_pending_skip_trace()

    assert _claimed([old, later]) == {old}
    with system_sync_session() as db:
        assert db.execute(text("SELECT status FROM pending_skip_trace_rows WHERE id = :p"),
                          {"p": later}).scalar() == "queued"


def _bulk_held_run(user_id: str, n_held: int, prefix: str) -> str:
    """n_held queued rows for one account, each with an in-flight twin (so each is
    held), followed by ONE good row. Seeded set-wise: the per-row helper is too slow
    for thousands. Returns the good row's pending id."""
    sc, job, twin_job = (str(uuid.uuid4()) for _ in range(3))
    base = datetime.now(UTC) - timedelta(hours=3)
    with system_sync_session() as db:
        db.execute(text("""
            INSERT INTO scraper_configs (id, user_id, name, county, state, record_type, fields,
                enrichment, schedule, deliver, skip_trace_enabled, active)
            VALUES (:sc, :u, 'cap bulk', 'pierce', 'WA', 'probate', '[]'::json, '[]'::json,
                    '{"frequency":"manual"}'::json, '{"format":"csv","emails":[]}'::json, true, true)
        """), {"sc": sc, "u": user_id})
        for j in (job, twin_job):
            db.execute(text("""
                INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger, page_current,
                                  page_total, record_count, retry_count)
                VALUES (:j, :u, :sc, 'done', 'manual', 0, 0, 0, 0)
            """), {"j": j, "u": user_id, "sc": sc})
        for job_id, status, rstatus in ((job, "queued", "queued"),
                                        (twin_job, "submitted", "submitted")):
            db.execute(text("""
                WITH s AS (SELECT g, gen_random_uuid() AS rid FROM generate_series(1, :n) g),
                r AS (INSERT INTO results (id, job_id, user_id, is_duplicate, skip_trace_status,
                                           party_name, property_address, enrichment_data, created_at)
                      SELECT rid, :j, :u, false, :rs, 'CAP TEST OWNER',
                             :p || '-' || g || ' BULK ST', '{}'::json, now() FROM s RETURNING 1)
                INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id,
                    property_address, city, state, trace_type, status, enqueued_at,
                    submitted_at, tracerfy_queue_id)
                SELECT gen_random_uuid(), :j, rid, :u, :p || '-' || g || ' BULK ST', 'TACOMA', 'WA',
                       'normal', :st, CAST(:base AS timestamptz) + g * interval '1 ms',
                       CASE WHEN :st = 'submitted' THEN now() - interval '3 days' END,
                       CASE WHEN :st = 'submitted' THEN 900003 END
                FROM s
            """), {"n": n_held, "j": job_id, "u": user_id, "rs": rstatus, "st": status,
                   "p": prefix, "base": base})
        db.commit()
    return _seed(user_id, enqueued_at=base + timedelta(minutes=30))


@pytest.mark.parametrize(("n_held", "reached"), [(4094, True), (4095, False)])
async def test_the_documented_cutoff_is_room_times_4095(
        make_account, dispatcher, monkeypatch, n_held, reached):
    """V2/V4 with the DEFAULT 12 rounds: at room 1 a pass inspects 1 + 2 + ... + 2**11
    = 4095 rows. A good row behind 4094 held ones is reached; behind 4095 it is not.
    The deadline is lifted so only the round limit decides."""
    u = await make_account()
    dispatcher(account_cap=1)
    monkeypatch.setattr(skip_trace_dispatcher, "_REFILL_DEADLINE", timedelta(minutes=5))
    good = _bulk_held_run(u, n_held, f"C{n_held}")

    dispatch_pending_skip_trace()

    assert (_claimed([good]) == {good}) is reached


# ── Review findings (Codex ii-b diff review) ──────────────────────────────────


async def test_the_account_list_is_four_parameters_at_any_tenant_scale(make_account):
    """The per-account frontier and limit reach SQL as four arrays, so the statement
    does not grow with the tenant count; and the walk still decides correctly with
    5,001 accounts in it (an account with no limit is not walked at all)."""
    from sqlalchemy import select as sa_select
    from sqlalchemy import tuple_

    from src.db.models import PendingSkipTraceRow as P

    full, fresh = await make_account(), await make_account()
    [_seed(full) for _ in range(2)]
    fresh_rows = [_seed(fresh) for _ in range(2)]
    strangers = {str(uuid.uuid4()): 3 for _ in range(5000)}

    def candidates_for(acct):
        return (sa_select(P.id, P.user_id, P.enqueued_at)
                .where(P.status == "queued", P.user_id == acct.c.user_id,
                       tuple_(P.enqueued_at, P.id) > tuple_(acct.c.after_at, acct.c.after_id))
                .order_by(P.enqueued_at, P.id).limit(acct.c.lim).lateral("cand"))

    compiled_params = []
    with system_sync_session() as db:
        for limits in ({fresh: 1}, {fresh: 1, **strangers}):
            class _Capture:
                def execute(self, stmt, *a, **k):
                    if hasattr(stmt, "selected_columns"):  # the walk, not SET/RESET
                        compiled_params.append(len(stmt.compile().params))
                    return db.execute(stmt, *a, **k)
            frontier = dict.fromkeys(limits, cap.FRONTIER_START)
            got = cap.allocate(_Capture(), candidates_for, frontier=frontier,
                               limits=limits, round_limit=5000,
                               watermark=datetime.now(UTC))
            assert [c.id for c in got] == [fresh_rows[0]], "only the walked account, 1 row"

    assert compiled_params[0] == compiled_params[1], compiled_params


async def test_a_row_retyped_between_allocation_and_lock_is_not_claimed_at_the_wrong_price(
        make_account, dispatcher, monkeypatch):
    """P1: 102 lets an UNSENT row change type. One allocated as advanced and
    retyped to normal before the lock must not be sent in the advanced batch while
    the ledger charges it 1 credit."""
    u = await make_account()
    dispatcher()
    rows = [_seed(u, trace_type="advanced") for _ in range(3)]
    real_allocate = skip_trace_dispatcher.allocate
    retyped: list[str] = []

    def allocate_then_retype(*a, **k):
        got = real_allocate(*a, **k)
        if got and not retyped:
            retyped.append(got[0].id)
            with system_sync_session() as other:
                other.execute(text("UPDATE pending_skip_trace_rows SET trace_type = 'normal' "
                                   "WHERE id = :p"), {"p": got[0].id})
                other.commit()
        return got
    monkeypatch.setattr(skip_trace_dispatcher, "allocate", allocate_then_retype)

    dispatch_pending_skip_trace()

    assert retyped, "the advanced pass allocated nothing"
    assert _claimed(rows) == set(rows) - set(retyped)
    with system_sync_session() as db:
        assert db.execute(text("SELECT status, trace_type FROM pending_skip_trace_rows "
                               "WHERE id = :p"), {"p": retyped[0]}).one() == ("queued", "normal")


async def test_a_full_account_is_not_even_allocated(make_account, dispatcher, monkeypatch):
    """A zero-room account is never walked: it gets no candidate limit, so the pass
    never locks or inspects rows it could not claim."""
    u = await make_account()
    dispatcher(account_cap=2)
    _spent(u, 2)
    queued = [_seed(u) for _ in range(5)]
    real_allocate = skip_trace_dispatcher.allocate
    seen: list[dict] = []

    def recording_allocate(*a, **k):
        seen.append(dict(k["limits"]))
        return real_allocate(*a, **k)
    monkeypatch.setattr(skip_trace_dispatcher, "allocate", recording_allocate)

    dispatch_pending_skip_trace()

    assert _claimed(queued) == set()
    assert all(u not in limits for limits in seen), seen


async def test_a_second_claimer_is_shut_out_while_a_pass_holds_the_lock(
        make_account, dispatcher, monkeypatch):
    """Two dispatchers at once (a beat double-fire): while one pass holds the claim
    lock, the other defers and claims nothing, so they cannot both spend the same
    allowance. Real second session, real advisory lock, deterministic order."""
    import threading

    u = await make_account()
    dispatcher(account_cap=3)
    rows = [_seed(u) for _ in range(6)]
    real_allocate = skip_trace_dispatcher.allocate
    second: dict = {}

    def allocate_while_another_tick_runs(*a, **k):
        if not second:
            t = threading.Thread(target=lambda: second.update(out=dispatch_pending_skip_trace()))
            second["started"] = True
            t.start()
            t.join(timeout=60)
        return real_allocate(*a, **k)
    monkeypatch.setattr(skip_trace_dispatcher, "allocate", allocate_while_another_tick_runs)

    dispatch_pending_skip_trace()

    assert second["out"].get("deferred") == "claim_locked"
    assert second["out"]["submitted_rows"] == 0
    assert len(_claimed(rows)) == 3


async def test_an_early_deadline_claims_the_survivors_found_so_far(
        make_account, dispatcher, monkeypatch, caplog):
    blocked, ok = await make_account(), await make_account()
    dispatcher(account_cap=1)
    monkeypatch.setattr(skip_trace_dispatcher, "_REFILL_DEADLINE", timedelta(0))
    base = datetime.now(UTC) - timedelta(hours=2)
    _in_flight_twin_of(blocked, "5 DEADLINE RD")
    head = _seed(blocked, address="5 DEADLINE RD", enqueued_at=base)
    second = _seed(blocked, enqueued_at=base + timedelta(minutes=1))
    served = _seed(ok, enqueued_at=base)

    import logging
    with caplog.at_level(logging.WARNING):
        dispatch_pending_skip_trace()

    assert _claimed([head, second, served]) == {served}
    assert any("refill_truncated reason=deadline" in r.getMessage() for r in caplog.records)


async def test_a_batch_that_fills_is_never_reported_as_truncated(
        make_account, dispatcher, monkeypatch, caplog):
    """The pass checks for a full batch BEFORE its limits, so a batch that fills on
    its last allowed round is not logged as cut short (Codex ii-c-2 consult)."""
    u = await make_account()
    dispatcher(global_cap=2)
    monkeypatch.setattr(skip_trace_dispatcher, "_REFILL_MAX_ROUNDS", 1)
    monkeypatch.setattr(skip_trace_dispatcher, "_REFILL_DEADLINE", timedelta(0))
    rows = [_seed(u) for _ in range(2)]

    import logging
    with caplog.at_level(logging.WARNING):
        dispatch_pending_skip_trace()

    assert _claimed(rows) == set(rows)
    assert not [r for r in caplog.records if "refill_truncated" in r.getMessage()]


# ── The early exit ────────────────────────────────────────────────────────────


async def test_the_tick_is_held_and_ops_paged_once_the_global_cap_is_spent(
        make_account, dispatcher, monkeypatch):
    u = await make_account()
    dispatcher(global_cap=4)
    _spent(u, 4, trace_type="advanced")
    queued = _seed(u)
    alerts: list[str] = []
    import src.workers.ops_alerts as ops
    monkeypatch.setattr(ops, "send_ops_alert", lambda *a, **k: alerts.append(a[0]))

    out = dispatch_pending_skip_trace()

    assert out["skipped"] == "daily_cap"
    assert out["spent_credits"] >= 4 and out["cap_credits"] == 4
    assert out["cap_source"] == "SKIP_TRACE_DAILY_CREDIT_CAP"
    assert "skip_trace_daily_cap" in alerts
    assert _claimed([queued]) == set()
