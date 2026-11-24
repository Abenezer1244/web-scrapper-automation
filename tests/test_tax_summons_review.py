"""Operator review of the Douglas Treasurer's tax-foreclosure summons (migration 173).

Real test database. Most tests run in one outer transaction rolled back at the end (the
CLI's commits release savepoints, as in test_nts_chelan_approval). The race tests need
two committed connections; they delete what they wrote afterwards. Rows are shaped like
the 2026 Douglas summons' Exhibit A; names and addresses are placeholders, never people.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from src.db.models import TaxSummonsEvent as Event
from src.db.models import TaxSummonsRow as Row
from src.db.models import TaxSummonsVersion as Version
from src.db.session import sync_engine
from src.utils.crypto import blind_index
from src.workers import tax_summons as store
from src.workers.tax_summons import (
    TaxReviewError,
    draft_rows,
    load_rows,
    publish,
    serving_status,
    set_manifest,
)
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring


def _load_module(name: str, *parts: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1].joinpath(*parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_module("tax_summons_review", "scripts", "tax_summons_review.py")
migration_173 = _load_module("migration_173", "alembic", "versions", "173_tax_summons_review.py")

_URL = "https://www.douglascountysheriff.org/DocumentCenter/View/0000/2026-Notice-and-Summons"
_CASE = "26-2-00337-09"
_AS_OF = date(2026, 8, 1)  # "Taxes and Interest Due As Of August 1, 2026"
_ESC = chr(27)  # a terminal escape
_A, _B, _C, _D = "41020600100", "41020600200", "41020600300", "41020600400"


@pytest.fixture
def c():
    with sync_engine.connect() as conn:
        outer = conn.begin()

        @contextmanager
        def _session():
            s = Session(bind=conn, join_transaction_mode="create_savepoint")
            try:
                yield s
            finally:
                s.close()

        conn.session_factory = _session
        try:
            yield conn
        finally:
            outer.rollback()


@pytest.fixture
def db(c):
    with c.session_factory() as s:
        yield s


_WATCHER = "bridgeleads_watcher"


@pytest.fixture(scope="module", autouse=True)
def _watcher_role():
    """The watcher's role, with the migration's own grants and policies, on the TEST
    cluster (tests/_db_safety pins this run to a *_test database). Roles are cluster-wide,
    so it is created once and kept; NOLOGIN here: tests act as it through SET ROLE from a
    superuser login, which the triggers allow (production logs in as it)."""
    with sync_engine.begin() as conn:
        conn.execute(text(f"DO $r$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_WATCHER}') "
                          f"THEN CREATE ROLE {_WATCHER} NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $r$"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {_WATCHER}"))
        for table, role in migration_173.ROLE_POLICIES:
            if role == _WATCHER:
                conn.execute(text(migration_173.role_sql(table, role)))
    yield


@contextmanager
def _as_watcher(db):
    """Run the block as the watcher (SET LOCAL ROLE inside a savepoint: a failure rolls the
    savepoint back, which also restores the role)."""
    sp = db.begin_nested()
    db.execute(text(f"SET LOCAL ROLE {_WATCHER}"))
    try:
        yield
    except BaseException:
        sp.rollback()
        raise
    db.execute(text("RESET ROLE"))
    sp.commit()


def claim_watch(db, **kw):
    with _as_watcher(db):
        return store.claim_watch(db, **kw)


def record_observation(db, **kw):
    with _as_watcher(db):
        return store.record_observation(db, **kw)


def open_version(db, *, county: str, state: str, source_url: str, sha256: str) -> tuple[str, bool]:
    """What the watcher does when it confirms the county serves these bytes: claim the
    county, then record the copy (the only way a version comes to exist)."""
    token = claim_watch(db, county=county, state=state, lease_seconds=600)
    assert token is not None
    seen = record_observation(db, county=county, state=state, token=token, source_url=source_url,
                              link_year=2026, sha256=sha256)
    return seen["version_id"], seen["created"]


def _digest(tag: str) -> str:
    return hashlib.sha256(tag.encode()).hexdigest()


def _sha(db, vid: str) -> str:
    """The digest of the operator's downloaded copy, when it is this version's copy."""
    return db.get(Version, vid).sha256


def _version(db, tag: str, *, pages: dict[int, int] | None = None, case: str = _CASE,
             new_case: bool = False) -> str:
    vid, created = open_version(db, county="Douglas", state="wa", source_url=_URL, sha256=_digest(tag))
    assert created
    set_manifest(db, vid, amount_as_of=_AS_OF, case_number=case, rows_per_page=pages or {6: 2}, actor="op-1", pdf_sha256=_sha(db, vid),
                 new_case=new_case)
    return vid


def _row(page: int, row: int, parcel: str | None, eligibility: str = "eligible", **kw) -> dict:
    base = {"page": page, "row": row, "parcel": parcel, "eligibility": eligibility, "confirmed": True}
    if eligibility == "eligible":
        base |= {"vested_owner": "OWNER PLACEHOLDER", "taxpayer": "TAXPAYER PLACEHOLDER",
                 "property_address": "1 PLACEHOLDER RD, EAST WENATCHEE, WA 98802",
                 "mailing_address": "PO BOX 0, PLACEHOLDER, WA 98000",
                 "amount": "$4,218.07", "tax_years": [2022, 2023]}
    return base | kw


def _payload(vid: str, *rows: dict, manifest: dict | None = None, case: str = _CASE,
             as_of: date = _AS_OF) -> dict:
    return {"version": vid, "case": case, "as_of": as_of.isoformat() if as_of else None, "manifest": manifest or {"6": 2},
            "rows": list(rows)}


def _load(db, vid, *rows, actor="op-1") -> int:
    v = db.get(Version, vid)
    return load_rows(db, vid, _payload(vid, *rows, manifest=v.manifest, case=v.case_number, as_of=v.amount_as_of),
                     actor=actor)[0]


def _publish(db, vid, rows_a=_A, rows_b=_B, b="eligible", **kw) -> int:
    rev = _load(db, vid, _row(6, 1, rows_a), _row(6, 2, rows_b, b, **kw))
    publish(db, vid, actor="op-1", expected_revision=rev, pdf_sha256=_sha(db, vid))
    return rev


# ── versions and the manifest ──

def test_digests_get_a_seen_order_and_a_repeat_only_moves_last_checked(db):
    v1, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
    v2, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v2"))
    first = db.get(Version, v1).last_checked_at
    assert open_version(db, county="douglas", state="WA", source_url=_URL,
                        sha256=_digest("v1").upper()) == (v1, False)
    a, b = db.get(Version, v1), db.get(Version, v2)
    assert (a.county, a.state, a.status, a.revision) == ("douglas", "WA", "pending", 0)
    assert b.seen_order == a.seen_order + 1 and a.last_checked_at >= first


@pytest.mark.parametrize("kw,match", [
    ({"county": "chelan"}, "no checked summons format"),
    ({"sha256": "not-a-digest"}, "hex digest"),
])
def test_open_version_refuses_unknown_counties_and_bad_digests(db, kw, match):
    args = {"county": "douglas", "state": "WA", "source_url": _URL, "sha256": _digest("x")} | kw
    with pytest.raises(TaxReviewError, match=match):
        open_version(db, **args)


@pytest.mark.parametrize("case,pages,match", [
    ("26-2-337-09", {6: 2}, "case number"),
    ("", {6: 2}, "case number"),
    (_CASE, {}, "page:rows"),
    (_CASE, {6: 0}, "page:rows"),
    (_CASE, {6: 51}, "page:rows"),
    (_CASE, {0: 2}, "page:rows"),
])
def test_the_manifest_is_validated(db, case, pages, match):
    vid, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
    with pytest.raises(TaxReviewError, match=match):
        set_manifest(db, vid, amount_as_of=_AS_OF, case_number=case, rows_per_page=pages, actor="op-1", pdf_sha256=_sha(db, vid))


@pytest.mark.parametrize("as_of", [date(1999, 12, 31), date(2999, 1, 1), "2026-08-01"])
def test_the_as_of_date_is_the_summons_own(db, as_of):
    vid, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
    with pytest.raises(TaxReviewError, match="as-of date"):
        set_manifest(db, vid, amount_as_of=as_of, case_number=_CASE, rows_per_page={6: 2}, actor="op-1",
                     pdf_sha256=_sha(db, vid))


def test_a_draft_from_before_an_as_of_correction_is_refused(db):
    vid = _version(db, "v1")
    stale = _payload(vid, _row(6, 1, _A), _row(6, 2, _B))
    set_manifest(db, vid, amount_as_of=date(2026, 8, 3), case_number=_CASE, rows_per_page={6: 2}, actor="op-1",
                 pdf_sha256=_sha(db, vid))
    with pytest.raises(TaxReviewError, match="different case, as-of date or manifest"):
        load_rows(db, vid, stale, actor="op-1")


def test_the_as_of_date_is_sealed_with_the_publication(db):
    vid = _version(db, "v1")
    _publish(db, vid)
    _refused(db, "UPDATE tax_summons_versions SET amount_as_of = '2026-09-01' WHERE id = :v", {"v": vid})
    v2 = _version(db, "v2")
    _refused(db, "UPDATE tax_summons_versions SET amount_as_of = '2026-09-01' WHERE id = :v", {"v": v2})  # no new revision
    db.execute(text("UPDATE tax_summons_versions SET amount_as_of = NULL, revision = revision + 1 WHERE id = :v"),
               {"v": v2})  # a pending copy may lose its date while under review...
    _raw_eligible_rows(db, v2)
    _refused(db, _RAW_PUBLISH, {"v": v2}, match="ck_tax_summons_versions_published")  # ...but not be published


def test_a_new_manifest_discards_loaded_rows(db):
    vid = _version(db, "v1")
    _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    rev = set_manifest(db, vid, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={6: 2, 7: 1}, actor="op-1", pdf_sha256=_sha(db, vid))
    with pytest.raises(TaxReviewError, match="no rows at this revision"):
        publish(db, vid, actor="op-1", expected_revision=rev, pdf_sha256=_sha(db, vid))


# ── load ──

def test_load_and_publish_the_first_version(db):
    vid = _version(db, "v1", pages={6: 2, 7: 2})
    rev, counts = load_rows(db, vid, _payload(vid, _row(6, 1, _A), _row(6, 2, "4102 0600-200", "redeemed"),
                                              _row(7, 1, None, "unreadable"),
                                              _row(7, 2, _C, "excluded", reason="duplicate"),
                                              manifest={"6": 2, "7": 2}), actor="op-1")
    assert counts == {"eligible": 1, "redeemed": 1, "unreadable": 1, "excluded": 1}
    r = db.execute(select(Row).where(Row.version_id == vid, Row.page == 6, Row.row_no == 1)).scalar_one()
    assert (r.parcel_id, r.amount_due, r.tax_years, r.entered_by, r.revision) == (
        _A, Decimal("4218.07"), [2022, 2023], "op-1", rev)
    assert publish(db, vid, actor="op-1", expected_revision=rev, pdf_sha256=_sha(db, vid)) is None
    v = db.get(Version, vid)
    assert (v.status, v.published_by, v.published_revision) == ("published", "op-1", rev)
    actions = db.execute(select(Event.action).where(Event.version_id == vid)).scalars().all()
    assert actions.count("entered") == 4 and {"manifest", "loaded", "published"} <= set(actions)


def test_events_carry_codes_and_parcels_never_names(db):
    vid = _version(db, "v1")
    _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    for e in db.execute(select(Event).where(Event.version_id == vid)).scalars():
        assert "PLACEHOLDER" not in " ".join(str(getattr(e, col.name)) for col in Event.__table__.columns)


_BAD = [
    ({"confirmed": False}, "not confirmed"),
    ({"confirmed": "yes"}, "not confirmed"),
    ({"eligibility": "paid"}, "eligibility must be"),
    ({"parcel": "4102060010"}, "parcel format"),
    ({"parcel": "４１０２０６００１００"}, "parcel format"),
    ({"parcel": 41020600100}, "parcel format"),
    ({"amount": None}, "needs amount"),
    ({"amount": "-5"}, "dollars and cents like"),
    ({"amount": "NaN"}, "dollars and cents like"),
    ({"amount": "12.345"}, "dollars and cents like"),
    ({"amount": "12,34"}, "dollars and cents like"),
    ({"amount": "1,2345.00"}, "dollars and cents like"),
    ({"amount": "$$12"}, "dollars and cents like"),
    ({"amount": 4218.07}, "dollars and cents like"),
    ({"amount": "0.00"}, "positive dollars"),
    ({"amount": True}, "not a number"),
    ({"tax_years": []}, "list of years"),
    ({"tax_years": [1850]}, "list of years"),
    ({"tax_years": ["2023"]}, "list of years"),
    ({"vested_owner": None}, "needs vested_owner"),
    ({"vested_owner": "x" * 513}, "up to 512"),
    ({"vested_owner": "A\x00B"}, "control character"),
    ({"vested_owner": "A\x1b[2KB"}, "control character"),   # a terminal escape
    ({"vested_owner": "A‮B"}, "control character"),     # a bidi override
    ({"vested_owner": "A\ud800B"}, "not valid text"),
    ({"reason": "duplicate"}, "only an excluded row"),
    ({"reinstate_reason": "because"}, "reinstate_reason is for"),
]


@pytest.mark.parametrize("change,match", _BAD)
def test_load_refuses_a_bad_row_and_writes_nothing(db, change, match):
    vid = _version(db, "v1")
    with pytest.raises(TaxReviewError, match=match):
        _load(db, vid, _row(6, 1, _A) | change, _row(6, 2, _B))
    assert db.execute(select(Row).where(Row.version_id == vid)).first() is None
    assert db.get(Version, vid).revision == 1


@pytest.mark.parametrize("rows,match", [
    ([_row(6, 1, _A)], "missing p6 r2"),
    ([_row(6, 1, _A), _row(6, 2, _B), _row(6, 3, _C)], "not in it p6 r3"),
    ([_row(6, 1, _A), _row(7, 1, _B)], "missing p6 r2; not in it p7 r1"),
    ([_row(6, 1, _A), _row(6, 1, _B)], "appear twice"),
    ([_row(6, 1, _A), _row(6, 2, _A, "redeemed")], "two rows"),
    ([_row(6, 1, _A), _row(6, 2, None, "redeemed")], "a redeemed row needs its parcel"),
    ([_row(6, 1, _A), _row(6, 2, None, "excluded")], "needs a reason"),
])
def test_load_must_account_for_exactly_the_manifest_rows(db, rows, match):
    vid = _version(db, "v1")
    with pytest.raises(TaxReviewError, match=match):
        _load(db, vid, *rows)


@pytest.mark.parametrize("payload,match", [
    (lambda vid: _payload(vid), "no rows"),
    (lambda vid: _payload(str(uuid.uuid4()), _row(6, 1, _A)), "different version"),
    (lambda vid: _payload(vid, _row(6, 1, _A)) | {"extra": 1}, "exactly"),
    (lambda vid: {"version": vid, "rows": [_row(6, 1, _A)]}, "exactly"),
    (lambda vid: _payload(vid, _row(6, 1, _A), _row(6, 2, _B), manifest={"6": 2, "7": 1}), "different case, as-of date or manifest"),
    (lambda vid: _payload(vid, _row(6, 1, _A), _row(6, 2, _B), case="26-2-00338-09"), "different case, as-of date or manifest"),
])
def test_load_refuses_a_bad_file(db, payload, match):
    vid = _version(db, "v1")
    with pytest.raises(TaxReviewError, match=match):
        load_rows(db, vid, payload(vid), actor="op-1")


def test_an_unknown_field_is_counted_never_echoed(db):
    vid = _version(db, "v1")
    with pytest.raises(TaxReviewError) as exc:
        _load(db, vid, _row(6, 1, _A) | {"JANE PLACEHOLDER DOE": "x"}, _row(6, 2, _B))
    assert "1 unknown field" in str(exc.value) and "PLACEHOLDER" not in str(exc.value)


def test_a_load_needs_a_manifest(db):
    vid, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
    with pytest.raises(TaxReviewError, match="no manifest"):
        _load(db, vid, _row(6, 1, _A))


def test_a_reload_adds_a_revision_and_keeps_the_earlier_rows(db):
    vid = _version(db, "v1")
    first = _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    second = _load(db, vid, _row(6, 1, _A), _row(6, 2, _B, "redeemed"), actor="op-2")
    assert second == first + 1
    by_rev = dict(db.execute(text(
        "SELECT revision, count(*) FROM tax_summons_rows WHERE version_id = :v GROUP BY revision"),
        {"v": vid}).tuples().all())
    assert by_rev == {first: 2, second: 2}


def test_a_published_version_takes_no_rows_or_manifest(db):
    vid = _version(db, "v1")
    _publish(db, vid)
    with pytest.raises(TaxReviewError, match="only a pending version"):
        _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    with pytest.raises(TaxReviewError, match="only a pending version"):
        set_manifest(db, vid, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={6: 2}, actor="op-1", pdf_sha256=_sha(db, vid))


@pytest.mark.parametrize("bad_id", ["not-a-uuid", str(uuid.uuid4())])
def test_an_unknown_version_is_refused_cleanly(db, bad_id):
    for call in (lambda: load_rows(db, bad_id, {}, actor="op"),
                 lambda: publish(db, bad_id, actor="op", expected_revision=1, pdf_sha256="0" * 64),
                 lambda: set_manifest(db, bad_id, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={6: 1}, actor="op",
                                      pdf_sha256="0" * 64),
                 lambda: draft_rows(db, bad_id)):
        with pytest.raises(TaxReviewError, match="no summons version"):
            call()


# ── the county's redemption stamp ──

def test_a_parcel_stamped_redeemed_stays_out_unless_reinstated(db):
    _publish(db, _version(db, "v1"), b="redeemed")
    v2 = _version(db, "v2")
    with pytest.raises(TaxReviewError, match="stamped this parcel redeemed"):
        _load(db, v2, _row(6, 1, _A), _row(6, 2, _B))
    _load(db, v2, _row(6, 1, _A), _row(6, 2, _B, reinstate_reason="redemption_reversed"), actor="op-2")
    e = db.execute(select(Event).where(Event.version_id == v2, Event.action == "reinstated")).scalar_one()
    assert (e.parcel_id, e.reason, e.actor) == (_B, "redemption_reversed", "op-2")


def test_a_published_reinstatement_holds_until_the_county_redeems_again(db):
    _publish(db, _version(db, "v1"), b="redeemed")
    _publish(db, _version(db, "v2"), reinstate_reason="redemption_reversed")
    v3 = _version(db, "v3")
    _publish(db, v3)  # no second reinstatement needed: the latest published decision is eligible
    v4 = _version(db, "v4")
    _publish(db, v4, b="redeemed")
    v5 = _version(db, "v5")
    with pytest.raises(TaxReviewError, match="stamped this parcel redeemed"):
        _load(db, v5, _row(6, 1, _A), _row(6, 2, _B))


def test_a_reload_cannot_drop_a_needed_reinstatement_and_the_published_row_keeps_it(db):
    _publish(db, _version(db, "v1"), b="redeemed")
    v2 = _version(db, "v2")
    rev = _load(db, v2, _row(6, 1, _A), _row(6, 2, _B, reinstate_reason="redemption_reversed"))
    with pytest.raises(TaxReviewError, match="stamped this parcel redeemed"):
        _load(db, v2, _row(6, 1, _A), _row(6, 2, _B))
    publish(db, v2, actor="op-1", expected_revision=rev, pdf_sha256=_sha(db, v2))
    assert db.execute(select(Row.reinstate_reason).where(
        Row.version_id == v2, Row.revision == rev, Row.parcel_id == _B)).scalar() == "redemption_reversed"


def test_reinstating_a_parcel_never_redeemed_is_refused(db):
    _publish(db, _version(db, "v1"), b="redeemed")
    v2 = _version(db, "v2")
    with pytest.raises(TaxReviewError, match="nothing to reinstate"):
        _load(db, v2, _row(6, 1, _A, reinstate_reason="entered_in_error"), _row(6, 2, _B, "redeemed"))


def test_a_later_years_case_starts_a_new_redemption_history(db):
    _publish(db, _version(db, "v1"), b="redeemed")
    v2 = _version(db, "2027", case="27-2-00001-09", new_case=True)
    _load(db, v2, _row(6, 1, _A), _row(6, 2, _B))
    e = db.execute(select(Event).where(Event.version_id == v2, Event.action == "manifest")).scalar_one()
    assert e.reason == "new_case"
    assert draft_rows(db, v2)["rows"] == []  # the old case's rows are not this case's draft


@pytest.mark.parametrize("case,new_case,match", [
    ("26-2-00338-09", False, "summons is case 26-2-00337-09"),   # a typo of the case in hand
    ("26-2-00338-09", True, "later year"),                       # a "new" case in the same year
    ("25-2-00001-09", True, "later year"),
])
def test_a_reupload_keeps_its_case(db, case, new_case, match):
    _publish(db, _version(db, "v1"), b="redeemed")
    v2, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v2"))
    with pytest.raises(TaxReviewError, match=match):
        set_manifest(db, v2, amount_as_of=_AS_OF, case_number=case, rows_per_page={6: 2}, actor="op-1", pdf_sha256=_sha(db, v2),
                     new_case=new_case)


def test_manifest_and_publish_refuse_a_different_copy_of_the_pdf(db):
    vid, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
    with pytest.raises(TaxReviewError, match="not this version's copy"):
        set_manifest(db, vid, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={6: 2}, actor="op-1", pdf_sha256=_digest("v2"))
    set_manifest(db, vid, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={6: 2}, actor="op-1", pdf_sha256=_digest("v1"))
    rev = _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    with pytest.raises(TaxReviewError, match="not this version's copy"):
        publish(db, vid, actor="op-1", expected_revision=rev, pdf_sha256=_digest("v2"))
    assert db.get(Version, vid).status == "pending"


def test_a_draft_needs_a_manifest(db):
    vid, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
    with pytest.raises(TaxReviewError, match="no manifest"):
        draft_rows(db, vid)


def test_a_redemption_in_an_unpublished_load_does_not_bind(db):
    v1 = _version(db, "v1")
    _load(db, v1, _row(6, 1, _A), _row(6, 2, _B, "redeemed"))
    v2 = _version(db, "v2")
    _load(db, v2, _row(6, 1, _A), _row(6, 2, _B))


# ── draft ──

def test_draft_prefills_the_last_published_rows_unconfirmed(db):
    v1 = _version(db, "v1")
    _publish(db, v1, b="redeemed")
    v2 = _version(db, "v2")
    draft = draft_rows(db, v2)
    assert draft["version"] == v2 and [r["parcel"] for r in draft["rows"]] == [_A, _B]
    assert all(r["confirmed"] is False for r in draft["rows"])
    with pytest.raises(TaxReviewError, match="not confirmed"):
        load_rows(db, v2, draft, actor="op-1")
    confirmed = copy.deepcopy(draft)
    for r in confirmed["rows"]:
        r["confirmed"] = True
    assert load_rows(db, v2, confirmed, actor="op-1")[1] == {"eligible": 1, "redeemed": 1, "unreadable": 0,
                                                             "excluded": 0}
    assert draft_rows(db, v1)["rows"] == []  # nothing was published before v1


# ── publish ──

def test_publish_supersedes_the_previous_and_older_pending_versions(db):
    v1 = _version(db, "v1")
    _publish(db, v1, b="redeemed")
    v_old_pending = _version(db, "v2")
    v3 = _version(db, "v3")
    rev = _load(db, v3, _row(6, 1, _A), _row(6, 2, _B, "redeemed"))
    assert publish(db, v3, actor="op-2", expected_revision=rev, pdf_sha256=_sha(db, v3)) == v1
    assert [db.get(Version, v).status for v in (v1, v_old_pending, v3)] == ["superseded", "superseded", "published"]


def test_publish_refuses_when_a_newer_copy_was_seen(db):
    v1 = _version(db, "v1")
    rev = _load(db, v1, _row(6, 1, _A), _row(6, 2, _B))
    _version(db, "v2")
    with pytest.raises(TaxReviewError, match="serves now"):
        publish(db, v1, actor="op-1", expected_revision=rev, pdf_sha256=_sha(db, v1))
    assert db.get(Version, v1).status == "pending"


def test_publish_names_the_revision_it_approves(db):
    vid = _version(db, "v1")
    seen = _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    _load(db, vid, _row(6, 1, _A), _row(6, 2, _B, "redeemed"), actor="op-2")
    with pytest.raises(TaxReviewError, match="rows changed since you looked"):
        publish(db, vid, actor="op-1", expected_revision=seen, pdf_sha256=_sha(db, vid))
    assert db.get(Version, vid).status == "pending"


def test_publish_refuses_an_empty_version(db):
    vid = _version(db, "v1")
    with pytest.raises(TaxReviewError, match="no rows"):
        publish(db, vid, actor="op-1", expected_revision=1, pdf_sha256=_sha(db, vid))


# ── the database's own guards ──

def test_the_database_holds_one_published_version_per_county(db):
    v1 = _version(db, "v1")
    _publish(db, v1)
    v2 = _version(db, "v2")
    rev = _load(db, v2, _row(6, 1, _A), _row(6, 2, _B))
    with pytest.raises(DBAPIError, match="uq_tax_summons_versions_one_published"):
        with db.begin_nested():
            db.execute(text("UPDATE tax_summons_versions SET status = 'published', published_by = 'x', "
                            "published_at = now(), published_revision = :r WHERE id = :i"), {"i": v2, "r": rev})


_RAW_PUBLISH = ("UPDATE tax_summons_versions SET status = 'published', published_by = 'x', published_at = now(), "
                "published_revision = revision WHERE id = :v")


def test_a_direct_publish_of_an_empty_or_partial_revision_is_refused(db):
    vid = _version(db, "v1")
    _refused(db, _RAW_PUBLISH, {"v": vid}, match="exactly the manifest")  # manifest only, no rows
    db.execute(text("INSERT INTO tax_summons_rows (id, version_id, revision, page, row_no, eligibility, entered_by) "
                    "SELECT gen_random_uuid(), id, revision, 6, 1, 'unreadable', 'x' FROM tax_summons_versions "
                    "WHERE id = :v"), {"v": vid})
    _refused(db, _RAW_PUBLISH, {"v": vid}, match="exactly the manifest")  # row 2 missing


def test_a_direct_publish_of_an_older_copy_is_refused(db):
    v1 = _version(db, "v1")
    _load(db, v1, _row(6, 1, _A), _row(6, 2, _B))
    _version(db, "v2")
    _refused(db, _RAW_PUBLISH, {"v": v1}, match="serves now")


def _raw_eligible_rows(db, vid: str, reinstate: str | None = None) -> None:
    """Rows typed straight into a new current revision, skipping the review module."""
    db.execute(text("UPDATE tax_summons_versions SET revision = revision + 1 WHERE id = :v"), {"v": vid})
    for row_no, parcel in ((1, _A), (2, _B)):
        db.execute(text("INSERT INTO tax_summons_rows (id, version_id, revision, page, row_no, parcel_id, "
                        "amount_due, tax_years, vested_owner, eligibility, reinstate_reason, entered_by) "
                        "SELECT gen_random_uuid(), id, revision, 6, :n, :p, 10, '[2023]', 'OWNER', 'eligible', "
                        ":r, 'x' FROM tax_summons_versions WHERE id = :v"),
                   {"v": vid, "n": row_no, "p": parcel, "r": reinstate if parcel == _B else None})


def test_a_direct_publish_cannot_revive_a_redeemed_parcel(db):
    _publish(db, _version(db, "v1"), b="redeemed")
    v2 = _version(db, "v2")
    _raw_eligible_rows(db, v2)
    _refused(db, _RAW_PUBLISH, {"v": v2}, match="reinstatement does not match")


def test_a_direct_publish_cannot_reinstate_a_parcel_never_redeemed(db):
    _publish(db, _version(db, "v1"))
    v2 = _version(db, "v2")
    _raw_eligible_rows(db, v2, reinstate="redemption_reversed")
    _refused(db, _RAW_PUBLISH, {"v": v2}, match="reinstatement does not match")


def test_a_direct_publish_cannot_swap_the_case(db):
    _publish(db, _version(db, "v1"), b="redeemed")
    v2 = _version(db, "v2")
    _raw_eligible_rows(db, v2)
    _refused(db, "UPDATE tax_summons_versions SET status = 'published', published_by = 'x', published_at = now(), "
                 "published_revision = revision, case_number = '27-2-00001-09' WHERE id = :v", {"v": v2},
             match="sealed")


def test_a_case_or_manifest_edit_must_start_a_new_revision_and_keep_the_case(db):
    _publish(db, _version(db, "v1"))
    v2 = _version(db, "v2")
    _refused(db, "UPDATE tax_summons_versions SET manifest = '{\"6\": 3}' WHERE id = :v", {"v": v2}, match="sealed")
    _refused(db, "UPDATE tax_summons_versions SET case_number = '26-2-00338-09', revision = revision + 1 "
                 "WHERE id = :v", {"v": v2}, match="the summons is case 26-2-00337-09")
    with db.begin_nested():  # a later year's case is allowed
        db.execute(text("UPDATE tax_summons_versions SET case_number = '27-2-00001-09', revision = revision + 1 "
                        "WHERE id = :v"), {"v": v2})


def test_a_mistaken_new_case_on_a_pending_copy_does_not_become_the_rule(db):
    _publish(db, _version(db, "v1"), b="redeemed")
    _version(db, "v2", case="27-2-00001-09", new_case=True)   # a mistake, never published
    v3, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v3"))
    with pytest.raises(TaxReviewError, match="summons is case 26-2-00337-09"):
        set_manifest(db, v3, amount_as_of=_AS_OF, case_number="27-2-00001-09", rows_per_page={6: 2}, actor="op-1",
                     pdf_sha256=_sha(db, v3))
    set_manifest(db, v3, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={6: 2}, actor="op-1", pdf_sha256=_sha(db, v3))
    with pytest.raises(TaxReviewError, match="stamped this parcel redeemed"):  # case 26's history still binds
        _load(db, v3, _row(6, 1, _A), _row(6, 2, _B))


def test_an_older_copy_takes_no_manifest(db):
    v1, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
    _version(db, "v2")
    with pytest.raises(TaxReviewError, match="serves now"):
        set_manifest(db, v1, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={6: 2}, actor="op-1", pdf_sha256=_sha(db, v1))
    _refused(db, "UPDATE tax_summons_versions SET case_number = :c, manifest = '{\"6\": 2}', "
                 "revision = revision + 1 WHERE id = :v", {"v": v1, "c": _CASE}, match="serves now")


def test_a_page_past_9999_is_refused_by_review(db):
    vid, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
    with pytest.raises(TaxReviewError, match="pages 1-9999"):
        set_manifest(db, vid, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={10000: 1}, actor="op-1", pdf_sha256=_sha(db, vid))


def test_only_a_newer_publication_supersedes_a_copy(db):
    vid = _version(db, "v1")
    db.execute(text("SET CONSTRAINTS tax_summons_versions_superseded IMMEDIATE"))
    _refused(db, "UPDATE tax_summons_versions SET status = 'superseded' WHERE id = :v", {"v": vid},
             match="superseded only by another published one")
    db.execute(text("SET CONSTRAINTS tax_summons_versions_superseded DEFERRED"))  # as at commit
    rev = _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    publish(db, vid, actor="op-1", expected_revision=rev, pdf_sha256=_sha(db, vid))
    v2 = _version(db, "v2")
    publish(db, v2, actor="op-1", expected_revision=_load(db, v2, _row(6, 1, _A), _row(6, 2, _B)),
            pdf_sha256=_sha(db, v2))
    db.execute(text("SET CONSTRAINTS tax_summons_versions_superseded IMMEDIATE"))  # runs the pending checks
    assert db.get(Version, vid).status == "superseded"


def test_the_order_copies_were_seen_is_the_databases(db):
    v1 = _version(db, "v1")
    with _as_watcher(db):
        db.execute(text("INSERT INTO tax_summons_versions (id, county, state, source_url, sha256, seen_order) "
                        "VALUES (gen_random_uuid(), 'douglas', 'WA', 'u', :d, 1)"), {"d": _digest("v2")})
    orders = db.execute(text("SELECT sha256, seen_order FROM tax_summons_versions WHERE county = 'douglas' "
                             "ORDER BY seen_order")).all()
    assert [o.seen_order for o in orders] == [1, 2] and orders[1].sha256 == _digest("v2")
    # A copy that reached the table without the watcher is not the county's current copy.
    rev = _load(db, v1, _row(6, 1, _A), _row(6, 2, _B))
    publish(db, v1, actor="op-1", expected_revision=rev, pdf_sha256=_sha(db, v1))


@pytest.mark.parametrize("manifest", ['{"6": 2, "7": 0}', '{"6": 2, "6": 3}', '{"0": 1}', '{"06": 1}',
                                      '[1]', '{"6": "2"}', '{"6": 2.5}', '{"6": 51}', '{}'])
def test_the_database_refuses_a_malformed_manifest(db, manifest):
    vid = _version(db, "v1")
    _refused(db, "UPDATE tax_summons_versions SET manifest = CAST(:m AS json), revision = revision + 1 "
                 "WHERE id = :v", {"v": vid, "m": manifest}, match="maps pages to row counts")


def test_review_writes_need_read_committed(committed):
    with Session(sync_engine) as s:
        vid = _version(s, "v1")
        _load(s, vid, _row(6, 1, _A), _row(6, 2, _B))
        s.commit()
    with Session(sync_engine) as other:
        other.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        with pytest.raises(DBAPIError, match="READ COMMITTED"):
            other.execute(text(_RAW_PUBLISH), {"v": vid})
        other.rollback()
    with Session(sync_engine) as s:
        assert s.get(Version, vid).status == "pending"


def test_the_publication_time_is_the_databases(db):
    vid = _version(db, "v1")
    rev = _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    db.execute(text("UPDATE tax_summons_versions SET status = 'published', published_by = 'x', "
                    "published_at = '2100-01-01', published_revision = :r WHERE id = :v"), {"v": vid, "r": rev})
    assert db.execute(text("SELECT published_at < now() + interval '1 minute' FROM tax_summons_versions "
                           "WHERE id = :v"), {"v": vid}).scalar()


def test_a_later_redemption_outranks_an_earlier_eligible_copy(db):
    """Decisions are ordered by the order copies were seen, not by their timestamps."""
    _publish(db, _version(db, "v1"))                 # eligible
    _publish(db, _version(db, "v2"), b="redeemed")   # the county stamps it
    v3 = _version(db, "v3")
    with pytest.raises(TaxReviewError, match="stamped this parcel redeemed"):
        _load(db, v3, _row(6, 1, _A), _row(6, 2, _B))


def _watcher_writes(sql: str) -> bool:
    """Observation writes and new copies are the watcher's alone; a refusal test of any
    other rule must run as the watcher, or the identity check would answer first."""
    return "tax_summons_observations" in sql or sql.startswith("INSERT INTO tax_summons_versions")


def _refused(db, sql: str, params: dict, match: str = "sealed|only changes by erasure", *,
             as_watcher: bool | None = None) -> None:
    with pytest.raises(DBAPIError, match=match):
        with db.begin_nested():
            if _watcher_writes(sql) if as_watcher is None else as_watcher:
                db.execute(text(f"SET LOCAL ROLE {_WATCHER}"))
            db.execute(text(sql), params)


def test_a_published_version_and_its_rows_are_sealed(db):
    vid = _version(db, "v1")
    rev = _publish(db, vid)
    p = {"v": vid, "r": rev}
    _refused(db, "INSERT INTO tax_summons_rows (id, version_id, revision, page, row_no, eligibility, entered_by) "
                 "VALUES (gen_random_uuid(), :v, :r, 9, 9, 'unreadable', 'x')", p)
    for change in ("published_revision = 1", "case_number = '26-2-00338-09'", "manifest = '{\"6\": 3}'",
                   "revision = revision + 1", "published_by = 'someone'", "status = 'pending'",
                   "sha256 = repeat('0', 64)"):
        _refused(db, f"UPDATE tax_summons_versions SET {change} WHERE id = :v", p)
    _refused(db, "UPDATE tax_summons_rows SET amount_due = 1 WHERE version_id = :v", p)
    _refused(db, "UPDATE tax_summons_rows SET vested_owner = 'SOMEONE ELSE' WHERE version_id = :v", p)
    _refused(db, "UPDATE tax_summons_rows SET vested_owner = NULL WHERE version_id = :v", p)  # not via erasure
    with db.begin_nested():  # what the watcher does to a known digest still goes through
        db.execute(text("UPDATE tax_summons_versions SET last_checked_at = now() WHERE id = :v"), p)


def test_rows_go_only_into_a_pending_versions_current_revision(db):
    vid = _version(db, "v1")
    first = _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    _refused(db, "INSERT INTO tax_summons_rows (id, version_id, revision, page, row_no, eligibility, entered_by) "
                 "VALUES (gen_random_uuid(), :v, :r, 9, 9, 'unreadable', 'x')", {"v": vid, "r": first})


@pytest.mark.parametrize("table", ["tax_summons_versions", "tax_summons_rows", "tax_summons_events"])
def test_review_records_are_never_deleted(db, table):
    vid = _version(db, "v1")
    _publish(db, vid)
    column = "id" if table == "tax_summons_versions" else "version_id"
    _refused(db, f"DELETE FROM {table} WHERE {column} = :v", {"v": vid}, match="never deleted")
    _refused(db, f"TRUNCATE {table} CASCADE", {}, match="never deleted")


def test_a_short_douglas_parcel_is_refused_at_the_database(db):
    vid = _version(db, "v1")
    _refused(db, "INSERT INTO tax_summons_rows (id, version_id, revision, page, row_no, parcel_id, eligibility, "
                 "entered_by) SELECT gen_random_uuid(), id, revision, 6, 1, '1234', 'redeemed', 'x' "
                 "FROM tax_summons_versions WHERE id = :v", {"v": vid}, match="11 digits")


def test_the_audit_is_never_rewritten(db):
    vid = _version(db, "v1")
    _refused(db, "UPDATE tax_summons_events SET actor = 'someone' WHERE version_id = :v", {"v": vid},
             match="append-only")


def test_a_copy_names_a_plain_county_and_state(db):
    _refused(db, "INSERT INTO tax_summons_versions (id, county, state, source_url, sha256, seen_order) "
                 "VALUES (gen_random_uuid(), :c, 'WA', 'u', repeat('b', 64), 1)",
             {"c": "douglas" + chr(27) + "[2K"}, match="ck_tax_summons_versions_place")


def test_a_case_number_in_other_digits_is_refused_plainly(db):
    vid, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
    arabic_indic = "".join(chr(0x660 + int(d)) if d.isdigit() else d for d in _CASE)
    with pytest.raises(TaxReviewError, match="case number must look like"):
        set_manifest(db, vid, amount_as_of=_AS_OF, case_number=arabic_indic, rows_per_page={6: 2}, actor="op-1", pdf_sha256=_sha(db, vid))


def test_a_reinstated_event_needs_its_eligibility(db):
    vid = _version(db, "v1")
    _refused(db, "INSERT INTO tax_summons_events (id, version_id, revision, action, parcel_id, reason, actor) "
                 "VALUES (gen_random_uuid(), :v, 1, 'reinstated', '41020600200', 'redemption_reversed', 'x')",
             {"v": vid}, match="ck_tax_summons_events_shape")


@pytest.mark.parametrize("columns,values", [
    ("status, published_by, published_at, published_revision, case_number",
     "'published', 'x', now(), 1, '26-2-00337-09'"),
    ("revision", "3"),
    ("case_number", "'26-2-00337-09'"),
    ("manifest", "'{\"6\": 2}'"),
])
def test_a_new_copy_cannot_be_inserted_already_reviewed(db, columns, values):
    _refused(db, f"INSERT INTO tax_summons_versions (id, county, state, source_url, sha256, seen_order, {columns}) "
                 f"VALUES (gen_random_uuid(), 'douglas', 'WA', 'u', repeat('a', 64), 99, {values})", {},
             match="starts pending and unreviewed")


def test_publication_must_name_the_current_revision(db):
    vid = _version(db, "v1")
    rev = _load(db, vid, _row(6, 1, _A), _row(6, 2, _B))
    _refused(db, "UPDATE tax_summons_versions SET status = 'published', published_by = 'x', published_at = now(), "
                 "published_revision = :r WHERE id = :v", {"v": vid, "r": rev - 1})


_BAD_SQL = [
    ("'eligible', NULL, 1, '[2023]', 'x'", "ck_tax_summons_rows_eligible_complete"),     # no parcel
    ("'eligible', '41020600100', NULL, '[2023]', 'x'", "ck_tax_summons_rows_eligible_complete"),  # no amount
    ("'eligible', '41020600100', 1, NULL, 'x'", "ck_tax_summons_rows_eligible_complete"),  # no years
    ("'eligible', '41020600100', 1, '[]', 'x'", "ck_tax_summons_rows_tax_years"),
    ("'eligible', '41020600100', 1, 'null', 'x'", "ck_tax_summons_rows_tax_years"),
    ("'eligible', '41020600100', 1, '{\"a\": 1}', 'x'", "ck_tax_summons_rows_tax_years"),
    ("'eligible', '41020600100', 1, '[null]', 'x'", "ck_tax_summons_rows_tax_years"),
    ("'eligible', '41020600100', 1, '[\"2023\"]', 'x'", "ck_tax_summons_rows_tax_years"),
    ("'eligible', '41020600100', 1, '[2023.5]', 'x'", "ck_tax_summons_rows_tax_years"),
    ("'eligible', '41020600100', 1, '[1850]', 'x'", "ck_tax_summons_rows_tax_years"),
    ("'eligible', '4102 06', 1, '[2023]', 'x'", "11 digits|ck_tax_summons_rows_parcel"),
    ("'unreadable', NULL, -1, NULL, 'x'", "ck_tax_summons_rows_amount"),
    ("'eligible', '41020600100', 'NaN', '[2023]', 'x'", "ck_tax_summons_rows_amount"),
    ("'eligible', '41020600100', 10000000.01, '[2023]', 'x'", "ck_tax_summons_rows_amount"),
    ("'redeemed', NULL, NULL, NULL, 'x'", "ck_tax_summons_rows_redeemed_parcel"),
]


@pytest.mark.parametrize("values,constraint", _BAD_SQL)
def test_the_database_refuses_an_incomplete_row(db, values, constraint):
    vid = _version(db, "v1")
    with pytest.raises(DBAPIError, match=constraint):
        with db.begin_nested():
            db.execute(text("INSERT INTO tax_summons_rows (id, version_id, revision, page, row_no, eligibility, "
                            f"parcel_id, amount_due, tax_years, entered_by) VALUES (gen_random_uuid(), :v, 1, "
                            f"1, 1, {values})"), {"v": vid})


@pytest.mark.parametrize("action,eligibility,reason,constraint", [
    ("deleted", None, None, "ck_tax_summons_events_action"),
    ("entered", "paid", None, "ck_tax_summons_events_eligibility"),
    ("published", None, "duplicate", "ck_tax_summons_events_reason"),
    ("entered", "excluded", "redemption_reversed", "ck_tax_summons_events_reason"),
    ("entered", None, None, "ck_tax_summons_events_shape"),
    ("reinstated", "eligible", "redemption_reversed", "ck_tax_summons_events_shape"),  # no parcel
    ("reinstated", None, None, "ck_tax_summons_events_shape"),
    ("manifest", None, "duplicate", "ck_tax_summons_events_reason"),
])
def test_the_database_refuses_an_unknown_audit_code(db, action, eligibility, reason, constraint):
    vid = _version(db, "v1")
    with pytest.raises(DBAPIError, match=constraint):
        with db.begin_nested():
            db.execute(text("INSERT INTO tax_summons_events (id, version_id, revision, action, eligibility, "
                            "reason, actor) VALUES (gen_random_uuid(), :v, 1, :a, :e, :r, 'x')"),
                       {"v": vid, "a": action, "e": eligibility, "r": reason})


def _as_system_role(db) -> None:
    """The migration's own grant + policy SQL, applied to a role created inside this
    transaction when the cluster has none (CI); the rollback removes both. The extra
    blanket grant is what scripts/provision_rls_roles.sql gives the role on every table."""
    db.execute(text("DO $r$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                    "'bridgeleads_system') THEN CREATE ROLE bridgeleads_system NOLOGIN NOBYPASSRLS; "
                    "END IF; END $r$"))
    db.execute(text("GRANT USAGE ON SCHEMA public TO bridgeleads_system"))
    for table, role in migration_173.ROLE_POLICIES:
        if role == "bridgeleads_system":
            db.execute(text(migration_173.role_sql(table, role)))


@pytest.mark.parametrize("provisioner_regrants", [False, True])
def test_the_system_role_reviews_but_never_rewrites_rows_or_audit(db, provisioner_regrants):
    vid = _version(db, "v1")
    _as_system_role(db)
    if provisioner_regrants:  # the provisioner re-run after the migration
        db.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON tax_summons_rows, tax_summons_events "
                        "TO bridgeleads_system"))
    with db.begin_nested():
        db.execute(text("SET LOCAL ROLE bridgeleads_system"))
        _publish(db, vid)
        db.execute(text("RESET ROLE"))
    for table in ("tax_summons_rows", "tax_summons_events"):
        for stmt in (f"UPDATE {table} SET entered_by = 'x'" if table == "tax_summons_rows"
                     else f"UPDATE {table} SET actor = 'x'", f"DELETE FROM {table}"):
            try:  # a savepoint rollback also undoes SET LOCAL ROLE
                with db.begin_nested():
                    db.execute(text("SET LOCAL ROLE bridgeleads_system"))
                    touched = db.execute(text(stmt)).rowcount
                    db.execute(text("RESET ROLE"))
            except DBAPIError as exc:
                assert "permission denied" in str(exc), stmt
                touched = 0
            assert touched == 0, stmt
    assert db.execute(text("SELECT count(*) FROM tax_summons_events WHERE version_id = :v AND actor = 'x'"),
                      {"v": vid}).scalar() == 0


# ── the watcher's observation: claim, fencing, holds, what may be served ──

def _expire_lease(db) -> None:
    """Move the current claim's lease into the past (test-only clock move)."""
    with db.begin_nested():
        db.execute(text("SET LOCAL session_replication_role = replica"))
        db.execute(text("UPDATE tax_summons_observations SET claim_expires_at = now() - interval '1 second' "
                        "WHERE county = 'douglas'"))


