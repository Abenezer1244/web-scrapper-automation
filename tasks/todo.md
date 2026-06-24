# First + last name (required) + hard profile-complete gate

Redesign of the just-shipped single optional `name`: now REQUIRED first_name +
last_name, with a hard gate forcing legacy null-name users to set them on next login.

## Codex consult (reconciled)
- Keep `name` column (don't drop — rollback window), add first_name/last_name nullable-at-DB.
- Server-side App Router gate in (dashboard)/layout.tsx (robust, no flash).
- Server-owned `profile_complete` boolean on /auth/me.
- Required = blank-after-sanitize -> 422; /auth/profile callable while incomplete.
- Scope call: server-side layout gate (cosmetic field), NOT backend route enforcement.

## Phase 1 — Backend (DONE)
- [x] mig 073: add first_name + last_name (Text, nullable); keep name
- [x] model: first_name/last_name columns
- [x] schemas: _validate_required_name; UserRegister first+last required;
      UserResponse first/last/profile_complete (name removed); ProfileUpdate first+last required
- [x] registration: set first/last
- [x] /auth/profile sets first/last; /auth/me returns profile_complete
- [x] regenerate openapi; verify (all assertions pass, single head 073)
- [ ] Codex review

## Phase 2 — Frontend
- [ ] (dashboard)/layout server-side gate -> CompleteProfile interstitial when !profile_complete
- [ ] register: first+last required (replace optional name)
- [ ] Settings>Account: first+last
- [ ] DashboardHeader: greet first_name
- [ ] types + api (first/last/profile_complete, updateProfile(first,last)); regen api-types
- [ ] tsc+eslint; Codex review
