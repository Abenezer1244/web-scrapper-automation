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
  (18/18) — but **not** for Snohomish (3 of 4 probes 404).
- **RC4 (bug, highest severity).** 12/14 cached King rows have a wrong `ts_number` and
  2 have a wrong `auction_date`, suppressing a live 2026-09-18 King auction.
- **RC5 (bug, separate path).** Batch/segment/overlap CSVs silently drop both fields.

## Plan

- [ ] 1. `nts_crawler`: bounded weekly-archive sweep for papers with a deterministic
      issue-URL template (King only — Snohomish's naming is not derivable), tolerating
      404s, idempotent via the existing `(source, ts_number)` upsert. Recovers lost
      weeks and self-heals future misses.
- [ ] 2. `repair_nts_ts_number.py`: also correct `auction_date` / `principal_owing`
      from the re-parsed truth (today it only rewrites `ts_number`), so RC4's wrong
      dates are fixed, not just the keys.
- [ ] 3. `lead_export.py`: include `auction_date` / `default_amount` in the
      overlap/batch/segment column set (RC5).
- [ ] 4. Regression tests keyed on source structure, not on Test 8 values.
- [ ] 5. Full pytest + lint; Codex diff review; browser re-verify.

## Explicitly NOT doing

- No fabricated or inferred auction dates. No mapping of `date_recorded` (a recording
  date) onto Auction Date.
- No Test 8-specific hardcoding.
- Not building recorded-document ingestion: LandmarkWeb is reCAPTCHA-gated, the
  2Captcha key is dead, King has IP-rate-blocked us before, and document images are
  likely paid. Reported as a product decision, not attempted here.

## Review

_(filled in at the end)_
