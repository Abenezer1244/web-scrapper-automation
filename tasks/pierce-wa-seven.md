# Pierce County, WA: seven record types, reliability audit (2026-11-27)

Branch `feat/pierce-wa-seven-record-types-reliability` (this worktree, off main 5dfd8a98).

## Baseline (measured 2026-11-27, read-only prod query, counts only)

| Type | Implemented | Source | Runs (all time) | Last run | Notes |
|---|---|---|---|---|---|
| probate | yes | ARMS recorder (checkbox 226) | 33 done (11 >0, 22 NULL count) | 2026-11-10 | 90d: 1,687 rows, 91 no parcel, 135 no address |
| pre_foreclosure | yes | ARMS (NOD/NOF/LP/NTS) + Tacoma Daily Index auction data | 34 done, 4 failed, 2 cancelled | 2026-11-25 | 90d: 5,760 rows, 62 no parcel, 127 no mailing |
| divorce | yes | ARMS (checkbox 87, decree of dissolution) | 1 done | 2026-09-13 | |
| trustee_sale | yes | Tacoma Daily Index NTS cache | 5 done, all NULL count | 2026-09-13 | no run on current code |
| code_violation | partial: City of Tacoma only | Tacoma ArcGIS open data | 1 done | 2026-09-13 | strict qualification policy applies |
| tax_delinquent | absent | (blocked) | 0 | - | ruled infeasible/blocked 2026-06-11 |
| death_certificate | absent | none | 0 | - | never researched for Pierce |

Every done run with NULL `records_found` is dated on or before 2026-09-20, the day migration 099
added the column. #598 (2026-11-06) made the count mandatory before done and added a watchdog alert.
No Pierce job is stuck in an active state today.

## Historical problems from the brief: status

- A. Trustee sale done with NULL count: pre-column history, fixed by #598 (code read confirms the
  guard). Not reproduced on current code because no run since 09-13. -> verify with one bounded run.
- B. Pre-foreclosure missing auction date / principal: correct for NOD, LP, NOF (no sale date at
  source). Only Trustee Sale rows match a notice, 55-150 days later (#184/#185, 180-day matcher).
  Open owner decision: unlabeled section-IV first dollar figure stored as Principal Owing (J1633).
- C. Missing parcel/address: source limits verified 09-02 (no parcel on ARMS, mobile homes,
  recorder typos repaired). One latent code defect found, see D1.
- D. Probate stuck retries: fixed by #217 (1-3 row pages) and #273 (heartbeat). UI shows
  "Waiting to retry" while pending a retry (schemas.py:1952).
- E. Progress accuracy: covered by D.
- F. Qualification: strict server-side policy exists for code_violation only. Other types use the
  older rule (no address at all = quarantined, not billed).

## Code defects verified this session

- D1 `pierce_wa_probate.py:241-249`: the parcel pass swallows a failure to return to results
  page 1, then walks from the last page, silently skipping parcels (and GIS/ATIP enrichment) for
  every earlier page. Fix: fail the run as transient instead of delivering degraded rows.
- D2 `pierce_wa_code_violation.py`: exhausted ArcGIS retries raise plain RuntimeError (permanent
  failure, no job retry) and backoff uses blocking `time.sleep` in async code.
  Fix: raise TransientScrapeError; use asyncio.sleep.
- D3 (check, maybe no-op) Tacoma `parcelnumber` field type: `str()` of a numeric drops a leading
  zero. Verify against the live layer's field metadata with one request.

## Blockers needing the owner (no code until decided)

1. ARMS (probate, pre_foreclosure, divorce, and any death-certificate cover sheets): robots.txt
   `Disallow: /`, terms forbid commercial/automated use. Legal audit P0-4 says get counsel and
   consider pausing. Still active in production.
2. LINX court search: "storing ... for the purpose of providing access to third parties is
   strictly prohibited". Not usable for Probate/Divorce without counsel.
3. Tax delinquent: Data Mart has no balance column; foreclosure list needs RCW 42.56.070(8)
   acknowledgement. Stays blocked.
4. Death certificate: no public index after 2017; certified copies restricted; ARMS cover sheets
   carry blocker 1. Stays not implemented.
5. Extending the strict qualification policy to probate/pre_foreclosure/divorce/trustee_sale
   changes billing. Needs an impact assessment and owner approval.
6. ATIP enrichment solves reCAPTCHA via 2Captcha (P0-4). Rule says never bypass CAPTCHA.

## Phase 1 plan (<= 5 files)

- [x] Codex consult on D1-D3 before code
- [x] D1 fix + regression test (tests/test_pierce_arms_parcel_pass.py)
- [x] D2 fix + regression test (tests/test_pierce_cv_retry.py)
- [x] D3 live field-type check: esriFieldTypeString, no change
- [x] pytest: 296 passed; ruff clean
- [x] Codex review: NO-GO (High/Medium/Low fixed), then GO
- [x] Journal entry 2026-11-27

Skipped on purpose: watchdog hardcoded retry cap 3 (equivalent today), UTC vs local expiry
edge on NTS (minutes per day, no lead loss shown), new scrapers for blocked types.

## Review (2026-11-27)

Phase 1 done, uncommitted. Follow-up: `_fetch_parcels_from_detail` can still stop early on
inner navigation failures (Codex). Owner decisions 1-6 still open.
