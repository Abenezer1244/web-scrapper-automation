# BridgeLeads — Build Journal

**Purpose:** The running, append-only record of what was **built**, **tried**, **failed**, and
**succeeded** — plus the decisions behind them. Newest entry on top. This is the place to look
to understand *why* the code is the way it is and *what's been attempted before*.

> **How to add an entry** (do this at the end of any substantial session):
> ```
> ## YYYY-MM-DD — <short title>
> **Built / Shipped:** what actually landed (with commits/paths).
> **Tried / Decided:** approaches considered, chosen, or rejected — and why.
> **Failed / Blocked:** what didn't work, dead ends, external blockers.
> **Caught & fixed:** bugs found in review before shipping.
> **Pending / Handoff:** what's left, who owns it.
> **Facts learned:** durable truths about the system worth remembering.
> ```
> Keep it honest — record failures and dead ends, not just wins. See also
> `docs/security/REVIEW-2026-06-01.md` (security tracker) and `CLAUDE.md`.

---

## 2026-06-06 (pm) — Frontend shadcn rollout: 6 screens migrated + shipped

**Built / Shipped:** Continued the shadcn rollout from `/segments` (the reference) across the 6 remaining
un-polished screens, in 4 Codex-gated phases, all merged to `master` + auto-deployed (Vercel). Repo = sibling
`bridgeleads-web`. Commits: `f125202` P1 `/deliver` · `5dac4ab` P2 `/login`+`/register` · `25c5042` P3 admin
`/funnel`+`/connectors` · `6a6df82`+`54b16f3` P4 `/results/[id]`. Every phase passed `tsc --noEmit` + `next build`
+ a **Codex diff review (no P1, no regressions)** before push.

**Tried / Decided:** Pre-implementation **Codex consult** pressure-tested the plan (session `019e9e44`): confirmed
directionally sound but flagged P2 + P4 are NOT purely mechanical. Key calls — D1: token namespaces already
reconciled in `globals.css` (`--color-amber`=emerald `#10b981`/`#34d399`, `--color-text-primary:var(--foreground)`)
so this was mechanical token-rename + primitive-swap, **no brand-color change**. D2: kept the established
`ErrorState`/`EmptyIllustration` four-state components (didn't rip out for shadcn `Empty`). D3: removed
`.impeccable`-banned decoration (auth radial-glow + card glow shadow; the results accent hover-stripe). D4:
base-nova = **Base UI not Radix** → Terms checkbox bound via RHF `Controller` (not `register`), Button-based
toggles (not ToggleGroup). Brand emerald kept **bright** via `var(--color-amber)` because `--primary` is
intentionally dull (`#065f46`) in dark — but buttons use default `bg-primary` to match the /segments reference.

**Phase 4 re-scope (the important decision):** after reading all 1186 lines, a **2nd Codex consult**
(`019e9e79`) confirmed: do **NOT** import the shadcn `Table` component into `/results/[id]`. The table is
coupled to framer-motion (`motion.tbody className="contents"`, `motion.tr` variants, `layoutId` pagination +
format pills) and a custom sticky-blur `<thead>` in a `max-h-[calc(100vh-340px)]` scroll container — shadcn
`Table`'s own `overflow-x-auto` would double-wrap it and `TableBody` would drop the motion / risk invalid
nested tbody. Migrated **in place** instead (form controls → primitives, removed banned stripe, neutral "Old"
badge) preserving the full Codex landmine list (scroll container, motion, `setPage(1)`, latched `hasTaxData`,
export-tax-not-search, `stopPropagation`, `colSpan={7}`, `is_duplicate` opacity). Same design result, far lower risk.

**Caught & fixed:** connector "degraded" **health dot was rendering emerald** (identical to "healthy") — a latent
bug from the earlier `--color-amber`→emerald rename; replaced with explicit emerald/amber/red-500 + `title`/aria
(never signal with color alone). Added `aria-invalid`/`aria-describedby` on auth + tax inputs.

**Failed / Blocked:** `/results/[id]` cannot be QA'd headlessly (no creds + needs a real result set). User
chose "ship + I QA on deploy." `tsc`+`build`+Codex are green but **don't** cover its coupled runtime behaviors —
manual browser QA of search/tax/export/expand/copy/mailto/pagination is the outstanding gate. `codex review --base`
flag is unsupported in the installed CLI (dropped it; default-diff review works). Direct `eslint` fails (project
uses Next eslintrc, not flat config) → lint runs via `next build`.

**Facts learned:** (1) Two emeralds coexist by design — `--primary` (fill, dull in dark `#065f46`) vs
`--color-amber`/`--color-green` (bright accent text/icons, `#34d399` dark); use primary for button FILLS,
the bright vars for accent TEXT on dark. (2) `components/ui/input.tsx` wraps Base UI input and forwards ref →
RHF `register()` + `useRef` bind fine; `checkbox.tsx` is Base UI (no native input) → needs `Controller`.
(3) shadcn `Input` default is `h-8` (too compact for forms → `h-10`); `Table` self-wraps `overflow-x-auto`.
(4) `/results/[id]` format pill is **backend-dead** (`getExportUrl` ignores `selectedFormat`).

**Follow-up resolution (same session, "do all yourself"):**
- **Format pill REMOVED** (`f03861e`): confirmed backend `/jobs/{id}/download` is CSV-only (`csv.DictWriter`,
  `text/csv`, no `format` param) so the CSV/Excel/JSON pill was purely decorative. Removed pill + `selectedFormat`
  + `FORMAT_LABELS`; relabeled "Download CSV". Chose remove over wire (multi-format = separate backend feature
  needing xlsx formula-injection hardening beyond `sanitize_for_csv`). tsc/build green, Codex clean.
- **Public-screen QA done myself** via headless Chromium against `bridgeleads.io` — **12/12 passed**: `/login`
  (Inputs/Button/Labels, no glow, onBlur+aria-invalid) + `/register` (mounts past Suspense, **Base-UI Checkbox
  toggles via Controller in prod**, live password checklist, no glow, **0 console errors**). All 4 gated routes
  return `307` auth-redirect (healthy, no 500). QA script: `%TEMP%/claude/qa_auth.py`.
- **Hard blocker:** interactive QA of GATED screens' authed content (`/deliver`, admin `/funnel`+`/connectors`,
  `/results/[id]` table behaviors) needs a real session + data + admin — a throwaway signup has no jobs/scrapers
  and isn't admin. Needs a user-supplied test login to finish.

- **Gated-screen QA DONE** (user-supplied account, headed Chromium, live — **15/16**): `/deliver` 132 shadcn
  Cards+Badges; `/admin/connectors` full agency view (25 Badges + 25 health dots WITH the a11y `title` fix, Add
  form Inputs + Button-toggles); `/results/[id]` on a real result — **"Download CSV"** (pill removal confirmed),
  search Input, **banned 3px stripe absent**, **search debounce updates table**, **row-expand works**. The 1
  non-pass = 5 `next-auth` "Failed to fetch" session-fetch console errors, UNRELATED to the migration (auth
  untouched; migrated UI = 0 errors). `/admin/funnel` data view unverified: account is agency but not `is_admin`,
  so it correctly showed the "Admin access required" gate.

**Post-QA gap work (same session, Codex-driven):**
- **REAL BUG FIXED — admin gate (`48d07b4`):** the funnel-QA gap turned out to be a production bug, not a data
  gap. The account IS `is_admin:true` server-side (verified via live `/auth/me`), but `lib/auth.ts` never threaded
  `is_admin` through authorize→jwt→session, so `session.user.is_admin` was ALWAYS undefined → `/admin/funnel`
  gated out EVERY admin. Fixed: thread `is_admin` (strict `===true`, fail-closed) all 3 hops + augment
  `types/next-auth.d.ts` + gate the query `enabled:!!session && isAdmin` (Codex hardening — no pointless 403 fetch
  for non-admins). UI gate only; backend `/billing/activation-funnel` independently enforces; value is
  server-sourced + sealed in the Auth.js-signed JWT (unforgeable). Codex consult (design) + Codex review clean.
  Re-QA on deploy: 5/5 — admin passes gate, data view + window toggles + step rows + conversion cards all render.
- **`next-auth` "Failed to fetch" — diagnosed BENIGN (no fix, Codex-agreed):** controlled repro showed idle-14s =
  0 console errors; the errors only occur during rapid `goto()` navigation (`net::ERR_ABORTED` on in-flight
  `/api/auth/session`, same as the react-query API calls). Navigation cancels in-flight fetches — test artifact,
  not a defect.
- **Facts learned:** next-auth v5 only carries what the jwt/session callbacks explicitly copy — any new
  `/auth/me` field (is_admin, etc.) MUST be threaded authorize→jwt→session AND declared in `types/next-auth.d.ts`
  or it's silently undefined client-side. `plan` was threaded; `is_admin` was the one that got missed.

**Pending / Handoff:** optional DS pass (standardize empties on shadcn `Empty`; invisible inline-`var`→class sweep
on results cells). Untouched by design: `/dashboard`, `/scrapers/new` wizard. **Rollout + both gaps DONE + live-QA'd.**

---

## 2026-06-06 — Full endpoint security audit (45 endpoints) + frontend shadcn library

**Built / Shipped:** (1) Security audit of ALL 45 API endpoints + fixes (merge `cdb6c0f`, deployed,
health 200). (2) Full shadcn/ui library into the frontend + `/segments` rebuilt as the reference screen.

**Security audit (the priority):** parallel per-file agents (one per route file) + **Codex independent
cross-check** + Codex diff-review gate. **No Critical, no missed High** — both reviewers confirmed the
multi-tenant core is solid: no IDOR (job_id/config_id/scraper_id all owner-scoped incl. the new
dialer-replay), no SQLi (segments binds `ANY(:types)`), Stripe webhook signature sound, no
mass-assignment (plan/is_admin/user_id never in request bodies). Fixed the 7 Highs:
- `billing.py`: 6 unthrottled endpoints rate-limited. The 3 outbound-Stripe ones
  (/subscription,/checkout,/portal) use a NEW `stripe` zone (10/min/user) added to `_FALLBACK_ZONES`
  so it **fails CLOSED** — a stolen JWT can't loop them to drain Stripe quota even during a Redis
  outage (Codex refined my first pass, which used the fail-open `general` zone).
- `webhooks.py`: Tracerfy secret was in the URL PATH (leaks to access logs). Added preferred
  header-auth `POST /webhooks/tracerfy` (`X-Tracerfy-Webhook-Secret`); kept legacy path route +
  header-first so live skip-trace ingestion doesn't break during migration.
- Report: `docs/security/ENDPOINT-AUDIT-2026-06-06.md` (+ Medium follow-ups + ops migration steps).

