"""Storing Asotin probate notices at crawl time (workers/probate_ingest, migration 160).

Real database, one session rolled back at the end. The notice texts and county-layer
rows are the redacted real fixtures of test_probate_nwmarket / test_asotin_parcels; the
layer is injected as ``lookup`` (the crawl passes asotin_parcels.query_surname).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.privacy import keys
from src.scrapers.enrichment.asotin_parcels import LayerError
from src.workers import probate_ingest as pi
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring
from tests.test_privacy_erase import _active_rule

_FIX = Path(__file__).parent / "fixtures"
_ROWS = [f["attributes"] for f in json.loads(
    (_FIX / "asotin_parcel_layer_owners.json").read_text())["features"]]
_NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
_TODAY = _NOW.date()


def _text(name: str) -> str:
    return (_FIX / f"{name}.txt").read_text(encoding="utf-8")


def _layer(rows):
    return lambda surname: [r for r in rows if surname in r["NAME"]]


def _down(_surname):
    raise LayerError("layer HTTP 503")


def _open(_parcels, _county):
    return None


@pytest.fixture
def s():
    with system_sync_session() as session:
        session.execute(text("DELETE FROM probate_notices WHERE source = 'lmtribune_probate'"))
        try:
            yield session
        finally:
            session.rollback()


def _row(s, cause: str):
    return s.execute(text(
        "SELECT * FROM probate_notices WHERE county = 'asotin' AND cause_no = :c"),
        {"c": cause}).mappings().one_or_none()


def _ingest(s, name: str, lookup=None, gate=_open, now=_NOW):
    return pi.ingest_text(s, _text(name), advert_id="2267", pdf_url="https://x/2267.pdf",
                          now=now, lookup=lookup or _layer(_ROWS), gate=gate)


def test_a_tied_notice_is_stored_with_its_parcel():
    with system_sync_session() as s:
        try:
            assert _ingest(s, "asotin_probate_notice_a") == "matched"
            row = _row(s, "26-4-00122-02")
            assert row["decedent"] == "MARGARET J. HOLLOWAY"
            assert row["personal_reps"] == ["RACHEL M. DUNN", "AMANDA J. HOLLOWAY"]
            assert row["first_published"] == date(2026, 11, 4)
            assert [p["parcel_id"] for p in row["parcels"]] == ["10412300600050000"]
            assert row["parcels"][0]["party_name"] == "HOLLOWAY, MARGARET J"
            assert row["parcels"][0]["ties"] == ["rep_shares_surname"]
        finally:
            s.rollback()


def test_other_notices_and_untied_estates(s):
    assert _ingest(s, "spokane_probate_notice") is None
    assert _ingest(s, "asotin_probate_notice_b") == "held"
    row = _row(s, "26-4-00117-02")
    assert row["parcels"] is None and row["held_reasons"] == ["no_parcel_in_decedents_name"]


def test_a_reread_is_idempotent_and_an_outage_keeps_the_answer(s):
    _ingest(s, "asotin_probate_notice_a")
    assert _ingest(s, "asotin_probate_notice_a", lookup=_down) == "failed"
    row = _row(s, "26-4-00122-02")
    assert row["match_outcome"] == "matched" and len(row["parcels"]) == 1
    assert s.execute(text("SELECT count(*) FROM probate_notices WHERE cause_no = "
                          "'26-4-00122-02'")).scalar() == 1


def test_a_first_read_during_an_outage_is_failed_and_rechecked(s):
    assert _ingest(s, "asotin_probate_notice_a", lookup=_down) == "failed"
    assert _row(s, "26-4-00122-02")["match_outcome"] == "failed"
    later = _NOW + timedelta(days=1)
    assert pi.rematch_due(s, today=later.date(), now=later, lookup=_layer(_ROWS), gate=_open) == 1
    assert _row(s, "26-4-00122-02")["match_outcome"] == "matched"


def test_a_privacy_rule_on_a_tied_parcel_tombstones_the_notice_for_good(s):
    parcel = "10412300600050000"
    anchor = keys.property_anchor_value(parcel, "asotin", "WA")
    rule_id, rule_row = _active_rule(s, "opt_out", anchor=("property", anchor),
                                     subject=("person", "holloway|margaret"))
    gate = pi.privacy_gate(s)
    assert gate([{"parcel_id": parcel, "party_name": "HOLLOWAY, MARGARET J",
                  "owner_name": "HOLLOWAY, MARGARET J"}], "asotin") == rule_row
    assert _ingest(s, "asotin_probate_notice_a", gate=gate) == "withheld"
    row = _row(s, "26-4-00122-02")
    assert (row["decedent"], row["personal_reps"], row["claims_address"], row["parcels"]) == (
        None, None, None, None)
    assert str(row["privacy_suppression_id"]) == rule_row
    # A later crawl, even one whose gate no longer sees the rule, never writes it back.
    assert _ingest(s, "asotin_probate_notice_a") == "withheld"
    assert _row(s, "26-4-00122-02")["decedent"] is None


def test_retention_deletes_old_notices_but_keeps_tombstones(s):
    _ingest(s, "asotin_probate_notice_a")
    _ingest(s, "asotin_probate_notice_b")
    _, rule_row = _active_rule(s, "opt_out")
    s.execute(text(
        "UPDATE probate_notices SET decedent = NULL, personal_reps = NULL, claims_address = "
        "NULL, parcels = NULL, privacy_suppression_id = CAST(:r AS uuid) "
        "WHERE cause_no = '26-4-00117-02'"), {"r": rule_row})
    later = date(2026, 11, 4) + timedelta(days=pi.RETENTION_DAYS + 2)
    assert pi.purge_expired(s, today=later) == 1
    assert _row(s, "26-4-00122-02") is None
    assert _row(s, "26-4-00117-02") is not None


def test_a_held_notice_is_rechecked_and_can_become_a_lead(s):
    _ingest(s, "asotin_probate_notice_b")      # DOROTHY L. PRATT owns nothing yet
    estate = dict(next(r for r in _ROWS if r["NAME"].startswith("HOLLOWAY")),
                  NAME="PRATT, DOROTHY L (ESTATE)", PARCEL_ID="10412300600070000")
    # Not due yet: checked within the last REMATCH_EVERY.
    assert pi.rematch_due(s, today=_TODAY, now=_NOW, lookup=_layer([estate]), gate=_open) == 0
    later = _NOW + pi.REMATCH_EVERY + timedelta(minutes=1)
    assert pi.rematch_due(s, today=later.date(), now=later, lookup=_layer([estate]),
                          gate=_open) == 1
    row = _row(s, "26-4-00117-02")
    assert row["match_outcome"] == "matched" and row["parcels"][0]["ties"] == ["estate_marked"]
