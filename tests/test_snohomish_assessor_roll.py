"""Snohomish Assessor Roll bulk mailing source — pure units, no network.

The county stripped its public GIS attribute table, so owner mailing now comes from
the Assessor Roll CSV export. Two things in that file will silently destroy coverage
if they are ever got wrong, and both are pinned here:

  * Its parcel keys have LEADING ZEROS STRIPPED. Our DB holds 00437860401300; the
    file holds 437860401300. Only 19.7% of its keys are full 14-char, so an exact
    match would miss four parcels in five and report them as having no mailing
    address — the exact bug this source exists to fix.
  * 11 keys carry more than one Taxpayer row. Ten are co-taxpayers at ONE address;
    one genuinely disagrees. Resolving that by row order would invent an answer.
"""
import csv
import io
import json
import sqlite3
import time
import zipfile

import pytest

from src.scrapers.enrichment import snohomish_assessor_roll as roll
from src.scrapers.enrichment.snohomish_assessor_roll import (
    ABSENT_IN_SNAPSHOT,
    AMBIGUOUS,
    FOUND,
    SOURCE_UNAVAILABLE,
    MailingAnswer,
    normalize_parcel_key,
)

# conftest stubs _ensure_index for every test so nothing reaches the live county
# file. Captured HERE, at import, before that autouse patch applies, so the tests
# that are ABOUT _ensure_index can put the real one back (and are not silently
# asserting against the stub).
_REAL_ENSURE_INDEX = roll._ensure_index

HEADER = [
    "ID", "PropId", "parcel_number", "Role", "PartyName",
    "line_1", "line_2", "line_3", "city", "State", "zip_postal_code",
]


def _row(pid, role="Taxpayer", name="SMITH JOHN", l1="1 MAIN ST", l2="", l3="",
         city="EVERETT", state="WA", zipc="98201", _id="1", prop="1"):
    return [_id, prop, pid, role, name, l1, l2, l3, city, state, zipc]


def _make_zip(path, rows, header=None, member="NameAddr.csv"):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header if header is not None else HEADER)
    for r in rows:
        w.writerow(r)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, buf.getvalue())
    return path


def _bulk(n, start=10_000_000):
    """Enough well-formed rows to clear the minimum-rows canary."""
    return [_row(str(start + i), _id=str(i), prop=str(i)) for i in range(n)]


class TestNormalizeParcelKey:
    def test_leading_zeros_are_stripped_to_match_the_county_export(self):
        assert normalize_parcel_key("00437860401300") == "437860401300"
        assert normalize_parcel_key("00507800001100") == "507800001100"

    def test_a_key_with_no_leading_zeros_is_unchanged(self):
        assert normalize_parcel_key("30061400301600") == "30061400301600"

    def test_both_spellings_collapse_to_one_key(self):
        assert normalize_parcel_key("00437860401300") == normalize_parcel_key("437860401300")

    def test_dashes_and_whitespace_are_tolerated(self):
        assert normalize_parcel_key("  0043786-0401300 ") == "437860401300"

    @pytest.mark.parametrize("bad", ["", None, "   ", "abc", "12a34", "00000000", "0"])
    def test_values_that_identify_no_parcel_are_rejected(self, bad):
        # Critically, an all-zero id must NOT become "" and match another blank.
        assert normalize_parcel_key(bad) is None

    def test_it_never_int_casts(self):
        # An int round-trip would lose the distinction this whole module turns on.
        assert isinstance(normalize_parcel_key("00437860401300"), str)


class TestCompose:
    def test_street_city_state_zip(self):
        assert roll._compose({
            "line_1": "73 KNIGHT HILL RD", "city": "ZILLAH",
            "State": "WA", "zip_postal_code": "98953",
        }) == "73 KNIGHT HILL RD, ZILLAH, WA 98953"

    def test_no_street_is_unusable(self):
        assert roll._compose({"line_1": "  ", "city": "EVERETT", "State": "WA",
                              "zip_postal_code": "98201"}) is None

    def test_line_2_is_ignored(self):
        # Populated on 4 rows of 316,584, half of them "C/O <person>" — an addressee
        # NAME this codebase does not collect.
        assert roll._compose({
            "line_1": "20247 86TH PL NE", "line_2": "C/O MCCARTHY WILLIAM",
            "city": "ARLINGTON", "State": "WA", "zip_postal_code": "98223",
        }) == "20247 86TH PL NE, ARLINGTON, WA 98223"

    def test_missing_locality_still_yields_the_street(self):
        assert roll._compose({"line_1": "PO BOX 4", "city": "", "State": "",
                              "zip_postal_code": ""}) == "PO BOX 4"

    def test_result_fits_the_column(self):
        out = roll._compose({"line_1": "X" * 900, "city": "EVERETT", "State": "WA",
                             "zip_postal_code": "98201"})
        assert len(out) <= 512


