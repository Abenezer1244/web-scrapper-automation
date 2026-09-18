# Plan: make "N already delivered" inspectable (Results detail)

Date: 2026-09-17. BE worktree `C:/Users/Windows/bl-wt-delivered` (branch `feat/already-delivered-view`
off `origin/main` 8e1d40e). FE worktree `C:/Users/Windows/bl-wt-delivered-fe` (same branch name,
off `origin/master` 66e792f). Builds on the D1-D5 work (`docs/HANDOFF-results-categories-2026-09-13.md`
section 8 step 7 is this exact UX, never built).

## Investigation findings (verified 2026-09-17, code + READ-ONLY prod query)

**Definition.** A row is "already delivered" when this job's
`INSERT INTO delivered_records ... ON CONFLICT (user_id, dedup_hash) DO NOTHING` lost, i.e. the SAME
user already holds a claim on the frozen `sha256(parcel|address)` key (`legacy_strong_signature`,
else weak NAME|DATE). Stamped at classification time: `is_duplicate=true`,
`duplicate_reason='prior_run'`, `duplicate_source_job_id/_at` = the claim holder's job and claim time
(`src/workers/tasks.py` dedup step 3). Scope: per user, any county, any record type, no expiry.
Header number = `duplicate_count - same_run_duplicate_count` = actionable, not superseded,
reason prior_run or NULL (pre-089). Answer to the A-G question: **G**, "an earlier run of THIS
account holds the claim on the same property key". Closest to A, but not "returned to you" in a
strict sense (see the 1 row below).

**Screenshot run** = job `adde2898-60f2-4b08-808b-c1d836bd63aa`, Pierce WA pre_foreclosure, user
`b6d2095d` (Agency), finished 2026-09-16 06:08:48 UTC (Sep 15 11:08 PM PT). new=3 (record_count 3),
prior_run=227, same_run=0.

**Proof of the 227 (prod, read-only):**
- 227 rows, 227 distinct ids, **225 distinct property hashes** (2 properties appear as 2 filings
  each in this run; each filing counts). 0 NULL hash, 0 NULL source, 0 rows owned by another user.
- 227/227 have a claim held by THIS user; claim holder job == stamped source job for 227/227.
- 210 of the hashes are ALSO claimed independently by other users. 0 rows lack an own-user claim
  while another user holds one. **No cross-tenant influence.**
- 16 source runs, all this user's Pierce pre_foreclosure runs (Apr 13 .. Sep 14). 10 still exist
  (done) = 202 rows; **6 were purged (retention) = 25 rows**, provenance date survives, link cannot.
- Delivery proof: 201 rows have a visible (non-dup, actionable) original in the source run; 1 has a
  non-dup original that is NOT actionable (never a visible lead there, a pre-cutoff D1 case); 25 have
  no original because the run is gone.
- Schema-wide: 0 dup rows sourced from another user's job; 0 results rows on another user's job;
  0 claims anchored to another user's row.
- Enrichment on the 227: all carry property + mailing address; 1 carries a copied skip-trace hit,
  226 `not_attempted`.

**Combined** = `duplicate_reason='same_run'`: other filings of a property that ALSO appears as a new
lead in this same run (collapse elects one survivor so the property bills once). The survivor is in
the New list. No loser->survivor pointer column (siblings share `dedup_hash` + job).
Decision: **no Combined tab in this change** (rows are not leads the user lacks; the survivor is
already listed). Recommendation recorded for later: show "Combined from N filings" in the
survivor's expanded row.

**Export today:** `/jobs/{id}/download` = new (non-dup) + actionable + tax cap (+ view filters),
never search. No billing, no quota, no skip trace in the download path.
**Quota/skip trace:** billing happens only in the worker; duplicates never bill; the dispatcher
buys only for non-dup rows of `done` jobs. GET routes write nothing.

## Design

Backend (`src/api/routes/jobs.py`, `src/api/schemas.py`):
- `category: Literal["new","already_delivered"] = "new"` on `GET /jobs/{id}/results`,
  `/export-url`, `/download`. Anything else is 422. `already_delivered` swaps
  `is_duplicate = false` for `is_duplicate AND coalesce(duplicate_reason,'prior_run')='prior_run'`.
  Every other predicate (job+user scope, actionable, tax cap, search, sort, pagination, view filters)
  is shared, so search/sort/paging work server-side unchanged.
