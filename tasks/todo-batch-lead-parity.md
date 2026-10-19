# Batch lead table parity with single-scrape Results (2026-10-11)

Owner request: the Batch Detail lead table must carry every useful field a regular Results table
carries (Date, Party Name, Parcel ID, record-type fields, Property, Mailing, Lists, County, Phone,
Email), keeping every batch feature. Plan only; NOT started until the owner approves.

## Root cause (traced on BE main 2c7c37ed / FE master a23f88d)

| Field | Child results row | `_COMBINED_SQL` | `BatchLeadRow` (API) | FE `BatchLeadsTable` | Combined CSV |
|---|---|---|---|---|---|
| parcel_id | yes | selected (rep row) | **yes** | **dropped** | yes |
| mailing_address | yes | selected (rep row) | **yes** (blank if hidden field) | **dropped** | yes |
| phones / emails (split) | yes | selected | **yes** | merged into one "Contact" cell | phone..phone_3, email..email_3 |
| delinquent_amount / bill_year | tax rows | bucket `max()` | **missing** | n/a | yes |
| auction_date / default_amount | pre-fc / trustee rows | **rep row only** | **missing** | n/a | yes (rep row only) |
| code_violation type / case | enrichment_data | selected (rep row) | **missing** | n/a | yes |

So: parcel + mailing = **present in the API, omitted by the frontend** (the example GREER EMI row:
to confirm with a read-only query on that batch, see 0.2). Record-type fields = **omitted by the
batch API schema** (the SQL already selects them). Not a scraping, matching or persistence defect.

Real defect found on the way (needs a policy decision, see D1): a stacked property's
representative row (rn=1) is chosen by "has a contact" first. In the screenshot (Pre-foreclosure +
Probate) the probate row carries the contact, so it represents the lead and its NULL
`auction_date` / `default_amount` hide the sibling pre-foreclosure row's auction data. That
happens today in the combined CSV too, not just the table. Tax fields avoid it via bucket `max()`,
but `max(amount)` and `max(bill_year)` are independent, so two disagreeing tax rows in one bucket
could pair one row's amount with another's year (rare: needs 2 tax rows for one property in one batch).

## Codex PLAN r1: NO-GO (2026-10-11). Verified and folded in:
- ADOPTED P1: full field inventory. The API also exposes heirs, legal_description, doc_type,
  trustee, ts_number, violation status / description / last inspection, located-parcel source
  (Results shows them in its row expansion; needed by the details view).
- ADOPTED P1: the tax group becomes group-coherent whatever D1 says (independent `max()` can pair
  one row's amount with another row's year).
- ADOPTED P1: every field group has a written rule (source row, null behaviour, tie-break) and the
  API returns the source row id + record type per group.
- ADOPTED P1: CSV / API parity test compares a normalized projection (derived date, synthetic tax
  date, days_to_auction, decrypted contacts, hidden mailing, provenance), not raw SQL rows.
- ADOPTED P2: record-type columns are decided from batch-level `available_record_types` (+ the
  active filter), never from the rows of the current page, so columns do not jump between pages.
- ADOPTED P2: hidden mailing is blanked AFTER any coalescing; quality counts measure the final
  projection (automatic: the policy lives in the shared CTE).
- ADOPTED P1 (defence in depth): test that a user cannot read another batch's child jobs, nor
  another tenant's rows. (job_ids already come only from the owned run's child_job_ids.)
- REJECTED P1 "contact detection uses scalar phone / email only": deliberate and documented in
  `BatchLeadRow.skip_trace_status` (a sibling with arrays but no scalar never displaces the rep).
- KEPT AS-IS: identity / address group (party, parcel, property, mailing, contacts) stays on the
  representative row as one coherent group; documented and tested, not mixed across rows.
- DEFERRED P3: perf check on the largest real batch (EXPLAIN ANALYZE) is part of Phase 1 gates.

