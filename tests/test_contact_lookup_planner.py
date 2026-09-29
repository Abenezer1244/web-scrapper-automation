"""The contact-lookup planner (Phase 1b-1c-i) and the canonical lookup price.

Contract: tasks/todo-lookup-contacts.md, "FINAL 1b-1c contract and build list".

The planner decides which leads a "look up contacts" quote may offer. It must never
quote a lead the real scrape enqueue would refuse, so the parity tests below run the
REAL `_enqueue_skip_trace_rows` over the same rows and compare. Real PG and Redis
(conftest); Tracerfy is never reached: the enqueue only writes pending rows.
"""
from __future__ import annotations

import random
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from src.api import contact_lookup_planner as planner
from src.api.contact_lookup_planner import (
    ALREADY_ANSWERED,
    ATIP,
    IN_PROGRESS,
    NO_ADDRESS,
    NOT_TRACEABLE,
    PLACEHOLDER,
    PREVIOUSLY_ATTEMPTED,
    QUOTABLE,
    SETTLED_CODE_VIOLATION,
    PlannerPolicy,
    classify,
    count_remaining,
    plan_tab_window,
    plan_window,
    policy_from_settings,
    status_buckets,
    tab_status_counts,
)
from src.config import lookup_pricing, settings
from src.db.models import Job, PendingSkipTraceRow, Result, ScraperConfig, SkipTraceCache
from src.db.session import system_sync_session

POLICY_OFF = PlannerPolicy(pierce_cv_owner_skip_trace_enabled=False)
POLICY_ON = PlannerPolicy(pierce_cv_owner_skip_trace_enabled=True)
_PARTY = "SAARENAS AVELINO G"


def _pin() -> str:
    return f"{random.randint(1, 9_999_999_999):010d}"


def _sdci(status: str | None = None) -> tuple[dict, str | None]:
    """A Seattle code violation whose owner was proven from King eRealProperty."""
    pin = _pin()
    ed = {"source": "seattle_sdci_code_violations", "record_number": "000123-26CP",
          "kc_pin": pin, "kc_pin_status": "matched",
          "kc_pin_source": "king_gis_point_in_parcel", "kc_pin_match": "exact",
          "owner_source": "king_erealproperty", "owner_pin": pin}
    if status is not None:
        ed["status"] = status
    return ed, None


def _tacoma() -> tuple[dict, str]:
    """A Tacoma code violation whose owner Pierce ATIP matched on the row's parcel."""
    pin = _pin()
    return ({"source": "tacoma_code_violations", "case_number": "CV000123",
             "owner_source": "pierce_atip", "owner_status": "matched", "owner_pin": pin}, pin)


def _row(**over) -> SimpleNamespace:
    base = {
        "id": str(uuid.uuid4()), "job_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC), "skip_trace_status": "not_attempted",
        "party_name": _PARTY, "parcel_id": None, "property_address": "1401 MAIN ST",
        "property_city": "VANCOUVER", "property_state": "WA", "property_zip": "98661",
        "mailing_address": None, "enrichment_data": {},
    }
    return SimpleNamespace(**{**base, **over})


# ── classify: one row per branch ─────────────────────────────────────────────