def _claim(db) -> int:
    token = claim_watch(db, county="douglas", state="WA", lease_seconds=600)
    assert token is not None
    return token


def _observe(db, tag: str, *, year: int = 2026) -> dict:
    return record_observation(db, county="douglas", state="WA", token=_claim(db), source_url=_URL,
                              link_year=year, sha256=_digest(tag))


def _serving(db) -> tuple[str, str | None]:
    return serving_status(db, county="douglas", state="WA")


def test_a_live_claim_keeps_a_second_watcher_out_until_it_records(db):
    token = _claim(db)
    assert claim_watch(db, county="douglas", state="WA", lease_seconds=600) is None
    record_observation(db, county="douglas", state="WA", token=token, hold="page_failed")
    assert _claim(db) == token + 1


def test_a_run_whose_claim_was_taken_over_cannot_record(db):
    stale = _claim(db)
    _expire_lease(db)  # a worker died mid-fetch
    fresh = _claim(db)
    assert fresh == stale + 1
    with pytest.raises(TaxReviewError, match="newer watcher run"):
        record_observation(db, county="douglas", state="WA", token=stale, source_url=_URL, link_year=2026,
                           sha256=_digest("late"))
    assert db.execute(text("SELECT count(*) FROM tax_summons_versions")).scalar() == 0


