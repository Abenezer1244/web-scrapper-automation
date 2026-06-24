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

## Phase 1 — Backend (worktree `feat/user-display-name`, max 5 files)
- [ ] 1. `alembic/versions/071_user_display_name.py` — add `users.name` (Text, nullable)
- [ ] 2. `src/db/models.py` — `name = Column(EncryptedString, nullable=True)`
- [ ] 3. `src/api/schemas.py` — `_validate_display_name` + `UserRegister.name` + `UserResponse.name` + `ProfileUpdate`
- [ ] 4. `src/api/routes/auth_helpers/registration.py` — set `name` on insert
- [ ] 5. `src/api/routes/auth.py` — `PUT /auth/profile`
- [ ] Verify: import schemas, alembic head linear, unit-test the validator
- [ ] Codex `codex review` the diff (gate) — fix any P1/P2

## Phase 2 — Frontend (`bridgeleads-web`, separate worktree, max 5 files)
- [ ] register page: optional Name field → send in body
- [ ] Settings → Account: editable Name + `updateProfile()` in `lib/api.ts`
- [ ] `DashboardHeader.tsx`: greet with `me.name`, else drop the name (never email stub)
- [ ] regen api-types after backend merges; tsc + eslint clean
- [ ] Codex review FE diff

## Review
_(filled at end)_