_CASES = [
    # (id, overrides, policy, bucket, trace_type)
    ("normal", {}, POLICY_OFF, QUOTABLE, "normal"),
    ("advanced_no_party", {"party_name": None}, POLICY_OFF, QUOTABLE, "advanced"),
    ("queued", {"skip_trace_status": "queued"}, POLICY_OFF, IN_PROGRESS, None),
    ("submitted", {"skip_trace_status": "submitted"}, POLICY_OFF, IN_PROGRESS, None),
    ("hit", {"skip_trace_status": "hit"}, POLICY_OFF, ALREADY_ANSWERED, None),
    ("miss", {"skip_trace_status": "miss"}, POLICY_OFF, ALREADY_ANSWERED, None),
    ("errored", {"skip_trace_status": "errored"}, POLICY_OFF, PREVIOUSLY_ATTEMPTED, None),
    ("unknown_status", {"skip_trace_status": "weird"}, POLICY_OFF, PREVIOUSLY_ATTEMPTED, None),
    # Status wins over the address: an in-progress lead is never reported as no-address.
    ("queued_no_address", {"skip_trace_status": "queued", "property_address": None},
     POLICY_OFF, IN_PROGRESS, None),
    ("address_null", {"property_address": None}, POLICY_OFF, NO_ADDRESS, None),
    ("address_empty", {"property_address": ""}, POLICY_OFF, NO_ADDRESS, None),
    ("address_spaces", {"property_address": "   "}, POLICY_OFF, NO_ADDRESS, None),
    ("address_tab_newline", {"property_address": "\t\n"}, POLICY_OFF, NO_ADDRESS, None),
    ("address_nbsp", {"property_address": "  "}, POLICY_OFF, NO_ADDRESS, None),
    ("address_no_address_but_mailing",
     {"property_address": None, "mailing_address": "9 ELM ST, SEATTLE, WA 98101"},
     POLICY_OFF, NO_ADDRESS, None),
    ("enrichment_unavailable_padded", {"property_address": "  (enrichment unavailable) "},
     POLICY_OFF, PLACEHOLDER, None),
    ("placeholder_street", {"property_address": "UNKNOWN UNKNOWN, VANCOUVER WA 98661"},
     POLICY_OFF, PLACEHOLDER, None),
    ("foreign", {"property_address": "1201-838 W HASTINGS ST VANCOUVER BC V6C 0A6"},
     POLICY_OFF, NOT_TRACEABLE, None),
    ("cv_owner_unproven", {"enrichment_data": {"source": "seattle_sdci_code_violations"}},
     POLICY_OFF, NOT_TRACEABLE, None),
    ("non_personal_party", {"party_name": "Weeds ? 1819 HARVARD AVE"},
     POLICY_OFF, NOT_TRACEABLE, None),
    ("no_locality", {"property_city": None, "property_state": None, "property_zip": None},
     POLICY_OFF, NOT_TRACEABLE, None),
]


@pytest.mark.parametrize(("over", "policy", "bucket", "trace_type"),
                         [c[1:] for c in _CASES], ids=[c[0] for c in _CASES])
def test_classify_branch(over, policy, bucket, trace_type):
    assert classify(_row(**over), policy) == (bucket, trace_type)


@pytest.mark.parametrize("status", ["Completed", "Open Duplicate"])
def test_a_settled_code_violation_is_excluded(status):
    ed, parcel = _sdci(status)
    assert classify(_row(enrichment_data=ed, parcel_id=parcel), POLICY_OFF).bucket == \
        SETTLED_CODE_VIOLATION


def test_an_open_code_violation_is_quotable():
    ed, parcel = _sdci("Open")
    assert classify(_row(enrichment_data=ed, parcel_id=parcel), POLICY_OFF).bucket == QUOTABLE


def test_the_settled_check_is_source_keyed_not_status_keyed():
    """A non-code-violation row whose json happens to say 'Completed' is not settled:
    `is_settled` keys on the SOURCE, so running it on every job never over-excludes."""
    row = _row(enrichment_data={"source": "king_probate", "status": "Completed"})
    assert classify(row, POLICY_OFF).bucket == QUOTABLE


def test_atip_follows_the_pinned_policy(monkeypatch):
    ed, parcel = _tacoma()
    row = _row(enrichment_data=ed, parcel_id=parcel, property_address="7 ELM ST",
               property_city="TACOMA", property_state="WA", property_zip="98402")
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", False)
    assert classify(row, POLICY_OFF).bucket == ATIP
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", True)
    assert classify(row, POLICY_ON) == (QUOTABLE, "normal")


def test_a_policy_that_disagrees_with_the_process_only_ever_excludes_more(monkeypatch):
    """15-14: the pinned flag says allowed, the process flag says not. The row must not
    be quoted: `build_pending_row_payload` re-reads the process flag and refuses it."""
    ed, parcel = _tacoma()
    row = _row(enrichment_data=ed, parcel_id=parcel, property_address="7 ELM ST",
               property_city="TACOMA", property_state="WA", property_zip="98402")
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", False)
    assert classify(row, POLICY_ON).bucket == NOT_TRACEABLE
    # And the reverse: the pinned flag says not allowed, the process says allowed.
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", True)
    assert classify(row, POLICY_OFF).bucket == ATIP


def test_policy_from_settings_reads_the_flag(monkeypatch):
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", True)
    assert policy_from_settings() == POLICY_ON
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", False)
    assert policy_from_settings() == POLICY_OFF


