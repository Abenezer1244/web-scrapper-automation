"""scripts/king_rpacct_mailing.py never reads, reports or writes a privacy-withheld lead
(Phase 6 P7).

Real database; the script's own statements (_CANDIDATES_SQL, and _UPDATE_SQL through
apply()) run against it. Two King situs-echo rows on the same parcel: one is withheld.
Synthetic people only.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from src.db.session import system_sync_session
from src.scrapers.enrichment import king_rpacct as kr
from tests.test_king_rpacct_mailing import _king_row, km
from tests.test_privacy_enforce import _rule_row

ECHO = "506 S 330TH PL, FEDERAL WAY, WA 98003-5900"
TX = kr.Answer("found", "2736 ROSECLIFF TERRACE, GRAPEVINE, TX 76051")


async def _withhold(db, rid: str) -> None:
    rule = await _rule_row(db)
    await db.execute(text(
        "UPDATE results SET privacy_suppressed_at = now(), privacy_suppression_id = :r "
        "WHERE id = :i"), {"r": rule, "i": rid})
    await db.commit()


def _select(ids: set[str]) -> list:
    with system_sync_session() as s:
        rows = [r for r in s.execute(text(km._CANDIDATES_SQL)).all() if str(r.id) in ids]
        s.rollback()
    return rows


def _apply(decisions) -> dict:
    with system_sync_session() as s:
        return dict(km.apply(s, decisions, "2026-09-05"))


async def test_a_withheld_row_is_never_a_candidate(db, business_user):
    withheld, _ = await _king_row(db, business_user, mailing=ECHO)
    kept, _ = await _king_row(db, business_user, mailing=ECHO)
    await _withhold(db, withheld)

    ids = {str(r.id) for r in await asyncio.to_thread(_select, {withheld, kept})}
    assert ids == {kept}                     # control: the deliverable twin is selected


async def test_a_row_withheld_after_the_read_is_never_written(db, business_user):
    withheld, _ = await _king_row(db, business_user, mailing=ECHO)
    kept, _ = await _king_row(db, business_user, mailing=ECHO)
    rows = await asyncio.to_thread(_select, {withheld, kept})
    decisions = km.plan(rows, {"1321400230": TX})
    assert {str(d["row"].id) for d in decisions} == {withheld, kept}
    await _withhold(db, withheld)

    counts = await asyncio.to_thread(_apply, decisions)

    assert counts == {"written": 1, "skipped_by_write_guard": 1}
    got = dict((await db.execute(text(
        "SELECT id::text, mailing_address FROM results WHERE id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": [withheld, kept]})).all())
    assert got[withheld] == ECHO
    assert got[kept] == TX.mailing_address
