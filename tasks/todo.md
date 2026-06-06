# Post-Milestone Build — Snohomish Tax Scraper (Thread 1 of 3)

Direction (user): do all 3 post-milestone threads **one-by-one**, **Codex-verifies each**,
via a **dynamic workflow**, **security is priority**.
Order: (1) Snohomish tax scraper → (2) DNC scrubbing → (3) native dialer connectors.

## Research + security review — DONE (dynamic workflow `wf_0e4598c4-344`, salvaged)
- Snohomish research + adversarial security review COMPLETE (verdict GO-WITH-FIXES).
- Native-dialer-connectors research + security COMPLETE (thread 3, parked).
- DNC research agent ran away (1h45m) → killed; DNC was predicted blocked-on-decision anyway (thread 2).

## LIVE FILE INVESTIGATION — DONE (the required precondition)
Source: Snohomish "Current Tax List" — `…/DocumentCenter/View/149173/snohomish_tax_data_totals`
(linked off `…/5568/Treasurer-Public-Records`, updated monthly, **doc-ID rotates**).
- **Pipe-delimited `.txt`, NO header row, 17 cols, 325,043 rows, 44.7 MB**, UTF-8 BOM, `\r\n`.
- **No HTTP redirect** (direct 200) — disproves the security review's "302" High.
- Columns: `0`=parcel/account, **`1`=tax/bill YEAR**, `2`=situs addr, `4`=situs city, `5`=st, `6`=zip,
  `7`=owner, `10/11/12`=mailing city/st/zip, `13`=as-of date, `14`=total annual,
  `15`=half installment, **`16`=amount owed/balance**.
- `parcel len`: 304,477 are **14-digit real property** (target) + 20,566 7-digit personal-property (exclude).
- **Delinquent set = 14-digit parcel AND `year < current` AND `owed(col16) > 0` → 10,548 accounts.**
  col16==col15 for all 10,548; col16==col14 for 8,948. Amounts already clean numerics (no `$`/commas).
- A parcel can recur across years → **aggregate per parcel: sum owed, MIN(year)=oldest=most months delinquent.**

## Mapping to existing Phase 4 infra (ZERO API/UI/migration-column change)
- `delinquent_amount` ← sum(col16) per parcel
- `delinquent_bill_year` ← min(col1) per parcel (true tax year, King Jan-1 semantic family → months filter works)
- `party_name` ← owner (col7); `property_address` ← situs (col2 + city/st/zip); `mailing_address` ← mailing
- `enrichment_data.source` = `"snohomish_county_delinquent_taxes"` (gates `_extract_tax_fields`)

## Security fixes folded in (from adversarial review + live facts)
- [HIGH-confirmed] **44.7 MB download → worker OOM.** Add size-capped STREAMING download helper to
  `safe_http.py` (stream=True, per-hop SSRF revalidate, abort > `Settings.MAX_DOWNLOAD_BYTES`), write to
  temp file, parse line-by-line, filter delinquent in the loop. NEVER materialize 325K rows in RAM.
- [HIGH-downgraded] redirect → none live, but helper still follows+revalidates per hop (future-proof).
- [HIGH-resolved] months semantic → real bill-year col exists; populate directly, do NOT synthesize from CoD PDFs.
- [MED] **doc-ID rotation** → connector base_url = stable landing page; scraper parses the current
  "Current Tax List" link (exclude the "description of the fields" anchor) before download.
- [MED] **canary** → 0 delinquent rows parsed ⇒ raise (job FAILS loudly), never silent-empty.
- [LOW] all human fields → first-class `ScrapedRecord` cols (exporter `sanitize_for_csv`); none raw from enrichment_data.
- [LOW] errors → reference-id/clean operator message on download/parse failure; no silent-swallow (the
  `_run_inline_enrichment` landmine); fail loudly.
- SSRF allowlist: `add_scrape_domain("www.snohomishcountywa.gov")` at module top (worker importlib picks it up).

## Plan (phased, ≤5 files/phase, TDD, verify each)

