"""Surrogate identities: Snohomish never keys on APN-, and a real TS retires a REF- twin.

P1-a: APN-<parcel> is not collision-free (a first and a second lien on one parcel would
collapse into one row), so the Snohomish Tribune parser rejects a notice whose ONLY
identity is its parcel. King keeps APN- (production rows already use it).

P1-b: a notice first stored under REF-<deed recording #> and later printed WITH its real
TS number is a different natural key. Upserting the real one must retire the REF- row
keyed by the notice's own recording number, or one sale shows up as two auction leads,
and a later re-crawl of the REF- version must not switch the retired twin back on.

Real Postgres (test DB, guarded by conftest) + the crawler's own upsert + real notice
text from tests/fixtures. No mocks.
"""
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text

from src.db.models import NtsNotice
from src.db.session import system_sync_session
from src.scrapers.sources import nts_pdf
from src.scrapers.sources import nts_tacoma_index as nts
from src.scrapers.sources.nts_king_pdf import parse_king_notice, parse_snoho_notice
from src.workers import nts_crawler

_FX = Path(__file__).parent / "fixtures"
_SOURCE = "test_surrogate_retire"  # unique to this file; every row is cleaned up by it
_REAL_TS = "TEST-SURROGATE-RETIRE-TS"


def _block(pdf: str, needle: str) -> str:
    text_ = nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text((_FX / pdf).read_bytes()))
    hits = [b for b in nts_pdf.split_notice_blocks(text_) if needle in b]
    assert len(hits) == 1
    return hits[0]


# ── P1-a ──────────────────────────────────────────────────────────────────────

_REF_SENTENCE = "Reference number of the deed of trust: Auditor's File No. 202303210039."


def test_snohomish_rejects_a_parcel_only_identity_that_king_keeps():
    block = _block("nts_snoho_tribune_2025-12-17.pdf", "Randy A. Lindquist")
    assert _REF_SENTENCE in block
    # The same real notice with its deed reference removed: only the parcel identifies it.
    parcel_only = block.replace(_REF_SENTENCE, "")
    assert parse_king_notice(parcel_only)["ts_number"] == "APN-005009-000-046-00"
    snoho = parse_snoho_notice(parcel_only)
    assert snoho["ts_number"] is None
    assert not nts.is_valid_nts(snoho)
    # With the deed reference present the unique REF- key stays.
    assert parse_snoho_notice(block)["ts_number"] == "REF-202303210039"


def test_snohomish_parser_matches_king_parser_on_every_non_apn_notice():
    for pdf in ("nts_snoho_tribune_2026-09-09.pdf", "nts_snoho_tribune_2025-12-17.pdf"):
        raw = nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text((_FX / pdf).read_bytes()))
        for b in nts_pdf.split_notice_blocks(raw):
            king = parse_king_notice(b)
            if not (king["ts_number"] or "").startswith("APN-"):
                assert parse_snoho_notice(b) == king


# ── P1-b ──────────────────────────────────────────────────────────────────────

def _affinia_row() -> dict:
    """The real 2026-09-09 Affinia notice as a row (REF-202411260448, 10/09/2026).

    `today` is pinned to the issue date: with date.today() the sale went past on
    2026-10-10, notice_to_row marked it inactive, and all seven tests below failed.
    """
    parsed = parse_snoho_notice(
        _block("nts_snoho_tribune_2026-09-09.pdf", "Affinia Default Services, LLC Current"))
    row = nts.notice_to_row(parsed, source_url="https://example.invalid/legals.pdf",
                            today=date(2026, 9, 9), source=_SOURCE, county="snohomish")
    assert row is not None and row["ts_number"] == "REF-202411260448" and row["is_active"]
    return row


def _upsert(db, row: dict, deed_reference: str | None = None) -> None:
    nts_crawler._upsert_notice(db, NtsNotice, row, deed_reference=deed_reference)
    db.commit()


def _active_by_ts(db) -> dict:
    return dict(db.execute(
        text("SELECT ts_number, is_active FROM nts_notices WHERE source = :s"),
        {"s": _SOURCE}).fetchall())