def test_what_may_be_served(db):
    assert _serving(db) == ("unobserved", None)
    vid = _observe(db, "v1")["version_id"]
    assert _serving(db) == ("unpublished", vid)
    set_manifest(db, vid, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={6: 2}, actor="op-1", pdf_sha256=_sha(db, vid))
    _publish(db, vid)
    assert _serving(db) == ("ok", vid)
    for hold in ("link_missing", "page_failed", "download_failed", "ambiguous"):
        record_observation(db, county="douglas", state="WA", token=_claim(db), hold=hold)
        assert _serving(db) == (hold, vid)  # the last confirmed copy is kept, not served
    assert _observe(db, "v1")["status"] == "ok"
    assert _serving(db) == ("ok", vid)


def test_a_confirmation_goes_stale(db):
    vid = _version(db, "v1")
    _publish(db, vid)
    with db.begin_nested():  # a day and a half with no confirmation (test-only clock move)
        db.execute(text("SET LOCAL session_replication_role = replica"))
        db.execute(text("UPDATE tax_summons_observations SET confirmed_at = now() - interval '37 hours' "
                        "WHERE county = 'douglas'"))
    assert _serving(db) == ("stale", vid)


def test_an_earlier_years_link_after_a_later_one_is_a_rollback_hold(db):
    vid = _version(db, "v1")
    _publish(db, vid)
    seen = _observe(db, "v0", year=2025)
    assert (seen["status"], seen["version_id"], seen["created"]) == ("rollback", vid, False)
    assert _serving(db) == ("rollback", vid)
    assert db.execute(text("SELECT count(*) FROM tax_summons_versions")).scalar() == 1


