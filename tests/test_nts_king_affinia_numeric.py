"""REAL King (Queen Anne & Magnolia News) Affinia notices the crawler discarded whole.

Affinia Default Services prints the sale LOCATION *after* the verb with a NUMERIC date:

    "...the undersigned Trustee will on 08/14/2026, at 10:00 AM sell at public
     auction located at the 4th Avenue Entrance of the King County Administration..."

_AUCTION needs a non-empty location BETWEEN the time and the verb; _AUCTION_KING allows
the location after but only for a MONTH-NAME date; _AUCTION_WORDED needs "Nth day of
<Month> ... o'clock". All three missed, so auction_date was None and is_valid_nts
discarded the notice — silently, counted only as `skipped`. Measured against the live
PDFs on 2026-09-03: 3 of 5 notices lost on the 08-05-26 issue and 2 of 2 (100%) on the
then-current 09-02-26 issue, i.e. every Affinia sale in King County.

Fixtures are the verbatim split blocks from those two published PDFs.
"""
import time
from datetime import date
from pathlib import Path

from src.scrapers.sources.nts_king_pdf import parse_king_notice
from src.scrapers.sources.nts_tacoma_index import (
    _AUCTION,
    _AUCTION_NUM_LOC_AFTER,
    is_valid_nts,
    notice_to_row,
    parse_nts_notice,
)

_FX = Path(__file__).parent / "fixtures"


def _parse_king(name: str) -> dict:
    return parse_king_notice((_FX / name).read_text(encoding="utf-8"))


def _row(parsed: dict, today: date) -> dict | None:
    return notice_to_row(
        parsed,
        source_url="https://pacificpublishingcompany.media.clients.ellingtoncms.com/x.pdf",
        today=today,
        source="queen_anne_news",
        county="king",
    )


# ── The three real notices, end to end ────────────────────────────────────────────

def test_affinia_numeric_addai_recovered():
    p = _parse_king("nts_king_affinia_numeric_addai.txt")
    assert p["auction_date"] == "08/14/2026"
    assert p["auction_time"] == "10:00 AM"
    assert p["parcel"] == "192105-9193"
    assert p["ts_number"] == "REF-20240305000737"
    assert is_valid_nts(p)
    row = _row(p, date(2026, 8, 6))
    assert row is not None
    assert row["auction_date"] == date(2026, 8, 14)
    # Section IV "sum owing ... Principal $512,000.00" — NOT the larger
    # "total debt now owing in the amount of $603,776.44" (interest + fees).
    assert str(row["principal_owing"]) == "512000.00"
    assert row["county"] == "king"


def test_affinia_numeric_walker_recovered():
    p = _parse_king("nts_king_affinia_numeric_walker.txt")
    assert p["auction_date"] == "09/04/2026"
    assert p["auction_time"] == "9:00 AM"
    assert p["parcel"] == "327692-0130-03"
    assert is_valid_nts(p)
    row = _row(p, date(2026, 8, 6))
    assert row is not None
    assert row["auction_date"] == date(2026, 9, 4)
    assert str(row["principal_owing"]) == "679597.29"


def test_affinia_numeric_lutes_recovered():
    p = _parse_king("nts_king_affinia_numeric_lutes.txt")
    assert p["auction_date"] == "10/02/2026"
    assert p["auction_time"] == "10:00 AM"
    assert p["parcel"] == "2898600015"
    assert is_valid_nts(p)
    row = _row(p, date(2026, 9, 3))
    assert row is not None
    assert row["auction_date"] == date(2026, 10, 2)
    assert str(row["principal_owing"]) == "414780.74"


def test_location_after_the_verb_is_captured():
    """Location is display-only, but it must not be left empty or swallow the notice."""
    p = _parse_king("nts_king_affinia_numeric_addai.txt")
    loc = p["auction_location"]
    assert loc and "4th Avenue Entrance" in loc
    assert "NOTICE OF TRUSTEE" not in loc.upper()
    assert len(loc) <= 300