class _Recorder:
    """A row that records every attribute read of it, and raises on an unknown one."""

    def __init__(self, ns: SimpleNamespace):
        object.__setattr__(self, "_ns", ns)
        object.__setattr__(self, "seen", set())

    def __getattr__(self, name):
        self.seen.add(name)
        return getattr(self._ns, name)


def test_the_selected_columns_cover_every_attribute_the_planner_reads(monkeypatch):
    """The DB walk selects plain columns, and `getattr(row, x, None)` on a Row that
    lacks `x` returns None SILENTLY, so a column `build_pending_row_payload` starts
    reading later would read as missing here and real in the enqueue. Every branch
    runs through a recorder; everything it read must be a selected column."""
    selected = {c.key for c in planner._ROW_COLUMNS}
    rows = [_row(**c[1]) for c in _CASES]
    for status in ("Open", "Completed"):
        ed, parcel = _sdci(status)
        rows.append(_row(enrichment_data=ed, parcel_id=parcel))
    ed, parcel = _tacoma()
    rows.append(_row(enrichment_data=ed, parcel_id=parcel))
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", True)
    seen: set[str] = set()
    for r in rows:
        for policy in (POLICY_OFF, POLICY_ON):
            rec = _Recorder(r)
            classify(rec, policy)
            seen |= rec.seen
    assert seen <= selected, seen - selected


# ── plan_window: the cap and the ceiling ─────────────────────────────────────


def _ordered(n: int, **over) -> list[SimpleNamespace]:
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    return [_row(created_at=t0 + timedelta(seconds=i), **over) for i in range(n)]


def test_the_cap_stops_the_window_at_exactly_2000_quotable():
    rows = _ordered(2001)
    w = plan_window(rows, POLICY_OFF)
    assert len(w.quoted_ids) == 2000
    assert w.quoted_ids == [r.id for r in rows[:2000]]
    assert w.stopped == "cap"
    assert w.examined == 2000
    assert w.window_end == (rows[1999].created_at, rows[1999].id)


def test_a_window_that_fills_the_cap_on_its_last_row_still_reports_cap():
    w = plan_window(_ordered(2000), POLICY_OFF)
    assert (len(w.quoted_ids), w.stopped) == (2000, "cap")


def test_excluded_rows_are_counted_and_advanced_counted():
    rows = (_ordered(3) + _ordered(2, property_address=None)
            + _ordered(4, party_name=None))
    w = plan_window(rows, POLICY_OFF)
    assert len(w.quoted_ids) == 7
    assert w.advanced_count == 4
    assert w.counts[NO_ADDRESS] == 2
    assert w.stopped is None


@pytest.mark.parametrize(("n", "stopped"), [(19_999, None), (20_000, "scan_limit"),
                                            (20_001, "scan_limit")])
def test_the_scan_ceiling_bounds_examined_rows(n, stopped):
    w = plan_window(_ordered(n, property_address=None), POLICY_OFF)
    assert w.examined == min(n, 20_000)
    assert w.stopped == stopped
    assert w.quoted_ids == []


# ── DB: the tab walk (keyset, status counts, what is left) ───────────────────


def _config(user_id: str, record_type: str = "probate") -> str:
    sc_id = str(uuid.uuid4())
    with system_sync_session() as db:
        db.add(ScraperConfig(
            id=sc_id, user_id=user_id, name="planner", county="clark", state="WA",
            record_type=record_type, fields=[], enrichment=[],
            schedule={"frequency": "manual"}, deliver={"formats": ["csv"], "emails": []},
            skip_trace_enabled=True,
        ))
        db.commit()
    return sc_id


def _job(user_id: str, record_type: str = "probate", status: str = "done") -> str:
    sc_id = _config(user_id, record_type)
    job_id = str(uuid.uuid4())
    with system_sync_session() as db:
        db.add(Job(id=job_id, user_id=user_id, scraper_config_id=sc_id,
                   status=status, trigger="manual"))
        db.commit()
    return job_id


def _seed(user_id: str, job_id: str, specs: list[dict]) -> list[str]:
    """Insert every spec in ONE transaction, so they share one `created_at` (now()
    is the transaction's start): the keyset must then order them by `id`."""
    ids = []
    with system_sync_session() as db:
        for n, spec in enumerate(specs):
            rid = spec.pop("id", None) or str(uuid.uuid4())
            fields = {
                "id": rid, "job_id": job_id, "user_id": user_id, "party_name": _PARTY,
                "property_address": f"{1400 + n} MAIN ST", "property_city": "VANCOUVER",
                "property_state": "WA", "property_zip": "98661",
                "skip_trace_status": "not_attempted", "is_duplicate": False,
                "enrichment_data": {},
            }
            db.add(Result(**{**fields, **spec}))
            ids.append(rid)
        db.commit()
    return ids


