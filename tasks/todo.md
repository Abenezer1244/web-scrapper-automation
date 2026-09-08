# Plan-limit notice + em-dash sweep — DONE, shipped, deployed

## 1. The plan-limit notice (closed)

**Root cause:** there was no bespoke alert component. `lib/errors.ts::toastError()` routes
**every** HTTP 402 to `toastUpgrade()`, which called `toast.error`; under the app's
`richColors` + `closeButton` Toaster that supplies the pale-red card, red text, red close
button and near-black action button. Picking the error channel for a plan gate was the whole
visual bug. The message came from `src/api/entitlements.py`.

- [x] Backend: structured 402 `{code, title, message}`; `Violation` dataclass; plural-correct,
      Title-Cased, unquoted plan names.
- [x] Frontend: `components/plan-limit-notice.tsx`, tolerant `readErrorBody`, `toastUpgrade`
      renders the notice.
- [x] Verified in Chromium at 320/375/390/430/768/1024/1440, both themes, `pointer: coarse`.
- [x] Contrast AA, keyboard, focus ring, live region, every dismissal path.
- [x] Shipped: **FE #117 `8dcc70b`**, **BE #252 `019a8c1`**. Frontend deployed first, on
      purpose: an old FE build reads `detail` as a string and would render `[object Object]`.
- [x] Confirmed live in the production bundle (via `/register`, a public route that imports
      the same module), including the toast-id race fix.

**Copy deviation, deliberate:** asked for "This scraper includes 2 counties", shipped "This
would put your account at 2 counties". `projected` is the account-wide distinct-county total,
so the requested wording is false whenever an already-saved scraper causes the overage, and
false for every batch create.

## 2. The em-dash sweep (closed)

- [x] 90 frontend rewrites across 43 files + the customer-facing backend strings.
- [x] Re-runnable checkers: `scripts/find-user-facing-dashes.mjs` (FE, TypeScript parser) and
      an AST walk on the backend that drops docstrings.
- [x] Shipped: **FE #118 `c0d8976`**, **BE #255 `9ef443f`**, **FE #119 `75f34b1`**.
- [x] Regenerated `schema/openapi.json` and `lib/api-types.generated.ts` (a reworded
      `Query(description=)` trips both gates).
- [x] **Production audit across 7 pages: 0 prose em dashes served.**

## Deliberately NOT changed

- The generated `party_name` in the two code-violation scrapers. It is customer-visible, and
  it is an input to `_compute_dedup_hash`, which keys billing dedup. Rewriting it would
  re-deliver and **re-bill** already-paid leads.
- en dashes in numeric ranges, the `—` empty-value glyph in table cells, the `·` separator.
- `scrapers/reliability.py`'s exception format: internal, verifiably never customer-visible.
- Plan limits, billing/Stripe, quota enforcement, county counting, scraper behaviour.

## Open at hand-off

- **FE #120** (one-line copy fix from the final read-back) is open, CI pending.
- **Codex round 2 on the sweep never completed** (OpenAI usage limit). Its questions were
  answered by hand; worth re-running after the quota resets.