### Phase A — safe_http size-capped download + settings  ✅ (commit ae8e61b)
- [x] `settings.py` + `.env.example`: `MAX_DOWNLOAD_BYTES` default 100 MB (104857600) — Codex-lowered from 250.
- [x] `safe_http.py`: `safe_download_to_file()` per-hop validate, stream, byte-cap abort, assert 200 + non-empty;
      cap logic extracted to pure `_stream_capped()`.
- [x] Tests in `test_safe_http.py` (mirrors src): 11 new — SSRF/https/cap-arg guards + `_stream_capped` real-I/O.

### Phase B — Snohomish scraper  ✅ (commit 8fc1c12)
- [x] `snohomish_wa_tax_delinquent.py` — pure-HTTP scraper; landing-link resolver (excludes desc twin);
      capped temp download + finally-cleanup; stream-parse; filter (14-digit + year<as_of + owed>0);
      per-parcel aggregate (sum owed, min year); year-level enrichment detail; structural-validation canary.
- [x] `tests/test_snohomish_tax.py` — 8 tests on REAL captured rows: multi-year aggregation, exclusions,
      malformed counting, link selection + id-rotation + no-link-raises.

### Phase C — wire-up: source gate + registry + migration  ✅ (commit 34b06b8)
- [x] `tasks.py` — `_extract_tax_fields` gate → `_TRUSTED_TAX_SOURCES` frozenset (King + Snohomish).
- [x] `registry.py` — module added to `_ALLOWED_SCRAPER_MODULES`.
- [x] `alembic/versions/040_*.py` — idempotent `county_connectors` INSERT; base_url = stable landing page.
- [x] +4 gate tests (Snohomish string-amount/int-year trusted; lookalike source ignored).

### Phase D — verify + Codex review + ship
- [x] py_compile / ruff (my files clean; tasks.py+registry.py pre-existing errors = on main, out of scope) /
      pytest (50 touched tests green; full suite collects 334, no import breakage).
- [~] Security Master Review (§14) on the diff — self-review below.
- [~] **Codex review the diff** (`codex review --base main`) — RUNNING. Critical/High from either = NO-GO.
- [ ] Live Railway smoke (scrape Snohomish tax_delinquent, confirm rows + delinquent_amount populated) — needs deploy.
- [ ] Merge to main (migration 040 deploy-order note), update BUILD_JOURNAL + memory.

## Security self-review (Master §14, BridgeLeads non-negotiables)
- **SSRF:** download host fixed county-gov (`add_scrape_domain` at module top + base_url host seeds allowlist);
  `safe_download_to_file` revalidates EVERY hop (`resolve=True`), `require_allowlisted=True`, `require_https=True`,
  refuses scheme downgrade; landing fetch via `safe_get(require_allowlisted=True)`. ✅
- **DoS/OOM:** hard byte cap (`MAX_DOWNLOAD_BYTES`) + early Content-Length reject + stream-to-disk + per-parcel
  aggregate (never 325K rows in RAM). ✅
- **CSV injection:** owner/situs/mailing → first-class `ScrapedRecord` cols (export `sanitize_for_csv` covers);
  nothing surfaced raw from `enrichment_data`. ✅
- **Tenant isolation:** no new queries; insert path keeps existing `user_id=job.user_id`; source-gate purely
  transforms enrichment_data. ✅
- **Source-gate trust:** frozenset exact-match; lookalike/untrusted sources ignored (tested); bounds/Decimal intact. ✅
- **Silent-empty / wrong-file:** structural validation (17-field, malformed-ratio) + zero-parcel canary → FAIL loudly,
  no silent-swallow. ✅
- **Secrets:** none added (public county data, no auth). ✅
- **Error leakage:** failures raise clean `RuntimeError`/`ValueError` (operator messages, no raw URL/stack to client;
  worker FAILED path attaches reference id as today). ✅
- **TCPA/DNC:** scraper emits no phones; `phone_dnc_flag` stays NULL → excluded from default dialer-ready set. ✅

