# Handoff: King pre-foreclosure property addresses (2026-09-15)

Branch for the next session: **`fix/king-property-followups`** (cut from `origin/main` at `6071663`)
Worktree: **`C:/Users/Windows/bl-wt-kingprefc`** (Railway is already linked for this directory).
Previous branch `investigate/king-prefc-dq` is merged and done; do not reuse it.

---

## 1. The goal (owner's original request, condensed)

Root-cause and fix King County WA **pre-foreclosure** data quality. In job `85692303`, many leads showed
Property Address = N/A, Auction Date = N/A and Default Owed = N/A, while Parcel ID and Mailing Address were filled.

Rules from the owner:
- Trace the fields separately.
- Never fabricate data, and never copy mailing into property.
- No quota double-charge and no duplicate Tracerfy calls.
- Respect King rate limits and terms.
- Consult Codex on every build and independently verify its findings.
- No em dash in user-facing copy.
- No broad production backfill without approval.

## 2. Current state: DONE and LIVE

| Item | Status |
|---|---|
| BE PR #306 `8c788c2`: condo unit situs, 12-digit account numbers, property recovery sweep, mailing recovery fix, repair script | MERGED + DEPLOYED (Railway api + worker, 2026-09-14 17:58 PDT) |
| FE PR #136 `37d92fc` (bridgeleads-web): "Default Owed" renamed to "Principal Owing" | MERGED + DEPLOYED (Vercel prod) |
| BE PR #307 `6071663`: `PROPERTY_RECOVERY_ENABLED` in `.env.example` | MERGED |
| Production repair (pre_foreclosure) | APPLIED: 84 candidates, 75 written, 0 guard skips, 9 unresolvable; re-run writes 0 |
| Job 85692303 | property address 109 -> **153/157**; billed 153 unchanged; skip trace untouched |
| Worker | registered `src.workers.property_recovery.recover_deferred_property` (beat every 20 min) |

## 3. Root causes (proven, do not re-diagnose)

**Property Address:**
- **Condo units have no GIS record.** King GIS `KingCo_PropertyInfo/2` has NO feature for condo UNIT PINs, so 45 of 47 blank parcels were units. Their only address source was the per-parcel eRealProperty page.
- **The page never ran.** On 09-13 it was not admitted: the SourceAdmission lease was held elsewhere (worker log `phase1_outcomes=not admitted (source busy)`). On 09-07 the circuit breaker had tripped.
- **Nothing retried property address.** The only deferral marker and recovery sweep were mailing-only.
- **Regression by job:** 99.3% (06-23), 98.7% (09-02), 70.3% (09-07), 69.4% (09-13).
- **12-digit IDs are account numbers.** The other 2 blanks were 12-digit recorder values, which are King tax ACCOUNT numbers: all 739,983 RPAcct rows have a 12-digit `AcctNbr` whose first 10 digits equal the PIN, and the 537 repeats are taxable/exempt splits of one PIN.

**Auction Date / Default Owed:**
- These are not parser bugs. The only King source is one cached newspaper (Queen Anne & Magnolia News, 37 notices); 0 of 155 parcels matched, and only 11 of 907 King prefc leads ever had values.
- The recorded NTS document has both values, but King Recorder terms (re-read live) forbid automated access and image mining.
- `default_amount` is NTS **Section IV principal/sum owing**, not Section III arrears, hence the rename.

**Mailing:**
- Comes from the Assessor RPAcct bulk extract, a different source from property.
- Parcel 8135200390 sold on 7/13/2026 before its NTS was recorded: the NTS grantor is the old borrower, while the mailing belongs to the new taxpayer.

**Leading zeros:** not a cause; parcel_id is a string throughout.

## 4. What was built (all on main)

