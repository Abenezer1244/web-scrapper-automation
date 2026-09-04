# Test 8 (King WA · pre_foreclosure) — data-quality audit

Branch: `fix/test8-data-quality`  ·  Worktree: `C:\Users\Windows\bridgeleads-worktrees\test8-dq`
Base: `origin/main` @ `88ba03f`  ·  Job: `5178ce6c-db28-4b53-8441-887795c89c52` (155 results)

## What Test 8 is

King County WA, `record_type=pre_foreclosure`, scraped from the King County Recorder
LandmarkWeb index (`enrichment_data.source = "king_landmark_json"`), window
2026-06-04 → 2026-09-01. Every row's `doc_type` is `NOTICE OF TRUSTEE SALE`.

## Established by measurement (not assumption)

| # | Finding | Evidence |
|---|---------|----------|
| F1 | All 155 rows: `auction_date`, `default_amount`, `nts_notice_id`, `nts_match_confidence` all NULL | prod query |
| F2 | The two fields are written by ONE `UPDATE` in `_write_match` (`src/workers/nts_matcher_task.py:213`) — never independently | code + prod: `auction_only=1, owed_only=0, both=200` |
| F3 | API + UI are correct. `has_auction_data=true` for pre_foreclosure; columns render; `—` is the null state | Playwright against prod |
| F4 | King's only wired auction source is the Queen Anne & Magnolia News weekly legals PDF | `src/workers/scheduler.py:187` |
| F5 | Full 18-issue archive 2026-05-06→2026-09-02 (all 18 fetched OK) = **36 distinct King notices**. Exact-parcel overlap with Test 8's 155 leads = **1** | live re-parse |
| F6 | `nts_notices` holds 14 King rows; last insert + last `fetched_at` = 2026-08-06. Issues 08-12/08-19/08-26/09-02 hold 3–5 parseable notices each and were never ingested | prod + live fetch |
| F7 | **12 of 14 cached King rows carry a `ts_number` belonging to a different notice in the same PDF**; **2 also carry a wrong `auction_date`** | re-parsed each row's own `source_url` |
| F8 | `OVERLAP_LEAD_COLUMNS` omits `auction_date`/`default_amount` although `batch_export.py` SELECTs them | `src/utils/lead_export.py:536` |

### F7 detail — wrong stored auction dates

| parcel | stored | truth (same PDF, current parser) | effect |
|---|---|---|---|
| `6385500350` | 2026-06-26 | **2026-09-18** | live auction stored as expired → `is_active=false` → invisible |
| `211101002007` | 2026-07-10 | 2026-08-28 | was live for ~1 month, stored as expired |

Residue of the split/label-bleed bug fixed in #195/#199 whose data repair covered
Pierce + Snohomish but never King.

## Source vs BridgeLeads classification

- **A (source had it, BridgeLeads lost it): 1 / 155** — `KIM MYONG HEE`, parcel
  `1112630120`. Notice `WA09000059-24-1`, auction 2026-08-07, owed $397,621.29, cached
  since 2026-07-30. Not attached: the auction had already passed when the job ran
  (2026-09-02) and the matcher only considers `is_active AND auction_date >= today`.
- **B (this source genuinely has no notice): 154 / 155.** The paper carries ~2 King
  trustee sales/week; King records ~50/month. Coverage ceiling ≈1%. **Leave NULL.**
- The recorded NTS *document* does contain both values by RCW 61.24.040(1)(f), but
  BridgeLeads reads only the recorder's *index*, never the document.

## Root causes

- **RC1 (shared).** Auction Date and Default Owed are the same defect, not two — both
  come from one matched notice in one UPDATE. For Test 8 no match ever occurred.
- **RC2.** Coverage: the only wired King auction source overlaps 1/155 leads. Not a bug.
- **RC3 (bug).** The crawler reads only the single "current issue" PDF; a missed or
  failed week is lost permanently. 4 weeks lost since 2026-08-06; 22 of 36 archive
  notices were never cached. The archive *is* reachable by constructed URL for King
  (18/18). *(Snohomish too — my initial 3-of-4 404s were a wrong filename guess; the
  paper uses "Legals - M-D-YY.pdf" for the back catalogue and "Legals M-D-YY.pdf" from
  September, and the shipped backfill script already knew the first.)*
- **RC4 (bug, highest severity).** 12/14 cached King rows have a wrong `ts_number` and
  2 have a wrong `auction_date`, suppressing a live 2026-09-18 King auction.
- **RC5 (bug, separate path).** Batch/segment/overlap CSVs silently drop both fields.

## Plan

