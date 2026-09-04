"""The two behaviours the 2026-09-04 "Test 8" audit added, pinned against the SOURCE
structure rather than against Test 8's own values.

Test 8 (King, pre_foreclosure, 155 leads) showed a blank Auction Date and Default Owed
on every row. Three separate causes were confirmed:

  1. The weekly crawl only ever read the CURRENT issue, so four consecutive missed weeks
     were lost permanently — 22 of the 36 notices published in the window were never
     cached, even though every back issue was fetchable by derived URL.
  2. A lead whose sale had already run got nothing at all, because the matcher only
     considered future-dated notices — a blank indistinguishable from "no notice
     exists", when the real date and amount were sitting in the cache.
  3. Batch/segment CSVs dropped auction_date and default_amount outright.

(A fourth — 154 of the 155 genuinely have no notice in King's only wired source — is a
coverage ceiling, not a defect, and is deliberately NOT "fixed" anywhere.)
"""
from datetime import date, timedelta

import pytest

# ── 1. The archive sweep ─────────────────────────────────────────────────────────


def test_sweep_orders_issues_oldest_first():
    """_upsert_notice refreshes every mutable field ON CONFLICT, and a republished
    notice can legitimately change between issues ("SALE POSTPONED TO ..."). Newest
    must therefore write LAST, or the sweep reverts a postponed sale to its old date."""
    from src.scrapers.sources.nts_pdf_archive import candidate_urls

    groups = candidate_urls("queen_anne_news", date(2026, 9, 2), weeks=4)
    names = [g[0].rsplit("/", 1)[-1] for g in groups]
    assert names == [
        "QA%20Legals%2008-12-26.pdf",
        "QA%20Legals%2008-19-26.pdf",
        "QA%20Legals%2008-26-26.pdf",
        "QA%20Legals%2009-02-26.pdf",
    ]


def test_sweep_urls_stay_on_the_cdn_host_and_the_papers_own_prefix():
    """The sweep builds URLs instead of following links, so nothing else may validate
    them for us — an off-host or off-prefix URL would be an SSRF-shaped own goal."""
    from src.scrapers.sources.nts_pdf_archive import (
        ARCHIVE_SOURCES,
        PDF_HOST,
        candidate_urls,
    )

    for source, cfg in ARCHIVE_SOURCES.items():
        for group in candidate_urls(source, date(2026, 9, 2), weeks=8):
            for url in group:
                assert url.startswith(f"https://{PDF_HOST}{cfg['prefix']}")
                assert " " not in url, "issue filenames contain spaces; must be encoded"


def test_sweep_reads_an_issue_again_when_its_rows_are_stale():
    """"Has a row" != "fully ingested": the 2026-08-05 King issue held 2 of its 5
    notices because the old splitter dropped the rest. A skip-if-any-row rule would
    have stranded those three forever, so staleness — not presence — gates the re-read.
    """
    import inspect

    from src.scrapers.sources.nts_pdf_archive import candidate_urls
    from src.workers import nts_crawler as c

    src = inspect.getsource(c._sweep_pdf_archive)
    assert "_ARCHIVE_REFRESH_DAYS" in src and "max(seen) < stale_before" in src, (
        "the sweep must gate on staleness, not on whether an issue has any row"
    )

    per_issue = candidate_urls("queen_anne_news", date(2026, 9, 2), c._ARCHIVE_WEEKS)
    assert len(per_issue) == c._ARCHIVE_WEEKS
    assert c._ARCHIVE_REFRESH_DAYS >= 1
    # The budget must be small enough that a daily beat stays polite but large enough
    # to work through a multi-week outage in a few days.
    assert 1 <= c._ARCHIVE_MAX_FETCH <= c._ARCHIVE_WEEKS


