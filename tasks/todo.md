# Plan entitlement audit (2026-09-08)

Branch: `chore/entitlement-audit` (BE) / `chore/entitlement-audit-fe` (FE)
Worktrees: `C:/Users/Windows/bridgeleads-worktrees/entitlement-audit`,
`C:/Users/Windows/bridgeleads-web-worktrees/entitlement-audit-fe`

## Phase 1 - locate authoritative config
- [x] `src/config/plans.py` PLAN_CATALOG (price, records_limit, marketed features) - source of the 4 in-app cards
- [x] `src/config/settings.py` PLAN_LIMITS (records) - DUPLICATE of catalog, currently agrees
- [x] `src/config/settings.py` SKIP_TRACE_BUNDLED_QUOTAS, ENTITLEMENT_ENFORCEMENT, STRIPE_PRICE_*
- [x] `src/config/constants.py` COUNTY_LIMIT_BY_PLAN, RECORD_TYPES_BY_PLAN, BATCH_PLANS,
      BUSINESS_FEATURES_PLANS, SKIP_TRACE_ADDON_PLANS, PRIORITY_QUEUE_PLANS, SUPPORTED_EXPORT_FORMATS
- [x] `src/api/entitlements.py` - county + record-type validator (flag-gated)
- [x] FE `lib/entitlements.ts` - mirror; FE `app/(marketing)/_monopo/data.ts` - SEPARATE static marketing copy
- [x] `src/api/routes/billing.py` `pricing_page().comparison` - THIRD copy of the matrix

## Phase 2-19 verification
- [x] Records limits (quota.py + worker reservation)
- [x] County limits (entitlements.py, both create paths + 4 run-time call sites)
- [x] Record types (RECORD_TYPES_BY_PLAN + call sites)
- [x] Skip tracing (bundled quotas, counter, meter events, overage price wiring)
- [x] Exports (SUPPORTED_EXPORT_FORMATS, DeliverConfig)
- [x] Schedules (ScraperFrequency, DeliverConfig/ScheduleConfig)
- [x] Delivery (email / webhook / dialer)
- [x] Batch (BATCH_PLANS, BATCH_MAX_COMBINATIONS)
- [x] API access (require_plan + api-key auth path)
- [x] Overlap / intersection (/segments/*, batch delivery_mode)
- [x] Priority queue (celery task_queues + start.sh WORKER_QUEUES)
- [x] White-label / account manager / seats / data freshness
- [x] Stripe mapping (_PRICE_TO_PLAN, checkout line_items)
- [ ] Run targeted pytest subset against local test DB
- [ ] Live UI verification (Playwright/Chromium) of the served pricing surfaces
- [ ] Codex independent review + independent verification of its findings

## Phase 20 - fixes
- [ ] Await owner decisions on the policy items (see report section 21)

## Review
(to be filled in)