class TestIndexBuildAndLookup:
    """End to end through a real zip + real sqlite index, no network."""

    @pytest.fixture
    def built(self, tmp_path, monkeypatch):
        monkeypatch.setattr(roll, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(roll, "_MIN_TAXPAYER_ROWS", 3)

        def _build(rows):
            z = _make_zip(tmp_path / "src.zip", rows)
            roll._build_index(z, roll._index_path("rev1"), "rev1")
            monkeypatch.setattr(roll, "_ensure_index", lambda: roll._index_path("rev1"))
        return _build

    def test_a_zero_padded_lookup_hits_a_zero_stripped_row(self, built):
        built([_row("437860401300", l1="2407 EVERETT AVE"), *_bulk(3)])
        ans = roll.resolve_mailing(["00437860401300"])["00437860401300"]
        assert ans.outcome == FOUND
        assert ans.mailing_address == "2407 EVERETT AVE, EVERETT, WA 98201"
        assert ans.role == "Taxpayer"

    def test_owner_rows_are_not_used(self, built):
        built([_row("999111", role="Owner", l1="OWNER ST"), *_bulk(3)])
        assert roll.resolve_mailing(["999111"])["999111"].outcome == ABSENT_IN_SNAPSHOT

    def test_a_parcel_absent_from_the_snapshot_is_not_a_negative(self, built):
        # "no address association in THIS revision", never "has no mailing address".
        built(_bulk(4))
        assert roll.resolve_mailing(["55555555"])["55555555"].outcome == ABSENT_IN_SNAPSHOT

    def test_co_taxpayers_at_one_address_are_not_ambiguous(self, built):
        # 10 of the 11 duplicate keys in the live file are exactly this.
        built([
            _row("1254100000100", name="PACIFIC RIDGE", l1="17921 BOTHELL EVERETT HWY"),
            _row("1254100000100", name="DRH ENERGY INC", l1="17921 BOTHELL EVERETT HWY"),
            *_bulk(3),
        ])
        ans = roll.resolve_mailing(["1254100000100"])["1254100000100"]
        assert ans.outcome == FOUND
        assert ans.mailing_address.startswith("17921 BOTHELL EVERETT HWY")

    def test_conflicting_taxpayer_rows_are_ambiguous_not_first_wins(self, built):
        built([
            _row("777000", name="A", l1="1 FIRST ST"),
            _row("777000", name="B", l1="2 SECOND ST"),
            *_bulk(3),
        ])
        ans = roll.resolve_mailing(["777000"])["777000"]
        assert ans.outcome == AMBIGUOUS
        assert ans.mailing_address is None

    def test_every_caller_spelling_gets_the_answer(self, built):
        built([_row("437860401300", l1="2407 EVERETT AVE"), *_bulk(3)])
        out = roll.resolve_mailing(["00437860401300", "437860401300", "0043786-0401300"])
        assert {a.mailing_address for a in out.values()} == {
            "2407 EVERETT AVE, EVERETT, WA 98201"
        }

    def test_unidentifiable_ids_do_not_collide(self, built):
        built(_bulk(4))
        out = roll.resolve_mailing(["", "0", "abc"])
        assert all(a.outcome == ABSENT_IN_SNAPSHOT for a in out.values())

    def test_revision_is_recorded_on_every_answer(self, built):
        built([_row("437860401300"), *_bulk(3)])
        assert roll.resolve_mailing(["437860401300"])["437860401300"].revision == "rev1"


class TestCanariesRejectABadRevision:
    """A bad revision must never take a good one out of service."""

    @pytest.fixture
    def cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(roll, "_CACHE_DIR", tmp_path)
        return tmp_path

    def test_a_truncated_file_is_rejected(self, cache, monkeypatch):
        monkeypatch.setattr(roll, "_MIN_TAXPAYER_ROWS", 1000)
        z = _make_zip(cache / "s.zip", _bulk(5))
        with pytest.raises(RuntimeError, match="taxpayer rows"):
            roll._build_index(z, roll._index_path("r"), "r")
        assert not roll._index_path("r").exists()

    def test_a_changed_header_is_rejected(self, cache, monkeypatch):
        monkeypatch.setattr(roll, "_MIN_TAXPAYER_ROWS", 1)
        z = _make_zip(cache / "s.zip", [], header=["ID", "parcel_number", "WHO"])
        with pytest.raises(RuntimeError, match="header changed"):
            roll._build_index(z, roll._index_path("r"), "r")

    def test_a_revision_that_lost_its_addresses_is_rejected(self, cache, monkeypatch):
        # The GIS collapse shape: rows present, every address blank.
        monkeypatch.setattr(roll, "_MIN_TAXPAYER_ROWS", 3)
        rows = [_row(str(900000 + i), l1="") for i in range(6)]
        z = _make_zip(cache / "s.zip", rows)
        with pytest.raises(RuntimeError, match="coverage"):
            roll._build_index(z, roll._index_path("r"), "r")

    def test_a_missing_member_is_rejected(self, cache, monkeypatch):
        monkeypatch.setattr(roll, "_MIN_TAXPAYER_ROWS", 1)
        z = _make_zip(cache / "s.zip", _bulk(3), member="Something.csv")
        with pytest.raises(RuntimeError, match="expected exactly one"):
            roll._build_index(z, roll._index_path("r"), "r")

    def test_a_good_build_publishes_an_index_and_its_provenance(self, cache, monkeypatch):
        monkeypatch.setattr(roll, "_MIN_TAXPAYER_ROWS", 3)
        z = _make_zip(cache / "s.zip", _bulk(5))
        stats = roll._build_index(z, roll._index_path("r7"), "r7")
        assert roll._index_path("r7").exists()
        assert stats["taxpayer_rows"] == 5
        meta = json.loads((cache / "snapshot.json").read_text())
        assert meta["revision"] == "r7"
        con = sqlite3.connect(str(roll._index_path("r7")))
        try:
            assert con.execute("SELECT count(*) FROM mailing").fetchone()[0] == 5
        finally:
            con.close()


class TestSourceUnavailableIsNeverANegative:
    def test_no_index_reports_source_unavailable(self, monkeypatch):
        monkeypatch.setattr(roll, "_ensure_index", lambda: None)
        out = roll.resolve_mailing(["00437860401300"])
        assert out["00437860401300"].outcome == SOURCE_UNAVAILABLE
        assert out["00437860401300"].mailing_address is None

    def test_a_raising_index_reports_source_unavailable(self, monkeypatch):
        def _boom():
            raise RuntimeError("network down")
        monkeypatch.setattr(roll, "_ensure_index", _boom)
        assert roll.resolve_mailing(["1"])["1"].outcome == SOURCE_UNAVAILABLE

    def test_empty_request_is_a_no_op(self):
        assert roll.resolve_mailing([]) == {}


class TestSourceAgeCeiling:
    """Keeping the last good revision through a failed refresh is right; serving it
    forever is not. The clock is the COUNTY's publish date, not ours: ageing by our
    own build time meant re-downloading an already-stale source bought it another
    90 days (Codex)."""

    def test_a_recent_revision_is_not_too_old(self):
        recent = str(int((time.time() - 5 * 86400) * 1000))
        assert roll._source_is_too_old(recent) is False

    def test_a_revision_past_the_ceiling_is_too_old(self):
        ancient = str(int((time.time() - 200 * 86400) * 1000))
        assert roll._source_is_too_old(ancient) is True

    @pytest.mark.parametrize("bad", [None, "", "not-a-number", "0", "-5"])
    def test_an_unreadable_revision_is_never_called_too_old(self, bad):
        # A format change must not take a working source out of service.
        assert roll._source_is_too_old(bad) is False

    def test_the_live_revision_is_inside_the_ceiling(self):
        # Guards against the ceiling being set so tight it disables the source.
        # 1787655906000 = 2026-08-25, the revision live when this was written.
        assert roll._source_is_too_old("1787655906000") is False

    def test_an_expired_index_is_not_served_by_the_revision_branch(self, tmp_path, monkeypatch):
        # The ceiling used to reject `prior` and then hand back the very same file
        # from the next branch, because prior_rev == revision (Codex).
        ancient = str(int((time.time() - 200 * 86400) * 1000))
        monkeypatch.setattr(roll, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(roll, "_MIN_TAXPAYER_ROWS", 3)
        z = _make_zip(tmp_path / "s.zip", _bulk(4))
        roll._build_index(z, roll._index_path(ancient), ancient)
        # checked_at must be OUTSIDE the backoff window or the cold/warm gate
        # returns first and this never reaches the branch it is named for (Codex).
        monkeypatch.setattr(roll, "_published_meta",
                            lambda: {"revision": ancient, "built_at": time.time(),
                                     "checked_at": time.time() - 2 * roll._REFRESH_AFTER_S})
        monkeypatch.setattr(roll, "_remote_revision", lambda: ancient)
        monkeypatch.setattr(roll, "_ensure_index", _REAL_ENSURE_INDEX)
        assert roll._ensure_index() is None

    def test_a_manifest_whose_index_file_is_gone_still_backs_off(self, tmp_path, monkeypatch):
        # A manifest revision makes a truthy Path even when the SQLite file is gone.
        # Both backoff branches used to miss it, so a failed build meant hitting the
        # county on EVERY batch despite a recent checked_at (Codex).
        monkeypatch.setattr(roll, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(roll, "_published_meta",
                            lambda: {"revision": "gone", "checked_at": time.time()})

        def _must_not_be_called():
            raise AssertionError("asked the county inside the backoff window")

        monkeypatch.setattr(roll, "_remote_revision", _must_not_be_called)
        monkeypatch.setattr(roll, "_ensure_index", _REAL_ENSURE_INDEX)
        assert roll._ensure_index() is None

    def test_a_cold_failure_backs_off_instead_of_redownloading_every_batch(
        self, tmp_path, monkeypatch
    ):
        # No index at all, and we tried moments ago. Without this gate a county
        # outage meant a fresh 33 MB download attempt on EVERY batch (Codex).
        monkeypatch.setattr(roll, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(roll, "_published_meta", lambda: {"checked_at": time.time()})

        def _must_not_be_called():
            raise AssertionError("asked the county inside the backoff window")

        monkeypatch.setattr(roll, "_remote_revision", _must_not_be_called)
        monkeypatch.setattr(roll, "_ensure_index", _REAL_ENSURE_INDEX)
        assert roll._ensure_index() is None

    def test_after_the_backoff_window_a_cold_start_tries_again(self, tmp_path, monkeypatch):
        monkeypatch.setattr(roll, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(roll, "_published_meta",
                            lambda: {"checked_at": time.time() - 2 * roll._REFRESH_AFTER_S})
        asked = []
        monkeypatch.setattr(roll, "_remote_revision",
                            lambda: asked.append(1) or None)
        monkeypatch.setattr(roll, "_ensure_index", _REAL_ENSURE_INDEX)
        roll._ensure_index()
        assert asked, "should have re-asked the county after the window"

    def test_a_cold_start_still_records_that_we_asked(self, tmp_path, monkeypatch):
        # _touch_checked_at({}) used to be a no-op, so a cold failure re-downloaded
        # on every single batch with no backoff (Codex).
        monkeypatch.setattr(roll, "_CACHE_DIR", tmp_path)
        roll._touch_checked_at({})
        assert "checked_at" in json.loads((tmp_path / "snapshot.json").read_text())


class TestMailingAnswer:
    def test_only_found_with_an_address_counts_as_found(self):
        assert MailingAnswer(FOUND, "1 MAIN ST").is_found is True
        assert MailingAnswer(FOUND).is_found is False
        assert MailingAnswer(SOURCE_UNAVAILABLE).is_found is False
        assert MailingAnswer(ABSENT_IN_SNAPSHOT).is_found is False
        assert MailingAnswer(AMBIGUOUS).is_found is False
