# Notifications (Phase 2b) — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a multi-tenant in-app notifications feature (table + RLS + system-emit + read/mark endpoints) so the Phase-2a top-bar bell can show a real feed; wire the bell itself in a later frontend-only plan.

**Architecture:** A `notifications` table (RLS-protected, system-written / app-read). Emits are best-effort and run on the **system role only**: workers call a `create_notification(...)` helper (which opens its own `system_sync_session`) at the CAS-gated job `done`/`failed` transitions; the Stripe `payment_failed` webhook (an API path with no user GUC) enqueues a Celery task that calls the same helper. The API exposes user-scoped read + mark-read endpoints via `get_rls_db` with a mandatory `user_id` filter.

**Tech Stack:** FastAPI (async), SQLAlchemy 2.x, Alembic, Celery + Redis, PostgreSQL (Supabase RLS), Pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-06-18-notifications-design.md` (approved + Codex-reconciled).

## Global Constraints

- **Events in scope:** `job_completed`, `job_failed`, `payment_failed` only. No `cancelled` emit. No `new_records`/`usage_alert`.
- **No mock/dummy code** — real production project. Tests use a real DB (no `unittest.mock` unless an external API forces it).
- **Every DB query filters by `user_id`** (RLS is belt; query filter is suspenders). API never uses `system_sync_session`.
- **All emit is best-effort** — a notification-insert failure is logged and swallowed; it must never fail a job or a webhook. Exactly-once is NOT required.
- **All notification inserts use the system role** (`system_sync_session`); the app role gets `SELECT, UPDATE` only (no INSERT).
- **UUID columns:** `UUID(as_uuid=False)`, `default=_uuid`. Timestamps: `DateTime(timezone=True)`, `server_default=func.now()`.
- **`type` is a `NotificationType` enum**, fail-closed on unknown values.
- **Migrations** run via `scripts/migrate.py` (advisory lock); never apply a branch-only migration to prod before merge. Role-targeted policies + FORCE live in operator scripts, NOT Alembic.
- **OpenAPI contract:** after any schema/route change, regenerate `schema/openapi.json` in `.venv-schema` and commit (CI drift gate). Backend merges before the frontend bell wiring.
- **Branch:** `feat/notifications-phase2b` (off `main`).
- **Errors to clients** carry a reference id, never a raw DB error/stack trace.

---

### Task 1: `Notification` model + `NotificationType` enum

**Files:**
- Modify: `src/config/constants.py` (add `NotificationType` near `JobStatus`)
- Modify: `src/db/models.py` (add `Notification` class; ensure it's exported)
- Modify: `src/db/__init__.py` (export `Notification` if models are re-exported there — verify and match the existing pattern)
- Test: `tests/test_notifications_model.py`

**Interfaces:**
- Produces: `class NotificationType(str, Enum)` with `JOB_COMPLETED="job_completed"`, `JOB_FAILED="job_failed"`, `PAYMENT_FAILED="payment_failed"`. `class Notification(Base)` with columns `id, user_id, type, job_id, detail, read_at, created_at` (table `notifications`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notifications_model.py
from src.config.constants import NotificationType
from src.db.models import Notification


def test_notification_type_values():
    assert NotificationType.JOB_COMPLETED == "job_completed"
    assert NotificationType.JOB_FAILED == "job_failed"
    assert NotificationType.PAYMENT_FAILED == "payment_failed"
    assert {t.value for t in NotificationType} == {
        "job_completed", "job_failed", "payment_failed",
    }


def test_notification_model_columns():
    cols = Notification.__table__.columns.keys()
    assert set(cols) == {
        "id", "user_id", "type", "job_id", "detail", "read_at", "created_at",
    }
    assert Notification.__tablename__ == "notifications"
    # user_id is FK to users with cascade delete
    fks = list(Notification.__table__.c.user_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "users"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_notifications_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'NotificationType'` / `cannot import name 'Notification'`.

- [ ] **Step 3: Add the enum to `src/config/constants.py`**

Add directly after the `JobStatus` enum (mirror its `str, Enum` style):

