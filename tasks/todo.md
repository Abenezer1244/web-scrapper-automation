# Plan-limit notice redesign (fix/plan-limit-notice)

## Root cause of the screenshot

- **Message text**: `src/api/entitlements.py` builds a prose string and raises
  `HTTPException(402, detail="Plan limit reached — <prose>. Upgrade your plan to continue.")`
  from `enforce_entitlements()` (create path) and `enforce_runnable_http()` (run path).
- **Rendering**: `bridgeleads-web` `lib/errors.ts::toastError()` routes **every** HTTP 402
  to `toastUpgrade()`, which calls `toast.error(msg, { action: { label: "Upgrade", ... } })`.
  The Toaster in `components/providers.tsx` is `richColors` + `closeButton`, so sonner
  paints the pale-red error surface, the red text, the red circular close button and the
  near-black default action button. Nothing about the visual is bespoke.
- It is therefore NOT a bespoke alert component; it is the shared sonner error toast, and
  it is shared by every 402 in the app, including `quota_block_reason()`'s **failed payment**
  message. So the redesign must not hard-code "Plan limit reached" as a title.

## Plan

### Phase 1 — backend: structured, human 402 payload (2 files)
- [ ] `src/config/constants.py`: add canonical `PLAN_LABELS` / `plan_label()` and
      `RECORD_TYPE_LABELS` / `record_type_label()` (single source of truth; two divergent
      copies of the record-type label map already exist).
- [ ] `src/api/entitlements.py`: emit `detail` as a dict
      `{code, title, message, upgradeable}` instead of a prose string.
      Copy: `County limit reached` / `Your Starter plan includes 1 county. This scraper
      includes 2 counties. Upgrade your plan to continue.` Correct singular/plural,
      Title-Cased plan name, no quotes, **no em dash**.
      Accuracy note: `projected` is the distinct-county total ACROSS the account, not
      necessarily "this scraper". Copy branches so it is true in both cases.
- [ ] Point `segments.py` / `batch_export.py` at the canonical label map.

### Phase 2 — frontend: calm plan notice (3 files)
- [ ] `lib/api.ts`: when `body.detail` is an object, use `detail.message` for
      `error.message` and attach the structured payload to the thrown Error
      (today an object detail would render as `[object Object]`).
- [ ] `components/plan-limit-toast.tsx`: new branded notice. Neutral card surface,
      subtle border, teal primary CTA (`Button` default variant), title + supporting
      text, aligned icon, real close button with an accessible label.
- [ ] `lib/errors.ts`: `toastUpgrade` renders it via `toast.custom` (role=status /
      aria-live=polite, not an assertive error) and keeps the existing
      `/settings?tab=billing` route.

### Phase 3 — verification
- [ ] `npx tsc --noEmit` + `npx eslint . --quiet` (FE), `ruff` + `pytest` subset (BE)
- [ ] Playwright: render the notice at 320 / 375 / 390 / 430 / 768 / 1024 px
- [ ] Keyboard + focus + contrast check
- [ ] Zero em dashes in every user-facing string touched
- [ ] Codex review of the diff

## Explicitly NOT changed
plan limits, billing/Stripe logic, quota enforcement, county counting, subscription
behaviour, scraper functionality.
