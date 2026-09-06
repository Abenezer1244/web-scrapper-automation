"""Stale-claim reconciliation: match a stuck claim to Tracerfy's own queue list.

The dispatcher commits a durable claim BEFORE the POST and never auto-resubmits
an unknown outcome (that would pay twice), so a claim whose outcome was never
learned parks in 'submitting'. Production accumulated 637 such rows across 15
jobs and 3 users, stuck up to four days, every one showing "Processing".

match_remote_queue is the decision that unsticks them, and it is the dangerous
one: adopting the WRONG queue id attaches one batch's results to another batch's
leads and bills the wrong tenants. These tests pin the conservative predicate --
especially every case where it must REFUSE rather than guess.
"""

from datetime import UTC, datetime

from src.workers.skip_trace_dispatcher import (
    _partition_submittable,
    match_remote_queue,
    row_is_submittable,
)

CLAIM = datetime(2026, 9, 3, 15, 0, 0, tzinfo=UTC)


def _q(**kw):
    """A Tracerfy queue-list entry, shaped exactly like GET /v1/api/queues/."""
    base = {
        "id": 500,
        "created_at": "2026-09-03T15:00:02.000000Z",
        "pending": False,
        "download_url": "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/x.csv",
        "rows_uploaded": 10,
        "credits_deducted": 10,
        "queue_type": "api",
        "trace_type": "normal",
        "credits_per_lead": 1,
    }
    base.update(kw)
    return base


class TestNoMatch:
    """No remote queue => Tracerfy never accepted it => never charged."""

    def test_empty_queue_list_is_a_clean_release(self):
        verdict, q = match_remote_queue([], CLAIM, "normal", 10, set())
        assert (verdict, q) == ("none", None)

    def test_this_is_the_637_row_production_case(self):
        # Prod: rows claimed 2026-09-03 15:00, and Tracerfy's queue list holds
        # nothing between 2026-09-02 (158749) and 2026-09-06 (162455/162456).
        remote = [
            _q(id=158749, created_at="2026-09-02T04:24:50.307452Z", rows_uploaded=24),
            _q(id=162455, created_at="2026-09-06T09:28:10.657913Z", rows_uploaded=2),
        ]
        verdict, q = match_remote_queue(remote, CLAIM, "normal", 374, set())
        assert (verdict, q) == ("none", None)

    def test_different_trace_type_never_matches(self):
        # normal and advanced go out ~0.6s apart; trace_type is what separates
        # the pair, so it must be an absolute filter.
        remote = [_q(trace_type="advanced")]
        assert match_remote_queue(remote, CLAIM, "normal", 10, set())[0] == "none"

    def test_queue_already_recorded_locally_is_not_a_candidate(self):
        # A queue owning a skip_trace_queues row belongs to a batch that was
        # booked successfully. Adopting it would re-point a healthy batch.
        remote = [_q(id=500)]
        assert match_remote_queue(remote, CLAIM, "normal", 10, {500})[0] == "none"

    def test_non_api_queue_is_ignored(self):
        # A queue created through Tracerfy's own UI is never ours.
        remote = [_q(queue_type="app")]
        assert match_remote_queue(remote, CLAIM, "normal", 10, set())[0] == "none"

    def test_created_before_the_window_is_ignored(self):
        remote = [_q(created_at="2026-09-03T14:57:00.000000Z")]  # 3 min early
        assert match_remote_queue(remote, CLAIM, "normal", 10, set())[0] == "none"

    def test_created_after_the_window_is_ignored(self):
        # The next tick is 5 min later; the window must not reach it.
        remote = [_q(created_at="2026-09-03T15:05:00.000000Z")]
        assert match_remote_queue(remote, CLAIM, "normal", 10, set())[0] == "none"

    def test_uploaded_more_rows_than_we_claimed_is_not_our_batch(self):
        remote = [_q(rows_uploaded=11)]
        assert match_remote_queue(remote, CLAIM, "normal", 10, set())[0] == "none"

    def test_zero_rows_uploaded_is_not_a_match(self):
        remote = [_q(rows_uploaded=0)]
        assert match_remote_queue(remote, CLAIM, "normal", 10, set())[0] == "none"

    def test_unparseable_created_at_is_ignored_not_assumed(self):
        remote = [_q(created_at="not-a-timestamp")]
        assert match_remote_queue(remote, CLAIM, "normal", 10, set())[0] == "none"

    def test_missing_created_at_is_ignored(self):
        remote = [_q(created_at=None)]
        assert match_remote_queue(remote, CLAIM, "normal", 10, set())[0] == "none"


