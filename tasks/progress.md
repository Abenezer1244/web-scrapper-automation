# BridgeLeads — Session Progress (2026-04-11)

> Comprehensive session dump saved before `/clear`. Resume from this file
> and the git log. Everything here is live on production unless marked
> otherwise.

## TL;DR for the next session

**Production is healthy.** All review batches shipped, 20 WA counties
visible in the user-facing picker, 12 of them fully verified this
session. Next work is fixing the 8 "degraded" visible counties and the
12 "down" hidden counties. Full WA matrix below.

Latest commits:
- Backend `67b66cd` — AcclaimWeb header-aware fallback (live on Railway)
- Frontend `b3b7b66` — defensive rendering (live on Vercel
  `bridgeleads-f8tzkmtod-abenezer1244s-projects.vercel.app`)

**Two critical regressions were found + reverted this session**:
1. L5 `ssl.CERT_NONE` passed directly to redis-py broke `/auth/register`
   + `/auth/login` — **reverted** in `4dcdca8`
2. M9 `split_name` regression was routing WA personal names to advanced
   trace — **fixed** in `3a06329` plus found and plugged a separate
   code-violation credit leak worth ~30% of skip-trace spend

## What shipped this session

### Full-SaaS code review — 6 batches (all CRITICAL/HIGH/MEDIUM + NIT closed)

Review started with 7 CRITICAL, 13 HIGH, 12 MEDIUM, 10 LOW findings.
Every one either shipped, verified not-a-bug, or deliberately deferred
with documented rationale.

| Batch | Commit | Findings | Notes |
|---|---|---|---|
| 1 | `69b2355` + frontend `7fc878d` | CRIT-1 savepoint, CRIT-2 copy fix, C4 atomic Stripe dedup, C5 webhook rate limit | Plan upgrade now survives webhook replay |
| 2 | `1e295ee` | C1 RLS on 9 tables + NULLIF, C2 advisory + integration test, H1 sync session helpers, H10 user_id scope | Multi-tenant isolation verified via `tests/test_rls_isolation.py` |
| 3 | `098dea8` | C3 Stripe customer race, H11 commit order, H12 stable meter identifier, M8 Celery ingest | Skip-trace billing is now safe against webhook replay |
| 4 | `7050479` | H5 daily reset, H8 zombie watchdog, H13 Playwright cleanup + `worker_max_tasks_per_child=25` | |
| 5 | `106111f` | C6 SSE atomic counter, C7 JobLog join, H2 dedup sanity, H3 log preserve, H4 DISTINCT join, H6 aud/iss/jti, H7 hash index, M2/M5/M6/M7/M9/M10/M11/M12 | H7 went from O(N) to indexed lookup |
| 6 | `77aa2d1` | L1, L4, L5 (reverted separately), L6, L7, L9, L10 | **L9 was a real production bug** — parcel extraction try/except was dedented out of the `for inst_num` loop, processing only 1/N instruments per page |

### Migrations applied to prod

| Rev | Title | Effect |
|---|---|---|
| 016 | cross-job lead dedup | `delivered_records` table, `is_duplicate`, `dedup_hash` |
| 017 | referral program | `users.referral_code`, `referred_by_user_id`, `referral_credit_cents`, `referral_events` |
| 018 | RLS on sprint tables | RLS enabled on delivered_records / pending_skip_trace_rows / skip_trace_queues / password_history / referral_events + NULLIF-safe policies on all 9 tables |
| 019 | unique stripe_customer_id | Partial unique index |
| 020 | records_period_start | Daily-catchup monthly reset |

**DB state**: `alembic current = 020 (head)`. Postgres role = `postgres` with `BYPASSRLS=true` — RLS policies are decorative in production until role is downgraded. Startup advisory logs this at ERROR level so ops sees it every boot.

### Sprint 7.3 — Referral program (shipped)

- $20 account credit per paid referral
- 8-char unambiguous referral code (I/L/0/O-free alphabet)
- `UserRegister.ref` field + URL `?ref=CODE` capture on frontend register page with sessionStorage fallback
- Savepoint around `_grant_referral_credit` insert so webhook replay doesn't nuke plan upgrade
- Settings → Referrals tab: shareable link, copy button, signup count, conversions, credit earned
- **Note**: credit is tracked but NOT auto-applied to Stripe `customer.balance` yet. Copy says "contact support to redeem" — truthful.