**Frontend:** pulled the FULL shadcn registry (46 components → 60 total in components/ui/) on the
existing base-nova theme; existing customized primitives preserved (skip-on-exists). 2 integration
fixes (Skeleton accepts div attrs; calendar uses react-day-picker@10 `month_grid`). Rebuilt `/segments`
(Lists) on shadcn Button/Badge/Table/Empty per `.impeccable.md` design context (created this session
via `/impeccable teach`). All Codex-clean, tsc + next build green, shipped to master.

**Tried / Decided:** Established `.impeccable.md` design context (confident & in-control, emerald/PT-Serif
DNA, anti-AI-slop). DECIDED NOT to blanket-rebuild already-polished screens (dashboard 804L, wizard
~1700L) — shadcn-for-its-own-sake would downgrade them + risk the live app; reserve rebuilds for plain
screens + new work. base-nova is **Base UI** (`@base-ui/react`), NOT Radix — ToggleGroup etc. have
non-standard APIs (value is always array, no `type` prop); used Button-based toggles in /segments instead.

**Failed / Blocked:** none this session. OPS pending: migrate Tracerfy to the header webhook + rotate
`TRACERFY_WEBHOOK_SECRET` (then drop the legacy path route).

**Facts learned:** (1) app role uses PER-TABLE grants + a convergence guard; system role has ALL-TABLES
— so worker-only tables dodge the RLS-grant landmine. (2) rate_limit zones: `general` fails OPEN,
`auth`/`webhook`/`stripe` fail CLOSED (`_FALLBACK_ZONES`). (3) base-nova = Base UI, not Radix.
(4) `/impeccable` design context lives in `bridgeleads-web/.impeccable.md`.

**Pending / Handoff:** UI screen-by-screen rollout (with Codex per screen) — /segments done; dashboard +
others pending (user wants all, I flagged polished-screen risk). Tracerfy webhook ops migration.

---

## 2026-06-05 — Post-milestone Threads 2 & 3: DNC (deferred) + dialer connectors (built)

**Built / Shipped:** Native dialer connectors (Thread 3) on `feature/dialer-connectors` (6 commits,
31 dialer tests, Codex review clean after 2 P2 fixes). Merged to main + deployed (migration 041).
- **`c0943d4` seam:** `src/workers/dialer_connectors/` — `DialerConnector` ABC + `GenericWebhookConnector`
  wrapping `build_dialer_push_payload` BYTE-IDENTICAL (locked by a regression test) + `deliver.dialer_type`
  discriminator (validated vs `REGISTERED_DIALER_VENDOR_IDS` in `constants.py`, kept out of `src.workers`
  so the API schema validates without importing Celery; `get_connector` lazy-imports).
- **`fd06201` outbox:** `DialerDelivery` model + migration 041 `dialer_deliveries` (per-contact state).
  Worker/system-only like `delivered_records` — NOT app-granted (app uses per-table grants; system has
  ALL-TABLES), so the replay endpoint uses the system session + explicit user_id filter, NO RLS-cutover change.
- **`fd677f0` transport + `97c96ef` P2:** `process_dialer_outbox` (chunked drain, per-row commit BEFORE
  next POST = at-most-once-per-contact, creds re-read from DB at send time, owner-match, host allowlist,
  response redaction) + sweep VENDOR branch (`_materialize_dialer_outbox` ON CONFLICT) + replay endpoint +
  PhoneBurner connector (contact-creation ONLY, host-pinned, OAuth, token write-only) + `DeliverConfig`
  model_validator requiring creds when `dialer_type=phoneburner`.

**Tried / Decided:** Codex design consult RAISED the bar — a single error column was wrong; PhoneBurner has
no bulk endpoint (500 leads = 500 POSTs → partial-success silent loss), so a per-contact OUTBOX with replay
is required. Scoped Phase B vendor-only: the GENERIC webhook path is UNTOUCHED (its catch-hook-URL-in-args
is pre-existing status quo, not regressed). Merged with PhoneBurner DORMANT (no user has dialer_type=phoneburner),
so the deploy is low-risk; the generic path is byte-identical.

**Thread 2 (DNC) — DEFERRED after research** (`docs/dnc_scrubbing_spike.md`): TCPA liability is the CALLER's
(the customer), not the lead-gen platform, so DNC scrubbing is a value-add, not a compliance gap. The federal
registry is per-SAN + non-redistributable; the buildable path is a commercial scrub API (DNC.com) gated on a
vendor account + budget. Current model (phone_dnc_flag NULL → dialer scrubs) is legally defensible as-is.

**Failed / Blocked:** PhoneBurner live smoke is BLOCKED on user OAuth creds (no-mock) — the connector's exact
field names come from public docs and need confirmation against the live API on first smoke. The earlier DNC
research agent ran away (killed). A prod-API connector check was blocked by the permission classifier.

**Caught & fixed:** Codex P2 ×2 — (1) outbox committed once per chunk → a crash after a successful POST left
rows 'pending' → duplicate contacts on replay; fixed with per-row commit. (2) DeliverConfig accepted
dialer_type=phoneburner without creds → jobs failed later; fixed with a model_validator.

**Facts learned:** (1) The app role (`bridgeleads_app`) uses PER-TABLE grants (+ a convergence guard that
revokes over-grants), so a new app-readable table needs registration in `provision_rls_roles.sql`; the system
role has ALL-TABLES grant, so worker-only tables work without RLS-script changes — make new worker tables
system-only + explicit-user_id-filtered to dodge the RLS landmine. (2) `safe_get`/`safe_get_following` load
the whole body in RAM; bulk downloads use the new `safe_download_to_file`. (3) Keep vendor credentials OUT of
Celery task args (they serialize into the Redis broker/result backend) — re-read from DB at send time.

**Pending / Handoff:** PhoneBurner live smoke (needs user OAuth token + owner_id, supplied via env/app config
not chat). Other dialers (BatchDialer/CallTools/Mojo) demand-gated. DNC scrubbing gated on a vendor decision.

---

## 2026-06-05 — Post-milestone Thread 1/3: Snohomish tax-delinquent scraper (SHIPPED + LIVE)

**Built / Shipped:** Snohomish County WA tax-delinquent scraper, extending the shipped Phase 4
tax filters (amount owed + months delinquent) from King to a 2nd county. Merged to main + deployed
(merge `9a70bab`, migration 040 applied on boot, health 200). Five commits on
`feature/snohomish-tax-delinquent`:
- `ae8e61b` **Phase A** — `safe_download_to_file()` in `src/utils/safe_http.py`: SSRF-revalidated
  per-redirect-hop, streams to disk, aborts past `Settings.MAX_DOWNLOAD_BYTES` (new, 100 MB) so a
  45 MB county file can't OOM the 512 MB worker. Cap logic in pure `_stream_capped()` for real-I/O tests.
- `8fc1c12` **Phase B** — `src/scrapers/snohomish_wa_tax_delinquent.py`: pure-HTTP (no browser).
  Resolves the monthly-rotating "Current Tax List" link off the stable landing page (excludes the
  same-named "description of the fields" twin), streams the pipe-delimited bulk file, aggregates
  PER PARCEL (sum owed across delinquent years, oldest year = bill_year). Structural-validation
  canary (17-field shape + malformed-ratio + zero-parcel) fails loudly on a wrong/changed file.
- `34b06b8` **Phase C** — `_extract_tax_fields` source gate widened to a `_TRUSTED_TAX_SOURCES`
  frozenset (King + Snohomish); registry allowlist; migration 040 (idempotent connector INSERT,
  base_url = stable landing page so the SSRF allowlist seeds + the scraper resolves the file link).
- `0761e75` **Codex P2 fix** — leave `doc_type` NULL (like King tax) so the cached-records filter's
  `doc_type IS NULL` branch keeps rows visible; the slug `tax_delinquent` matched neither that nor
  the keyword ILIKE patterns.

**Live smoke (real source, prod code path):** 44.7 MB, 325,043 rows, 0 malformed, 10,548 delinquent
rows → **4,269 unique parcels, every one with `delinquent_amount` + `bill_year`**, $16.3 M total owed.
Multi-year aggregation confirmed (VERIZON $2,376.01 across 2023+2024+2025). ZERO API/UI/migration-column
change — the existing Phase 4 columns/filters/UI light up data-driven.

**Tried / Decided:** Dynamic workflow (3 threads × research + adversarial security review) to scope the
work. Codex consult BEFORE coding (approach sound, 6 refinements folded: structural validation beyond
zero-row, year-level enrichment detail, 100 MB cap not 250, temp-file discipline, fuller test matrix).
Chose the bulk Treasurer "Current Tax List" (real bill-year column) over the scanned Certificate-of-
Delinquency PDFs (security reviewer flagged synthesizing bill_year from CoD membership as a months-filter
semantic bug — foreclosure-entry year ≠ bill year). bill_year is an accepted approximation (WA halves due
Apr/Oct, King treats bill_year ≈ Jan 1; same family). Personal-property (7-digit) accounts excluded.

**Failed / Blocked:** The DNC-scrubbing research agent (Thread 2) ran away ~1h45m (endless web searches) →
killed; salvaged the other two threads. The prod-API connector check via admin login was (correctly)
blocked by the permission classifier — not covered by the deploy approval; relied on health-200 +
idempotent-migration as proof instead.

**Caught & fixed:** Codex diff review found 1 P2 (doc_type slug hides rows from cached-records endpoint) —
fixed by mirroring King's NULL. No Critical/High from either reviewer.

**Facts learned:** (1) Snohomish "Current Tax List" = pipe-delimited `.txt`, NO header, 17 cols, ~45 MB,
325 K rows, DocumentCenter doc-ID ROTATES monthly (parse the landing page, never hard-code the id); the
"description of the fields" link is actually a same-named prior-month data dump, not a description.
(2) Delinquent = 14-digit parcel AND tax-year < as-of-year (col 13) AND owed (col 16) > 0; col 16 = balance,
col 15 = half, col 14 = total annual. (3) `safe_get`/`safe_get_following` materialize the whole body in RAM
— for big files use the new `safe_download_to_file`. (4) Adding a tax county = scraper + one line in
`_TRUSTED_TAX_SOURCES` + registry allowlist + a connector migration; columns already exist (038).