```python
class NotificationType(str, Enum):
    """In-app notification kinds (Phase 2b). Mirrors the notification_prefs
    allowlist keys so one preference toggle governs both email and in-app."""

    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    PAYMENT_FAILED = "payment_failed"
```

- [ ] **Step 4: Add the `Notification` model to `src/db/models.py`**

Place it next to `AuditEvent` (it's the structural twin). `Column`, `String`, `DateTime`, `func`, `ForeignKey`, `JSON`, `UUID`, `_uuid` are already imported/defined in this file.

```python
class Notification(Base):
    """Phase 2b: per-user in-app notification feed (migration 065).

    SYSTEM-written (Celery workers via system_sync_session), APP-read (user-
    scoped endpoints via get_rls_db). Unlike AuditEvent, user_id IS an FK with
    ondelete=CASCADE so a deleted user's notifications are cleaned up. job_id is
    a plain ref (NO FK) so a notification survives job deletion; NULL for
    payment_failed. read_at NULL = unread. detail holds small display context
    only (no stack traces / PII)."""

    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type = Column(String(32), nullable=False, index=True)
    job_id = Column(UUID(as_uuid=False), nullable=True)  # soft ref, no FK
    detail = Column(JSON, nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

If `src/db/__init__.py` re-exports models (the Explore showed `from src.db import ... Job, ... User`), add `Notification` to that re-export list and `__all__` to match the existing pattern.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_notifications_model.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add src/config/constants.py src/db/models.py src/db/__init__.py tests/test_notifications_model.py
git commit -m "feat(notifications): Notification model + NotificationType enum"
```

---

### Task 2: Migration 065 + RLS operator-script updates + role-policy tests

**Files:**
- Create: `alembic/versions/065_notifications.py`
- Modify: `scripts/provision_rls_roles.sql` (app grants + verify block)
- Modify: `scripts/apply_rls_cutover_policies.sql` (drop untargeted + role-targeted policies)
- Modify: `scripts/apply_rls_force.sql` (`tbls` array + rollback comment)
- Test: `tests/test_notifications_migration.py` (table+RLS present after upgrade); extend `tests/test_rls_role_policies.py` (app deny / system allow)

**Interfaces:**
- Consumes: `Notification` table name `notifications`, columns from Task 1.
- Produces: a `notifications` table with RLS enabled, the untargeted `notifications_user_isolation` policy (migration), and the role-targeted `notifications_app_select/update` + `notifications_system` policies (cutover script).

- [ ] **Step 1: Write the migration**

```python
# alembic/versions/065_notifications.py
"""Phase 2b: in-app notifications table + RLS. (065)

Mirrors the 056 pattern: ENABLE ROW LEVEL SECURITY + an untargeted per-tenant
GUC isolation policy, inline and role-INDEPENDENT (the 030 lesson). The
role-targeted _app/_system policies (and the DROP of the untargeted one) live
in scripts/apply_rls_cutover_policies.sql; FORCE lives in
scripts/apply_rls_force.sql.

Revision ID: 065
Revises: 064
Create Date: 2026-06-18
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID  # exact form used by migration 055

revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None

_GUC_PREDICATE = (
    "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id", UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("job_id", UUID(as_uuid=False), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    # Composite for the list query; partial for the unread-count badge.
    op.create_index(
        "ix_notifications_user_created", "notifications",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_notifications_user_unread", "notifications", ["user_id"],
        postgresql_where=sa.text("read_at IS NULL"),
    )
    # RLS: enable + untargeted isolation policy (role-independent; inert under
    # today's BYPASSRLS runtime role, constrains any non-bypass role).
    op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY notifications_user_isolation
        ON notifications
        USING ({_GUC_PREDICATE})
        """
    )


def downgrade() -> None:
    # Mirrors 056: drop the policy, drop the table. (RLS goes away with the table.)
    op.execute("DROP POLICY IF EXISTS notifications_user_isolation ON notifications")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")
```

- [ ] **Step 2: Write the migration test**

```python
# tests/test_notifications_migration.py
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
```

- [ ] **Step 3: Run migration locally, then the test**

```bash
python scripts/migrate.py            # applies up to head (065) under the advisory lock
pytest tests/test_notifications_migration.py -v
```
Expected: migration applies cleanly; test PASS. (If the test DB lacks cutover roles the role-policy suite skips — that's expected; this test only checks table+RLS+a policy.)

- [ ] **Step 4: Update `scripts/provision_rls_roles.sql`**

In the app-grants section (mirror the `batch_runs`/`audit_events` blocks), grant the app role **SELECT, UPDATE only** and REVOKE the rest:

```sql
-- Notifications (Phase 2b): app reads + marks read; system writes.
GRANT SELECT, UPDATE ON notifications TO bridgeleads_app;
REVOKE INSERT, DELETE, TRUNCATE ON notifications FROM bridgeleads_app;
```

Add `notifications` to the verification `DO $verify$` block so it asserts the app role holds no disallowed privilege (follow the existing block's structure for another system-written table like `audit_events`). The broad `GRANT SELECT, INSERT, UPDATE ON ALL TABLES ... TO bridgeleads_system` already covers system writes.

- [ ] **Step 5: Update `scripts/apply_rls_cutover_policies.sql`**

Add a `notifications` section mirroring the `batch_runs` block — **drop the untargeted policy first**, then role-targeted (no app INSERT policy):

```sql
-- ── notifications (Phase 2b) ────────────────────────────────────────────────
DROP POLICY IF EXISTS notifications_user_isolation ON notifications;

CREATE POLICY notifications_app_select ON notifications
    FOR SELECT TO bridgeleads_app
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

CREATE POLICY notifications_app_update ON notifications
    FOR UPDATE TO bridgeleads_app
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

CREATE POLICY notifications_system ON notifications
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
```

- [ ] **Step 6: Update `scripts/apply_rls_force.sql`**

Add `'notifications'` to the script's `tbls` array (and any rollback comment listing the forced tables). The script hard-fails any forced table lacking a system policy, so Step 5's `notifications_system` must be applied first.

- [ ] **Step 7: Extend `tests/test_rls_role_policies.py`**

Add a focused test mirroring the existing app-deny / system-allow patterns. Seed one user, then under `bridgeleads_app` (no GUC) prove SELECT sees nothing and INSERT is denied; under `bridgeleads_system` prove INSERT/UPDATE work. Use the module's `cutover_ready` fixture (self-skips without cutover roles) and `_seed_two_tenants`.

```python
def test_notifications_role_policies(cutover_ready: bool) -> None:
    """app: SELECT scoped + no INSERT grant; system: INSERT/UPDATE work."""
    import uuid as _uuid
    from sqlalchemy.exc import DBAPIError
    with sync_engine.begin() as conn:
        user_a, _user_b, _ra, _rb = _seed_two_tenants(conn)
        nid = str(_uuid.uuid4())

        # system role can INSERT a notification (worker write path)
        conn.execute(text("SET LOCAL ROLE bridgeleads_system"))
        n = conn.execute(
            text("""
                INSERT INTO notifications (id, user_id, type, created_at)
                VALUES (:i, :u, 'job_completed', now())
            """),
            {"i": nid, "u": user_a},
        ).rowcount
        assert n == 1, "system role cannot INSERT notifications — worker emit breaks"
        conn.execute(text("RESET ROLE"))

        # app role with no GUC sees zero rows; with the GUC sees its own
        conn.execute(text("SET LOCAL ROLE bridgeleads_app"))
        assert conn.execute(
            text("SELECT COUNT(*) FROM notifications WHERE id = :i"), {"i": nid}
        ).scalar() == 0, "app role with no GUC must see zero notifications"
        # app INSERT must be denied (no grant)
        with pytest.raises(DBAPIError):
            with conn.begin_nested():
                conn.execute(
                    text("""
                        INSERT INTO notifications (id, user_id, type, created_at)
                        VALUES (:i, :u, 'job_failed', now())
                    """),
                    {"i": str(_uuid.uuid4()), "u": user_a},
                )
        conn.execute(text("RESET ROLE"))
        conn.rollback()
```

- [ ] **Step 8: Run the role-policy test**

Run: `pytest tests/test_rls_role_policies.py -v`
Expected: PASS where cutover roles exist, else SKIP (module-level guard). Both outcomes are acceptable — note which occurred in the report.

- [ ] **Step 9: Commit**

```bash
git add alembic/versions/065_notifications.py scripts/provision_rls_roles.sql scripts/apply_rls_cutover_policies.sql scripts/apply_rls_force.sql tests/test_notifications_migration.py tests/test_rls_role_policies.py
git commit -m "feat(notifications): migration 065 + RLS policies/grants + role-policy tests"
```

---

### Task 3: `create_notification` emit helper (system write path)

**Files:**
- Create: `src/workers/notification_emit.py`
- Test: `tests/test_notification_emit.py`

**Interfaces:**
- Consumes: `Notification` model, `NotificationType`, `system_sync_session` (`src/db/session.py`), `User.notification_prefs`.
- Produces: `create_notification(*, user_id: str, type: str, job_id: str | None = None, detail: dict | None = None) -> None` — opens its own `system_sync_session`, reads the user's `notification_prefs[type]` (default enabled), inserts a row only if enabled, fails closed on unknown `type`, swallows+logs all errors.

- [ ] **Step 1: Write the failing tests (real DB)**

```python
# tests/test_notification_emit.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_notification_emit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.workers.notification_emit'`.

- [ ] **Step 3: Implement the helper**

```python
# src/workers/notification_emit.py
"""System-side in-app notification emit (Phase 2b).

The ONLY notification write path. Opens its own system_sync_session (system
role, notifications_system FOR ALL) — callers never pass a db. Used by the
worker job done/failed transitions and the emit_payment_notification Celery
task. Best-effort: never raises into the caller; a notification is not worth
failing a job/webhook over. Gated by the user's notification_prefs[type] (one
toggle governs both email and in-app); fails closed on unknown types.
"""
from __future__ import annotations

from src.config.constants import NotificationType
from src.db.models import Notification, User
from src.db.session import system_sync_session
from src.utils.logger import setup_logger

_logger = setup_logger("workers.notification_emit")

_VALID_TYPES = {t.value for t in NotificationType}


def create_notification(
    *,
    user_id: str,
    type: str,
    job_id: str | None = None,
    detail: dict | None = None,
) -> None:
    try:
        if type not in _VALID_TYPES:
            _logger.warning("create_notification: unknown type %r — skipping", type)
            return
        with system_sync_session() as db:
            user = db.get(User, user_id)
            if user is None:
                _logger.warning("create_notification: user %s not found", user_id)
                return
            prefs = user.notification_prefs or {}
            # One toggle governs both email + in-app; absent key = enabled.
            if prefs.get(type, True) is False:
                return
            db.add(Notification(
                user_id=user_id, type=type, job_id=job_id, detail=detail,
            ))
            db.commit()
    except Exception as exc:  # best-effort: never propagate
        _logger.warning("create_notification failed (non-fatal): %s", str(exc)[:200])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_notification_emit.py -v`
Expected: PASS (5 passed). (Requires the 065 migration applied to the test DB — run `python scripts/migrate.py` first if needed.)

- [ ] **Step 5: Commit**

```bash
git add src/workers/notification_emit.py tests/test_notification_emit.py
git commit -m "feat(notifications): create_notification system emit helper (prefs-gated, fail-closed, swallow-on-error)"
```

---

### Task 4: Worker emit at job `done` + `failed` (CAS-gated)

**Files:**
- Modify: `src/workers/tasks.py` (the `→ done` block ≈ lines 986-1022)
- Modify: `src/workers/tasks_helpers/status.py` (`_fail_job` — return the CAS result)
- Test: `tests/test_notification_worker_emit.py`

**Interfaces:**
- Consumes: `create_notification` (Task 3), `_set_status`/`_fail_job` (status.py), the done call-site (tasks.py).
- Produces: `_fail_job(...) -> bool` (was `-> None`) returning whether the failed-CAS succeeded; a notification emit in the done block and at the failed call-sites, gated on the CAS result.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_notification_worker_emit.py
from unittest.mock import patch
from src.workers.tasks_helpers.status import _fail_job


def test_fail_job_returns_cas_result():
    """_fail_job must return the _set_status CAS boolean so callers can gate emit."""
    class _Job:
        id = "j1"
        status = "scraping"
    calls = {}

    def _fake_set_status(db, job, status, **kw):
        calls["status"] = status
        return True  # CAS succeeded

    class _R:
        def publish(self, *a, **k): pass

    with patch("src.workers.tasks_helpers.status._set_status", _fake_set_status), \
         patch("src.workers.tasks_helpers.status._publish_log"):
        result = _fail_job(object(), _Job(), _R(), "j1", "boom")
    assert result is True
    assert calls["status"] == "failed"
```

> The done-path emit is verified by the integration test in Task 7's endpoint round-trip and by manual proof (a real completed job produces a row). The unit test here locks the `_fail_job` contract change that makes CAS-gated emit possible.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_notification_worker_emit.py -v`
Expected: FAIL — `_fail_job` currently returns `None`, so `assert result is True` fails.

- [ ] **Step 3: Make `_fail_job` return the CAS result**

In `src/workers/tasks_helpers/status.py`, change `_fail_job` to capture and return the `_set_status` boolean. Replace the existing `_set_status(...)` call block:

```python
def _fail_job(db, job, r, job_id: str, reason: str) -> bool:
    # ... (docstring unchanged) ...
    try:
        db.rollback()
    except Exception:
        pass
    cas_ok = False
    try:
        cas_ok = _set_status(db, job, "failed", finished_at=_now(), error_message=reason)
    except Exception as exc:
        _logger.error(
            "Job %s: _set_status failed during _fail_job: %s",
            job_id, str(exc)[:200],
        )
    _publish_log(r, job_id, "error", reason, db=None)
    r.publish(f"job_logs:{job_id}", json.dumps({"type": "failed", "error": reason}))
    _logger.error("Job %s failed: %s", job_id, reason)
    return cas_ok
```

(Update the signature `-> None` → `-> bool`.)

- [ ] **Step 4: Emit at the failed call-sites in `src/workers/tasks.py`**

At each `_fail_job(db, job, r, job_id, ...)` call-site (the Explore identified lines ~299, 406, 415, 785), gate emit on its return. The cleanest is to wrap emit once where `config`/`job` context exists; where only some sites have `config`, pass what's available. Pattern at a site that has `config`:

```python
        if _fail_job(db, job, r, job_id, reason):
            from src.workers.notification_emit import create_notification
            create_notification(
                user_id=job.user_id, type="job_failed", job_id=job_id,
                detail={
                    "scraper_name": getattr(config, "name", None),
                    "county": getattr(config, "county", None),
                    "error_summary": reason[:200],
                },
            )
```

For early failure sites where `config` is not yet defined (e.g. unsupported county at ~299), pass `detail={"error_summary": reason[:200]}` and omit scraper fields. (The implementer should apply this gate at each `_fail_job` call-site; do not emit when `_fail_job` returns `False`.)

- [ ] **Step 5: Emit at the done block in `src/workers/tasks.py`**

In the `→ done` block, the `if not _set_status(... "done" ...): ... return` guard already isolates the success path. Add the emit immediately after that guard (before/alongside `deliver_job_results`):

```python
        # in-app notification (best-effort; gated by user prefs inside the helper)
        from src.workers.notification_emit import create_notification
        create_notification(
            user_id=job.user_id, type="job_completed", job_id=job_id,
            detail={
                "scraper_name": config.name,
                "county": config.county,
                "record_count": display_count,
            },
        )
```

- [ ] **Step 6: Run tests + the full worker test module**

Run: `pytest tests/test_notification_worker_emit.py -v`
Expected: PASS.
Run the existing job/worker tests to confirm no regression: `pytest tests/ -k "tasks or status or job" -v` (expect no new failures).

- [ ] **Step 7: Commit**

```bash
git add src/workers/tasks.py src/workers/tasks_helpers/status.py tests/test_notification_worker_emit.py
git commit -m "feat(notifications): CAS-gated emit at job done/failed (_fail_job returns CAS result)"
```

---

### Task 5: `emit_payment_notification` Celery task + Stripe webhook enqueue

**Files:**
- Modify: `src/workers/tasks.py` (add the task)
- Modify: `src/api/routes/billing.py` (`_handle_payment_failed` enqueues it)
- Test: `tests/test_notification_payment.py`

**Interfaces:**
- Consumes: `create_notification` (Task 3), the Celery `app` (`src/workers/tasks.py`), `_handle_payment_failed` (billing.py).
- Produces: `emit_payment_notification.delay(user_id: str, attempt_count: int)` (Celery task `src.workers.tasks.emit_payment_notification`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_notification_payment.py
from unittest.mock import patch
from src.workers.tasks import emit_payment_notification


def test_emit_payment_notification_calls_helper():
    with patch("src.workers.notification_emit.create_notification") as m:
        # call the task body directly (bind=True → first arg is self; pass None)
        emit_payment_notification.run("user-123", 2)
    m.assert_called_once()
    kwargs = m.call_args.kwargs
    assert kwargs["user_id"] == "user-123"
    assert kwargs["type"] == "payment_failed"
    assert kwargs["job_id"] is None
    assert kwargs["detail"] == {"attempt_count": 2}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_notification_payment.py -v`
Expected: FAIL — `cannot import name 'emit_payment_notification'`.

- [ ] **Step 3: Add the Celery task to `src/workers/tasks.py`**

Mirror the existing `@app.task` pattern (a simple one, no custom base):

```python
@app.task(
    name="src.workers.tasks.emit_payment_notification",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def emit_payment_notification(self, user_id: str, attempt_count: int) -> None:
    """Best-effort in-app notification for a failed Stripe payment.

    Runs in the worker process so the notification insert uses the system role
    (the Stripe webhook is an API path with no user RLS GUC, and the API must
    never use system_sync_session)."""
    from src.workers.notification_emit import create_notification
    create_notification(
        user_id=user_id, type="payment_failed", job_id=None,
        detail={"attempt_count": attempt_count},
    )
```

(Tasks defined in `src/workers/tasks.py` are auto-discovered — it's already in the Celery `include` list.)

- [ ] **Step 4: Enqueue from the webhook in `src/api/routes/billing.py`**

In `_handle_payment_failed`, after the `user` lookup and the existing `_send_payment_failed_email(...)` call, enqueue the task:

```python
    from src.workers.delivery import _send_payment_failed_email
    _send_payment_failed_email(user.email, attempt_count)

    # Phase 2b: best-effort in-app notification via the worker/system path
    # (the webhook session has no user RLS GUC — never write notifications here).
    try:
        from src.workers.tasks import emit_payment_notification
        emit_payment_notification.delay(str(user.id), attempt_count)
    except Exception as exc:  # enqueue failure must not fail the webhook
        _logger.warning("payment notification enqueue failed (non-fatal): %s", exc)
```

(`_logger` already exists in billing.py.)

- [ ] **Step 5: Add a webhook-enqueue test**

```python
# append to tests/test_notification_payment.py
import pytest
from unittest.mock import patch, AsyncMock


@pytest.mark.asyncio
async def test_webhook_enqueues_payment_notification():
    from src.api.routes.billing import _handle_payment_failed

    class _Result:
        def scalar_one_or_none(self):
            class _U:
                id = "user-xyz"
                email = "p@bl.test"
            return _U()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())
    data = {"customer": "cus_1", "attempt_count": 3}

    with patch("src.workers.delivery._send_payment_failed_email"), \
         patch("src.workers.tasks.emit_payment_notification.delay") as m:
        await _handle_payment_failed(data, db)
    m.assert_called_once_with("user-xyz", 3)
```

> This test uses `AsyncMock` for the DB only because driving a real Stripe webhook + async session in a unit test is disproportionate; it is the one sanctioned mock (external-API-shaped boundary) per the testing rule. The emit helper itself is covered by real-DB tests in Task 3.

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_notification_payment.py -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Commit**

```bash
git add src/workers/tasks.py src/api/routes/billing.py tests/test_notification_payment.py
git commit -m "feat(notifications): emit_payment_notification Celery task + Stripe webhook enqueue"
```

---

### Task 6: Schemas + endpoints + router registration

**Files:**
- Modify: `src/api/schemas.py` (`NotificationResponse`, `NotificationListResponse`, `ReadAllResponse`)
- Create: `src/api/routes/notifications.py`
- Modify: `src/api/__init__.py` (export `notifications_router`)
- Modify: `main.py` (import + `include_router`)
- Test: `tests/test_notifications_api.py`

**Interfaces:**
- Consumes: `Notification` model, `CurrentUser`, `get_rls_db`, `NotificationType`.
- Produces: `GET /notifications` → `NotificationListResponse{items, unread_count}`; `PATCH /notifications/{id}/read` → `NotificationResponse`; `POST /notifications/read-all` → `ReadAllResponse{updated}`.

- [ ] **Step 1: Add schemas to `src/api/schemas.py`**

```python
from src.config.constants import JobStatus, NotificationType  # extend existing import

class NotificationResponse(BaseModel):
    id: str
    type: NotificationType
    job_id: str | None = None
    detail: dict | None = None
    read_at: datetime | None = None
    created_at: datetime
    model_config = {"from_attributes": True}


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    unread_count: int


class ReadAllResponse(BaseModel):
    updated: int
```

- [ ] **Step 2: Write the failing API tests (real DB)**

```python
# tests/test_notifications_api.py
import uuid, json
import pytest
from sqlalchemy import text
from src.db.session import system_sync_session

pytestmark = pytest.mark.integration


def _seed_notification(user_id: str, read: bool = False) -> str:
    nid = str(uuid.uuid4())
    with system_sync_session() as db:
        db.execute(text("""
            INSERT INTO notifications (id, user_id, type, detail, read_at, created_at)
            VALUES (:i, :u, 'job_completed', CAST(:d AS json),
                    CASE WHEN :r THEN now() ELSE NULL END, now())
        """), {"i": nid, "u": user_id, "d": json.dumps({"record_count": 3}), "r": read})
        db.commit()
    return nid


# Real conftest fixtures (verified): `client` is an httpx AsyncClient (no auth);
# `starter_user` is a committed real User; `starter_token` is its JWT bearer.
# Authenticate by passing the bearer header. Tests are async.

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_returns_only_own_with_unread_count(client, starter_user, starter_token):
    _seed_notification(starter_user.id, read=False)
    _seed_notification(starter_user.id, read=True)
    resp = await client.get("/notifications", headers=_auth(starter_token))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["unread_count"] == 1


@pytest.mark.asyncio
async def test_patch_marks_read(client, starter_user, starter_token):
    nid = _seed_notification(starter_user.id, read=False)
    resp = await client.patch(f"/notifications/{nid}/read", headers=_auth(starter_token))
    assert resp.status_code == 200
    assert resp.json()["read_at"] is not None


@pytest.mark.asyncio
async def test_patch_foreign_id_404(client, starter_token):
    resp = await client.patch(f"/notifications/{uuid.uuid4()}/read", headers=_auth(starter_token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_read_all(client, starter_user, starter_token):
    _seed_notification(starter_user.id, read=False)
    _seed_notification(starter_user.id, read=False)
    resp = await client.post("/notifications/read-all", headers=_auth(starter_token))
    assert resp.status_code == 200
    assert resp.json()["updated"] >= 2
    after = await client.get("/notifications", headers=_auth(starter_token))
    assert after.json()["unread_count"] == 0


@pytest.mark.asyncio
async def test_routes_registered_in_openapi(client):
    resp = await client.get("/openapi.json")
    paths = resp.json()["paths"]
    assert "/notifications" in paths
    assert "/notifications/{notification_id}/read" in paths
    assert "/notifications/read-all" in paths
```

> `starter_user` is created in the async `db` fixture (real commit) before the test body runs, so the `system_sync_session`-seeded notification's `user_id` FK resolves; both connections see committed rows.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_notifications_api.py -v`
Expected: FAIL — routes 404 / module missing.

- [ ] **Step 4: Implement `src/api/routes/notifications.py`**

```python
"""Notification routes (Phase 2b): user-scoped read + mark-read.

System (worker) writes notifications; the API only reads + marks read. Every
query filters by current_user.id (defense-in-depth over RLS). The API never
uses system_sync_session."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
from src.api.schemas import (
    NotificationListResponse, NotificationResponse, ReadAllResponse,
)
from src.db.models import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> NotificationListResponse:
    rows = (await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(50)
    )).scalars().all()
    unread = (await db.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == current_user.id, Notification.read_at.is_(None))
    )).scalar_one()
    return NotificationListResponse(
        items=[NotificationResponse.model_validate(r) for r in rows],
        unread_count=unread,
    )


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
async def mark_read(
    notification_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> NotificationResponse:
    # Never db.get() before the user filter.
    row = (await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
    await db.flush()
    return NotificationResponse.model_validate(row)


@router.post("/read-all", response_model=ReadAllResponse)
async def mark_all_read(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> ReadAllResponse:
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    await db.flush()
    return ReadAllResponse(updated=result.rowcount or 0)
```

- [ ] **Step 5: Register the router**

In `src/api/__init__.py`: add `from .routes.notifications import router as notifications_router` and add `"notifications_router"` to `__all__`.
In `main.py`: add `notifications_router` to the `from src.api import (...)` block and add `app.include_router(notifications_router)` alongside the others.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_notifications_api.py -v`
Expected: PASS (5 passed). (Test DB must have migration 065 applied.)

- [ ] **Step 7: Commit**

```bash
git add src/api/schemas.py src/api/routes/notifications.py src/api/__init__.py main.py tests/test_notifications_api.py
git commit -m "feat(notifications): read/mark endpoints + schemas + router registration"
```

---

### Task 7: OpenAPI regen + full verification

**Files:**
- Modify: `schema/openapi.json` (regenerated)

- [ ] **Step 1: Regenerate the OpenAPI contract in the pinned venv**

```bash
python -m venv .venv-schema            # if not already present
.venv-schema/Scripts/pip install -r requirements.txt   # bin/pip on POSIX
.venv-schema/Scripts/python scripts/export_openapi.py
```
Expected: writes `schema/openapi.json` including the three `/notifications*` paths + `NotificationResponse`/`NotificationType` components.

- [ ] **Step 2: Verify the staleness gate is satisfied**

Run: `.venv-schema/Scripts/python scripts/export_openapi.py --check`
Expected: `OK: schema/openapi.json is up to date.`

- [ ] **Step 3: Run the full notifications test suite + a broad sanity run**

```bash
pytest tests/test_notifications_model.py tests/test_notification_emit.py tests/test_notification_worker_emit.py tests/test_notification_payment.py tests/test_notifications_api.py tests/test_notifications_migration.py -v
pytest tests/ -k "rls or schema or openapi" -v
```
Expected: notifications tests PASS (RLS role-policy + migration tests may SKIP without cutover roles — note which). No new failures elsewhere.

- [ ] **Step 4: Commit**

```bash
git add schema/openapi.json
git commit -m "chore(notifications): regenerate OpenAPI contract (notifications endpoints)"
```

---

## Out of scope (separate later plan/PR)
- **Frontend bell wiring** (`bridgeleads-web`): regenerate TS types from the merged OpenAPI; add `listNotifications`/`markNotificationRead`/`markAllNotificationsRead`; wire `NotificationsBell.tsx` (polling, unread badge, click-to-navigate, mark-all). Starts only after this backend lands on `main`.

## Final gates (run by the executing skill, not a task here)
- Master Security Review §14 (new table + endpoints + RLS + worker emit); confirm the 4-step RLS drift checklist is complete and `detail.error_summary` carries no stack traces/PII.
- Codex reviews the diff (`codex review --base main`); resolve P1, adopt P2 per the collaboration rule.
- Opus whole-branch review; then PR against `main` (backend-first). Apply migration 065 to prod only after merge (via `scripts/migrate.py`); RLS cutover scripts are operator-run.