### County expansion

**Pierce code_violation** shipped end-to-end: 442 records via live API E2E, 100% parcels + addresses via Tacoma ArcGIS FeatureServer.

**King code_violation** shipped end-to-end: 500 records via live API E2E (capped at plan limit), 100% addresses via Seattle SDCI Socrata. Parcel enrichment via GIS is 0/500 because Seattle's dataset doesn't expose parcelnumber — documented limitation.

**WA court records (eviction, divorce)** blocked at source — LINX requires per-name search, Odyssey requires sign-in. Documented in `docs/compliance/wa-court-records.md`. Decision: defer until JIS Link vendor contract or partial supply via recorder Decree of Dissolution.

**Clark tax_delinquent** blocked by RCW 42.56.070(8) (commercial-list restriction). Documented in `docs/compliance/wa-tax-delinquent.md`. King remains only supported WA county for tax_delinquent.

### Sprint 6.3 — connector health audit + canary fixes

- **Canary window** widened from 1 day -> 7 days (Batch 2 Phase 2)
- **Canary is now sticky** (commit `a850e98`) — refuses to downgrade `healthy -> degraded` on an empty probe, only real exceptions can downgrade
- **SSRF allowlist loader** fixed (Batch 2 Phase 3) — startup hook loads every active connector's base_url hostname into `_ALLOWED_SCRAPE_DOMAINS`, validates each through `validate_scraping_target()` first
- **`GET /scrapers/connectors` filter** now returns `healthy OR degraded` by default (commit `a850e98`). Only excludes `down` + `unknown`. Admin can pass `?include_all=true`. This is what unlocked the jump from 11 -> 20 counties visible.
- **AcclaimWeb template** added header-aware plain-HTML-table fallback (commit `67b66cd`) — unlocks non-Kendo AcclaimWeb deployments

### Bug fixes from the full review + related

- **Whatcom pagination fix** (commit `b708ac7`) — was bailing when `new_count == 0` on filtered rows; now bails only when `new_raw_count == 0`. Verified: 7 probate records on 30-day window (Transfer on Death, Lack of Probate, Death Certificate).
- **`/scrapers` + `/deliver` crash fix** (commits `b8d8fed` backend + `03fd25c` frontend) — 3 legacy configs had empty `deliver = {}` which crashed both pages. Fixed with backend `model_post_init` backfill + frontend `?? []` fallbacks + one-shot DB cleanup.
- **`config.schedule.frequency` crash** (commit `b3b7b66`) — same class as above, found during defensive-rendering sweep. Now `config.schedule?.frequency ?? "manual"`.
- **Vercel register page prerender** (commit `4a8f8ee`) — Sprint 7.3 edit to `useSearchParams` broke SSG prerender. Fixed by wrapping `RegisterPageInner` in `<Suspense>`.
- **Skip-trace cost audit** (commit `3a06329`) — THREE fixes in one: M9 LAST FIRST heuristic restored for 2-token WA names, code-violation case descriptions rejected from skip-trace entirely, EST/EXEC/ADMIN tokens added to entity classifier. Expected ~30-40% reduction in Tracerfy cost per batch.

### New test

`tests/test_rls_isolation.py` — 2 passing integration tests that create a NOBYPASSRLS role and verify user A cannot read user B's `results` or `delivered_records`. These are the first real proof that RLS policies work as intended.

## Current production state (as of session end)

### Live endpoints
- API: `https://api.bridgeleads.io` — healthy, returning 201 on register, 200 on connectors
- Frontend: `bridgeleads-f8tzkmtod-abenezer1244s-projects.vercel.app` on Vercel, auto-deployed via manual `vercel --prod`
- DB: Supabase Postgres, `alembic current = 020 (head)`
- Redis: Upstash, `ssl_cert_reqs="none"` (string, NOT the integer — this is correct per redis-py API)

### `/scrapers/connectors` live result
- 20 unique counties visible to users
- 23 connector rows total (Pierce + King have multiple rows for different record-type families)

### WA county matrix — complete breakdown

