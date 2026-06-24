# Editable user display name — kill the email-stub dashboard greeting

**Problem:** Dashboard greets "Good evening, mikitsegaye29" — `DashboardHeader.tsx`
derives the name from `emailFirstPart(userEmail)` because the system stores **no
name anywhere**. `users` has only `email` (encrypted); registration takes
email+password+ref; `/auth/me` returns no name.

**Fix (user chose: real, editable name):** add an editable display name; greet
with it; never fall back to the email stub.

## Codex consult (done — design reconciled)
- (a) Column type → **EncryptedString** (nullable, no blind index). Matches the
  encrypt-PII-at-rest posture; we never query by name.
- (b) Endpoint → **`PUT /auth/profile`**, mirroring the existing
  `PUT /auth/notification-preferences` pattern (Codex blessed for local
  consistency; its isolated pick was `PATCH /auth/me`).
- (c) Validation → NFC normalize → collapse Unicode whitespace → reject control
  /format chars (covers bidi overrides U+202A-202E/U+2066-2069 + zero-width
  U+200B/C/D/FEFF) → empty-after-strip => NULL → 120 code points AND 255 UTF-8
  bytes. One shared validator for register + profile.
- Extras adopted: nullable (no server_default/backfill), no JWT reissue,
  own-row-only update, **audit logs the action not the value**.

## Phase 1 — Backend (worktree `feat/user-display-name`, DONE + Codex-clean)
- [x] 1. `alembic/versions/071_user_display_name.py` — add `users.name` (Text, nullable)
- [x] 2. `src/db/models.py` — `name = Column(EncryptedString, nullable=True)`
- [x] 3. `src/api/schemas.py` — `_validate_display_name` + `UserRegister.name` + `UserResponse.name` + `ProfileUpdate`
- [x] 4. `src/api/routes/auth_helpers/registration.py` — set `name` on insert
- [x] 5. `src/api/routes/auth.py` — `PUT /auth/profile`
- [x] Verify: 16/16 validator+schema assertions pass; alembic single head 071→070
- [x] Codex review: caught **P1** (stale `schema/openapi.json` — CI gate) → regenerated (commit 4d14d86, pure additions, 0 drift) → re-review CLEAN
- Commits: 7e98a80 (feature) + 4d14d86 (schema). NOT pushed, no PR yet.

## Phase 2 — Frontend (`bridgeleads-web`, worktree `feat/user-display-name-fe`, DONE)
- [x] register page: optional Name field → sent in body
- [x] Settings → Account: editable Name + `updateProfile()` in `lib/api.ts`
- [x] `DashboardHeader.tsx`: greet with `me.name`, else drop the name (never email stub); removed dead `emailFirstPart` + `userEmail`
- [x] `User` type gains `name`; `page.tsx` passes `me?.name`
- [x] tsc --noEmit CLEAN + eslint CLEAN (exit 0)
- [x] Codex review FE diff — DONE (via bounded `codex exec` on the diff). Found P2×2 (useEffect reseed clobbers typing; untrimmed dirty-check) + P3 (seed-once survives account switch). ALL FIXED + re-review confirmed resolved.
- [ ] regen `api-types.generated.ts` via `gen:api-types` AFTER backend merges to main (pulls schema from GitHub main)
- Commits: 0d14ebb (feat) + cb4b445 (P2 fixes) + 872c773 (P3 fix). NOT pushed, no PR yet.

## Review

**Root cause:** No name stored anywhere — greeting fell back to the email
local-part (`emailFirstPart(userEmail)` → "mikitsegaye29").

**Built (two isolated worktrees, no collision with concurrent sessions):**
- Backend `feat/user-display-name`: encrypted nullable `users.name` (mig 071),
  shared hardened validator, `UserRegister.name`/`UserResponse.name`/
  `ProfileUpdate`, `PUT /auth/profile`, regenerated openapi schema.
- Frontend `feat/user-display-name-fe`: optional name at signup, editable in
  Settings→Account, greeting uses the name or no identifier (never the stub).

**Codex:** consulted on design (encrypt column, PUT /auth/profile, harden
validation against bidi/zero-width/control chars). Backend review caught a P1
(stale openapi schema) → fixed → re-review clean. FE review deferred (rate limit).

**Verification:** backend 16/16 validator+schema asserts pass, alembic head
linear; FE tsc + eslint clean.

**Deploy order (when shipping):** backend first (migration 071 + API on
Railway) → then `gen:api-types` + FE on Vercel. Greeting degrades gracefully if
FE ships first (name reads `undefined` → no identifier).

**Not done:** push / PRs (awaiting your go); FE Codex pass (rate-limited);
api-types regen (needs backend on main).
