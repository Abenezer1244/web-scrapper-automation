# Task: Migrate billing to the new $199 pricing strategy (Phase 1: prices)

> Decision (user, 2026-06-21): **Go with the new pricing.**
> Scope = **Prices first, entitlements next** (value-metric county/record-type gating is a SEPARATE later build).
> PR #39 = **Split**: ship the app redesign now, HOLD the $199 marketing pages until billing matches.
> Source of truth: `docs/pricing-strategy-2026-06.md`.

## Verified ground truth (2026-06-21)
- LIVE `/billing/plans`: Starter $0 / Pro **$79** / Business **$149** / Agency **$499** (records_limit 50/1000/5000/-1).
- Target: Pro **$199** / Business **$499** / Agency **$1,499** + annual ~20% off; skip-trace metered $0.08.
- 🔴 **Stripe misconfig found:** Railway `STRIPE_PRICE_{PRO,BUSINESS,AGENCY}` hold **`prod_…` (product) IDs**, and `STRIPE_PRODUCT_*` are **unset**. Checkout feeds `STRIPE_PRICE_*` straight into `line_items[].price`, which Stripe requires to be `price_…`. ⇒ **live checkout is currently broken** (consistent with 0 paying customers). Skip-trace price vars ARE correct (`price_…`).
- 0 paying customers ⇒ no grandfathering needed (confirm no active paid subs on the 4 billing accounts).
- PR #39 commits (frontend `redesign/monopo-marketing`): marketing redesign = base commit `1a9da04`; app redesign (theme/auth/dashboard/scrapers/settings) stacked on top.

## DECISIONS (user, 2026-06-21)
- Annual = **wire it now** (6 Stripe prices: monthly+annual ×3).
- Founding coupon = **reduce to 25%** (Pro ~$149.25 / Business ~$374.25 / Agency ~$1,124.25 effective).
- Stripe objects = **Claude creates them via live API**, captures IDs into a committed doc, sets Railway env, user verifies in dashboard.

## Phase 1 — Backend code (web-scrapper-automation) [2 files: settings.py + billing.py]
- [ ] `settings.py`: add `STRIPE_PRICE_{PRO,BUSINESS,AGENCY}_ANNUAL: str = ""`.
- [ ] `billing.py` `_PLANS`: Pro 79→199 (annual 758→**1910**), Business 149→499 (annual 1430→**4790**), Agency 499→1499 (annual 4790→**14390**). Add `stripe_price_id_annual` per paid plan.
- [ ] Feature bullets: keep honest (drop the unenforced "5 counties" cap on Pro; keep records/skip-trace/exports/schedule/delivery/API). NO new county/record-type claims (Codex HIGH).
- [ ] `_PRICE_TO_PLAN`: include BOTH monthly + annual price IDs → same (plan, records_limit). Keep records_limit unchanged.
- [ ] `/checkout`: guard that resolved id starts with `price_` → clean 400 (defensive, no boot crash).
- [ ] Startup: log-WARN (not raise) if any `STRIPE_PRICE_*` lacks `price_` prefix (avoid crash-loop landmine).
- [ ] Verify: `pytest` (billing tests), security Master Review, **Codex review of diff**.

## Phase 2 — Live Stripe + env (Claude, with per-object confirmation) [config only]
- [ ] Create 6 new Stripe **Price** objects (monthly+annual ×3) under existing products → `price_…` IDs.
- [ ] Create new **25% founding coupon** → update `_FOUNDING_COUPON_ID` + cache key + display (`percent_off: 25`, `code: "FOUNDING25"`); archive/retire old 40% coupon `8mX1xa35`.
- [ ] Document all IDs/amounts/interval in a committed `docs/stripe-prices-2026-06.md`.
- [ ] Set Railway `STRIPE_PRICE_{PRO,BUSINESS,AGENCY}` + `_ANNUAL` on **api AND worker** with real `price_…` IDs (fixes prod_/price_ bug). Optionally set `STRIPE_PRODUCT_*` too.
- [ ] Deploy/restart both services; smoke test `/billing/plans`, monthly + annual checkout, a webhook event.

## Phase 3 — Frontend split (bridgeleads-web) [git surgery]
- [ ] New branch from `master`; include app-redesign commits, EXCLUDE marketing commit `1a9da04` (verify no later commit edits `app/(marketing)` shared files e.g. globals.css).
- [ ] Open replacement PR (redesign only). Hold marketing for a later PR once entitlements/marketing copy are ready.
- [ ] Verify: `npx tsc --noEmit` + `npm run lint` + `npm run build`.

## Open questions for Codex
1. Should Phase 1 `_PLANS` feature bullets adopt the new-strategy text (3 counties / 250 skip-traces) even though NOT enforced yet, or stay accurate to the still-volume-based enforcement?
2. Best/safest way to split marketing out of PR #39 (cherry-pick onto master vs revert base commit) given marketing is the base commit.
3. Anything that breaks when only PRICES change but entitlement enforcement stays volume-based (webhook `_PRICE_TO_PLAN`, records_limit on upgrade, founding coupon math)?
4. Should we fix the prod_/price_ Stripe bug as part of this, and is creating live Price objects via API acceptable vs dashboard?

