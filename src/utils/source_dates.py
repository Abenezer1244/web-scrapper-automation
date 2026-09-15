"""Reading county source dates, and telling a real notice date from a stand-in.

Two scrapers write the lead's AUCTION date into ``date_recorded`` when a notice has
no notice (mailing) date of its own:
  - trustee_sale: ``nod_date`` when it is M/D/YYYY, else the notice's auction date
    as M/D/YYYY, with the same auction date kept in
    ``enrichment_data["nts_source"]["auction_date"]`` (ISO);
  - Snohomish pre_foreclosure: ``nod_date`` or else the raw auction date string
    ("9/18/2026" or "September 18, 2026"), kept in ``enrichment_data["auction_date"]``.

That stand-in is not a notice date. Owner decision (2026-09-15): the Date column never
shows it; it reads as blank and sorts with the undated rows. The stored text stays
exactly as written, because ``date_recorded`` feeds ``dedup_hash`` and
``source_fingerprint``.

The comparison is against the auction date the SCRAPER used, not
``results.auction_date``: the NTS matcher moves that column when a sale is postponed,
which would turn an old stand-in back into a "notice date". Only those two scrapers
write the origin keys, and nothing rewrites them.

``src/api/results_sort.py`` holds the SQL twin of ``is_auction_date_fallback``; the
two must agree (tests/test_notice_date.py checks both on one set of inputs).
"""
import re
from datetime import date
from typing import Any

# [0-9], never a digit class: the SQL twin must accept exactly the same text, and in
# PostgreSQL a locale-widened digit class would reach an integer cast and raise.
_NUMERIC = re.compile(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})")
_MONTH_NAME = re.compile(r"([A-Za-z]+)[.]? +([0-9]{1,2}),? +([0-9]{4})")
_ISO = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
MONTH_NUMBERS: dict[str, int] = {
    **{name: i for i, name in enumerate(_MONTHS, start=1)},
    **{name[:3]: i for i, name in enumerate(_MONTHS, start=1)},
    "sept": 9,
}


def _real_date(year: int, month: int | None, day: int) -> date | None:
    try:
        return date(year, month, day) if month is not None else None
    except ValueError:
        return None


def parse_source_date(value: Any) -> date | None:
    """A date from "M/D/YYYY" or "Month D, YYYY" text, else None. No lenient parsing."""
    if not isinstance(value, str):
        return None
    value = value.strip(" ")  # PostgreSQL btrim: spaces only
    if m := _NUMERIC.fullmatch(value):
        return _real_date(int(m[3]), int(m[1]), int(m[2]))
    if m := _MONTH_NAME.fullmatch(value):
        return _real_date(int(m[3]), MONTH_NUMBERS.get(m[1].lower()), int(m[2]))
    return None


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not (m := _ISO.fullmatch(value.strip(" "))):
        return None
    return _real_date(int(m[1]), int(m[2]), int(m[3]))


# enrichment_data["source"] the Snohomish pre_foreclosure scraper stamps on its rows
# (src/scrapers/snohomish_wa_pre_foreclosure._SOURCE; a test keeps them equal).
SNOHOMISH_TRIBUNE_SOURCE = "snohomish_tribune"


def origin_auction_date(enrichment_data: Any) -> date | None:
    """The auction date the scraper itself recorded for this row, else None.

    Scoped by the provenance each scraper stamps, never by a key name alone (Codex):
      - an ``nts_source`` object is the trustee_sale scraper's contract: only its ISO
        auction_date counts. Snohomish trustee sales also carry the tribune ``source``
        at the top level, so this is checked first;
      - otherwise only a row stamped ``source == "snohomish_tribune"`` (the Snohomish
        pre_foreclosure scraper) reads the top-level auction_date;
      - anything else has no origin, so nothing can hide its date.
    """
    if not isinstance(enrichment_data, dict):
        return None
    nts_source = enrichment_data.get("nts_source")
    if isinstance(nts_source, dict):
        return _parse_iso_date(nts_source.get("auction_date"))
    if enrichment_data.get("source") == SNOHOMISH_TRIBUNE_SOURCE:
        return parse_source_date(enrichment_data.get("auction_date"))
    return None


def is_auction_date_fallback(date_recorded: Any, enrichment_data: Any) -> bool:
    """True when ``date_recorded`` is the scraper's auction-date stand-in.

    Fails closed: a missing or unreadable date on either side is NOT a stand-in, so a
    real date is never hidden on a guess.
    """
    recorded = parse_source_date(date_recorded)
    return recorded is not None and recorded == origin_auction_date(enrichment_data)
