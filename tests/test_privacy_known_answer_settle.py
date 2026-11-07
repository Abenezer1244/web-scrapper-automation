"""The dispatcher's known-answer settle checks a cached answer WITH its phones and
emails before keeping it (Phase 6 P5h).

Real database, the reuse tests' own fixtures (tests/test_lookup_subject_reuse.py). One
seam: the rule snapshot (no rule may be active in the database before P7), its active
row pointed at a REAL pending rule row. The withheld lead's name cannot be read
(non-Latin): allowed until the cached phone, the rule's anchor, is on it, then
anchor + unresolved subject -> review -> withdrawn. Synthetic people only.
"""
from __future__ import annotations

# Fixtures imported from the reuse tests and taken as parameter names read as
# redefinitions to ruff. Only F811, only this file.
# ruff: noqa: F811
from sqlalchemy import text

import src.privacy.authorize as authorize_mod
from src.db.session import system_sync_session
from src.privacy.authorize import build_snapshot
from tests.test_lookup_subject_reuse import (  # noqa: F401 -- fixture
    DECEASED,
    _cache_row,
    _lead,
    _pending_row,
    _row,
    _run,
    _skip_trace_on,
    _subject,
)
from tests.test_privacy_authorize import _history, _keyring  # noqa: F401 -- autouse keyring
from tests.test_privacy_nts_readers import _rule_row


async def test_a_cached_answer_whose_phone_a_rule_anchors_is_withdrawn_not_kept(
    business_user, _skip_trace_on, monkeypatch,
):
    from src.workers.skip_trace_dispatcher import _settle_queued_from_known_answers

    rule = _rule_row()
    rows = _history(["pending", "active"], anchor=("phone", "2065550107"))
    rows[1]["id"] = rule
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", lambda db: build_snapshot(rows))
    job = _run(business_user.id)
    withheld = _lead(business_user.id, job, 7, party="ИВАНОВ ИВАН")
    held_row = _pending_row(business_user.id, job, withheld, 7, first="IVAN", last="IVANOV")
    _cache_row(_subject(business_user.id, 7, "IVAN", "IVANOV"), "2065550107")
    control = _lead(business_user.id, job, 8, party=DECEASED)
    _pending_row(business_user.id, job, control, 8, first="AVELINO", last="SAARENAS")
    _cache_row(_subject(business_user.id, 8, "AVELINO", "SAARENAS"), "2065550107")

    with system_sync_session() as db:
        _settle_queued_from_known_answers(db)

    r = _row(withheld)
    assert (r.phone, r.phones, r.skip_trace_status, r.skip_trace_source) == (
        None, None, "not_attempted", None)
    assert str(r.privacy_suppression_id) == rule
    with system_sync_session() as db:
        assert db.execute(text("SELECT status FROM pending_skip_trace_rows WHERE id = :p"),
                          {"p": held_row}).scalar() == "cancelled"
    c = _row(control)                       # control: a readable other person is settled
    assert (c.skip_trace_status, c.phone, c.skip_trace_source) == ("hit", "2065550107", "reused")