def test_both_pacific_papers_get_the_sweep():
    """King is what the audit measured, but Snohomish runs the identical pipeline off
    the identical CMS and had the identical single-point-of-failure."""
    import inspect

    from src.workers import nts_crawler as c

    for fn in (c.crawl_nts_king_queenanne, c.crawl_nts_snoho_tribune):
        src = inspect.getsource(fn)
        assert "archive=True" in src, f"{fn.__name__} does not enable the archive sweep"


# ── 2. Past auctions ─────────────────────────────────────────────────────────────


def _notice(**kw):
    base = {
        "id": "n1", "parcel": "1112630120", "property_address": "36423 10TH CT SW",
        "property_address_normalized": "36423 10TH CT SW|98023",
        "grantor": "MYONG HEE KIM", "auction_date": date(2026, 8, 7),
        "auction_time": None, "auction_location": None, "trustee": None,
        "beneficiary": None, "ts_number": "WA09000059-24-1",
        "principal_owing": 397621.29, "source": "queen_anne_news", "source_url": "u",
    }
    base.update(kw)
    return base


class _FakeResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _CapturingDB:
    """Records the params every _write_match UPDATE is issued with."""

    def __init__(self):
        self.calls = []

    def execute(self, _stmt, params=None):
        self.calls.append(params or {})
        return _FakeResult(1)


def test_live_notice_may_replace_an_already_past_attachment():
    """A postponed or re-noticed sale must be able to overwrite the stale date we
    attached. Without this the lead is frozen on a sale that no longer happens —
    strictly worse than the blank it replaced."""
    from src.workers.nts_matcher_task import _write_match

    db = _CapturingDB()
    _write_match(db, "r1", _notice(), 0.95, live=True, today=date(2026, 9, 4))
    assert db.calls[0]["live"] is True
    assert db.calls[0]["today"] == date(2026, 9, 4)


def test_historical_notice_never_displaces_a_live_sale():
    """The past pass may claim an unset row or one holding a STRICTLY OLDER past sale;
    it can never touch a row holding a live (future) one."""
    from src.workers.nts_matcher_task import _write_match

    db = _CapturingDB()
    _write_match(db, "r1", _notice(), 0.95, live=False, today=date(2026, 9, 4))
    assert db.calls[0]["live"] is False
    assert db.calls[0]["auction_date"] == date(2026, 8, 7)


def test_write_match_guard_encodes_every_claim_rule():
    """The SQL itself must carry the rules — a caller-side check would be bypassed by
    the other entry point (beat vs inline)."""
    import inspect

    from src.workers import nts_matcher_task as m

    sql = inspect.getsource(m._write_match)
    assert "auction_date IS NULL" in sql
    assert "(:live AND auction_date < :today)" in sql          # live supersedes past
    assert "NOT :live AND auction_date < :today" in sql        # past-only branch
    assert "auction_date < :auction_date" in sql               # ...and only if newer


def test_candidate_queries_keep_past_attached_leads_eligible():
    """A lead carrying a historical sale must STAY a candidate, or the live re-notice
    that supersedes it could never be found."""
    import inspect

    from src.workers import nts_matcher_task as m

    for fn in (m.match_nts_notices, m.match_job_inline):
        src = inspect.getsource(fn)
        assert "auction_date IS NULL OR" in src and "auction_date < :today" in src, (
            f"{fn.__name__} still selects only auction_date IS NULL"
        )


def test_historical_lookback_is_bounded_and_staleness_uses_fetched_at():
    """is_active cannot filter stale past sales — the expiry pass flips it false for
    EVERY notice the day its auction passes — so fetched_at has to do that job."""
    import inspect

    from src.workers import nts_matcher_task as m

    assert 0 < m._PAST_AUCTION_DAYS <= 365
    src = inspect.getsource(m._match_and_write)
    assert "fetched_at" in src, "past pass must filter staleness on fetched_at"
    assert "auction_date < :today" in src


