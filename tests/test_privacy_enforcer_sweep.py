"""The privacy enforcer sweep (beat), driven for real against the test database.

One seam, as in tests/test_privacy_enforce.py: the rule snapshot (no rule may be active
in the database before P7). Synthetic people only.
"""
from __future__ import annotations

import pytest

import src.workers.privacy_enforcer as enforcer
from src.privacy.authorize import RuleSnapshotError, build_snapshot
from tests.test_privacy_authorize import _history, _keyring  # noqa: F401 -- autouse keyring
from tests.test_privacy_enforce import _PARCEL, _job, _rows, _rule_row, _state, active_snapshot


def _rules(monkeypatch, snapshot) -> None:
    monkeypatch.setattr(enforcer, "load_rule_snapshot", lambda db: snapshot)


async def test_with_no_rule_ever_active_the_tick_reads_nothing_else(db, business_user,
                                                                    monkeypatch):
    job = await _job(db, business_user)
    (rid,) = await _rows(db, business_user, job, ("SMITH JOHN", _PARCEL))
    _rules(monkeypatch, build_snapshot(_history(["pending"])))
    assert enforcer.enforce_privacy_rules() == {
        "generation": 0, "batches": 0, "checked": 0, "flagged": 0}
    assert await _state(db, rid) == (False, None, 0)


async def test_a_tick_flags_and_stamps_and_the_next_tick_rereads_nothing(db, business_user,
                                                                         monkeypatch):
    rule = await _rule_row(db)
    job = await _job(db, business_user)
    smith, doe = await _rows(db, business_user, job, ("SMITH JOHN", _PARCEL),
                             ("DOE JANE", _PARCEL))
    snapshot = active_snapshot(rule)
    _rules(monkeypatch, snapshot)
    first = enforcer.enforce_privacy_rules()
    assert first["generation"] == snapshot.generation and first["flagged"] >= 1
    assert await _state(db, smith) == (True, rule, snapshot.generation)
    assert await _state(db, doe) == (False, None, snapshot.generation)
    second = enforcer.enforce_privacy_rules()
    assert (second["checked"], second["flagged"]) == (0, 0)


async def test_one_row_batches_walk_every_row(db, business_user, starter_user, monkeypatch):
    rule = await _rule_row(db)
    bad_job = await _job(db, business_user, config_owner=starter_user)  # corrupt job
    (cross,) = await _rows(db, business_user, bad_job, ("SMITH JOHN", _PARCEL))
    job = await _job(db, business_user)
    smith, doe = await _rows(db, business_user, job, ("SMITH JOHN", _PARCEL),
                             ("DOE JANE", _PARCEL))
    snapshot = active_snapshot(rule)
    _rules(monkeypatch, snapshot)
    stats = enforcer.enforce_privacy_rules(batch=1)
    assert stats["batches"] >= 3
    assert await _state(db, cross) == (True, rule, snapshot.generation)
    assert await _state(db, smith) == (True, rule, snapshot.generation)
    assert await _state(db, doe) == (False, None, snapshot.generation)


async def test_rules_that_cannot_load_fail_the_tick_and_stamp_nothing(db, business_user,
                                                                      monkeypatch):
    job = await _job(db, business_user)
    (rid,) = await _rows(db, business_user, job, ("SMITH JOHN", _PARCEL))

    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(enforcer, "load_rule_snapshot", _broken)
    with pytest.raises(RuleSnapshotError):
        enforcer.enforce_privacy_rules()
    assert await _state(db, rid) == (False, None, 0)
