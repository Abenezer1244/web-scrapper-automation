"""The scrape window is applied FORWARD for trustee_sale, by length.

nts_notices holds UPCOMING sales and a lead's date_recorded is its FUTURE auction date,
so the window's absolute dates are always in the past. Applying them literally matches
nothing; discarding them outright (the old behavior) meant a job asking for
06/04/2026..09/02/2026 delivered a row dated 9/4/2026 and the control the user set did
nothing at all. Keeping the user's INTENT (how much data) while flipping the DIRECTION
is what makes the number mean something — and it puts date_recorded back inside the
requested window instead of provably outside it.
"""
from datetime import date
from pathlib import Path

from src.scrapers.trustee_sale import _parse_mdy, _window_span_days


def test_span_is_the_length_of_the_requested_window():
    # The real Test 6 window: 06/04/2026..09/02/2026 = 90 days.
    assert _window_span_days("06/04/2026", "09/02/2026") == 90
    assert _window_span_days("6/4/2026", "9/2/2026") == 90       # unpadded is the same window
    assert _window_span_days("08/03/2026", "09/02/2026") == 30


def test_absent_or_unparseable_window_means_no_horizon():
    """None => every upcoming auction. A malformed window must never shrink results."""
    for a, b in (
        (None, "09/02/2026"),
        ("06/04/2026", None),
        (None, None),
        ("", ""),
        ("2026-06-04", "2026-09-02"),   # ISO is not the stored format
        ("garbage", "09/02/2026"),
        ("13/45/2026", "09/02/2026"),   # matches the shape, not a real date
    ):
        assert _window_span_days(a, b) is None, (a, b)


def test_same_day_or_inverted_window_falls_back_to_all_upcoming():
    """Neither honored literally nor clamped to a floor — both would lose leads.

    Honoring a same-day window delivers only auctions happening today; clamping it to
    some floor silently becomes "next week only" when a saved range is malformed or the
    date helper regresses (Codex). None = legacy all-upcoming is the only option that
    cannot lose a lead.
    """
    assert _window_span_days("09/02/2026", "09/02/2026") is None
    assert _window_span_days("09/02/2026", "06/04/2026") is None


def test_parse_mdy_round_trip():
    assert _parse_mdy("06/04/2026") == date(2026, 6, 4)
    assert _parse_mdy("6/4/2026") == date(2026, 6, 4)
    assert _parse_mdy(" 09/02/2026 ") == date(2026, 9, 2)
    assert _parse_mdy("2026/09/02") is None
    assert _parse_mdy("02/30/2026") is None          # shape ok, not a real date
    assert _parse_mdy(None) is None


def test_the_test_6_window_would_now_cover_its_own_result():
    """Regression on the exact complaint: a 9/4/2026 auction in a 06/04..09/02 job.

    Under the forward reading, that 90-day window runs from the run date, so the
    9/4/2026 sale is inside it rather than 2 days past its far edge.
    """
    span = _window_span_days("06/04/2026", "09/02/2026")
    run_day = date(2026, 9, 3)
    horizon = date.fromordinal(run_day.toordinal() + span)
    assert run_day <= date(2026, 9, 4) <= horizon


def test_scraper_uses_the_county_local_auction_clock():
    """A WA sale must not read as past because UTC already rolled over."""
    import inspect

    from src.scrapers import trustee_sale

    src = inspect.getsource(trustee_sale._TrusteeSaleScraper.scrape)
    assert "auction_reference_date()" in src
    assert "date.today()" not in src


def test_horizon_exclusions_are_logged_not_silent():
    """A narrower window may legitimately exclude auctions — but never silently.

    Measured 2026-09-03: every trustee_sale config runs rolling_90 and the furthest
    auction anywhere is 29 days out, so nothing is excluded today. A shorter window on a
    longer-dated county would exclude, and the operator has to be able to see it (Codex).
    """
    import inspect

    from src.scrapers import trustee_sale

    src = inspect.getsource(trustee_sale._TrusteeSaleScraper.scrape)
    assert "beyond" in src
    assert "fall BEYOND" in src


def test_backfill_cap_aborts_rather_than_truncating():
    """Applying an oldest-first PREFIX would let a stale issue win the upsert."""
    import re

    script = (Path(__file__).resolve().parents[1]
              / "scripts" / "backfill_nts_pdf_archive.py").read_text(encoding="utf-8")
    # The probe loop must NOT be bounded by the cap...
    assert re.search(r"while d <= today:\s*$", script, re.M)
    assert "len(issues) < _MAX_ISSUES" not in script
    # ...the cap is enforced afterwards, as an abort.
    assert "exceeds the {_MAX_ISSUES}-issue cap" in script


def test_backfill_uses_the_same_county_local_clock_as_the_scraper():
    script = (Path(__file__).resolve().parents[1]
              / "scripts" / "backfill_nts_pdf_archive.py").read_text(encoding="utf-8")
    assert "today = auction_reference_date()" in script


def test_backfill_row_failure_does_not_abort_the_run():
    """Newer issues are what repair stale reposts — they must still get to write."""
    script = (Path(__file__).resolve().parents[1]
              / "scripts" / "backfill_nts_pdf_archive.py").read_text(encoding="utf-8")
    assert "UPSERT FAILED" in script
    assert "except Exception as exc:  # noqa: BLE001\n                        errored += 1" in script