**`src/scrapers/enrichment/king_condo_units.py` (new)**
- Reads the Assessor "Condo Complex and Units.zip" (`EXTR_CondoUnit2.csv`) with a daily cache.
- `unit_situs()` never appends `UnitNbr`: it is not a postal unit (E409 is mailed as #409).
- `compose_fill()` builds "STREET, CITY, ST ZIP" only when the complex (major+0000) GIS ZIP equals the unit ZIP.

**`src/scrapers/enrichment/king_rpacct.py`**
- Shared `download_zip` / `cached_zip` helpers (the public names are unchanged).
- `load_account_pins` / `resolve_account_pins` map accounts to PINs, only on an exact and unanimous match.

**`src/workers/tasks_helpers/enrich.py`** (King-only steps)
- `_resolve_king_account_parcels` runs before the GIS sweep. It writes `resolved_parcel_id`, `source_parcel_id`, `resolved_by="rpacct_account_number"` and `resolved_snapshot`, only for recorder rows (`source == king_landmark_json`). `parcel_id` is never changed.
- `_king_lookup_pin` feeds GIS, the RPAcct prefill and the condo lookup.
- `_fill_king_condo_unit_situs` runs after the GIS sweep. Writes go through `_guarded_update`: fill-only in SQL, JSON merge, ORM sync, reload on a miss, expunge if the row was deleted.
- `_apply_king` guard: an eRealProperty page that does not prove the account-resolved PIN writes nothing; a different PIN is recorded as `resolved_conflict`. A verified page with no site address, or a mismatch, records `property_lookup_outcome`.
- `_mark_king_property_deferred` runs after all King passes. It is a set-based guarded UPDATE setting `property_lookup_deferred=true` on rows still missing property, excluding vacant parcels and settled outcomes.
- Shared SQL constants: `ED_MERGE_SQL`, `ED_MERGEABLE_SQL`, `KING_ACCOUNT_RESOLVER`, `PROPERTY_*`.

**`src/workers/property_recovery.py` (new beat sweep)**
- **Scope:** only marked, delivered (non-duplicate, no `delivery_excluded_reason`) rows on done King jobs.
- **Source order:** condo extract first, then `batch_enrich_king_county(do_mailing=False)` with the resolved PIN, under the shared lease and health gate.
- **Outcomes:** found, no_site_address, parcel_mismatch settle; transient is charged, with max 5 attempts then gave_up; unreached rotates uncharged.
- **Guards:** the write re-checks lookup PIN, unchanged mailing, and eligibility, and recomputes owner flags.
- **Borrowed city:** only for extract-confirmed units.
- **Kill switch:** `PROPERTY_RECOVERY_ENABLED`. Beat entry is in `scheduler.py`; the include is in `src/workers/__init__.py`.

**`src/workers/mailing_recovery.py`**
- The King sweep selects and asks by the lookup PIN (`_LOOKUP_PIN`).
- A `parcel_lookup == "mismatch"` page is checked first and settles as `parcel_mismatch` (charged, terminal).
- The GIS-county sweep is untouched.

**`scripts/repair_king_property_situs.py`**
- Dry-run by default, `--apply`, `--record-type` (default pre_foreclosure), `--job-id`, JSON-lines evidence.
- Delivered non-duplicate rows on done jobs only.
- Guarded UPDATE (id, user_id, parcel_id, lookup PIN, unchanged mailing, empty property, job done) that recomputes owner flags.
- No billing, quota, skip trace or eRealProperty calls.

**Frontend and ops copy**
- FE `bridgeleads-web`: `ResultsTable.tsx` column "Principal Owing", `LeadCards.tsx` row "Principal owing". The CSV key `default_amount` is deliberately unchanged.
- `trustee_sale_finalize.py` ops alert subject renamed.

**Tests:** `test_king_condo_unit_situs.py`, `test_repair_king_property_situs.py`, `test_property_recovery.py`, `test_mailing_recovery_account_numbers.py`. Every guard was mutation-checked (removing it fails a test). Full suite: 3337 passed.

**Docs:** `docs/BUILD_JOURNAL.md` has the 2026-09-14 entry; the `tasks/todo.md` top section covers this work.

## 5. Failed attempts / rejected approaches (don't repeat)

**Design dead ends:**
- **STR check for 12-digit PIDs:** rejected. It only proves the 6-digit major, never the minor (Codex P1 trap in `king_parcel_repair.py`).
- **Appending `UnitNbr` to addresses:** rejected (not a postal unit).
- **COALESCE-ing auction/amount/trustee across notices** in `nts_matcher_task._write_match`: rejected (Codex P1). It is event-atomic by design; merging mixes notices.
- **Recorded NTS images for auction/default:** blocked by King Recorder terms, and the search is reCAPTCHA-gated. Never solve captchas for this.
- **A sweep over all historical blanks:** rejected. That would be an unapproved backfill; history goes through the reviewed repair script only.

**Codex findings, adopted and disproved:**
- **Adopted round-1 findings** (P1s: explicit json cast, stale ORM after a refused write, status-independent resolver guard; P2: placeholder counted as empty; P3: null patch keys):
  - An explicit `::json` cast on the merge.
  - A stale ORM object after a refused write now reloads.
  - The conflict guard no longer depends on status.
  - "(enrichment unavailable)" counts as empty.
  - Null patch keys are no longer written.
- **Disproved round-2 P1:** "phase-2 mailing blocked by the guard". Phase 2 returns the seeded row, and a test proves it.
- **Adopted round-2 P2:** `db.refresh` on a deleted row (expunge instead).
- **Adopted phase-2 round (P1s: lookup-PIN recheck, non-condo borrowed city, mismatch checked before mailing; P2s: the rest):**
  - The write re-checks the lookup PIN.
  - A non-condo parcel no longer borrows a neighbouring parcel's city.
  - Mismatch is checked before mailing.
  - Page results only count for requested PINs.
  - Time limits are re-raised.
  - The marker re-checks settled outcomes in SQL.
  - The repair is restricted to delivered rows.
- **Not adopted, with reasons:**
  - **Owner flags from the stored situs columns.** The UPDATE COALESCEs those columns, so the flags describe what is stored.
  - **Unreached rows rotating uncharged.** That is the settled design of both existing sweeps.

**Environment traps hit this session:**
- **`.env*` access:** blocked for shell commands (sed/grep), but the Read/Edit tools worked.
- **Missing Stripe env vars:** the local suite needs the CI `STRIPE_PRICE_*` vars, or 9 billing tests fail falsely.
- **Dirty test DB:** a reused test DB produced 57 phantom failures. Drop and recreate before trusting a run.
- **CRLF:** files are CRLF, so do scripted replacements with count asserts; a naive LF `str.replace` silently no-ops.
- **Pattern collisions:** `_CANDIDATE_SQL` also matches `_GIS_CANDIDATE_SQL` in `mailing_recovery.py`, so scope edits.
- **Arg length:** `codex exec "$(cat big)"` hits the argument limit (exit 126). Pipe the prompt via stdin: `codex exec ... - < prompt.txt`.
- **Quoting:** bash heredocs containing `\"\"\"` break the tool's quoting; write scripts with the Write tool.

## 6. Next steps (in priority order)

1. **Logged-in UI check (needs the owner).** The live results route redirects to login without a session.
   - Ask the owner to open job `85692303` in Results, or to provide a session.
   - Confirm the 4 parcels (2388800070, 7574800150, 8135200390, 0268000490) show full addresses with city, and the column reads "Principal Owing".
   - Do NOT mint a JWT for a customer account.
2. **King tax_delinquent dry run** (~3,358 condo gaps estimated). Run and review with the owner before any `--apply`:
   `cd C:/Users/Windows/bl-wt-kingprefc && PYTHONIOENCODING=utf-8 railway run C:/Users/Windows/bl-rescat-venv/Scripts/python.exe scripts/repair_king_property_situs.py --record-type tax_delinquent --report C:/Users/Windows/kp_data/tax_dryrun.jsonl`
   Then summarize `by_outcome` plus a per-job breakdown from the evidence file.
3. **Watch the sweep in production.**
   - `railway logs -s worker` and grep `Property recovery:`.
   - Then check that marked rows on a new King job settle, via a read-only query on `enrichment_data->>'property_lookup_deferred'` / `property_lookup_outcome`.
4. **Optional, needs owner decision:**
   - (a) Pre-existing: later eRealProperty ORM writes in the `_apply_king` block are not DB-guarded (Codex called it non-blocking).
   - (b) The 9 unresolvable prefc rows (parcels 3110700100, 3268350090, 6391240030, 8637220160) could get one eRealProperty page check each at 5 s spacing under the lease.

## 7. How to run things (proven this session)

- **Python:** `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe`. Anaconda is gone, and a long scratchpad venv path breaks wheels.
- **Prod read-only queries:** write a script under `C:/Users/Windows/`, then `cd C:/Users/Windows/bl-wt-kingprefc && PYTHONIOENCODING=utf-8 railway run <python> <script>`. Start the transaction with `SET TRANSACTION READ ONLY`.
- **Local tests:** `source C:/Users/Windows/kp_data/env.sh`, which sets DB `bridgeleads_kingprefc_test`, redis db 11 and the CI Stripe vars. Run `python -m pytest <files> -q -p no:cacheprovider -o addopts=""`. For the full suite, use 4 foreground batches (`split -n l/4`).
  - Reset the DB before trusting a run: drop and recreate `bridgeleads_kingprefc_test` via psycopg2 on 127.0.0.1:5432 (user bridgeleads / testpassword), `redis-cli -p 6379 -n 11 flushdb`, then `alembic upgrade head`.
- **Lint:** `ruff check src/ tests/` (0.15.6). There is no type checker in CI.
- **Codex:** run from `C:/Users/Windows/kp_data` with no repo access: `codex exec -c 'model_reasoning_effort="high"' -c 'mcp_servers={}' --skip-git-repo-check - < prompt.txt`. Start the prompt with "Do NOT load any skill ... DO NOT run shell, read files, or git".
- **eRealProperty verification:** at most a few GETs at 5 s spacing, holding the real lease through `REDIS_PUBLIC_URL`. A local `railway run` otherwise fails open on the private Redis host. Delete any file holding the Redis URL afterwards.
- **FE:** worktree `C:/Users/Windows/bl-fe-prefc`, with `node_modules` junctioned to `C:/Users/Windows/bl-fe-kingcv/node_modules`. Verify with tsc, eslint, `next build`, then grep `.next/static`.

## 8. Coordination

- **King tax session** (worktree `bl-wt-kingtax`) owns: owner recovery (`owner_recovery.py`), the parcel-echo gate, plan-cap order, completion copy and the FE tax year.
- **King code-violation session** (`bl-wt-kingcv`) owns the code-violation block of `enrich.py`.
- **Property address work** (condo, account numbers, `property_recovery.py`, the mailing-recovery PIN keying) is owned by this line of work.
- Check `ListAgents` and message peers before editing the shared King block.

## 9. Evidence files (local, not in git)

- `C:/Users/Windows/kp_data/prod_apply.jsonl`: every repaired row, old and new values.
- `C:/Users/Windows/kp_data/prod_dryrun2.jsonl` and `prod_after.jsonl`.
- `C:/Users/Windows/kp_data/condo.zip` and `rpacct.zip`: the 2026-09-05 snapshots.
- `C:/Users/Windows/kp_rows.json`: job 85692303 before the repair.
