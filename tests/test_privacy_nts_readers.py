"""nts_notices readers skip a notice whose grantor a privacy rule withholds (Phase 6 P5e1,
migration 132).

Real database, the matcher's own end-to-end fixture (tests/test_nts_matcher_task.py).
The withheld state is written here by hand, as P5e2's crawler will write it; the rule
row is a REAL (pending) privacy_suppressions row. Each case has its positive control:
the same notice WITHOUT the rule is matched, so the skip is the rule's doing.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.db.models import Result
from src.db.session import SyncSessionLocal
from src.workers.nts_matcher_task import match_nts_notices
from tests.test_nts_matcher_task import _bridge_fixture, _cleanup


def _rule_row() -> str:
    with SyncSessionLocal() as s:
        request_id = s.execute(text(
            "INSERT INTO privacy_requests (kind, channel, deadline_at) "
            "VALUES ('opt_out', 'web', now() + interval '45 days') RETURNING id")).scalar()
        rule = s.execute(text(
            "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, anchor_hmac, "
            "subject_kind, subject_hmac, scope, key_version, normalization_version, state) "
            "VALUES (gen_random_uuid(), :r, 'property', :h, 'person', :h, 'subject', 'v1', "
            "'n3', 'pending') RETURNING id"), {"r": request_id, "h": "c" * 64}).scalar()
        s.commit()
    return str(rule)


def _scenario(*, past: bool, withheld: bool) -> dict:
    tag = uuid.uuid4().hex[:8]
    pin = "".join(c for c in tag if c.isdigit()).ljust(10, "7")[:10]
    with SyncSessionLocal() as s:
        ids = _bridge_fixture(s, county="king", notice_parcel=f"{pin[:6]}-{pin[6:]}-08",
                              result_parcel=pin, tag=tag)
    sets = []
    params: dict = {"n": ids["notice"]}
    if past:  # a sale that already ran: the past pass reads inactive notices too
        sets += ["auction_date = :d", "fetched_at = :f", "is_active = false"]
        params |= {"d": date.today() - timedelta(days=10), "f": datetime.now(UTC)}
    if withheld:
        sets += ["grantor = NULL", "is_active = false", "privacy_suppression_id = :r"]
        params["r"] = _rule_row()
    if sets:
        with SyncSessionLocal() as s:
            s.execute(text(f"UPDATE nts_notices SET {', '.join(dict.fromkeys(sets))} "  # noqa: S608
                           "WHERE id = :n"), params)
            s.commit()
    return ids


def _matched(ids: dict) -> str | None:
    with SyncSessionLocal() as s:
        return s.get(Result, ids["result"]).nts_notice_id


@pytest.mark.parametrize("past", [False, True], ids=["live", "past"])
def test_a_withheld_notice_is_never_attached_but_the_same_notice_otherwise_is(past):
    control = _scenario(past=past, withheld=False)
    withheld = _scenario(past=past, withheld=True)
    try:
        match_nts_notices()
        assert _matched(control) == control["notice"]   # the pass does read this notice
        assert _matched(withheld) is None               # ... unless a rule withholds it
    finally:
        _cleanup(control)
        _cleanup(withheld)


@pytest.mark.parametrize(("grantor", "active"), [("SMITH JOHN", False), (None, True)])
def test_a_notice_carrying_a_rule_has_no_grantor_and_is_inactive(grantor, active):
    ids = _scenario(past=False, withheld=False)
    rule = _rule_row()
    try:
        with pytest.raises(IntegrityError), SyncSessionLocal() as s:
            s.execute(text("UPDATE nts_notices SET grantor = :g, is_active = :a, "
                           "privacy_suppression_id = :r WHERE id = :n"),
                      {"g": grantor, "a": active, "r": rule, "n": ids["notice"]})
            s.commit()
    finally:
        _cleanup(ids)