## Group policy (Phase 1, shared CTE, applies to table + CSV + quality)
- Identity / address / contact group: representative row (unchanged rn=1 rule).
- Tax group (amount, bill_year, billed, paid, account status): ONE tax_delinquent row in the
  bucket: newest bill_year, then has amount, then newest job, then id.
- Auction group (auction_date, default_amount, trustee, ts_number): ONE pre_foreclosure /
  trustee_sale row: has auction_date, then most auction fields non-null, then newest job, then id.
  (Only if D1 = coalesce; otherwise rep row as today.)
- Code-violation group: ONE code_violation row, newest job, then id.

## Decisions for the owner
- D1 (auction / tax conflict policy). Proposed: for each record-type field group, take the WHOLE
  group from ONE source row of that record type in the bucket (auction group from the newest
  pre_foreclosure/trustee_sale row that has an auction_date; tax group from the newest
  tax_delinquent row that has an amount), never mixing fields across rows. Applied in the shared
  SQL so the table, the CSV and the quality counts change together. CSV impact: no column change;
  cells that were blank for stacked leads become filled. Alternative: keep today's rep-row-only
  behaviour and only expose it (smaller, keeps the known blank).
- D2 (lead details view). Proposed: a row click opens a side sheet with all secondary fields
  (heirs, legal description, doc type, trustee / TS#, violation detail, all phones and emails with
  provenance, per-list sources). Alternative: inline row expansion like Results.
- D3 (search + sort). The batch table has NEITHER today (only filters + pagination). Adding them
  means server-side params on `/batches/{id}/leads` (the set is paged in SQL). Proposed: search
  (name / parcel / address) + sort by date and by auction date, server-side. Could be a later phase.

## Phases (each <= 5 files, its own PR, owner approval between phases)

### Phase 0: verify + consult (no code)
- [x] 0.1 Codex consult on this plan; fold findings in (r1 NO-GO, folded above)
- [x] 0.2 Read-only prod query (2026-10-11): the example property has 2 rows in the batch run, a
      probate row (rep: parcel, property, mailing, phone, email all present, contact reused) and a
      pre-foreclosure row (parcel / addresses present, NO auction data: no NTS match). So the
      screenshot's missing parcel / mailing = FE omission only; its blank auction is TRUE (the
      probate-rep hiding case is real in general, not for this lead)
- [x] 0.3 Owner decided: D1 per-group source row; D2 side sheet; D3 search + sort a later phase

### Phase 1: BE API parity (feat/batch-lead-parity, BE worktree bl-wt/batch-parity-be)
Files: `src/workers/batch_export.py` (only if D1 = coalesce), `src/api/schemas.py`,
`src/api/routes/batches.py`, a test file, `openapi.json`.
- [ ] Add to `BatchLeadRow`: delinquent_amount, delinquent_bill_year, auction_date,
      default_amount, days_to_auction (derived, same clock as the CSV), code_violation_type,
      case_number, located-parcel provenance; each source-attributed if D1 = coalesce
- [ ] D1 group-coherent coalesce in the shared CTE (+ quality SQL follows automatically)
- [ ] Tests (real test DB): parcel / property / mailing / split contacts returned; stacked
      pre-fc+probate lead carries the pre-fc auction group; conflicting tax rows never mix;
      hidden mailing still blank; already_delivered unchanged; reused contact + skip OFF makes no
      Tracerfy call and no credit row; another tenant's rows never appear; combined CSV row equals
      the API row field-for-field for the shared fields
- [ ] Regenerate openapi.json (requirements.txt venv), §14 x2, Codex diff gate, PR