def test_the_county_serving_an_older_superseded_copy_holds(db):
    a = _version(db, "a")
    _publish(db, a)
    b = _version(db, "b")
    _publish(db, b)
    assert _observe(db, "a")["version_id"] == a
    assert _serving(db) == ("superseded", a)
    with pytest.raises(TaxReviewError, match="only a pending version"):
        publish(db, a, actor="op-1", expected_revision=1, pdf_sha256=_sha(db, a))


def test_a_published_copy_served_again_after_a_pending_one_is_served(db):
    a = _version(db, "a")
    _publish(db, a)
    _version(db, "b")  # pending, never published
    assert _serving(db)[0] == "unpublished"
    assert _observe(db, "a")["version_id"] == a
    assert _serving(db) == ("ok", a)


def test_a_pending_copy_served_again_can_still_be_published(db):
    b = _version(db, "b")
    c = _version(db, "c")
    assert _observe(db, "b")["version_id"] == b  # the county went back to B
    set_manifest(db, b, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={6: 2}, actor="op-1", pdf_sha256=_sha(db, b))
    _publish(db, b)
    assert (db.get(Version, b).status, db.get(Version, c).status) == ("published", "superseded")
    assert _serving(db) == ("ok", b)


def test_nothing_is_published_during_a_hold(db):
    a = _version(db, "a")
    _publish(db, a)
    b = _version(db, "b")
    rev = _load(db, b, _row(6, 1, _A), _row(6, 2, _B))
    record_observation(db, county="douglas", state="WA", token=_claim(db), hold="link_missing")
    with pytest.raises(TaxReviewError, match="serves now"):
        publish(db, b, actor="op-1", expected_revision=rev, pdf_sha256=_sha(db, b))
    _refused(db, _RAW_PUBLISH, {"v": b}, match="serves now")
    assert db.get(Version, a).status == "published"
    with pytest.raises(TaxReviewError, match="serves now"):
        set_manifest(db, b, amount_as_of=_AS_OF, case_number=_CASE, rows_per_page={6: 2}, actor="op-1", pdf_sha256=_sha(db, b))