**WORKING (12) — verified producing records this session:**
```
benton     clark      douglas    island
jefferson  king       kitsap     lewis
pierce     spokane    thurston   whatcom
```

Record-type coverage on the top counties:
- Clark:   probate, pre_foreclosure, divorce, tax_delinquent (4)
- King:    probate, pre_foreclosure, death_certificate, tax_delinquent, code_violation (5)
- Pierce:  probate, pre_foreclosure, divorce, code_violation (4)
- All others: probate + pre_foreclosure (2)

Full type count: King 5, Clark 4, Pierce 4, every other WA county 2.

**NEEDS WORK — visible but degraded (8):**

| County | Platform | Known issue |
|---|---|---|
| chelan | AcclaimWeb | Portal uses SINGLE-DATE picker, not range. Needs per-instance day-by-day iteration. Portal HAS data (saw "11 of 59" in one probe). |
| clallam | EagleWeb | Returns 0 on 30/60/90 day probes. Unclear if selector mismatch or truly empty. |
| grant | Tyler SelfService | Returns 0 on 60-day. Template mismatch. |
| mason | EagleWeb | "Could not find date inputs" — Mason's DOM differs from template. Per-instance selector fix needed. |
| okanogan | Tyler SelfService | Not directly probed this session |
| pacific | CountyGovernmentRecords | Not directly probed this session |
| pend oreille | AcclaimWeb-variant | TargetClosedError on probe |
| whitman | CountyGovernmentRecords | Not directly probed this session |

**NEEDS WORK — hidden, status=down (12):**

| County | Root cause |
|---|---|
| columbia | iDocMarket (paid portal) |
| cowlitz | Landing-page URL, needs real search URL |
| ferry | Non-standard portal path |
| garfield | NEMRC portal |
| grays harbor | `http://` legacy URL, fails HTTPS check |
| klickitat | Landing-page URL |
| lincoln | Tyler SelfService |
| san juan | Landing-page URL |
| skagit | Landing-page URL |
| skamania | Landing-page URL |
| stevens | Tyler SelfService |
| walla walla | Landing-page URL |

**DEACTIVATED / terminal:**
- snohomish — ToS blocker (`docs/compliance/`)
- yakima — AVA portal has no probate/pre_foreclosure doc types (`docs/compliance/`)
- adams, asotin, franklin, kittitas, wahkiakum — small rural eastern counties with registered-but-inactive rows; need working URLs

### Pending work from the user's last request

User asked to:
1. **Confirm the 12 working counties** — `scripts/probe_batch.py` was written and ready to run but the batch execution was interrupted. Script iterates all 12 counties with a 30-day window and writes one status line per county to `/tmp/batch1.txt`.
2. **Pick 2 new counties + do full 100% verification E2E** — not started. Best candidates to try:
   - **okanogan + whitman** — haven't been probed yet, might just work
   - OR **clallam + pacific** — EagleWeb + unknown-platform, different risk profiles

**Recommended resumption**: run `python scripts/probe_batch.py` first to confirm the 12 pass, then pick 2 from the NEEDS-WORK bucket and fire real jobs via the API (matching the pattern used for Pierce/King code_violation earlier this session).

## Files touched this session (backend)

### New files
- `alembic/versions/017_add_referral_program.py`
- `alembic/versions/018_rls_sprint_tables.py`
- `alembic/versions/019_unique_stripe_customer_id.py`
- `alembic/versions/020_add_records_period_start.py`
- `src/workers/tracerfy_ingest.py` (Celery task for Tracerfy CSV ingest, M8)
- `tests/test_rls_isolation.py` (RLS integration test)
- `scripts/e2e_random_county.py` (live E2E walkthrough)
- `scripts/probe_one_connector.py` (single-county scraper probe)
- `scripts/probe_batch.py` (multi-county batch probe, not yet run)
- `docs/compliance/wa-tax-delinquent.md`
- `docs/compliance/wa-court-records.md`
- `docs/compliance/connector-audit-2026-04-10.md`

