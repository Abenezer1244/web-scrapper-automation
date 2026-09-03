# HANDOFF — "Test 2" Pierce pre_foreclosure data-quality audit (2026-09-02 → 09-03)

**Status: the code work is DONE, MERGED (PR #187, squash `58202bb`) and DEPLOYED** (Railway `api` +
`worker` both SUCCESS on `58202bb`; `/health` and `/ready` 200). What remains is two user/legal
decisions and three deliberately-deferred items. A fresh session can start from *Next steps* below.

---

## 1. The goal

The user (account `zowiegirma29@gmail.com`, user id `01dc9396-9a36-49b5-9b98-5343ec107232`) reported
data-quality problems in the leads under the scraper config **"Test 2"** (`fde53328`, pierce / WA /
`pre_foreclosure`, job **`e72bd6bf-6bf4-4562-abe9-9de3375d5380`**, 217 rows, window 06/03–09/01/2026,
scraped 2026-09-02):

1. Auction Date + Default Owed blank on every row.
2. Rows with a Party Name and Parcel ID but no Property/Mailing Address.
3. Rows with only a Party Name.

Standing rules the user set: no guessing (verify against real sources/DB), fix the ROOT CAUSE, use
Codex as an independent reviewer on every step, Playwright/Chromium (never Claude-in-Chrome) for
browser work, work in an isolated branch/worktree, never fabricate data, and verify every fix.
Later the user added: "we have a recaptcha passer so use that", then "work on all [12 open items]
1 by 1 … verify each other's job with Codex".

---

## 2. Current state

### Merged + deployed (`58202bb`, PR #187)

