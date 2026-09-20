"""UNKNOWN must never reach the client as ZERO.

Verified in production on job b80bd9a5 (King WA probate, manual, completed, 57
records scraped, 2 billed): the run took 8m42s, and 8m18s of it produced no
status change, no log line and no counter movement. The Live Run page therefore
showed a giant "0%" over "Records 0 / Pages 0" for a run that was working
perfectly, then jumped straight to done.

The cause is in the data model, not the UI. jobs.page_current / page_total /
record_count are `Integer NOT NULL DEFAULT 0`, so "nothing has been measured
yet" and "we measured, and the answer is zero" are the same value, and no client
can tell them apart. Migration 099 adds nullable observations where NULL means
UNOBSERVED, and these tests pin the one rule that makes them worth having: a
fact nobody has measured is absent, never 0, and never a percentage.

They also pin the reverse, which is just as easy to get wrong: a county that
really did return nothing must report a confident 0, not an evasive blank.
"""
from datetime import UTC, datetime, timedelta

from src.api.schemas import JobResponse
from src.config.constants import HEARTBEAT_STALE_MINUTES, JOB_STAGES


def _ago(**kw) -> datetime:
    """Relative to NOW AT CALL TIME — the full suite runs long enough that an
    import-time 'now' drifts past the liveness thresholds mid-run."""
    return datetime.now(UTC) - timedelta(**kw)


def _job(**over) -> JobResponse:
    """A live scraping job that has reported NOTHING. The default is the state the
    whole bug lived in: healthy, working, and with nothing measurable to say."""
    base = {
        "id": "b80bd9a5-c5f7-4239-9519-71eb8fbc4fa3",
        "user_id": "e73585c6-e10d-48e3-941f-28090380ff51",
        "scraper_config_id": "ea533c9e-d194-4f74-a8e7-4b32e9d428e1",
        "status": "scraping",
        "trigger": "manual",
        "page_current": 0,
        "page_total": 0,
        "record_count": 0,
        "export_key": None,
        "error_message": None,
        "retry_count": 0,
        "started_at": _ago(minutes=2),
        "finished_at": None,
        "created_at": _ago(minutes=3),
        "last_heartbeat_at": _ago(seconds=30),
        "stage": "connecting",
        "stage_started_at": _ago(minutes=2),
    }
    base.update(over)
    return JobResponse(**base)


# ─── The rule: unknown is absent, not zero ───────────────────────────────────

def test_an_unreported_run_reports_no_counters_and_no_percentage():
    """State 2: connecting, with no record count. This is 401 of the 522 seconds
    of the production run, and the state that rendered as 0%."""
    j = _job()
    assert j.progress_pct is None
    assert j.units_done is None
    assert j.units_total is None
    assert j.records_found is None
    assert j.stage_label == "Connecting to the county records system"


def test_unknown_never_serializes_as_zero():
    """The guarantee, checked on the wire rather than on the object. A client reads
    JSON, and `0` and `null` are what it has to tell apart."""
    payload = _job().model_dump(mode="json")
    for field in ("progress_pct", "units_done", "units_total", "records_found",
                  "progress_unit", "estimated_total_records",
                  "estimated_seconds_remaining", "estimated_time_remaining"):
        assert payload[field] is None, f"{field} leaked {payload[field]!r} for an unmeasured run"


def test_an_observed_zero_is_reported_as_zero():
    """The other half of the rule. A county that answered and had nothing is a
    RESULT. Reporting it as unknown would leave the user waiting for a number that
    is never coming."""
    j = _job(stage="saving", records_found=0, stage_started_at=_ago(seconds=5))
    assert j.records_found == 0
    assert j.model_dump(mode="json")["records_found"] == 0
    assert j.stage_label == "Saving records: 0 records found"


def test_a_denominator_of_zero_is_unknown_not_complete():
    """units_total=0 would divide by zero, and reads as 'no work' rather than
    'unknown work'. It must produce no percentage, not 0% and not 100%."""
    j = _job(stage="scraping", units_done=0, units_total=0)
    assert j.progress_pct is None