def test_location_never_swallows_the_auction_verb():
    """The location-after match ends AT the verb, so the search window starts before it.

    A capture that begins "sell at public auction located at ..." is the venue plus the
    sentence that introduced it — rejected outright rather than shown to the user (Codex).
    """
    for name in ("nts_king_affinia_numeric_addai.txt",
                 "nts_king_affinia_numeric_walker.txt",
                 "nts_king_affinia_numeric_lutes.txt"):
        loc = _parse_king(name)["auction_location"]
        if loc:
            assert "SELL AT PUBLIC AUCTION" not in loc.upper()
            assert "NOTICE OF TRUSTEE" not in loc.upper()


def test_trustee_comma_will_appositive_still_matches():
    """"the undersigned Trustee, will on ..." is real in these papers (Codex)."""
    text = ("I. NOTICE IS HEREBY GIVEN that the undersigned Trustee, will on 08/14/2026, "
            "at 10:00 AM sell at public auction located at the 4th Avenue Entrance")
    p = parse_nts_notice(text)
    assert p["auction_date"] == "08/14/2026"
    assert p["auction_time"] == "10:00 AM"


# ── Postponement: the printed sale date is stale the moment it is published ───────

def test_inline_postponement_supersedes_the_original_sale_date():
    """REAL notice: "on June 26, 2026, 09:00 AM***THE SALE WAS POSTPONED TO 09/18/2026***".

    Reading the original date buried a still-upcoming King sale as "past": is_active
    flipped False and the lead vanished from the product. This is TS WA05000073-24-2,
    whose sale is genuinely on 09/18/2026.
    """
    p = _parse_king("nts_king_postponed_guiler.txt")
    assert p["ts_number"] == "WA05000073-24-2"
    assert p["auction_date"] == "09/18/2026"
    assert p["auction_time"] == "9:00AM"
    row = _row(p, date(2026, 9, 3))
    assert row is not None
    assert row["auction_date"] == date(2026, 9, 18)
    assert row["is_active"] is True          # the whole point: still a live lead
    assert str(row["principal_owing"]) == "155361.99"
    assert row["parcel"] == "6385500350"


def test_postponement_only_ever_moves_the_date_forward():
    """A postponement cannot go backwards, so a stray earlier date must not win."""
    backwards = ("the undersigned Trustee will on 09/18/2026, at 9:00 AM sell at public "
                 "auction. The sale was postponed to 06/26/2026 @ 9:00AM")
    p = parse_nts_notice(backwards)
    assert p["auction_date"] == "09/18/2026"

    forwards = ("the undersigned Trustee will on 06/26/2026, at 9:00 AM sell at public "
                "auction. The sale was postponed to 09/18/2026 @ 9:00AM")
    p = parse_nts_notice(forwards)
    assert p["auction_date"] == "09/18/2026"


def test_postponement_does_not_fire_without_a_parsed_sale_date():
    """No sale sentence => nothing to supersede; the notice still fails validation."""
    p = parse_nts_notice("The sale was postponed to 09/18/2026 @ 9:00AM")
    assert p["auction_date"] is None
    assert not is_valid_nts(p)


# ── The new pattern must stay strictly additive ───────────────────────────────────

def test_new_pattern_does_not_fire_on_location_before_layout():
    """A location-BEFORE notice is _AUCTION's job; the new fallback must not match it.

    The fallback requires the verb to follow the time immediately, so a layout with a
    location in between can never reach it — _AUCTION keeps owning those notices.
    """
    text = ("the undersigned Trustee will on 08/14/2026, at 10:00 AM at the 4th Avenue "
            "Entrance of the King County Administration Building sell at public auction")
    assert _AUCTION.search(text) is not None
    assert _AUCTION_NUM_LOC_AFTER.search(text) is None


