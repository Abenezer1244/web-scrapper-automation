"""Phase 1 RLS cutover: per-transaction GUC reapply.

Proves the `after_begin` listener in src/db/session.py re-binds
`app.current_user_id` on every new transaction, so the worker's mid-task
commit in run_scrape_job does NOT strip the RLS user context.

`set_config(..., is_local=true)` is transaction-scoped: it clears on COMMIT.
Before this listener, the next transaction on a reused session ran with no
user context — invisible today (the prod role bypasses RLS) but fatal under
the non-BYPASSRLS cutover role, where RLS would block every user-scoped query
after the first commit.

Real DB, no mocks (testing rules). The tests only read a session-level GUC and
roll back, so they seed nothing and are safe to run against any environment DB.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from src.db.session import rls_sync_session, system_sync_session

# Needs provisioned RLS cutover roles + RLS_ENFORCE — excluded from the unit CI job.
pytestmark = pytest.mark.integration


def test_guc_survives_commit_via_after_begin() -> None:
    """The RLS GUC is reapplied on the transaction that follows a commit."""
    uid = str(uuid.uuid4())
    with rls_sync_session(uid) as s:
        # First transaction — set by the explicit set_config + the listener.
        first = s.execute(
            text("SELECT current_setting('app.current_user_id', true)")
        ).scalar()
        assert first == uid, f"GUC not set on first transaction: {first!r}"

        # run_scrape_job-style mid-task commit ends the transaction and clears
        # the transaction-local GUC.
        s.commit()

        # The next query opens a NEW transaction. after_begin must reapply the
        # GUC from session.info — otherwise this comes back NULL/empty and RLS
        # would hide every row under a non-BYPASSRLS role.
        after = s.execute(
            text("SELECT current_setting('app.current_user_id', true)")
        ).scalar()
        assert after == uid, (
            "GUC NOT reapplied after commit — run_scrape_job would lose RLS "
            f"context under the cutover role. got {after!r}, expected {uid!r}"
        )
        s.rollback()


async def test_async_session_guc_survives_commit() -> None:
    """The async API session reapplies the GUC after a commit (Codex note D).

    `get_rls_db` binds `rls_user_id` on `db.sync_session.info`; the same
    `after_begin` listener must fire through the AsyncSession greenlet path.
    The sync path is proven above — this closes the async half before Phase 4.
    """
    from src.db.session import AsyncSessionLocal

    uid = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        # Mirror get_rls_db (src/api/deps.py): bind info + set the GUC.
        s.sync_session.info["rls_user_id"] = uid
        await s.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": uid},
        )
        first = (
            await s.execute(
                text("SELECT current_setting('app.current_user_id', true)")
            )
        ).scalar()
        assert first == uid, f"GUC not set on first async transaction: {first!r}"

        await s.commit()

        after = (
            await s.execute(
                text("SELECT current_setting('app.current_user_id', true)")
            )
        ).scalar()
        assert after == uid, (
            "GUC NOT reapplied after commit on the ASYNC session — API routes "
            f"would lose RLS context mid-request. got {after!r}, expected {uid!r}"
        )
        await s.rollback()


def test_system_session_carries_no_user_context() -> None:
    """system_sync_session must leave app.current_user_id unset.

    Cross-tenant work (canary/watchdog/ingest/retention) intentionally runs
    with no user GUC; the listener must no-op for it (session.info unset).
    """
    with system_sync_session() as s:
        got = s.execute(
            text("SELECT current_setting('app.current_user_id', true)")
        ).scalar()
        # current_setting(..., true) returns NULL (→ None) or '' when unset.
        assert got in (None, ""), (
            f"system_sync_session leaked a user GUC: {got!r} "
            "(cross-tenant ops must carry no RLS user context)"
        )
        s.rollback()