# ─── Percentages come from real denominators, and only from those ────────────

def test_a_real_denominator_produces_a_real_percentage():
    j = _job(stage="scraping", units_done=3, units_total=5, progress_unit="chunk",
             records_found=42)
    assert j.progress_pct == 60
    assert j.stage_label == "Collecting records: Part 3 of 5"


def test_counts_without_a_denominator_are_shown_without_a_percentage():
    """Most connectors never learn a total. The counts they DO have are still worth
    showing; the percentage they cannot support is not invented."""
    j = _job(stage="scraping", units_done=3, units_total=None, progress_unit="page",
             records_found=87)
    assert j.progress_pct is None
    assert j.stage_label == "Collecting records: Page 3"


def test_a_near_complete_stage_is_not_rounded_up_to_finished():
    """99.6% of a stage is not a finished stage. min(99) exists so the bar cannot
    announce a completion that has not happened."""
    j = _job(stage="scraping", units_done=249, units_total=250, progress_unit="page")
    assert j.progress_pct == 99


def test_the_percentage_may_go_down_when_more_work_is_discovered():
    """A chunked scraper can find more work mid-run. Revising 60% to 37% with the
    new counts beside it is honest; freezing or capping it to avoid the jolt is
    not. Nothing in the response may prevent the decrease."""
    before = _job(stage="scraping", units_done=3, units_total=5, progress_unit="chunk")
    after = _job(stage="scraping", units_done=3, units_total=8, progress_unit="chunk")
    assert before.progress_pct == 60
    assert after.progress_pct == 37
    assert after.progress_pct < before.progress_pct


def test_elapsed_time_is_never_turned_into_a_percentage():
    """The forbidden shortcut, pinned. A run can be long, healthy and completely
    unmeasured; time passing is not progress."""
    j = _job(started_at=_ago(minutes=45), last_heartbeat_at=_ago(seconds=10))
    assert j.elapsed_seconds >= 45 * 60
    assert j.progress_pct is None
    assert j.estimated_time_remaining is None


def test_a_measured_zero_of_n_is_zero_percent_not_unknown():
    """The mirror of the headline bug, and wrong for the same reason. King
    announces its chunk count before it has finished one, so 0 of 6 is something
    somebody MEASURED. _stage_label already says "Part 0 of 6"; refusing the
    matching 0% had one field calling that observation measured and another
    calling it unknown."""
    j = _job(stage="scraping", units_done=0, units_total=6, progress_unit="chunk")
    assert j.progress_pct == 0
    assert j.stage_label == "Collecting records: Part 0 of 6"
    # Still no estimate: a rate needs two COMPLETED units, and there are none.
    assert j.estimated_seconds_remaining is None


def test_no_denominator_is_still_unknown_and_never_zero():
    """The other half of the pair, kept next to it on purpose. Without a
    denominator there is nothing to be zero percent OF, and this is the state the
    production run sat in for 401 seconds."""
    assert _job(stage="scraping", units_done=0, units_total=None).progress_pct is None
    # page_total=0 has always meant "no denominator", so a legacy row's 0 is
    # unknown too, never a measured zero.
    assert _job(stage=None, page_current=0, page_total=0).progress_pct is None

# ─── Estimates need more than one observation ────────────────────────────────

def test_one_completed_unit_is_not_enough_for_an_estimate():
    """The first unit of a scrape carries all of the browser startup and captcha
    cost, so extrapolating from it is wrong by minutes. Better to say nothing."""
    j = _job(stage="scraping", units_done=1, units_total=5, progress_unit="chunk",
             records_found=10, started_at=_ago(minutes=6))
    assert j.progress_pct == 20
    assert j.estimated_seconds_remaining is None
    assert j.estimated_time_remaining is None
    assert j.estimated_total_records is None


def test_two_completed_units_support_an_estimate():
    j = _job(stage="scraping", units_done=2, units_total=6, progress_unit="chunk",
             records_found=20, started_at=_ago(minutes=4))
    assert j.estimated_seconds_remaining is not None
    assert j.estimated_time_remaining is not None
    # 20 records over 2 of 6 units extrapolates to 60.
    assert j.estimated_total_records == 60