def test_a_copy_is_not_superseded_by_an_earlier_publication(db):
    _publish(db, _version(db, "a"))
    b = _version(db, "b")
    db.execute(text("SET CONSTRAINTS tax_summons_versions_superseded IMMEDIATE"))
    _refused(db, "UPDATE tax_summons_versions SET status = 'superseded' WHERE id = :v", {"v": b},
             match="superseded only by another published one")


def test_a_result_needs_a_live_claim(db):
    with pytest.raises(TaxReviewError, match="no live claim"):  # never claimed: token 0
        record_observation(db, county="douglas", state="WA", token=0, hold="page_failed")
    token = _claim(db)
    record_observation(db, county="douglas", state="WA", token=token, source_url=_URL, link_year=2026,
                       sha256=_digest("v1"))
    with pytest.raises(TaxReviewError, match="no live claim"):  # a replay of a recorded result
        record_observation(db, county="douglas", state="WA", token=token, hold="page_failed")
    assert _serving(db)[0] == "unpublished"


def test_an_expired_claim_cannot_record_even_without_a_takeover(db):
    token = _claim(db)
    _expire_lease(db)
    with pytest.raises(TaxReviewError, match="no live claim"):
        record_observation(db, county="douglas", state="WA", token=token, hold="page_failed")


@pytest.mark.parametrize("change,match", [
    ("status = 'ok'", "needs the live claim"),                                   # clear a hold, no claim
    ("claim_token = claim_token + 1, status = 'ok'", "a claim only takes"),      # a claim that also clears it
    ("claim_token = claim_token + 1, claim_expires_at = now() + interval '1 day'", "a claim only takes"),
    ("claim_token = claim_token + 2, claim_expires_at = now() + interval '1 minute'", "needs the live claim"),
])
def test_a_hold_is_cleared_only_by_a_new_confirmation(db, change, match):
    _publish(db, _version(db, "v1"))
    record_observation(db, county="douglas", state="WA", token=_claim(db), hold="link_missing")
    _refused(db, f"UPDATE tax_summons_observations SET {change} WHERE county = 'douglas'", {}, match=match)
    assert _serving(db)[0] == "link_missing"