### Modified
- `main.py` — lifespan loads SSRF allowlist from DB + RLS advisory check
- `src/db/session.py` — `rls_sync_session` / `system_sync_session` context managers + `check_rls_role_status`
- `src/db/models.py` — User referral columns, ReferralEvent, User.records_period_start, DeliveredRecord
- `src/api/schemas.py` — `UserRegister.ref`, `DeliverConfig.webhook_secret`, `ScraperConfigResponse.model_post_init`
- `src/api/auth.py` — API key hash index lookup (H7)
- `src/api/routes/auth.py` — register accepts ref, generates referral code, datetime imports at module level
- `src/api/routes/billing.py` — all Batch 3 fixes + referral grant + checkout Stripe customer race fix
- `src/api/routes/jobs.py` — SSE atomic counter, JobLog join, DISTINCT AI quota, download token aud/iss/jti
- `src/api/routes/scrapers.py` — connector filter `healthy OR degraded`, removed redundant commit
- `src/api/routes/webhooks.py` — rate limit, Celery dispatch for Tracerfy ingest, H10 user_id filter
- `src/api/billing/skip_trace_usage.py` — H11 commit order, H12 stable identifier
- `src/api/middleware/security.py` — SSRF domain loader with pre-validation, IPv6 fe80, COOP/CORP headers
- `src/api/middleware/rate_limit.py` — `webhook` zone
- `src/api/middleware/auth_hardening.py` — L10 log swallowed exceptions
- `src/workers/__init__.py` — `worker_max_tasks_per_child=25`, tracerfy_ingest route, worker_ready bootstrap
- `src/workers/scheduler.py` — 7-day canary window, sticky canary, zombie queued watchdog, daily reset task, M12 reset page counters
- `src/workers/tasks.py` — run_scrape_job uses rls_sync_session bootstrap, H2 dedup sanity, H3 fail_job log preservation, L9 indent fix
- `src/workers/skip_trace_dispatcher.py` — system_sync_session
- `src/config/settings.py` — redis_kwargs (reverted L5)
- `src/scrapers/base_scraper.py` — `__aexit__` defensive cleanup, redirect re-validation
- `src/scrapers/ai_scraper.py` — M10 thread-safe date injection
- `src/scrapers/enrichment/skip_trace.py` — `split_name` LAST FIRST restored, `looks_like_non_personal_party_name`, expanded entity tokens
- `src/scrapers/templates/acclaimweb.py` — header-aware table fallback
- `src/scrapers/whatcom_wa.py` — pagination fix (raw-card hash check)

## Files touched this session (frontend — `bridgeleads-web` master branch)

### New
- `app/(dashboard)/admin/funnel/page.tsx` (Sprint 5.5 activation funnel)

### Modified
- `app/(auth)/register/page.tsx` — Suspense wrapper, ?ref= capture
- `app/(dashboard)/layout.tsx` — Funnel nav link, TrendingUp icon
- `app/(dashboard)/scrapers/page.tsx` — defensive rendering for `deliver` + `schedule`
- `app/(dashboard)/scrapers/new/page.tsx` — listConnectors wrapper (TanStack)
- `app/(dashboard)/deliver/page.tsx` — defensive rendering for `deliver`
- `app/(dashboard)/admin/connectors/page.tsx` — `listConnectors({ includeAll: true })`
- `app/(dashboard)/settings/page.tsx` — Referrals tab, truthful copy
- `lib/api.ts` — listConnectors opts, getReferralStatus, getActivationFunnel

## Critical gotchas for the next session