def test_the_estimate_rate_comes_from_the_stage_clock_not_the_run_clock():
    """The production shape: 401 seconds connecting, THEN a scrape that is
    moving briskly. The per-unit rate must be measured over the scrape alone.
    Charging the connect prelude to the scrape inflates the ETA by minutes,
    which is a fabricated number by a slower route than a fake percentage."""
    j = _job(stage="scraping", stage_started_at=_ago(seconds=60),
             started_at=_ago(seconds=460), last_heartbeat_at=_ago(seconds=5),
             units_done=2, units_total=6, progress_unit="chunk")
    # 2 chunks in 60s of SCRAPING is 30s each, so 4 left is about 120s. Billing
    # the whole 460s to those 2 chunks would say about 920s instead.
    assert j.elapsed_seconds >= 450
    assert 100 <= j.estimated_seconds_remaining <= 140


def test_a_legacy_row_with_no_stage_clock_still_gets_an_estimate():
    """stage_started_at is NULL only for a worker predating migration 099. For
    that row the whole run is the one unmeasured activity, so elapsed_seconds is
    the honest denominator rather than a reason to say nothing."""
    j = _job(stage=None, stage_started_at=None, started_at=_ago(seconds=200),
             page_current=2, page_total=8, record_count=40)
    assert j.stage_seconds is None
    # 2 of 8 pages in 200s is 100s each, so the 6 remaining are about 600s.
    assert 540 <= j.estimated_seconds_remaining <= 660


# ─── Stage reporting ─────────────────────────────────────────────────────────

def test_every_stage_the_worker_writes_has_customer_copy():
    """A stage with no entry in the label map would reach the screen as a raw
    identifier like 'queuing_contacts'. This is the test that fails when someone
    adds a stage to JOB_STAGES and forgets the wording."""
    for stage in JOB_STAGES:
        label = _job(stage=stage).stage_label
        assert label, f"{stage} has no customer-facing copy"
        assert stage not in label, f"{stage} leaked its identifier into {label!r}"


def test_stage_seconds_measures_the_current_activity_not_the_run():
    """'Still connecting to King County' needs the age of the STAGE. The run may
    have been going far longer and spent most of it elsewhere."""
    j = _job(started_at=_ago(minutes=30), stage="connecting",
             stage_started_at=_ago(minutes=3), last_heartbeat_at=_ago(seconds=10))
    assert j.elapsed_seconds >= 30 * 60
    assert 170 <= j.stage_seconds <= 220


def test_a_long_stage_with_a_live_heartbeat_is_not_stalled():
    """State 3: connecting for minutes while the worker is provably alive. This is
    the production run. It must read as slow, never as stuck."""
    j = _job(stage="connecting", stage_started_at=_ago(minutes=7),
             started_at=_ago(minutes=7), last_heartbeat_at=_ago(seconds=20))
    assert j.progress_stalled is False
    assert j.stage_seconds >= 6 * 60


def test_a_stalled_run_keeps_its_stage_but_stops_claiming_progress():
    """State 17. The stage is retained because 'it went quiet while connecting' is
    the most useful thing support can be told, but the copy must stop describing
    work that is not happening."""
    j = _job(stage="connecting", last_heartbeat_at=_ago(minutes=HEARTBEAT_STALE_MINUTES + 5))
    assert j.progress_stalled is True
    assert j.stage == "connecting"
    assert j.stage_label == "No recent progress reported. Checking on this run."


def test_a_worker_too_old_to_report_a_stage_still_gets_a_label():
    """Rolling deploys guarantee these rows exist. A NULL stage falls back to the
    coarser status wording rather than going blank."""
    j = _job(stage=None, stage_started_at=None, status="enriching")
    assert j.stage_label == "Adding property and mailing details"
    assert j.stage_seconds is None


# ─── Legacy rows, which the rolling deploy guarantees ────────────────────────

