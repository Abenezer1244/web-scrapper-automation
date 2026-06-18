# Phase 2b — In-App Notifications — Design Spec

**Date:** 2026-06-18
**Repos:** backend `web-scrapper-automation` (primary), frontend `bridgeleads-web` (consumer)
**Backend branch:** `feat/notifications-phase2b` (off `main`)
**Status:** Approved (brainstorm), pending Codex consult → implementation plan

---

## 1. Purpose & scope

Give users an in-app notification feed behind the top-bar bell that Phase 2a shipped as an inert empty state (`bridgeleads-web/components/shell/NotificationsBell.tsx`). This is a **greenfield** feature: there is no `notifications` table and no per-user notification concept today — only fire-and-forget transactional emails (`src/workers/delivery.py`) and operator alerts (`src/workers/ops_alerts.py`).

**Events in scope (Phase 2b):** `job_completed`, `job_failed`, `payment_failed`. These three have clean existing emit points and are unambiguous "you need to know this" events. **Out of scope:** `new_records` (too noisy) and `usage_alert` (needs a threshold detector that does not exist) — deferred to a later phase. **No emit on `cancelled`** (user-initiated; nothing to tell them).

**Non-goals:** real-time push, new settings UI, auto-pruning, replacing any existing email.

### Success criteria
- A completed/failed scrape and a failed payment each create exactly one user-scoped notification row (when the user's pref for that type is enabled).
- The bell shows an unread-count badge that updates by polling and clears as the user reads items.
- A user can never read another tenant's notifications (RLS + mandatory `user_id` query filter).
- The OpenAPI contract stays in sync; backend merges before the frontend wiring.

---

## 2. Data model — `notifications` table (Alembic migration 065)

Mirrors the `AuditEvent` (`src/db/models.py`) and `Job` conventions: string UUIDs (`UUID(as_uuid=False)`, `default=_uuid`), tz-aware `created_at`.

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID(as_uuid=False)` PK | `default=_uuid` |
| `user_id` | `UUID(as_uuid=False)` FK→`users.id` `ondelete=CASCADE`, `nullable=False`, indexed | tenant scope (FK so a deleted user's rows are cleaned up — unlike `AuditEvent`, which is FK-less to survive deletion) |
| `type` | `String(32)`, `nullable=False`, indexed | `job_completed` \| `job_failed` \| `payment_failed` |
| `job_id` | `UUID(as_uuid=False)`, `nullable=True`, **no FK** | soft link (survives job deletion), like `Result.nts_notice_id`; `NULL` for `payment_failed` |
| `detail` | `JSON`, `nullable=True` | `{ "scraper_name": str?, "county": str?, "record_count": int?, "error_summary": str? }` — the frontend builds title/message from `type` + `detail`. Keep `error_summary` short and free of stack traces/PII. |
| `read_at` | `DateTime(timezone=True)`, `nullable=True` | unread = `NULL` |
| `created_at` | `DateTime(timezone=True)`, `server_default=func.now()`, `nullable=False`, indexed | newest-first ordering |

Model: `Notification(Base)` in `src/db/models.py`. Volume is low (≈1 row/job + rare payment events), so the `(user_id)` and `(created_at)` indexes are created normally in the migration — **not** `CONCURRENTLY` (the >50k-row caution from migrations 062/064 does not apply here).

### RLS — the 4-step drift checklist (verbatim to existing precedent)
1. **In migration 065:** `ALTER TABLE notifications ENABLE ROW LEVEL SECURITY` + an untargeted isolation policy (the role-independent pattern from migration 056):
   ```sql
   CREATE POLICY notifications_user_isolation ON notifications
     USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
   ```
   No `FORCE` and no role-targeted DDL in Alembic (the 030/031 lesson: role-conditional DDL no-ops in CI and never re-runs).
2. **`scripts/provision_rls_roles.sql`:** grant `bridgeleads_app` `SELECT, INSERT, UPDATE` on `notifications`; the broad system grant already covers it. Add `notifications` to the verification `DO $verify$` block's expectations and **REVOKE DELETE** from the app role (app never deletes notifications in 2b).
3. **`scripts/apply_rls_cutover_policies.sql`:** role-targeted policies:
   ```sql
   CREATE POLICY notifications_app_select ON notifications FOR SELECT TO bridgeleads_app
     USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
   CREATE POLICY notifications_app_insert ON notifications FOR INSERT TO bridgeleads_app
     WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
   CREATE POLICY notifications_app_update ON notifications FOR UPDATE TO bridgeleads_app
     USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
     WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
   CREATE POLICY notifications_system ON notifications FOR ALL TO bridgeleads_system
     USING (true) WITH CHECK (true);
   ```
4. **`scripts/apply_rls_force.sql`:** add `ALTER TABLE notifications FORCE ROW LEVEL SECURITY` (operator cutover script only).

---

## 3. Backend — emit + endpoints

### Emit helper
`create_notification(db, *, user_id, type, job_id=None, detail=None)` — the single insert point. It **reads `User.notification_prefs[type]` and inserts only if enabled** (one toggle governs both email and in-app — consistent with existing email gating). Defaults to enabled if the key is absent. Idempotency is not required (one terminal transition → one call), but the helper must not raise into the job's critical path — a notification-insert failure is logged and swallowed (a notification is not worth failing a completed job over), consistent with the existing fire-and-forget email behavior.

Emit call-sites (all already carry `user_id`):
- `src/workers/tasks.py` `→ done` (≈ line 991, alongside `deliver_job_results`) → `type="job_completed"`, `job_id`, `detail={scraper_name, county, record_count}`.
- `src/workers/tasks_helpers/status.py` `_fail_job` (or the `tasks.py` fail call-sites) `→ failed` → `type="job_failed"`, `job_id`, `detail={scraper_name, county, error_summary}`.
- Stripe `payment_failed` path (alongside `_send_payment_failed_email`) → `type="payment_failed"`, `job_id=None`, `detail={attempt_count}`.

**Write path:** workers run outside any request, so they insert via **`system_sync_session()`** (system role, covered by `notifications_system FOR ALL`). The API path never uses `system_sync_session` (session.py constraint).

### Endpoints (`src/api/routes/notifications.py`)
All use `current_user: CurrentUser` + `db = Depends(get_rls_db)` and the **mandatory `.where(Notification.user_id == current_user.id)` filter** (defense-in-depth on top of RLS). Router `prefix="/notifications"`.

- `GET /notifications` → `{ items: NotificationResponse[], unread_count: int }`. `items` = newest 50 (`order_by(created_at.desc()).limit(50)`); `unread_count` = `COUNT(read_at IS NULL)` for the user (one wrapper response avoids a second round-trip).
- `PATCH /notifications/{id}/read` → set `read_at = now()` on that row (scoped to the user); returns the updated `NotificationResponse`. 404 if not owned/not found.
- `POST /notifications/read-all` → set `read_at = now()` for all unread rows of the user; returns `{ updated: int }`.

### Schemas & types
- `NotificationType(str, Enum)` in `src/config/constants.py` (`job_completed`/`job_failed`/`payment_failed`).
- `NotificationResponse(BaseModel)` in `src/api/schemas.py` (`id, type: NotificationType, job_id: str | None, detail: dict | None, read_at: datetime | None, created_at: datetime`, `model_config={"from_attributes": True}`); plus `NotificationListResponse(items, unread_count)` and `ReadAllResponse(updated)`.
- **Regenerate `schema/openapi.json` in `.venv-schema`** and commit (CI drift gate). The status/type union must be a Python `Enum` so OpenAPI emits the literal union for the frontend.

---

## 4. Frontend — bell wiring (`bridgeleads-web`, separate branch/PR, after backend merges)

- Regenerate TS types from the merged OpenAPI; add `lib/api.ts`: `listNotifications()`, `markNotificationRead(id)`, `markAllNotificationsRead()`.
- `NotificationsBell.tsx`: react-query `["notifications"]` with `refetchInterval` ≈ 45s + refetch-on-window-focus. **Unread-count badge** on the bell (display cap `9+`). Dropdown lists items (type icon + title + relative time, message from `detail`). Clicking an item → `markNotificationRead(id)` (optimistic) + navigate:
  - `job_completed` → `/results/{job_id}`
  - `job_failed` → `/live/{job_id}` if still running else `/results/{job_id}` (reuse `isRunning`)
  - `payment_failed` → `/settings?tab=billing`
- **"Mark all as read"** action in the dropdown header → `markAllNotificationsRead()` (optimistic badge clear). Preserve the real empty state when there are none. All colors via Phase-1 tokens; destructive/error red, success green per the darkmatter rules.

---

## 5. Defaulted decisions (locked unless revised)
- **Retention:** no auto-prune in 2b; `GET` caps at newest 50; badge counts all unread.
- **Complement, not replace:** existing transactional emails unchanged; in-app is additive.
- **No realtime, no `cancelled` emit, no new settings UI** (reuses existing `notification_prefs` keys).

---

## 6. Testing (real DB, no mocks)
- **RLS isolation:** user A cannot `SELECT`/`PATCH` user B's notifications (needs the OWNER DSN per the existing RLS test pattern).
- **Emit:** job→done and job→failed each create one row with correct `type`/`job_id`/`detail`; emit is **suppressed when the user's pref for that type is disabled**; `payment_failed` path inserts a row.
- **Endpoints:** `GET` returns only the caller's rows, newest-first, with correct `unread_count`; `PATCH /{id}/read` sets `read_at` and 404s on a foreign id; `POST /read-all` marks only the caller's unread.
- **Emit safety:** a forced notification-insert failure does not fail the job (helper swallows + logs).

---

## 7. Security & rollout
- **Master Security Review §14** after the build (new table + endpoints + RLS + worker emit); confirm the 4-step drift checklist is complete; no PII/stack traces in `detail.error_summary`; API never uses `system_sync_session`; errors to clients carry a reference id, not raw DB errors.
- **Codex** consulted on this design before implementation, and reviews the diff after the build (per `.claude/rules/codex-collaboration.md`).
- **Rollout order:** backend-first — migration 065 (via `scripts/migrate.py` advisory lock; **do not apply the branch-only migration to prod until merged** — crash-loop landmine) → endpoints → OpenAPI regen + commit → merge. Then the frontend branch consumes the merged contract. RLS cutover scripts (`provision_rls_roles.sql` / `apply_rls_cutover_policies.sql` / `apply_rls_force.sql`) are operator-run at deploy, not in Alembic.

---

## 8. Component boundaries (one responsibility each)
- `Notification` model — schema only.
- migration 065 + the three RLS scripts — DDL + isolation.
- `create_notification(...)` helper — the gated, swallow-on-error emit primitive (one place all emit points call).
- `src/api/routes/notifications.py` — read + mark endpoints (user-scoped).
- `NotificationsBell.tsx` — presentation + polling + read actions (consumes the typed API).
