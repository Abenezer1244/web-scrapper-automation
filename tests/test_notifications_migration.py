import pytest
from sqlalchemy import text
from src.db.session import sync_engine

pytestmark = pytest.mark.integration


def test_notifications_table_and_rls_present():
    with sync_engine.begin() as conn:
        # Table exists
        assert conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'notifications'"
        )).scalar() == 1
        # RLS enabled
        assert conn.execute(text(
            "SELECT relrowsecurity FROM pg_class WHERE relname = 'notifications'"
        )).scalar() is True
        # Untargeted isolation policy present (pre-cutover) OR role-targeted
        # (post-cutover) — at least one notifications policy must exist.
        assert conn.execute(text(
            "SELECT COUNT(*) FROM pg_policies WHERE tablename = 'notifications'"
        )).scalar() >= 1