1. **Vercel GitHub integration is broken** — manual `vercel --prod` required for frontend deploys
2. **Railway auto-deploys backend** on push to `main`, takes ~60-90s
3. **`ssl_cert_reqs` must be the STRING `"none"`** in `redis_kwargs`, NOT `ssl.CERT_NONE`. This is counter-intuitive but correct per redis-py's API. Don't "fix" it again.
4. **Postgres role has BYPASSRLS=true** — RLS policies are defense-in-depth, not enforced. Integration test `tests/test_rls_isolation.py` proves they WOULD work under a non-bypass role. Role downgrade is a future ops task.
5. **The canary is now sticky** — once a connector is `healthy`, only an exception downgrades it. Empty probes no longer flip the flag.
6. **Connector picker filter is `healthy OR degraded`** by default, not just `healthy`. Only `down` and `unknown` are hidden. `?include_all=true` for admin view.
7. **Skip-trace name parser is now WA-convention aware**: comma-free 2-token names assume `LAST FIRST` (WA recorder convention). 3+ token no-comma names are unsplittable and go to advanced trace. Entity detection catches LLC/INC/TRUST/BANK/EST/EXEC/ADMIN/PR. Code violation case descriptions are rejected from skip-trace entirely.
8. **Chelan portal uses a single-date picker**, not a date range. Every chunk currently types one day and gets "No Results". Fixing requires per-instance day-by-day iteration in the AcclaimWeb template OR detecting the picker variant and switching strategies.
9. **Mason's EagleWeb** has different DOM than the template expects — "Could not find date inputs to fill". Needs per-instance selector investigation.
10. **BYPASSRLS + system_sync_session** — worker paths use `system_sync_session()` (no RLS context) because batches like Tracerfy ingest legitimately cross tenants. The eventual role downgrade will need to refactor these paths to use `rls_sync_session(user_id)` per-user.

## Open questions for the user

1. Should we add a Tyler SelfService template to unlock Grant, Okanogan, Lincoln, Stevens (4 counties)?
2. Are the 5 small rural counties (Adams, Asotin, Franklin, Kittitas, Wahkiakum) worth pursuing, or deactivate them cleanly?
3. Is RCW 42.56.070(8) legal review still on the table? That's the gate for Pierce/Spokane/Clark tax_delinquent.
4. Skip-trace cost audit prediction: ~30-40% reduction in Tracerfy spend. Worth verifying against Stripe billing in the next 7 days.
5. The 20-county picker is live but 8 of them will return 0 records until their individual issues are fixed. Do we want to mark them as "experimental" in the UI or leave them as-is?

## Git state summary at session end

```
backend main:  67b66cd  AcclaimWeb: header-aware table fallback
frontend master: b3b7b66  Defensive rendering: guard config.schedule.frequency
alembic head:  020
```

19 backend commits + 16 frontend commits delivered this session. All pushed. All deployed (backend via Railway auto, frontend via manual Vercel).

## Session commit log (backend main)

```
67b66cd  AcclaimWeb: header-aware table fallback for non-Kendo deployments
a850e98  Make connector health sticky + surface degraded in picker
3a06329  Fix skip-trace M9 regression + plug code-violation credit leak
29e2d6e  Document WA court-records bulk-scrape blockers (Phase A.3/A.4)
b8d8fed  Fix /scrapers + /deliver crash on legacy configs with empty deliver
4dcdca8  Revert L5: ssl_cert_reqs must be string "none", not ssl.CERT_NONE
77aa2d1  Review Batch 6: LOW/NIT + L9 real bug fix
106111f  Review Batch 5: remaining CRIT/HIGH + MEDIUM findings
7050479  Review Batch 4: worker robustness (H5 + H8 + H13)
098dea8  Review Batch 3: billing integrity (C3 + H11 + H12 + M8)
1e295ee  Review Batch 2: multi-tenant isolation (C1 + C2 + H1 + H10)
69b2355  Review Batch 1: critical fixes from full-SaaS review
df6dab3  Sprint 7.3: referral program
b708ac7  Fix Whatcom scraper pagination — bail only when no new raw cards
8f196a7  Sprint 6.3 Phases 2-5: canary + SSRF + filter
c3c81bb  Sprint 6.3 Phase 1: WA connector health audit
1fd7353  Sprint 6.2: document Pierce tax delinquent legal blocker
a4462a3  Sprint 6.4: cross-job lead deduplication
7de4f96  Sprint 6.5: webhook delivery on job completion
```

## Session commit log (frontend master)

```
b3b7b66  Defensive rendering: guard config.schedule.frequency
03fd25c  Defensive deliver-field rendering on /scrapers and /deliver
4a8f8ee  Fix register page prerender: wrap useSearchParams in Suspense
7fc878d  Fix false claim that referral credit auto-applies to invoices
3a0a950  Sprint 7.3: referral program frontend
5f616cd  Sprint 5.5: activation funnel dashboard UI
09dff8b  Sprint 6.3 Phase 5: filter county picker to healthy connectors
```
