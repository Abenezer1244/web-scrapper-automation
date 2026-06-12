"""DB integration tests for property_list_membership (Phase 1).

Real Postgres via SyncSessionLocal (mirrors how workers/tasks.py writes).
Run these in CI / against a dedicated TEST database — NOT production.
"""
import uuid

import pytest
from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.workers.tasks import _upsert_property_membership


class _Row:
    """Minimal stand-in for a Result row (only fields the upsert reads)."""
    def __init__(self, parcel_id, property_address):
        self.parcel_id = parcel_id
        self.property_address = property_address


@pytest.fixture
def membership_user():
    uid = str(uuid.uuid4())
    with SyncSessionLocal() as db:
        db.execute(text(
            "INSERT INTO users (id, email, password_hash, plan, records_used, records_limit) "
            "VALUES (:id, :email, 'x', 'business', 0, 5000)"
        ), {"id": uid, "email": f"test_{uid[:8]}@test.bridgeleads.io"})
        db.commit()
    yield uid
    with SyncSessionLocal() as db:
        db.execute(text("DELETE FROM property_list_membership WHERE user_id = :u"), {"u": uid})
        db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        db.commit()


def _count(db, uid):
    return db.execute(
        text("SELECT count(*) FROM property_list_membership WHERE user_id = :u"), {"u": uid}
    ).scalar()


def test_upsert_inserts_strong_rows_only(membership_user):
    rows = [
        _Row("1234567890", "123 MAIN ST"),
        _Row(None, "456 OAK AVENUE"),
        _Row(None, None),
        _Row("12", "x"),
    ]
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, rows, membership_user, "probate", "king", "WA")
        assert _count(db, membership_user) == 2


def test_upsert_repeated_key_in_one_job_no_double_affect(membership_user):
    rows = [_Row("1234567890", "123 MAIN ST"), _Row("1234-56-7890", "123 Main St.")]
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, rows, membership_user, "probate", "king", "WA")
        row = db.execute(text(
            "SELECT sighting_count FROM property_list_membership WHERE user_id = :u"
        ), {"u": membership_user}).fetchone()
        assert _count(db, membership_user) == 1
        assert row.sighting_count == 2


def test_upsert_rerun_keeps_first_seen_advances_last_seen(membership_user):
    rows = [_Row("1234567890", "123 MAIN ST")]
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, rows, membership_user, "probate", "king", "WA")
        first = db.execute(text(
            "SELECT first_seen_at, last_seen_at FROM property_list_membership WHERE user_id=:u"
        ), {"u": membership_user}).fetchone()
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, rows, membership_user, "probate", "king", "WA")
        second = db.execute(text(
            "SELECT first_seen_at, last_seen_at, sighting_count "
            "FROM property_list_membership WHERE user_id=:u"
        ), {"u": membership_user}).fetchone()
    assert second.first_seen_at == first.first_seen_at
    assert second.last_seen_at >= first.last_seen_at
    assert second.sighting_count == 2


def test_same_property_two_record_types_two_rows(membership_user):
    row = [_Row("1234567890", "123 MAIN ST")]
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, row, membership_user, "probate", "king", "WA")
        _upsert_property_membership(db, row, membership_user, "pre_foreclosure", "king", "WA")
        assert _count(db, membership_user) == 2


@pytest.mark.asyncio
async def test_overlap_returns_properties_on_both_lists(membership_user):
    shared = _Row("1234567890", "123 MAIN ST")
    only_probate = _Row("9990001112", "1 LONE LN")
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, [shared, only_probate], membership_user, "probate", "king", "WA")
        _upsert_property_membership(db, [shared], membership_user, "pre_foreclosure", "king", "WA")

    from src.workers.membership_query import users_overlap
    keys = await users_overlap(membership_user, ["probate", "pre_foreclosure"])
    from src.workers.property_identity import compute_property_key
    assert keys == {compute_property_key("1234567890", "123 MAIN ST", "king", "WA")}