**Pending / Handoff:** Thread 2 (DNC scrubbing) — needs a legal/vendor DECISION (can BridgeLeads scrub
the federal DNC registry and pass the flag to customers, or is that the customer's SAN/responsibility?);
no real DNC source = nothing to build yet (no-mock rule). Thread 3 (native dialer connectors) — research
done, demand-gated (build the abstraction seam + 1 reference connector when a customer names their dialer).
Next non-King tax county = Snohomish is done; Pierce (per-parcel only) / Kitsap (foreclosure PDFs) remain weak.

---

## 2026-06-06 — MILESTONE COMPLETE: frontend P2b/P3/P5 UI + backfills run + bulk-optimized

**Lead-Targeting & Delivery milestone is now fully shipped — all backend (P1-P5), all frontend UI, security hardening, and historical backfills are live.**

**Frontend (all merged to `bridgeleads-web` master → Vercel; each Codex-reviewed to clean):**
- **P5 dialer settings** (`76e4cda`): `dialer_webhook_url` field in the config wizard delivery step (Business+), with proper `new URL()` https validation (not a prefix regex), an invalid-submit toast + inline field errors, and the dialer hook shown on the Deliver page. Codex caught 4 across rounds (test-run bypass, silent save, weak regex, missing delivery display).
- **P2b doc-type selector** (`9e5100e`): pre-foreclosure document-type checkboxes on the Record-type step, gated on `connector.pre_foreclosure_doc_types` (King/Pierce); selection flows into `doc_types` on both payloads; empty = omit (backend rejects `[]`); reset only on actual type change (Codex P3).
- **P3 Lists/Segments builder** (`421ec68`): net-new `/segments` screen — record-type chips, "On both lists" (intersection ≥2) vs "Combine" (union ≥1) toggle, Build → preview table with an "On N lists" overlap badge (+ weak tag), Export CSV. New `getSegmentIntersection/Union` + `exportSegment` (POST+blob) in lib/api.ts; types match the REAL backend shapes (no pagination). Codex caught 5 (death_certificate omitted, stale-criteria export, in-flight race, failed-build-shown-as-empty) — all fixed.

**Dynamic workflow:** used the Workflow tool (2 parallel Explore agents) to produce build-ready, integration-aware plans for P2b + P3 before building — fast parallel research, then I built + Codex-gated each (agents didn't mutate the repo).

**Backfills (prod, bulk-optimized `084631a`):** property_key=160,011 / tax=58,269 / membership=29,091. Per-row over remote prod was ~4h → bulk `UPDATE…FROM(VALUES)` = minutes. Gotchas: run with `PYTHONPATH=.`; the supabase pooler had a transient DNS blip (idempotent re-run fixed it); silence SQLAlchemy echo.

**Facts learned:** (1) the four-states discipline (loading/error/empty/data) keeps biting — a failed build must not render as "empty"; the filtered-`total==0` ≠ empty-job trap recurs on every new filter. (2) Record types are DB-driven — never hardcode a closed list without `death_certificate`. (3) On-demand async UIs need stale-response guards (disable criteria while loading). (4) Webhook secrets in JSON-column configs leak via wholesale responses — redact on read.

**Remaining (post-milestone, optional):** Snohomish tax scraper (best non-King candidate per the spike); native per-dialer connectors (demand-gated); BridgeLeads-side DNC scrubbing (needs a DNC data source — currently the dialer scrubs). See `[[project_lead_targeting_milestone]]`.

---

## 2026-06-05 — Phase 4 tax-filter UI (frontend) + Phase 3-5 security hardening

**Frontend (branch `feature/phase4-tax-filters-ui` in sibling repo `bridgeleads-web`, UNMERGED/UNDEPLOYED, tsc clean, Codex-clean):** tax-delinquent filter UI on the results view (`app/(dashboard)/results/[id]/page.tsx`). Amount-owed + months-delinquent min/max inputs (debounced) wired to the params the backend already accepts (get_results + download + export-url via `lib/api.ts`). Gated on the **presence of structured tax data** (latched `hasTaxData` = the King-tax gate, since `delinquent_amount` is null elsewhere) so it survives a too-narrow filter returning 0 rows. Codex caught: filtered-empty showed the "all duplicates" notice (gated it on `!taxFilterActive && !search` + a filter-specific empty message); export honors tax filters but not search (deliberate: filters = lead-selection controls in the deliverable, search = view-only find — documented). ESLint not configured in that repo; tsc is the gate.

**Security hardening (branch merged to main `8e1586f`, no migration):** ran a **Codex adversarial security pass** over the whole milestone (`b78d698..main`). CLEAN: tenant isolation (segments/tax/dialer all user_id-scoped), SQL injection (params bound; county_clause a fixed toggle), CSV injection (sanitized/numeric), SSRF (validate_outbound_webhook + redirects off + redacted), PII-in-logs (host-only + response redacted). Fixed 3 findings:
- **Medium** — unbounded `min/max_months` produced an out-of-int4 `bill_year` bound → Postgres "integer out of range" / log churn (cheap DoS). Added `le=1200` (months) + `le=100_000_000` (amount) on get_results + download_export + export-url.
- **Medium** — dialer sweep joined ScraperConfig by id only (DB doesn't enforce job.user_id==config.user_id; sweep is a system session that bypasses RLS) → added `ScraperConfig.user_id == Job.user_id` owner-match (defense-in-depth vs cross-tenant PII push).
- **Low** (pre-existing, P5 widened) — config responses echoed `webhook_secret`/`dialer_webhook_secret` → made WRITE-ONLY in `ScraperConfigResponse` (presence flags `*_secret_set`; secrets popped). +3 regression tests. Deploy healthy (200).

**Pending / Handoff:** **deploy decision for the frontend** (push `feature/phase4-tax-filters-ui` → master = Vercel auto-deploy); remaining phase UIs (2b doc-type, 3 segments [design review first], 5 dialer settings); non-King tax data spike; run offline backfills (property_key, membership, tax_fields). See `[[project_lead_targeting_milestone]]`.

**Facts learned:** the "filtered total==0 ≠ empty job/all-duplicates" trap recurs in BOTH backend (previous-job suggestion) and frontend (empty-state notice) whenever a filter changes `total` — audit empty-state logic on every new filter. Secrets in JSON-column config dicts get echoed by wholesale `deliver` responses — redact on read.

---

## 2026-06-05 — Lead Targeting Phase 5 (5B): generic "push to any dialer" (Enzo dropped)

**Decision:** dropped Enzo as the integration target (newest vendor, no public API/pricing/reviews = worst first integration; web-researched). Built a **vendor-agnostic push** instead — works with any dialer via its inbound webhook / Zapier catch-hook. Zero lock-in; matches the PRD's "integrate, don't build a dialer" stance.

**Built (branch `feature/phase5-dialer`, UNMERGED, commit `c8b3ed9`, 107 tests pass, Codex CLEAN after 8 review rounds):**
- `DeliverConfig.dialer_webhook_url` + `dialer_webhook_secret` (separate from the job-summary webhook; shared https/secret validators extracted; gated Business+ in `create_scraper` alongside `webhook_url`).
- `webhook_delivery.build_dialer_push_payload`: event `leads.dialer_ready`, `schema_version`, stable `batch.id` + per-lead `external_id` (retry-safe consumer dedup), `dnc_scrubbed:false` + per-lead `dnc_status`, flattened scraper fields, `lead_count`/`total_dialer_ready_count`/`truncated`, HMAC-signed, cap 500. `deliver_job_webhook` reused as the transport (SSRF re-validate, HMAC, retry, non-fatal) + now **redacts the receiver response body for dialer events** (PII echo risk).
- **DEFERRED trigger** (`scheduler.dialer_push_sweep`, beat every 5 min, migration **039** `Job.dialer_pushed_at`): pushes a job's dialer-ready leads only once its **async skip-trace has SETTLED**, claimed durably **before** publish (at-most-once).

**Codex caught (8 rounds — async + TCPA are subtle):**
- P1: push at scrape completion missed async skip-trace leads → moved to a settled-gated sweep.
- P1: strict `phone_dnc_flag IS FALSE` matched NOTHING (Tracerfy leaves DNC NULL) → use not-known-DNC + honest `dnc_status`/`dnc_scrubbed:false` labeling; dialer does the authoritative scrub.
- P2s: plan gate for dialer URL; `FOR UPDATE`/atomic-claim race; exclude `is_duplicate`; settle on the pending QUEUE not `Result.skip_trace_status` (errored rows leave Result stuck); time-bound only **submitted** rows (queued = backlog, never age out); re-check entitlement at push time (downgrade); claim durable before publish; redact response PII.

**⚠️ COMPLIANCE — decision for the user (surfaced, not silently decided):** BridgeLeads has **no DNC data feed** (`phone_dnc_flag` is always NULL). So "dialer-ready" = valid phone + not-KNOWN-DNC, and the **receiving dialer is the DNC/TCPA compliance layer** (industry standard; the payload says `dnc_scrubbed:false`). If BridgeLeads-side DNC scrubbing is required, that's a separate feature needing a DNC data source.

**Pending / Handoff:** merge Phase 5 (migration 039 deploy-order note like 038); "push to dialer" config UI (frontend); optional native per-dialer connectors (CallTools/BatchDialer/PhoneBurner) only on real demand + API docs; run all offline backfills. See `[[project_lead_targeting_milestone]]`.

**Facts learned:** (1) async skip-trace means any "use the phone" feature must trigger AFTER skip-trace settles, not at scrape completion. (2) The system never populates DNC — strict DNC filters silently match nothing. (3) Reviewer oscillation (strict-vs-functional DNC) was the signal that the real issue was a missing data source / product decision, not a code bug.

---

## 2026-06-05 — Lead Targeting Phase 5 (dialer-ready foundation) + Phase 4 merged to prod

**Shipped to prod:** Phase 4 (King tax filters) merged to main + pushed (`76c9e77`), deploy healthy (health 200), migration 038 applied on boot. Run `backfill_result_tax_fields.py` offline (pending).

**Built Phase 5 foundation (branch `feature/phase5-dialer` off main, UNMERGED, 4 tests / 72 total pass, Codex CLEAN first pass):** the Enzo-INDEPENDENT half of "push leads into a dialer".
- **5A `2609ddb`** — `src/api/dialer_filters.py` (pure): `dialer_ready_conditions(include_unknown_dnc=False)` → valid phone (`phone IS NOT NULL AND trim<>''`) + **TCPA-safe DNC (`phone_dnc_flag IS FALSE` — unknown/NULL excluded, per FTC TSR)**. `include_unknown_dnc=True` gives the looser "candidate" set (`IS NOT TRUE`). Provenance-agnostic (NO skip_trace gate — a valid phone from any source qualifies; the future Enzo task can add `='hit'`). Exposed as `dialer_ready=true` view/export param on `get_results` + `download_export` + `export-url` (threaded through the in-app flow proactively, applying the 4B lesson). Users can export dialer-ready CSVs to ANY dialer today (matches the PRD "integrate, don't build a dialer" stance).

**Tried / Decided:** Codex consult confirmed: ship the dialer-ready SELECTION now (real, valuable, Enzo-independent), but do NOT build Enzo tables/DTOs/fake clients/tasks — speculative without the API docs. Reuse `webhook_delivery.py`'s outbound pattern (SSRF allowlist, HMAC, Celery retry) as the connector model, but Enzo needs a dedicated connector, not a generic webhook. DNC: chose the compliance-SAFE default (`IS FALSE`) over including unknown-DNC — TCPA non-negotiable; looser "candidate" mode is an explicit opt-in (function supports it; API exposes only the safe default for now).

**BLOCKED — Slice 5B (the actual Enzo connector):** spec says Enzo API docs/credentials are "supplied at Phase 5" — NOT provided. Cannot build a real connector against an unknown API (no mock code). Need from user: base URL + env, auth (key/OAuth/HMAC/bearer + refresh), endpoint(s) (create/update contact, add to list/campaign, bulk import), payload schema (required fields, phone format, lead IDs), rate limits + batching, idempotency/upsert (external ID, dup handling), DNC/consent source of truth (Enzo vs us), campaign/list model, error contract (retryable vs terminal), audit/PII-redaction/retention, status callback.

**Pending / Handoff:** merge Phase 5 foundation; obtain Enzo API docs/creds → build 5B connector + push task; "push to dialer" delivery option (UI = frontend); run `backfill_result_tax_fields.py` offline. Earlier milestone follow-ups still open: P3/P4 UI (frontend), non-King tax data spike, Phase-1/3 backfills offline. See `[[project_lead_targeting_milestone]]`.

**Facts learned:** (1) TCPA/FTC TSR: "dialer-ready" must mean DNC-confirmed-FALSE, not merely not-known-DNC — unknown DNC is not callable. (2) The view/export filter pattern (params on get_results + download_export + export-url, empty≠404, gate previous-job suggestion) is now reused 3×; it's the project's standard "filter what's shown/exported" shape.

---

## 2026-06-05 — Lead Targeting Phase 4 (King tax filters) + Phase 3 merged to prod

**Shipped to prod:** Phase 3 (combine/overlap) merged to main + pushed (`827040c`), deploy healthy (api.bridgeleads.io/health 200), migration 037 applied on boot. Both backfills (`backfill_result_property_key.py`, `backfill_property_membership.py`) still to run offline.

**Built Phase 4 backend (branch `feature/phase4-tax-filters` off main, UNMERGED, 29 Phase-4 tests / 68 total pass, Codex CLEAN):** filter `tax_delinquent` leads by amount owed + time delinquent. KING FIRST (only King's Socrata feed has structured $ + tax year).
- **4A `7f6f88a`** — migration **038**: `results.delinquent_amount NUMERIC(12,2)` + `delinquent_bill_year INTEGER` + 2 partial indexes. `_extract_tax_fields` (workers/tasks.py): SOURCE-GATED (King tax_delinquent only), coerced (`Decimal(str(v))`, quantized, reject negative/NaN/absurd; bill_year 1900..now+1) — every non-King row stays NULL. Populated at insert; offline `backfill_result_tax_fields.py` reuses the same extractor.
- **4B `b9c048b`→`f86f6e0`** — VIEW/EXPORT filter (user chose option B: no billing change). `src/api/tax_filters.py` (pure): months↔bill_year math (King bills ~01/01/year → derive months at query time, never stale) + SQLAlchemy predicates (NULL structured rows never match a set filter). Wired into `get_results` (view), `download_export` (export), and `export-url` (carries params through the in-app flow). `delinquent_amount`/`delinquent_bill_year` surfaced in `ResultRow` + CSV.

**Tried / Decided:** Codex consult recommended shipping 4A + option-B view-filter FIRST, deferring scrape-time filtering + the post-filter billing redesign (option A, the spec's eventual goal, HIGH risk). User confirmed option B. Stored `bill_year` (stable), not a volatile "months" value. Source-gated extraction (not "if keys present") so a future scraper reusing those key names can't silently poison the filter columns.

**Caught & fixed (Codex reviewed every commit — 4 review rounds on 4B):**
- 4A [P2]: worker writes 038 columns but workers don't run migrations → deploy-order race. DOCUMENTED (not coded around): same pattern as Phase 2a `doc_type`, self-healing via Celery `max_retries=3`. Merge-time note: API applies 038 before workers steady-state.
- 4B [P2]: empty FILTERED export returned header-CSV even for a genuinely-empty job → added unfiltered existence check (404 preserved for empty job, header-CSV only when rows exist but none match).
- 4B [P2]: `export-url` (in-app flow) dropped the filter params → unfiltered download. Threaded params through.
- 4B [P3]: filtered `total==0` triggered the "previous job" empty-scrape suggestion → gated off when a tax filter is active.

**Failed / Blocked:** non-King tax sourcing (Pierce/Snohomish/Kitsap have NO structured amount/age — recorder keyword matches only) is a separate research spike, NOT done. Scrape-time filter + billing redesign deferred.

**Pending / Handoff:** merge Phase 4 to main (then run `backfill_result_tax_fields.py` offline; deploy-order note applies); tax-filter UI (frontend `bridgeleads-web`); non-King tax data spike; Phase 5 (Enzo dialer push). See `[[project_lead_targeting_milestone]]`.

**Facts learned:** (1) workers skip migrations (`start.sh`) → any new column the worker writes is subject to a deploy-window race healed by Celery retry. (2) For King tax, "months delinquent" is a derived product metric off `bill_year` (bills issue ~Jan 1), not tax-law truth — don't overclaim exact duration. (3) View-filters that change `total` can leak into downstream empty-state logic (previous-job suggestion) — audit those when adding filters.

---

## 2026-06-05 — Lead Targeting Phase 3 (slice 3C): inclusive UNION ("combine") export

**Built (branch `feature/phase3-combine-overlap`, commit `6e42182`, 13 new tests; 44 Phase-3 tests pass):** the other half of combine/overlap — merge selected record-type lists into ONE deduped export, NEVER dropping weak leads.
- `POST /segments/union` (JSON preview) + `/union/export` (CSV with `identity_strength` column). 1+ distinct types (union of one list still dedupes it across counties/jobs).
- **Dedup bucket** `COALESCE(property_key, dedup_hash, 'id:'||id)`: strong rows dedupe by property_key, weak by dedup_hash (name|date), rows with neither stand alone. NO `pk:`/`dh:` prefix — so an un-backfilled strong row (whose strong hash still lives in dedup_hash) coalesces by hash VALUE with a backfilled row (Codex). `identity_strength` per-bucket via `bool_or(property_key IS NOT NULL)`.
- **Ranking** (Codex P2): contactable FIRST → is_duplicate → recent job → id. Contactable-first so a bucket never drops an available phone/email by preferring an older non-duplicate row over a newer skip-traced duplicate; matches intersection. NEVER filters is_duplicate=false (would drop a lead whose only row is a dup).
- No membership (union = direct per-user results scan by record_type). Explicit `j.user_id`/`sc.user_id` tenant predicates (retro-added to intersection too). `sanitize_for_csv` all fields incl identity_strength.

**Codex (consult + 2 reviews):** consult shaped the bucket/ranking design; review caught the is_duplicate-vs-contactable ordering (reversed it). Final review CLEAN. Notably Codex's diff-review reversed its own consult advice ("is_duplicate first") once it reasoned through the skip-traced-duplicate scenario — took the better take.

**Documented caveats (not hidden — Codex):** (1) dedup_hash is PRE-enrichment, property_key POST — un-backfilled strong rows whose enrichment changed parcel/addr can split/mislabel until `backfill_result_property_key.py` runs (forward rows always correct; backfill is a precondition for full accuracy). (2) weak dedup is name|date, not property identity → weak buckets merge same-name/date leads across types (intended, mirrors existing system dedup); overlap_count not overclaimed for weak rows. (3) county filter is county-only (WA-only data today; county names collide across states — revisit if multi-state).

**Pending:** segment-builder UI (frontend repo); saved `Segment` + scheduled delivery (Phase 5); optional `(user_id, dedup_hash) WHERE dedup_hash IS NOT NULL` index if heavy-user union scans surface. Migration 037 still branch-only. See `[[project_lead_targeting_milestone]]`.

---

## 2026-06-04 — Lead Targeting Phase 3 (first slice): combine/overlap — intersection export

**Built (branch `feature/phase3-combine-overlap`, 3 commits, 31 no-DB tests pass):** the "on both lists" feature — properties a user has on 2+ record-type lists (e.g. probate ∩ pre_foreclosure), the highest-motivation sellers.
- **3A `d2513dc`** — `Result.property_key` join key. Migration **037** (additive nullable + PARTIAL index `(user_id, property_key) WHERE property_key IS NOT NULL`). `_write_result_property_keys` stamps the strong-identity key (reuses `compute_property_key`) on a job's post-enrichment rows, BEFORE the membership upsert, in its OWN isolated txn (bulk `UPDATE … FROM (VALUES …)` by id, idempotent via `property_key IS NULL`). Offline `scripts/backfill_result_property_key.py` (keyset by id, all computable rows incl is_duplicate).
- **3B `fa132c1`** — `POST /segments/intersection` (JSON preview, cap 500) + `/export` (CSV, cap 50k). Overlap computed IN-SQL from `property_list_membership` (indexed Phase 1 rollup) as a subquery; 3 CTEs (candidates → agg(`array_agg DISTINCT` + `count DISTINCT`, NOT a window aggregate in PG) → ranked(`row_number`: contactable→recent-job→id)) → one representative row/property + `matched_record_types` + `overlap_count`. Strong-identity only and SAYS SO (`identity_strength="strong"`). Tenant-scoped (RLS + explicit `user_id`), `sanitize_for_csv` all fields, rate-limited.

**Tried / Decided:** Followed the committed Codex-reviewed design (use membership as the indexed overlap source, not a results self-join). Codex plan-consult shaped 3A: bulk UPDATE (NOT ORM attribute-set — autoflush could push writes early / poison the shared session before the membership commit), key-write before membership (so 3B never sees overlap w/o joinable rows), partial index, backfill ALL rows. Rejected `CREATE INDEX CONCURRENTLY` (can't run in the advisory-lock migrate txn; results ~277K = sub-second plain index, matches 033/034 precedent). Replaced a closed `SUPPORTED_RECORD_TYPES` enum with shape validation — record types are DB-driven/open-ended, matching the existing `bound_record_types` convention.

**Caught & fixed (Codex reviewed every commit):**
- 3A [P2]: backfill seeded `last_id=""` for `WHERE id > :last_id` but `results.id` is UUID → Postgres `invalid input syntax for type uuid: ""` crashes the first query. Fixed: nil-UUID seed + `CAST(:last_id AS uuid)`.
- **Same latent bug in the Phase 1 twin** `backfill_property_membership.py` → fixed in `a68dbbf`.
- 3B [P2×2]: (a) county filter could return a property only on ONE list in-scope as an "intersection" → added `agg HAVING count(DISTINCT record_type)=:n`; (b) all overlap keys materialized into Python before LIMIT applied → pushed overlap into a membership subquery (no key array, LIMIT bounds in-query).
- 3B [P2]: closed enum rejected `death_certificate` (real King type) → shape validation.
- Final Codex review: CLEAN ("tenant-scoped, validated, and bounded as intended").

**Failed / Blocked:** No local test DB / Playwright (standing constraint) → window-function ranking + the results↔membership join correctness are verified by Codex + unit tests + deferred to CI roundtrip, not run here.

**Pending / Handoff:** inclusive UNION export (strong+weak rows, `identity_strength` column); segment-builder UI (frontend repo `bridgeleads-web`); saved `Segment` model + scheduled combined delivery (Phase 5). **Migration 037 is branch-only — do NOT apply to prod until merged to main** (migration/branch landmine). Run the two backfills offline post-merge.

**Facts learned:** (1) Postgres does NOT allow `array_agg(DISTINCT …) OVER (…)` — DISTINCT window aggregates are unimplemented; split into a GROUP BY agg CTE. (2) UUID keyset pagination must seed the nil UUID, never `""`. (3) Record types are DB-driven (`county_connectors.record_types`), so `death_certificate` and future slugs exist beyond CLAUDE.md's documented 6 — never hardcode a closed set. See `[[project_lead_targeting_milestone]]`.

---

## 2026-06-04 — Lead Targeting Phase 2b (backend): choose pre-foreclosure doc type

**Built (branch `feature/phase2b-doc-type-select`, UNMERGED, 28 no-DB tests pass):** Users can select which pre-foreclosure document(s) a config scrapes.
- Capability registry `src/scrapers/doc_types.py` — SINGLE SOURCE OF TRUTH: canonical vocab, per-county availability (fail-closed), normalize, validate_selection, canonical_tokens_for (all-or-nothing), selectable_availability.
- `scraper_configs.doc_types` JSON col (migration **036**, additive nullable).
- Create-route validation via registry (NOT Pydantic): pre_foreclosure-only, available-only, `[]`=422, `None` ok, rejects hidden EagleWeb counties.
- King/Pierce constructors honor selection (`canonical_tokens_for` → search_text / checkbox-id subset), plumbed through `_run_scraper` constructor introspection.
- `/connectors` exposes `pre_foreclosure_doc_types` (King/Pierce only).

**THE invariant (Codex):** `doc_types=None` = today's EXACT output (King NOTS, Pierce ALL 4, EagleWeb unchanged). `_run_scraper` never passes doc_types when None → zero shrink for existing users. Selection only narrows. Verified by construct-level tests (Pierce None→4 ids, NOD+LisPendens→[187,146]).

**Decided:** constructor-param plumbing (not a new scrape() contract); EagleWeb kept `supported_for_selection=False` (hidden) until per-county coverage verified (Codex: don't assume 16 counties share one truth); no update endpoint exists so validation is create-time + defensive all-or-nothing at scrape-time.

**Caught & fixed (Codex full-diff PASS, no P1):** [P2] validate_selection didn't reject hidden counties → now does; [P2] canonical_tokens_for partial-narrowed on stale/unmapped types → now all-or-nothing (falls back to legacy). Both re-confirmed PASS.

**Pending:** Task 7 = doc-type selector UI in frontend repo `bridgeleads-web` (separate, unverifiable here). EagleWeb selection (hidden). Pierce per-record doc_type capture (from 2a, needs live ARMS run). See `[[project_lead_targeting_milestone]]`.

---

## 2026-06-04 — Lead Targeting Phase 2a: surface pre-foreclosure doc_type

**Built (branch `feature/phase2-doc-type`, UNMERGED):** Real `results.doc_type` column (migration **035**, additive nullable, offline-render verified). Carried end-to-end: worker bulk insert, BOTH worker exports + `_COLUMN_ORDER`, **and the live `/jobs/{id}/download` CSV** (which rebuilds from DB, not the stored file — Codex caught this; I'd wrongly assumed it streamed R2). Added `doc_type` to API `ResultRow`. EagleWeb now captures the matched `desc` as `record.doc_type` (was dropped). Commits `c3446fc`..`23322f0` + P1 fix `<download>`.

**Decided (Codex, 584k+848k tokens):** real column not JSON; old rows NULL (no backfill — `CountyRecord.doc_type` isn't safely keyed to `results`); EagleWeb capture placed after filter `continue`s so it applies to matched + `all` paths.

**Failed/Blocked:** Pierce per-record doc_type **DEFERRED** — its `_map_row` can't identify the ARMS doc-type column without live fixture validation; faking it is worse than NULL. No test DB/Playwright here, so DB-roundtrip + live scraper unverified locally (Codex oracle: doc_type flows correctly end-to-end; CI to confirm).

**Caught & fixed:** [P1] `/download` hardcoded fieldnames omitted doc_type (live CSV, separate from worker export) — added to fieldnames+writerow (sanitized); Codex re-confirmed PASS.

**Pending:** Pierce capture (live run); **Phase 2b** = user doc-type selection + code-level capability registry (single source of truth, NOT duplicated into county_connectors) + per-county availability/confidence + `ScrapeOptions` plumbing + King-NOD-hidden + defaults + UI. See `[[project_lead_targeting_milestone]]`.

---

## 2026-06-04 — Lead Targeting milestone: Phase 1 (property membership foundation)

**Context:** User requested 4 features for King/Pierce/Snohomish/Kitsap: (1) tax filters by amount
owed + months delinquent, (2) pre-foreclosure doc-type control (NOD>NOTS>Lis Pendens), (3) automate
scrape→skip-trace→Enzo dialer, (4) combine lists (union) + overlap/intersection (probate ∩
pre-foreclosure). Brainstormed, brought Codex in heavily, decomposed into a **5-phase milestone**.

**Built / Shipped (branch `feature/lead-targeting-delivery`, UNMERGED, no prod contact):**
- Spec `docs/superpowers/specs/2026-06-04-lead-targeting-delivery-design.md` + Phase-1 plan
  `docs/superpowers/plans/2026-06-04-phase1-property-membership.md`.
- **Phase 1 code** (8 task commits `a77630a`..`7d49724`, fixes `5fbfc69`+docstring): new
  `src/workers/property_identity.py` (shared strong-identity hash), `_compute_dedup_hash` refactored
  to use it (behavior-preserving, lockstep test), `PropertyListMembership` model + migration **034**
  (schema-only + RLS USING policy, app-readable→registered across all RLS cutover scripts modeled on
  `results`), `_upsert_property_membership` in `tasks.py` (post-enrichment, pre-aggregated upsert,
  pgcode retry, billing path untouched), `membership_query.users_overlap`, purge retention,
  `scripts/backfill_property_membership.py` (offline best-effort).

**Tried / Decided:** Overlap identity must be **post-enrichment + strong-only** (parcel/address) or
probate (name-keyed) never matches pre-foreclosure (parcel-keyed) — the flagship case. Normalized
table keyed (user_id, record_type, property_key) — no bitmask/JSON (a job is one record_type).
`is_duplicate`/`delivered_records` left untouched; membership additive + isolated from billing.

**Failed / Blocked:** No safe test DB locally (`.env` = PRODUCTION Supabase, Docker not running).
Per user, built all code + ran only no-DB checks (9 pure unit tests pass, py_compile/ruff clean);
**DB-backed tests (`tests/test_property_membership.py`) + migration 034 must run in CI / a dedicated
test DB — NOT applied to prod, NOT merged.**

**Caught & fixed (Codex, 4 deep passes ~3M tokens):** is_duplicate hiding overlap; pre-enrichment
identity miss; `ON CONFLICT` double-affect (pre-aggregate); psycopg2 `pgcode` not `sqlstate`;
function-local `sa_text` NameError; RLS grants incomplete (app SELECT + system DELETE); refetch
failure overwriting export with empty file (P1); membership failure poisoning the session before
`done` (P1); backfill idempotency claim; `users_overlap` dedupe.

**Pending / Handoff:** Run DB tests + migration 034 in CI/test DB → merge to `main` → apply
migration via `scripts/migrate.py` → run backfill manually. Then Phase 2 (doc-type, first UI).
See `[[project_lead_targeting_milestone]]` memory.

**Facts learned:** `deps.get_rls_db` binds tenant via `set_config('app.current_user_id', :uid, true)`;
`delivered_records` is worker-only RLS but membership is app-readable (modeled on `results`).

---

## 2026-06-03 — Live all-county scraper audit + 2 fixes (cowlitz, spokane)

**Context:** Asked to live-test every county over a 3-month window, one by one, driven through the
real bridgeleads.io UI with visible Playwright Chromium, fix any failures, Codex verifying each.

**Built / Shipped:**
- Audit harness (`scripts/`, untracked): `ui_county_audit.py` (drives the real UI wizard
  state→county→record-type→Continue×3→Test run→/live, polls API for completion; `--resume`),
  `saas_county_audit.py` (API path, authoritative), `live_county_audit.py` (local visible-Chromium,
  subprocess-per-combo), plus `probe_cowlitz_live.py` / `probe_spokane_dump.py` /
  `probe_spokane_formsubmit.py` diagnostics.
- **cowlitz fix** (`693e563`, `src/scrapers/templates/laserfiche_weblink.py`): poll ~30s for the
  Laserfiche "N Results" count instead of one early read. Was 0 → 44 records (local-verified).
- **spokane fix** (`b2dabd0`, `src/scrapers/templates/eagleweb.py`): `form.submit()` fallback that
  fires only while stuck on `docSearchPOST.jsp`; primary click timeout 120s→30s; early poll-break.
  Jefferson no-regression (128 records via normal click path).

**Result:** 23 PASS (King ×5, Pierce ×4, Clark, Skagit, Kitsap, Okanogan, Island 154, Jefferson 116,
Grant, Douglas, Clallam, Thurston).

**Tried / Decided:** Started building a local visible-Chromium audit, then user clarified "live
chromium" = the engine ON the SaaS → pivoted to driving the real UI + real Railway jobs. Score
PASS on results `total` (incl. dedup rows), NOT `record_count`(new), or already-scraped windows
read as false EMPTY.

**Failed / Blocked:**
- **⚠️ SPOKANE = Cloudflare bot protection.** `recording.spokanecounty.org` intermittently serves a
  "Performing security verification" interstitial. The submit fix recovers unblocked chunks but
  Cloudflare is the deeper blocker — NOT solved. Deliberately did not build bot-evasion. Needs a
  pacing/proxy strategy or accept partial coverage.
- Codex's root-cause hypotheses were WRONG twice (cowlitz column-offset; spokane volume). Live repro
  with visible Chromium refuted both — reproduce before trusting a hypothesis.

**Caught & fixed (in review before shipping):** Codex review of the harness fixed 4 issues
(resume dedup, WA-option check, coverage sentinel, job timeout). Codex review of the eagleweb fix →
hardened the fallback's form guard (only fire on the POST/search page, never an unrelated form).

**Pending / Handoff:** Prod re-verify of cowlitz/spokane post-deploy (in progress). 7 EMPTYs
(pre_foreclosure/secondary types in small counties — likely genuinely empty, unverified). whatcom
flaky-but-functional. UI harness: NextAuth session drops on long runs (API re-login likely
invalidates the shared admin session) → caused thurston/whatcom UI false-errors.

**Facts learned:** Laserfiche results are an async PrimeFaces datatable (read count AFTER it loads).
EagleWeb: click-submit can stick on docSearchPOST.jsp; form.submit() follows the redirect. Spokane
is Cloudflare-gated. Degraded-health counties aren't clickable in the UI wizard (healthy-only).

---

## 2026-06-03 — Migration boot-race fixed: advisory lock serializes Alembic across API replicas (commit 48e5482)

**Context:** Resumed after a laptop power-loss mid-session (prior chat context gone). Reconstructed
state from git/journal/memory — nothing lost: `main` was clean and in sync, the migration-033
cherry-pick (`a3681cc`) was already committed, pushed, and deployed. Asked to verify deploy health.

**Built / Shipped:** A Postgres-advisory-lock wrapper that serializes `alembic upgrade head` across
the multiple Railway `api` replicas.
- `scripts/migrate.py` (NEW) — acquires a session-level `pg_try_advisory_lock(0x424C, 1)` and runs
  Alembic **in-process on the SAME connection** via `cfg.attributes["connection"]`. URL is validated
  session-capable (rejects the Supabase `:6543` transaction pooler). Bounded jittered wait (900s),
  fail-closed on timeout. `48e5482`
- `alembic/env.py` — honors `config.attributes["connection"]` (shared-connection recipe); bare
  `alembic` CLI still works via the engine fallback.
- `start.sh` — API branch runs `python scripts/migrate.py` instead of bare `alembic upgrade head`.

**The bug it fixes (confirmed live in prod logs):** the `api` service runs MULTIPLE replicas and
rolling deploys overlap; both run migrations on boot. On the 032→033 deploy two replicas raced the
same revision — one won, the loser's `UPDATE alembic_version WHERE version='032'` matched 0 rows →
`ERROR ... expected to match one row ... 0 found` → `FAILED ... refusing to start API`. Self-healed
only by Railway retry + transactional DDL. **Migration 033 uses `CREATE INDEX CONCURRENTLY` in an
`autocommit_block`** — the non-atomic case where a racing replica can leave a half-built INVALID index.

**Proof it works:** post-deploy `api` logs show one replica `migrate: lock acquired` while the other
logs `migrate: migration lock held by another replica; waiting...` then proceeds after release. Zero
`0 found`, zero `FAILED`. Both booted to `Uvicorn running`; `/health` → 200.

**Tried / Decided:** Consulted Codex on whether this was worth fixing — both AIs agreed
safe-now-but-fragile; advisory lock = best effort/payoff vs a single-run release step (Railway has no
native release phase, deferred as the cleaner long-term fix). Chose the two-int `(classid, objid)` lock
form so it cannot collide with `daily_scrape.py`'s single-bigint per-county locks.

**Caught & fixed (Codex, 2 rounds — both Highs were in MY first draft):**
- HIGH: original `:6543→:5432` string-replace was not a safe "force direct" contract — on Supabase,
  pooler vs direct differ by HOST not just port. Replaced with explicit session-capability validation
  (unit-tested 7 URL shapes).
- HIGH: original draft held the lock on a parent connection while alembic ran in a **subprocess** on a
  different connection → a dropped lock connection orphans the lock mid-migration. Fixed by running
  alembic in-process on the lock-holding connection.
- MED: "migrations stay atomic" comment was over-broad (033's `autocommit_block` is intentionally
  non-atomic). Corrected.

**Pending / Handoff:** (Low) move migrations to a single release/deploy step instead of
every-API-replica-on-boot; (Low) bare `alembic` CLI bypasses the lock + URL validation — don't run
manual migrations against `:6543`. Other open threads untouched: `security/redteam-remediation-2026-06-01`
(19 commits, unmerged), HIGH-2 RLS cutover.

**Facts learned:** prod `DATABASE_URL_MIGRATE` = `aws-0-us-west-2.pooler.supabase.com:5432` (Supavisor
SESSION mode) — already set, so migrations run session-mode (advisory-lock safe); `DATABASE_URL` (async)
is the `:6543` transaction pooler. Session-level advisory locks are UNSAFE through transaction pooling.
A session advisory lock survives `commit()` and Alembic's per-migration transactions on the same
connection, and auto-releases when the connection/process dies (crash-safe). Codex CLI session resume
(`codex exec resume <id>`) did NOT persist here (`thread not found`) — start a fresh consult instead.

---

## 2026-06-02 — RLS cutover: Codex HOLISTIC review caught a ship blocker → restructured (commit 3225778)

**The catch:** after all phases were committed, a final cross-phase Codex review (the kind per-phase
review can't do) found that migrations 030/031 (role-targeted policies + FORCE) would **no-op on the
first post-merge `alembic upgrade head`** (cutover roles don't exist yet), advance `alembic_version`,
and then **never re-run when the roles are actually provisioned** — silently skipping the entire policy
install. Root cause: role-dependent DDL doesn't belong in Alembic's one-shot chain.

**Fix (Codex blueprint):** moved cutover DDL out of Alembic into idempotent operator scripts.
- 030/031 → no-op placeholders (chain intact).
- `scripts/apply_rls_cutover_policies.sql` (NEW) — role-targeted policies, hard-fail unless both roles,
  029 binding backfill, transactional, idempotent.
- `scripts/apply_rls_force.sql` (NEW) — FORCE, hard-fail unless policies converged + owner BYPASSRLS.

**6 more findings fixed in the same pass:** referral_events app SELECT-only (grant+policy; write is via
the definer fn); delivered_records/pending/queues system-only (grant/policy aligned); provision REVOKEs +
verify block (idempotent convergence vs prior over-grants); password_history app SELECT+INSERT not FOR ALL
(immutable audit rows); FORCE convergence check verifies every table's system policy; worker boot warms
public_sample_cache; corrected the false "inert under BYPASSRLS" claim (the route CODE is active today —
only the policies are inert). Codex final: SHIP-READY.

**Lesson:** per-phase Codex review APPROVED every piece; only the holistic "review the whole diff for
cross-phase gaps" pass caught the migration-consumption blocker. Worth doing on any multi-migration change.

**Canonical cutover order:** `alembic upgrade head` → `provision_rls_roles.sql` →
`apply_rls_cutover_policies.sql` → repoint connections (staging, RLS_ENFORCE=False) → verify →
RLS_ENFORCE=True → `apply_rls_force.sql`. All operational; no more code.

---

## 2026-06-02 — RLS cutover CODE COMPLETE: Phases 2c→4 (policies, repoint, FORCE)

**Built / Shipped (continuing the cutover):**
- **Phase 2c** (`40497ce`): migration 030 — role-targeted policies. Drops the untargeted tenant
  policies; adds `<t>_app TO bridgeleads_app` (tenant GUC) + `<t>_system FOR ALL TO bridgeleads_system`
  on every table. referral_events app=SELECT-only (writes via the definer fn). users/county_connectors
  broad app + system. county_records app shared-read + system all. skip_trace_* system-only. Python
  role-guard: no-op if neither role exists (CI), RAISE if exactly one, swap if both. Backfills 029
  bindings. anon/authenticated default-denied.
- **Phase 2d** (`51655ca`): `test_rls_role_policies.py` — SET LOCAL ROLE bridgeleads_app tenant
  isolation + bridgeleads_system cross-tenant; skips unless the cutover is applied.
- **Phase 3** (`a268fd1`): `alembic/env.py` prefers `DATABASE_URL_MIGRATE` (owner/DDL), falls back to
  `DATABASE_URL_SYNC`; `settings.DATABASE_URL_MIGRATE` added.
- **Phase 4** (`9893633`): migration 031 — FORCE ROW LEVEL SECURITY on 16 tables, gated on both roles
  existing + a guard that RAISEs unless the 029 SECURITY DEFINER function owners carry BYPASSRLS.

**Caught & fixed (Codex):** 2c — backfill 029 bindings (roles may be provisioned after 029) + downgrade
idempotency + restore 025 WITH CHECK. 4 — exact-function guard via `to_regprocedure` (bare proname+LIMIT 1
could match wrong overload) + ungated downgrade.

**Decided:** referral_events app SELECT-only once writes moved to the definer fn (vs Codex's earlier
asymmetric-WITH-CHECK, which assumed direct app write). FORCE shipped as an audited migration (not
manual-only) after the owner-bypass guard — it adds little since app/system aren't table owners, but is
harmless defence-in-depth.

**Pending / Handoff — NO MORE CODE.** All 11 commits authored + Codex-reviewed (e5d50e8→9893633).
Remaining = OPERATIONAL per `docs/security/RLS-CUTOVER-RUNBOOK.md`: (1) `scripts/provision_rls_roles.sql`
+ add `DATABASE_URL_MIGRATE` to `.env.example`; (2) deploy staging, run migrations 029/030, repoint
connections, verify with `RLS_ENFORCE=False`; (3) staging `RLS_ENFORCE=True` + E2E; (4) prod: flip
`RLS_ENFORCE=True`, run migration 031 (FORCE) — `postgres` owner keeps BYPASSRLS so the definer fns
survive. Everything inert under today's BYPASSRLS role; nothing deployed.

---

## 2026-06-02 — RLS least-privilege cutover: Phases 0→2b executed (6 commits, Codex-gated)

**Built / Shipped (branch `security/redteam-remediation-2026-06-01`):**
- **Phase 0** (`e5d50e8`): `scripts/provision_rls_roles.sql` (3-role model: app SELECT/INSERT/UPDATE no-DELETE,
  system +DELETE on county_records only, owner=DDL) + `RLS-CUTOVER-RUNBOOK.md`. Idempotent, txn-wrapped,
  password-on-create-only, DDL fail-fast (RAISE not `\quit`). Codex: 2 rounds, 6 findings fixed.
- **Phase 1** (`a27ff9f`): `after_begin` listener in `session.py` reapplies `app.current_user_id` every
  transaction (gated on `session.info['rls_user_id']`) so the worker's mid-task commit doesn't strip RLS
  context under NOBYPASSRLS. `deps.py`+`jobs.py` set the info. Test proves GUC survives a commit. Codex APPROVE.
- **Phase 2a** (`d5e2fe1`): tenant-table routes set the GUC — `/onboarding`,`/change-password`,`/referral`→
  `get_rls_db`; `/reset-password`→manual GUC (token-auth). Codex caught reset-password (silent password-reuse
  regression) in review → fixed.
- **Phase 2b** (`5efda74`,`06ce1c8`,`de3d40e`): cross-tenant routes via bounded primitives, NOT role elevation.
  Migration 029: `grant_referral_credit()`+`activation_funnel()` SECURITY DEFINER fns (search_path pinned,
  schema-qualified, REVOKE PUBLIC+anon+authenticated, EXECUTE app-only) + `public_sample_cache` singleton.
  Webhook/funnel routes call the fns; a Celery task precomputes the sanitized public sample cache and
  `/sample` reads it (no live tenant query from an unauth endpoint). Tests for fn idempotency/aggregate.

**Tried / Decided:** Codex vetoed `GRANT bridgeleads_system TO bridgeleads_app` (internet-facing RCE→worker
role) and broad app policies on results/jobs/scraper_configs (OR with tenant policy → destroys isolation).
Chose SECURITY DEFINER fns + a precomputed sample table. User chose the THOROUGH cutover over pragmatic.

**Caught & fixed (Codex):** activation-funnel CTE referenced `stripe_customer_id` without selecting it
(latent runtime error) — fixed in the ported fn. `date_recorded.isoformat()` in the sample task — column is
String(32), would crash — store verbatim. App role over-granted on worker tables — split into write/read/none
after verifying the real route surface (Tracerfy webhook dispatches to a worker via `.delay`, so skip-trace
is worker-only). `county_connectors` needed INSERT (POST /connectors) — caught in self-review.

**Failed / Blocked / Environment:** Codex CLI 400'd for a stretch — root cause was the shared companion
runtime auto-attaching an `image_generation` tool pinned to nonexistent model `gpt-image-2`; bypass with
`-c 'tools.image_generation=false'`. Same cause broke the stop-time review gate (disabled it via
`codex-companion.mjs setup --disable-review-gate`; re-enable when the account-level image tool is fixed).

**Pending / Handoff:** **Phase 2c** = the big migration: role-targeted policies (`TO bridgeleads_app` tenant
policies; `FOR ALL TO bridgeleads_system` on ALL worker tables — MUST include results/jobs/scraper_configs
per the 2b-iii dependency; broad `TO bridgeleads_app` on `users`+shared catalogs; asymmetric referral_events
INSERT `WITH CHECK (referrer_id=GUC)`). Then **2d** isolation tests, **Phase 3** repoint connections (add
`DATABASE_URL_MIGRATE`; `alembic/env.py` still uses `DATABASE_URL_SYNC`), **Phase 4** flip `RLS_ENFORCE=True`
+ `FORCE ROW LEVEL SECURITY` last. Full plan: `tasks/rls-cutover-todo.md`. Nothing deployed; all inert under
today's BYPASSRLS role.

---

## 2026-06-02 — SQL-injection audit (Claude × Codex): NO SQLi found; pivoted to DB role least-privilege cutover plan

**Built / Shipped:**
- Full SQLi audit of the FastAPI/SQLAlchemy/Supabase app on a user request ("search bar wiped the users table").
  Traced every user-input→DB path. **Verdict: no SQL injection exists.** Everything is parameterized:
  `text()` with `:named` binds throughout; search uses ORM `ilike(pattern, escape="\\")` (`jobs.py:241`) and
  static clause-strings with `:q`/`:kw_n` binds (`scrapers.py:477-532`, plus `sanitize_search()`); the f-string
  INSERT in `tasks.py:463` interpolates only placeholder *tokens* (data → `params`); alembic/scripts f-strings
  use hardcoded constants only; advisory-lock f-string is a guaranteed `int(md5,16)`. No psycopg/asyncpg raw
  cursor, no `from_statement`/`literal_column` w/ user data, no dynamic `order_by`/column injection.
- **Codex independently CONFIRMED** "no SQLi" (read the files itself; also noted `billing.py:58` — `days` bound,
  int 1-365, safe). Codex then sharpened the real fix (below). Cross-confirmation per codex-collaboration rule.
- **Real risk = over-privileged role,** not injection: prod connects as a `BYPASSRLS` role (matches today's
  earlier journal entry + the RLS_ENFORCE landmine). Authored a staged least-privilege cutover:
  `tasks/rls-cutover-todo.md` + `docs/security/RLS-CUTOVER-RUNBOOK.md` + Phase-0 `scripts/provision_rls_roles.sql`.

**Tried / Decided:** Three-role model (Codex's refinement of my single-restricted-role idea): `bridgeleads_owner`
(DDL/alembic), `bridgeleads_app` (API: SELECT/INSERT/UPDATE, **no DELETE** — user deletes are soft:
`jobs.status='cancelled'`, `scraper_configs.active=false`), `bridgeleads_system` (workers: + DELETE on
`county_records` only, the lone physical delete at `scheduler.py:521`). Rejected blanket-DELETE app role.

**Caught & fixed:** Self-review of the Phase-0 SQL (Codex was rate-limited) caught a missing grant: the API
**writes** `county_connectors` via `POST /connectors` (`scrapers.py:313`). SELECT-only would have permission-
denied at Phase 4 — changed to **SELECT + INSERT** on that table.

**Failed / Blocked:** Codex CLI hit its usage limit mid-session (resets ~3:25 PM local) → the Phase-0 Codex
review gate is DEFERRED, must run before Phase 3 repoints connections. Did NOT fabricate any SQLi "fix" — there
was nothing to fix; reported that honestly instead.

**Pending / Handoff:** Phases 1-4 await user approval (per phased-execution rule). Open Qs answered: custom roles
OK; API uses `DATABASE_URL` / workers+alembic share `DATABASE_URL_SYNC` (`alembic/env.py:15`) → Phase 3 adds
`DATABASE_URL_MIGRATE` for the owner role. Phase 1 is the load-bearing code change (per-transaction GUC reapply
so `app.current_user_id` survives the mid-task commit; else NOBYPASSRLS breaks `run_scrape_job`).

**Facts learned:** This codebase is genuinely hardened against SQLi (8 prior red-team rounds show). The tenancy
boundary today is the app-layer `WHERE user_id` filter, NOT RLS — because the role bypasses RLS. The cutover is
what makes RLS actually load-bearing. `FORCE ROW LEVEL SECURITY` must be the very last step.

---

## 2026-06-02 — CRITICAL: Supabase `rls_disabled_in_public` (county_records PII) — live-fixed + Codex-verified

**Built / Shipped:**
- Supabase advisor flagged CRITICAL "Table publicly accessible — RLS not enabled." Different surface from the
  red-team (which audited the FastAPI app): this is Supabase's auto-exposed PostgREST API (anon key in the
  frontend) where **RLS is the only guard**. A `public` table without RLS is readable/writable by anyone with
  the project URL + anon key, bypassing the app.
- **Live audit** (`scripts/check_rls_roles.py`, read-only): both app roles (`DATABASE_URL` async +
  `DATABASE_URL_SYNC` sync) = `postgres`, `bypassrls=true`. Live, exactly ONE public table had RLS disabled:
  **`county_records`** (3305 rows of scraped homeowner PII) — RLS was *explicitly* disabled in migration 023
  (which relied on a write-trigger that does nothing against anon *reads*).
- **Live hotfix applied** (`scripts/apply_rls_hotfix.py`): `ENABLE ROW LEVEL SECURITY` + a shared-read SELECT
  policy on `county_records`. **Verified live by role impersonation:** `postgres`(BYPASSRLS)=3305 rows,
  `anon`=0, `authenticated`=0 → exposure closed, app unaffected.
- **Permanent migrations:** `027` (ENABLE RLS on the 5 anon-exposed app tables — idempotent, covers the new
  `skip_trace_meter_events` once 026 deploys) + `028` (the county_records shared-read policy).
- **Codex** consulted on the plan (consensus: enable RLS, no policy/FORCE = default-deny for anon, safe under
  BYPASSRLS), then reviewed the build → caught a real **deadlock** (concurrent webhooks locking overlapping
  users in opposite order in the meter outbox) → fixed with `ORDER BY user_id` deterministic locking.

**Tried / Decided:** enable-RLS-no-policy (not FORCE) for the emergency lockout — default-deny stops anon while
the BYPASSRLS app is untouched; FORCE/the WITH-CHECK enforcement belongs in the deferred HIGH-2 cutover. The
county_records SELECT policy denies anon (never sets `app.current_user_id`) yet allows authenticated app
sessions — forward-compat for the non-bypass cutover, inert today.

**Facts learned:** the app connects as `postgres` (BYPASSRLS, not superuser) on both URLs. Supabase exposes a
public PostgREST API guarded ONLY by RLS — every `public` table needs RLS even though the app never uses that
API. `county_records` write-trigger ≠ read protection.

**Pending:** `alembic upgrade head` (applies 025–028) on the next deploy; the live hotfix already covers the
exposed table so prod is safe meanwhile. county_records *writes* under a future non-bypass role still need the
HIGH-2 system-role handling.

---

## 2026-06-01 — Claude × Codex adversarial red-team + remediation (branch `security/redteam-remediation-2026-06-01`)

**Built / Shipped** (14 atomic commits on the branch; full register `docs/security/REDTEAM-2026-06-01.md`):
- **Round 1 — Claude red team:** 6 parallel security-auditor subagents across auth, SSRF, multi-tenancy,
  exports, billing, infra. ~26 findings, each with a proven exploit.
- **Round 2 — Codex independent verification:** Codex re-derived every finding from code — **refuted 6**
  Claude over-claimed (incl. 2 fake "Criticals" → both real but HIGH), and **found 3 Claude missed**
  (PACS assessor SSRF `N1`, unauth `/scrapers/sample` real-PII leak `N2`, dead connector validation `N3`).
- **Round 3 — remediation:** Phases 1–5 by Claude directly; Phases 6/7/8 + the A3 reset flow by 4 parallel
  coder subagents on disjoint files. Fixes (all committed):
  - Auth: refresh rotation (atomic `consume_once`), change-pw revokes sessions, **new password-reset flow**,
    register timing parity, lockout-DoS cap (real TTL decay), narrowed `/refresh` except.
  - SSRF: in-page fetch/XHR egress closed (`base_scraper` route guard validates ALL resource types),
    model-emitted `evaluate` JS removed, PACS `assessor_url` validated, raw `requests`→`safe_http`,
    `validate_scraping_target` resolve-by-default + IDNA fail-closed + loopback aliases.
  - CSV: leading-quote + embedded-tab formula-injection bypasses closed (proven vs 11 payloads) + tests.
  - Billing: Tracerfy webhook replay/SSRF guard, counter↔meter consistency, **transactional meter outbox**
    (`SkipTraceMeterEvent`, migration 026, retrying task + 180s beat sweep), coupon caching.
  - Tenancy: migration 025 adds `WITH CHECK` to RLS write policies + startup hard-fails on a BYPASSRLS
    role; download-token audience hardened; PII log demoted to `Result.id`.
  - Infra: XFF rightmost-hop (kill spoof bypass), fail-closed auth rate-limit fallback, CORS origin validation.

**Tried / Decided:**
- Two independent reviewers with different blind spots is the whole point — kept Claude and Codex passes
  fully independent in Round 1/2 (Codex never saw Claude's findings before re-deriving them).
- Billing durability: rejected fire-and-forget meter reporting; chose a **transactional outbox** (intent
  persisted in the same txn as the counter advance, swept by a beat task) — the only design that survives
  Stripe-down AND broker-down without double-billing (stable MeterEvent id).
- Removed the Tracerfy webhook edge dedup entirely — the worker `FOR UPDATE` + status guard is the
  authoritative idempotency; the edge claim was net-negative (could drop a legit retry).
- Phased, ≤5 files/phase, atomic commit per phase; subagents on disjoint files to parallelize safely.

**Caught & fixed (the headline):** the Claude×Codex loop caught **~17 bugs in the fixes themselves** across
**8 Codex review rounds** — refresh-rotation TOCTOU (→ SET NX), XFF trusting spoofable Fly/CF headers on
Railway, password-change revoking *after* commit, a cosmetic lockout cap, a swallowed Stripe error defeating
autoretry, an enqueue-failure losing a meter event, a stale second reset link surviving a reset, password
recovery leaving the API key valid, a fail-open revoke cache, an RLS guard swallowed by the worker
bootstrap, and — biggest — a **production-outage-class** T2 bug: the hard-fail-on-BYPASSRLS would have made
the API + workers refuse to boot on the *current* prod role, and a downgraded role would block scrapes/ingest
(mid-task commit clears the `SET LOCAL` GUC; `system_sync_session` has no tenant context). Findings shrank
and deepened each round (3→2→3→2→2→1→2) — convergence. Each was re-fixed + re-verified. Two reviewers > one.

**Failed / Blocked:** full integration tests can't run locally (need CI Postgres+Redis; `conftest.py` wires
real infra) — verified statically (`py_compile` + `ruff` every phase) + pure-function CSV tests + the Codex
review gate. Local `pytest -k auth` ran against degraded infra (503s from Redis-unavailable revocation, DB
connection errors) — not logic regressions.

**Pending / Handoff:**
- **NOT merged to `main`** — branch awaits review/merge.
- **T2 / `RLS_ENFORCE` — DO NOT enable yet:** default is OFF (advisory log; the API/workers boot normally on
  today's BYPASSRLS role). The `025` WITH CHECK policies are inert until the role is downgraded. Flip
  `RLS_ENFORCE=True` ONLY after the deferred HIGH-2 cutover lands (non-BYPASSRLS role + per-transaction GUC
  reapply in `rls_sync_session` + a system RLS policy for `system_sync_session`) — else scrapes + ingest break.
  Add `RLS_ENFORCE` to `.env.example` (access-restricted in this session). Run `alembic upgrade head` (025 + 026).
- **Migration collision:** the older `security/high-2-rls` branch also has a `025_*`; this branch's
  `025_rls_with_check_write_policies` + `026_add_skip_trace_meter_outbox` chain off `024`. Reconcile before merge.
- **✅ CONVERGED (2026-06-02):** final Codex review (round 9) over the whole 19-commit diff returned CLEAN —
  "no discrete, actionable regressions ... that would break existing behavior or undermine the intended
  fixes." Both reviewers agree. Branch is review-ready (pending the RLS_ENFORCE/`.env.example` handoff above).

**Facts learned:**
- The codebase was already well-hardened from the prior 2026-06-01 review — remaining bugs were subtle
  (races, TOCTOU, fail-open ordering, durability), exactly where a second independent model pays off.
- New `settings` added: `TRUSTED_PROXY_HOPS` (default 1). New tables: `skip_trace_meter_events`.

---

## 2026-06-01 — Security pack adoption + full review remediation

**Built / Shipped** (all on `main` unless noted; every fix Codex-reviewed):
- **Standing security + Codex workflow:** copied the security pack to `docs/security/`; added
  `.claude/rules/security.md` + `.claude/rules/codex-collaboration.md` (auto-loaded) and a
  SessionStart hook (`.claude/helpers/security-codex-reminder.cjs`) so every session/build runs
  the security baseline and brainstorms-with-Codex-then-Codex-reviews.
- **Full security review** (5 parallel reviewers + Codex cross-check) → `docs/security/REVIEW-2026-06-01.md`
  (Critical 1, High 9, Medium 9, Low 8).
- **CRITICAL-1** webhook SSRF closed (`validate_outbound_webhook`, fail-closed worker gate). `a8b358a`
- **HIGH-1** DNS rebinding + **per-hop context route guard** (`base_scraper._ssrf_route_guard`),
  live-validated with Chromium. `3ace79c`,`f0a2dc4`
- **HIGH-3/4/5** SSRF cluster — `src/utils/safe_http.py` (`safe_get`, `safe_get_following`),
  eagleweb cookie-fetch origin-pin, county_gis endpoint validation. `ea4b4b8`
- **HIGH-6** CSV injection, **HIGH-7** `.e2e` gitignore, **HIGH-8** API-key revocation on logout-all,
  **HIGH-9** Stripe error leak. `51b7b45`,`056a1b6`
- **All 9 Mediums** (`f37180c`,`9d52aea`,`c144b01`,`edb0eb2`,`84e02e7`,`ae5235b`,`a9987c8`) — input
  bounds, Tracerfy SSRF, rate-limits, `R2_PUBLIC_URL` gate, RLS-ordering, **revocable download-token
  delivery links** (`src/api/download_tokens.py`, gated on `API_BASE_URL`).
- **All 8 Lows** (`4e3aa4d`,`a8a3315`,`4a70c3d`,`be2fe59`) — global exception handler, admin 403→404,
  scoped reads, PII-log demotion, central `_RedactionFilter`.
- **HIGH-2 (RLS role downgrade): code-ready on branch `security/high-2-rls`** (`d0a89dd`, NOT on main):
  migration `025` (policy `bridgeleads_system` escape + WITH CHECK), `session.py` system engine +
  `after_begin` GUC reapply, cutover runbook. Pending staged cutover.
- **Cleanup:** deleted stray junk + committed-junk (`.terraform` cache w/ a `.exe`, scratch PNGs);
  hardened `.gitignore` (incl. root scratch). `1f5e7ca`,`5f284f3`
- **Ops:** set `API_BASE_URL=https://api.bridgeleads.io` on Railway via CLI; deleted live-credential
  `.e2e_*` files from disk.

**Tried / Decided:**
- Pack adaptation: **kept the Abro pack + a stack-translation rules layer** rather than a full
  native rewrite (Codex argued for native; we deferred it — translation table lives in
  `.claude/rules/security.md`).
- HIGH-2: **deferred to a branch** rather than applying to `main` — a `FORCE RLS` migration
  auto-runs on deploy and would break prod before roles/code exist. Role-based policy escape
  chosen over a GUC bypass (a GUC any SQL path could flip is a weak boundary).
- Delivery links: chose revocable app download-tokens over raw 48h presigned R2 URLs;
  gated on `API_BASE_URL` so it's a safe no-op until configured.

**Failed / Blocked:**
- SSH `git push` denied (no authorized key) → switched remote to HTTPS via `gh` (Abenezer1244).
- Railway `variables --set` first failed ("trial expired"); later succeeded — `API_BASE_URL` set.
- `.env.example` is **sandbox-hard-blocked** for the agent → user must add `API_BASE_URL`,
  `R2_ALLOW_PUBLIC_URLS=false`, `DATABASE_URL_SYSTEM` manually.

**Caught & fixed** (Codex review caught real bugs before shipping):
- CSV sanitizer leading-whitespace bypass (` \t=cmd`); Tracerfy redirect-revalidation gap +
  https→http scheme downgrade; progressively-more unbounded Pydantic fields (4 rounds);
  `safe_get`/`probe` ambient-proxy (`trust_env`) gaps; King page-route taking precedence over
  the context SSRF guard; a `scheduler.py` `F821` NameError (would crash dispatch on deploy);
  2 cutover-runbook P1s (missing `DATABASE_URL` switch; broken `DO/:'var'` role create);
  `.gitignore` missing root scratch paths.

**Pending / Handoff (user/ops):**
- The leaked credential was an **auto-generated E2E demo test-user login**
  (`king_e2e_*@bridgeleads.io`, created by the E2E test tooling — not a real
  customer/admin or external-portal password). Low severity. Optional: rotate or
  disable that throwaway test account. (Local `.e2e_*` files already deleted.)
- HIGH-2 cutover: provision `bridgeleads_app`/`bridgeleads_system` non-owner `NOBYPASSRLS` roles,
  switch all 3 DB URLs, extend `tests/test_rls_isolation.py` to 10 tables, `FORCE` last on staging
  → promote. Runbook: `docs/security/high-2-cutover-runbook.sql`.
- Add a Cloudflare R2 lifecycle expiry rule on the `exports/` prefix (30–90d).
- Decide on `design-system/bridgeleads/MASTER.md` (deleted on disk, deletion not committed).

**Facts learned (durable):**
- Prod DB role has `BYPASSRLS=true` → RLS policies are decorative in prod today; the `WHERE
  user_id` filter is the only tenant boundary until the HIGH-2 cutover.
- No table uses `FORCE ROW LEVEL SECURITY`; the app role owns the tables → a role swap alone is a
  no-op without `FORCE`.
- `SET LOCAL` / `set_config(...,true)` is transaction-local and **dies on commit** — worker
  sessions that commit mid-block lose RLS context (fixed via an `after_begin` listener).
- Download route path is `/jobs/{id}/download` (no `/api/v1` prefix); API domain is
  `https://api.bridgeleads.io` (Railway service `api`, project `bridgeleads-production`).
- Codex CLI works here (`codex exec resume <session> -`); SSH push doesn't (use `gh`/HTTPS).