### Phase 1 status (2026-10-12): BUILT, local commits only, NOT pushed
- [x] Group source CTEs (tax / auction / violation) + apply_group_sources (CSV + API share it)
- [x] BatchLeadRow parity fields + source ids; hidden heirs / legal / mailing blanked
- [x] Tests: 8 new + 1 intended expectation change; RED proven on main's code for each
- [x] ruff clean; openapi.json regenerated, additive only (0 deletions), --check OK
- [x] Full suite, per file (machine at ~3.8 GB free, so the batched run was memory-killed and
      slow): all pass except 3, each reproduced on the untouched base 2c7c37ed or passing in
      isolation: test_claim_transfer.py does not finish in 9 min (same on base);
      test_contact_lookup_quote::test_a_quote_redis_cannot_store_is_never_shown (same on base,
      needs a write-refusing Redis); test_contact_cache_headers 429 test (passes alone, timing)
- [x] Codex diff r1 GATE: FAIL (P1 no-source group kept the rep row's keys; P2 enrichment-only
      tax rows; P2 JSON-null nts) -> all fixed + tested; P2 perf not adopted (each DISTINCT ON
      sorts one record type's rows). Codex r2 GATE: PASS
- [x] §14 self-review x2, clean: tenant scope inherited from `candidates` (user_id + job_ids on
      every row; new CTEs read only candidates); no new endpoint; RLS session unchanged;
      no-store unchanged; hidden fields blanked after the fold in API and CSV; CSV values still
      pass sanitize_for_csv in build_lead_export_row; enrichment_data is the owner's own data,
      already returned by the Results API; no provider call on view / download (tested)
- [ ] EXPLAIN ANALYZE of _COMBINED_SQL on the largest real batch: owner approved 2026-10-12, but
      the session's permission classifier blocked the prod read; script left for the owner to run
      (scratchpad explain_batch_parity.py: main vs branch, page / full CSV / quality, 3 runs each)
- [x] PR + merge: owner approved 2026-10-12 ("Run proceed open merge")

### Phase 1b: combined CSV column parity (found while testing Phase 1)
The combined CSV (OVERLAP_LEAD_COLUMNS) omits 21 per-job columns, incl. trustee, ts_number,
code_violation_*, tax_billed / paid / account_status, months_delinquent, wa_foreclosure_eligible,
freshness_days, contactability_score, absentee / out_of_state / owner_state, current_owner,
title_status, assessed_value, instrument_number, parcel_source. trustee / ts_number were left out
on purpose ("blank on most rows"); the owner's parity requirement supersedes that.
- [x] Owner approved 2026-10-12. NOT appended to OVERLAP_LEAD_COLUMNS: segments (Lists) also
      write it, and their SQL selects no enrichment_data / owner flags and applies no group
      sources, so the new headers would print empty there, or a probate "status" as a violation
      status. New BATCH_LEAD_COLUMNS = OVERLAP_LEAD_COLUMNS + the 21, spelled out (Codex P2: no
      ordinal drift if LEAD_CSV_COLUMNS changes); only batch_export passes it. crm_v1 is a
      per-job layout, not used by the combined writer. Tests: values on the stacked lead + header
      prefix / set guard. 336 batch + export tests and 321 batch / export / segment tests pass.
      Codex r1 FAIL (P1 rep-row fields: rejected, each is group-sourced or the rep row's like
      filed_date; P2 ordinal: adopted), r2 FAIL (sent a partial diff), r3 PASS. §14 x2 clean
- [ ] Pre-existing, not ours: `import src.utils.lead_export` alone raises a circular ImportError
      via src.api.routes.batches (reproduced on main 8269998f); note only

### Phase 2: FE table parity (bridgeleads-web, own worktree from master after Phase 1 merges)
Files: `BatchLeadsTable.tsx`, a shared `lead-columns` helper (extracted only where Results already
has the logic: date label, parcel display, money, auction countdown), `lib/types` regen,
`page.tsx` (only if needed), and nothing else.
- [ ] Column order: Date | Party Name | Parcel ID | [record-type cols present in this page's
      lists] | Property Address | Mailing Address | Lists | County | Phone | Email
- [ ] Record-type columns appear only when the view holds that record type (same rule as Results'
      hasTaxData / hasAuctionData), never an all-empty column
- [ ] Reuse PhoneCell / EmailCell / PartyName / MailingValue / CopyButton / AuctionCountdown;
      provenance line under Phone (kept wording); sticky Party Name column; horizontal scroll cue
- [ ] Mobile (< xl): lead cards with the same fields grouped (identity, property, lists, contact)
- [ ] Coordinate with the UX 3.15a session (it edits page.tsx / BatchOverview / BatchLeadFilters
      for number formatting): rebase on it, no overlapping hunks

### Phase 2 status (2026-10-12): BUILT, FE commit b8f452b (bl-wt/batch-parity-fe), NOT pushed
- [x] Columns, scope, shared helpers, sticky Party Name, mobile cards (all items above)
- [x] tsc, eslint, test:unit 24/24, next build --webpack (Turbopack refuses the junctioned
      node_modules); 44 Chromium checks at 1440/1280/768/390 on the stub rig, ALL PASSED
- [x] Codex r1 FAIL: P2 cards ignored the record-type scope (fixed + browser check), P3 status-only
      violation hidden on cards (fixed), P2 date label from page rows REJECTED (on master already,
      deliberate: a stable "Notice Date" would mislabel an overlap's probate date). r2 PASS
- [x] Types came from FE #275 (identical regen); rebased onto 5159c8f, gates re-run, pushed as
      FE PR #276 (owner approved push + PR 2026-10-12), CI check PASS. MERGED 2026-10-12 as f980ee43 (owner "merge now"); master CI
      PASS; Vercel production READY on f980ee43 (via vercel api, no GitHub status posted)

### Phase 3: details view (D2) and Phase 4: search + sort (D3), each its own PR if approved

### Phase 3 status (2026-10-19): BUILT, FE commits 6e0accf + 393f907 on feat/batch-lead-details
(bl-wt/batch-parity-fe, rebased on master a11258d), NOT pushed
- [x] Side sheet (Radix dialog): row click (skips buttons / links / text selection) or the row's
      details button; mobile cards get "View details"; focus returns to the opener; closes when rows
      change. Shared helpers moved to lead-fields.tsx (table, cards, sheet)
- [x] Laptop died mid-session 2026-10-12 with the work uncommitted; recovered intact. Found a TDZ
      crash in the unfinished edit (violation description / inspection read before declared: a
      description-only violation lead crashed the sheet); fixed + a stub row + browser check
- [x] Gates: tsc, eslint, test:unit 24/24; Chromium on the stub rig: details 32/32, table 43/43
      (1440 / 1280 / 768 / 390)
- [x] Codex r1 FAIL: P1 "group extras read from the rep row" REJECTED (apply_group_sources, BE
      #508, already folds tax / violation / nts keys from each source row; assessor = identity
      group by policy), P3 stale open state fixed. r2 PASS + P2 trustee / TS # from nts missing:
      fixed + check. r3 PASS; its P2s rejected (render-phase reset is React's documented
      prop-change pattern; text() already returns only strings / numbers)
- [ ] Follow-up, not in scope: assessed value renders raw ("412000"), same as Results; format in
      both together if wanted
- [x] Pushed + FE PR #279 opened 2026-10-19 (owner "open push merge"); CI + Vercel preview PASS.
      Merge BLOCKED by the session's auto-mode classifier ("Merge Without Review"): owner merges

### Phase 4 plan (D3: search + sort, server-side), owner "then phase 4" 2026-10-19
BE branch feat/batch-lead-search (worktree bl-wt/batch-search-be, from main a3bb2c37).
Files: `src/workers/batch_export.py`, `src/api/routes/batches.py`, a new test file, `openapi.json`.
- [ ] `q` (str, <= 100 chars, trimmed; blank = no search) and `sort` (Literal relevance | date_desc
      | date_asc | auction_asc | auction_desc, default relevance) on BOTH leads endpoints
- [ ] Search matches the REPRESENTATIVE row's shown fields (what the table shows): party_name,
      parcel_id (compared alnum-only, so "0419-12-3456" finds 0419123456), property_address, and
      mailing_address ONLY when the batch does not hide it. Case-insensitive `strpos` on lower(),
      never LIKE, so % and _ are literal (no escaping to get wrong). Phones / emails are encrypted:
      not searchable (stated, not hidden)
- [ ] Search lives in the shared `_VIEW_FILTERS`, so the page and its pager use one predicate.
      `_FILTERED_TOTAL_SQL` counts rn=1 representative rows (ranked JOIN agg) instead of agg alone,
      so a search on rep-row fields is countable; one row per bucket either way
- [ ] Sort: CASE keys on a `:sort` bind placed BEFORE today's ORDER BY (which stays as the
      tie-break, then id). A NULL / relevance bind leaves every CASE NULL = today's order exactly,
      so the combined CSV is unchanged. Date key = the date the table SHOWS: `filing_date_sql('rk')`
      (Results' parser), NULL for an auction-date stand-in (`auction_date_fallback_sql('rk')`) and
      for the synthetic tax date when rk is its own tax source (FE `leadDate` rule). Auction key =
      the auction group's `src_auction_date`. NULLS LAST in both directions
- [ ] CSV caller (`batch_export.py:439`) binds the new params as NULL; existing tests updated
- [ ] Tests (real test DB): each searchable field; dashed parcel; hidden mailing NOT searchable;
      %/_ literal; no-match total 0; total == rows across pages; search + filter combined; date sort
      both ways, stand-in + synthetic tax dates sort as undated, nulls last; auction sort; another
      tenant's same-name lead never returned; run-scoped endpoint; CSV order unchanged
### Phase 4 BE status (2026-10-19): BUILT on feat/batch-lead-search
- [x] Codex PLAN r1 FAIL, 4 P1s folded in (punctuation-only needle, hidden mailing in rows AND
      total, tenant scope on every path, total == rendered rows); P3 FE races: React Query keys
      every response by its query, so a stale answer never lands on the current key
- [x] q + sort on both endpoints; view_filter_params() is the ONE bind builder (API, CSV, tests),
      so no caller can miss a bind; the CSV binds all-NULL = today's file, tested
- [x] 20 tests on the real test DB; RED proven (main route: 19 fail; no punctuation guard: 1;
      mailing always searchable: 1). 656 batch / export / segment / csv tests pass; ruff clean;
      openapi additive only (+76), --check OK
- [x] 20k-row seeded batch (isolated test DB): pages 0.2-0.8 s, totals 0.1-0.4 s (filtered total
      71 -> 257 ms, it now ranks). A first run with no ANALYZE hit the 120 s statement timeout,
      and main's own page 1 took 12 s once on the same data: pre-existing plan sensitivity, note
- [x] Codex diff r1 PASS (P3 CSV-endpoint test: covered by the existing CSV golden tests; P3
      run-scoped twin: added). §14 x2 clean
- [ ] Not searched (stated): located parcel (Python tier rules), phones / emails (encrypted)

FE (worktree from master after #279 merges): `lib/api.ts`, `page.tsx`, `BatchLeadFilters.tsx`,
`BatchLeadsTable.tsx`, types regen.
- [ ] `q` + `sort` in the URL like the filters; search box debounced, resets to page 1; clear button
- [ ] Sortable Date / Auction Date headers (button + aria-sort + arrow); a Sort select on cards
- [ ] Empty state "No leads match …" with clear search; Chromium checks 1440 / 1280 / 768 / 390

## Verification (every phase)
- BE: pytest on the guarded test DB (TEST_DATABASE_URL, never prod), ruff
- FE: tsc, eslint, `next build`, `test:unit`; Playwright on Chromium (stub API rig, never Claude in
  Chrome) at 1440 / 1280 / 768 / 390: every column present, filters, pagination, stacked, delivered,
  reused-contact provenance, empty fields, mobile cards
- §14 security review twice; Codex diff gate until PASS; no push / merge / deploy without approval

## Review
(filled at the end)
