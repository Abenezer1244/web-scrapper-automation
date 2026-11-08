"""scripts/king_taxbill_mailing_check.py never looks up, reports or writes a privacy-withheld
lead (Phase 6 P7).

Real database; the script's own select_candidates() (the Assessor-script candidates and
_CV_CANDIDATES_SQL) and apply() (_UPDATE_SQL) run against it. The extract answers nothing,
so every unsuppressed echo and located code violation is a tax-bill candidate. No King
page is fetched. Synthetic people only.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from src.db.session import system_sync_session
from tests.test_king_mailing_repairs import _row_in_job, tb
from tests.test_king_rpacct_mailing import _king_row
from tests.test_privacy_enforce import _rule_row

ECHO = "506 S 330TH PL, FEDERAL WAY, WA 98003-5900"
NEW_MAIL = "PO BOX 7, SEATTLE WA 98101"
# Only a shown tier may carry a mailing address (src/utils/located_parcel.py).
TIER = {"kc_pin_status": "matched", "kc_pin_source": "king_gis_point_in_parcel",
        "kc_pin_match": "exact", "kc_pin": "0904000025"}


async def _withhold(db, *rids: str) -> None:
    rule = await _rule_row(db)
    await db.execute(text(
        "UPDATE results SET privacy_suppressed_at = now(), privacy_suppression_id = :r "
        "WHERE id = ANY(CAST(:ids AS uuid[]))"), {"r": rule, "ids": list(rids)})
    await db.commit()


def _select(ids: set[str]) -> list[dict]:
    with system_sync_session() as s:
        return [c for c in tb.select_candidates(s, lambda pins: {}) if str(c["row"].id) in ids]


def _apply(decisions) -> dict:
    with system_sync_session() as s:
        return dict(tb.apply(s, decisions))


async def _four(db, user) -> tuple[str, str, str, str]:
    echo_w, _ = await _king_row(db, user, mailing=ECHO)
    echo_k, _ = await _king_row(db, user, mailing=ECHO)
    cv_w = await _row_in_job(db, user, record_type="code_violation", ed=dict(TIER))
    cv_k = await _row_in_job(db, user, record_type="code_violation", ed=dict(TIER))
    return echo_w, echo_k, cv_w, cv_k


async def test_withheld_rows_are_never_candidates(db, business_user):
    echo_w, echo_k, cv_w, cv_k = await _four(db, business_user)
    await _withhold(db, echo_w, cv_w)

    got = {str(c["row"].id): c["kind"]
           for c in await asyncio.to_thread(_select, {echo_w, echo_k, cv_w, cv_k})}
    assert got == {echo_k: "echo", cv_k: "cv"}    # control: the deliverable twins are selected


async def test_rows_withheld_after_the_read_are_never_written(db, business_user):
    echo_w, echo_k, cv_w, cv_k = await _four(db, business_user)
    candidates = await asyncio.to_thread(_select, {echo_w, echo_k, cv_w, cv_k})
    assert {str(c["row"].id) for c in candidates} == {echo_w, echo_k, cv_w, cv_k}
    decisions = [{**c, "outcome": "found", "new_mail": NEW_MAIL, "write": True}
                 for c in candidates]
    await _withhold(db, echo_w, cv_w)

    counts = await asyncio.to_thread(_apply, decisions)

    assert counts == {"written": 2, "skipped_by_write_guard": 2}
    got = dict((await db.execute(text(
        "SELECT id::text, mailing_address FROM results WHERE id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": [echo_w, echo_k, cv_w, cv_k]})).all())
    assert (got[echo_w], got[cv_w]) == (ECHO, None)
    assert (got[echo_k], got[cv_k]) == (NEW_MAIL, NEW_MAIL)
