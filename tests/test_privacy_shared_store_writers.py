"""The shared-store writers under the privacy rules (Phase 6 P5e2): the King owner store
and the NTS notice cache never keep a name a rule withholds.

Real database. The King half drives the owner recovery sweep with its own fixtures
(tests/test_owner_recovery.py: only the county page and its lease are replaced). The NTS
half calls the crawler's upsert directly with a real session. One seam: the rule
snapshot (no rule may be active in the database before P7), built in memory with its
active row pointed at a REAL pending rule row so every foreign key holds.
"""
from __future__ import annotations

# Fixtures imported from the owner recovery tests and taken as parameter names read as
# redefinitions to ruff. Only F811, only this file.
# ruff: noqa: F811
import asyncio
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text

import src.privacy.authorize as authorize_mod
import src.workers.owner_recovery as orc
from src.db.models import NtsNotice
from src.db.session import SyncSessionLocal, system_sync_session
from src.privacy import keys
from src.privacy.authorize import RuleSnapshotError, build_snapshot
from src.workers.nts_crawler import _upsert_notice
from tests.test_owner_recovery import (  # noqa: F401 -- _fast_and_clean is autouse
    _county,
    _fast_and_clean,
    _get,
    _job,
    _lease,
    _page,
    _Resp,
    _row,
    _tick,
)
from tests.test_privacy_authorize import _history, _keyring  # noqa: F401 -- autouse keyring
from tests.test_privacy_enforce import _rule_row
from tests.test_privacy_nts_readers import _rule_row as _sync_rule_row

_PIN = "0007200015"


def _rules(monkeypatch, snapshot) -> None:
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", lambda db: snapshot)


def _active(rule: str, anchor: str, subject: str):
    rows = _history(["pending", "active"], anchor=("property", anchor),
                    subject=("person", subject))
    rows[1]["id"] = rule
    return build_snapshot(rows)


def _stored(pid: str):
    with system_sync_session() as s:
        return s.execute(text(
            "SELECT outcome, owner_name, privacy_suppression_id::text AS rule "
            "FROM king_parcel_owners WHERE parcel_id = :p"), {"p": pid}).first()


async def _flag(db, rid: str) -> tuple:
    return tuple((await db.execute(text(
        "SELECT privacy_suppressed_at IS NOT NULL, privacy_suppression_id::text "
        "FROM results WHERE id = :i"), {"i": rid})).one())


# ── King owner store ─────────────────────────────────────────────────────────────

async def test_a_withheld_owner_king_names_is_stored_as_a_tombstone_never_a_name(
    db, business_user, monkeypatch,
):
    rule = await _rule_row(db)
    _rules(monkeypatch, _active(rule, keys.property_anchor_value(_PIN, "king", "WA"),
                                "smith|john"))
    rid = await _row(db, business_user, await _job(db, business_user), parcel=_PIN)
    _lease(monkeypatch)
    _county(monkeypatch, {_PIN: _Resp(200, _page(_PIN, "SMITH JOHN"))})
    stats = await asyncio.to_thread(_tick)
    assert tuple(_stored(_PIN)) == ("privacy_withheld", None, rule)
    row = await _get(db, rid)
    assert row.party_name is None
    assert await _flag(db, rid) == (True, rule)
    assert stats["privacy_withheld"] == 1 and stats["found"] == 0


async def test_someone_else_king_names_is_stored_and_named_as_before(
    db, business_user, monkeypatch,
):
    rule = await _rule_row(db)
    _rules(monkeypatch, _active(rule, keys.property_anchor_value(_PIN, "king", "WA"),
                                "smith|john"))
    rid = await _row(db, business_user, await _job(db, business_user), parcel=_PIN)
    _lease(monkeypatch)
    _county(monkeypatch, {_PIN: _Resp(200, _page(_PIN, "DOE JANE"))})
    await asyncio.to_thread(_tick)
    assert tuple(_stored(_PIN)) == ("found", "DOE JANE", None)
    assert (await _get(db, rid)).party_name == "DOE JANE"
    assert await _flag(db, rid) == (False, None)


async def test_unreadable_rules_stop_the_tick_before_any_request(
    db, business_user, monkeypatch,
):
    rid = await _row(db, business_user, await _job(db, business_user), parcel=_PIN)

    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    _lease(monkeypatch)
    asked = _county(monkeypatch, {_PIN: _Resp(200, _page(_PIN, "SMITH JOHN"))})
    stats = await asyncio.to_thread(_tick)
    assert asked == [] and stats["skipped"] == "privacy rules unavailable"
    assert _stored(_PIN) is None and (await _get(db, rid)).party_name is None