def _cleanup(db) -> None:
    db.execute(text("DELETE FROM nts_notices WHERE source = :s"), {"s": _SOURCE})
    db.commit()


def test_real_ts_upsert_retires_exactly_its_own_recording_number_twin():
    # Two different sales on the same parcel and sale day (e.g. first and second lien):
    # only the surrogate built from the real notice's OWN recording number is retired.
    with system_sync_session() as db:
        _cleanup(db)
        try:
            base = _affinia_row()
            _upsert(db, base)                                         # REF-202411260448
            _upsert(db, {**base, "ts_number": "REF-202211100430"})   # other deed, same parcel/day
            _upsert(db, {**base, "ts_number": _REAL_TS}, deed_reference="202411260448")
            assert _active_by_ts(db) == {
                "REF-202411260448": False, "REF-202211100430": True, _REAL_TS: True}
        finally:
            _cleanup(db)


def test_twin_is_retired_even_when_the_sale_was_postponed():
    # The real notice's date moved; recording-number identity still finds the twin.
    with system_sync_session() as db:
        _cleanup(db)
        try:
            base = _affinia_row()
            _upsert(db, base)
            moved = {**base, "ts_number": _REAL_TS,
                     "auction_date": base["auction_date"] + timedelta(days=21)}
            _upsert(db, moved, deed_reference="202411260448")
            assert _active_by_ts(db) == {"REF-202411260448": False, _REAL_TS: True}
        finally:
            _cleanup(db)


def test_real_notice_without_a_numeric_deed_reference_retires_nothing():
    with system_sync_session() as db:
        _cleanup(db)
        try:
            base = _affinia_row()
            _upsert(db, base)
            _upsert(db, {**base, "ts_number": _REAL_TS}, deed_reference=None)
            _upsert(db, {**base, "ts_number": _REAL_TS + "-B"}, deed_reference="Auditor")
            assert _active_by_ts(db) == {
                "REF-202411260448": True, _REAL_TS: True, _REAL_TS + "-B": True}
        finally:
            _cleanup(db)


def test_recrawling_the_surrogate_does_not_resurrect_a_retired_twin():
    with system_sync_session() as db:
        _cleanup(db)
        try:
            base = _affinia_row()
            _upsert(db, base, deed_reference="202411260448")
            _upsert(db, {**base, "ts_number": _REAL_TS}, deed_reference="202411260448")
            # An older issue that still prints the REF- version is read again.
            _upsert(db, base, deed_reference="202411260448")
            assert _active_by_ts(db) == {"REF-202411260448": False, _REAL_TS: True}
        finally:
            _cleanup(db)


def test_a_postponed_sale_does_not_let_the_old_surrogate_come_back():
    # The real notice moved the sale date; re-reading the older REF- issue afterwards
    # must still leave the retired twin off (Codex r3).
    with system_sync_session() as db:
        _cleanup(db)
        try:
            base = _affinia_row()
            _upsert(db, base, deed_reference="202411260448")
            moved = {**base, "ts_number": _REAL_TS,
                     "auction_date": base["auction_date"] + timedelta(days=21)}
            _upsert(db, moved, deed_reference="202411260448")
            _upsert(db, base, deed_reference="202411260448")
            assert _active_by_ts(db) == {"REF-202411260448": False, _REAL_TS: True}
        finally:
            _cleanup(db)


def test_a_fresh_surrogate_with_no_real_twin_becomes_active():
    with system_sync_session() as db:
        _cleanup(db)
        try:
            base = _affinia_row()
            _upsert(db, base, deed_reference="202411260448")
            _upsert(db, base, deed_reference="202411260448")  # re-crawl, still no real twin
            assert _active_by_ts(db) == {"REF-202411260448": True}
        finally:
            _cleanup(db)


def test_a_surrogate_upsert_never_retires_another_row():
    with system_sync_session() as db:
        _cleanup(db)
        try:
            base = _affinia_row()
            _upsert(db, {**base, "ts_number": "REF-202211100430"})
            _upsert(db, base, deed_reference="202411260448")  # also a surrogate
            assert _active_by_ts(db) == {"REF-202211100430": True, "REF-202411260448": True}
        finally:
            _cleanup(db)
