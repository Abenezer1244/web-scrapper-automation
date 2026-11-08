"""The beat sweeps that name a lead check the name they write (Phase 6 P5f).

Real database, the King code-violation owner sweep driven with its own fixtures
(tests/test_cv_owner_recovery.py: only the county page and its lease are replaced). One
seam: the rule snapshot (no rule may be active in the database before P7), its active row
pointed at a REAL pending rule row. A code-violation lead carries no parcel_id, so the
rule's person named on it is a subject without its anchor: REVIEW, withheld like WITHHELD.
Synthetic people only.
"""
from __future__ import annotations

# Fixtures imported from the CV owner tests and taken as parameter names read as
# redefinitions to ruff. Only F811, only this file.
# ruff: noqa: F811
import asyncio

from sqlalchemy import text

import src.privacy.authorize as authorize_mod
from src.privacy.authorize import RuleSnapshotError, build_snapshot
from tests.test_cv_owner_recovery import (  # noqa: F401 -- _fast_and_clean is autouse
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


async def _flag(db, rid: str) -> tuple:
    return tuple((await db.execute(text(
        "SELECT privacy_suppressed_at IS NOT NULL, privacy_suppression_id::text "
        "FROM results WHERE id = :i"), {"i": rid})).one())


async def test_a_name_the_sweep_writes_that_a_rule_withholds_flags_the_lead(
    db, business_user, monkeypatch,
):
    rule = await _rule_row(db)
    rows = _history(["pending", "active"])          # SMITH JOHN
    rows[1]["id"] = rule
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", lambda db: build_snapshot(rows))
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    withheld = await _row(db, business_user, job_id, pin="9138100481")
    other = await _row(db, business_user, job_id, pin="5249802770")
    _county(monkeypatch, {
        "9138100481": _Resp(200, _page("9138100481", "SMITH JOHN")),
        "5249802770": _Resp(200, _page("5249802770", "NGUYEN THANH")),
    })
    await asyncio.to_thread(_tick)
    assert await _flag(db, withheld) == (True, rule)
    assert await _flag(db, other) == (False, None)  # control: named, not flagged
    assert (await _get(db, other)).party_name == "NGUYEN THANH"


async def test_unreadable_rules_stop_the_sweep_before_any_request(
    db, business_user, monkeypatch,
):
    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    _lease(monkeypatch)
    rid = await _row(db, business_user, await _job(db, business_user), pin="9138100481")
    asked = _county(monkeypatch, {"9138100481": _Resp(200, _page("9138100481", "SMITH JOHN"))})
    stats = await asyncio.to_thread(_tick)
    assert asked == [] and stats["skipped"] == "privacy rules unavailable"
    assert (await _get(db, rid)).party_name is None