def _today() -> date:
    return datetime.now(UTC).date()


async def test_the_keyset_pages_a_shared_created_at_by_id(db, business_user):
    job = _job(business_user.id)
    ids = _seed(business_user.id, job, [{} for _ in range(7)])
    w = await plan_tab_window(db, job, business_user.id, "new", _today(), POLICY_OFF,
                              cap=5, chunk=2)
    assert w.quoted_ids == sorted(ids)[:5]
    assert w.stopped == "cap"
    assert await count_remaining(db, job, business_user.id, "new", _today(), w) == 2


async def test_leads_bought_earlier_do_not_hold_the_window_back(db, business_user):
    job = _job(business_user.id)
    ids = sorted(_seed(business_user.id, job, [{} for _ in range(6)]))
    with system_sync_session() as s:
        s.execute(text("UPDATE results SET skip_trace_status = 'queued' "
                       "WHERE id = ANY(CAST(:i AS uuid[]))"),
                  {"i": ids[:2]})
        s.execute(text("UPDATE results SET skip_trace_status = 'hit' WHERE id = :i"),
                  {"i": ids[2]})
        s.commit()
    w = await plan_tab_window(db, job, business_user.id, "new", _today(), POLICY_OFF,
                              cap=2, chunk=2)
    assert w.quoted_ids == ids[3:5]
    assert w.examined == 2
    counts = status_buckets(await tab_status_counts(db, job, business_user.id, "new",
                                                    _today()))
    assert counts == {IN_PROGRESS: 2, ALREADY_ANSWERED: 1, PREVIOUSLY_ATTEMPTED: 0}
    assert await count_remaining(db, job, business_user.id, "new", _today(), w) == 1


async def test_nothing_left_counts_zero_without_a_query(db, business_user):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{} for _ in range(3)])
    w = await plan_tab_window(db, job, business_user.id, "new", _today(), POLICY_OFF)
    assert w.stopped is None
    assert await count_remaining(db, job, business_user.id, "new", _today(), w) == 0


async def test_the_tab_is_one_category_of_one_job_of_one_account(
    db, business_user, starter_user,
):
    job = _job(business_user.id)
    new, dup, quota, unlisted, superseded = _seed(business_user.id, job, [
        {},
        {"is_duplicate": True, "duplicate_reason": "prior_run"},
        {"enrichment_data": {"delivery_excluded_reason": "over_quota"}},
        # No property and no mailing address: not a lead, never listed.
        {"property_address": None, "mailing_address": None},
        {"is_duplicate": True, "duplicate_reason": "superseded"},
    ])
    other_job = _job(business_user.id)
    _seed(business_user.id, other_job, [{}])
    other_user_job = _job(starter_user.id)
    _seed(starter_user.id, other_user_job, [{}])

    w_new = await plan_tab_window(db, job, business_user.id, "new", _today(), POLICY_OFF)
    w_dup = await plan_tab_window(db, job, business_user.id, "already_delivered", _today(),
                                  POLICY_OFF)
    assert w_new.quoted_ids == [new]
    assert w_dup.quoted_ids == [dup]
    # A job id with the wrong owner is an empty tab, not the other account's rows.
    w_foreign = await plan_tab_window(db, other_user_job, business_user.id, "new",
                                      _today(), POLICY_OFF)
    assert w_foreign.quoted_ids == [] and w_foreign.examined == 0
    assert quota not in w_new.quoted_ids and unlisted not in w_new.quoted_ids
    assert superseded not in w_dup.quoted_ids


# ── PARITY with the real scrape enqueue ──────────────────────────────────────


@pytest.fixture
def _skip_trace_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