async def test_the_scrape_jobs_answers_store_a_tombstone_too(db, monkeypatch):
    rule = await _rule_row(db)
    _rules(monkeypatch, _active(rule, keys.property_anchor_value(_PIN, "king", "WA"),
                                "smith|john"))
    with system_sync_session() as s:
        assert orc.store_answers(s, {_PIN: ("found", "SMITH JOHN"),
                                     "0007200016": ("found", "DOE JANE")}) == 2
    assert tuple(_stored(_PIN)) == ("privacy_withheld", None, rule)
    assert tuple(_stored("0007200016")) == ("found", "DOE JANE", None)


async def test_the_scrape_jobs_answers_are_not_stored_when_rules_cannot_be_read(
    db, monkeypatch,
):
    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    with system_sync_session() as s:
        assert orc.store_answers(s, {_PIN: ("found", "SMITH JOHN")}) == 0
    assert _stored(_PIN) is None


# ── NTS notice cache ─────────────────────────────────────────────────────────────

_NTS_PARCEL = "0220104064"


def _notice_row(ts: str, grantor: str | None, *, active: bool = True) -> dict:
    return {"source": "tacoma_daily_index", "ts_number": ts, "county": "pierce",
            "state": "WA", "parcel": _NTS_PARCEL, "grantor": grantor, "is_active": active,
            "auction_date": date.today() + timedelta(days=20), "source_url": "https://x/y"}


def _upsert(row: dict) -> None:
    with SyncSessionLocal() as s:
        _upsert_notice(s, NtsNotice, row)
        s.commit()


def _notice(ts: str):
    with SyncSessionLocal() as s:
        return s.execute(text(
            "SELECT grantor, is_active, privacy_suppression_id::text AS rule FROM nts_notices "
            "WHERE source = 'tacoma_daily_index' AND ts_number = :t"), {"t": ts}).one()


@pytest.fixture
def nts_ts():
    ts = f"TEST-PRIV-{uuid.uuid4().hex[:8]}"
    yield ts
    with SyncSessionLocal() as s:
        s.execute(text("DELETE FROM nts_notices WHERE ts_number = :t"), {"t": ts})
        s.commit()


def _nts_rule(monkeypatch) -> str:
    rule = _sync_rule_row()
    _rules(monkeypatch, _active(
        rule, keys.property_anchor_value(_NTS_PARCEL, "pierce", "WA"), "smith|john"))
    return rule


def test_a_withheld_grantor_is_never_stored_and_the_notice_stays_off(nts_ts, monkeypatch):
    rule = _nts_rule(monkeypatch)
    _upsert(_notice_row(nts_ts, "JOHN SMITH, A MARRIED MAN"))
    assert tuple(_notice(nts_ts)) == (None, False, rule)
    # A later crawl whose parser cannot read the grantor, and says the sale is live:
    _upsert(_notice_row(nts_ts, None, active=True))
    assert tuple(_notice(nts_ts)) == (None, False, rule)
    # ... and one after every rule is gone again: still held (lifting is P7).
    _rules(monkeypatch, build_snapshot(_history(["pending"])))
    _upsert(_notice_row(nts_ts, "JOHN SMITH", active=True))
    assert tuple(_notice(nts_ts)) == (None, False, rule)


def test_a_crawl_that_misses_the_name_is_judged_on_the_stored_grantor(nts_ts, monkeypatch):
    _rules(monkeypatch, build_snapshot(_history(["pending"])))  # no rule yet
    _upsert(_notice_row(nts_ts, "JOHN SMITH"))
    assert tuple(_notice(nts_ts)) == ("JOHN SMITH", True, None)
    rule = _nts_rule(monkeypatch)                                # the rule activates
    _upsert(_notice_row(nts_ts, None))                           # parser missed the name
    assert tuple(_notice(nts_ts)) == (None, False, rule)


def test_someone_elses_notice_is_stored_as_before(nts_ts, monkeypatch):
    _nts_rule(monkeypatch)
    _upsert(_notice_row(nts_ts, "JANE DOE"))
    assert tuple(_notice(nts_ts)) == ("JANE DOE", True, None)


def test_unreadable_rules_write_no_notice(nts_ts, monkeypatch):
    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    with pytest.raises(RuleSnapshotError):
        _upsert(_notice_row(nts_ts, "JANE DOE"))
    with SyncSessionLocal() as s:
        assert s.execute(text("SELECT count(*) FROM nts_notices WHERE ts_number = :t"),
                         {"t": nts_ts}).scalar() == 0
