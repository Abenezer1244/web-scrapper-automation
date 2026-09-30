"""Which leads a "look up contacts" action would buy: the planner shared by the quote
(Phase 1b-1c) and the action worker (Phase 1b-2), so the two cannot drift.

Contract: tasks/todo-lookup-contacts.md, "FINAL 1b-1c contract and build list".

`classify()` mirrors the scrape enqueue's gates (`_enqueue_skip_trace_rows`,
src/workers/tasks_helpers/enrich.py) in their order, reading only what the API role
can see: the `results` row. The enqueue ALSO reads two things the API cannot (the
charged-unanswered pending rows and the lookup cache), so what the planner quotes is
an UPPER BOUND: the worker can only lower it, never raise it (finding 16-8). Where
this module differs from the enqueue it only ever excludes MORE:

  * the settled-complaint check is source-keyed and unconditional (the enqueue runs it
    on code-violation jobs only; `is_settled` is False for every other source);
  * the ATIP policy is PINNED once per request (15-14); `build_pending_row_payload`
    re-reads the process flag, so a stricter process flag relabels such a row
    `not_traceable`, never quotes it.

Imports no `src.workers` module: the API imports this, and that package builds the
Celery app.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import NamedTuple

from sqlalchemy import and_, func, literal, select, text, tuple_

from src.api.lead_actionability import actionable_condition
from src.api.results_category import (
    ResultsCategory,
    category_condition,
    skip_trace_eligible_condition,
)
from src.api.tax_filters import tax_cap_condition
from src.config import settings
from src.db.models import Result
from src.scrapers import king_cv_sources
from src.scrapers.enrichment.pierce_atip_owner import OWNER_SOURCE as PIERCE_OWNER_SOURCE
from src.scrapers.enrichment.skip_trace import build_pending_row_payload
from src.utils.address_intel import street_is_placeholder

# 2: the trial credit cap (audit S3-03/S4-01's lifetime allowance, applied as the claim does).
PLANNER_VERSION = 2
# Most new lookups one quote (and so one action) may carry.
QUOTE_CAP = 2000
# Most not-attempted rows one quote examines: a bound on one request's work, above the
# largest tab in production (16,965 rows, 2026-09-28), so not a normal stopping point.
SCAN_CEILING = 20_000
_CHUNK = 500
# Tracerfy credits per lookup: `CREDITS_PER_ROW` in src/workers/skip_trace_capacity.py,
# restated because importing `src.workers` builds the Celery app. A test pins the two
# equal. An unknown trace type raises there, and KeyErrors here.
CREDITS = {"normal": 1, "advanced": 2}
# The legacy placeholder property_address (src/api/lead_actionability.py).
_ENRICHMENT_UNAVAILABLE = "(enrichment unavailable)"

# Buckets. The first three come from `skip_trace_status` alone; the window only ever
# walks `not_attempted` rows, which land in one of the rest.
IN_PROGRESS = "in_progress"
ALREADY_ANSWERED = "already_answered"
PREVIOUSLY_ATTEMPTED = "previously_attempted"
NO_ADDRESS = "no_address"
PLACEHOLDER = "placeholder"
SETTLED_CODE_VIOLATION = "settled_code_violation"
ATIP = "atip"
NOT_TRACEABLE = "not_traceable"
QUOTABLE = "quotable"

EXCLUDED_BUCKETS = (NO_ADDRESS, PLACEHOLDER, SETTLED_CODE_VIOLATION, ATIP, NOT_TRACEABLE)
# The verdict the 1b-2 confirm writes for each exclusion: migration 101 lets a
# user-scoped session create exactly these (plus `quoted`).
EXCLUSION_DISPOSITIONS = {
    NO_ADDRESS: "excluded_no_address",
    PLACEHOLDER: "excluded_placeholder_address",
    SETTLED_CODE_VIOLATION: "excluded_settled_code_violation",
    ATIP: "excluded_atip_policy",
    NOT_TRACEABLE: "excluded_not_traceable",
}

_IN_PROGRESS_STATUSES = frozenset({"queued", "submitted"})
_ANSWERED_STATUSES = frozenset({"hit", "miss"})
_QUOTABLE_STATUS = "not_attempted"

# The columns `classify` (and `build_pending_row_payload` inside it) read, plus the
# keyset. Selected as plain columns: no ORM identity, and the encrypted contact
# columns are never loaded or decrypted.
_ROW_COLUMNS = (
    Result.id, Result.job_id, Result.user_id, Result.created_at,
    Result.skip_trace_status, Result.party_name, Result.parcel_id,
    Result.property_address, Result.property_city, Result.property_state,
    Result.property_zip, Result.mailing_address, Result.enrichment_data,
)


@dataclass(frozen=True)
class PlannerPolicy:
    """The policy inputs a quote was planned under, pinned into it (15-14)."""
    pierce_cv_owner_skip_trace_enabled: bool


def policy_from_settings() -> PlannerPolicy:
    """Read the policy ONCE per request; everything after uses this copy."""
    return PlannerPolicy(bool(settings.PIERCE_CV_OWNER_SKIP_TRACE_ENABLED))


class Verdict(NamedTuple):
    bucket: str
    trace_type: str | None = None  # "normal" | "advanced", for QUOTABLE only


def _atip_blocked(row, policy: PlannerPolicy) -> bool:
    """`code_violation_skip_trace_allowed` (src/scrapers/enrichment/skip_trace.py) with
    the flag taken from the PINNED policy rather than the process settings."""
    ed = row.enrichment_data
    return (isinstance(ed, dict)
            and ed.get("source") == "tacoma_code_violations"
            and ed.get("owner_source") == PIERCE_OWNER_SOURCE
            and not policy.pierce_cv_owner_skip_trace_enabled)


def classify(row, policy: PlannerPolicy) -> Verdict:
    """One lead's verdict. Pure: no I/O and no query (the only settings read is the one
    `build_pending_row_payload` makes, see the module docstring). First match wins."""
    status = row.skip_trace_status
    if status in _IN_PROGRESS_STATUSES:
        return Verdict(IN_PROGRESS)
    if status in _ANSWERED_STATUSES:
        return Verdict(ALREADY_ANSWERED)
    if status != _QUOTABLE_STATUS:
        # `errored` is ambiguous: a pre-submit rejection OR a lookup Tracerfy accepted
        # and CHARGED but we could not match, and `last_trace_outcome` is NULL until
        # 1b-2 (finding 16-3). Never quotable, and nor is any status unknown here.
        return Verdict(PREVIOUSLY_ATTEMPTED)
    street = (row.property_address or "").strip()
    if not street:
        return Verdict(NO_ADDRESS)
    if street == _ENRICHMENT_UNAVAILABLE or street_is_placeholder(row.property_address):
        return Verdict(PLACEHOLDER)
    ed = row.enrichment_data
    if isinstance(ed, dict) and king_cv_sources.is_settled(ed.get("source"), ed.get("status")):
        return Verdict(SETTLED_CODE_VIOLATION)
    if _atip_blocked(row, policy):
        return Verdict(ATIP)
    payload = build_pending_row_payload(row)
    if payload is None:
        return Verdict(NOT_TRACEABLE)
    return Verdict(QUOTABLE, payload["trace_type"])


@dataclass
class Window:
    """What one quote examined, in `(created_at, id)` order."""
    quoted_ids: list[str] = field(default_factory=list)
    advanced_count: int = 0
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(EXCLUDED_BUCKETS, 0))
    examined: int = 0
    # The last (created_at, id) examined: where a follow-up count of what is left, and
    # the 1b-2 confirm's re-classification of the window, stop.
    window_end: tuple[datetime, str] | None = None
    # "cap" | "credit_cap" | "scan_limit" | None (the rows ran out)
    stopped: str | None = None
    # A trial account's LIFETIME allowance, or None (no credit budget).
    credit_cap: int | None = None
    quoted_credits: int = 0
    # Quotable leads left out because they did not fit the credit budget.
    over_credit_cap: int = 0

    def add(self, row, policy: PlannerPolicy, *, cap: int, ceiling: int) -> bool:
        """Classify one row; False once the window is full.

        `self.credit_cap` is a trial account's LIFETIME allowance (`paid_lookup_access`
        == trial). It is applied exactly as `claim_skip_trace_rows` applies the room,
        in the order given: a lead whose cost does not fit is skipped and a later,
        cheaper one may still fit. The claim's room is the allowance MINUS what the
        trial already used, so capping by the whole allowance stays an upper bound.
        """
        verdict = classify(row, policy)
        self.examined += 1
        self.window_end = (row.created_at, str(row.id))
        if verdict.bucket == QUOTABLE:
            cost = CREDITS[verdict.trace_type]
            if self.credit_cap is not None and self.quoted_credits + cost > self.credit_cap:
                self.over_credit_cap += 1
            else:
                self.quoted_ids.append(str(row.id))
                self.quoted_credits += cost
                if verdict.trace_type == "advanced":
                    self.advanced_count += 1
        else:
            self.counts[verdict.bucket] = self.counts.get(verdict.bucket, 0) + 1
        if len(self.quoted_ids) >= cap:
            self.stopped = "cap"
        elif (self.credit_cap is not None
              and self.credit_cap - self.quoted_credits < min(CREDITS.values())):
            self.stopped = "credit_cap"  # nothing, not even the cheapest lookup, fits
        elif self.examined >= ceiling:
            self.stopped = "scan_limit"
        return self.stopped is None


def plan_window(
    rows: Iterable, policy: PlannerPolicy, *, cap: int = QUOTE_CAP,
    ceiling: int = SCAN_CEILING, credit_cap: int | None = None,
) -> Window:
    """Plan over rows already in `(created_at, id)` order."""
    window = Window(credit_cap=credit_cap)
    for row in rows:
        if not window.add(row, policy, cap=cap, ceiling=ceiling):
            break
    return window


# ── Database reads (async: the API's request session) ───────────────────────


def tab_condition(job_id: str, user_id: str, category: ResultsCategory, today: date):
    """The rows a Results TAB lists, exactly as `GET /jobs/{id}/results` builds them
    without its optional view filters (the quote covers the whole tab), plus the
    skip-trace eligibility the enqueue applies (implied by both categories today)."""
    return and_(
        Result.job_id == job_id,
        Result.user_id == user_id,
        category_condition(category),
        tax_cap_condition(today),
        actionable_condition(),
        skip_trace_eligible_condition(),
    )


async def _bound_statements(db) -> None:
    # Transaction-scoped: bounds every read below, released at the request's end.
    await db.execute(text("SET LOCAL statement_timeout = '5s'"))


async def tab_status_counts(db, job_id: str, user_id: str, category: ResultsCategory,
                            today: date) -> dict[str, int]:
    """Rows on the tab per `skip_trace_status`, in ONE statement (exact strings only:
    every address decision stays in `classify`)."""
    await _bound_statements(db)
    rows = await db.execute(
        select(Result.skip_trace_status, func.count())
        .where(tab_condition(job_id, user_id, category, today))
        .group_by(Result.skip_trace_status)
    )
    return {status: int(n) for status, n in rows.all()}


def status_buckets(status_counts: dict[str, int]) -> dict[str, int]:
    """The tab-wide status buckets of a `tab_status_counts` result."""
    out = {IN_PROGRESS: 0, ALREADY_ANSWERED: 0, PREVIOUSLY_ATTEMPTED: 0}
    for status, n in status_counts.items():
        if status in _IN_PROGRESS_STATUSES:
            out[IN_PROGRESS] += n
        elif status in _ANSWERED_STATUSES:
            out[ALREADY_ANSWERED] += n
        elif status != _QUOTABLE_STATUS:
            out[PREVIOUSLY_ATTEMPTED] += n
    return out


def _not_attempted(job_id: str, user_id: str, category: ResultsCategory, today: date):
    return and_(tab_condition(job_id, user_id, category, today),
                Result.skip_trace_status == _QUOTABLE_STATUS)


def _after(key: tuple[datetime, str]):
    # Each value bound with its column's type: a bare tuple binds the id as VARCHAR,
    # and Postgres has no `uuid > varchar`.
    return tuple_(Result.created_at, Result.id) > tuple_(
        literal(key[0], Result.created_at.type), literal(key[1], Result.id.type),
    )


async def plan_tab_window(
    db, job_id: str, user_id: str, category: ResultsCategory, today: date,
    policy: PlannerPolicy, *, cap: int = QUOTE_CAP, ceiling: int = SCAN_CEILING,
    chunk: int = _CHUNK, credit_cap: int | None = None,
) -> Window:
    """Walk the tab's `not_attempted` rows in `(created_at, id)` keyset chunks until
    the cap (or a trial's `credit_cap`) fills, the ceiling is reached, or the rows run
    out. Rows an earlier action bought are no longer `not_attempted`, so the window
    always moves forward."""
    await _bound_statements(db)
    window = Window(credit_cap=credit_cap)
    after: tuple[datetime, str] | None = None
    while True:
        stmt = select(*_ROW_COLUMNS).where(_not_attempted(job_id, user_id, category, today))
        if after is not None:
            stmt = stmt.where(_after(after))
        rows = (await db.execute(
            stmt.order_by(Result.created_at, Result.id).limit(chunk)
        )).all()
        for row in rows:
            if not window.add(row, policy, cap=cap, ceiling=ceiling):
                return window
        if len(rows) < chunk:
            return window
        after = window.window_end


async def count_remaining(db, job_id: str, user_id: str, category: ResultsCategory,
                          today: date, window: Window) -> int:
    """`not_attempted` tab rows past the window: what a follow-up quote would cover.
    Its own COUNT, never a subtraction, so it cannot go negative (consult r3, R3)."""
    if window.stopped is None or window.window_end is None:
        return 0
    await _bound_statements(db)
    return int((await db.execute(
        select(func.count()).select_from(Result).where(
            _not_attempted(job_id, user_id, category, today), _after(window.window_end),
        )
    )).scalar_one())


__all__ = [
    "ALREADY_ANSWERED", "ATIP", "CREDITS", "EXCLUDED_BUCKETS", "EXCLUSION_DISPOSITIONS",
    "IN_PROGRESS", "NOT_TRACEABLE", "NO_ADDRESS", "PLACEHOLDER", "PLANNER_VERSION",
    "PREVIOUSLY_ATTEMPTED", "QUOTABLE", "QUOTE_CAP", "SCAN_CEILING",
    "SETTLED_CODE_VIOLATION", "PlannerPolicy", "Verdict", "Window", "classify",
    "count_remaining", "plan_tab_window", "plan_window", "policy_from_settings",
    "status_buckets", "tab_condition", "tab_status_counts",
]