- `already_delivered_count` on ResultsPage: the unfiltered category predicate (same as the list,
  incl. tax cap) so the tab number == the list total. Header uses it.
- Per-row provenance on ResultRow (null on new rows): `duplicate_source_job_id`,
  `duplicate_source_at`, `duplicate_source_available` (source job exists, same user, `done`,
  ONE batched query per page, no N+1).
- CSV: category carried through export-url -> download; filename marks the delivered set.
- OpenAPI regenerated with pinned deps; zero deletions.

Frontend (`bridgeleads-web`):
- `?view=delivered` in the URL (existing `router.replace` convention from batches/[id]); default New.
- Header "227 already delivered" becomes a text-styled button that selects the view.
- Segmented tablist above the table: `New 3` / `Already delivered 227` (only when > 0), keyboard
  arrows, `role=tablist/tab`, `aria-selected`, horizontal scroll at 320px, no wrap.
- Same ResultsTable; row badge `NEW` vs `DELIVERED`; expanded row shows
  "First delivered Sep 12, 2026" + "View original run" (only when available) or
  "The original run is no longer available" or "Delivery date not recorded".
- Download label follows the view: "Download new CSV" / "Download delivered CSV".
- No em dash in new copy.

## Codex plan consult (2026-09-17, GATE: FAIL -> reconciled)

- [P1] "First delivered" overclaims: `duplicate_source_at` is CLAIM time. ADOPTED. Per page, one
  batched query also checks whether the source run still holds the visible original lead
  (same user, same hash, non-dup, actionable). Copy: original visible -> "Delivered by your run
  on {date}" + "View original run"; run exists but original not visible -> "Matched your run on
  {date}" + link to the run; run gone -> "Matched your run on {date}. That run is no longer
  available."; NULL source -> "Matched an earlier run. The date was not recorded."
- [P1] export-token IDOR. ADOPTED as a verification: confirm the download token binds user+job and
  the path job must equal the token job; add cross-tenant `/export-url` + `/download` tests.
- [P2] unfiltered badge vs filtered total. As designed: badge = unfiltered category count, list
  `total` = filtered; pagination reads `total`.
- [P2] category not signed into the token. REJECTED: same user, same job, data the user can already
  list; view filters are unsigned today by design; the in-app flow uses the bearer header.
- [P2] NULL legacy reasons. VERIFIED in prod: 33,927 NULL-reason actionable dups, 0 on trustee_sale
  (the only pre-089 same-run collapse path), and pre-089 code could only flag a row by losing a
  PRIOR claim. Treated as prior_run with "date not recorded" copy.
- [P2] weak NAME|DATE hash. Covered by "Matched" wording unless the original is proven.
- [P2] assert no writes / no skip trace from viewing. ADOPTED in tests.
- [P3] `?view=delivered` with 0 rows. ADOPTED: normalize back to New.

## Todo

Phase 1 (backend, <= 5 files): jobs.py, schemas.py, new tests file, openapi.json
- [x] Codex pressure-test of this plan; fold in findings
- [ ] category param + predicate helper on results/export-url/download
- [ ] already_delivered_count + per-row provenance (batched)
- [ ] tests: new-only, mixed, delivered-only, zero, exact count, tenant A/B (worker dedup + API),
      direct cross-tenant 404, invalid category 422, search/sort/paging in category, provenance
      available / purged / foreign source id, previously enriched row, no quota/skip-trace mutation,
      CSV scope per category
- [ ] ruff, targeted + full suite (isolated DB), OpenAPI regen (zero deletions)
- [ ] Codex review + security §14; fix; STOP for owner approval
Phase 2 (frontend): api types regen, api.ts, page.tsx, ResultsTable.tsx (+ small tabs component)
- [ ] implement, tsc + eslint, Playwright CLI Chromium at 320/375/390/430/768/1024/1440
- [ ] Codex review; journal entry; PRs (BE first, FE after BE merges)

## Review
(filled at the end)