def _branch_specs() -> tuple[list[dict], set[str]]:
    """One lead per enqueue gate, on a code-violation job (so the enqueue runs its
    settled check too). Returns the specs and the tags expected to be QUOTED."""
    settled_ed, _ = _sdci("Completed")
    open_ed, _ = _sdci("Open")
    tacoma_ed, tacoma_pin = _tacoma()
    specs = [
        {"id": "normal"},
        {"id": "advanced", "party_name": None},
        {"id": "open_cv", "enrichment_data": open_ed},
        {"id": "tacoma_atip", "enrichment_data": tacoma_ed, "parcel_id": tacoma_pin,
         "property_address": "7 ELM ST", "property_city": "TACOMA", "property_zip": "98402"},
        {"id": "settled_cv", "enrichment_data": settled_ed},
        {"id": "placeholder", "property_address": "UNKNOWN UNKNOWN, VANCOUVER WA 98661"},
        {"id": "blank_with_mailing", "property_address": "   ",
         "mailing_address": "9 ELM ST, SEATTLE, WA 98101"},
        {"id": "non_personal", "party_name": "Weeds ? 1819 HARVARD AVE"},
        {"id": "foreign", "property_address": "1201-838 W HASTINGS ST VANCOUVER BC V6C 0A6"},
        {"id": "no_locality", "property_city": None, "property_state": None,
         "property_zip": None},
        {"id": "errored", "skip_trace_status": "errored"},
        {"id": "hit", "skip_trace_status": "hit"},
    ]
    return specs, {"normal", "advanced", "open_cv"}


def _tagged(user_id: str, job_id: str, specs: list[dict]) -> dict[str, str]:
    """Seed specs whose `id` is a TAG; returns tag -> real result id."""
    tags = [s["id"] for s in specs]
    real = [{**s, "id": str(uuid.uuid4())} for s in specs]
    return dict(zip(tags, _seed(user_id, job_id, real), strict=True))


def _enqueue(job_id: str, redis_client) -> None:
    from src.workers.tasks_helpers.enrich import _enqueue_skip_trace_rows

    with system_sync_session() as s:
        job = s.get(Job, job_id)
        cfg = s.get(ScraperConfig, job.scraper_config_id)
        _enqueue_skip_trace_rows(s, job, redis_client, job_id, cfg)


def _queued(job_id: str) -> dict[str, str]:
    with system_sync_session() as s:
        rows = s.execute(select(PendingSkipTraceRow.result_id, PendingSkipTraceRow.trace_type)
                         .where(PendingSkipTraceRow.job_id == job_id)).all()
    return {str(r): t for r, t in rows}


async def _planned(db, job_id: str, user_id: str) -> tuple[list[str], dict[str, str]]:
    policy = policy_from_settings()
    w = await plan_tab_window(db, job_id, user_id, "new", _today(), policy)
    rows = (await db.execute(select(*planner._ROW_COLUMNS)
                             .where(Result.id.in_(w.quoted_ids)))).all()
    return w.quoted_ids, {str(r.id): classify(r, policy).trace_type for r in rows}


@pytest.mark.parametrize("atip_allowed", [False, True])
async def test_the_planner_quotes_exactly_what_the_enqueue_queues(
    db, business_user, redis_client, _skip_trace_on, monkeypatch, atip_allowed,
):
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", atip_allowed)
    job = _job(business_user.id, record_type="code_violation", status="enriching")
    specs, expected = _branch_specs()
    ids = _tagged(business_user.id, job, specs)
    if atip_allowed:
        expected = expected | {"tacoma_atip"}

    quoted, trace_types = await _planned(db, job, business_user.id)
    _enqueue(job, redis_client)
    queued = _queued(job)

    assert set(quoted) == {ids[t] for t in expected}
    assert set(queued) == set(quoted)
    assert queued == trace_types
    assert trace_types[ids["advanced"]] == "advanced"


