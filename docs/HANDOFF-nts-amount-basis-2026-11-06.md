# HANDOFF: trustee-sale silent done + NTS amount type marker (2026-11-06)

Branch for the next session: `docs/handoff-nts-amount-basis`. Worktree: `C:/Users/micha/bl-wt/nts-handoff`. It was cut from origin/main `93e9d026`.
All feature work is **merged and live**. What remains is post-deploy verification and owner-gated follow-ups.
Read `docs/BUILD_JOURNAL.md` first. Its two 2026-11-06 entries cover this work.

## 1. Goal (original mission)
"BRIDGELEADS: TRUSTEE-SALE SILENT FAILURE AND PRE-FORECLOSURE FINANCIAL DATA REMEDIATION".
- **A.** Pierce, King and Clark trustee_sale jobs reportedly finished `done` with `records_found` and the breakdown NULL since July.
- **B.** Principal Owing was present on only 875 of 8,323 pre_foreclosure rows (10.5%), and the landing page overstated coverage.

Constraints still in force:
- Never work on main.
- Codex consults before and reviews after every build.
- No deploy, prod write, paid enrichment or mass backfill without the owner's OK.
- Never bypass authentication, CAPTCHA or source terms.
- No em dashes in customer copy.
- Never invent metrics.
- Tests only against an isolated `_test` DB.
- Every query filters by user_id.
- At most 5 files per phase.

## 2. What we found (verified, read-only prod, 2026-11-06)

**Trustee sale**
- The "silent failure" is mostly a schema-history artifact.
  - 11 of 12 trustee_sale jobs since 2026-07-01 have NULL `records_found`. All 11 predate migration 099, which added the column.
  - Those jobs did save rows: Pierce 78, King 9, Snohomish 50, Clark 0.
- Real gap, now fixed: a failed `records_found` write was ignored, so a run could still finish `done` with a NULL count.
- The trustee_sale scrapers read the `nts_notices` cache, which newspaper crawlers fill. They never touch county portals.
- Clark is a source gap, not a scraper bug: the Columbian listing had 0 trustee sales, and the cache holds only 2 Clark notices.

**Amount coverage** (by distinct property)
| County | Properties with an amount | Share |
|---|---|---|
| All | 154 of 1,048 | 14.7% |
| Pierce | 87 of 554 | 15.7% |
| King | 25 of 394 | 6.3% |
| Snohomish | 42 of 43 | 98% |
| Clark | 0 of 27 | 0% |

- The gap is mostly "no notice in the paper we monitor".
- `default_amount` = Section IV `principal_owing`. The parser's unlabelled-first-figure fallback is a total, not principal. That is what the marker below fixes.

