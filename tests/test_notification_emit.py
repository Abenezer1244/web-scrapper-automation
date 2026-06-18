import uuid

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.workers.notification_emit import create_notification

pytestmark = pytest.mark.integration


def _make_user(prefs: dict) -> str:
    from src.api.auth import hash_password
    from src.utils.crypto import blind_index
    uid = str(uuid.uuid4())
    email = f"notif_{uid[:8]}@bl.test"
    with system_sync_session() as db:
        db.execute(
            text("""
                INSERT INTO users (id, email, email_hmac, password_hash, plan,
                    records_used, records_limit, is_active, is_admin,
                    referral_credit_cents, notification_prefs)
                VALUES (:i, :e, :h, :p, 'starter', 0, 50, true, false, 0,
                        CAST(:prefs AS json))
            """),
            {"i": uid, "e": email, "h": blind_index(email),
             "p": hash_password("testpassword123"),
             "prefs": __import__("json").dumps(prefs)},
        )
        db.commit()
    return uid


def _count(uid: str) -> int:
    with system_sync_session() as db:
        return db.execute(
            text("SELECT COUNT(*) FROM notifications WHERE user_id = :u"), {"u": uid}
        ).scalar()


def test_emit_inserts_when_pref_enabled():
    uid = _make_user({"job_completed": True})
    create_notification(user_id=uid, type="job_completed",
                        job_id=str(uuid.uuid4()), detail={"record_count": 5})
    assert _count(uid) == 1


def test_emit_suppressed_when_pref_disabled():
    uid = _make_user({"job_completed": False})
    create_notification(user_id=uid, type="job_completed", job_id=str(uuid.uuid4()))
    assert _count(uid) == 0


def test_emit_default_enabled_when_pref_absent():
    uid = _make_user({})
    create_notification(user_id=uid, type="job_failed", detail={"error_summary": "x"})
    assert _count(uid) == 1


def test_emit_fails_closed_on_unknown_type():
    uid = _make_user({})
    create_notification(user_id=uid, type="not_a_real_type")
    assert _count(uid) == 0


def test_emit_swallows_errors(monkeypatch):
    # A bad user_id (not a uuid) must not raise out of the helper.
    create_notification(user_id="not-a-uuid", type="job_completed")  # no exception