def test_new_pattern_does_not_bind_a_recording_or_publication_date():
    """Numeric dates are everywhere in these notices; only the sale sentence may match.

    A deed recording / "Interest Paid To" / publication date is never immediately
    followed by "sell at public auction", and none hangs off "Trustee will".
    """
    for text in (
        "Deed of Trust recorded on 03/05/2024, at 10:00 AM sell at public auction",
        "Interest Paid To: 04/01/2025, at 10:00 AM. The Trustee will sell at public auction",
        "Published on 08/05/2026, at 10:00 AM in the Queen Anne & Magnolia News",
    ):
        assert _AUCTION_NUM_LOC_AFTER.search(text) is None


def test_existing_month_name_and_worded_layouts_unchanged():
    """The three pre-existing shapes still parse exactly as before (no preemption)."""
    mtc = ("I. NOTICE IS HEREBY GIVEN that on September 4, 2026, 09:00 AM, Main Entrance, "
           "King County Administration Building, 500 4th Avenue, Seattle, WA 98104, MTC "
           "Financial Inc. dba Trustee Corps, the undersigned Trustee, will sell at public "
           "auction to the highest and best bidder")
    p = parse_nts_notice(mtc)
    assert p["auction_date"] == "September 4, 2026"
    assert p["auction_time"] == "09:00 AM"

    worded = ("the undersigned Trustee will on the 17th day of July, 2026, at the hour of "
              "10:00 o'clock AM at the county courthouse steps sell at public auction")
    p = parse_nts_notice(worded)
    assert p["auction_date"] == "July 17, 2026"
    assert p["auction_time"] == "10:00 AM"

    numeric_loc_before = ("the undersigned Trustee will on 9/25/2026, at 10:00 AM at the "
                          "Pierce County Courthouse steps sell at public auction")
    p = parse_nts_notice(numeric_loc_before)
    assert p["auction_date"] == "9/25/2026"
    assert p["auction_time"] == "10:00 AM"
    assert p["auction_location"] == "the Pierce County Courthouse steps"


# ── Backtracking safety (a prior regex change in this file ran >120s) ─────────────

def test_no_catastrophic_backtracking_on_a_large_real_block():
    """The Walker fixture is a real 44k-char block; parsing must stay far under a second."""
    text = (_FX / "nts_king_affinia_numeric_walker.txt").read_text(encoding="utf-8")
    assert len(text) > 40_000
    start = time.perf_counter()
    parse_king_notice(text)
    assert time.perf_counter() - start < 2.0


def test_no_catastrophic_backtracking_on_adversarial_non_matching_text():
    """Many 'Trustee will on <date>, at <time>' near-misses that never reach the verb."""
    text = ("the undersigned Trustee will on 08/14/2026, at 10:00 AM " + "x " * 400) * 120
    assert len(text) > 100_000
    start = time.perf_counter()
    assert _AUCTION_NUM_LOC_AFTER.search(text) is None
    assert time.perf_counter() - start < 2.0


# ── The drop must never be silent again ───────────────────────────────────────────

def test_undated_real_notice_is_counted_separately_from_chrome():
    """A block with an identity but no auction date is a lost lead, not chrome."""
    from src.workers.nts_crawler import _note_undated_drop

    summary: dict = {}
    # A real trustee sale whose date shape the parser could not read.
    assert _note_undated_drop(summary, {"ts_number": "REF-20240305000737"}, "queen_anne_news") is True
    assert summary["dropped_undated"] == 1

    # Chrome / a non-NTS legal notice — no identity, must stay quiet.
    assert _note_undated_drop(summary, {"ts_number": None, "grantor": None}, "queen_anne_news") is False
    # A fully parsed notice is not a drop at all.
    assert _note_undated_drop(
        summary, {"ts_number": "WA05000073-24-2", "auction_date": "08/14/2026"}, "queen_anne_news"
    ) is False
    assert summary["dropped_undated"] == 1