## 3. Changes made (all merged)
| PR | Repo / branch | What |
|---|---|---|
| #598 | BE `fix/trustee-sale-silent-done` | `records_found` write must succeed (retry once, else fail before save, reserve or bill). |
| | | Watchdog alert `job_done_without_count` (done + NULL count, last 7 days). |
| | | Matcher beat first runs `refresh_matched_from_notices` (pre_foreclosure leads follow their notice's restated amount or date; never blanks). |
| | | **Migration 127** `nts_notices.issue_date`: an older weekly-PDF issue only fills gaps. |
| #599 | BE `feat/nts-amount-basis` | **Migration 128** `nts_notices.principal_owing_basis` ('principal' / 'sum_owing' / NULL = unknown). |
| | | Parser `_section_iv_amount` returns (amount, basis). |
| | | Crawler pairs the basis with its amount; the resweep re-reads active amounts with unknown basis after the null-amount ones. |
| | | `enrichment_data.nts.amount_basis` is set on match, on refresh, and in the trustee_sale snapshot/finalizer. |
| | | CSV column `default_amount_type` (crm_v1: "Amount Type") appended LAST in every layout. |
| | | Lists/segment queries project `default_amount_basis`. |
| | | JSON-null `enrichment_data` no longer becomes an array. |
| #314 | FE `fix/principal-owing-copy` | Landing copy: a matched notice "can also carry the principal owing, trustee and TS number it states". |
| | | Batch check "Principal owing". |
| | | Fixed a date-bomb test (`lib/session-cookie.test.mjs`). |
| #315 | FE `feat/amount-type-label` | `lib/amount-type.ts`: only a 'principal' basis is called "Principal owing"; everything else is "Amount stated in notice". |
| | | "Amount Owing" column with the type under the figure; per-lead labels. |

**Labels:** "Principal" only when the notice labels the figure principal. A 'sum_owing' basis and an unknown basis both read "Amount stated in notice". There is no amount label when the amount is blank.

**Prod data write (owner-approved).** 12 stale pre_foreclosure `default_amount` values were synced to their notice principal.
- Keyed on `nts_notice_id`, with an exact-count guard. Afterwards, 0 stale rows remained.
- The before-values are in the old session's scratchpad (`stale_sync_apply.log`), which may be gone:

| Rows | County / notice | Before |
|---|---|---|
| 3 | King WA07000149-26-1 | 456249.92 |
| 8 | Pierce WA-25-1025270-BF | 254015.94 |
| 1 | Snohomish WA-26-1036687-BB | NULL |

**Deploy state at handoff (about 16:30 UTC)**
- Prod alembic = **128**.
- Railway api and worker deploys succeeded.
- Vercel Production = `32e10d3`.
- bridgeleads.io serves the new landing copy.
- **All 263 cached notices with an amount had `principal_owing_basis` NULL at deploy.** That is expected; it fills in as the crawl and resweep re-read active notices.

## 4. Failed attempts / gotchas (do not repeat)
- **Migration number collision.** Main's privacy work took 126 while I held 126, so I renumbered mine to 127. Always `git ls-tree origin/main alembic/versions` before numbering.
- **Force-push is blocked** by the auto-mode classifier ("Git Destructive"). To update a stale PR branch, merge main into it and push normally.
- **`gh pr merge` was blocked** ("Merge Without Review") until the owner explicitly said "merge".
- **Retargeting a PR base does not trigger CI.** Push a commit (merge main or master) to get the required checks.
- **I missed `tests/test_contact_residue_raw_sql.py`.** It holds byte-for-byte golden CSVs. Any new export column must update `GOLDEN_SEGMENTS_CSV` and `GOLDEN_COMBINED_CSV`. CI caught it.
- **Another Claude session** (cloud, not a local peer) pushed the same golden fix to #599 and merged it. Check the PR head SHA before merging.
- **`Result(enrichment_data=None)` stores JSON `null`, not SQL NULL.** `COALESCE(...)::jsonb || {...}` then builds an array, so guard with `jsonb_typeof(...) = 'object'`.
- **`results.enrichment_data` is a `json` column.** CASE branches must be cast back to `::json`.
- **Local tests need port 6543.** `src.db.session` rewrites `:5432` to `:6543`, so run a forwarder (asyncio TCP proxy, 127.0.0.1:6543 to 5432). The last one was killed for low memory.
- **Codex CLI** has to run from PowerShell: `$env:Path += ";$env:APPDATA\npm;C:\Program Files\nodejs"; ... | codex.cmd exec -s read-only -`.
- **Playwright MCP was not available.** The headless Chromium checks used Playwright from `C:\Users\micha\blvenv`.

## 5. Active files (for reference)
**Backend**
- `src/workers/tasks.py` (records_found guard)
- `src/workers/scheduler_helpers/health.py` (`_alert_done_without_count`)
- `src/workers/nts_matcher_task.py` (`_REFRESH_MATCHED_SQL`, `_write_match`)
- `src/workers/nts_crawler.py` (`_upsert_notice` basis pairing, `_RESWEEP_SELECT` order)
- `src/scrapers/sources/nts_tacoma_index.py` (`_section_iv_amount`)
- `src/scrapers/sources/nts_pdf_archive.py` (`issue_date_from_url`)
- `src/scrapers/trustee_sale.py`
- `src/workers/trustee_sale_finalize.py`
- `src/utils/lead_export.py` (`_amount_type`, layouts)
- `src/api/routes/segments.py`
- `alembic/versions/127_*`, `alembic/versions/128_*`
- Tests: `tests/test_nts_amount_basis.py`, `test_nts_issue_version.py`, `test_done_without_count_alert.py`

**Frontend**
- `lib/amount-type.ts`
- `app/(dashboard)/results/[id]/_components/{ResultsTable,LeadCards}.tsx`
- `app/(dashboard)/results/[id]/_columns.ts`
- `app/(dashboard)/batches/[id]/_components/{BatchLeadsTable,BatchLeadDetails,BatchOverview}.tsx`

## 6. Next steps (in order)
1. **Verify the basis is filling in** (read-only, the day after deploy). From the main repo dir, run `railway run --service worker <venv python> script.py`, where the script executes `SET TRANSACTION READ ONLY` and then:
   ```sql
   SELECT source, principal_owing_basis, count(*) FROM nts_notices
   WHERE principal_owing IS NOT NULL AND is_active AND auction_date >= current_date
   GROUP BY 1,2 ORDER BY 1,2;
   SELECT enrichment_data::jsonb #>> '{nts,amount_basis}' b, count(*) FROM results
   WHERE nts_notice_id IS NOT NULL GROUP BY 1;
   ```
   - Expect active notices to move from NULL to 'principal' or 'sum_owing' within about 1 to 2 crawl days.
   - If they stay all NULL, debug the crawl path: `notice_to_row` carries `principal_owing_basis`, and so does `_upsert_notice` `update_cols`.
   - Also confirm that no row in `jobs` with status 'done' has a NULL `records_found` (the watchdog alert should be quiet).
2. **Browser check** of the dashboard labels (Results "Amount Owing" plus its type tag, the lead card, the batch views) in a logged-in session. Use Playwright/Chromium, never Claude in Chrome. This has not been done yet.
3. **Owner-gated (do not start without the owner):**
   - **Source expansion.** The rights review found no King or Clark source that is clean to crawl.
     - DJC: robots.txt disallows `/notices/` and its ToS bars reuse.
     - WNPA wapublicnotices.com: ToU bars scraping, with liquidated damages.
     - Licensing is recommended. Clark options: Vancouver Business Journal, The Columbian (with permission), The Reflector PDFs.
   - **The owner said they will rebuild all sources later, and declined a Clark disclosure.**
4. **Housekeeping (optional):**
   - Delete the merged branches.
   - Remove the worktrees `C:/Users/micha/bl-wt/{ts-silent,nts-basis,fe-principal,fe-amount}`.
   - Drop the local DB `bridgeleads_tssilent_test`.

## 7. Known limits
- CSVs delivered before the deploy keep the figures and labels they shipped with.
- Notices with past sales and trustee_sale snapshots frozen before migration 128 keep an unknown basis forever, so they read "Amount stated in notice".
- An older issue can still fill a NULL basis only when it also supplies the amount (paired by design).