async def test_what_only_the_worker_can_see_makes_the_quote_an_upper_bound(
    db, business_user, redis_client, _skip_trace_on,
):
    """The enqueue also reads the lookup cache and the charged-unanswered pending rows,
    which the API role cannot. Both leads are quoted and neither is bought: what the
    worker queues is a SUBSET of the quote, never more (16-8)."""
    from src.scrapers.enrichment.skip_trace import lookup_subject_key

    earlier = _job(business_user.id, status="done")
    [original] = _seed(business_user.id, earlier, [
        {"property_address": "1600 MAIN ST", "dedup_hash": "h-unmatched",
         "skip_trace_status": "errored"},
    ])
    with system_sync_session() as s:
        s.add(PendingSkipTraceRow(
            job_id=earlier, result_id=original, user_id=business_user.id,
            property_address="1600 MAIN ST", city="VANCOUVER", state="WA",
            trace_type="normal", status="unmatched",
            enqueued_at=datetime.now(UTC) - timedelta(days=1),
        ))
        s.add(SkipTraceCache(
            address_hash=lookup_subject_key(business_user.id, "1700 MAIN ST", "VANCOUVER",
                                             "WA", "normal", "AVELINO", "SAARENAS"),
            phone="2065550133", phone_type="Mobile", email=None,
            phones=[{"number": "2065550133", "type": "Mobile"}], emails=None,
            fetched_at=datetime.now(UTC),
        ))
        s.commit()

    job = _job(business_user.id, status="enriching")
    plain, unmatched, cached = _seed(business_user.id, job, [
        {"property_address": "1500 MAIN ST"},
        {"property_address": "1600 MAIN ST", "dedup_hash": "h-unmatched",
         "is_duplicate": True, "duplicate_reason": "prior_run"},
        {"property_address": "1700 MAIN ST"},
    ])

    w_new = await plan_tab_window(db, job, business_user.id, "new", _today(), POLICY_OFF)
    w_dup = await plan_tab_window(db, job, business_user.id, "already_delivered", _today(),
                                  POLICY_OFF)
    quoted = set(w_new.quoted_ids) | set(w_dup.quoted_ids)
    _enqueue(job, redis_client)
    queued = set(_queued(job))

    assert quoted == {plain, unmatched, cached}
    assert queued == {plain}
    assert queued <= quoted


# ── The trial credit cap (audit S3-03 / S4-01: a trial's lifetime allowance) ─


def test_the_planners_credit_table_is_the_workers():
    """Restated in the planner because importing `src.workers` builds the Celery app."""
    from src.workers.skip_trace_capacity import CREDITS_PER_ROW

    assert planner.CREDITS == dict(CREDITS_PER_ROW)


def test_a_credit_cap_stops_at_the_allowance():
    rows = _ordered(30)
    w = plan_window(rows, POLICY_OFF, credit_cap=25)
    assert w.quoted_ids == [r.id for r in rows[:25]]
    assert (w.quoted_credits, w.stopped, w.over_credit_cap) == (25, "credit_cap", 0)
    assert w.credit_cap == 25
    assert plan_window(rows, POLICY_OFF).credit_cap is None


def test_a_lead_that_does_not_fit_is_skipped_and_a_cheaper_one_still_fits():
    """The claim's rule: in order, keep a lead when its cost fits the room left."""
    advanced = _ordered(13, party_name=None)  # 2 credits each
    t = advanced[-1].created_at
    normal = _row(created_at=t + timedelta(seconds=1))  # 1 credit
    w = plan_window([*advanced, normal], POLICY_OFF, credit_cap=25)
    assert w.quoted_ids == [r.id for r in advanced[:12]] + [normal.id]
    assert (w.quoted_credits, w.advanced_count, w.over_credit_cap) == (25, 12, 1)
    assert w.stopped == "credit_cap"


def test_a_credit_cap_of_zero_quotes_nothing():
    w = plan_window(_ordered(3), POLICY_OFF, credit_cap=0)
    assert (w.quoted_ids, w.stopped, w.examined) == ([], "credit_cap", 1)


def test_no_credit_cap_counts_credits_but_never_stops_on_them():
    rows = _ordered(3) + _ordered(2, party_name=None)
    w = plan_window(rows, POLICY_OFF)
    assert (len(w.quoted_ids), w.quoted_credits, w.stopped) == (5, 7, None)