def test_legacy_counters_are_read_but_a_legacy_zero_stays_unknown():
    """on_progress has always passed page_total=0 to mean 'no denominator'. The
    fallback must resolve that ambiguity toward UNKNOWN — reading it as a measured
    zero is how the bug would come back through the back door."""
    j = _job(stage=None, stage_started_at=None, page_current=4, page_total=0,
             record_count=88)
    assert j.units_done == 4          # a real count, carried over
    assert j.units_total is None      # 0 meant "unknown", not "zero"
    assert j.records_found == 88
    assert j.progress_pct is None     # no denominator, so no percentage
    # "4", not "4 of 0" and not "4 of ?": there is no denominator, so none is implied.
    assert j.stage_label == "Collecting records: 4"


def test_a_legacy_row_with_a_real_denominator_still_gets_a_percentage():
    j = _job(stage=None, stage_started_at=None, page_current=3, page_total=4,
             record_count=30)
    assert (j.units_done, j.units_total) == (3, 4)
    assert j.progress_pct == 75
    # No progress_unit was recorded, so the count is shown WITHOUT a guessed noun.
    assert j.stage_label == "Collecting records: 3 of 4"


# ─── Retry and terminal states ───────────────────────────────────────────────

def test_a_waiting_retry_carries_its_earliest_start_time():
    """State 5. next_retry_at is passed through so the client can say how long the
    wait is, and is documented as NOT BEFORE rather than a promise."""
    soon = datetime.now(UTC) + timedelta(seconds=18)
    j = _job(status="pending", retry_count=1, started_at=None,
             last_heartbeat_at=None, stage=None, stage_started_at=None,
             next_retry_at=soon)
    assert j.retry_pending is True
    assert j.stage_label == "Waiting to retry"
    assert j.next_retry_at == soon


def test_a_finished_run_reports_completion_not_a_stage():
    """State 14. 100 here is not an extrapolation, the run finished. But there is
    no current activity left, so the stage clock must stop rather than keep
    counting time since the work ended."""
    j = _job(status="done", finished_at=_ago(seconds=5), record_count=2,
             records_found=57, stage="finalizing")
    assert j.progress_pct == 100
    assert j.stage_label == "Complete: 2 records"
    assert j.stage_seconds is None
    # Both numbers survive: 2 is what was billed and delivered, 57 is what the
    # scrape turned up. Conflating them is what made the Records tile read "2".
    assert j.records_found == 57


def test_a_cancelled_run_claims_no_progress():
    """State 16."""
    j = _job(status="cancelled", finished_at=_ago(seconds=5))
    assert j.progress_pct is None
    assert j.progress_stalled is False
    assert j.stage_label == "Cancelled"


# ─── A counter belongs to the activity that produced it ──────────────────────

def test_counters_do_not_leak_from_the_scrape_into_the_next_activity():
    """Codex, on the first version of the stage work.

    A scrape that finished 5 of 5 chunks leaves units_done=5, units_total=5 on the
    row. If the next stage inherits them, enrichment is labelled "Adding property
    and mailing details: Part 5 of 5" at 99% with a zero-second estimate — a real
    number describing work nobody measured, which is the whole failure this change
    set exists to remove. The worker clears them on every stage change; this pins
    the API half, that a cleared row stays cleared.
    """
    j = _job(stage="enriching", stage_started_at=_ago(seconds=10),
             units_done=None, units_total=None, progress_unit=None,
             records_found=57, record_count=57,
             page_current=5, page_total=5)  # legacy counters still hold the scrape
    assert j.progress_pct is None
    assert j.units_done is None and j.units_total is None
    assert j.estimated_time_remaining is None
    assert j.stage_label == "Adding property and mailing details: 57 records found"


def test_a_pre_099_worker_still_gets_its_page_counters_read():
    """The legacy fallback is gated on a NULL stage, so it must still fire for a row
    a worker from before the migration wrote."""
    j = _job(stage=None, stage_started_at=None, page_current=3, page_total=4,
             record_count=30)
    assert (j.units_done, j.units_total) == (3, 4)
    assert j.progress_pct == 75
