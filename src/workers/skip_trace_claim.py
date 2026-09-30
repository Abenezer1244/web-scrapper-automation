"""The single way a lead is claimed into the skip-trace queue.

Two callers use this: the scrape enqueue (`_enqueue_skip_trace_rows`) and, from
Phase 1b-2, the "look up contacts" action worker. They MUST share it, or the two
paths drift and the same lead is claimed -- and charged for -- twice.

Why this exists (Codex round 15, finding 15-1). Migration 100 adds a PARTIAL
UNIQUE INDEX on ``pending_skip_trace_rows (result_id)`` WHERE status IN
('queued','submitting','submitted'). Before this module the scrape enqueue built
rows with ``db.add()`` in a loop and flushed them all at ONE ``db.commit()``
whose handler was ``except Exception: db.rollback(); db.commit()``. With the
index in place, one conflicting row anywhere in a job's batch would roll back THE
WHOLE JOB'S enqueue and then commit an empty transaction: silent, total,
unreported loss. So the claim is ONE set-based statement that reports exactly
which leads it won, and `results` is advanced for those and only those.

THE CLAIM IS ITS OWN ELIGIBILITY CHECK (round 15 diff review). The insert does
not trust its payloads. It selects through a join to `results`, so a lead only
gets a pending row when that row still EXISTS, still belongs to the claiming
tenant, and is still 'not_attempted'. Without that join a payload naming a
deleted result kills the whole batch with a foreign-key error; a payload naming
another tenant's result inserts a pending row its real owner could then never
claim (the unique index blocks them) while the dispatcher's tenant-pinned joins
ignore it forever; and a lead that settled to 'hit' between the eligibility read
and the claim gets an active queued row that nothing will ever settle.

THE CONFLICT CLAUSE NAMES ITS ARBITER, AND THE CLAIM FAILS CLOSED. ``ON CONFLICT
(result_id) WHERE status IN (...)`` makes Postgres resolve the partial index at
planning time and raise if it is missing, so enforcement does not depend on this
module's idea of how a predicate renders. `claim_enforcement_ok` checks the same
thing first only to produce a readable error.

An earlier version let the scrape proceed unenforced, reasoning that refusing
would strand every lookup in the product because `start.sh` starts the worker
even when migrations fail. The Security Master Review rejected that, and the
premise was wrong: an unclaimed lead stays 'not_attempted' and is claimed by the
next run once the migration lands, while scrapes still run and leads are still
delivered. Refusing is a PAUSE; charging a customer twice is not undoable.

LOCK ORDER. Pending rows first, then `results` -- the same order
``_cancel_undeliverable_queued`` uses (it UPDATEs pending rows, then UPDATEs
`results`). An earlier version of this helper took ``SELECT ... FOR UPDATE`` on
`results` FIRST, which bought nothing (the index plus ON CONFLICT already
arbitrates uniqueness) and inverted the order against the sweep, so a sweep
holding pending rows and waiting on results could deadlock against a claim
holding results and waiting on the index. Any future writer of this pair must
take pending before results.

THE CALLER OWNS THE TRANSACTION. Nothing here commits. The action worker needs
the claim, the action-result dispositions and the audit event in ONE transaction,
which is impossible if the helper commits underneath it.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import text

from src.utils.logger import setup_logger

_logger = setup_logger(__name__)

# The pending statuses that mean "this lead is already being looked up". Must
# stay identical to the predicate of the partial unique index in migration 100;
# a mismatch either lets a second claim through (double charge) or blocks a
# legitimate one forever. test_the_active_predicate_matches_the_index pins it.
ACTIVE_PENDING_STATUSES: tuple[str, ...] = ("queued", "submitting", "submitted")

INDEX_NAME = "uq_pending_skip_trace_active_result"

# The same list as a SQL literal, DERIVED rather than written out twice, so the
# ON CONFLICT arbiter predicate and ACTIVE_PENDING_STATUSES cannot drift apart.
_ACTIVE_SQL = ", ".join(f"'{s}'" for s in ACTIVE_PENDING_STATUSES)

# Only a lead that has never been looked up may be claimed. The scrape enqueue
# already filters on this; repeating it INSIDE the insert is what makes the
# decision atomic rather than advisory.
CLAIMABLE_RESULT_STATUS = "not_attempted"

# ── WHO MAY BUY A LOOKUP (audit #3 S3-03, audit #4 S4-01) ─────────────────────
#
# Every lookup is a Tracerfy charge to us. It is recovered only from a live paid
# subscription, so the account decides, not just the plan name: a free trial is
# `plan='pro'` with no subscription, and the old gate ("not Starter") let it buy
# as many lookups as a paying Pro account (owner decision 2026-09-27: trials get
# a small lifetime allowance). The rule is decided HERE because this module is
# the only writer of the queue; the dispatcher re-applies the blocking half to
# rows queued before an account froze or ended.
ACCESS_FULL = "full"
ACCESS_TRIAL = "trial"
# Blocked: nothing is queued, nothing already queued is sent.
ACCESS_STARTER = "starter"
ACCESS_FROZEN = "frozen"
ACCESS_ENDED = "ended"
BLOCKED_ACCESS = frozenset({ACCESS_STARTER, ACCESS_FROZEN, ACCESS_ENDED})


def held_lookup_message(access: str, held: int, trial_allowance: int) -> str | None:
    """The customer's job-log line for leads the claim HELD (audit #4 4b-ii), or None.

    Says why, and nothing more. It must not promise a later lookup: nothing
    re-queues a held lead, because enqueue is job-scoped, so a lead held here is
    looked up only if a later run of a scraper delivers it again.
    """
    if held <= 0 or access not in (ACCESS_TRIAL, *BLOCKED_ACCESS):
        return None
    head = f"Contact lookups were not run for {held} lead(s): "
    if access == ACCESS_TRIAL:
        if trial_allowance <= 0:
            return head + ("your free trial does not include contact lookups. "
                           "Paid plans include contact lookups for new leads.")
        return head + (f"your free trial includes up to {trial_allowance} lookup credits, "
                       "and not enough remain for these leads. Paid plans include "
                       "contact lookups for new leads.")
    if access == ACCESS_FROZEN:
        return head + "your account is frozen because a payment did not go through."
    if access == ACCESS_ENDED:
        return head + "your paid plan has ended."
    return head + "your plan does not include contact lookups."

# The users columns the rule reads. One list, so the claim and the dispatcher
# cannot read different facts.
ACCESS_COLUMNS = (
    "id", "plan", "is_admin", "subscription_status", "trial_ends_at",
    "entitlement_ends_at", "entitlement_grace_ends_at",
)


def paid_lookup_access(user, now=None) -> str:
    """Which lookups this account may buy. First match wins:

    | account                                                  | access  |
    |----------------------------------------------------------|---------|
    | Starter plan                                             | starter |
    | frozen for non-payment (`is_frozen`)                     | frozen  |
    | paid term already ended (`entitlement_ends_at <= now`)   | ended   |
    | admin                                                    | full    |
    | paid term ends LATER (`entitlement_ends_at > now`)       | full    |
    | subscription `active`                                    | full    |
    | `past_due` inside its grace (frozen once it expires)     | full    |
    | no subscription status AND no trial date                 | full    |
    | anything else (app trial, Stripe `trialing`, `canceled`, |         |
    | `incomplete`, `past_due` with no grace, unknown)         | trial   |

    "No status and no trial date" is an operator-granted plan: registration
    always stamps `trial_ends_at`, and only a Stripe active/trialing
    subscription clears it, which also records the status. Every state that
    cannot show a live payment falls to the trial allowance, never to full.
    """
    from datetime import UTC, datetime

    from src.api.quota_window import as_utc, is_frozen
    from src.config.constants import normalize_plan

    now = as_utc(now or datetime.now(UTC))
    if normalize_plan(getattr(user, "plan", None)) == "starter":
        return ACCESS_STARTER
    if is_frozen(user, now):
        return ACCESS_FROZEN
    ends_at = getattr(user, "entitlement_ends_at", None)
    if ends_at is not None and now >= as_utc(ends_at):
        return ACCESS_ENDED
    if getattr(user, "is_admin", False):
        return ACCESS_FULL
    if ends_at is not None:
        # Paid access scheduled to stop LATER (a cancellation at period end):
        # billing sets this only for a customer who paid for the term, whatever
        # the status reads meanwhile ('canceled' included). Codex 4a review.
        return ACCESS_FULL
    status = (getattr(user, "subscription_status", None) or "").strip()
    if status == "active":
        return ACCESS_FULL
    if status == "past_due" and getattr(user, "entitlement_grace_ends_at", None) is not None:
        return ACCESS_FULL
    if not status and getattr(user, "trial_ends_at", None) is None:
        return ACCESS_FULL
    return ACCESS_TRIAL


def read_access_rows(db, user_ids, *, lock: str) -> dict:
    """{user_id: row} of the ACCESS_COLUMNS, row-locked. `lock` is the row-lock
    clause: the claim takes FOR NO KEY UPDATE (it serializes two claimers of one
    trial and waits for a billing write in flight, while still letting other
    transactions insert rows that reference the user); the quota reservation
    takes FOR UPDATE (the lock its grant statement takes anyway); the dispatcher
    takes FOR SHARE SKIP LOCKED (it never waits: an account whose row is being
    written right now is missing from the result, which callers read as
    blocked, and its rows go out on a later tick). An empty `lock` is a plain
    read, for a filter that a locked read re-checks later."""
    if lock not in ("", "FOR NO KEY UPDATE", "FOR UPDATE", "FOR SHARE SKIP LOCKED"):
        raise ValueError(f"unsupported lock clause {lock!r}")
    ids = sorted({str(u) for u in user_ids})
    if not ids:
        return {}
    rows = db.execute(
        text(
            f"SELECT {', '.join(ACCESS_COLUMNS)} FROM public.users "  # noqa: S608 - fixed literals
            f"WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY id {lock}"
        ),
        {"ids": ids},
    ).all()
    return {str(r.id): r for r in rows}


def lifetime_credits_queued(db, user_id: str) -> int:
    """Credits of every queue row this account has EVER had, whatever became of
    it: cancelled, errored and released rows count too. Rows are removed only by
    the claim withdrawing its own uncommitted insert and by operator cleanup
    scripts, so no customer action can hand an allowance back."""
    from src.workers.skip_trace_capacity import credits_for

    counts = db.execute(
        text(
            "SELECT trace_type, count(*) FROM public.pending_skip_trace_rows "
            "WHERE user_id = CAST(:uid AS uuid) GROUP BY trace_type"
        ),
        {"uid": str(user_id)},
    ).all()
    return sum(credits_for(t) * int(n) for t, n in counts)

# The columns build_pending_row_payload produces, in insert order. Listed
# explicitly rather than derived from the payload dict so a stray key can never
# widen the INSERT.
_COLUMNS: tuple[str, ...] = (
    "job_id", "result_id", "user_id",
    "property_address", "city", "state", "zip",
    "first_name", "last_name",
    "mail_address", "mail_city", "mail_state", "mail_zip",
    "trace_type",
)

_UUID_COLUMNS = frozenset({"job_id", "result_id", "user_id"})

# Column widths from src/db/models.py::PendingSkipTraceRow, for the fields the
# Phase 1a subject key hashes at the SAME width (_SUBJECT_ADDRESS_MAX = 512,
# _SUBJECT_FIELD_MAX = 128). Truncating here, in the one place that writes the
# row, is what keeps read and write hashing identical bytes: a 128-truncated
# write key would never match the untruncated read key and every repeat trace
# would be re-paid. A code-violation description of 250+ chars would otherwise
# raise StringDataRightTruncation and poison the session.
#
# `state` and `mail_state` are deliberately ABSENT. Their column is String(2)
# but lookup_subject_key hashes state at _SUBJECT_FIELD_MAX = 128, so silently
# cutting a longer value down to two characters would STORE one value and HASH
# another -- the exact divergence this table exists to prevent. A state that does
# not fit is a bug upstream in build_pending_row_payload, so the payload is
# REFUSED and reported rather than quietly mangled.
_WIDTHS: dict[str, int] = {
    "property_address": 512, "mail_address": 512,
    "city": 128, "zip": 16,
    "first_name": 128, "last_name": 128,
    "mail_city": 128, "mail_zip": 16,
}

_EXACT: dict[str, int] = {"state": 2, "mail_state": 2}

# `trace_type` is VALIDATED, never truncated. Truncating it to its 16-character
# column could turn a bad value into a row the dispatcher never drains: it
# batches strictly by 'normal' / 'advanced', so anything else sits queued
# forever, counted as in progress and never submitted or settled.
_TRACE_TYPES = frozenset({"normal", "advanced"})

# Rows per INSERT statement. Postgres caps a statement at 65535 bind parameters
# and each row costs len(_COLUMNS) + 1 (currently 15), so the hard ceiling is
# about 4,368 rows. 1,000 leaves room for the ceiling to survive a column being
# added to _COLUMNS without anyone remembering this, and keeps each statement's
# SQL text small. It bounds the STATEMENT only: every chunk runs inside the
# caller's one transaction. test_a_batch_larger_than_the_parameter_limit_claims
# pins it above the ceiling.
_INSERT_CHUNK_ROWS = 1000

# How long a second claimer of the SAME job waits before giving up. Long enough
# that an ordinary enqueue never trips it, short enough that nobody waits on a
# stuck worker: the loser raises, its caller logs and moves on, and the leads it
# did not claim stay 'not_attempted' for the next run.
_LOCK_WAIT_SECONDS = 30


# How Postgres renders migration 100's WHERE clause back from the catalog. Kept
# beside ACTIVE_PENDING_STATUSES so the two cannot drift silently;
# test_the_active_predicate_matches_the_index asserts the live index against it.
_EXPECTED_PREDICATE = (
    "((status)::text = ANY ((ARRAY["
    + ", ".join(f"'{s}'::character varying" for s in ACTIVE_PENDING_STATUSES)
    + "])::text[]))"
)


def _normalize_sql(expression: str | None) -> str:
    """Whitespace-insensitive form, so formatting is not mistaken for meaning."""
    return " ".join((expression or "").split())


class ClaimLockNotHeldError(RuntimeError):
    """The caller did not take `lock_job_for_claim` first.

    A programming error, not a runtime condition: the lock is what serializes
    the cache-hit write against another claimer, and no SQL in the claim itself
    can substitute for it (the cache-hit write is an ORM write on encrypted
    columns, and it cannot see another writer's uncommitted pending row).
    """


class ClaimUnenforcedError(RuntimeError):
    """Migration 100's index is absent, so a second active claim is possible.

    Raised for EVERY caller: the database is what stops a lead being charged for
    twice, and no amount of logging substitutes for it. See
    `claim_skip_trace_rows` for why refusing is a pause rather than a loss.
    """


def claim_enforcement_ok(db) -> bool:
    """True when migration 100's index is present, valid, and the RIGHT index.

    Checked by identity, not by name: `CREATE INDEX IF NOT EXISTS` would happily
    accept a same-named index on another table or with a wider predicate, and
    either one enforces something other than "one active claim per lead" while
    looking applied. So this asserts the table, uniqueness, validity, the indexed
    column, and that the predicate names exactly ACTIVE_PENDING_STATUSES and
    nothing else.
    """
    row = db.execute(
        text(
            "SELECT i.indisunique, i.indisvalid, i.indnatts, i.indnkeyatts, "
            "       i.indexprs IS NULL AS plain_columns, "
            "       a.attname, "
            "       pg_get_expr(i.indpred, i.indrelid) AS predicate "
            "FROM pg_class c "
            "JOIN pg_namespace cn ON cn.oid = c.relnamespace "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
            "LEFT JOIN pg_attribute a "
            "       ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
            # Schema-qualified on BOTH sides: a same-named index on another
            # schema's pending_skip_trace_rows would otherwise satisfy this
            # while the table the application actually writes stays unenforced.
            "WHERE c.relname = :n AND cn.nspname = 'public' "
            "  AND t.relname = 'pending_skip_trace_rows' AND tn.nspname = 'public'"
        ),
        {"n": INDEX_NAME},
    ).first()
    if row is None or not row.indisunique or not row.indisvalid:
        return False
    # Structural, not textual. Matching "(result_id)" inside pg_get_indexdef
    # would also accept an EXPRESSION index such as (lower(result_id::text)),
    # which cannot serve the named ON CONFLICT arbiter at all: every claim would
    # then fail closed while the index looked correct. Exactly one key column,
    # no expressions, and that column is result_id.
    # indnkeyatts is the KEY column count; indnatts also counts INCLUDE payload
    # columns. ON CONFLICT infers on the key alone, so a unique partial index
    # with INCLUDE columns still arbitrates correctly and must not be rejected --
    # rejecting it would fail every claim closed, an outage, over a difference
    # that does not affect the guarantee.
    if (row.indnkeyatts != 1 or row.indnatts < row.indnkeyatts
            or not row.plain_columns or row.attname != "result_id"):
        return False
    # The predicate is compared EXACTLY, not by counting casts. Counting was
    # bypassable: a predicate of
    #   status IN ('queued','submitting','submitted')
    #     AND result_id <> '000...0'::uuid
    # has the right table, uniqueness, validity, one column and exactly three
    # ::character varying occurrences, yet permits a duplicate active row for
    # that one lead. Any conjunct at all changes the rendered expression, so an
    # exact match is the only check that cannot be widened past.
    #
    # Rendering is a Postgres implementation detail, so a mismatch is treated as
    # NOT enforced. That errs toward the action refusing to claim, which costs
    # availability and never money.
    return _normalize_sql(row.predicate) == _normalize_sql(_EXPECTED_PREDICATE)


def warn_if_unenforced(db) -> bool:
    """`claim_enforcement_ok`, but says so out loud when the answer is no."""
    enforced = claim_enforcement_ok(db)
    if not enforced:
        _logger.error(
            "Skip-trace claim is running UNENFORCED: %s is missing, invalid or not "
            "the expected index, so nothing stops a second active claim for one "
            "lead and a lead can be charged for twice. Apply migration 100.",
            INDEX_NAME,
        )
    return enforced


def _truncate(column: str, value: Any) -> Any:
    width = _WIDTHS.get(column)
    if width is None or value is None:
        return value
    text_value = str(value)
    return text_value[:width] if len(text_value) > width else text_value


def payload_is_writable(payload: dict) -> bool:
    """False for a payload that cannot be stored without changing its meaning.

    Public so a DRY RUN can apply the same rule the real claim would. Counting a
    payload the claim would refuse makes a cost estimate promise spend that
    never happens.
    """
    for column, width in _EXACT.items():
        value = payload.get(column)
        if value is not None and len(str(value)) > width:
            return False
    return payload.get("trace_type") in _TRACE_TYPES


def lock_job_for_claim(db, job_id: str) -> None:
    """Serialize every claimer of ONE job's leads. Transaction-scoped.

    MANDATORY for any caller that decides a lead is eligible and then acts on
    that decision -- which includes the cache-hit path, not just the claim. The
    claim's own SQL makes the INSERT atomic, but copying a cached answer onto a
    Result is an ORM write (phone/email are EncryptedString, so they cannot be
    written as raw SQL without storing plaintext PII), and nothing in that write
    can see another writer's uncommitted pending row. Two claimers of one job
    must therefore not overlap at all.

    Today's callers: the scrape enqueue and the backfill script. **Phase 1b-2's
    contact-lookup action worker MUST call this too**, before its own
    cache-and-claim pass, or it can queue a lead between the scrape's cache-hit
    read and its commit -- leaving an active queue row while the Result is
    overwritten as settled, which the dispatcher may submit and pay for before
    the cancellation sweep collects it.

    Transaction-scoped, so a commit and a rollback both release it and a crash
    cannot hold it. Keyed on the job, so unrelated jobs never wait on each other.

    Not merely documented: `claim_skip_trace_rows` ASSERTS the lock is held, so
    a caller that forgets it is refused rather than silently racing.
    """
    # BOUNDED, not an indefinite wait. pg_advisory_xact_lock blocks forever by
    # default, so a slow enqueue of a very large job could hold another claimer
    # of the SAME job for as long as the worker's own timeout -- which for the
    # Phase 1b-2 action means a customer's "look up contacts" sitting silent for
    # up to an hour. lock_timeout turns that into a prompt, retryable failure.
    # SET LOCAL, so it reverts with the transaction and never leaks onto the
    # session's other statements.
    previous = db.execute(text("SHOW lock_timeout")).scalar()
    db.execute(text("SELECT set_config('lock_timeout', :v, true)"),
               {"v": f"{_LOCK_WAIT_SECONDS}s"})
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": _lock_key(job_id)},
    )
    # Restored only on SUCCESS, and to the caller's OWN prior value rather than
    # the server default. Deliberately not in a `finally`: a lock timeout aborts
    # the transaction, and any statement issued on the way out would fail too,
    # masking the real error with "current transaction is aborted". The caller
    # sees the timeout and rolls back, which discards this setting anyway.
    db.execute(text("SELECT set_config('lock_timeout', :v, true)"),
               {"v": previous or "0"})


def _lock_key(job_id: str) -> str:
    return f"skip_trace_enqueue:{job_id}"


def job_claim_lock_held(db, job_id: str) -> bool:
    """True when THIS backend already holds `job_id`'s claim lock.

    pg_advisory_xact_lock(bigint) stores the key split across pg_locks.classid
    (high 32 bits) and objid (low 32 bits), with objsubid = 1 for the
    single-argument form. The key is signed, so the shift is arithmetic on a
    possibly negative value and both halves are masked back to their unsigned
    oid representation before comparing. Roughly half of all job ids hash
    negative, so that is the common case, not an edge one; it is exercised in
    both signs by test_the_lock_check_detects_the_lock_for_negative_hashes_too.

    hashtextEXTENDED, not hashtext: the latter is 32 bits, and a collision there
    would let one job's lock satisfy another job's assertion -- which is worse
    than the needless serialization it also causes, because it makes
    ClaimLockNotHeldError pass for a job nothing is actually holding. 64 bits
    makes that negligible rather than merely unlikely.
    """
    return bool(db.execute(
        text(
            "WITH k AS (SELECT hashtextextended(:k, 0) AS key) "
            "SELECT 1 FROM pg_locks l, k "
            "WHERE l.locktype = 'advisory' AND l.pid = pg_backend_pid() "
            "  AND l.granted AND l.objsubid = 1 "
            "  AND l.classid = ((k.key >> 32) & 4294967295)::oid "
            "  AND l.objid = (k.key & 4294967295)::oid"
        ),
        {"k": _lock_key(job_id)},
    ).scalar())


def claim_skip_trace_rows(
    db, payloads: list[dict], *, report: dict | None = None, action_id: str | None = None,
) -> list[str]:
    """Claim `payloads` into the queue. Returns the result ids actually won.

    `payloads` are ``build_pending_row_payload`` dicts. A payload whose lead no
    longer exists, belongs to another tenant, or is no longer 'not_attempted' is
    simply not claimed -- the insert filters it, so it can neither fail the batch
    nor strand an active row nobody will settle.

    FAILS CLOSED, for every caller, when migration 100's index is missing or is
    not the expected index. An earlier version let the scrape proceed anyway, on
    the grounds that refusing would "strand every lookup in the product". The
    Security Analyst was right to reject that, and the premise was wrong: a lead
    that is not claimed stays 'not_attempted' and is picked up by the next run
    once the migration lands. Refusing is a PAUSE, not a loss -- scrapes still
    run and leads are still delivered; only the paid add-on waits. Against that,
    proceeding unenforced risks charging a customer twice for one lead, which is
    not recoverable by trying again later.

    CLAIMS ONLY WHAT THE ACCOUNT MAY BUY (`paid_lookup_access`): nothing for a
    Starter, frozen or ended account, and for a trial only what fits in the rest
    of its lifetime allowance. A held lead stays 'not_attempted'. Nothing
    re-queues it: enqueue is job-scoped, so it is looked up only if a later run
    delivers it again. `report`, when given, receives ``access`` and ``held``
    (leads held by this rule, not by the insert); the caller tells the customer
    with `held_lookup_message`. It also receives ``held_ids``, those leads' result
    ids in the caller's order, so the contact-lookup action worker can record a
    verdict per held lead (Phase 1b-2, W5).

    `action_id` (Phase 1b-2) is the contact-lookup action buying these leads. Every
    inserted row carries it (the scrape path passes None and its SQL is unchanged),
    and the insert JOINs that action on its id, the claim's tenant AND the claim's
    job: an action of another job or tenant claims NOTHING, so one job's spend can
    never be attributed to another's action (W6). Migration 107's composite FK is the
    database's own backstop for the tenant half.

    Does NOT commit. The caller owns the transaction.
    """
    if not payloads:
        return []

    if not claim_enforcement_ok(db):
        raise ClaimUnenforcedError(
            f"{INDEX_NAME} is missing, invalid or not the expected index; refusing "
            "to claim, because nothing would stop this lead being charged for "
            "twice. Leads stay 'not_attempted' and are claimed by the next run "
            "once migration 100 is applied."
        )

    # One claim is one tenant's work: the single tenant-scoped UPDATE that
    # advances `results` would otherwise be too broad or silently partial.
    user_ids = {str(p["user_id"]) for p in payloads}
    if len(user_ids) != 1:
        raise ValueError(
            f"claim_skip_trace_rows: expected one user_id, got {len(user_ids)}"
        )
    user_id = next(iter(user_ids))

    # ONE job per claim, and its lock must already be held. Both are asserted
    # rather than documented (Security Master Review pass 2): a caller that
    # forgets `lock_job_for_claim` reintroduces the race where a cache-hit write
    # settles a Result while another claimer holds an uncommitted paid queue row
    # for it. A mixed-job batch would make a single job lock meaningless.
    job_ids = {str(p["job_id"]) for p in payloads}
    if len(job_ids) != 1:
        raise ValueError(
            f"claim_skip_trace_rows: expected one job_id, got {len(job_ids)}"
        )
    job_id = next(iter(job_ids))
    if not job_claim_lock_held(db, job_id):
        raise ClaimLockNotHeldError(
            f"lock_job_for_claim(db, {job_id!r}) must be called before claiming: "
            "without it a cache-hit write can settle a lead another claimer has "
            "already queued and paid for."
        )

    # Refuse rather than mangle: a value whose column cannot hold it (_EXACT) or
    # a trace_type the dispatcher would never drain.
    usable, refused = [], []
    for payload in payloads:
        (usable if payload_is_writable(payload) else refused).append(payload)
    if refused:
        # Result ids only: never the homeowner's name or address in a log line.
        _logger.warning(
            "Skip-trace claim refused %d payload(s) with an unusable state or "
            "trace_type; result ids: %s",
            len(refused), [str(p.get("result_id")) for p in refused][:20],
        )
    if not usable:
        return []

    # Deduplicate within the batch so the statement is deterministic about WHICH
    # payload wins for a repeated lead. ON CONFLICT DO NOTHING would also drop
    # the loser, but then which row survives depends on evaluation order. A job
    # listing one lead twice is not hypothetical: the trustee_sale collapse
    # produces sibling rows for one property.
    by_result: dict[str, dict] = {}
    for payload in usable:
        by_result.setdefault(str(payload["result_id"]), payload)

    # THE ACCOUNT MAY BUY THIS (S3-03, S4-01). Read under a row lock on the
    # user, held to the caller's commit: two jobs of one trial cannot both see
    # the same room, and a freeze or cancellation being written right now is
    # waited for rather than raced. Taken AFTER the job lock (the order every
    # caller already follows), and nothing that locks the user first takes a
    # job claim lock.
    account = read_access_rows(db, [user_id], lock="FOR NO KEY UPDATE").get(user_id)
    access = paid_lookup_access(account) if account is not None else ACCESS_ENDED
    held_ids: list[str] = []
    if access in BLOCKED_ACCESS:
        held_ids = list(by_result)
        by_result = {}
    elif access == ACCESS_TRIAL:
        from src.config import settings
        from src.workers.skip_trace_capacity import credits_for

        room = settings.SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE - lifetime_credits_queued(db, user_id)
        kept: dict[str, dict] = {}
        # The caller's order, not result-id order, decides which leads fit.
        for rid, payload in by_result.items():
            cost = credits_for(payload["trace_type"])
            if cost <= room:
                kept[rid] = payload
                room -= cost
            else:
                held_ids.append(rid)
        by_result = kept
    held = len(held_ids)
    if report is not None:
        report["access"] = access
        report["held"] = held
        report["held_ids"] = held_ids
    if held:
        # Result ids only, never the homeowner's name or address.
        _logger.info(
            "Skip-trace claim held %d lead(s) for user %s: access=%s", held, user_id, access,
        )
    if not by_result:
        return []
    ordered = [by_result[k] for k in sorted(by_result)]

    columns = ["id", *_COLUMNS]
    select_list = ", ".join(f"v.{c}" for c in columns)

    # THE ARBITER IS THE ENFORCEMENT (Codex round 15 diff review, round 4).
    #
    # When enforcement is required, name the arbiter: Postgres then resolves it
    # against a real index at planning time and raises "no unique or exclusion
    # constraint matching the ON CONFLICT specification" if 100 is missing or
    # does not match. That is strictly stronger than asking the catalog first,
    # because it removes the window between checking and inserting, and because
    # the database -- not this module's idea of how a predicate renders -- is
    # what decides whether the index really arbitrates.
    #
    # Named for every caller, since every caller now fails closed.
    conflict_sql = (
        f"ON CONFLICT (result_id) WHERE status IN ({_ACTIVE_SQL}) DO NOTHING"
    )

    # Inserted ALREADY 'queued' -- an ACTIVE status, so the partial unique index
    # applies at insert time. Inserting inactive first and activating later would
    # sit outside the index and let two rows collide afterwards, when no
    # statement is left that could refuse one.
    #
    # noqa: S608 - nothing interpolated is input. `columns`/`select_list` come
    # from the _COLUMNS literals, and `rows_sql` holds only generated ":name"
    # bind slots and their CAST types. EVERY payload value travels in `params`
    # as a bound parameter.
    # The action, when there is one, is a constant bound parameter (never a VALUES
    # column): one claim is one action. Its JOIN is the action<->job<->tenant proof.
    # With action_id=None these three fragments are empty and the statement is
    # byte-for-byte the scrape path's.
    action_col = ", action_id" if action_id is not None else ""
    action_val = ", CAST(:action_id AS uuid)" if action_id is not None else ""
    action_join = (
        "JOIN public.contact_lookup_actions a ON a.id = CAST(:action_id AS uuid) "
        "                                    AND a.user_id = v.user_id "
        "                                    AND a.job_id = v.job_id "
        if action_id is not None else ""
    )
    insert_sql = (
        f"INSERT INTO pending_skip_trace_rows ({', '.join(columns)}{action_col}, status) "  # noqa: S608
        f"SELECT {select_list}{action_val}, 'queued' "
        f"FROM (VALUES {{rows}}) "
        f"     AS v({', '.join(columns)}) "
        # r.job_id = v.job_id as well as the tenant: the payload carries a
        # job_id that is written onto the queue row, and a malformed one
        # would produce a row tied to a job the lead does not belong to.
        # The dispatcher's tenant-pinned joins would then ignore that row
        # forever while the unique index blocked the legitimate claim --
        # a lead that can never be looked up again.
        f"JOIN public.results r ON r.id = v.result_id AND r.user_id = v.user_id "
        f"                       AND r.job_id = v.job_id "
        # The JOB's owner too, not just the result's: a row tied to another
        # tenant's job would be ignored by the dispatcher's tenant-pinned joins
        # forever while the unique index blocked the legitimate claim.
        f"JOIN public.jobs j ON j.id = v.job_id AND j.user_id = v.user_id "
        f"{action_join}"
        f"WHERE r.user_id = CAST(:uid AS uuid) AND j.user_id = CAST(:uid AS uuid) "
        f"  AND r.skip_trace_status = :claimable "
        f"{conflict_sql} "
        f"RETURNING result_id"
    )

    # CHUNKED, because Postgres caps a statement at 65535 bind parameters and
    # each row here costs len(_COLUMNS) + 1. One VALUES list for a whole job
    # therefore breaks at ~4,368 leads -- and production holds 100,548 claimable
    # leads, so a job past that ceiling is ordinary, not hypothetical. The whole
    # enqueue used to fail on such a job, which is the opposite of the "a
    # conflict costs only itself" property this module exists for.
    #
    # Chunking the STATEMENT is not chunking the TRANSACTION. Every chunk runs
    # in the caller's single transaction and commits with it, so "one commit, no
    # crash window" still holds: a failure part-way rolls back the lot and
    # nothing is half-claimed.
    inserted_ids: list[str] = []
    # result_id -> the pending-row primary key WE generated for it. Kept across
    # chunks because the withdrawal below deletes by our own ids, and `params`
    # is rebuilt per chunk: indexing it by a position in `ordered` would read
    # the wrong chunk's id, or KeyError past the last chunk's length, and a
    # wrong delete would remove a legitimately claimed row.
    pending_id_by_result: dict[str, str] = {}
    for start in range(0, len(ordered), _INSERT_CHUNK_ROWS):
        chunk = ordered[start:start + _INSERT_CHUNK_ROWS]
        rows_sql: list[str] = []
        params: dict[str, Any] = {
            "uid": user_id, "claimable": CLAIMABLE_RESULT_STATUS,
        }
        if action_id is not None:
            params["action_id"] = str(action_id)
        for i, payload in enumerate(chunk):
            # `id` is supplied explicitly: PendingSkipTraceRow.id carries a
            # PYTHON-side default (`default=_uuid`), not a server default, so a
            # raw INSERT that bypasses the ORM would write NULL and violate the
            # primary key.
            pending_id = str(uuid4())
            params[f"id_{i}"] = pending_id
            pending_id_by_result[str(payload["result_id"])] = pending_id
            slots = [f"CAST(:id_{i} AS uuid)"]
            for column in _COLUMNS:
                key = f"{column}_{i}"
                params[key] = _truncate(column, payload.get(column))
                cast = "uuid" if column in _UUID_COLUMNS else "text"
                slots.append(f"CAST(:{key} AS {cast})")
            rows_sql.append(f"({', '.join(slots)})")
        won = db.execute(
            text(insert_sql.format(rows=", ".join(rows_sql))), params,
        ).scalars().all()
        inserted_ids.extend(str(r) for r in won)
    if not inserted_ids:
        return []

    # Advance the rows the insert won. The status predicate is repeated because
    # the insert's join and this update are separate statements, and it is what
    # arbitrates a concurrent claim: the loser's UPDATE waits on the winner's row
    # lock, then re-evaluates against the committed 'queued' and matches nothing.
    advanced = db.execute(
        text(
            "UPDATE results SET skip_trace_status = 'queued' "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND user_id = CAST(:uid AS uuid) "
            "  AND skip_trace_status = :claimable "
            "RETURNING id"
        ),
        {"ids": sorted(inserted_ids), "uid": user_id,
         "claimable": CLAIMABLE_RESULT_STATUS},
    ).scalars().all()
    claimed_ids = [str(r) for r in advanced]

    # A row we inserted whose result we could NOT advance is one a concurrent
    # writer settled or claimed in between. Withdraw it here, in this same
    # uncommitted transaction, so it never exists for anyone else to see.
    #
    # This is what makes the claim genuinely atomic rather than
    # eventually-consistent (Codex round 15 diff review, round 3). Leaving such a
    # row and trusting the dispatcher's cancel sweep to collect it was not
    # sufficient: the sweep runs ONCE per tick and BEFORE the submit loop, so a
    # row stranded after the sweep and before the loop could still be submitted
    # and charged for a lead that already had its answer. It also made
    # `require_enforcement=False` genuinely unsafe, because two concurrent
    # claims could both leave an active row behind with no index to stop them.
    # Now the loser of any such race withdraws its own row, index or not.
    stranded = sorted(set(inserted_ids) - set(claimed_ids))
    if stranded:
        db.execute(
            text(
                "DELETE FROM pending_skip_trace_rows "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            # By OUR primary keys, never by result_id: another writer's row for
            # the same lead is its business, and deleting it would hand that
            # lead back while its owner still believes it is claimed.
            {"ids": [pending_id_by_result[rid] for rid in stranded]},
        )
        _logger.info(
            "Skip-trace claim withdrew %d row(s) whose lead was settled or claimed "
            "concurrently; result ids: %s", len(stranded), stranded[:20],
        )
    return claimed_ids