- [x] 1. `nts_crawler`: bounded weekly-archive sweep, tolerating 404s, idempotent via
      the existing `(source, ts_number)` upsert. Recovers lost weeks and self-heals
      future misses. *(Revised during implementation: PR #200 already shipped
      `scripts/backfill_nts_pdf_archive.py` with filename builders for BOTH papers — it
      was simply never run. So the sweep reuses that map rather than a King-only template
      of my own, and covers Snohomish too. My earlier "Snohomish's naming is not
      derivable" was wrong: my probe guessed the wrong spelling, and the paper uses two.)*
- [x] 2. `repair_nts_ts_number.py`: also correct `auction_date` / `principal_owing`
      from the re-parsed truth (today it only rewrites `ts_number`), so RC4's wrong
      dates are fixed, not just the keys.
- [x] 3. `lead_export.py`: include `auction_date` / `default_amount` in the
      overlap/batch/segment column set (RC5).
- [x] 4. Regression tests keyed on source structure, not on Test 8 values.
- [x] 5. Full pytest + lint; Codex diff review; browser re-verify.

## Explicitly NOT doing

- No fabricated or inferred auction dates. No mapping of `date_recorded` (a recording
  date) onto Auction Date.
- No Test 8-specific hardcoding.
- Not building recorded-document ingestion. *(Scoped 2026-09-04 — see the addendum in
  `docs/scoping-king-nts-coverage-2026-09-03.md`. The reason is NOT cost: unofficial
  images are free, and only certified copies are paid. King County LandmarkWeb's terms
  prohibit "high-volume, automated" access and "Data Mining (mass downloading) of
  images" outright, and King has already IP-rate-blocked this project once. The
  reCAPTCHA/dead-2Captcha-key issues compound it but are not the decisive blocker.)*

## Review

**PR #209** (`fix/test8-data-quality`), CI green, **merge blocked by the auto-mode
classifier — awaiting the user**. FE needed no change: `origin/master` already renders a
past sale as a muted "N days ago" (my first read was of a stale working tree).

### Verdict on Test 8

Test 8 **passes** the data-quality audit. Party names, parcel IDs, property addresses and
mailing addresses are real, correctly associated, and match the recorder's index (153/155
carry a property address; 155/155 a mailing address). Phone/email are empty because
skip-trace was still queued, not because of a defect. Auction Date and Default Owed are
legitimately NULL on 154 rows and were recoverable on exactly 1.

### Auction Date and Default Owed: ONE defect, not two

Both are written by a single `UPDATE` in `_write_match` from a single matched notice
(prod-wide: auction-only 1, owed-only 0, both 200). No match ever occurred on Test 8, so
both were NULL together. Not two independent bugs, and not an API, serialization,
timezone, or frontend problem — all of those were checked and cleared.

### Source vs BridgeLeads

- **A — source had it, we lost it: 1/155.** `KIM MYONG HEE`, parcel `1112630120`; notice
  `WA09000059-24-1`, auction 2026-08-07, $397,621.29, cached since 2026-07-30.
- **B — source genuinely lacks it: 154/155.** Left NULL. Nothing inferred from
  `date_recorded`.
- The recorded document *does* contain both by RCW 61.24.040(1)(f), but King's own terms
  prohibit the bulk automated access that would take (see the 2026-09-04 addendum in
  `docs/scoping-king-nts-coverage-2026-09-03.md`).

### Fixed

1. Weekly-archive self-heal in the beat (both Pacific Publishing papers), with the URL
   map extracted to `src/scrapers/sources/nts_pdf_archive.py` and shared with the
   operator backfill — which had drifted and could no longer reach Snohomish's September
   filenames.
2. `repair_nts_ts_number.py --fields` and `--retire-wrong-key`; `--results` parcel join
   normalized (it had been skipping King's rows outright).
3. `auction_date` / `days_to_auction` / `default_amount` restored to the batch/segment CSV.
4. Historical-sale matching pass, live-wins, newer-past-wins.

### Codex

Five findings, all reproduced against the code and fixed: a cross-county rewrite risk I
introduced by normalizing the parcel join; a fresher past sale unable to correct an older
one across a capped catch-up; archive recoveries masking the barren alert; `--fields` not
deploy-gated; and the sweep missing Snohomish's slipped-day issues.

### Still to do (needs the user)

- Merge + deploy #209.
- Then run, in this order:
  `backfill_nts_pdf_archive.py --source queen_anne_news --apply`, then
  `repair_nts_ts_number.py --source queen_anne_news --retire-wrong-key --fields --results
  --i-confirm-fixed-parser-is-deployed --apply` (dry run: 8 rows).
- Snohomish `--fields` also has 1 legitimate correction pending (a NULL `principal_owing`
  the current parser now reads as 350,661.98).

### Unverified

- Why the King crawl stopped ingesting after 2026-08-06 was never pinned to a specific
  failure — log retention did not reach back, and the code path works when run today. The
  archive sweep makes the cause moot rather than answering it.
- Whether LandmarkWeb serves text-layer or image-only PDFs.
- 7+ of King's 22 approved legal newspapers were never checked for robots.txt/ToS.
