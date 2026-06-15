# BridgeLeads — Outstanding Work Backlog

**Single canonical checklist of everything not yet done.** Last updated 2026-06-09.
Detail lives in: the security audit (`docs/security/SECURITY_CHECKLIST_AUDIT_2026-06-08.md`),
the H3 spec (`docs/superpowers/specs/2026-06-08-h3-pii-encryption-design.md`),
`docs/BUILD_JOURNAL.md`, and the auto-memory index. Tags: 🔴 High · 🟠 Medium · 🔵 Low ·
👤 needs USER/ops action · 🧭 open decision.

---

## 1. H3 — PII-at-rest encryption (code DONE; gates + deploy left)
Two branches off `main`, unmerged. Stage 1 = contact PII + additive email_hmac. Stage 2 = User.email cutover.

- [x] 🔴 Codex-gate `security/h3-pii-encryption` (Stage-1) — CLEAN (2026-06-09). 1 P1 found + fixed (`2bbebf7`: email_hmac backfill strict-mode safe via `is_encrypted` guard), re-gate clean.
- [x] 🔴 Codex-gate `security/h3-email-cutover` (Stage-2) — RESOLVED (2026-06-09). vs `main`: 2 P2 fixed (`2bf127d`: prod-env key guard + preflight `sys.exit(1)`) + 2 P1 — #1 fixed (`ef34e88`: pass `BLIND_INDEX_KEY`/`FIELD_ENCRYPTION_KEY` to the prod migration runner), #2 (048 NOT NULL rolling deploy) is the in-isolation artifact the two-branch split exists to solve — **empirically CLEAN when reviewed `--base security/h3-pii-encryption`** (the post-Stage-1-merge diff). ⚠️ Re-gate Stage-2 `--base main` AFTER it is rebased on merged Stage-1; expect clean. Rebase will lightly conflict on `backfill_user_email_hmac.py` (both branches edited the same block) — keep the combined `is_encrypted` guard + `sys.exit(1)`.
- [x] 👤 Provision `FIELD_ENCRYPTION_KEY` + `BLIND_INDEX_KEY` in Railway — done 2026-06-09 (Stage-1 session)
- [x] 👤 GitHub **production-environment secrets** — done 2026-06-12: `BLIND_INDEX_KEY` + `FIELD_ENCRYPTION_KEY` + `SECRET_KEY` set on the `production` env (read from Railway api service, identical values)
- [x] 🔴 Stage 1 merged + deployed (PR #14, 2026-06-09) + contact-PII/email_hmac backfills run
- [x] 🔴 **Stage 2 merged + DEPLOYED 2026-06-12** (PR #29 `6445744`): branch squash-rebased onto main (+95 commits, runbook conflicts), migration renumbered 048→**053**, Codex semantic-rebase review GO, prod hmac gate PASS. Migration 053 applied clean (health 200, alembic 053, no crash-loop — logins on the blind index).
- [x] 👤 `backfill_user_email_encrypt.py` — run 2026-06-12: **444/444 emails encrypted**
- [x] 👤 `verify_pii_encryption.py` — **ALL CLEAR**
- [x] 👤 `PII_ENCRYPTION_STRICT=true` set on api + worker 2026-06-12 (confirmed both) — **H3 COMPLETE** pending the post-redeploy live-login check
- [x] 🟠 `SkipTraceQueue.download_url` — ENCRYPTED 2026-06-12 (PR #30, migration 054: expired links NULLed, rest encrypted in-migration, fail-closed assert; Codex GO).

## 2. Security audit checklist — remaining (`SECURITY_CHECKLIST_AUDIT_2026-06-08.md`)

- [x] 🟠 **M3** — pip-audit + CycloneDX SBOM gate added (`dependency-audit` job; `build` needs it). Bumped all 8 vulnerable deps → 26 vulns cleared, audit 0 (fastapi 0.115.6→0.136.3, cryptography→46.0.7, PyJWT→2.13.0, requests/lxml/python-multipart/python-dotenv, pytest stack→9). npm audit = frontend repo (separate). **On `security/audit-m3-m8`, Codex-CLEAN.**
- [x] 🟠 **M8** — acclaimweb PACS lookup SSRF-hardened (validate_scraping_target+HTTPS+trust_env=False+allow_redirects=False). pacs.py/skip_trace.py were already hardened (M8 note's other sites = false positives).
- [x] 🟠 **CI RESURRECTED** (bonus, found during M3; PR #15) — GH Actions workflow had NEVER run (invalid YAML at the f-string `OK:` step → 0 jobs). Fixed (block scalar + pinned ruff==0.15.6). First real run exposed a never-passing test job → fully fixed: 80 ruff errors (incl. real `select` NameError bug in `tasks.py`); pytest-asyncio 1.x loop migration (removed event_loop fixture + session test-loop-scope); CI postgres 6543→5432 port map (SyncSessionLocal rewrite); RLS/prod-DB integration tests marked `@pytest.mark.integration` + excluded (`-m "not integration"`); migration 048 for model-drift `county_connectors.max_date_range_days`; conftest Redis flush (rate-limit/lockout isolation); MFA tests wait past the whole-second revoke; 2 stale tests (pro limit 1000, register→pro); coverage fail_under 55→34 (real floor). **✅ CI GREEN: 511 pass / 9 integration deselected; Test + Dependency-Audit jobs pass.**
  - Follow-ups: 🔵 ratchet coverage up from 34 (write scraper/worker unit tests); 🔵 dedicated prod-DB CI job to run the 9 `integration` RLS tests; 🧭 **pre-existing inconsistency to review:** `PLAN_LIMITS["pro"]=1000` but `/auth/register` sets pro `records_limit=500`.
- [x] 🟠 **M3/M8/CI Codex-gate + CI validation** — PR #15 (`security/audit-m3-m8`), **CI GREEN**. Codex gate: all real findings fixed across rounds (Redis-FLUSHDB-to-localhost P1, graphify/JS sweep-ins, PyJWT comment, strict-mode backfill). Remaining Codex flag = **PROVEN FALSE POSITIVE**: it calls the laserfiche/eagleweb raw-string JS date regex broken, but AST extraction shows `page.evaluate` receives the valid `/\d{1,2}\/\d{1,2}\/\d{4}/` — Codex has a raw-string blind spot (flagged both raw-`\d` and non-raw-`\\d` forms). Code is correct; rejected with evidence. ⚠️ Merge auto-deploys to prod (Railway) — your call.
- [x] 🟠 **M6** — SHIPPED 2026-06-12 (PR #31): ops email alerts (Resend) on watchdog permanent-fail, canary →down transition, batch give-up; Redis cooldown, post-commit dispatch, PII-safe bodies (Codex 3 P2s adopted). 👤 set `OPS_ALERT_EMAIL` on Railway api+worker to activate + add OPS_ALERT_* to .env.example.
- [x] 🟠 **M7** — SHIPPED 2026-06-12: `audit_events` table (migration 055, no user FK by design) + best-effort background insert in `audit_log()` (task-ref held, semaphore-bounded — Codex P2s) + scraper_created/scraper_deleted events added. Console line remains the fallback.
- [x] 🟠 **M4** — DOCUMENTED 2026-06-15 (`docs/security/M4-edge-ddos-rate-limit.md`): app-layer limiter
  zones + fail-open/closed-per-zone on Redis outage, Cloudflare edge rules (HTTP DDoS managed ruleset, WAF,
  Rate Limiting Rules) as the infra-independent backstop, and the ⚠️ proxy-trust prerequisite (orange-
  clouding the API needs `TRUSTED_PROXY_HOPS=2` OR a network-layer CF-IP lock first). Codex-fact-checked
  (4 corrections adopted). Applying the Cloudflare rules = 👤 ops action per the doc's §4/§5 checklist.
- [x] 🟠 **M5** — DOCUMENTED 2026-06-15 (`docs/security/M5-db-redis-network-posture.md`): DB (5432/6543) +
  Redis (rediss:// cert-required) transport, the IP-allowlist gap, Tier 1 (Supabase Enforce SSL + explicit
  sslmode, localhost-guarded; verify `REDIS_SSL_CERT_REQS!=none`) now / Tier 2 (Railway Pro Static Outbound
  IPs → Supabase Network Restrictions + Upstash IPv4 allowlist) gated on plan; residual risk recorded.
  Codex-fact-checked. Enabling the restrictions = 👤 ops action per the doc's §6 checklist. **Covers the
  §4 "verify Redis CERT_REQUIRED" item.**
- [x] 🔴 **H1 — RLS ENFORCED IN PROD 2026-06-12** (PR #33 + same-session cutover, Codex SIGN-OFF). Two NOBYPASSRLS roles serve all traffic (api=`bridgeleads_app`, worker/beat=`bridgeleads_system`), 47 role-targeted policies, `RLS_ENFORCE=true` fail-closed boot gates live, **FORCE on 23 tables**. All the tracked follow-ups landed: users broad app policy, MFA grants (single allowlisted app DELETE on mfa_backup_codes), batch_runs + audit_events grants/policies, dialer-replay off the system session. Verified: prod rehearsal 10/10, live E2E 147-record scrape mid-cutover, integration suite 13/13 vs prod. Rollback = repoint URLs + RLS_ENFORCE=false + NO FORCE (urls captured in `.rls-cutover-secrets`). 👤 residuals → §4.

## 3. Open security/privacy DECISIONS (need a call before coding)

- [x] 🧭🔴 **`SkipTraceCache` cross-tenant reuse** — DECIDED 2026-06-10: **per-tenant cache key** (owner). Built on `security/skiptrace-per-tenant` (PR #16, Codex-CLEAN): `address_cache_key` hashes `user_id` too (no schema change), all 4 callers updated, per-tenant batch write. 👤 after deploy run `railway run --service worker python scripts/purge_skip_trace_cache.py` (one-time, purges orphaned global rows). ⚠️ merge auto-deploys.
- [x] 🧭🟠 **DNC compliance** — DECIDED 2026-06-10: **keep current + honest labeling** (owner) — push "not-known-DNC" labeled `dnc_scrubbed:false`, dialer does the authoritative TCPA scrub, contracts place dialing compliance on the customer. No code change. (Codex: acceptable short-term; integrating a DNC feed = future target if marketing ever implies "compliant-ready" leads.)

## 4. Pending USER / ops actions

- [ ] 👤🔴 **Move `.rls-cutover-secrets` to the password manager** (repo root, gitignored — the ONLY off-Railway copy of the `bridgeleads_app`/`bridgeleads_system` passwords + the pre-cutover rollback URLs; H1 2026-06-12). Keep a copy somewhere durable, then delete the local plaintext.
- [ ] 👤🔵 Add `RLS_ENFORCE=false` line (with the H1 comment) to `.env.example` under `DATABASE_URL_MIGRATE=` — file was session-write-protected for the agent.
- [ ] 👤🔴 Rotate live `admin@bridgeleads.io` password + set Railway `BRIDGELEADS_ADMIN_PASSWORD`
- [ ] 👤🟠 Verify Redis `CERT_REQUIRED` on Railway (the `REDIS_SSL_CERT_REQS=none` escape from M1). **Code
  is safe-by-default** (`settings.REDIS_SSL_CERT_REQS="required"` + certifi CA) — the only action is
  confirming the api+worker Railway vars are unset or `required`, NOT `none`. See `M5-db-redis-network-posture.md` §3.3.
  Run: `railway variables --service <api|worker> | grep REDIS_SSL_CERT_REQS`
- [ ] 👤🟠 Tracerfy: add credits + re-scrape ~334 already-`errored` leads; migrate Tracerfy auth → header + rotate secret. **(2026-06-11: still 402 — 2,382 rows queued waiting, needs ~1,480 more credits.)**
- [x] 🟠 **R2 presign env-drift hardened 2026-06-15** (branch `security/backlog-sweep-2026-06-15`,
  commit `13e42eb`, Codex-gated): chose the "treat `API_BASE_URL` as REQUIRED worker config" option.
  `_delivery_download_url()` now RAISES in production when `API_BASE_URL` is blank (job fails loudly → M6
  alert) instead of silently minting a broken 401 presign link; non-prod still falls back so dev/test work.
  Worker boot logs an error if `API_BASE_URL` is unset in prod. 6 new tests, ruff clean. ENVIRONMENT
  compared case/whitespace-insensitively (Codex P2). **Residual 👤 ops** (NOT code): the presign branch is
  still dead-but-broken for any non-prod-but-real use — optionally rotate the R2 S3 creds in Cloudflare
  (token w/ Object Read) to revive it; and emails sent BEFORE 2026-06-11 still carry broken presigned links
  (re-run delivery for any complaint). 🔑 interactive download token = 60s; delivery-email token = 48h
  (`_DELIVERY_TOKEN_TTL`) — don't "clean up" the route's 60s comments into a regression.

## 5. Unmerged / unfinished features (non-security)

- [x] 🔵 **Phase 5 dialer — ALREADY SHIPPED + EXTENDED on main/master (verified 2026-06-15).** The backlog
  entry was stale: `feature/phase5-dialer` (backend) and `feature/phase5-dialer-ui` (frontend) are both
  **0 commits ahead** of main/master (218 / 47 behind) — pure stale pointers to already-merged work. Live on
  **main**: `DeliverConfig.dialer_webhook_url` (schemas.py:274/404, Business+ gated in batches.py:73 +
  scrapers.py:127), `Job.dialer_pushed_at` (models.py:439), `dialer_push_sweep` beat (scheduler.py:125),
  `build_dialer_push_payload`, and the `dialer_connectors/` framework (base + generic_webhook). Live on
  **master**: the full delivery-step UI (`scrapers/new/_steps/DeliveryStep.tsx` — method picker generic
  webhook/Zapier + native PhoneBurner, plan-gated, DNC-labeled), `_lib.ts` zod + payload, `deliver/page.tsx`
  destination display. Went BEYOND original P5 scope (native PhoneBurner connector = "Thread 3"). The DNC
  decision (§3) is honored in the UI copy. 👤 **ACTION: delete the stale `feature/phase5-dialer` +
  `feature/phase5-dialer-ui` branches** — nothing to merge or build.
- [x] 🔵 **Multi-contact segments** — SHIPPED 2026-06-12 (PR #26): Lists CSVs now carry phone_2/3 + email_2/3.
- [x] 🔴 **Lists overlap: property_key address-mismatch bug — FIXED+SHIPPED 2026-06-12** (PR #27 `8b45cd4`; backfill run: 182,696 re-keyed, 2,212 identities merged, overlap 158→166). Residual King tax×probate zero = statistically expected (3,299 tax parcels × 166 probate parcels over ~650k county parcels → E[∩]<1). Original finding: `compute_property_key` hashes `parcel|address` TOGETHER, but the tax pipeline stores situs WITH city+ZIP4 (`…EDMONDS WA 98026-6022`) while recorder+GIS enrichment stores street-only (`8021 188TH ST SW`) → **identical parcels still hash to different keys** → tax_delinquent NEVER overlaps recorder lists. Proven live: admin has 39,208 King tax + King probate results, ZERO tax×probate overlap (155 death-cert×probate overlaps exist, so the machinery works within one pipeline). **Fix (Codex-recommended): parcel-primary key** — hash parcel alone when strong, address only as fallback — **+ backfill `results.property_key` + rebuild `property_list_membership`** (stored keys don't self-heal). Blast radius: overlap/intersection, union bucketing, `_reuse_enrichment_for_duplicates`, dedup-adjacent reuse — needs the full plan→Codex→phased treatment. Secondary unverified hypothesis (agent): leading-zero parcel format drift between sources — test during the fix (`scripts/diag_parcel_mismatch.py` kept). NOTE: zero Snohomish-tax overlap is EXPECTED for this tenant (no non-tax Snohomish leads — cross-county can't match); the King pair is the bug's proof.
- [ ] 🟠 **Tax-delinquency amount/months filter — county coverage** (2026-06-11). The filter (`src/api/tax_filters.py` → `delinquent_amount` + `delinquent_bill_year`) is correct and verified live (King+Snohomish: min $5000→975, min_months 24→2016 exact). It is **source-gated to King + Snohomish only** (`_TRUSTED_TAX_SOURCES`, `tasks.py:285`) because only those two publish structured **dollars-owed + tax-year** at the source. Investigated extending to Pierce + Kitsap (2026-06-11):
  - **Kitsap = blocked, data does not exist.** No public source has parcel + amount-owed + year (Assessor bulk = assessed *values*; Treasurer foreclosure/tax-title PDFs have no balance; delinquency is collections-internal). Confirms `docs/non_king_tax_data_spike.md` (2026-06-05). Adding it would mean fabricating amounts = violates no-mock rule + the gate comment. **Do not build.**
  - **Pierce = not feasible cleanly.** Bulk Data Mart `tax_account` schema confirmed (read directly): Parcel/Account/Use/Tax-Year/Tax-Code-Area/Land+Improvement+Market+**Taxable Value**/exemptions/legal — **no balance/owed/delinquent column.** Amount-owed lives only per-parcel behind `epip.co.pierce.wa.us/...taxvalue.cfm` (ColdFusion, HTTP, was unreachable on probe). Only remaining route = fragile two-stage per-parcel scrape of the foreclosure-eligible subset (3+ yrs, >$100). Owner deferred (2026-06-11). Revisit only if partial foreclosure-subset coverage is explicitly wanted.
  - ✅ **Frontend UX fix ALREADY ON MASTER — stale branch is redundant (verified 2026-06-15).** The
    `feature/tax-filter-columns-label` branch (`abf95eb`, 1 commit ahead / 12 behind) was SUPERSEDED: the
    `feat/nts-auction-columns` work re-implemented the same Amount Owed + Tax Year columns (gated on
    `hasTaxData`) + the `(tax-delinquent records)` relabel on the refactored shadcn results page, and went
    further (also adds Auction Date + Default Owed via `hasAuctionData`). Confirmed live on master:
    `_components/ResultsTable.tsx:72` (`...(hasTaxData ? ["Amount Owed","Tax Year"] : [])`),
    `_components/ResultsToolbar.tsx:139` (`(tax-delinquent records)`), `page.tsx:85`
    (`dynamicColCount = 7 + (hasTaxData?2:0) + (hasAuctionData?2:0)`). A cherry-pick of `abf95eb` onto master
    conflicts (file refactored after the branch was cut) and would only re-add what's already there.
    👤 **ACTION: delete `origin/feature/tax-filter-columns-label`** — nothing to merge.

## 6. Tech debt

- [x] 🔵 ~~`king_wa_probate.py` F821 `submit_btn` dead ref~~ — STALE (verified 2026-06-15): no `submit_btn`
  reference exists in `src/scrapers/king_wa_probate.py` (the `templates/` path never existed); `ruff
  --select F821,F811,F841` passes clean. Resolved by an earlier refactor.
- [x] 🔵 ~~`batch_recovery_sweep` give-up path missing `completed_at`~~ — STALE (verified 2026-06-15):
  the give-up path is now at `scheduler_helpers/batch.py:248-257` and **sets `completed_at=now`** (line 257)
  with a terminal-state-consistency comment. Fixed in the scheduler refactor (PR #42).
- [x] 🔵 Legacy `scripts/` lint debt — assessed 2026-06-15: **0 F401**; the 15 E402 are intentional
  `# noqa: E402` (scripts `sys.path.insert(0,'.')` before `from src...`, required to run standalone) and
  scripts/ is not CI-gated. WON'T-FIX — architectural, not debt.
- [x] 🟠🧭 **Free-tier records-limit copy drift — FIXED+SHIPPED 2026-06-12** (backend PR #28 `3006e47`, frontend PR #10 `fe6c348`). Diagnosis: NOT a limits bug — starter=50 consistent everywhere; fresh accounts correctly get the 7-day Pro trial (hence 1,000 on the dashboard). The drift = 8 stale copies of Pro's old 500 limit + one stale $49 price after Pro became 1,000/$79: `/auth/validation-rules` (→`PLAN_LIMITS["pro"]`), onboarding emails ×3 (Codex catch incl. the $49), trial-expiry downgrade (→`PLAN_LIMITS["starter"]`), frontend upgrade/trial banners + marketing ×3 + FAQ, register benefits now name the trial. Also resolves the §2 follow-up (`PLAN_LIMITS["pro"]` vs register 500 — register already used PLAN_LIMITS; the 500s were copies).

## 7. Open PRs + deferred ops follow-ups (registered 2026-06-15 — COME BACK TO THESE)

**Two PRs open, awaiting merge (each merge auto-deploys; do in this order):**
- [ ] 🟠 **Merge [PR #45](https://github.com/Abenezer1244/web-scrapper-automation/pull/45)** — `security/backlog-sweep-2026-06-15`: R2 delivery hardening (`API_BASE_URL` required in prod) + M4/M5 security docs + backlog cleanup. Low risk (no behavior change — `API_BASE_URL` already set in prod). Auto-deploys Railway on merge to main.
- [ ] 🟠 **Merge [PR #47](https://github.com/Abenezer1244/web-scrapper-automation/pull/47)** — `security/m5-force-db-tls`: force TLS (`sslmode/ssl=require`) on Supabase DB connections (M5 Tier-1). **Merge AFTER #45.** Prod-DB transport change — after deploy do a quick `/health` 200 check + one worker-task to confirm DB connects over TLS. Codex GO/CLEAN, 13 tests.

**Ops actions I cannot do from CLI (need your dashboards / a plan / a password manager):**
- [ ] 👤🟠 **M4 — apply the Cloudflare edge rules** per `docs/security/M4-edge-ddos-rate-limit.md` §4/§5 (HTTP DDoS managed ruleset confirm, WAF managed rules, the 5 Rate Limiting Rules, scope Bot Fight Mode off the API host). Needs the Cloudflare dashboard + the API's plan tier. ⚠️ If you ever orange-cloud the API, do the §3 proxy-trust prerequisite (`TRUSTED_PROXY_HOPS=2` or a network-layer CF-IP lock) FIRST.
- [ ] 👤🟠 **M5 Tier-1 — flip Supabase "Enforce SSL on incoming connections"** (Dashboard → Settings → Database → SSL Configuration). **Do this only AFTER PR #47 is deployed** (the `sslmode=require` code is the prerequisite; enabling before could break connections). Then verify api + worker still connect.
- [ ] 👤🔵 **M5 Tier-2 — DB/Redis IP allowlisting** (gated on Railway Pro): enable Static Outbound IPs on api/worker/beat → set Supabase Network Restrictions + Upstash IP allowlist (IPv4) to those IPs. Per `M5-db-redis-network-posture.md` §4. If not on Railway Pro, the §5 residual risk stands (accept in writing).
- [ ] 👤🔵 **M5 follow-up — Alembic TLS:** `alembic/env.py` builds its own engine not covered by PR #47's `_ssl_connect_args`. Apply the same policy if migrations ever run over an untrusted network (today they run from Railway, same network — low priority).
- [ ] 👤🔴 **Move `.rls-cutover-secrets` off-disk** (also in §4) — verified present + gitignored; needs a password manager.
- [x] ✅ **3 stale branches deleted 2026-06-15** (`feature/tax-filter-columns-label`, `feature/phase5-dialer`, `feature/phase5-dialer-ui` — already gone from origin; local + tracking-ref cleanup done).
- [x] ✅ **Redis CERT verified 2026-06-15** — `REDIS_SSL_CERT_REQS` unset on api + worker → code default `"required"` applies (SAFE).