@pytest.mark.parametrize("as_role", [None, "bridgeleads_system"])
def test_only_the_watcher_records_what_the_county_serves(db, as_role):
    """A superuser login acting as itself, or the system role (whatever its grants: here
    the old FOR ALL ones), is refused by the trigger before any other rule."""
    vid = _version(db, "v1")
    _publish(db, vid)
    record_observation(db, county="douglas", state="WA", token=_claim(db), hold="link_missing")
    if as_role:
        _as_system_role(db)
        db.execute(text("GRANT SELECT, INSERT, UPDATE ON tax_summons_observations, tax_summons_versions "
                        "TO bridgeleads_system"))  # the provisioner's blanket grant, re-run
        for table in ("tax_summons_observations", "tax_summons_versions"):
            db.execute(text(f"CREATE POLICY {table}_system_all ON {table} FOR ALL TO bridgeleads_system "
                            "USING (true) WITH CHECK (true)"))
    for sql in ("UPDATE tax_summons_observations SET claim_token = claim_token + 1, "
                "claim_expires_at = now() + interval '5 minutes' WHERE county = 'douglas'",
                "UPDATE tax_summons_observations SET status = 'ok' WHERE county = 'douglas'",
                "INSERT INTO tax_summons_versions (id, county, state, source_url, sha256, seen_order) "
                "VALUES (gen_random_uuid(), 'douglas', 'WA', 'u', repeat('c', 64), 1)"):
        with pytest.raises(DBAPIError, match="only the watcher"):
            with db.begin_nested():
                if as_role:
                    db.execute(text(f"SET LOCAL ROLE {as_role}"))
                db.execute(text(sql))
    assert _serving(db) == ("link_missing", vid)
    with pytest.raises(DBAPIError, match="only the watcher"):  # the module, called without the watcher login
        with db.begin_nested():
            store.claim_watch(db, county="douglas", state="WA", lease_seconds=60)


def test_the_watcher_only_notes_when_it_saw_a_copy(db):
    vid = _version(db, "v1")
    with _as_watcher(db):
        db.execute(text("UPDATE tax_summons_versions SET last_checked_at = now() WHERE id = :v"), {"v": vid})
    _refused(db, "UPDATE tax_summons_versions SET status = 'superseded' WHERE id = :v", {"v": vid},
             match="permission denied", as_watcher=True)  # its grant is UPDATE (last_checked_at) only
    db.execute(text(f"GRANT UPDATE ON tax_summons_versions TO {_WATCHER}"))  # a drifted grant, this test only
    _refused(db, "UPDATE tax_summons_versions SET status = 'superseded' WHERE id = :v", {"v": vid},
             match="only records when it saw a copy", as_watcher=True)


def test_a_result_cannot_rewrite_the_held_copy(db):
    _publish(db, _version(db, "v1"))
    token = _claim(db)
    _refused(db, "UPDATE tax_summons_observations SET claim_expires_at = NULL, status = 'page_failed', "
                 "confirmed_at = now() WHERE county = 'douglas' AND claim_token = :t", {"t": token},
             match="keeps the last confirmed copy")


@pytest.mark.parametrize("columns,values", [
    ("claim_expires_at", "now() + interval '1 day'"),
    ("max_link_year", "2099"),
    ("link_year", "2026"),
    ("link_url", "'https://example.test/x.pdf'"),
])
def test_a_county_starts_unobserved(db, columns, values):
    _refused(db, f"INSERT INTO tax_summons_observations (county, state, {columns}) VALUES ('douglas', 'WA', {values})",
             {}, match="starts unobserved")