async def test_the_credit_cap_keeps_exactly_what_the_real_claim_keeps_for_a_trial(
    db, business_user, monkeypatch,
):
    """Give the REAL `claim_skip_trace_rows` every quotable lead of the window, in
    window order (the order the 1b-2 worker will hand it), for a trial account with
    nothing used yet: what it keeps is what the planner quotes under the allowance."""
    from src.scrapers.enrichment.skip_trace import build_pending_row_payload
    from src.workers.skip_trace_claim import (
        ACCESS_TRIAL,
        claim_skip_trace_rows,
        lock_job_for_claim,
        paid_lookup_access,
    )

    monkeypatch.setattr(settings, "SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE", 7)
    await db.execute(text(
        "UPDATE users SET plan = 'pro', subscription_status = NULL, "
        "trial_ends_at = now() + interval '7 days' WHERE id = :u"), {"u": business_user.id})
    await db.commit()
    job = _job(business_user.id)
    # Allowance 7: advanced (2), normal (3), advanced (5), advanced (7), then the room
    # is 0, so the last advanced and the last normal are both held. Each row gets its
    # own created_at: rows of one transaction share it, and the window would then
    # order them by random id, making the sequence (and the credits) vary per run.
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    kinds = [None, _PARTY, None, None, None, _PARTY]
    specs = [{"party_name": p, "created_at": t0 + timedelta(seconds=i)}
             for i, p in enumerate(kinds)]
    _seed(business_user.id, job, specs)

    window = await plan_tab_window(db, job, business_user.id, "new", _today(), POLICY_OFF,
                                   credit_cap=settings.SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE)

    with system_sync_session() as s:
        user = s.execute(text(
            "SELECT id, plan, is_admin, subscription_status, trial_ends_at, "
            "entitlement_ends_at, entitlement_grace_ends_at FROM users WHERE id = :u"
        ), {"u": business_user.id}).one()
        assert paid_lookup_access(user) == ACCESS_TRIAL  # the case is real
        rows = s.execute(select(Result).where(Result.job_id == job)
                         .order_by(Result.created_at, Result.id)).scalars().all()
        lock_job_for_claim(s, job)
        report: dict = {}
        claimed = claim_skip_trace_rows(
            s, [build_pending_row_payload(r) for r in rows], report=report)
        s.rollback()  # nothing is kept: the claim is only the oracle here

    assert report["access"] == ACCESS_TRIAL
    assert sorted(claimed) == sorted(window.quoted_ids)
    assert window.quoted_credits == 7
    assert report["held"] == len(rows) - len(claimed)


# ── Pricing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("plan", "cents"), [
    ("pro", 8), ("business", 8), ("agency", 5), (" Pro ", 8), ("AGENCY", 5),
    ("starter", None), (None, None), ("enterprise", None),
])
def test_unit_price_per_plan(plan, cents):
    assert lookup_pricing.unit_price_cents(plan) == cents


def test_the_price_snapshot_constants():
    assert lookup_pricing.CURRENCY == "USD"
    assert lookup_pricing.PRICING_VERSION == "2026-06"


@pytest.mark.parametrize("plan", ["starter", "pro", "business", "agency"])
async def test_the_quoted_price_equals_what_the_billing_page_shows(
    db, client, business_user, business_token, plan,
):
    """`/billing/skip-trace-usage` still carries its own copy of the rates (moving it
    onto `lookup_pricing` is a logged follow-up). Until then the two must agree."""
    await db.execute(text("UPDATE users SET plan = :p WHERE id = :u"),
                     {"p": plan, "u": business_user.id})
    await db.commit()
    r = await client.get("/billing/skip-trace-usage",
                         headers={"Authorization": f"Bearer {business_token}"})
    assert r.status_code == 200, r.text
    rate = r.json()["overage_rate_usd"]
    expected = lookup_pricing.unit_price_cents(plan)
    assert (None if rate is None else round(rate * 100)) == expected


def _user(plan: str, used: int, period_start: datetime | None, *, window_start: datetime,
          window_end: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        plan=plan, skip_trace_used_this_month=used, skip_trace_period_start=period_start,
        quota_period_start=window_start, quota_period_end=window_end,
        quota_anchor_at=window_start, subscription_status="active",
        entitlement_grace_ends_at=None, entitlement_ends_at=None,
    )


_NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
_START = _NOW - timedelta(days=10)
_END = _NOW + timedelta(days=20)


@pytest.mark.parametrize(("plan", "used", "period_start", "window", "left"), [
    ("business", 100, _START, (_START, _END), 900),
    ("business", 1500, _START, (_START, _END), 0),
    ("pro", 250, _START, (_START, _END), 0),
    ("agency", 0, _START, (_START, _END), 2000),
    # Unset period: the counter never belonged to this window.
    ("business", 700, None, (_START, _END), 1000),
    # The window ENDED 5 days ago and the rollover has not caught up: the stored
    # counter belongs to the old window, so the new one's full allowance is left.
    ("business", 1000, _START - timedelta(days=35),
     (_START - timedelta(days=35), _NOW - timedelta(days=5)), 1000),
    ("starter", 0, _START, (_START, _END), 0),
    (" Business ", 100, _START, (_START, _END), 900),
])
def test_included_lookups_remaining(plan, used, period_start, window, left):
    user = _user(plan, used, period_start, window_start=window[0], window_end=window[1])
    assert lookup_pricing.included_lookups_remaining(user, _NOW) == left
