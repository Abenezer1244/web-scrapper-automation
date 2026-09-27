"""How much Tracerfy spend the dispatcher may still claim, and whose rows claim it.

Phase 1b-1b-ii-b (the caps), ii-c (the keyset walk that finds the rows: see "The
keyset frontier" below). The unit is the Tracerfy CREDIT: a normal lookup costs 1, an
advanced one 2 (`CREDITS_PER_ROW`). Two caps, both over a rolling 24h window of
`pending_skip_trace_rows.submitted_at`:

  * global (`SKIP_TRACE_DAILY_CREDIT_CAP`), across every tenant;
  * per account (`SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP`).

`0` disables a cap. Spend is read INSIDE the dispatcher's claim lock, after it is
acquired, in READ COMMITTED: every earlier claim committed its `submitted_at`
before releasing that lock, so each pass reads spend that includes it, and no
two passes can claim the same allowance. `submitted_at` is the claim time and is
cleared only by a proven-uncharged release (1b-1b-i), so claimed, submitted,
unknown-outcome and finished rows all count. Migration 102 makes the weight a
database fact (only 'normal'/'advanced' exist, and a spent row's type cannot
change) and gives this read its index.

Fair selection: within the allowance, accounts take turns (each account's oldest
eligible row, then each one's second, ...), so one tenant's backlog cannot starve
the rest. That holds among rows visible before the pass's first candidate read,
whenever a batch can hold one row per eligible account; below that, the earliest
first rows win.
"""
from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from sqlalchemy import (
    TIMESTAMP,
    Integer,
    String,
    any_,
    bindparam,
    case,
    cast,
    func,
    select,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.skip_trace_capacity")

# Tracerfy's price per row, in credits. The ONE copy: the dispatcher's 402 path
# and the spent query below both read it.
CREDITS_PER_ROW = {"normal": 1, "advanced": 2}

# The most rows one batch may carry, whatever the caps allow (Tracerfy handles
# large batches; this has always been the safety bound).
BATCH_ROW_LIMIT = 5000


def credits_for(trace_type: str) -> int:
    """Credits one row of this type costs. An unknown type is an error, never 1:
    counting it cheap would under-charge the cap (Codex R4)."""
    try:
        return CREDITS_PER_ROW[trace_type]
    except KeyError:
        raise ValueError(f"unknown skip-trace trace_type {trace_type!r}") from None


class Caps(NamedTuple):
    global_cap: int  # credits per rolling 24h, all tenants; 0 = disabled
    account_cap: int  # credits per rolling 24h, per account; 0 = disabled
    global_source: str  # the setting the global value came from (for operators)


_legacy_warned = False


def resolve_caps() -> Caps:
    """The effective caps. `SKIP_TRACE_DAILY_CREDIT_CAP` wins when set (0 included,
    which disables). Otherwise the deprecated `SKIP_TRACE_DAILY_ROW_CAP` is read AS
    CREDITS, with a warning once per process: 1000 "rows" becomes 1000 credits,
    which is 500 advanced lookups."""
    global _legacy_warned
    account_cap = settings.SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP or 0
    if settings.SKIP_TRACE_DAILY_CREDIT_CAP is not None:
        return Caps(settings.SKIP_TRACE_DAILY_CREDIT_CAP, account_cap,
                    "SKIP_TRACE_DAILY_CREDIT_CAP")
    legacy = settings.SKIP_TRACE_DAILY_ROW_CAP
    if legacy is None:
        return Caps(0, account_cap, "unset")
    if not _legacy_warned:
        _legacy_warned = True
        _logger.warning(
            "SKIP_TRACE_DAILY_ROW_CAP=%d is deprecated and is now read as CREDITS "
            "(normal lookup = 1, advanced = 2), so it allows %d advanced lookups a day. "
            "Set SKIP_TRACE_DAILY_CREDIT_CAP instead.", legacy, legacy // 2,
        )
    return Caps(legacy, account_cap, "SKIP_TRACE_DAILY_ROW_CAP")


def _weight_sql(trace_type_col):
    """The credit weight as SQL, built from CREDITS_PER_ROW. No ELSE: an unknown
    type yields NULL, and 102's CHECK means none exists."""
    return case(
        *[(trace_type_col == t, c) for t, c in CREDITS_PER_ROW.items()],
    )


def spent_credits(db, since: datetime) -> tuple[int, dict[str, int]]:
    """Credits claimed since `since`: (all tenants, {user_id: credits}). One query,
    riding ix_pending_skip_trace_spent."""
    from src.db.models import PendingSkipTraceRow as P

    by_user = {
        str(u): int(n)
        for u, n in db.execute(
            select(P.user_id, func.sum(_weight_sql(P.trace_type)))
            .where(P.submitted_at >= since)
            .group_by(P.user_id)
        ).all()
    }
    return sum(by_user.values()), by_user


def row_allowance(cap: int, spent: int, cost: int) -> int | None:
    """Rows of `cost` credits that still fit under `cap`. None = no cap."""
    if not cap:
        return None
    return max(0, (cap - spent) // cost)


def batch_rows(global_cap: int, global_spent: int, cost: int) -> int:
    """Rows this pass may claim in total: the global allowance, never above the
    batch bound, and never unbounded (a NULL LIMIT would drop the bound)."""
    allowed = row_allowance(global_cap, global_spent, cost)
    return BATCH_ROW_LIMIT if allowed is None else min(BATCH_ROW_LIMIT, allowed)


# ── The keyset frontier (Phase 1b-1b-ii-c) ────────────────────────────────────
#
# ii-b re-ranked every eligible row on every round. Now each account keeps a
# FRONTIER, the (enqueued_at, id) of the last row a round returned for it, and the
# next round walks that account's rows strictly after it, over migration 103's
# index. Frontiers are PASS-LOCAL and never persisted: every pass starts every
# account at the beginning, so a row that became eligible behind a frontier goes
# out on the next pass (Codex ii-c consult H1).

# The frontier every account starts from: before any real (enqueued_at, id). Typed
# in SQL and never NULL, so the tuple comparison is always defined (F1).
FRONTIER_START = ("-infinity", "00000000-0000-0000-0000-000000000000")


class Candidate(NamedTuple):
    id: str
    user_id: str
    enqueued_at: datetime


def discover_accounts(db, trace_type: str, watermark: datetime) -> list[str]:
    """Every account with a queued row of this type enqueued by `watermark`.

    A loose index scan over ix_pending_skip_trace_queued_frontier (103): one
    index probe per account, not a walk of every queued row (Codex ii-c consult
    round 2 supplied this query). Eligibility beyond 'queued' is decided per row
    by the candidate walk, so an account listed here may yet yield nothing."""
    return [str(u) for u in db.execute(text("""
        WITH RECURSIVE active_users(user_id) AS (
            (
                SELECT p.user_id
                FROM public.pending_skip_trace_rows AS p
                WHERE p.status = 'queued' AND p.trace_type = :trace_type
                  AND p.enqueued_at <= :watermark
                ORDER BY p.user_id
                LIMIT 1
            )
            UNION ALL
            SELECT nxt.user_id
            FROM active_users AS u
            CROSS JOIN LATERAL (
                SELECT p.user_id
                FROM public.pending_skip_trace_rows AS p
                WHERE p.status = 'queued' AND p.trace_type = :trace_type
                  AND p.user_id > u.user_id
                  AND p.enqueued_at <= :watermark
                ORDER BY p.user_id
                LIMIT 1
            ) AS nxt
        )
        SELECT user_id FROM active_users
    """), {"trace_type": trace_type, "watermark": watermark}).scalars()]


def round_limits(
    active: list[str],
    *,
    room_left: dict[str, int] | None,
    default_rows: int | None,
    global_left: int,
    round_no: int,
) -> tuple[dict[str, int], int]:
    """Per-account candidate limits for round `round_no`, and the round's limit.

    round_limit = min(BATCH_ROW_LIMIT, global_left * 2**r) bounds what a round
    RETURNS (and keeps the room * 4095 cutoff: at room 1 a pass inspects up to
    1 + 2 + ... + 2**11 rows). Each account with room gets
    min(room_left * 2**r, ceil(global_left / accounts_with_room) * 2**r,
    round_limit), so a round's WORK stays O((accounts + global_left) * 2**r)
    whatever the tenant count (Codex ii-c consult H2). Accounts without room are
    left out; `room_left=None` means the account cap is off."""
    grow = 2 ** round_no
    round_limit = min(BATCH_ROW_LIMIT, global_left * grow)

    def room(u: str) -> int | None:
        if room_left is None:
            return None
        return room_left.get(u, default_rows)

    with_room = [u for u in active if room(u) is None or room(u) > 0]
    if not with_room or round_limit <= 0:
        return {}, round_limit
    share = -(-global_left // len(with_room))  # ceil
    limits = {}
    for u in with_room:
        lim = min(share * grow, round_limit)
        r = room(u)
        if r is not None:
            lim = min(lim, r * grow)
        limits[u] = lim
    return limits, round_limit


def allocate(
    db,
    candidates_for,
    *,
    frontier: dict[str, tuple[str, str]],
    limits: dict[str, int],
    round_limit: int,
    watermark: datetime,
) -> list[Candidate]:
    """The next candidates enqueued by `watermark`, fairly ordered. NO locks are
    taken here.

    For each account in `limits`, `candidates_for(acct)` must return a LATERAL
    subquery of that account's eligible rows strictly after its frontier
    (`(enqueued_at, id) > (acct.after_at, acct.after_id)`), ordered by
    (enqueued_at, id), limited to `acct.lim`, with columns (id, user_id,
    enqueued_at), NO window function (ranking is done here, after the walk) and
    NO bound on enqueued_at (the watermark is applied here, after the walk; see
    below). The accounts reach SQL as four array parameters: a fixed statement
    shape at any tenant count, though psycopg2 still writes each list out element
    by element, so the text sent and the planning grow with the accounts (measured
    at 15,000 accounts, 2026-09-27: round 0 296-372 ms, 121 ms of it planning;
    the tenant-scale bound is 500 ms, Codex ii-c-2 consult). Order: rank, then
    age; at most `round_limit` rows.

    Rows after the watermark sort after every row before it, so dropping them
    trims only a TAIL of each account's walk. Because rank 1 of every account
    precedes rank 2 of any, the rows returned for an account are then a PREFIX of
    its candidates, so the caller may advance its frontier to the last row
    returned for it (F1)."""
    if not limits:
        return []
    users = list(limits)
    acct = func.unnest(
        cast(bindparam("acct_users", users, type_=ARRAY(String)),
             ARRAY(UUID(as_uuid=False))),
        cast(bindparam("acct_after_at", [frontier[u][0] for u in users], type_=ARRAY(String)),
             ARRAY(TIMESTAMP(timezone=True))),
        cast(bindparam("acct_after_id", [frontier[u][1] for u in users], type_=ARRAY(String)),
             ARRAY(UUID(as_uuid=False))),
        cast(bindparam("acct_lim", [limits[u] for u in users], type_=ARRAY(Integer)),
             ARRAY(Integer)),
    ).table_valued("user_id", "after_at", "after_id", "lim").render_derived(name="acct")
    cand = candidates_for(acct)
    # Rank OUTSIDE the lateral, over what it returned: a window function inside it
    # would force every matching row to be sorted before its LIMIT, and the planner
    # then abandons the ordered walk of 103's index for a full bitmap scan
    # (measured: 1.4 s a round at 117k queued, against milliseconds without it).
    walked = (
        select(
            cand.c.id, cand.c.user_id, cand.c.enqueued_at,
            func.row_number().over(
                partition_by=cand.c.user_id,
                order_by=(cand.c.enqueued_at, cand.c.id),
            ).label("rk"),
        )
        .select_from(acct)
        .join(cand, true())
        # The watermark OUTSIDE the walk (Postgres does not push a filter into a
        # subquery with a LIMIT). Inside it, `enqueued_at <= watermark` paired with
        # the `enqueued_at >= after_at` the planner derives from the frontier
        # compare into a range with one unknown end, which it prices at a flat
        # 0.5% of the table: ~585 rows for what is really the whole queue. It then
        # walked ix_pending_skip_trace_dispatch (status, trace_type, enqueued_at)
        # and filtered out every other account's rows, 64,680 per account.
        .where(cand.c.enqueued_at <= watermark)
        .subquery("walked")
    )
    stmt = (
        select(walked.c.id, walked.c.user_id, walked.c.enqueued_at)
        .order_by(walked.c.rk, walked.c.enqueued_at, walked.c.id)
        .limit(round_limit)
    )
    # Bitmap scans off for THIS statement only. The walk's frontier and LIMIT are
    # per-account parameters, so the planner cannot see that each account walks
    # thousands of rows: it guesses ~16 per account and prefers a bitmap that
    # reads every queued row of the type for every account. Round 0 at 117k queued
    # / 50 accounts (2026-09-27, the real statement, warm): watermark inside the
    # walk 676 ms with bitmaps off, 1,020 ms with them on; watermark outside 286 ms
    # with bitmaps on, and 1.2 ms with them off, the ordered walk of 103's index
    # that stops at the LIMIT. SET LOCAL + RESET keeps it to this one statement, and
    # the savepoint keeps it there on failure too: rolling back to it undoes the SET
    # and leaves the transaction usable, so the walk's own error is the one raised
    # (a RESET in an aborted transaction would fail and mask it: Codex ii-c-2 review).
    with db.begin_nested():
        db.execute(text("SET LOCAL enable_bitmapscan = off"))
        got = db.execute(stmt).all()
        db.execute(text("RESET enable_bitmapscan"))
    return [Candidate(str(i), str(u), at) for i, u, at in got]


def advance(frontier: dict[str, tuple[str, str]], got: list[Candidate]) -> dict[str, int]:
    """Move each account's frontier to the last row returned for it (returned rows
    are in rank order, so the last one per account is its furthest). Advances over
    EVERY returned row, whatever later becomes of it (F1). Returns the count per
    account."""
    counts: dict[str, int] = {}
    for c in got:
        frontier[c.user_id] = (c.enqueued_at.isoformat(), c.id)
        counts[c.user_id] = counts.get(c.user_id, 0) + 1
    return counts


def lock_allocated(db, ids: list[str], trace_type: str) -> list:
    """Lock the allocated rows that are STILL queued and still of this pass's type,
    skipping any another transaction holds, and return them IN ALLOCATION ORDER
    (FOR UPDATE does not keep it). The re-check matters: 102 lets an unsent row
    change type, and a row allocated as advanced but claimed after becoming normal
    would be sent at one price and counted at another."""
    if not ids:
        return []
    from src.db.models import PendingSkipTraceRow

    got = {
        str(r.id): r
        for r in db.execute(
            select(PendingSkipTraceRow)
            .where(
                PendingSkipTraceRow.id == any_(cast(
                    bindparam("allocated_ids", list(ids), type_=ARRAY(String)),
                    ARRAY(UUID(as_uuid=False)))),
                PendingSkipTraceRow.status == "queued",
                PendingSkipTraceRow.trace_type == trace_type,
            )
            .with_for_update(skip_locked=True, of=PendingSkipTraceRow)
        ).scalars()
    }
    return [got[i] for i in ids if i in got]


def take_within_caps(
    rows: list,
    *,
    account_room: dict[str, int] | None,
    default_rows: int | None,
    global_rows: int,
) -> list:
    """Walk `rows` in order and keep each one while its account and the batch
    still have room. `account_room` is each account's remaining room for the pass
    (before anything is taken); `default_rows` applies to accounts not in it;
    None disables the per-account bound."""
    taken: list = []
    per_user: dict[str, int] = {}
    for row in rows:
        if len(taken) >= global_rows:
            break
        u = str(row.user_id)
        if default_rows is not None:
            room = (account_room or {}).get(u, default_rows)
            if per_user.get(u, 0) >= room:
                continue
        per_user[u] = per_user.get(u, 0) + 1
        taken.append(row)
    return taken


__all__ = [
    "BATCH_ROW_LIMIT", "CREDITS_PER_ROW", "FRONTIER_START", "Candidate", "Caps",
    "advance", "allocate", "batch_rows", "credits_for", "discover_accounts",
    "lock_allocated", "resolve_caps", "round_limits", "row_allowance",
    "spent_credits", "take_within_caps",
]
