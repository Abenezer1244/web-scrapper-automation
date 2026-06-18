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

Model: `Notification(Base)` in `src/db/models.py`. Volume is low (≈1 row/job + rare payment events), so indexes are created normally in the migration — **not** `CONCURRENTLY` (the >50k-row caution from migrations 062/064 does not apply here).

**Indexes (match the actual queries — Codex P2):**
- Composite `ix_notifications_user_created` on `(user_id, created_at DESC)` — serves `GET /notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50`.
- Partial `ix_notifications_user_unread` on `(user_id) WHERE read_at IS NULL` — serves the `unread_count` and keeps it cheap as old unread rows accumulate.
- (No standalone single-column `user_id`/`created_at` index — the composite covers `user_id` prefix lookups.)

### RLS — the 4-step drift checklist (verbatim to existing precedent)
1. **In migration 065:** `ALTER TABLE notifications ENABLE ROW LEVEL SECURITY` + an untargeted isolation policy (the role-independent pattern from migration 056):
   ```sql
   CREATE POLICY notifications_user_isolation ON notifications
     USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
   ```
   No `FORCE` and no role-targeted DDL in Alembic (the 030/031 lesson: role-conditional DDL no-ops in CI and never re-runs).
2. **`scripts/provision_rls_roles.sql`:** grant `bridgeleads_app` **`SELECT, UPDATE` only** on `notifications` (the app endpoints only read and mark-read; **all inserts come from the system role** — see §3 — so the app gets no INSERT surface, per Codex P2 least-privilege). The broad system grant (`GRANT SELECT, INSERT, UPDATE … TO bridgeleads_system`) already covers writes. Add `notifications` to the verification `DO $verify$` block's expectations and **REVOKE INSERT, DELETE** from the app role.
3. **`scripts/apply_rls_cutover_policies.sql`:** **first `DROP POLICY IF EXISTS notifications_user_isolation ON notifications`** (the repo's cutover pattern drops the untargeted policy when installing role-targeted ones — Codex P2), then:
   ```sql
   CREATE POLICY notifications_app_select ON notifications FOR SELECT TO bridgeleads_app
     USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
   CREATE POLICY notifications_app_update ON notifications FOR UPDATE TO bridgeleads_app
     USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
     WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
   CREATE POLICY notifications_system ON notifications FOR ALL TO bridgeleads_system
     USING (true) WITH CHECK (true);
   ```
   (No `notifications_app_insert` — the app never inserts.)
4. **`scripts/apply_rls_force.sql`:** add `notifications` to the script's `tbls` array (and the rollback comment) so it's included in the FORCE cutover — the script hard-fails any forced table lacking a system policy, so step 3's `notifications_system` must exist first (Codex P2).

---

## 3. Backend — emit + endpoints

### Emit helper — unified system write path
`create_notification(*, user_id, type, job_id=None, detail=None)` — the single insert point. **It opens its OWN `system_sync_session()`** (system role, covered by `notifications_system FOR ALL`) — it does NOT take a caller's `db`. This matters: the only correct insert path is the system role, because (a) the worker's job session is `rls_sync_session(job.user_id)` and we want notification emit decoupled from the job's transaction, and (b) the Stripe webhook's session has no user GUC at all (see payment path below). Inside, it **reads `User.notification_prefs[type]` and inserts only if enabled** (one toggle governs both email and in-app — defaults to enabled if the key absent), and **fails closed on an unknown `type`** (Codex P3). The helper never raises into the caller — a notification-insert failure is logged and swallowed (a notification is not worth failing a completed job or a webhook over). Exactly-once is **not** required; emit is best-effort (the email is the primary channel).

**This helper runs in the WORKER process only.** Call-sites:

- **`job_completed`** — `src/workers/tasks.py` `→ done` (≈ line 991). The done transition already checks the `_set_status(... "done")` CAS return before delivering email; emit the notification in the **same `if cas_succeeded:` block, after the CAS commit** (Codex P1/P2 — never emit if the row was already terminalized). `detail={scraper_name, county, record_count}`.
- **`job_failed`** — gate emit on the `_set_status(..., "failed")` CAS result. `_fail_job` (`src/workers/tasks_helpers/status.py:156`) currently **ignores** that boolean — thread it out (return it) and emit `job_failed` only when the failed-transition actually occurred (Codex P1). `detail={scraper_name, county, error_summary}` (short, no stack trace/PII).
- **`payment_failed`** — runs in the **FastAPI Stripe webhook** (`src/api/routes/billing.py`, `Depends(get_db)`, no user GUC), NOT a worker. The API process must never use `system_sync_session` (non-negotiable). So the webhook **enqueues a Celery task** `emit_payment_notification.delay(user_id=…, attempt_count=…)`; the worker task calls `create_notification(type="payment_failed", job_id=None, detail={attempt_count})`. The user is resolved via the unique `stripe_customer_id` (migration 019), which the webhook already does to send the email. Best-effort: if the enqueue/insert is lost after Stripe's event is claimed (Redis dedup → no retry), the notification is simply absent — acceptable for a supplementary channel.

### Endpoints (`src/api/routes/notifications.py`)
All use `current_user: CurrentUser` + `db = Depends(get_rls_db)` and the **mandatory `.where(Notification.user_id == current_user.id)` filter** (defense-in-depth on top of RLS). Router `prefix="/notifications"`. **Wire it up** (Codex P1): register the router in `src/api/__init__.py` and `app.include_router(...)` in `main.py` — otherwise the endpoints + OpenAPI silently never appear.

- `GET /notifications` → `{ items: NotificationResponse[], unread_count: int }`. `items` = newest 50 (`order_by(created_at.desc()).limit(50)`); `unread_count` = `COUNT(read_at IS NULL)` for the user (one wrapper response avoids a second round-trip).
- `PATCH /notifications/{id}/read` → set `read_at = now()`. **Fetch via `select(Notification).where(id == … , user_id == current_user.id)` — never `db.get(Notification, id)` before applying the user filter** (Codex P2). 404 if not owned/not found.
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
- **RLS role-policy suite (extend `tests/test_rls_role_policies.py`, not just endpoint tests — Codex P3):** under the `bridgeleads_app` role, `SELECT`/`UPDATE` are scoped to `app.current_user_id` and a foreign-user row is invisible/unwritable; **app `INSERT` is denied** (no grant); under `bridgeleads_system`, `INSERT`/`UPDATE` succeed (`FOR ALL`). Policies can be right while grants are missing — this suite catches that. (Needs the OWNER DSN per the existing pattern.)
- **Route-wiring test:** the app's OpenAPI/paths include `/notifications`, `/notifications/{id}/read`, `/notifications/read-all` (mirror the batch route-wiring tests) — proves the router was registered.
- **Emit:** job→done and job→failed each create one row with correct `type`/`job_id`/`detail`, **only when the `_set_status` CAS returns True**; emit is **suppressed when the user's pref for that type is disabled**; unknown `type` fails closed; the `emit_payment_notification` Celery task inserts a `payment_failed` row.
- **Endpoints:** `GET` returns only the caller's rows, newest-first, with correct `unread_count`; `PATCH /{id}/read` sets `read_at` and 404s on a foreign id (and does not leak existence); `POST /read-all` marks only the caller's unread.
- **Emit safety:** a forced notification-insert failure does not fail the job or the webhook (helper swallows + logs).

## 6a. Codex consult reconciliation (folded into this spec)
Pressure-tested before implementation (`.claude/rules/codex-collaboration.md`). Findings adopted:
- **P1 — payment_failed write path:** the Stripe webhook is an API path with no user GUC; inserting under an app policy would fail post-cutover. → All emits use the system role; the webhook **enqueues a Celery task** rather than writing directly (API never uses `system_sync_session`).
- **P1 — failed-job CAS:** emit gated on the `_set_status` CAS result (thread it out of `_fail_job`); emit after the commit, never for an already-terminal row.
- **P1 — router wiring:** register in `src/api/__init__.py` + `main.py`.
- **P2:** drop the untargeted isolation policy at cutover; app role = `SELECT, UPDATE` only (no INSERT); add `notifications` to `apply_rls_force.sql`; composite `(user_id, created_at DESC)` + partial unread index; PATCH filters by `user_id` (no `db.get` first).
- **P3:** enum `type` + fail-closed on unknown; extend the role-policy test suite + add a route-wiring test.
Confirmed-correct by Codex: `system_sync_session` as the worker write path; `get_rls_db` + mandatory `user_id` filter on reads; prefs gating from a system session (`users_system FOR ALL` exists); OpenAPI drift gate.

---

## 7. Security & rollout
- **Master Security Review §14** after the build (new table + endpoints + RLS + worker emit); confirm the 4-step drift checklist is complete; no PII/stack traces in `detail.error_summary`; API never uses `system_sync_session`; errors to clients carry a reference id, not raw DB errors.
- **Codex** consulted on this design before implementation, and reviews the diff after the build (per `.claude/rules/codex-collaboration.md`).
- **Rollout order:** backend-first — migration 065 (via `scripts/migrate.py` advisory lock; **do not apply the branch-only migration to prod until merged** — crash-loop landmine) → endpoints → OpenAPI regen + commit → merge. Then the frontend branch consumes the merged contract. RLS cutover scripts (`provision_rls_roles.sql` / `apply_rls_cutover_policies.sql` / `apply_rls_force.sql`) are operator-run at deploy, not in Alembic.

---

## 8. Component boundaries (one responsibility each)
- `Notification` model — schema only.
- migration 065 + the three RLS scripts — DDL + isolation.
- `create_notification(...)` helper — the gated, swallow-on-error emit primitive that owns its own `system_sync_session` (worker-process only; one place all emit points call).
- `emit_payment_notification` Celery task — the webhook's bridge into the worker/system write path for `payment_failed`.
- `src/api/routes/notifications.py` — read + mark endpoints (user-scoped); registered in `src/api/__init__.py` + `main.py`.
- `NotificationsBell.tsx` — presentation + polling + read actions (consumes the typed API).