def test_live_notices_are_offered_before_past_ones():
    """used_result_ids gives the first notice to claim a lead the win, so ordering IS
    the priority rule."""
    import inspect

    from src.workers import nts_matcher_task as m

    src = inspect.getsource(m._match_and_write)
    assert "list(live) + list(past)" in src


# ── 3. Combined/segment CSV columns ──────────────────────────────────────────────


def test_overlap_csv_carries_the_auction_columns():
    """batch_export.py SELECTs r.auction_date / r.default_amount, but the overlap
    column list dropped them, so a batch- or segment-delivered lead silently lost both
    while the per-job CSV kept them."""
    from src.utils.lead_export import OVERLAP_LEAD_COLUMNS

    for col in ("auction_date", "days_to_auction", "default_amount"):
        assert col in OVERLAP_LEAD_COLUMNS


def test_auction_columns_are_appended_not_inserted():
    """Column order is a compatibility contract for anyone consuming these CSVs."""
    from src.utils.lead_export import OVERLAP_LEAD_COLUMNS

    assert OVERLAP_LEAD_COLUMNS[-3:] == [
        "auction_date", "days_to_auction", "default_amount",
    ]


@pytest.mark.parametrize("record_type", ["pre_foreclosure", "trustee_sale"])
def test_overlap_row_actually_emits_the_values(record_type):
    """The column list alone proves nothing — build_overlap_export_row copies from the
    canonical row, so the keys have to line up there too."""
    from types import SimpleNamespace

    from src.utils.lead_export import build_overlap_export_row

    rec = SimpleNamespace(
        party_name="KIM MYONG HEE", parcel_id="1112630120",
        property_address="36423 10TH CT SW", mailing_address=None,
        date_recorded="06/11/2026", doc_type="NOTICE OF TRUSTEE SALE",
        auction_date=date(2026, 8, 7), default_amount=397621.29,
        heirs=None, legal_description=None, phone=None, email=None,
        enrichment_data={}, record_type=record_type,
    )
    row = build_overlap_export_row(
        rec, {"lists_count": 1, "lists": "", "counties": "king"},
        today=date(2026, 9, 4), auction_today=date(2026, 9, 4),
    )
    assert row["auction_date"] == "2026-08-07"
    assert row["default_amount"] == "397621.29"
    # already past on the reference date -> a NEGATIVE countdown, never clamped to 0
    assert int(row["days_to_auction"]) < 0


def test_days_to_auction_stays_signed_for_a_past_sale():
    """Clamping a past sale to 0 would render it as "Today" — the exact bug the signed
    value was introduced to kill, and now reachable on far more rows."""
    from src.utils.lead_signals import derive_signals

    sig = derive_signals(
        {"auction_date": date(2026, 8, 7)}, date(2026, 9, 4),
        auction_today=date(2026, 9, 4)
    )
    assert sig["days_to_auction"] == -(date(2026, 9, 4) - date(2026, 8, 7)).days
    assert sig["days_to_auction"] < 0


def test_upcoming_sale_still_counts_down_positively():
    from src.utils.lead_signals import derive_signals

    today = date(2026, 9, 4)
    sig = derive_signals(
        {"auction_date": today + timedelta(days=14)}, today, auction_today=today
    )
    assert sig["days_to_auction"] == 14


# ── 4. Fixes from the Codex review of this change ────────────────────────────────


def test_historical_notice_may_supersede_a_strictly_older_past_sale():
    """Pass ordering only holds WITHIN one run, and the sweep is capped, so a multi-week
    catch-up can attach an old sale on Monday and only see the newer re-notice on
    Tuesday. The guard must therefore allow past->newer-past, never past->older-past."""
    import inspect

    from src.workers import nts_matcher_task as m

    sql = inspect.getsource(m._write_match)
    assert "NOT :live AND auction_date < :today" in sql
    assert "auction_date < :auction_date" in sql, (
        "a historical notice must only replace a STRICTLY OLDER past attachment"
    )


