"""scripts/verify_tracerfy_provenance.py never prints a privacy-withheld lead (Phase 6 P7).

It prints names, addresses and decrypted phones/emails of skip-trace hits. Real database,
one session rolled back at the end; the script's own sample statement (_sample_stmt)
runs against it. Two hits in one account: one is withheld. Synthetic people only.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.models import Result
from src.db.session import system_sync_session
from tests.test_privacy_erase import _owner, _result


def _script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_tracerfy_provenance.py"
    spec = importlib.util.spec_from_file_location("_verify_tracerfy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def s():
    with system_sync_session() as session:
        try:
            yield session
        finally:
            session.rollback()


def _rule(s) -> str:
    """A real (pending) privacy rule row, so a withheld result's flag pair holds."""
    request_id = s.execute(text(
        "INSERT INTO privacy_requests (kind, channel, deadline_at) "
        "VALUES ('opt_out', 'web', now() + interval '45 days') RETURNING id")).scalar()
    return str(s.execute(text(
        "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, anchor_hmac, "
        "subject_kind, subject_hmac, scope, key_version, normalization_version, state) "
        "VALUES (gen_random_uuid(), :r, 'property', :h, 'person', :h, 'subject', 'v1', "
        "'n3', 'pending') RETURNING id"), {"r": request_id, "h": "c" * 64}).scalar())


def _withhold(s, rid: str) -> str:
    rule = _rule(s)
    s.execute(text(
        "UPDATE results SET privacy_suppressed_at = now(), privacy_suppression_id = :r "
        "WHERE id = :i"), {"r": rule, "i": rid})
    return rule


def test_a_withheld_hit_is_never_sampled(s):
    uid, job = _owner(s)
    kept = _result(s, uid, job, "DOE JANE", "1111111111", skip_trace_status="hit")
    withheld = _result(s, uid, job, "SMITH JOHN", "2222222222", skip_trace_status="hit")
    _withhold(s, withheld)

    stmt = _script()._sample_stmt(1000).where(Result.user_id == uid)
    sampled = [str(r.id) for r in s.execute(stmt).scalars()]

    assert sampled == [kept]          # control: the unwithheld hit is still sampled