def test_the_three_recovered_notices_would_have_tripped_the_drop_counter():
    """Pins the regression: pre-fix, each of these was an undated real notice."""
    from src.workers.nts_crawler import _note_undated_drop

    summary: dict = {}
    for name in ("nts_king_affinia_numeric_addai.txt",
                 "nts_king_affinia_numeric_walker.txt",
                 "nts_king_affinia_numeric_lutes.txt"):
        p = _parse_king(name)
        # They parse now, so they are NOT drops...
        assert _note_undated_drop(summary, p, "queen_anne_news") is False
        # ...but the same notice minus its auction date is exactly what used to be lost.
        assert _note_undated_drop(
            summary, {**p, "auction_date": None}, "queen_anne_news"
        ) is True
    assert summary["dropped_undated"] == 3


def test_unparseable_date_is_also_a_visible_drop():
    """A captured-but-unconvertible date fails validation the same way (Codex)."""
    from src.workers.nts_crawler import _note_undated_drop

    summary: dict = {}
    assert _note_undated_drop(
        summary, {"ts_number": "WA05000073-24-2", "auction_date": "13/40/2026"}, "queen_anne_news"
    ) is True
    assert _note_undated_drop(
        summary, {"ts_number": "WA05000073-24-2", "auction_date": "09/0S/2026"}, "queen_anne_news"
    ) is True
    assert summary["dropped_undated"] == 2


# ── Postponement override must stay narrow (Codex round 2) ───────────────────────

def test_unrelated_continuance_language_never_overrides_the_sale():
    """Litigation / mediation / hearing continuances share the verb but not the sale."""
    for text in (
        ("the undersigned Trustee will on 07/10/2026, at 10:00 AM at the courthouse sell "
         "at public auction. Borrower filed a related court action. The motion hearing "
         "was CONTINUED TO 09/18/2026 at 9:00AM."),
        ("the undersigned Trustee will on 07/10/2026, at 10:00 AM at the courthouse sell "
         "at public auction. The foreclosure mediation conference was RESCHEDULED TO "
         "09/18/2026."),
    ):
        p = parse_nts_notice(text)
        assert p["auction_date"] == "07/10/2026", p["auction_date"]


def test_postponement_far_from_the_sale_sentence_is_ignored():
    """Only the sale sentence's own inline marker counts; the window is bounded."""
    text = ("the undersigned Trustee will on 07/10/2026, at 10:00 AM at the courthouse "
            "sell at public auction. " + ("filler text. " * 60) +
            "The sale was postponed to 09/18/2026.")
    p = parse_nts_notice(text)
    assert p["auction_date"] == "07/10/2026"


def test_same_day_continuance_does_not_rewrite_the_time():
    """Strictly later, so a same-day hearing time can't become the auction time."""
    text = ("the undersigned Trustee will on 09/18/2026, at 10:00 AM at the courthouse "
            "sell at public auction. The sale was continued to 09/18/2026 at 1:30 PM.")
    p = parse_nts_notice(text)
    assert p["auction_date"] == "09/18/2026"
    assert p["auction_time"] == "10:00 AM"


def test_postponement_never_rescues_an_unparseable_original_date():
    """An original that cannot convert must stay a visible parse failure, not a live row."""
    from src.scrapers.sources.nts_tacoma_index import _POSTPONED, _to_date

    assert _to_date("13/40/2026") is None
    m = _POSTPONED.search("The sale was postponed to 09/18/2026 @ 9:00AM")
    assert m is not None and _to_date(m.group(1)) is not None
    # The guard requires a convertible original, so the rescue cannot fire.
    text = ("the undersigned Trustee will on 13/40/2026, at 10:00 AM at the courthouse "
            "sell at public auction. The sale was postponed to 09/18/2026 @ 9:00AM.")
    p = parse_nts_notice(text)
    assert _to_date(p["auction_date"]) is None
    assert notice_to_row(
        p, source_url="x", today=date(2026, 9, 3), source="queen_anne_news", county="king"
    ) is None