def test_live_notice_selection_is_deterministic():
    """Two live notices could target one lead; without ORDER BY the winner depended on
    whatever order Postgres returned."""
    import inspect

    from src.workers import nts_matcher_task as m

    assert "ORDER BY auction_date ASC, id" in inspect.getsource(m._match_and_write)


def test_barren_alert_ignores_archive_recoveries():
    """A healthy back-catalogue must not hide today's issue failing to parse — that is
    precisely the layout-drift failure the alert exists for."""
    import inspect

    from src.workers import nts_crawler as c

    src = inspect.getsource(c._crawl_pacific_publishing_pdf)
    assert "upserted=current_upserted" in src
    assert 'upserted=summary["upserted"]' not in src


def test_repair_results_is_scoped_to_its_own_source():
    """Parcels are not globally unique and normalizing widens the collision surface, so
    the rewrite must join through nts_notice_id to this paper's notices."""
    import inspect
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "scripts" / "repair_nts_ts_number.py"
    text = src.read_text(encoding="utf-8")
    assert "JOIN nts_notices n ON n.id = r.nts_notice_id" in text
    assert "n.source = :src" in text
    del inspect


def test_field_rewrites_require_the_deploy_confirmation():
    """--fields rewrites product-facing auction data from a LOCAL re-parse; running it
    against a differently-versioned deployed parser is the damage class this repairs."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "scripts"
            / "repair_nts_ts_number.py").read_text(encoding="utf-8")
    assert "args.notices or args.retire_wrong_key or args.fields" in text


def test_archive_sweep_also_probes_a_slipped_issue_day():
    """The Snohomish Tribune has dated an issue a day late (2026-08-27, a Thursday); a
    slipped issue inside a missed week would otherwise be unreachable."""
    from src.scrapers.sources.nts_pdf_archive import candidate_urls

    groups = candidate_urls("snohomish_tribune", date(2026, 9, 2), weeks=2)
    aug26 = groups[0]
    assert any("8-26-26" in u for u in aug26), "nominal Wednesday must be probed"
    assert any("8-27-26" in u for u in aug26), "slipped Thursday must also be probed"
    # nominal day is tried FIRST
    assert aug26[0].endswith("8-26-26.pdf") or "8-26-26" in aug26[0]


# ── 5. Pointer repair after an archive backfill ──────────────────────────────────


def test_repair_repoints_nts_notice_id_when_it_names_another_parcel():
    """`nts_notices` is upserted ON CONFLICT (source, ts_number). The archive backfill
    therefore REPLACES the content of a row that was squatting on the wrong TS number
    with the real notice for that key — leaving any lead pointing at it aimed at a
    stranger's parcel. Measured on King 2026-09-04: 1 of 4 matched leads, immediately
    after the backfill. The lead's own auction_date/default_amount are unaffected."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "scripts"
            / "repair_nts_ts_number.py").read_text(encoding="utf-8")
    assert "nts_notice_id = CAST(:nid AS uuid)" in text
    assert "notice_by_ts" in text, "must resolve the correct notice by its ts_number"


def test_repoint_is_decided_before_the_ts_only_skip():
    """A row can hold the RIGHT ts_number and still point at the wrong parcel; skipping
    on the ts alone would leave that unrepaired forever."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "scripts"
            / "repair_nts_ts_number.py").read_text(encoding="utf-8")
    skip = text.index("if stored == correct and not top_wrong")
    decide = text.index("repoint = (")
    assert decide < skip, "repoint must be computed BEFORE the skip"
    assert "not repoint" in text[skip:skip + 120], "the skip must account for repoint"


def test_repoint_never_guesses_a_target():
    """Only ever repoints to the notice whose ts_number is the one this parcel's own
    issue prints, and only when that notice exists."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "scripts"
            / "repair_nts_ts_number.py").read_text(encoding="utf-8")
    assert "want_id = notice_by_ts.get(correct)" in text
    assert "want_id is not None and want_id != m[\"notice_id\"]" in text
