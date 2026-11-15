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
from sqlalchemy.orm import Session

from src.db.session import sync_engine, system_sync_session
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


def _open(_parcels, _county, _reps):
    return None


_CLEAN = text("DELETE FROM probate_notices WHERE source = 'lmtribune_probate'")


@pytest.fixture
def s():
    # rematch_due commits per notice, so the table is cleaned before and after.
    with system_sync_session() as session:
        session.execute(_CLEAN)
        session.commit()
        try:
            yield session
        finally:
            session.rollback()
            session.execute(_CLEAN)
            session.commit()


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
                  "owner_name": "HOLLOWAY, MARGARET J"}], "asotin", []) == rule_row
    assert _ingest(s, "asotin_probate_notice_a", gate=gate) == "withheld"
    row = _row(s, "26-4-00122-02")
    assert (row["decedent"], row["personal_reps"], row["claims_address"], row["parcels"]) == (
        None, None, None, None)
    assert str(row["privacy_suppression_id"]) == rule_row
    # A later crawl, even one whose gate no longer sees the rule, never writes it back.
    assert _ingest(s, "asotin_probate_notice_a") == "withheld"
    assert _row(s, "26-4-00122-02")["decedent"] is None


def _rep_rule(s, person="dunn|rachel", parcel="10412300600050000") -> str:
    # A personal representative (natural order on the notice) with her own rule, by
    # default on the estate's tied parcel.
    anchor = keys.property_anchor_value(parcel, "asotin", "WA")
    return _active_rule(s, "opt_out", anchor=("property", anchor),
                        subject=("person", person))[1]


def _tombstoned(s, rule_row: str) -> None:
    row = _row(s, "26-4-00122-02")
    assert row["match_outcome"] == "withheld"
    assert (row["decedent"], row["personal_reps"], row["claims_address"], row["parcels"]) == (
        None, None, None, None)
    assert str(row["privacy_suppression_id"]) == rule_row


def test_a_personal_representatives_rule_on_a_tied_parcel_tombstones_at_ingest(s):
    rule_row = _rep_rule(s)
    gate = pi.privacy_gate(s)
    assert _ingest(s, "asotin_probate_notice_a", gate=gate) == "withheld"
    _tombstoned(s, rule_row)


def test_a_personal_representatives_rule_tombstones_at_the_recheck():
    # rematch_due commits per notice and a rule can never be deleted, so the whole test
    # runs in one outer transaction (each commit is a savepoint) that is rolled back.
    with sync_engine.connect() as conn:
        outer = conn.begin()
        try:
            s = Session(bind=conn, join_transaction_mode="create_savepoint")
            _ingest(s, "asotin_probate_notice_a", lookup=_down)  # failed, reps stored
            s.commit()
            rule_row = _rep_rule(s)
            later = _NOW + pi.REMATCH_EVERY + timedelta(minutes=1)
            assert pi.rematch_due(s, today=later.date(), now=later, lookup=_layer(_ROWS),
                                  gate=pi.privacy_gate(s)) == 1
            _tombstoned(s, rule_row)
            s.close()
        finally:
            outer.rollback()


def test_every_personal_representative_is_checked(s):
    rule_row = _rep_rule(s, person="holloway|amanda")              # the second one
    assert _ingest(s, "asotin_probate_notice_a", gate=pi.privacy_gate(s)) == "withheld"
    _tombstoned(s, rule_row)


def test_a_representatives_rule_elsewhere_still_withholds_like_the_decedents(s):
    # Her name without the rule's parcel is a review, and the gate keeps nobody under
    # review (as for the decedent): the notice is stored as a tombstone.
    rule_row = _rep_rule(s, parcel="10412300600099999")
    assert _ingest(s, "asotin_probate_notice_a", gate=pi.privacy_gate(s)) == "withheld"
    _tombstoned(s, rule_row)


@pytest.mark.parametrize("stored", [None, "RACHEL M. DUNN", {"name": "RACHEL M. DUNN"}])
def test_a_recheck_hands_the_gate_a_list_whatever_was_stored(s, stored):
    # Notice B's estate is tied by its estate-marked owner, not by a representative.
    _ingest(s, "asotin_probate_notice_b", lookup=_down)
    s.execute(text("UPDATE probate_notices SET personal_reps = CAST(:r AS json) "
                   "WHERE cause_no = '26-4-00117-02'"), {"r": json.dumps(stored)})
    s.commit()
    estate = dict(next(r for r in _ROWS if r["NAME"].startswith("HOLLOWAY")),
                  NAME="PRATT, DOROTHY L (ESTATE)", PARCEL_ID="10412300600070000")
    seen = []

    def spy(_parcels, _county, reps):
        seen.append(reps)
        return None

    later = _NOW + pi.REMATCH_EVERY + timedelta(minutes=1)
    assert pi.rematch_due(s, today=later.date(), now=later, lookup=_layer([estate]),
                          gate=spy) == 1
    assert seen == [[]]


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


def test_a_rematch_never_overwrites_a_newer_crawl_answer(s):
    _ingest(s, "asotin_probate_notice_a", lookup=_down)          # stored as failed
    s.commit()
    later = _NOW + timedelta(days=1)

    def lookup_while_a_crawl_rereads(surname):
        # The crawl re-reads the advert (layer back) while this rematch waits on the layer.
        with system_sync_session() as other:
            assert _ingest(other, "asotin_probate_notice_a", now=later) == "matched"
            other.commit()
        raise LayerError("layer HTTP 503")                         # the rematch's own answer

    assert pi.rematch_due(s, today=later.date(), now=later, lookup=lookup_while_a_crawl_rereads,
                          gate=_open) == 1
    assert _row(s, "26-4-00122-02")["match_outcome"] == "matched"


def test_one_failing_recheck_backs_off_and_the_rest_continue(s):
    _ingest(s, "asotin_probate_notice_a", lookup=_down)
    _ingest(s, "asotin_probate_notice_b", lookup=_down)
    s.commit()
    later = _NOW + timedelta(days=1)

    def lookup(surname):
        if surname == "HOLLOWAY":
            raise RuntimeError("unexpected answer")
        return _layer(_ROWS)(surname)

    assert pi.rematch_due(s, today=later.date(), now=later, lookup=lookup, gate=_open) == 2
    a, b = _row(s, "26-4-00122-02"), _row(s, "26-4-00117-02")
    assert a["match_outcome"] == "failed" and a["matched_at"] == later   # backed off
    assert b["match_outcome"] == "held"
    # Backed off: not due again until REMATCH_EVERY has passed.
    assert pi.rematch_due(s, today=later.date(), now=later, lookup=lookup, gate=_open) == 0


def test_rematch_stops_at_the_deadline(s):
    _ingest(s, "asotin_probate_notice_a", lookup=_down)
    s.commit()
    later = _NOW + timedelta(days=1)
    assert pi.rematch_due(s, today=later.date(), now=later, lookup=_layer(_ROWS), gate=_open,
                          deadline=0.0) == 0