def test_a_confirmation_keeps_the_year_high_water_mark(db):
    _publish(db, _version(db, "v1"))  # confirmed for 2026
    token = _claim(db)
    _refused(db, "UPDATE tax_summons_observations SET claim_expires_at = NULL, status = 'ok', link_year = 2025 "
                 "WHERE county = 'douglas' AND claim_token = :t", {"t": token}, match="is a rollback")
    with _as_watcher(db):
        db.execute(text("UPDATE tax_summons_observations SET claim_expires_at = NULL, status = 'ok', "
                        "link_year = 2027 WHERE county = 'douglas' AND claim_token = :t"), {"t": token})
    assert db.execute(text("SELECT max_link_year FROM tax_summons_observations")).scalar() == 2027


def test_a_hold_before_any_confirmation_is_reported_as_the_hold(db):
    record_observation(db, county="douglas", state="WA", token=_claim(db), hold="page_failed")
    assert _serving(db) == ("page_failed", None)


@pytest.mark.parametrize("change,match", [
    ("claim_token = claim_token - 1", "only moves forward"),
    ("max_link_year = 2025", "only moves forward"),
    ("max_link_year = NULL", "only moves forward"),
    ("current_version_id = gen_random_uuid()", "one of this county"),
])
def test_the_observation_only_moves_forward(db, change, match):
    _publish(db, _version(db, "v1"))
    _refused(db, f"UPDATE tax_summons_observations SET {change} WHERE county = 'douglas'", {}, match=match)


def test_the_observation_clock_is_the_databases_and_the_row_stays(db):
    _version(db, "v1")
    _refused(db, "UPDATE tax_summons_observations SET checked_at = '2000-01-01', confirmed_at = '2000-01-01' "
                 "WHERE county = 'douglas'", {}, match="needs the live claim")
    token = _claim(db)  # a result stamps the database's clock, whatever the writer sends
    with _as_watcher(db):
        db.execute(text("UPDATE tax_summons_observations SET claim_expires_at = NULL, checked_at = '2000-01-01', "
                        "confirmed_at = '2000-01-01' WHERE county = 'douglas' AND claim_token = :t"), {"t": token})
    assert db.execute(text("SELECT checked_at > now() - interval '1 minute' AND confirmed_at > now() - "
                           "interval '1 minute' FROM tax_summons_observations")).scalar()
    _refused(db, "DELETE FROM tax_summons_observations", {}, match="never deleted", as_watcher=False)
    _refused(db, "DELETE FROM tax_summons_observations", {}, match="permission denied", as_watcher=True)
    _refused(db, "INSERT INTO tax_summons_observations (county, state, status) VALUES ('chelan', 'WA', 'ok')", {},
             match="starts unobserved|ck_tax_summons_observations_ok")


# ── races between connections (committed; cleaned up) ──

@pytest.fixture
def committed():
    def clean():
        with sync_engine.begin() as conn:
            # The review tables refuse every DELETE (tax_summons_no_delete); test cleanup on
            # the test database alone switches triggers off for its own transaction.
            conn.execute(text("SET LOCAL session_replication_role = replica"))
            ids = [r[0] for r in conn.execute(text(
                "SELECT id FROM tax_summons_versions WHERE county = 'douglas' AND source_url = :u"), {"u": _URL})]
            conn.execute(text("DELETE FROM tax_summons_observations WHERE county = 'douglas'"))
            for table in ("tax_summons_events", "tax_summons_rows"):
                conn.execute(text(f"DELETE FROM {table} WHERE version_id = ANY(CAST(:i AS uuid[]))"), {"i": ids})
            conn.execute(text("DELETE FROM tax_summons_versions WHERE id = ANY(CAST(:i AS uuid[]))"), {"i": ids})

    clean()
    yield
    clean()


def _in_thread(fn):
    out: dict = {}

    def run():
        try:
            with Session(sync_engine) as s:
                out["value"] = fn(s)
                s.commit()
        except Exception as exc:  # noqa: BLE001 -- the test asserts on it
            out["error"] = exc

    t = threading.Thread(target=run)
    t.start()
    return t, out


def _wait_blocked(t, seconds=1.5):
    t.join(seconds)
    assert t.is_alive(), "the second session did not wait for the county lock"


def test_a_new_copy_opened_mid_publish_stops_the_older_copy_publishing(committed):
    with Session(sync_engine) as s:
        v1 = _version(s, "v1")
        rev = _load(s, v1, _row(6, 1, _A), _row(6, 2, _B))
        s.commit()
    with Session(sync_engine) as watcher:
        open_version(watcher, county="douglas", state="WA", source_url=_URL, sha256=_digest("v2"))
        t, out = _in_thread(lambda s: publish(s, v1, actor="op-1", expected_revision=rev, pdf_sha256=_sha(s, v1)))
        _wait_blocked(t)
        watcher.commit()
    t.join(10)
    assert isinstance(out.get("error"), TaxReviewError) and "serves now" in str(out["error"])


def test_a_load_waiting_on_a_publish_is_refused_not_applied(committed):
    with Session(sync_engine) as s:
        v1 = _version(s, "v1")
        rev = _load(s, v1, _row(6, 1, _A), _row(6, 2, _B))
        s.commit()

    def late_load(s):
        assert s.get(Version, v1).status == "pending"  # cached before the wait
        return _load(s, v1, _row(6, 1, _A), _row(6, 2, _B, "redeemed"), actor="op-2")

    with Session(sync_engine) as publisher:
        publish(publisher, v1, actor="op-1", expected_revision=rev, pdf_sha256=_sha(publisher, v1))
        t, out = _in_thread(late_load)
        _wait_blocked(t)
        publisher.commit()
    t.join(10)
    assert isinstance(out.get("error"), TaxReviewError) and "is published" in str(out["error"])
    with Session(sync_engine) as s:
        v = s.get(Version, v1)
        assert (v.status, v.revision, v.published_revision) == ("published", rev, rev)


def test_a_committed_revision_takes_no_more_rows(committed):
    """Only the transaction that opened a revision fills it: once committed, its rows are
    what show displayed and what publish --revision approves."""
    with Session(sync_engine) as s:
        v1 = _version(s, "v1", pages={6: 2, 7: 1})
        rev = s.get(Version, v1).revision
        for row_no, parcel in ((1, _A), (2, _B)):   # partial, committed (as a crashed load might leave)
            s.execute(text("INSERT INTO tax_summons_rows (id, version_id, revision, page, row_no, parcel_id, "
                           "eligibility, entered_by) VALUES (gen_random_uuid(), :v, :r, 6, :n, :p, 'redeemed', "
                           "'x')"), {"v": v1, "r": rev, "n": row_no, "p": parcel})
        s.commit()
    with Session(sync_engine) as intruder:
        with pytest.raises(DBAPIError, match="is sealed"):
            intruder.execute(text("INSERT INTO tax_summons_rows (id, version_id, revision, page, row_no, "
                                  "eligibility, entered_by) VALUES (gen_random_uuid(), :v, :r, 7, 1, "
                                  "'unreadable', 'x')"), {"v": v1, "r": rev})
        intruder.rollback()
    with Session(sync_engine) as s:
        with pytest.raises(TaxReviewError, match="missing p7 r1"):
            publish(s, v1, actor="op-1", expected_revision=rev, pdf_sha256=_digest("v1"))


def test_a_direct_publish_waits_for_the_watcher_recording_a_new_copy(committed):
    """The seal trigger takes the county lock too: while the watcher records a new current
    copy, a raw publish of the old one waits, then sees it is no longer current."""
    with Session(sync_engine) as s:
        v1 = _version(s, "v1")
        _load(s, v1, _row(6, 1, _A), _row(6, 2, _B))
        s.commit()
    with Session(sync_engine) as watcher:
        open_version(watcher, county="douglas", state="WA", source_url=_URL, sha256=_digest("v2"))
        t, out = _in_thread(lambda s: s.execute(text(_RAW_PUBLISH), {"v": v1}))
        _wait_blocked(t)
        watcher.commit()
    t.join(10)
    assert isinstance(out.get("error"), DBAPIError) and "serves now" in str(out["error"])


