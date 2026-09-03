# HANDOFF — "Test 1" lead data-quality audit + unactionable-lead quarantine (2026-09-02)

Written at ~79% context. Everything below is verified, not assumed. Read §1 and §2 first.

---

## 1. Where things stand (read this first)

**Two branches, both COMMITTED, both UNPUSHED, no PRs opened.**

| | Backend | Frontend |
|---|---|---|
| Worktree | `C:/Users/Windows/bridgeleads-worktrees/test1-data-quality` | `C:/Users/Windows/bridgeleads-worktrees/fe-test1-view` |
| Repo | `web-scrapper-automation` | `bridgeleads-web` (sibling repo) |
| Branch | `fix/test1-lead-data-quality` | `fix/scraper-view-latest-results` |
| Commits | `4cbc252`, `0504a29` | `e7c6352` |
| Base | `origin/main` | `origin/master` |
| State | clean; **ahead 2, behind 4** | clean; ahead 1 |

🛑 **The backend branch is 4 commits behind `origin/main`** (#184/#185/#186/#187 from the parallel
Test 2 / Test 3 sessions landed while this work was in progress). **Merge `origin/main` into the
branch before opening a PR** — GitHub SKIPS CI entirely on a conflicting PR, so it would look
untested rather than blocked ([[landmine_ops_and_db_roles]]).

