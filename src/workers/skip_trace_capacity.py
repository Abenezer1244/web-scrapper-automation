"""How much Tracerfy spend the dispatcher may still claim, and whose rows claim it.

Phase 1b-1b-ii-b. The unit is the Tracerfy CREDIT: a normal lookup costs 1, an
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
    Integer,
    String,
    any_,
    bindparam,
    case,
    cast,
    func,
    literal,
    select,
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


def allocate(
    db,
    eligible,
    *,
    account_rows: dict[str, int],
    default_rows: int | None,
    lookahead: int,
    limit: int,
) -> list[str]:
    """Ids of the next candidates, fairly ordered. NO locks are taken here.

    `eligible` is a subquery with columns (id, user_id, enqueued_at), already
    carrying every eligibility predicate, the pass watermark, and the exclusion
    of ids already considered this pass, so row_number() ranks what is LEFT.

    Per account, up to `rooms * lookahead` rows are allocated, where an account's
    room is `account_rows[user]`, or `default_rows` for an account with no spend
    and nothing taken yet; `default_rows=None` means the account cap is off (no
    per-account bound). Order: rank, then age. At most `limit` rows.
    """
    ranked_cols = [
        eligible.c.id, eligible.c.user_id, eligible.c.enqueued_at,
        func.row_number().over(
            partition_by=eligible.c.user_id,
            order_by=(eligible.c.enqueued_at, eligible.c.id),
        ).label("rn"),
    ]
    source = eligible
    if default_rows is None:
        room = None
    elif account_rows:
        # Two array parameters zipped by unnest, not a VALUES list: the statement
        # and its parameter count stay fixed however many accounts spent today
        # (Codex ii-b review round 2).
        users = list(account_rows)
        rooms = [account_rows[u] for u in users]
        acct = func.unnest(
            cast(bindparam("room_users", users, type_=ARRAY(String)),
                 ARRAY(UUID(as_uuid=False))),
            cast(bindparam("room_rows", rooms, type_=ARRAY(Integer)),
                 ARRAY(Integer)),
        ).table_valued("user_id", "rows").render_derived(name="acct_room")
        source = eligible.outerjoin(acct, acct.c.user_id == eligible.c.user_id)
        room = func.coalesce(acct.c.rows, default_rows)
    else:
        room = literal(default_rows, Integer)
    if room is not None:
        ranked_cols.append((room * lookahead).label("room"))
    ranked = select(*ranked_cols).select_from(source).subquery("ranked")
    stmt = select(ranked.c.id)
    if room is not None:
        stmt = stmt.where(ranked.c.rn <= ranked.c.room)
    stmt = stmt.order_by(ranked.c.rn, ranked.c.enqueued_at, ranked.c.id).limit(limit)
    return [str(i) for i in db.execute(stmt).scalars()]


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
    "BATCH_ROW_LIMIT", "CREDITS_PER_ROW", "Caps", "allocate", "batch_rows",
    "credits_for", "lock_allocated", "resolve_caps", "row_allowance",
    "spent_credits", "take_within_caps",
]
