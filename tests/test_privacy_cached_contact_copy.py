"""A cached skip-trace answer is never copied onto a lead a privacy rule withholds once
its phones/emails are on it (Phase 6 P5h, copy_cached_answer).

Real database, real cache row, real copy. One seam, as everywhere in Phase 6: the rule
snapshot (no rule may be active in the database before P7), built in memory with its
active row pointed at a REAL pending rule row. The lead's name cannot be read in its
source's order (non-Latin), so it is ALLOWED until the cached phone, the rule's anchor,
lands on it; then the rule matches with an unresolved subject -> REVIEW -> withheld.
Synthetic people and numbers only.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text

from src.db.models import Result, SkipTraceCache
from src.db.session import system_sync_session
from src.privacy.authorize import build_snapshot
from src.scrapers.enrichment.skip_trace import payload_subject_key
from src.workers.tasks_helpers.enrich import copy_cached_answer
from tests.test_privacy_authorize import _history, _keyring  # noqa: F401 -- autouse keyring
from tests.test_privacy_enforce import _job, _rows, _rule_row

_PHONE = "(206) 555-1234"


def _payload() -> dict:
    return {"property_address": f"{uuid.uuid4().hex[:6]} TEST ST", "city": "SEATTLE",
            "state": "WA", "trace_type": "normal", "first_name": "IVAN",
            "last_name": "IVANOV"}


def _cache(uid: str, payload: dict) -> None:
    with system_sync_session() as s:
        s.add(SkipTraceCache(address_hash=payload_subject_key(uid, payload), phone=_PHONE,
                             phones=[{"number": _PHONE, "type": "Mobile"}],
                             fetched_at=datetime.now(UTC)))
        s.commit()


def _copy(uid: str, rid: str, payload: dict, snapshot) -> str:
    with system_sync_session() as s:
        rec = s.execute(select(Result).where(Result.id == rid)).scalar_one()
        outcome = copy_cached_answer(s, uid, rec, payload, snapshot)
        s.commit()
    return outcome


async def _state(db, rid: str) -> tuple:
    row = (await db.execute(text(
        "SELECT phone IS NOT NULL, phones IS NOT NULL, skip_trace_source, "
        "privacy_suppression_id::text FROM results WHERE id = :i"), {"i": rid})).one()
    return tuple(row)


async def test_a_cached_phone_that_matches_a_rule_is_never_copied_and_flags_the_lead(
    db, business_user,
):
    rule = await _rule_row(db)
    (rid,) = await _rows(db, business_user, await _job(db, business_user),
                         ("ИВАНОВ ИВАН", None))
    payload = _payload()
    _cache(business_user.id, payload)
    rows = _history(["pending", "active"], anchor=("phone", "2065551234"))
    rows[1]["id"] = rule
    assert _copy(business_user.id, rid, payload, build_snapshot(rows)) == "withheld"
    assert await _state(db, rid) == (False, False, None, rule)


async def test_with_no_rule_active_the_cached_answer_is_copied(db, business_user):
    (rid,) = await _rows(db, business_user, await _job(db, business_user),
                         ("ИВАНОВ ИВАН", None))
    payload = _payload()
    _cache(business_user.id, payload)
    assert _copy(business_user.id, rid, payload,
                 build_snapshot(_history(["pending"]))) == "copied"
    assert await _state(db, rid) == (True, True, "reused", None)