## Pre-code gate
- [x] **Consult Codex on this approach** (session `019e9b22…`) — DONE. Approach sound, no architectural change.
  Reconciled refinements folded in (all adopted):
  - **Structural validation (not just zero-row canary):** expect 17 pipe-fields/row, col1 = 4-digit year;
    track malformed-row count, FAIL if malformed-rate high OR expected structure missing → catches the
    "county swapped the file, we parse the WRONG file but nonzero" silent failure (Codex's #1 prod risk).
  - **Year granularity in enrichment_data:** `delinquent_years[]`, `delinquent_year_count`, `oldest_tax_year`,
    `as_of_date` (col13) — audit/debug, don't collapse to just sum+min.
  - **bill_year is APPROXIMATE** (WA halves due Apr30/Oct31, not Jan1): keep `min(year)` for King-compat,
    document as approximation (both reviewers agree it's acceptable; same semantic family as King).
  - **MAX_DOWNLOAD_BYTES default = 100 MB** (104857600), not 250 MB — 512 MB worker under concurrency.
  - **Temp file:** `NamedTemporaryFile(delete=False)` + guaranteed `finally` unlink (Windows handle care).
  - **Test matrix:** parser/aggregation fixture; landing-link selection excludes "description of the fields"
    anchor; `_extract_tax_fields` IGNORES non-allowlisted source even with tax-looking fields; end-to-end
    source-string → both columns populated. + INFO metrics (bytes, rows, malformed, delinquent, parcels, oldest yr, total $).

# Thread 3 — Dialer connectors (full build, user-approved). Codex design consult done (session 019e9b22 follow-up).
Codex raised the bar: per-contact OUTBOX is required (PhoneBurner has no bulk endpoint → 500 POSTs →
partial-success silent loss). Refined scope to bound blast radius:
- **Phase A ✅ (c0943d4):** connector seam (ABC + GenericWebhookConnector byte-identical + dialer_type
  discriminator + sweep dispatch). 7 tests. No transport change.
- **Phase B foundation ✅ `fd06201`** (model + migration 041). **Phase B/C ✅ `fd677f0`:** process_dialer_outbox
  transport (creds re-read at send time, host allowlist, owner-match, response redaction, per-row state) +
  sweep vendor branch + materialize helper + replay endpoint + PhoneBurner connector + DeliverConfig creds
  (token write-only) + 17 tests. 4 HIGH fixes implemented. **Codex review of full Thread 3 diff: RUNNING.**
  Remaining: live smoke (needs user PhoneBurner creds). _Original Phase B/C scope below:_
- **Phase B (vendor-only outbox — generic path UNTOUCHED):** `dialer_delivery` outbox table (migration 041:
  id, job_id, result_id, user_id, scraper_config_id, vendor_id, status[pending|delivered|failed],
  attempts, last_error, vendor_response_code, vendor_contact_id, created_at, delivered_at). Sweep: for a
  VENDOR dialer_type, claim job → INSERT one outbox row per lead → enqueue chunked processor; GENERIC stays
  on the existing deliver_job_webhook path (no billing-path change). New `process_dialer_outbox` task:
  re-reads config from DB (owner-match ScraperConfig.user_id==Job.user_id + Result.user_id), builds vendor
  request via connector, POST host-allowlisted + response redacted, updates per-row status/last_error;
  creds built at send time (never a task arg). Replay endpoint (user_id-scoped) resets failed→pending.
  NOTE: generic catch-hook-URL-in-args is PRE-EXISTING status quo, not regressed; hardening it = documented follow-up.
- **Phase C:** PhoneBurner connector (contact-creation ONLY, host allowlist www.phoneburner.com, OAuth
  Bearer + owner_id from deliver config, extra=forbid + token validators). Live smoke needs user creds.

## Review — Thread 1 (Snohomish) SHIPPED + LIVE ✅
- Merged to main (`9a70bab`), pushed, deployed — health 200 (migration 040 applied on boot).
- **Live smoke against the real source:** 44.7 MB / 325,043 rows / 0 malformed → 10,548 delinquent rows →
  **4,269 parcels, all with delinquent_amount + bill_year**, $16.3 M total owed. Multi-year aggregation
  confirmed (VERIZON 2023+2024+2025 = $2,376.01). ZERO API/UI/migration-column change.
- Codex consult (pre-build) + Codex diff review (1 P2 found + fixed, no Critical/High). 58 tests, ruff-clean.
- Prod-API connector check blocked by permission classifier (not in deploy scope) — health-200 +
  idempotent migration + live smoke stand as proof.

## Next threads (2 & 3)
- **Thread 2 — DNC scrubbing:** BLOCKED-ON-DECISION (legal/vendor). Needs: can BridgeLeads scrub the
  federal DNC registry + pass the flag to customers, or is that the customer's SAN/responsibility? Which
  vendor (DNC.com / Contact Center Compliance / etc.) + budget? No real DNC source ⇒ nothing to build
  (no-mock rule). Surfaced to user.
- **Thread 3 — native dialer connectors:** research done, DEMAND-GATED. Smallest useful step = the
  DialerConnector abstraction seam + ONE reference connector, built when a paying customer names a dialer.

---

# Enrichment/skip-trace dedup-reuse + cache fix (2026-06-06) — IN PROGRESS

**Problem (user-reported, confirmed in code):** a `since_last_run` re-scrape inserts fresh `Result` rows
(192), dedup flags all `is_duplicate=true` (0 new), but enrichment + skip-trace key off "row missing field"
and run on ALL fresh rows — so duplicates get RE-enriched (GIS/PACS/King) and RE-skip-traced (paid Tracerfy,
"0 cache hits, 167 queued" ≈ $13). Dedup is delivery/billing-only; it never short-circuits enrichment.

**Root causes:** (1) no cross-job reuse — `_run_inline_enrichment`/`_enqueue_skip_trace_rows` never copy the
prior enriched Result. (2) skip-trace cache miss: WRITE keys off Tracerfy's echoed address
(`tracerfy_ingest.py:325`), READ keys off our GIS address (`tasks.py:1246`) — `address_cache_key` strips
punct/case but NOT USPS expansion (St→STREET), so a standardized echo ≠ our string → miss on re-run.

**Fix A — duplicate reuse (tasks.py), tenant-scoped:** in ENRICHING, before GIS/skip-trace, for this job's
`is_duplicate` rows JOIN `delivered_records (user_id=job.user_id, dedup_hash)` → `first_result_id` → prior
`Result (user_id=job.user_id)`; COPY property_address/mailing_address/parcel_id/enrichment_data/
delinquent_amount+year unconditionally, and phone/phone_type/phone_dnc_flag/email/skip_trace_status/attempted_at
ONLY when prior `skip_trace_status IN ('hit','miss')` (settled) AND within 90d (SKIP_TRACE_CACHE_DAYS). Existing
selectors then auto-skip them (address present → no GIS; status≠not_attempted → no Tracerfy). Fallback: if prior
lacks address/unsettled, duplicate flows through normal path. **SECURITY: every join leg filtered by
job.user_id (no cross-tenant copy = no IDOR); worker uses system session so the explicit user_id filter is the guard.**

**Fix B — cache key consistency (tracerfy_ingest.py):** write the cache keyed off the PENDING row's address
(`matches[0].property_address/city/state`, = what READ uses), not Tracerfy's echoed `csv_row` address →
write key == read key by construction → cross-job hits even when Tracerfy standardizes the street.

**Workflow:** Codex consult (pre-build, security) → build → Codex review + Master Security Review (§14) → deploy.
- [x] Codex consult on design — caught: weak-hash PII risk, fill-missing, settled-only TTL, FixB truncation bug, **global SkipTraceCache (no user_id)** finding
- [x] Implemented Fix A (`_reuse_enrichment_for_duplicates`) + Fix B (cache key + 512-trunc) — commit `6a2f343`, deployed to main
- [x] compile + ruff clean (baseline 5, +0); worker tests 10/12 (2 pre-existing watchdog/kombu broker fails, unrelated)
- [x] **3 Codex review rounds** — caught + fixed TWO P1s: (1) weak NAME|DATE hash → fixed via recompute-strong-key==dedup_hash gate; (2) placeholder parcel (all-zeros/junk passes is_strong_identity) → fixed via address-anchor-or-non-placeholder-parcel guard. Final review CLEAN.
- [ ] ⚠️ **OPEN finding (pre-existing, user decision):** `SkipTraceCache` is GLOBAL (keyed by address only, no `user_id`) → one tenant's skip-trace phone/email is served to another tenant who scrapes the same address. Intentional vendor-cache cost-saver, but a cross-tenant PII-reuse concern. Per-user cache would multiply Tracerfy spend. NOT changed — surfaced for the user.
- [ ] User verify on a re-run: log should show "Reused prior enrichment for N duplicate leads" + far fewer Tracerfy queued.

---

# Frontend shadcn rollout — continuation (2026-06-06)

Repo: sibling `Desktop/bridgeleads-web`. Reference screen = `/segments` (clean shadcn + `.impeccable.md` DNA).
Already migrated: `/segments`, `/results`. **Do NOT touch** (polished/complex, prior decision): `/dashboard` (806L),
`/scrapers/new` wizard (1768L). User picked **all 6 remaining**: `/deliver`, `/login`, `/register`,
`/admin/funnel`, `/admin/connectors`, `/results/[id]`.

**Method (per CLAUDE.md):** phased, ≤5 files/phase, `tsc --noEmit` + `next build` green + Codex review each
phase, user approval between phases. Step-0 dead-code cleanup before any >300L structural refactor.

### Cross-cutting decisions (settle before Phase 1)
- **D1 — Tokens:** namespaces already reconciled in `globals.css` — `--color-amber` = emerald `#10b981`/`#34d399`,
  `--color-text-primary: var(--foreground)`, `--primary: #10b981`. So migrate `style={{var(--color-*)}}` →
  Tailwind token classes (`text-foreground`, `text-muted-foreground`, `bg-card`, `border-border`, `text-primary`).
  **No brand-color change** — colors are already emerald via aliases.
- **D2 — Empty/Error states:** KEEP the established four-state convention components (`ErrorState`,
  `EmptyIllustration` — memory `project_frontend_ui_state_conventions`); don't rip working ones out for shadcn
  `Empty`. New/blank states may use shadcn `Empty` like `/segments`. Consistency *within* a screen > across.
- **D3 — Banned decoration:** remove `.impeccable`-banned bits while migrating — radial-glow gradient on
  `/login` (+ `/register`), any accent border-stripes, gradient text. Emerald stays RARE (primary action / live state).
- **D4 — base-nova = Base UI, not Radix:** ToggleGroup/Select APIs differ (value always array, no `type`).
  Prefer Button-based toggles + `native-select`/`select` carefully (memory + prior /segments choice).

### Phase 1 — `/deliver` (173L, 1 file) — safest user-facing win ✅ DONE (uncommitted)
- [x] Mapped inline-styled cards/badges → shadcn `Card`/`Badge` + token classes; dropped framer-motion + rainbow format colors (signal-over-decoration); neutral badges (emerald rare)
- [x] Kept `ErrorState`/`EmptyIllustration` (D2); kept react-query loading/error/empty/data four states; preserved `hasDestination` dialer/PhoneBurner logic
- [x] `tsc --noEmit` clean + `next build` green (lint incl.) → Codex review PASS (no findings) — awaiting commit/deploy decision

### Phase 2 — Auth `/login` (203L) + `/register` (422L), 2 files ✅ DONE (`5dac4ab`, deployed)
- [x] `input-base` → `Input` (h-10); `.btn-amber` → `Button` (full-width h-11); `<label>` → `Label`
- [x] Terms checkbox → Base UI `Checkbox` via RHF `Controller` (checked/onCheckedChange); links `stopPropagation` so opening Terms/Privacy doesn't toggle consent
- [x] Removed banned radial-glow + card glow shadow + framer-motion. Brand emerald kept BRIGHT via `var(--color-amber)` (not `--primary`, which is dull `#065f46` in dark) — buttons use default `bg-primary` like /segments
- [x] PRESERVED: onBlur, autofocus, noValidate, server-error block, password checklist, Suspense/useSearchParams referral; added `aria-invalid`/`aria-describedby`
- [x] `tsc` clean + `next build` green (login+register still static) → Codex review PASS (no P1, Controller flow verified)

### Phase 3 — Admin `/funnel` (264L) + `/connectors` (394L), 2 files ✅ DONE (`25c5042`, deployed)
- [x] funnel: window toggle → Button-toggles (aria-pressed); tokens; error → destructive; DM Mono numbers; emerald reserved for data bars (key data point)
- [x] connectors: btn-amber/ghost → Button, inputs → Input, labels → Label, AI/Manual chips → Badge (Manual neutral), skeletons → Skeleton, record-type pills → Button-toggles. **Fixed latent bug**: degraded health dot used `--color-amber` (=emerald) → now explicit emerald/amber/red-500 + title/aria-label. Mutation/gating/grouping behavior-identical
- [x] `tsc` clean + `next build` green (both static) → Codex review PASS (no P1)

### Phase 4 — `/results/[id]` (1186L, 1 file) — highest value, biggest risk; LAST
**REVISED after full read + Codex re-consult (session `019e9e79`):** table is coupled to framer-motion
(`motion.tbody className="contents"`, `motion.tr` variants, `layoutId` pagination/format pills) + a custom
sticky-blur `<thead>` in a `max-h-[calc(100vh-340px)]` scroll container. **Codex AGREES: do NOT import the
shadcn `Table` component** (its own `overflow-x-auto` double-wraps; `TableBody` would drop motion / risk
invalid nested tbody). Migrate IN PLACE instead — same design result, far lower regression risk. User QAs on deploy.
- [x] STEP 0 (`6a6df82`): removed dead no-op `useEffect`. `selectedFormat` confirmed **backend-dead** — LEFT + FLAGGED in commit msg (removing the pill is a product call)
- [x] Migration (`54b16f3`): `.input-base` → `Input` (search + 4 tax + a11y ids), `.btn-amber`/`.btn-ghost` → `Button`, tax `<label>` → `Label`, h1 → serif; **REMOVED banned 3px green left-hover stripe**; "Old" badge → neutral muted (New stays emerald). NO shadcn Table component (Codex-confirmed wrong fit)
- [x] PRESERVED (Codex-verified intact): scroll container, motion.tbody/.tr + expansion, layoutId motion, setPage(1), hasTaxData gate, export-tax-not-search, stopPropagation, colSpan={7}, is_duplicate opacity, search ref
- [x] `tsc` clean + `next build` green → Codex review PASS (no P1, all 10 landmines intact). ⏳ **user QA on Vercel deploy pending** (search/tax/export/expand/copy/mailto/pagination)

---

## Review — Frontend shadcn rollout COMPLETE ✅ (all 6 screens, 5 commits, deployed)
**Shipped to `master` → Vercel (frontend auto-deploys):**
- `f125202` Phase 1 `/deliver` · `5dac4ab` Phase 2 `/login`+`/register` · `25c5042` Phase 3 admin `/funnel`+`/connectors` · `6a6df82`+`54b16f3` Phase 4 `/results/[id]`
- Every phase: `tsc --noEmit` clean + `next build` green + **Codex diff review (no P1/no regressions)** before push.
- Pre-implementation **Codex consult** pressure-tested the plan; a 2nd Codex consult re-scoped Phase 4 (no Table component) after the full read.
**Key decisions:** D1 tokens already reconciled (`--color-amber`=emerald) → mechanical. D2 kept `ErrorState`/`EmptyIllustration`. D3 removed banned glow (auth) + accent hover-stripe (results). D4 Base-UI (not Radix) → Controller for Checkbox, Button-toggles not ToggleGroup. Brand emerald kept BRIGHT via `var(--color-amber)` (—primary is dull `#065f46` in dark); buttons use default `bg-primary` like /segments.
**Bonus fixes:** connector "degraded" health dot (was emerald via the `--color-amber` rename) → explicit emerald/amber/red + a11y title; +aria on auth/results inputs.
**Untouched by design:** `/dashboard` (806L), `/scrapers/new` wizard (1768L) — polished, rebuild = downgrade/risk.
**Follow-ups — RESOLVED ("do all yourself"):**
- ✅ **Format pill: REMOVED** (`f03861e`). Backend `/jobs/{id}/download` (jobs.py) is CSV-only (builds via `csv.DictWriter`, `media_type text/csv`, no `format` param) → the pill was decorative. Removed pill + `selectedFormat` state + `FORMAT_LABELS`; relabeled "Download CSV". Real multi-format in-app export = separate backend feature (needs xlsx injection hardening) — out of scope. tsc/build green, Codex clean.
- ✅ **Public-screen QA DONE myself** on the live deploy (`bridgeleads.io`, headless Chromium, **12/12 passed**): `/login` (2 Inputs/Button/Labels, no glow, onBlur+aria-invalid+error-id) and `/register` (form mounts past Suspense, 3 Inputs, **Base-UI Checkbox toggles via Controller**, live password checklist, Terms link, no glow, **0 console errors**). Confirmed migrated markup served in prod; no banned `radial-gradient`.
- ✅ All 4 gated routes deploy healthy (`307` auth-redirect, no 500): `/deliver`, `/admin/funnel`, `/admin/connectors`, `/results/[id]`.
- ✅ **GATED-screen QA DONE** (user-supplied account, headed Chromium, live — **15/16**): `/deliver` (132 shadcn Cards+Badges), `/admin/connectors` (full agency view: 25 Badges + 25 health dots **with a11y `title`**, Add-county form Inputs + Button-toggles), `/results/[id]` on a REAL result (`9f4e31a0…`: **"Download CSV"** confirms pill removed, search Input, **banned 3px stripe absent**, **search debounce updates table**, **row-expand works**). The 1 non-pass = 5 `next-auth` "Failed to fetch" session-fetch console errors — UNRELATED to the migration (auth untouched; migrated UI produced 0 errors), transient navigation noise.
- ✅ **`/admin/funnel` — REAL BUG FOUND + FIXED + live-QA'd** (`48d07b4`). The account IS `is_admin:true` server-side (verified via live `/auth/me`), but `lib/auth.ts` never threaded `is_admin` through authorize→jwt→session → `session.user.is_admin` was ALWAYS undefined → the funnel gated out **every** admin in prod. Fix: thread `is_admin` (strict `===true`, fail-closed) all 3 hops + augment next-auth types + gate the query `enabled:!!session && isAdmin` (Codex hardening: no 403-fetch for non-admins). Codex consult (design) + Codex review both clean. **Re-QA on deploy: 5/5** — admin passes gate, data view renders, window toggles flip aria-pressed, step rows + conversion cards render.
- ✅ **The 5 `next-auth` "Failed to fetch" — diagnosed BENIGN (no fix, Codex-agreed).** Controlled repro: idle 14s on one page = **0 console errors**; the errors only appear during rapid `goto()` navigation as `net::ERR_ABORTED` on in-flight `/api/auth/session` (same as react-query API calls) — navigation cancels them. Test artifact, not a defect; real users don't hit it and the session already works.

## Both post-rollout gaps RESOLVED (2026-06-06) — 6 commits total this session
`f125202`,`5dac4ab`,`25c5042`,`6a6df82`,`54b16f3`,`f03861e` (rollout + pill) + `48d07b4` (is_admin fix). All Codex-gated, deployed, live-QA'd.
- (optional, deferred) standardize empties on shadcn `Empty`; finish the invisible inline-`var`→class sweep on `/results/[id]` cells (renders identically today).

### Status
- [x] Codex pressure-test of THIS plan (pre-implementation, mandatory) — DONE (session `019e9e44`, 333k tok). Verdict: directionally sound; Phase 2 + 4 NOT purely mechanical (checkbox=Base UI, results table behavior-heavy). Refinements folded into Phases 2 & 4 above. No disagreements with the plan.
- [ ] User approval of plan + phasing — pending