def test_two_watchers_opening_one_digest_make_one_version(committed):
    with Session(sync_engine) as first:
        vid, created = open_version(first, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
        t, out = _in_thread(lambda s: open_version(s, county="douglas", state="WA", source_url=_URL,
                                                   sha256=_digest("v1")))
        _wait_blocked(t)
        first.commit()
    t.join(10)
    assert created and out.get("value") == (vid, False), out.get("error")


# ── the CLI ──

def _operator(c, *, mfa: bool = True) -> str:
    email = f"op-{uuid.uuid4().hex[:8]}@example.test"
    c.execute(text("INSERT INTO users (id, email, password_hash, email_hmac, is_admin, mfa_enabled) "
                   "VALUES (gen_random_uuid(), :e, 'x', :h, true, :m)"),
              {"e": email, "h": blind_index(email), "m": mfa})
    return email


def _run(c, email, *argv):
    cli.main(["--operator-email", email, *argv], auth_factory=c.session_factory,
             session_factory=c.session_factory)


def test_the_cli_walks_manifest_draft_load_show_publish(c, db, tmp_path, capsys):
    vid, _ = open_version(db, county="douglas", state="WA", source_url=_URL, sha256=_digest("v1"))
    db.commit()
    email = _operator(c)
    pdf, other = tmp_path / "summons.pdf", tmp_path / "replaced.pdf"
    pdf.write_bytes(b"v1")   # its sha256 is the version's digest
    other.write_bytes(b"v2")
    with pytest.raises(SystemExit, match="not this version's copy"):
        _run(c, email, "manifest", "--version", vid, "--pdf", str(other), "--case", _CASE, "--as-of", "2026-08-01", "--pages", "6:2")
    _run(c, email, "manifest", "--version", vid, "--pdf", str(pdf), "--case", _CASE, "--as-of", "2026-08-01", "--pages", "6:2")
    assert "revision 1" in capsys.readouterr().out
    out = tmp_path / "rows.json"
    _run(c, email, "draft", "--version", vid, "--out", str(out))
    assert json.loads(out.read_text()) == {"version": vid, "case": _CASE, "as_of": "2026-08-01",
                                           "manifest": {"6": 2}, "rows": []}
    with pytest.raises(SystemExit, match="exists"):
        _run(c, email, "draft", "--version", vid, "--out", str(out))
    out.write_text(json.dumps(_payload(vid, _row(6, 1, _A), _row(6, 2, _B, "redeemed"))))
    capsys.readouterr()
    _run(c, email, "load", "--version", vid, "--file", str(out))
    loaded = capsys.readouterr().out
    assert "revision 2: eligible 1 redeemed 1" in loaded and "PLACEHOLDER" not in loaded
    _run(c, email, "list")
    assert vid in capsys.readouterr().out
    _run(c, email, "show", "--version", vid)
    shown = capsys.readouterr().out
    assert "revision 2" in shown and "OWNER PLACEHOLDER" in shown
    with pytest.raises(SystemExit, match="refused: the version is at revision 2, not 1"):
        _run(c, email, "publish", "--version", vid, "--revision", "1", "--pdf", str(pdf))
    _run(c, email, "publish", "--version", vid, "--revision", "2", "--pdf", str(pdf))
    assert "published revision 2" in capsys.readouterr().out
    uid = c.execute(text("SELECT id FROM users WHERE email_hmac = :h"), {"h": blind_index(email)}).scalar()
    v = c.execute(text("SELECT status, published_by FROM tax_summons_versions WHERE id = :i"), {"i": vid}).one()
    assert (v.status, v.published_by) == ("published", str(uid))


@pytest.mark.parametrize("argv", [
    ["publish", "--version", "x", "--revision", "JANE-PLACEHOLDER", "--pdf", "f"],
    ["draft", "--version", "x", "--out", "C:/drafts/JANE", "PLACEHOLDER", "DOE.json"],
])
def test_bad_arguments_are_refused_without_echoing_them(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--operator-email", "op@example.test", *argv])
    err = capsys.readouterr().err
    assert exc.value.code == 2 and "invalid or missing arguments" in err and "PLACEHOLDER" not in err


@pytest.mark.skipif(os.name != "nt", reason="Windows access lists")
def test_a_windows_draft_file_drops_access_its_folder_grants_others(c, db, tmp_path):
    """A folder that lets every local user read (BUILTIN\\Users) must not pass that on."""
    import subprocess
    icacls = os.path.join(os.environ["SystemRoot"], "System32", "icacls.exe")
    shared = tmp_path / "shared"
    shared.mkdir()
    subprocess.run([icacls, str(shared), "/grant", "*S-1-5-32-545:(OI)(CI)R"], check=True, capture_output=True)
    vid = _version(db, "v1")
    db.commit()
    out = shared / "rows.json"
    _run(c, _operator(c), "draft", "--version", vid, "--out", str(out))
    acl = subprocess.run([icacls, str(out)], capture_output=True, text=True, check=True).stdout
    entries = [line.split(" ", 1)[-1].strip() if line[:1] != " " else line.strip()
               for line in acl.splitlines() if ":(" in line]
    allowed = (os.environ["USERNAME"], "NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators", "OWNER RIGHTS")
    assert entries and all(any(a in e for a in allowed) for e in entries), acl
    assert not any("(I)" in e for e in entries), acl  # nothing inherited from the folder


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_draft_file_is_readable_by_its_owner_only(c, db, tmp_path):
    vid = _version(db, "v1")
    db.commit()
    out = tmp_path / "rows.json"
    _run(c, _operator(c), "draft", "--version", vid, "--out", str(out))
    assert out.stat().st_mode & 0o077 == 0


def test_a_draft_written_before_a_cleanup_failure_is_reported_as_written(c, db, tmp_path):
    vid = _version(db, "v1")
    db.commit()
    email = _operator(c)

    @contextmanager
    def failing_close():
        with c.session_factory() as s:
            yield s
            s.execute(text("SELECT * FROM tax_summons_no_such_table"))  # a real error on the way out

    out = tmp_path / "rows.json"
    with pytest.raises(SystemExit, match="the --out file was written; closing the session failed"):
        cli.main(["--operator-email", email, "draft", "--version", vid, "--out", str(out)],
                 auth_factory=c.session_factory, session_factory=failing_close)
    assert out.exists()


def test_show_never_sends_a_control_character_to_the_terminal(c, db, capsys):
    vid = _version(db, "v1")
    db.execute(text("INSERT INTO tax_summons_rows (id, version_id, revision, page, row_no, eligibility, "
                    "vested_owner, entered_by) SELECT gen_random_uuid(), id, revision, 6, 1, 'unreadable', "
                    ":o, 'x' FROM tax_summons_versions WHERE id = :v"),
               {"v": vid, "o": f"OWNER{_ESC}[2K{_ESC}[1Aeligible 9999.00"})  # typed past the review module
    db.commit()
    _run(c, _operator(c), "show", "--version", vid)
    shown = capsys.readouterr().out
    assert _ESC not in shown and "OWNER" + chr(92) + "x1b[2K" in shown  # shown as a visible escape


def test_fetch_refuses_a_copy_the_county_no_longer_serves(c, db, tmp_path):
    v1 = _version(db, "v1")
    _version(db, "v2")
    db.commit()
    out = tmp_path / "summons.pdf"
    with pytest.raises(SystemExit, match="not the copy the county serves now"):
        _run(c, _operator(c), "fetch", "--version", v1, "--out", str(out))
    assert not out.exists()


def test_fetched_bytes_must_be_this_versions_pdf():
    pdf = b"%PDF-1.3 the county's summons"
    sha = hashlib.sha256(pdf).hexdigest()
    assert cli.verified_summons(pdf, sha) == pdf
    with pytest.raises(SystemExit, match="different bytes"):
        cli.verified_summons(pdf + b" re-stamped", sha)
    with pytest.raises(SystemExit, match="not a PDF"):
        cli.verified_summons(b"<html>sale results</html>", hashlib.sha256(b"<html>sale results</html>").hexdigest())


def test_the_cli_refuses_an_operator_without_mfa(c, db):
    email = _operator(c, mfa=False)
    with pytest.raises(SystemExit, match="refused: the operator must be an admin with MFA"):
        _run(c, email, "list")


def test_the_cli_reports_a_refusal_without_writing_or_echoing(c, db, tmp_path):
    vid = _version(db, "v1")
    db.commit()
    email = _operator(c)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_payload(vid, _row(6, 1, _A, amount="-1"), _row(6, 2, _B))))
    with pytest.raises(SystemExit, match="refused: page 6 row 1"):
        _run(c, email, "load", "--version", vid, "--file", str(bad))
    assert c.execute(text("SELECT count(*) FROM tax_summons_rows WHERE version_id = :i"), {"i": vid}).scalar() == 0
    # A row that visibly says redeemed, with an earlier 'eligible' json would silently keep... the last one.
    dup = tmp_path / "dup.json"
    text_rows = json.dumps(_payload(vid, _row(6, 1, _A), _row(6, 2, _B, "redeemed")))
    dup.write_text(text_rows.replace('"eligibility": "redeemed"',
                                     '"eligibility": "eligible", "eligibility": "redeemed"'))
    with pytest.raises(SystemExit) as exc:
        _run(c, email, "load", "--version", vid, "--file", str(dup))
    assert "repeats a key" in str(exc.value) and "eligib" not in str(exc.value)
    with pytest.raises(SystemExit, match="--pages must be"):
        _run(c, email, "manifest", "--version", vid, "--pdf", str(bad), "--case", _CASE, "--as-of", "2026-08-01", "--pages", "6:2,6:3")
    named = tmp_path / "JANE PLACEHOLDER DOE.json"   # a path that names someone is never echoed
    with pytest.raises(SystemExit) as exc:
        _run(c, email, "load", "--version", vid, "--file", str(named))
    assert "cannot read the --file (FileNotFoundError)" in str(exc.value) and "PLACEHOLDER" not in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        _run(c, email, "publish", "--version", vid, "--revision", "1", "--pdf", str(named))
    assert "cannot read the --pdf file" in str(exc.value) and "PLACEHOLDER" not in str(exc.value)


def test_the_cli_reports_a_database_error_by_reference_only(c, db, capsys, tmp_path):
    """A real database error: the CLI's session is read-only, so its write fails in the
    database. The CLI rolls back and prints a reference, not the driver's text."""
    vid = _version(db, "v1")
    db.commit()
    email = _operator(c)

    @contextmanager
    def read_only():
        with c.session_factory() as s:
            s.execute(text("SET LOCAL transaction_read_only = on"))
            yield s

    pdf = tmp_path / "summons.pdf"
    pdf.write_bytes(b"v1")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--operator-email", email, "manifest", "--version", vid, "--pdf", str(pdf), "--case", _CASE, "--as-of", "2026-08-01",
                  "--pages", "6:3"],
                 auth_factory=c.session_factory, session_factory=read_only)
    assert "database error (ref " in str(exc.value) and "read-only" not in str(exc.value)
    assert "tax_summons_review: database error" in capsys.readouterr().err
    assert c.execute(text("SELECT manifest FROM tax_summons_versions WHERE id = :i"), {"i": vid}).scalar() == {"6": 2}