class TestSingleMatch:
    def test_exact_row_count_matches(self):
        verdict, q = match_remote_queue([_q(id=777)], CLAIM, "normal", 10, set())
        assert verdict == "one"
        assert q["id"] == 777

    def test_deduped_upload_still_matches(self):
        """Tracerfy de-duplicates identical addresses, so rows_uploaded is
        legitimately SMALLER than what we claimed (prod: 25 sent -> 24
        uploaded -> all 25 rows reconciled). Requiring equality here would
        strand every batch containing a repeated address."""
        verdict, q = match_remote_queue([_q(rows_uploaded=24)], CLAIM, "normal", 25, set())
        assert verdict == "one"
        assert q["rows_uploaded"] == 24

    def test_matches_at_the_window_edges(self):
        early = _q(id=1, created_at="2026-09-03T14:59:00.000000Z")  # -60s
        late = _q(id=2, created_at="2026-09-03T15:02:00.000000Z")   # +120s
        assert match_remote_queue([early], CLAIM, "normal", 10, set())[0] == "one"
        assert match_remote_queue([late], CLAIM, "normal", 10, set())[0] == "one"

    def test_missing_queue_type_is_tolerated(self):
        # Only EXCLUDE on a queue_type we can read and that is not 'api'.
        remote = [_q(queue_type=None)]
        assert match_remote_queue(remote, CLAIM, "normal", 10, set())[0] == "one"

    def test_the_advanced_sibling_does_not_confuse_a_normal_claim(self):
        # The real dispatcher shape: both trace types submitted ~0.6s apart.
        remote = [
            _q(id=100, trace_type="normal", rows_uploaded=8),
            _q(id=101, trace_type="advanced", rows_uploaded=4,
               created_at="2026-09-03T15:00:02.600000Z"),
        ]
        verdict, q = match_remote_queue(remote, CLAIM, "normal", 10, set())
        assert verdict == "one" and q["id"] == 100
        verdict, q = match_remote_queue(remote, CLAIM, "advanced", 10, set())
        assert verdict == "one" and q["id"] == 101


class TestAmbiguous:
    """Refuse rather than guess. Adopting the wrong queue cross-contaminates."""

    def test_two_plausible_queues_refuse_adoption(self):
        remote = [
            _q(id=200, rows_uploaded=9),
            _q(id=201, rows_uploaded=10, created_at="2026-09-03T15:00:05.000000Z"),
        ]
        verdict, q = match_remote_queue(remote, CLAIM, "normal", 10, set())
        assert verdict == "ambiguous"
        assert q is None, "must not hand back a queue it refused to choose"

    def test_the_prod_double_submission_shape_is_ambiguous(self):
        """Prod holds 98183 and 98193: both advanced, both 147 rows / 144
        credits. If two such twins ever land in one window, refuse."""
        remote = [
            _q(id=98183, trace_type="advanced", rows_uploaded=147,
               created_at="2026-09-03T15:00:01.000000Z"),
            _q(id=98193, trace_type="advanced", rows_uploaded=147,
               created_at="2026-09-03T15:00:03.000000Z"),
        ]
        assert match_remote_queue(remote, CLAIM, "advanced", 147, set())[0] == "ambiguous"

    def test_one_known_twin_disambiguates_the_other(self):
        # Once one twin is recorded locally, the other is unambiguous.
        remote = [
            _q(id=98183, trace_type="advanced", rows_uploaded=147,
               created_at="2026-09-03T15:00:01.000000Z"),
            _q(id=98193, trace_type="advanced", rows_uploaded=147,
               created_at="2026-09-03T15:00:03.000000Z"),
        ]
        verdict, q = match_remote_queue(remote, CLAIM, "advanced", 147, {98183})
        assert verdict == "one" and q["id"] == 98193


class _Row:
    """Minimal stand-in for a PendingSkipTraceRow (no DB needed)."""

    def __init__(self, address="123 MAIN ST", city="SEATTLE", state="WA", rid="r1"):
        self.property_address = address
        self.city = city
        self.state = state
        self.id = rid
        self.result_id = rid


class TestSubmittability:
    """Tracerfy requires address+city+state and DROPS a row missing one rather
    than erroring, so an unsubmittable row must never reach the POST."""

    def test_complete_row_is_submittable(self):
        assert row_is_submittable(_Row()) is True

    def test_missing_state_is_not_submittable(self):
        # The live prod case: queue 162456 sent 4 rows, rows_uploaded=3.
        assert row_is_submittable(_Row(state=None)) is False

    def test_missing_city_is_not_submittable(self):
        assert row_is_submittable(_Row(city=None)) is False

    def test_missing_address_is_not_submittable(self):
        assert row_is_submittable(_Row(address=None)) is False

    def test_whitespace_only_is_not_a_value(self):
        assert row_is_submittable(_Row(state="   ")) is False
        assert row_is_submittable(_Row(city="\t")) is False

    def test_empty_string_is_not_a_value(self):
        assert row_is_submittable(_Row(state="")) is False

    def test_zip_is_not_required(self):
        # Tracerfy documents zip_column as optional and fills it from its data.
        row = _Row()
        row.zip = None
        assert row_is_submittable(row) is True

    def test_partition_preserves_fifo_order_and_splits_correctly(self):
        rows = [
            _Row(rid="a"),
            _Row(rid="b", state=None),
            _Row(rid="c"),
            _Row(rid="d", city=""),
        ]
        ok, bad = _partition_submittable(rows)
        assert [r.id for r in ok] == ["a", "c"]
        assert [r.id for r in bad] == ["b", "d"]

    def test_partition_of_all_valid_leaves_nothing_to_fail(self):
        ok, bad = _partition_submittable([_Row(rid="a"), _Row(rid="b")])
        assert len(ok) == 2 and bad == []

    def test_partition_of_all_invalid_yields_no_batch(self):
        ok, bad = _partition_submittable([_Row(rid="a", state=None)])
        assert ok == [] and len(bad) == 1
