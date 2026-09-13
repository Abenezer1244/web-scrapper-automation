# HANDOFF: Mailing addresses (testserserf / Snohomish / King) - 2026-09-13

Read this whole file before acting. Everything below was verified in this session unless
marked UNVERIFIED.

## 1. Goal (from the user)

The user reported Results tables showing `Mailing Address: N/A` for every lead
(`testserserf` = Cowlitz probate; a Snohomish pre_foreclosure job; then King tax / probate /
trustee / code_violation jobs and Clark probate). Product rule: BridgeLeads must make every
supported attempt to populate BOTH property and mailing address from authoritative sources,
never fabricate, never copy property into mailing without source evidence, never store
"N/A", show Pending (not N/A) while a lookup is still expected, no double quota / Tracerfy
charge, no em dashes in user-facing copy. Consult Codex before and after every build; any
Codex P1 = no-go until fixed. Phased work, user approval between phases.

## 2. Root causes found (all verified)

| Symptom | Root cause | Status |
|---|---|---|
| Snohomish + Cowlitz 0% mailing | `batch_enrich_parcels_gis` only knew pierce/king; other counties fell to WA statewide situs-only layer (mailing hardcoded None) | FIXED (#275 BE, #128 FE), deployed, 71 historical leads repaired and verified 71/71 vs live county layers |
| "Enriching addresses..." on Complete jobs | `enriching` flag matched only 2 log prefixes | FIXED in #275 (terminal job never enriching) |
| King tax/probate/trustee N/A | King rate-limits per-parcel tax-bill page; big job (16,630 parcels) deferred all; recovery sweep 30 parcels/10 min | FIXED: Assessor bulk extract (#279) |
| 11,105 King leads mailing == property | pre-#210 King tax scraper echoed situs into mailing; `repair_king_tax_historical.py` never applied | REPAIRED in prod from extract (19,371 rows touched: 10,911 real different county address, 8,460 county confirms property) |
| King code_violation 0% (1,782 rows) | Seattle SDCI feed has no parcel_id | BUILT (#283), NOT merged, backfill NOT applied |
| Clark 0% | no public mailing source (restricted layers need token) | User: "we will get back to it another time" |
| 16,593 deferred rows on FAILED King job `60a0e80c` | job failed, never delivered | intentionally untouched |

Legal: Snohomish Parcels dataset terms cite RCW 42.56.070(8) (no commercial use of lists).
The USER CONFIRMED LEGAL CLEARANCE on 2026-09-13 for Snohomish + Cowlitz. The King extract
redacts names; only addresses are used.

## 3. What shipped (merged + deployed)

- **BE #275** `6b7557f` (branch `fix/mailing-source-snoho-cowlitz`, worktree `C:/Users/Windows/bl-wt-mailing`):
  snohomish_WA + cowlitz_WA county GIS configs and parsing; failed/truncated/rolled-back county
  requests mark `enrichment_data.mailing_lookup_deferred`; `recover_deferred_gis_mailing` sweep
  (one lock + one budget with King); recovery recomputes owner flags; JSON-null merge bug fixed
  (`'null'::jsonb || '{}'` builds an array); `ResultsPage.enriching` false for terminal jobs;
  `scripts/requeue_gis_mailing_recovery.py` (APPLIED: 71 rows, all recovered 09:02 UTC).
- **FE #128** `82f5129` (bridgeleads-web, branch `fix/mailing-pending-state`, worktree
  `C:/Users/Windows/bl-wt-mailing-fe`): `MailingValue` shows Pending when job enriching or row
  deferred, N/A otherwise; table + mobile cards.
- **BE #279** `57857ad` (branch `feat/king-rpacct-mailing`): `src/scrapers/enrichment/king_rpacct.py`
  (download, 24h worker cache, 14-day stale bound, schema check, unambiguous resolve);
  `scripts/king_rpacct_mailing.py` (APPLIED 09:33-09:48 UTC: 35,381 rows); live King enrichment
  fills mailing from the extract before tax-bill pages (`_fill_king_mailing_from_extract` in
  `src/workers/tasks_helpers/enrich.py`); King recovery sweep answers from the extract first;
  `tests/conftest.py` autouse fixture disables the real download in tests. Worker redeployed
  (deployment f16a21a3 SUCCESS 10:43 UTC).

## 4. IN PROGRESS: where to continue

**Worktree:** `C:/Users/Windows/bl-wt-rpacct`  **Branch:** `feat/king-code-violation-mailing`
**PR:** #283 (base retargeted to `main`, rebased onto `57857ad`, head `7e48e36` = empty commit
pushed only to trigger CI, because retargeting does not trigger CI).

Files in #283 (4):
- `src/scrapers/enrichment/king_parcel_locate.py` - point-in-polygon on
  `gismaps.kingcounty.gov .../KingCo_PropertyInfo/MapServer/2` (lon,lat, inSR 4326); accept only:
  exactly one polygon, not `exceededTransferLimit`, `_normalize_street` equal, ZIP equal when
  both known, and NO unit anywhere in the lead address. PIN stored in `enrichment_data.kc_pin`
  (NEVER `parcel_id`: dedup/billing key). `resolve_code_violation_mailing()` dedupes by point,
  leaves transient errors and extract outages retryable (no `kc_pin_status`).
- `src/workers/tasks_helpers/enrich.py` - live hook for King code_violation, 420 s budget,
  re-raises Celery time limits.
- `scripts/backfill_king_code_violation_mailing.py` - dry-run default, guarded UPDATE on done
  jobs, owner flags, converges on `kc_pin_status`.
- `tests/test_king_code_violation_mailing.py` (13 tests pass).

Codex: r1 5 P1 fixed + 4 P2 rejected (Codex agreed), r2 1 P1 fixed, r3 GATE PASS.

Prod DRY-RUN (read-only) result: 1,782 candidates, 1,208 strict matches, 1,100 mailing found,
326 address_mismatch, 6 multiple, 5 no_parcel, 237 left for retry (local DNS blips on this box).

**USER APPROVALS ALREADY GIVEN (2026-09-13):** "merge", "approved" (apply the code-violation
backfill), "deploy". So the next session should:

1. Wait for CI on #283: `cd C:/Users/Windows/bl-wt-rpacct && gh pr checks 283`. The FE-style
   "Regenerate API types" failure does not apply to BE; BE checks are Test + Dependency Audit.
2. Merge: `gh pr merge 283 --squash` (do not delete branches; shared repo rule). If the
   auto-mode classifier blocks it, ask the user to run `! gh pr merge 283 --squash`.
3. Watch deploy: `railway deployment list --service worker` (run from the main checkout,
   `C:/Users/Windows/OneDrive - Seattle Colleges/Desktop/web-scrapper-automation`; railway link is
   per-directory). Merges restart workers and kill in-flight jobs.
4. Apply backfill (approved), from the main checkout dir:
   `railway run --service worker C:/Users/Windows/bl-wt-mailing/.venv-mail/Scripts/python.exe C:/Users/Windows/bl-wt-rpacct/scripts/backfill_king_code_violation_mailing.py --apply --report <scratchpad>/cv_apply.jsonl`
   Then run it again (without changes) to pick up the ~237 retry rows. The script downloads the
   extract itself (cached per process temp dir).
5. Verify read-only: count King code_violation rows with `enrichment_data->>'mailing_source'='king_rpacct'`
   and `kc_pin`, spot-check 5 random matched rows: `kc_parcel_address` equals lead street, and
   the mailing matches King's live tax bill (render `payment.kingcounty.gov` via Playwright; URL
   comes from `https://blue.kingcounty.com/Assessor/eRealProperty/Detail.aspx?ParcelNbr=<PIN>`;
   the page's "Mailing Address" block). Pace >= 5 s; King rate-blocks.
6. Post results as a PR #283 comment; update memory
   `project_king_rpacct_mailing_2026_09_13.md`; append `docs/BUILD_JOURNAL.md` entry.
7. Browser UI verification was NOT done (no admin login available). Ask the user for a test
   login if they want Playwright UI proof.

## 5. Failed attempts / landmines hit this session

- Codex review rounds found real bugs I had shipped into branches before review (test double
  signature, truncated ArcGIS pages, deny-list provenance). Always run the Codex gate.
- `codex review --base origin/main` ran pytest itself on the same local Redis DB 15 as my suite;
  do not run both at once.
- Low RAM on this Windows box: the harness repeatedly killed BACKGROUND wrappers ("low memory")
  while the child python/pytest kept running. Check the child PID (`tasklist /FI "PID eq N"`)
  before assuming failure; prefer foreground waits for important runs.
- `git stash` is shared across worktrees; avoid it.
- bash heredocs with regex backslashes / quotes break: write patch scripts to the scratchpad with
  the Write tool and run them; always `assert s.count(old) == 1` (CRLF files: normalize first).
- `gh pr merge` was blocked by the auto-mode classifier once; it worked after the user said
  "merge" explicitly.
- After deploys, the recovery single-flight lock (`bl:mailing_recovery:lock`, TTL 1200 s) stays
  held by the killed tick; recovery resumes up to 20 min later. Not a defect of the fix.
- `test_plan_entitlement_audit.py` (7 tests) fails locally only (needs Stripe metered price env);
  CI is green.
- Local rig: `C:/Users/Windows/bl-testenv` portable PG + Redis; own DB `bridgeleads_mailing_test`,
  redis db 15; the default `python` points at a removed anaconda, use
  `C:/Users/Windows/bl-wt-mailing/.venv-mail/Scripts/python.exe`. Env vars: see
  `.claude/rules/testing.md` (TEST_DATABASE_URL + _SYNC must end `_test`).

## 6. Open items for later (not approved / not started)

- Clark County mailing source (user deferred).
- 149 King echo rows the extract could not answer (left unchanged by design).
- King code_violation statuses "Completed" / "Open Duplicate": Codex suggested not spending
  Tracerfy on them; product decision, untouched.
- Snohomish "Test 5" 4 assumed-mailing rows (job 425d49ce) from an old backfill: not cleared.
