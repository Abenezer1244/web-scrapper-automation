# Plan-limit notice redesign (fix/plan-limit-notice) — DONE

## Root cause

There was no bespoke alert component. `lib/errors.ts::toastError()` routes **every** HTTP 402
to `toastUpgrade()`, which called `toast.error`. Under the app's `richColors` + `closeButton`
Toaster (`components/providers.tsx`) that paints the pale-red card, the red body text and the
red close button, and sonner's default action button supplied the near-black "Upgrade".
Picking the error channel for a plan gate was the whole visual bug. The message itself came
from `src/api/entitlements.py`.

## Done

- [x] Backend: structured 402 `{code, title, message}`; `Violation` dataclass; plural-correct,
      Title-Cased, unquoted, no em dash.
- [x] Backend: `plan_label` reads `PLAN_CATALOG`; one shared `RECORD_TYPE_LABELS`.
- [x] Frontend: `components/plan-limit-notice.tsx`; tolerant `readErrorBody`; `toastUpgrade`
      renders the notice.
- [x] Verified in Chromium at 320/375/390/430/768/1024/1440, light + dark, `pointer: coarse`
      on the phone widths.
- [x] Contrast, keyboard, focus, live-region, dismissal-path checks.
- [x] 28 new backend tests; 293-test targeted suite green; `tsc` and `eslint` clean.
- [x] Two Codex rounds. r1: 1 P2 + 2 P3. r2: 1 P2 + 1 P3. No P1 in either. All five fixed.
- [x] Zero em dashes in every user-facing string this change touched.

## Copy: one deliberate deviation from the request

Asked for: `Your Starter plan includes 1 county. This scraper includes 2 counties.`
Shipped:   `Your Starter plan includes 1 county. This would put your account at 2 counties.`

`projected` is the account-wide distinct (state, county) total unioned with the request, so
"this scraper includes 2 counties" is false whenever an already-saved scraper contributes to
the overage, and false for every batch create, which calls the same helper. Everything else in
the requested copy is exactly as asked. Say the word and it becomes literal.

## NOT changed

Plan limits, billing/Stripe, quota enforcement, county counting, subscription behaviour,
scraper functionality. `ENTITLEMENT_ENFORCEMENT` still defaults off.

## Handoff

- Both branches are committed and **unpushed**; no PRs opened.
- **Ship the frontend first.** An old FE build reads `detail` as a string and would render
  `[object Object]` against the new backend. `schema/openapi.json` declares no 402 responses,
  so CI cannot catch this.
- Em dashes survive in user-facing strings this change did not touch (`routes/batches.py` 422
  copy, `quota.py` frozen-account copy). Worth a separate sweep.
