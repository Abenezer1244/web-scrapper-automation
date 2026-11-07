"""King owner store: a privacy tombstone is terminal and flags the leads waiting on it
(Phase 6 P5e1, migration 131).

Real database, the owner recovery sweep's own fixtures (tests/test_owner_recovery.py):
only the King county page and its admission lease are replaced. Tombstones are written
here by hand, as P5e2's writers will write them; the rule row is a REAL (pending)
privacy_suppressions row so every foreign key holds. Synthetic people only.
"""
from __future__ import annotations

# Fixtures imported from the owner recovery tests and taken as parameter names read as
# redefinitions to ruff. Only F811, only this file.
# ruff: noqa: F811
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

import src.workers.owner_recovery as orc
from src.db.models import Result
from src.db.session import system_sync_session
from src.workers.tasks_helpers.enrich import _fill_king_owners_from_store
from tests.test_owner_recovery import (  # noqa: F401 -- _fast_and_clean is autouse
    _county,
    _fast_and_clean,
    _get,
    _job,
    _lease,
    _row,
    _store_answer,
    _tick,
)
from tests.test_privacy_enforce import _rule_row

_PIN = "0007200015"


def _tombstone(pid: str, rule: str, *, days_old: int = 0) -> None:
    with system_sync_session() as sdb:
        sdb.execute(text(
            "INSERT INTO king_parcel_owners (parcel_id, outcome, owner_name, observed_at, "
            "source, privacy_suppression_id) VALUES (:p, 'privacy_withheld', NULL, "
            "now() - make_interval(days => :d), 'erealproperty', :r)"),
            {"p": pid, "d": days_old, "r": rule})
        sdb.commit()


async def _flag(db, rid: str) -> tuple:
    row = (await db.execute(text(
        "SELECT privacy_suppressed_at IS NOT NULL, privacy_suppression_id::text "
        "FROM results WHERE id = :i"), {"i": rid})).one()
    return tuple(row)


async def test_a_tombstone_of_any_age_flags_every_waiting_lead_and_asks_no_one(
    db, business_user, starter_user, monkeypatch,
):
    rule = await _rule_row(db)
    _tombstone(_PIN, rule, days_old=400)            # far past the 30-day freshness
    mine = await _row(db, business_user, await _job(db, business_user), parcel=_PIN)
    theirs = await _row(db, starter_user, await _job(db, starter_user), parcel=_PIN)
    named = await _row(db, business_user, await _job(db, business_user), parcel=_PIN,
                       party="DOE JANE", deferred=False)
    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    stats = _tick()
    assert asked == []                                # the county is never asked
    assert stats["privacy_withheld"] == 2 and stats["cached"] == 0
    for rid in (mine, theirs):                        # every tenant's waiting lead
        row = await _get(db, rid)
        assert row.party_name is None                 # never named
        assert row.enrichment_data["owner_recovery_outcome"] == "privacy_withheld"
        assert row.enrichment_data["owner_lookup_deferred"] is False
        assert await _flag(db, rid) == (True, rule)
    # Someone else named on that parcel is not the withheld owner: left alone.
    assert await _flag(db, named) == (False, None)


async def test_a_flagged_lead_is_never_named_by_a_later_answer(db, business_user, monkeypatch):
    rule = await _rule_row(db)
    _tombstone(_PIN, rule)
    rid = await _row(db, business_user, await _job(db, business_user), parcel=_PIN)
    _lease(monkeypatch)
    _county(monkeypatch, {})
    _tick()
    with system_sync_session() as sdb:      # a later (fresh) answer replaces the tombstone
        sdb.execute(text("UPDATE king_parcel_owners SET outcome = 'found', "
                         "owner_name = 'SMITH JOHN', privacy_suppression_id = NULL, "
                         "observed_at = now() WHERE parcel_id = :p"), {"p": _PIN})
        sdb.commit()
    _tick()
    assert (await _get(db, rid)).party_name is None
    assert await _flag(db, rid) == (True, rule)


async def test_fresh_answers_return_a_tombstone_at_any_age_with_its_rule(db):
    rule = await _rule_row(db)
    _tombstone(_PIN, rule, days_old=400)
    _store_answer("0007200016", "found", "SMITH JOHN", days_old=400)  # stale: not read
    with system_sync_session() as sdb:
        answers = orc.fresh_answers(sdb, {_PIN, "0007200016"})
    assert answers == {_PIN: ("privacy_withheld", None, rule)}


async def test_the_scrape_jobs_store_fill_flags_and_settles_without_a_name(db, business_user):
    rule = await _rule_row(db)
    _tombstone(_PIN, rule, days_old=400)
    rid = await _row(db, business_user, await _job(db, business_user), parcel=_PIN)
    with system_sync_session() as sdb:
        rows = sdb.execute(select(Result).where(Result.id == rid)).scalars().all()
        assert _fill_king_owners_from_store(sdb, rows) == 1
        sdb.commit()
    row = await _get(db, rid)
    assert row.party_name is None
    assert row.enrichment_data["owner_recovery_outcome"] == "privacy_withheld"
    assert await _flag(db, rid) == (True, rule)


@pytest.mark.parametrize(("outcome", "name", "with_rule"), [
    ("privacy_withheld", None, False),     # a tombstone must name its rule
    ("found", "SMITH JOHN", True),         # only a tombstone carries a rule
    ("not_on_record", None, True),
])
async def test_only_a_tombstone_carries_a_rule(db, outcome, name, with_rule):
    rule = await _rule_row(db)
    with pytest.raises(IntegrityError), system_sync_session() as sdb:
        sdb.execute(text(
            "INSERT INTO king_parcel_owners (parcel_id, outcome, owner_name, observed_at, "
            "source, privacy_suppression_id) VALUES (:p, :o, :n, now(), 'erealproperty', :r)"),
            {"p": _PIN, "o": outcome, "n": name, "r": rule if with_rule else None})
        sdb.commit()
