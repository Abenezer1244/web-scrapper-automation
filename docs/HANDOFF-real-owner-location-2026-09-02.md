# HANDOFF — "real data everywhere": owner location (audit items 3 + 4)

**Written:** 2026-09-02, end of session `817RM2fq…`. Read this top to bottom before touching anything.
**Branch:** `feat/real-owner-location` · **worktree:** `C:/Users/Windows/bridgeleads-worktrees/real-owner-location` (off `origin/main`, includes a merge of #187)
**Open PR:** **#188** — CI GREEN (Test pass 3m59s, Dependency Audit pass). NOT merged.

---

## 1. The goal (user's words)

> "it should not assume the owner lives [at the property] … lets go with your recommendation on both … **i want real data everywhere**"

Two audit findings, both approved as **option B** (my recommendation, Codex-agreed):

* **Item 3** — enrichment wrote the property's own address into `mailing_address` whenever the real
  source didn't have one (an owner-occupied *assumption* stored as fact). Fix: never assume; leave
  NULL and recover the real value where a real source exists.
* **Item 4** — `property_address` is the assessor's **street-only** situs line and is the FROZEN
  dedup/billing + skip-trace key, so `property_state` was NULL on every lead, `out_of_state_owner`
  could never be computed, and `absentee_owner` could never be a confirmed False. Fix: add
  structured situs columns beside the frozen key and compute the flags from them.

**Standing rules for this work:** verify before concluding; root-cause fixes; Codex consulted on
every phase (design AND implementation) and independently verified; never fabricate data; isolated
worktree; test everything.

---

## 2. Where the work stands, phase by phase

| Phase | What | Code | Prod |
|---|---|---|---|
| 1 | No situs-as-mailing anywhere (statewide single+batch, generic GIS config w/o mailing fields, AI assessor, Regrid, Pierce ATIP) | ✅ in #188, Codex GATE PASS | n/a (deploys with #188) |
| 2 | `scripts/backfill_assumed_mailing.py` — rules S/K/P | ✅ in #188, Codex GATE PASS | **S+P APPLIED**; **K 180/217 DONE, ~37 LEFT** |
| 3 | migration 085 + `compute_owner_flags` opt-in parts | ✅ in #188, Codex GATE PASS | migration not yet deployed |
| 4 | Fill situs parts from real sources at scrape/enrich time | ✅ in #188, Codex GATE PASS after 2 fixes | n/a |
| 5 | `scripts/backfill_property_situs_parts.py` for existing leads | ✅ written + unit-tested in #188 | **NOT RUN** (needs 085 deployed) |

### Phase 2 production results (already written, do NOT redo)
* **Rule S (Snohomish, 18 rows):** mailing → NULL. No real mailing source exists for
  pre_foreclosure/trustee_sale there.
* **Rule P (Pierce, 1,272 candidates):** 74 refreshed from the live county row, 1,173 confirmed
  identical, 43 skipped (parcel absent from the county layer). `updated=1247, stale=0`.
* **Rule K (King, 217 candidates):** **6 batches × 30 = 180 rows applied**, every one
  `write / king_assessor_tax_bill`, `stale=0`. The background loop was **killed by the harness**
  (task `b90pdxq7a`), not by an error. Evidence files:
  `…/scratchpad/assumed_mailing_K_applied_{1..6}.jsonl`.
  Earlier dry-run of 30: 25 assessor values differed from the situs, 5 equalled it (real
  owner-occupied). Two rows moved to genuinely different owners (a Kirkland suite, a Puyallup street).

---

## 3. Files changed on this branch (all committed)

**Source**
* `src/scrapers/enrichment/county_gis.py` — statewide returns `mailing_address=None` + `_situs_parts()`
  (SITUS city/zip, state WA); generic config without mailing fields no longer copies the situs;
  `_situs_parts_from_confirmed_mailing()` (Pierce City_State/Zipcode **only** when
  `Delivery_Address == Site_Address` and not a PO box — `_PO_BOX_RE` covers `PO BOX`/`P.O.`/`P O B…`);
  `_arcgis_literal` quote-escaping (from the earlier PR).
* `src/scrapers/enrichment/ai_assessor.py`, `national.py`, `parcel.py` — removed the
  "no mailing found → use the property address" fallbacks.
* `src/scrapers/enrichment/king_county_assessor.py` — `mailing_lookup` provenance
  (`not_attempted|error|none|found`; `"none"` only when the rendered page is provably that parcel's
  tax-bill page or the explicit "No accounts") + `pace_s` param. **Merge note:** #187 added a
  `time_budget_s`/`stats` deferral system to the same function; I kept BOTH (theirs + my provenance
  and pace). Don't revert that merge resolution.
* `src/utils/address_intel.py` — `compose_situs()` + `compute_owner_flags(..., property_city=,
  property_state=, property_zip=)` **strictly opt-in** (no parts ⇒ byte-identical old behaviour).
* `src/db/models.py` — `Result.property_city` (128), `Result.property_zip` (10); `property_state`
  repurposed to the structured situs state.
* `alembic/versions/085_results_property_city_zip.py` — nullable ADD COLUMN ×2, revises 084.
  **Applied to the local rig already; NOT to prod.**
* `src/workers/tasks.py` — insert-time capture of the scraper's situs parts; end-of-job recompute
  passes the parts.
* `src/workers/tasks_helpers/enrich.py` — `_TRAILING_ZIP_RE` (anchored), `_keep_situs_parts()`
  (called BEFORE the GIS street-only line overwrites `property_address`), King trailing-ZIP fill,
  2-letter-state guard.

**Scripts**
* `scripts/backfill_assumed_mailing.py` — rules S/K/P, dry-run default, `--apply`, `--rules`,
  `--king-limit` (30), `--king-pace` (3.0 s), JSONL evidence, guarded `UPDATE … WHERE
  mailing_address = :old`, `enrichment_data.mailing_source` provenance.
* `scripts/backfill_property_situs_parts.py` — Phase 5; fill-only `COALESCE` update, evidence order
  notice → embedded → Pierce-confirmed → statewide; recomputes the flags.
* (already merged in #186) `scripts/backfill_pierce_statewide_mailing.py`,
  `scripts/repair_trustee_sale_from_notices.py`.

**Tests (all passing locally)**
`test_county_gis_batch_mapping.py` (rewritten for the new behaviour), `test_backfill_assumed_mailing.py`,
`test_owner_flags_structured_situs.py`, `test_situs_parts_capture.py`,
`test_backfill_property_situs_parts.py`. Last full related run: **134 + 34 passed, ruff clean**.

---

## 4. Root causes discovered (keep these; they explain the whole design)

1. The statewide parcel layer is **situs-only** — it has no owner mail data at all, yet the code
   built a mailing line from the situs + city + ", WA" + zip.
2. **The statewide copy pre-empted the real King lookup.** The King assessor pass only runs for rows
   with **no** mailing address, so once the statewide copy filled it, 217 King leads never got their
   real assessor mailing address. This is why Phase 1 must ship before/with the King recovery.
3. Evidence that the assumption was materially wrong, not merely incomplete: PO boxes, different
   streets, and out-of-state owners (Salem MA, Fort Mill SC, Greenville SC) behind rows that had been
   displayed as owner-occupied.

---

## 5. Failed attempts / dead ends (don't repeat)

* **`git merge --ff-only origin/main`** fails — the branch has its own commits; use a real merge.
* **Merging #187 conflicts in `king_county_assessor.py`** (3 hunks) and `tasks/todo.md`. Resolution:
  take THEIRS wholesale, then re-apply my `pace_s` + `mailing_lookup` edits programmatically. Already
  committed as `8a1272d`; don't redo.
* **`ruff` `S608`** on the f-string SQL in `backfill_assumed_mailing.py`: the `# noqa: S608` must sit
  on the **closing** `"""` line of the f-string, not the opening one.
* **`re` was missing** from `county_gis.py` and `enrich.py` when I added regexes — both fixed.
* **Codex on Windows:** a >30 KB prompt as an argv parameter dies with "Argument list too long";
  always feed the prompt via **stdin** (`codex exec … < file`).
* `railway variables --service <x>` prints nothing useful here; read the deployed commit with
  `railway run --service <x> python <script that prints os.environ>` or `railway deployment list`.
* Earlier in the session: `railway run` was **blocked by the auto-mode classifier** once; it worked
  after the user authorised it. If it's blocked again, ask rather than working around it.

---

## 6. NEXT STEPS — do these in order

1. **Merge PR #188** (`gh pr merge 188 --squash --subject "…(#188)"`). CI is already green.
   Then watch the main CI/CD run and confirm **`Run Migrations` succeeded** (that is migration 085
   reaching prod) and that Railway `api` + `worker` both show a fresh SUCCESS deployment.
2. **Finish rule K (~37 rows).** From the MAIN checkout (`…/Desktop/web-scrapper-automation`):
   ```
   railway run --service worker python C:/Users/Windows/bridgeleads-worktrees/real-owner-location/scripts/backfill_assumed_mailing.py --apply --rules K --king-limit 30 --report <scratch>/K7.jsonl
   ```
   Repeat until `candidates: 0`. Each run takes ~4 min (30 parcels × 3 s pace + Playwright).
   **Do not raise the pace** — King has IP-rate-blocked this app before. If a run prints
   `ABORT — King assessor unavailable`, stop and report; the source-health gate has tripped.
3. **Run Phase 5** (only after 085 is deployed):
   ```
   railway run --service worker python …/scripts/backfill_property_situs_parts.py            # dry-run
   ```
   Read the evidence JSONL, sanity-check a few `gained` rows against the source, then `--apply`.
4. **Verify in the live UI** (Playwright headless Chromium, creds `zowiegirma29@gmail.com` /
   `1212!Michael`, host `bridgeleads.io` NOT `app.`): open Test 3 (job
   `5db4a9c7-36aa-4426-ac73-b6ed9886dd0a`) and confirm mailing addresses are real and
   `absentee_owner` is now a real **False** where the county says mail goes to the property
   (Hill, Vicedo, Kallansrud, CN Foods) rather than blank/unknown.
5. **Journal + memory:** append a `docs/BUILD_JOURNAL.md` entry for items 3+4 (the 2026-09-02 entries
   above it cover the earlier phases) and update
   `…/memory/project_test3_pierce_auction_dq_2026_09_02.md` + `MEMORY.md`.
6. **Re-run the read-only prod stats** to prove the outcome:
   `<scratch>/ro_situs_policy_stats.py` — expect `property_state` no longer NULL everywhere and
   `absentee_false` > 0 outside King pre_foreclosure.

---

## 7. Still open, unrelated to this branch (from the same audit)

* Dependabot **#175** (alembic), **#176** (anthropic 0.52→0.120), **#177** (stripe 11→15),
  **#178** (playwright 1.61 — needs a prod canary like #180). **#174 (redis) was CLOSED** per the
  kombu landmine; never merge it.
* `feat/fields-output-visibility` is still checked out in the **shared OneDrive repo**; it was
  merged long ago as #107/#111 and reshaped by #128. Obsolete — do not re-merge, do not delete
  branches in that shared checkout.

## 8. Useful invariants

* `property_address` is **FROZEN** — it feeds `dedup_hash` (billing) and the skip-trace key. Never
  change its content; that's the whole reason items 3/4 were solved with side columns.
* Owner flags are tri-state: `IS TRUE` / `IS NOT TRUE` in SQL; `None` = unknown, never falsy checks.
* Local test rig: PG on 5432 + proxy 6543 + Redis 6379 are UP; env block is in every pytest command
  in this session's history. Only the first run after resetting BOTH stores is fully trustworthy.