| Area | Change |
|---|---|
| `src/workers/nts_matcher_task.py` | Beat re-match window `_RECENT_DAYS` **45 → 180 days**. RCW 61.24.040: a notice of sale is recorded ≥90 (120) days before the sale and published 35–28 / 14–7 days before it, so the newspaper cache sees a lead's notice **55–150 days after recording**. 21 real Pierce leads had aged out. |
| `src/scrapers/pierce_wa_probate.py` | `ARMS_DOC_TYPE_LABELS` + `_grid_doc_type()`: pre_foreclosure rows store the REAL ARMS document type (NOTICE OF DEFAULT / NOTICE OF FORECLOSURE / LIS PENDENS / TRUSTEE SALE) instead of a flat `PRE-FORECLOSURE`. Only a TRUSTEE SALE can ever carry auction fields. |
| `src/scrapers/enrichment/pierce_atip.py` (NEW) | Pierce assessor (ATIP) address fallback for parcels no GIS layer has — in practice personal-property **mobile-home** accounts. reCAPTCHA-Enterprise-gated JSON API unlocked with a 2Captcha Enterprise token in the `recaptcha-response` header. **Address only — the taxpayer `name` is never read into the app** (RCW 42.56.070(8) boundary documented in the module). |
| `src/scrapers/enrichment/captcha.py` | `solve_recaptcha(..., enterprise=True)`; token cache keyed `(sitekey, site_url, enterprise)`; `invalidate_token` can narrow to one class. King caller unchanged. |
| `src/scrapers/enrichment/pierce_legal_repair.py` | Trailing `LT n BLK m` parsing (bounded block tokens), `parcel_repair_method()` = lot-suffix OR digit edit-distance 1, `legal_plat_adjacent()` guard. Edit-1 only for a SINGLE exact-legal survivor. |
| `src/workers/tasks_helpers/enrich.py` | Legal repair now runs for probate **and** pre_foreclosure; ATIP fallback after it; both extracted into **`pierce_address_recovery()`**. King mailing lookup gets a 200 s budget inside the 240 s kill-switch + try/except + `mailing_lookup_deferred` marker + 4-number warning, so skip-trace enqueue always runs. |
| `src/scrapers/enrichment/king_county_assessor.py` | `batch_enrich_king_county(parcel_ids, *, time_budget_s=None, stats=None)` — monotonic deadline checked before every fetch/navigation **and before launching Playwright**; returns PARTIAL results; `stats` carries requested / property_found / mailing_candidates / mailing_attempted / mailing_found / deferred / budget_exhausted. |
| `src/scrapers/sources/nts_tacoma_index.py` | 4 more real layouts: "at the hour of" between date and time, explicit weekday prefix, "10 o'clock" without minutes, TS-label word boundaries (prose "defaul**ts no**w" no longer yields a TS#), `Assessor's Parcel No.` label, `(Deed of Trust)`-tagged instrument as surrogate key. |
| `scripts/rerun_pierce_address_recovery.py` (NEW) | Re-runs `pierce_address_recovery()` for an existing job. `--dry-run` supported. Redis pub/sub is best-effort (unreachable from the dev box); recomputes owner flags like the worker does. |
| Tests (NEW) | `test_pierce_arms_doc_type.py`, `test_pierce_atip.py`, `test_captcha_token_cache.py`, `test_king_time_budget.py`, `test_nts_tacoma_layouts_2026_09.py` (+ 4 real fixtures `tests/fixtures/nts_tacoma_*.txt`), plus additions to `test_nts_matcher_task.py` and `test_pierce_legal_repair.py`. |
| Deleted | `tests/test_atip_enrichment.py`, `tests/test_atip_detail.py` — exploratory scripts (not pytest tests, no importers) that hardcoded a 2Captcha key. |

### Production data changes already applied (idempotent, fill-missing only)

- **Test 2** (`e72bd6bf`): 11 of 12 address-less rows filled (2 via legal repair, 9 via ATIP).
  `parcel_no_addr` 12 → 1; API `enriched_count` 202 → 213.
- **5 more Pierce jobs** (`3df1845a`, `1ec86fae`, `ba64bb1a`, `1e358ca8`, `32a76562`): 26 of 28 filled.
- **NTS cache**: one-off 40-page Tacoma crawl → 50/50 upserted, cache 55 → 61 rows, 16 active.
- **Matcher** run once with the 180-day window → **32 leads enriched** with auction data.

### Verification done

- 307 tests green on the merged tree (NTS, Pierce, King, workers, trustee-sale, GIS suites); ruff clean.
- CI on PR #187: Test + Dependency Audit **pass**.
- Live: full ARMS window diff (0 parser losses), ATIP 9/12, legal repair 2/2, Results page re-checked
  in Playwright Chromium (only the 3 name-only rows blank, no console errors).
- Codex: design consult + 4 diff reviews. Final gates PASS; every P2 adopted (plat adjacency, ATIP
  provenance, explicit weekday names, deed-of-trust-tagged instrument, pre-browser budget check).

---

## 3. Findings: source-gap vs app-bug (the audit's core answer)

| Symptom | Verdict |
|---|---|
| Auction Date / Default Owed blank on all 217 rows | **Source, on 09-02**: ARMS never carries them; they come from the newspaper cache, and none of Test 2's trustee sales had been published yet. |
| …but they would have stayed blank forever | **App bug** — the 45-day window. Fixed. |
| Pierce rows all labelled `PRE-FORECLOSURE` | **App bug** — the grid prints the real type per row. Fixed. |
| 12 rows parcel-but-no-address | **Source-layer gap** — Pierce personal-property MOBILE HOME accounts (tell: `heirs` = "… MHP LLC / MHC LLC / HOMEOWNERS COOPERATIVE"). Absent from Pierce GIS Tax_Parcels AND the WA statewide layer. Recovered via ATIP after the user authorised the captcha passer. |
| 3 rows name-only | **Source gap** — verified on the ARMS detail pages: the Legal Description tab has NO Parcel Id (2 TRUSTEE SALE, 1 LIS PENDENS). Kept as honest nulls. |
| 2 malformed parcels | **Recorder typos** — `9066600050`→`9066000050`, `718500090`→`7185000190`. Now repaired by the edit-1 rule. |
| "parcel but no NAME" the user saw | **NOT Test 2** — it is `test 10 - King Tax Delinquent` (384 rows, 0 names): King's Socrata feed has no owner column and owner lookup is blocked by the standing RCW 42.56.070(9) decision. |
| King job missing 172 addresses | **App bug** — the 240 s timeout. Fixed. |
| Live ARMS shows 235 vs 217 scraped | **Not a loss** — 12 intentional no-person drops + 6 filings indexed after the scrape. |
| Placeholder/dummy scan | **Clean** — 217 real instruments, no fake names/parcels, no cross-lead merges. |

---

## 4. Failed attempts / dead ends (do not repeat)

1. **ATIP without a browser or token** — plain `curl`/`requests` to `/api/pcAtipSummary` returns
   **HTTP 200 with an EMPTY body**. That is the reCAPTCHA rejection signal, not an outage. `[]` means
   "unknown parcel". Only an Enterprise token in the `recaptcha-response` header works.
2. **GIS legal-description lookup for the 3 name-only rows** — ambiguous (e.g. `PALMER LAKE L 28 B 5`
   exists in two subdivisions). Abandoned; never guess a property.
3. **First production run of the recovery script** died on `redis.railway.internal` (pub/sub is
   private-network only) *after* the legal-repair commit. Fixed with a best-effort publisher wrapper.
4. **Local rig start-up**: `pg_ctl -w start` and `nohup … &` inside the Bash tool hang or get killed;
   two competing `proxy6543.py` instances made port 6543 close every connection. Start ONE proxy via
   PowerShell `Start-Process -WindowStyle Hidden`.
5. **Full-suite pytest while a `railway run` is active** → 17 phantom auth/API failures. All pass in
   isolation. Quiesce the box before trusting a full run.
6. **Inline `python - <<'EOF'` heredocs for regex patches** — bash mangles backslashes (`\b` → backspace).
   Write the patch script to a file with raw strings instead.
7. `railway logs --service worker` does not reach far enough back for a job that ran hours ago; query
   the `job_logs` table instead.

---

## 5. Next steps

### Blocking on the user (decisions, not code)

1. **ATIP stance** — the assessor fallback is LIVE and takes only situs + mailing address, never the
   taxpayer name. Confirm this is acceptable (commercial workflow behind a captcha; RCW 42.56.070(8)).
2. **King tax owner names** — 384 rows ship nameless until the RCW 42.56.070(9) decision is lifted.
   See [[project_king_owner_names_gate_2026_07_30]] in memory.

### Deferred with reasoning (re-open only if the reasoning changes)

3. **Address match key has no ZIP for Pierce** (`address_match_key` = `street|zip`, Pierce GIS situs
   has no ZIP) → address matching is effectively dead there; parcel matching carries everything.
   A street-only key risks cross-city false attaches. Left alone.
4. **Legacy `PRE-FORECLOSURE` doc_type** on rows scraped before `58202bb`. New scrapes carry the real
   label; a backfill would need a re-scrape.
5. **Shared checkout** `C:\Users\Windows\OneDrive - Seattle Colleges\Desktop\web-scrapper-automation`
   is still on the stale `feat/fields-output-visibility` (merged as #107/#111, 70+ commits behind).
   Switch it to `main` only when no other agent is using it.

### Unverified / to watch

6. **King time budget in production** — no King tax job has run since deploy. The next one should log
   `King County mailing lookup stopped early: N parcels requested, …` instead of
   `Address enrichment failed`, and skip trace should enqueue. Verify with:
   `SELECT message FROM job_logs WHERE job_id = '<new king job>' ORDER BY created_at;`
7. **Test 2 auction fields** fill in as the Tacoma Daily Index publishes each sale (≈ 09/08 → late Nov).
   The 10:30 UTC crawl + 11:00 UTC matcher do this automatically now.
8. **1 unresolvable row** — `9009002080` (WELLMAN, no legal description) is not on file in Pierce GIS,
   the WA statewide layer, or ATIP. Correct as a null.

---

## 6. Environment + tooling cheat-sheet

- **Worktree used**: `C:\Users\Windows\bridgeleads-worktrees\test2-dq` (branch `fix/test2-data-quality`,
  merged; this handoff is on `docs/handoff-test2-2026-09-03`).
- **Prod read/write scripts**: write to the scratchpad with `sys.path.insert(0, "<worktree>")`, then run
  from the MAIN repo dir (Railway link is per-directory):
  `PYTHONUTF8=1 railway run --service worker python <script>`
- **Local test rig**: `bash C:/Users/Windows/bl-testenv/run-full-pytest.sh <worktree>`; or export
  `TEST_DATABASE_URL` / `TEST_DATABASE_URL_SYNC` / `DATABASE_URL` / `DATABASE_URL_SYNC` /
  `REDIS_URL` / `SECRET_KEY` / `STRIPE_SECRET_KEY` / `ENVIRONMENT=test` and run pytest directly.
- **Codex**: `codex exec "<inline code + questions>" -c 'model_reasoning_effort="high"' -c 'mcp_servers={}' --skip-git-repo-check < /dev/null` — never let it read files or run shell.
- **Re-run address recovery for any Pierce job**:
  `railway run --service worker python scripts/rerun_pierce_address_recovery.py <job_id> [--dry-run]`
- **Key source facts**: ARMS instrument search fields are `#cphNoMargin_f_txtInstrumentNoFrom/To`;
  ATIP summary API is `https://atip.piercecountywa.gov/api/pcAtipSummary?iParcelNumber=<digits>`;
  Pierce GIS is the ArcGIS `Tax_Parcels/FeatureServer/0` layer; the NTS cache table is `nts_notices`
  with natural key `(source, ts_number)`.

Related memory: `project_test2_pierce_prefc_dq_2026_09_02.md`,
`reference_pierce_arms_atip_verification.md`. Full narrative: `docs/BUILD_JOURNAL.md` (2026-09-02).