## Codex consult notes (2026-06-21, consult mode)
Codex agreed the plan is coherent IF the offer is framed around enforceable limits. Key findings reconciled:
- **HIGH — feature text:** do NOT advertise "3 counties / 250 skip-traces / record types" in `/plans` unless enforced or labeled "coming soon". Keep bullets to what's enforced (records_limit, skip-trace metered $0.08, existing features). → **Decision: keep honest bullets; defer county/record-type marketing to the held marketing PR + Phase-2 entitlement build.**
- **HIGH — annual:** `/plans` shows annual but `/checkout` only uses monthly env → annual is unbuyable. Either wire annual honestly or drop the display. Minimal fix: add `STRIPE_PRICE_*_ANNUAL`, include annual price IDs in `_PLANS` + `_PRICE_TO_PLAN`; `/checkout` already validates any price_id, so frontend just sends the chosen one. → **needs user decision.**
- **HIGH/MED — webhook mapping:** swapping `STRIPE_PRICE_*` means old price IDs won't map in the webhook. With 0 paying customers + broken checkout (prod_ ids) there are no valid in-flight sessions, so low risk; cheap insurance = keep old IDs mappable during the window / expire stale Checkout Sessions.
- **MED — founding coupon:** FOUNDING40 (40% off) at new prices = Pro **$119.40** / Business **$299.40** / Agency **$899.40**. Confirm intent or retire/adjust before cutover. → **needs user decision.**
- **MED — startup validation:** add a guard that every `STRIPE_PRICE_*` starts with `price_` (this misconfig is how `prod_` got in). → **adopt.**
- **Cutover order (adopt):** create NEW Price objects (Stripe prices are immutable; archive old later) → separate `_MONTHLY`/`_ANNUAL` env vars → deploy backend w/ startup validation + both-ID mapping → set env on BOTH api+worker same window → restart → smoke test plans/checkout/webhook.
- **API vs dashboard:** dashboard preferred for founder traceability on a one-time business change; document product IDs, price IDs, amounts, currency, interval, coupon behavior either way. → **needs user decision.**

## Decisions resolved by Codex (no user input needed)
- Feature bullets stay accurate to enforcement; no unenforced entitlement claims.
- Add `price_`-prefix startup validation for STRIPE_PRICE_*.
- Keep existing records_limit values (volume metric unchanged this phase).
- Create new Stripe Price objects (never edit), archive old.

## Progress (2026-06-21)
- ✅ Phase 1 code (commit `9764652`): _PLANS $199/$499/$1499 + annual, annual env vars, _PRICE_TO_PLAN monthly+annual, /checkout price_ guard (503), import-time config WARN. Compiles; no test asserts on prices.
- ✅ Codex review of diff = **GATE PASS** (no P1/P2/P3).
- ✅ Phase 2 live Stripe (commit `bfb25fd`): created 6 Price ids + FOUNDING25 (25%) via `scripts/stripe_pricing_migration_2026_06.py`; deleted old 40% coupon `8mX1xa35` (0 redemptions); ids in `docs/stripe-prices-2026-06.md`; founding code 40%→25%.
- ✅ **PR #90 OPEN** (branch `feat/pricing-199-migration`, worktree `../bridgeleads-pricing`).
- ⏳ POST-MERGE: set 6 `STRIPE_PRICE_*`(+`_ANNUAL`) env on api AND worker → smoke test plans/checkout/webhook.
- ⏳ Phase 3 frontend split (bridgeleads-web): ship app redesign, hold $199 marketing.
- ⏳ Dashboard BillingTab annual toggle (frontend) so annual is actually selectable.

## Review — SHIPPED + VERIFIED 2026-06-21
**Backend (PR #90 MERGED → main, Railway deployed):**
- /billing/plans live: Pro $199/$1910, Business $499/$4790, Agency $1499/$14390; FOUNDING25 25% active (25 spots).
- Checkout E2E verified in prod: monthly AND annual both create live `cs_live_…` sessions; invalid/`prod_` ids 400 cleanly.
- Fixed a latent prod bug: `STRIPE_PRICE_*` held `prod_` ids → checkout was BROKEN; now real `price_` ids on api+worker.
- 6 live Stripe prices + FOUNDING25 created via `scripts/stripe_pricing_migration_2026_06.py`; old 40% coupon deleted (0 redemptions). IDs in `docs/stripe-prices-2026-06.md`.

**Frontend (PR #39 + #40 MERGED → master, Vercel deployed):**
- App redesign + $199 marketing live on bridgeleads.io.
- Marketing copy made HONEST: removed unenforced county-count caps, record-type gating, overlap-gated-at-Business; skip-trace numbers match backend (Pro pay-per-use, Business 1000, Agency 2000); FOUNDING40→FOUNDING25.
- Codex review caught a P1 (Starter "Sample" type-gate) + P2 (Business-only overlap) → both fixed before merge.
- Hotfix PR #40: two hardcoded `FOUNDING40` banners → FOUNDING25. Prod verified clean.

**Process:** Codex consulted before build + reviewed every diff (2 backend, 2 frontend). tsc/lint/build all green. Isolated worktrees used.

## ⏭️ Follow-ups (NOT done — deliberate)
- **Value-metric entitlement enforcement** (per-tier county allowlist + record-type gating + skip-trace bundle for Pro): the strategy's #1 lever, deferred. Until built, copy stays volume-honest.
- **Dashboard BillingTab annual toggle**: annual is buyable via API but the dashboard UI only sends monthly; add a monthly/annual switch to expose annual prepay.
- **Backend `/pricing` comparison matrix** (billing.py) still has old county/volume framing — dormant (marketing uses static data.ts), align if ever consumed.
- **WTP validation**: still 0 paying customers; the $199 matrix remains a hypothesis — validate via founding calls.
