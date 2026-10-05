# Profile & Account system redesign (2026-10-05)

BE worktree `C:/Users/Windows/bl-wt/profile-be` (branch `feat/profile-account-redesign`, base main `96802c9d`).
FE worktree `C:/Users/Windows/bl-wt/profile-fe` (branch `feat/profile-account-redesign`, base master `a6b03eb`).
No merge, no deploy without owner OK. Each phase <= 5 files, verified (pytest / tsc / eslint), Codex diff review.

## Owner decisions (2026-10-05)
- Sessions/devices: IN (browser/OS + last active; no IP, no geolocation).
- Email change: IN (secure verify flow).
- Danger zone (delete/export): design doc only; no UI until approved.
- Prod data check: 8 users, all have first+last name, legacy `name` unused -> no backfill.
- Avatar storage: Postgres table `user_avatars` (bytea WebP 256px, separate from users), served via authenticated API. Removes R2 atomicity P1.
- Session absolute lifetime: 30 days (enforced at refresh via user_sessions.created_at).
- Cadence: run all phases, stop only on Critical/High or decisions. No merge/deploy without owner.

## Codex consult (architecture) -> reconciliation
GATE: FAIL with 10 P1. Verified each against code:
| Codex P1 | Verdict | Action |
|---|---|---|
| Pillow bomb / memory | AGREE | cap 4096x4096 (MAX_IMAGE_PIXELS), DecompressionBombWarning -> error, reject animated + truncated, decode in threadpool behind a semaphore |
| R2 + PG not atomic | AGREE (if R2) | see storage decision; Postgres bytea removes it entirely |
| Redis-only revocation durability | PRE-EXISTING, partly | new per-session revoke writes `user_sessions.revoked_at` (DB authoritative, checked on refresh) + Redis family marker -> worst case bounded by 1h access TTL. System-wide Redis eviction policy = separate PR |
| Family has no absolute lifetime | CONFIRMED pre-existing (refresh re-mints 7d in same fam, login.py:540) | owner decision: enforce absolute session lifetime via user_sessions.created_at? |
| Refresh grace concurrency | REJECT: already handled (remember_rotation + _await_rotation_result, login.py:476-500) | none |
| revoke-others legacy race | AGREE problem, different fix | lazy-adopt: refresh inserts a row for any fam with no row (fam-less legacy tokens already get a fam on refresh), so within 1h every live session is listed; revoke-others revokes listed families. No cutoff/reissue dance |
| Pending email rows mutable | AGREE | new row (new uuid) per request, prior pending deleted; JWT sub=row id + jti consume_once |
| Email/Stripe side effects need retry | AGREE | Celery tasks with retry for notices + Stripe customer email sync |
| CSRF on Next proxy | REJECT: browser calls API directly with bearer (lib/api.ts:50) | none. Logout already revokes family (routes/auth.py:304) |
| avatar_url in Auth.js JWT | AGREE as constraint | identity only from `["me"]` query, never the Auth.js JWT |
Adopted P2s: fixed DTO for security events (no detail/path/ip), sanitize forwarded UA, generic email-change responses incl. confirm failure, fragment token cleared via history.replaceState, "Last active" updated throttled on authenticated use.

## Plan (phases, <= 5 files each)
- [ ] P0 GATES.md + this plan; owner approval
- [ ] P1 BE profile fields: migration (avatar storage + users.timezone + user_sessions + pending_email_changes), models, schemas (timezone on ProfileUpdate, avatar_url/timezone on UserResponse)
- [ ] P2 BE avatar: Pillow dep (SBOM check), avatar util, POST/DELETE /auth/avatar + GET image, tests (JPEG/PNG/WebP, invalid, oversized, bomb, animated, replace, remove, cross-user)
- [ ] P3 BE sessions: write row at every login path + lazy-adopt on refresh, X-Client-User-Agent, GET/DELETE /auth/sessions, revoke-others, last-active, tests
- [ ] P4 BE security events + email change: GET /auth/security-events, POST /auth/email/change, POST /auth/email/confirm, Celery notices + Stripe sync, tests (verify, invalid, duplicate, expired, unauthorized)
- [ ] P5 FE identity: UserAvatar + initials util (+ node:test), UserMenu redesign (Billing removed), Toaster theme fix, regen api types
- [ ] P6 FE Account tab: identity header, avatar editor dialog (crop/zoom/reposition, file + drag/drop + mobile camera via accept), personal info single Save, timezone, change-email dialog, confirm-email page
- [ ] P7 FE Settings nav + Security: ?tab routing, mobile list->detail, password moved to Security, sessions list, security events
- [ ] P8 Danger-zone design doc (deletion + export architecture, retention, Stripe, R2)
- [ ] P9 Verification: pytest, tsc, eslint, Playwright/Chromium walkthrough 320-1440, light/dark, a11y, cross-user, console/network; screenshots
- [ ] P10 Security Master Review x2 + Codex diff review; BUILD_JOURNAL entry; review section here

## Out of scope / separate PRs
- Redis maxmemory policy for revocation markers (docker-compose.prod.yml:99 says allkeys-lru)
- Timezone applied to schedule UI / timestamp formatting app-wide
- Workspace/account split (plan, Stripe, quota live on users)
- API key revoke endpoint + api_key_revoked event (key only cleared as side effect today)
- Delete account / export data implementation