⚠️ Expect **conflicts in `src/workers/tasks.py`** (this branch moved a ~80-line billing block and
the Test 2 branch touched the same file) and possibly `src/scrapers/enrichment/county_gis.py`
(#184 "key county GIS by caller parcel" touches the same batch functions this branch rewrote).

**Nothing is deployed.** No migration was added by either branch.

---

## 2. Immediate next steps, in order

1. `cd` to the BE worktree, `git merge origin/main`, resolve (see §6 for what each hunk means),
   re-run the full suite (§5), open the PR.
2. Open the FE PR (`bridgeleads-web`, base `master`). It is one file, no conflicts expected.
3. 👤 **User action, not code:** top up Tracerfy credits (they said they would). 565 skip-trace rows
   across 7 jobs are `queued` and will auto-submit within 5 minutes of funding — the dispatcher
   already retries every tick. Verify with `scripts/diag_skip_trace_state.py` via `railway run`.
4. 👤 **User/ops action:** purge the dead `county_records` cache (3,305 rows from March) using
   `DATABASE_URL_MIGRATE` — the app role has **no DELETE**. The API now filters those rows out for
   typed configs, so this is cleanup, not a fix.

---

## 3. The goal and what was actually wrong

The user asked for an end-to-end root-cause investigation of scraper **"Test 1"** (config
`be5dc73b-4db1-42d7-bd51-e01a80b9c4b6`, job `1e358ca8-edbd-489b-850f-eaadbde67e07`,
pierce/WA/probate, 110 rows, account `zowiegirma29@gmail.com`, Pro trial): leads with missing party
name, parcel, property address, mailing address, phone, email, and possible placeholder data.

**Verified at the sources — these are NOT bugs, do not re-investigate:**

- The **4 parcel-less rows** have no "Parcel Id" on the ARMS Legal Description tab. Confirmed by
  re-opening each instrument (`202606300050`, `202607270013`, `202607310390`, `202608200380`) on
  armsweb.co.pierce.wa.us; positive controls on the *same pages* returned parcels, including
  BAKKE's `0121228036` which matches what we stored. Method saved at
  `<scratchpad>/arms_source_check.py` (search one date + probate checkbox id 226, click the first
  instrument, iterate the `#cphNoMargin_OptionsBar1_ItemList` dropdown, click the Legal Description
  tab, read "Parcel Id:").
- **BAKKE's missing address**: parcel `0121228036` is absent from Pierce GIS `Tax_Parcels` AND from
  WA statewide `Current_Parcels`. Genuinely unavailable.
- **Party names are correct**: recorder grantor/grantee strings ("BERNATH DAVID WAYNE EST OF" /
  heirs "BERNATH GLORIA A"). Zero placeholder/dummy/synthetic values anywhere in the 110 rows.

**Application defects that WERE real (all fixed in `4cbc252`):**

| # | Defect | Root cause | Prod scope |
|---|---|---|---|
| 1 | Real people excluded from skip trace | `looks_like_non_personal_party_name` substring-matched `" ave"`/`" way"` → AVELINO, AVERY, WAYNE, WAYLAND classified "not a person" and **dropped entirely** (the gate suppresses eligibility, it is not the entity router) | 43 rows / 12 jobs in 90d |
| 2 | Tracerfy never got the mailing address | `build_pending_row_payload` hard-coded every `mail_*` field to `None` under a comment claiming they were "populated below" — nothing did | every traced row ever |
| 3 | False "absentee owner" | Pierce GIS `Site_Address` drops the suffix/post-directional that `Delivery_Address` keeps ("20508 ISLAND PKWY" vs "… PKWY E"); `_addresses_differ` compared normalized streets exactly | 5 prod rows, **already corrected** |
| 4 | Fabricated mailing address | WA statewide GIS fallback copied the situs into `mailing_address` → read downstream as "owner-occupied" (`absentee=False`) and shipped in the CSV Mailing column | latent (Pierce hit the county endpoint) |
| 5 | Skip trace stalled for every tenant | Tracerfy 402 "insufficient credits"; the dispatcher returned without alerting and without trying a smaller batch; rows were read with no durable claim | 565 rows / 7 jobs, 7+ hours |
| 6 | Dead cache shown as this scraper's leads | `get_cached_records` had a `doc_type IS NULL OR …` escape hatch; the whole 3,305-row March cache (doc_type NULL everywhere, column-shifted rows, literal `(enrichment unavailable)` addresses) passed every record-type filter | every typed config |
| 7 | "View" opened the cache, not the results | command palette routed to `/scrapers/{id}/records` | FE |

Then the user made a **product decision** (implemented in `0504a29`): *"what is the use of
displaying if they are useless and unmailable"* → **a row with no property address AND no mailing
address is not a lead.** Not listed, not exported, not counted, not billed — but KEPT in `results`
for cross-job dedup and scraper-health. Test 1 now reads **105**, not 110.

---

## 4. Files changed and why

### Commit `4cbc252` — the audit fixes

- `src/scrapers/enrichment/skip_trace.py` — rewrote `looks_like_non_personal_party_name`
  (code-violation shapes only: category prefixes, `"? <house number>"` separators, bare-address
  names; whole-word street suffixes; street-named LLCs deliberately still pass so the *advanced*
  trace can handle them). `build_pending_row_payload` fills `mail_*`. `submit_batch` now
  distinguishes `requests.ConnectionError` ("Connection error …", never delivered) from other
  request errors ("Network error …", outcome unknown).
- `src/utils/address_intel.py` — `_same_street_modulo_trailing_tokens`: a **trailing** suffix or
  post-directional present on one side only no longer means "different street". Deliberately
  trailing-only — a dropped *leading* directional ("E MAIN" vs "MAIN") or any non-suffix extra
  token still reads as different.
- `src/scrapers/enrichment/county_gis.py` — `_statewide_result()`: `mailing_address=None`, situs
  locality kept **on** `property_address` in the 3-part `"STREET, CITY, WA ZIP"` shape (a bare
  `"STREET, WA"` would be mis-parsed as `city="WA"` downstream). Generic parser's else-branch no
  longer copies situs→mailing.
- `src/workers/skip_trace_dispatcher.py` — the big one. `FOR UPDATE SKIP LOCKED` + a **committed
  `status='submitting'` claim before the POST**; `classify_submit_failure()` maps failures to
  release-to-`queued` (429/402/5xx/connection-refused), mark-`errored` (definite 4xx/config, which
  also flips the lead's `skip_trace_status` to `errored`), or **leave `submitting`** (timeout /
  non-JSON / missing queue_id — Tracerfy may have charged us, never auto-resubmit);
  `_alert_stale_claims()` pages ops after 30 min; `affordable_row_count()` parses "need N more
  credits" and submits the affordable FIFO head; `_alert_out_of_credits()` pages ops with the
  backlog size.
- `src/workers/scheduler_helpers/dialer.py` — the sweep treats `submitting` as unsettled.
- `src/api/routes/scrapers.py` — `_cached_doc_type_filter()` mirrors the scraper's own matcher
  (`ILIKE` for phrases, PostgreSQL `~*` with `\m…\M` word boundaries for short codes like `SUCC`,
  plus `_DOC_TYPE_EXCLUDE`); `_cache_address_or_none()` maps the placeholder to null.
- `scripts/backfill_owner_flags.py` — `--recompute-suffixless` mode (the normal backfill only
  looks at rows where all four flags are NULL, so it could never revisit a wrong `TRUE`).
- Tests: `test_skip_trace_eligibility.py`, `test_county_gis_parse.py`,
  `test_skip_trace_dispatcher_credits.py`, `test_skip_trace_dispatcher_claim.py`,
  `test_cached_records_filter.py`, plus a `TestSuffixlessSitus` class in `test_address_intel.py`.

### Commit `0504a29` — the quarantine

- **`src/api/lead_actionability.py` (NEW)** — the whole rule, three spellings:
  `actionable_condition()` (ORM), `actionable_sql(alias)` (raw-SQL twin for hand-written queries),
  `is_actionable(row)` (Python twin for in-memory lists). All three `btrim`/`strip`. Modeled
  exactly on `src/api/tax_filters.py::tax_cap_condition` — **that is the pattern to follow if you
  add another standing rule.**
- Wired as a standing filter into: `jobs.py` (results base query, live download, `total_scraped`,
  `duplicate_count`, `previous_job_id` exists(), the download's "has any rows" check),
  `batch_export.py`, `segments.py` (4 queries), `analytics.py`, `scheduler_helpers/dialer.py`,
  `dialer_outbox.py`, and `tasks_helpers/enrich.py::_enqueue_skip_trace_rows` (never pay Tracerfy
  for a quarantined row).
- `src/workers/tasks.py` — **the risky part**:
  - both exports filtered by `is_actionable` (the `refreshed` list itself stays complete so the
    property-membership upsert still sees every row);
  - the **billing block moved** from before inline enrichment to right before the done-CAS (a
    county whose addresses come from enrichment — King probate, the generic GIS sweep — cannot be
    scored for actionability any earlier). The force-finalize guard moved with it;
  - `billable_count` = persisted non-duplicate **actionable** rows; `display_count` = that same
    number, so headline / email / webhook / notification / bill can never disagree (the webhook
    was separately sending `len(records)`, which included duplicates);
  - billing CAS + `records_used` increment now commit **in the same transaction as the done-CAS**
    via the new `_set_status(..., commit=False)` in `tasks_helpers/status.py`; a failed CAS rolls
    both back (a cancelled job is never charged);
  - a failed **post-enrichment re-upload is now fatal before billing** (releases this job's
    `delivered_records` claims, `_fail_job`, `job_failed` notification, return) — mirroring the
    first-upload rule. Without this, the R2 file could omit rows that enrichment had just made
    actionable while the bill counted them.
- Fixtures in 5 test modules that built "leads" with a `property_key` but **no address** now carry
  one: `test_batch_delivery_mode.py`, `test_segments_tax_cap.py`, `test_tax_cap_jobs.py`,
  `test_dialer_tax_cap.py` (also added `actionable_condition()` to its reconstructed WHERE clauses
  and its wiring guard), `test_batch_leads_endpoint.py`, `test_analytics.py`.
- `tests/test_lead_actionability.py` (NEW, 19 tests) — cross-checks all three spellings.

### FE commit `e7c6352`

`components/shell/CommandPalette.tsx` only — routes a scraper entry to its latest **done** job's
`/results/{id}`, falling back to the cache page only when the scraper has never finished a run.
The Scrapers page and the dashboard row already had this rule; the palette was the straggler.

---

## 5. How this was verified (and how to re-verify)

- **Full suite**: `bash C:/Users/Windows/bl-testenv/run-full-pytest.sh "C:/Users/Windows/bridgeleads-worktrees/test1-data-quality"`
  → **1844 passed, 2 skipped, 54 deselected** on the state before the final two edits.
- 🛑 The **last** full run showed 16 failures in `test_auth` / `test_batches_read` /
  `test_break_glass_login` / `test_jobs` / `test_register_email_verification` — that is the known
  **shared-rig contention** signature ([[reference_local_pytest_rig_hygiene]]: one PG + one Redis
  shared with every process on the box; `assert 401 == 400` from the auth rate limiter). **All 88
  of those tests pass in isolation** and none touch the changed code. Re-run them alone before
  believing a failure. GitHub CI is the authoritative gate.
- **Ruff**: clean across `src`, `tests`, `scripts/backfill_owner_flags.py`.
- **Browser (headless Chromium, per the user's "never Claude in Chrome" rule)**:
  `<scratchpad>/ui_verify_results.py` logs in at bridgeleads.io with credentials read from a local
  file (never printed), opens `/results/1e358ca8…`, waits for a **non-`animate-pulse`** row
  (skeleton rows render first — a naive `table tbody tr` wait returns empty cells), and reports
  the rendered grid, console errors and 4xx API calls. Result: 110 rows, correct field mapping,
  0 console errors, 0 API errors.
- **Prod data fix applied**: `railway run python scripts/backfill_owner_flags.py
  --recompute-suffixless` (dry run: 5) then `--commit` (corrected: 5). Verified via the API that
  VAN ODOM's `absentee_owner` is now `None` while JARVIS (genuinely different street) stays `True`.
- **Codex**: consult before coding + **4 adversarial review rounds** on the quarantine and 3 on the
  audit. Final verdict **PASS**.

---

## 6. Failed attempts and dead ends (do not repeat)

- **`WebFetch` on cyberbackgroundchecks.com returns 403** on `/`, `/faqs` and `/terms` — the site
  blocks automated access. See §8.
- **Codex's `rows_uploaded` mismatch finding was REJECTED with prod evidence**: queue `158749` sent
  25 rows, reported `rows_uploaded=24`, and all 25 reconciled hit/miss via the webhook. Tracerfy
  **de-duplicates identical addresses**, so a count mismatch is normal, not a lost tail. Codex
  agreed in round 2. Do not "fix" this.
- **Codex round-2 asked to fold billing+done into one transaction; I first tried the cheaper fix**
  (read `job.billed_count` on an already-billed re-run). Codex round 3 correctly pointed out that
  still leaves a crash window where a watchdog re-run re-scrapes and re-exports against a stale
  bill. The transaction fold is the version that shipped.
- **First `_statewide_result()` implementation emitted `"STREET, WA 98501"` when the city was
  missing** — `_parse_full_address` reads that as `city="WA 98501"`. Now a bare street when there
  is no city.
- **A raw-string docstring is required** on `_cached_doc_type_filter` (the PostgreSQL `\m…\M`
  regex triggers a `SyntaxWarning` otherwise).
- **Two `git`-splice scripts aborted on a bad import anchor** before writing — `tasks.py` has no
  `from src.api.schemas import` at module level; the working anchor is
  `from src.config.constants import (`.
- **`railway run` from a fresh worktree** needs an entry copied in `~/.railway/config.json`
  ([[reference_railway_link_worktree]]) — already done for `test1-data-quality`.
- **The FE worktree's `node_modules` junction** must point at a *populated* install; the main
  `bridgeleads-web/node_modules` is EMPTY (0 entries). I junctioned
  `bridgeleads-worktrees/responsive-7/node_modules` (467 entries) via PowerShell `New-Item
  -ItemType Junction`. `tsc --noEmit` and `eslint` both pass.

---

## 7. Landmines for whoever picks this up

- 🛑 **Fixtures that build a "lead" must set `property_address` (or `mailing_address`)** or the row
  is quarantined and the test silently sees nothing. That is the single most likely cause of a
  confusing new-test failure.
- 🛑 **`/scrapers/{id}/records` is the SHARED `county_records` cache**, keyed by county, not by
  config. `/results/{job_id}` is the tenant's own data. `ENABLE_DAILY_SCRAPE` defaults **False**,
  so that cache has been frozen since 2026-03-23.
- 🛑 **Historical usage is not credited back.** Rows billed before `0504a29` stay billed. Only
  future runs use the new count.
- 🛑 The dispatcher deliberately **never auto-resubmits** rows left in `submitting`. That is the
  double-pay guard, not a bug. Ops reconciles against Tracerfy's queue list.
- 🛑 Don't raise prod alarms from local artifacts ([[feedback_verify_before_alarming]]), and don't
  delete/force-move branches in the shared OneDrive repo
  ([[feedback_no_branch_delete_shared_onedrive]]).

---

## 8. Open question the user raised last (answered, no code written)

A user of theirs resolves name-only leads manually via **cyberbackgroundchecks.com**. My research
and recommendation, in full, is in the conversation; the short version:

- Its terms **prohibit** using the data for "any other form of solicitation" and prohibit
  reselling/commercializing it — direct mail to a probate estate is solicitation, and integrating
  it would be a resale.
- It is a **consumer people-search aggregator**, not a property source; its "property records" are
  re-aggregated, its addresses are often stale/relatives', and it publishes **no DNC scrubbing**.
- It **403s all automated access**, so any integration would be circumventing an access control.
- The compliant path to name→property in WA is the county's own owner-name search, which is exactly
  the **RCW 42.56.070(9)** question the user already froze on 2026-07-30
  ([[project_king_owner_names_gate_2026_07_30]]) — do not re-open it without them raising it.
- If name-only resolution is wanted as a *feature*, it needs a vendor licensed for marketing use
  (PropertyRadar / BatchData class, ~$0.07–0.20/record) plus a legal read. Tracerfy cannot help:
  every trace type needs a property **address** as the key.

---

## 9. Deferred / not done

- Codex round-3 Medium (**deferred with rationale**): a Tracerfy webhook arriving before the
  dispatcher's own commit is discarded as `unknown_queue`. The window is the milliseconds between
  the POST returning and the commit, and the ingest already re-checks under a lock. A bounded
  Celery retry on `unknown_queue` is the follow-up.
- A **"Delayed" state** on the results page when skip-trace rows have been queued > 1h (today it
  says "Processing … 10–15 minutes" indefinitely). Needs `pending_skip_trace_rows.enqueued_at`
  exposed on the results payload — Codex advised a separate PR.
- The historical SAARENAS row stays `not_attempted` on the old job; re-tracing costs credits.
- `public_cache.py`'s aggregate count requires only a property address (line ~78) while its sample
  rows require both — align it if "lead" semantics should be global. Not paid-delivery affecting.
