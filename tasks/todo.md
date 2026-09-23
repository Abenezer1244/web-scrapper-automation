# King County pre_foreclosure: Auction Date / Principal Owing = N/A

Branch: `investigate/king-nts-parcel-bridge` · worktree `C:/Users/Windows/bl-wt-kingnts`
Opened 2026-09-20. Reported from job `18076769` (51 rows, 0 with auction data).

---

## Findings (evidence, not hypothesis)

All numbers read from production 2026-09-19/20 via `DATABASE_URL_SYNC` (read-only).

### The record type is correct. This is not a misclassification bug.

1,114 / 1,114 King `pre_foreclosure` results carry `doc_type = "NOTICE OF TRUSTEE SALE"`.
No deeds, assignments, liens or transfers. `king_wa_probate.py:922-933` already runs
`_is_preforeclosure_doc()` then `is_cancellation_or_admin()` (drops Discontinuance /
Rescission / Withdrawal / Reconveyance / Substitution of Trustee), then
`orient_pre_foreclosure_party()`. Every sample parcel from the screenshot is a real NTS
with its recorder instrument number stored in `enrichment_data.instrument_number`.

### BridgeLeads DOES stop at the recorder index, by design, and the index has no auction fields.

`king_wa_probate.py:871-963` parses the LandmarkWeb `GetSearchResults` DataTables JSON.
The payload carries grantor, grantee, record date, doc type, recording number, legal/PID.
There is no document-detail fetch and no PDF/image path anywhere in the module. The sale
date and the amount owing exist only in the body of the recorded notice (RCW 61.24.040(1)(f)).
`docs/scoping-king-nts-coverage-2026-09-03.md` (+2 addenda) records why that route is closed:
King's LandmarkWeb terms ban "high-volume, automated" access and "Data Mining (mass
downloading) of images", and King has IP-rate-blocked this project before.

### So auction data comes from newspapers, and King's supply is ~1%.

Architecture: recorder -> `pre_foreclosure` lead; legal newspaper -> `nts_notices`;
`nts_matcher_task` attaches. All 22 court-approved King legal newspapers were checked in
Sept; only Queen Anne & Magnolia News is usable. Prod holds **38 King notices ever**
against **1,114 King leads / 298 distinct parcels**.

Fill rate by county, `pre_foreclosure`, all time:

| county | results | auction_date | % |
|---|---|---|---|
| pierce | 2,426 | 260 | 10.7% |
| king | 1,114 | 15 | **1.35%** |
| snohomish | 76 | 76 | 100% |
| clark | 27 | 0 | 0% |

### Why THIS job is 0/51 and not 1/51: a timing window, not a failure.

Recording -> auction lag on matched King leads: **57-137 days (avg 109)**. A notice is
published 7-35 days before the sale (RCW 61.24.040(5)), i.e. ~2-4 months AFTER recording.
Job `18076769` scraped recordings from **2026-08-20 to 2026-09-18**. Those sales fall in
Dec 2026-Jan 2027 and have not been published anywhere yet. The newest King auction we hold
is 2026-10-16.

King leads by recording month vs auction data obtained:

| recorded | leads | with auction |
|---|---|---|
| 2026-03 | 23 | 1 |
| 2026-04 | 64 | 3 |
| 2026-05 | 26 | 3 |
| 2026-06 | 156 | 8 |
| 2026-07 | 325 | 0 |
| 2026-08 | 372 | 0 |
| 2026-09 | 148 | 0 |

The zeros for Jul/Aug/Sep are structurally correct. `nts_matcher_task` already keeps each
lead a match candidate for 180 days (`_RECENT_DAYS = 180`) and re-runs daily, so these rows
can still acquire auction data later. The beat is healthy: every exact-parcel pair that
exists has already been attached (11 notices -> 15 results, fully converged).

### The one real code bug: King's 12-digit account number vetoes the match.

King publishes two identifiers. The recorder index emits the **10-digit PIN** (`2895650150`).
Trustees print the **12-digit tax account number** on the notice (`289565-0150-04`).
`nts_matcher._norm_parcel` strips punctuation but not the length difference, so the two read
as a parcel CONFLICT and `score_match` returns `0.0` at line 88-89 -- a hard veto that even
an agreeing street address and an agreeing surname cannot override.

Reproduced (`scratchpad/repro_matcher.py`, pure function, no DB):

| notice | result | score today |
|---|---|---|
| `289565-0150-04` | `2895650150` | **0.0** |
| `421640022008` | `4216400220` | **0.0** |
| `327692-0130-03` | `3276920130` | **0.0** |
| `198920-1275` (control) | `1989201275` | 0.96 |
| different property (control) | | 0.0 |

Prod impact: 14 of 38 King notices normalize to 12 chars. Exact overlap today is 11 notices
-> 15 results; on the 10-digit PIN it is 16 notices -> **21 results**. The 7 missed pairs all
have an agreeing surname; two are LIVE upcoming auctions:

- `WA07000188-22-3` 2026-09-25 $190,752.06 (SIMON JOANN, 11120 NE 68TH ST #B-206)
- `WA08000108-25-1` 2026-10-09 $525,833.17 (KORKONDA/CHODIMELLA, 4206 S GREENBELT STATION DR) x3 tenant rows

This is King-only: Pierce notices normalize to 10, Snohomish to 14, Clark to 9, and no PIN
outside King appears in both forms.

### API / UI / CSV are innocent.

`schemas.py:1165-1166` passes `auction_date: date | None` / `default_amount: float | None`
straight through; NULL serializes as JSON null. `lead_export.py:458-460` emits blank, not
"N/A". The "N/A" in the screenshot is a frontend empty state. No backend coercion, no
fabrication anywhere.

### Field semantics: "Principal Owing" is right most of the time, over-specific sometimes.

`nts_tacoma_index._principal_owing` anchors on the RCW 61.24.040(1)(f) section IV sentence
("the sum owing on the obligation secured by the Deed of Trust"), PREFERS the figure labelled
`Principal`, and only falls back to the first dollar figure in section IV when the notice
carries no "principal" label (matured/balloon notices). In that fallback the number is the
total sum owing, not strictly principal. `note_amount` (the original loan size) is stored
separately and is never used as Principal Owing.

---

## Plan

### Phase 1 - matcher fix (the only recoverable data)

Codex consulted 2026-09-20, `GATE: FAIL` with 4 P1s. All four verified against the code and
accepted:

- [ ] P1-a: a bridged parcel must NOT inherit the unconditional `0.90` branch (that is exactly
      `MATCH_THRESHOLD`, so an uncorroborated bridge would auto-attach).
- [ ] P1-b: gate the bridge on county. `score_match` has no county argument today; thread one
      through and fail closed when it is missing. A shape-only rule is a King encoding rule
      wearing a generic costume.
- [ ] P1-c: `best_match_group._same_property` (lines 173-185) compares `_norm_parcel` for
      equality too. Patching only `score_match` makes a bridged sibling read as a DIFFERENT
      property and bails the whole group to `[]` -- strictly worse than today. Model parcel
      identity once as an explicit relation (`EXACT` / `KING_PIN_ACCOUNT` / `CONFLICT` /
      `UNKNOWN`) and use it in both places.
- [ ] P1-d: the relation is non-transitive (12A == PIN == 12B, 12A != 12B). Fail closed when
      two distinct 12-digit accounts share one PIN. Verified: **zero** such cases in prod
      today, in both `nts_notices` and `results`.
- [ ] Regression tests per Codex's list, plus the 7 measured King pairs as fixtures.

**One deliberate departure from Codex, needs a call (see Open question 1).** Codex wants the
bridge to require BOTH address and grantor agreement. Address agreement is structurally
unavailable on King recorder rows: `property_address` is the frozen street-only dedup key, so
`address_match_key` yields `4206 S GREENBELT STATION DR` against the notice's
`4206 S GREENBELT STATION DR|98118`. Requiring both recovers **1 of 7** pairs. Requiring
grantor agreement alone recovers **7 of 7**.

### Phase 2 - missing-reason classification (the user asked for this explicitly)

- [ ] Record WHY auction data is absent, internally, without changing what the customer sees:
      `SOURCE_NOT_PUBLISHED_YET` (recording too recent for the statutory publication window),
      `NO_SOURCE_FOR_COUNTY`, `SOURCE_HAS_NO_NOTICE`, `SUPERSEDED`, `PARSE_FAILED`.
      This is what makes the next King question answerable in one query instead of a session.

### Phase 3 - backfill

- [ ] Idempotent re-match of existing King rows. The daily beat already does this; the only
      new rows are the 6-7 the bridge unlocks. No new leads, no quota touched, no delivery
      re-marked, no skip trace. Uses the existing `_write_match` guard (claims only rows that
      are unset or hold a past auction), so a re-run writes 0.

### Phase 4 - verification

- [ ] `pytest` on the local rig (NOT bare pytest -- see `.claude/rules/testing.md`).
- [ ] Codex review of the diff.
- [ ] Live UI check.

---

## Owner decisions (2026-09-20)

1. **Bridge corroborator: Codex's stricter rule** - a bridged parcel requires BOTH
   address and surname agreement. Costs 6 of the 7 recoverable pairs (the King recorder
   stores a street-only `property_address`, so its address key carries no ZIP to match
   the notice's), and keeps the false-attach risk at its lowest. Measured effect: King
   15 -> 16 filled.
2. **Scope: all four phases**, including the backfill and the UI.

---

## Review

### What changed

**Backend** (`investigate/king-nts-parcel-bridge`, 2 commits)

`6c1d480` - the matcher fix + missing-reason classification
- `src/scrapers/sources/nts_matcher.py`: parcel identity is now an explicit relation
  (`parcel_relation` -> EXACT / BRIDGED / CONFLICT / UNKNOWN) shared by the scorer and
  by `best_match_group._same_property`, plus `parcel_index_keys` for pool membership.
- `src/workers/nts_matcher_task.py`: pool indexed/looked-up under both spellings;
  `auction_missing_reason` + `_stamp_missing_reasons` record WHY a lead is blank.
- 38 new tests.

`0c59d81` - the honesty fix + the backfill
- `src/api/schemas.py` + `src/api/routes/jobs.py`: `auction_coverage` on ResultsPage.
- `src/config/constants.py`: `AUCTION_PUBLICATION_LAG_DAYS = 55`, one source of truth.
- `scripts/backfill_nts_matches.py`: dry-run-by-default re-match over a wide window.
- `schema/openapi.json` regenerated (32 insertions, 0 deletions).
- 6 new tests.

**Frontend** (`fix/auction-coverage-note`, sibling repo)
- `lib/coverage.ts`: `auctionCoverageNote()`, a pure function over the API's numbers.
- results page: one amber note above the table. Hidden while searching/tax-filtering
  (the counts describe the whole run), and silent when every lead has its sale date.

### Results

- Reproduced before/after on the real King pair: TS WA07000188-22-3 goes 0.0 -> 0.95.
  Controls unchanged (different property 0.0, two 12-digit accounts 0.0, exact 0.96).
- Prod dry run, all four NTS counties, 3,442 candidates: pierce 0, snohomish 0,
  clark 0, king +1. Surgical, no cross-county effect.
- Missing-reason split for King: 585 SOURCE_NOT_PUBLISHED_YET, 513 SOURCE_HAS_NO_NOTICE.
- Mutation check: forcing the county gate from `king` to `pierce` fails 16 tests,
  including every county-gate assertion. The new tests are not vacuous.
- Browser (Chromium, real page against a scratchpad stub API, 13 assertions): the note
  renders and is correctly worded for reported / none-found / partly-matched, is ABSENT
  when every lead is matched, is hidden while searching, sits above the table, and
  does not overflow at 390px. `tsc --noEmit` and eslint clean.
- CSV needs no change and is already covered: `tests/test_lead_export.py:318-336`
  asserts a row carrying `auction_date` / `default_amount` emits both, and the export
  reads those stored columns without knowing how they were filled, so the bridge flows
  through automatically.
- Codex: design consult GATE: FAIL (4 P1s, all adopted), Phase 1 diff review
  GATE: PASS (no P1). ⏭️ The Phase 3/4 diff review did NOT run - Codex hit its usage
  limit mid-review ("try again at 4:02 AM"). Per `.claude/rules/codex-collaboration.md`
  that review is still owed before this lands.

### Already-covered behaviour worth stating (the report asked about these)

- **Superseded / postponed sales.** `_write_match`'s two claim rules already handle it:
  a LIVE notice may replace an attached PAST date (so a postponed or re-noticed sale is
  not frozen on the stale one), while a historical notice claims only a row that is
  unset or holds a STRICTLY OLDER past sale. Inline postponements are parsed too:
  `nts_tacoma_index` reads "SALE POSTPONED TO <date>" and moves the auction date.
- **Discontinued / rescinded foreclosures** never become leads:
  `preforeclosure.is_cancellation_or_admin` drops DISCONTINU / RESCISSION / CANCEL /
  WITHDRAW / RECONVEY / SATISFACTION / SUBSTITUTION OF TRUSTEE before the row is built.
- **"Principal Owing" semantics.** `nts_tacoma_index._principal_owing` anchors on the
  RCW 61.24.040(1)(f) section IV "sum owing on the obligation" and PREFERS the figure
  labelled `Principal`. On a matured/balloon notice carrying no "principal" label it
  falls back to the first dollar figure inside section IV, which is the total sum owing,
  not strictly principal. The label is therefore right for most rows and slightly
  over-specific for that minority. `note_amount` (the original loan size) is stored
  separately and is never used as Principal Owing.

### Found while tracing, fixed here

`45ed404` - **the Lists CSV was printing three auction headers over three permanently
empty columns.** `OVERLAP_LEAD_COLUMNS` gained `auction_date` / `days_to_auction` /
`default_amount` *because* a segment export lost them (`lead_export.py:775-781`), and
`batch_export.py` was fixed alongside that comment while `segments.py` was not. All three
segment queries omitted `r.auction_date` / `r.default_amount` from both the inner SELECT
and the final projection; `build_overlap_export_row` reads with `getattr(row, name, None)`,
so an absent column is indistinguishable from a NULL one. Surfaced by the field-trace
subagent, verified independently, test written red-first, and each modified query EXPLAINs
against the real schema. The same omission also blanks `doc_type`, `delinquent_amount`,
`delinquent_bill_year`, `heirs`, `legal_description` and the owner flags there - left alone
rather than widened into an unrelated change. ⏭️

### Codex gate: PASS (2026-09-20, after a new login)

Codex was rate-limited when the first report went out. On its return it reviewed
everything it had not seen, across five rounds:

- API + backfill: **GATE: PASS**, no P1.
- Lists CSV + frontend: **GATE: FAIL** - P1, fixing 2 of 7 silently-blank columns in the
  same SELECTs was arbitrary; P2, the all-three-buckets note dropped `no_notice_found`;
  P2, hiding the note during search reintroduced the ambiguity it exists to remove.
- After the fix: **GATE: FAIL** - P1, the contract-derived test trusted a human
  classification; P3, the new branch said "the other 1".
- After the live test: **GATE: FAIL** - P1, `typed_elsewhere` was just another allowlist.
- Final: **GATE: PASS**, no P1.

Two Codex findings were NOT adopted, with evidence:
- It named the owner flags as missing from the Lists CSV. They are not in
  `OVERLAP_LEAD_COLUMNS`, so that CSV never emits them.
- It claimed `_seed()` fails to set `Result.record_type`. `Result` has no such column;
  it lives on `ScraperConfig`, which `_seed()` does set. Codex retracted this.

⏭️ Its open P2: the combined MIXED-record case (intersection across 2+ record types,
membership-backed) is untested. Name order is a per-representative-row property so the
behaviour should hold, but nothing proves it.

### Notes and follow-ups

- The reported job (`18076769`) gains nothing from this and is *correct* as it stands:
  its 51 leads were recorded 08/20-09/18, and their sales cannot be published until
  roughly December. The new note is what that run needed, not more matching.
- ⏭️ The backfill was NOT applied. Applying it before the code deploys would enrich prod
  with unmerged logic, and once deployed the daily beat picks the same row up by itself
  (every King lead is inside the 180-day window). Run it only to reach leads older than
  180 days.
- ⏭️ The FE PR must land AFTER the BE PR: `lib/api-types.generated.ts` is generated from
  the BE's `main`, so `auction_coverage` does not exist in it until the BE merges.
- 🛑 **Latent test-fixture bug, pre-existing, not fixed here.**
  `tests/test_rls_isolation.py::_create_non_bypass_role_if_missing` creates the role and
  grants it in the same `if not exists` branch. Postgres roles are CLUSTER-scoped while
  table grants are DATABASE-scoped, so against any NEW test database the role already
  exists, the GRANTs are skipped, and the test fails with `permission denied for table
  results`. Proven: granting by hand made it pass. Anyone running the suite against a
  fresh DB will hit this and may mistake it for their own diff.
- ⏭️ Codex P2, pre-existing and out of scope: when NEITHER side carries a parcel,
  `_same_property` groups on `addr_key`, which strips unit numbers, so one notice can
  still attach to two distinct units. Contradicts the safety comment above it.
- ⏭️ Clark's NTS source looks dead: 2 notices total, `last_created` 2026-07-28,
  `last_fetched` 2026-08-03. Every Clark pre_foreclosure lead is unmatched. Worth a look
  on its own; nothing in this change touches it.
- ⏭️ Stale comment at `src/scrapers/king_wa_probate.py:913-916` says Amended Notices of
  Trustee Sale are dropped. `_CANCELLATION_ADMIN` has no AMEND token, so they are not.
  Prod shows zero such rows, so this is a doc bug rather than a behaviour bug.
- 👤 The 1.35% ceiling itself is a commercial question, not an engineering one. All 22
  court-approved King legal newspapers were checked in September and only one is usable;
  the recorded document carries both values but LandmarkWeb's terms forbid bulk image
  retrieval. Raising King coverage means licensing a foreclosure feed.

---

# CRM CSV follow-ups, one by one (2026-09-15)
# Live Run progress: truthful state, not a frozen 0%

BE worktree: `C:/Users/Windows/bl-wt-liverun` — branch `feat/live-run-progress` off `origin/main` @ 6b03ece
FE worktree: `C:/Users/Windows/bl-wt-liverun-fe` — branch `feat/live-run-progress` off `origin/master` @ 6c435d0

> The Desktop checkout was 141 commits behind `origin/main` and the FE checkout was on
> a divergent branch 419 lines behind `origin/master`. Everything below is against the
> upstream code that is actually deployed.

---

## The run in the screenshot was healthy, not stuck (verified in prod)

Job `b80bd9a5-c5f7-4239-9519-71eb8fbc4fa3`, King WA probate, manual, `status=done`.

```
21:54:37  started
21:54:39  "Connecting to county portal..."
          <-- 401s: no log, no status change, page_current=0, page_total=0, record_count=0
22:01:20  "Scrape complete: 57 records found"
22:01:21  Saving records to database...
22:01:22  Checking for duplicate leads...   57 saved (2 new, 55 duplicates)
22:01:23  Building CSV export...  /  Export uploaded to cloud storage
22:01:24  Looking up property and mailing addresses...
22:01:29  "Looking up county records for 48 properties..."
          <-- another 97s of nothing
22:03:07  Found 48/48 mailing addresses
22:03:19  Job complete: 2 new leads (55 duplicates filtered)
```

Heartbeat was alive at 22:02:40. 8m18s of an 8m42s run had nothing measurable.
Final counters: `page_current/page_total = 1/1`, `record_count = 2` (not 57).

---

## Findings

### Why the screen says 0%

| # | Finding | Evidence |
|---|---------|----------|
| **F1** | The ring is fed `progress ?? (isRunning ? 5 : 0)` but the NUMBER inside it is fed `progress ?? 0`. With `progress === null` the arc draws 5% and the label reads **0%**. That is literally the screenshot. | FE `live/[id]/page.tsx:604` vs `:613` |
| **F2** | UNKNOWN is not representable. `jobs.page_current / page_total / record_count` are `Integer NOT NULL DEFAULT 0`. "Not measured yet" and "measured, found nothing" are the same value. | `src/db/models.py:682-684` |
| **F3** | Two fabricated percentages already ship: enrichment renders a hardcoded **90%**, real page progress is capped at **85%**. | FE `page.tsx:307-311` |
| **F4** | The tile is labelled **ETA** but renders `"1m 43s elapsed"`. | FE `page.tsx:337-339, 792` |

### Why there is genuinely nothing to report for minutes

| # | Finding | Evidence |
|---|---------|----------|
| **F5** | **The dead zone.** `"Connecting to county portal..."` and the next log line is `"Scrape complete"`, with the whole scrape in between under a 30-minute timeout. No status change, no log, no counter. | `tasks.py:644` then `:793` |
| **F6** | The 13 `on_progress` call sites are mutually inconsistent: `(0,0,n)`, `(page_num,0,n)`, `(page_num,0,0)`, end-only `(1,1,n)`, `(chunk_num,total_chunks,n)`. Snohomish / trustee_sale / Clark / Whatcom can never produce a denominator. | `src/scrapers/*.py` |
| **F7** | King probate's first `on_progress` fires only after chunk 1 completes, after browser launch, up to 3 startup attempts, a captcha interceptor and a disclaimer accept. `total_chunks` is known before the loop and never reported. | `king_wa_probate.py:165, 228` |
| **F8** | `enriching` is a second mega-stage covering save, dedup, export, address lookup, upload and delivery. It is **not a linear pipeline**: the CSV export runs BEFORE enrichment. | `tasks.py:837-1404`, prod log above |

### What already works (do not rebuild)

- Worker heartbeat is live (`tasks.py:549`), attempt-scoped, 60s.
- `JobResponse.progress_stalled` from `last_heartbeat_at`, on the same thresholds the watchdog recovers on. `schemas.py:1160-1196`
- `retry_pending` (`pending` + `retry_count > 0`). `schemas.py:1197`
- FE already renders stalled runs and gates ETA/LIVE on `isLive`. `page.tsx:301-303`
- FE SSE hook already models `connecting | live | paused | failed | ended` with backoff and lease-aware 429 handling, and already parses a `{"type":"progress"}` event **the backend never emits**. `hooks/use-log-stream.ts:28, 172`

---

## Codex review of the first plan: P1s, all verified by me

Codex (`gpt-6-astra`, high) rejected the first plan's data model and found a live bug.
Every claim below I re-read in the tree myself; all confirmed.

| ID | Sev | Finding | My verification |
|----|-----|---------|-----------------|
| **C1** | P1 | One `last_progress_at` sentinel cannot express independently-known counters. After `(page_num, 0, n)` records and pages-done are known but the total is not. And `records_found` **cannot alias `record_count`**: `done` overwrites it with the billed count. | `tasks.py:2205 record_count=display_count`. Prod: 57 scraped, stored 2. Confirmed. |
| **C2** | P1 | A stage helper calling `commit()` on the work session commits **all pending work**, which can separate billing from the terminal CAS. An independent connection instead blocks on the job-row lock. | `_set_status(commit=False)` exists for exactly this reason. Pool is `pool_size=2, max_overflow=3`. Confirmed. |
| **C3** | P1 | Progress writes need **attempt ownership**, not just a terminal guard. `_on_progress` writes ORM attributes and commits unguarded, so a stale attempt can overwrite a replacement attempt's counters. | `tasks.py:621-634`. Confirmed. |
| **C4** | P1 | **The watchdog can undo a cancellation.** It SELECTs, then mutates ORM objects and commits **by primary key with no status precondition**. A cancel committed in that window is overwritten `cancelled` -> `pending` and re-enqueued. | `scheduler_helpers/health.py:92-215`. Confirmed. **Pre-existing production bug, not caused by this work.** |
| **C5** | P1 | Scrape percentage is not job percentage. A page denominator measures the scrape only; the prod run spent 401s of 522s there. Showing its 100% as whole-run completion still violates the no-fake-percentage rule. | Confirmed against the prod timeline. |
| **C6** | P2 | `next_retry_at` would be a promise the system breaks: the watchdog's stranded-retry branch keys on `Job.created_at < 70min`, not the retry time, so it can redispatch during the backoff. | `health.py:119-131`. Confirmed. |
| **C7** | P2 | The stage list describes a pipeline that does not exist. Export precedes enrichment; scrapers do their own parcel lookup; skip-trace only enqueues; delivery happens **after** `done` and its SSE event, so a guarded `delivering` stage would be rejected and the stream may already be closed. | `tasks.py:1261 -> 1404`, `:2244`. Confirmed by the prod log. |
| **C8** | P2 | King's denominator is wrong at boundaries: `max(1, days // 90 + 1)` reports 2 chunks for exactly 90 days while the loop runs 1; a same-day range reports 1 and runs 0. Failed chunks are skipped, so `chunk_num` counts failures as completed. | `king_wa_probate.py:165, 197-232`. Confirmed. |
| **C9** | P2 | Stage-only SSE events do not carry counter changes, and Pub/Sub is not replayable: reconnect needs an authoritative snapshot and late events must not overwrite it. | Design point, accepted. |
| **C10** | P2 | The SSE premise was outdated: this tree uses `sse_leases:{user_id}` with 20s renewal / 60s TTL, not `sse_conn`/`sse_count`. | `src/api/sse_leases.py`. My earlier read was the stale tree. Confirmed. |
| **C11** | P2 | Cancellation is terminalization, not interruption: the endpoint writes `cancelled` and does not revoke Celery, close Playwright, release the reservation, or publish an event. Riskiest point is **exporting after quota reservation** — the grant is charged and only the 5-minute sweep returns it. | `routes/jobs.py:332-363`. Confirmed. |

**Cut on Codex's advice:** whole-run percentage, whole-run ETA, `delivering` as a
scrape stage, and the assumption of a linear stage sequence.

---

## Revised plan

Hard rule: a percentage renders only with a real denominator, and it is **scoped and
labelled to the activity it measures**. No whole-run percentage. No elapsed-to-percent.
No auto-increment. No cap, no freeze. `done` is a completion state, not "100%".

### Phase 1 — BE: make UNKNOWN representable, per fact (2 files)
- [ ] Migration, all nullable, no backfill (NULL = unobserved). Deployed **before** any worker change so old workers simply report unknown.
  - `stage` varchar, `stage_started_at` timestamptz
  - `records_found` int — raw scrape total, **never** overwritten by billing (C1)
  - `units_done` int, `units_total` int, `progress_unit` varchar (`page|chunk|parcel|record`) (C1, C8)
  - `last_progress_at` timestamptz — progress-granularity liveness, not the UNKNOWN sentinel
  - `next_retry_at` timestamptz — a **not-before** target, never a guarantee (C6)
- [ ] `src/db/models.py` columns plus comments naming the invariants.

### Phase 2 — BE: attempt-scoped, commit-safe progress writes (3 files)
- [ ] `_set_progress()` in `tasks_helpers/status.py`: one guarded UPDATE on
      `(id, started_at == this attempt, status NOT IN terminal)`. Rowcount 0 means
      superseded: write nothing, publish nothing (C3).
- [ ] Stage changes ride the transaction boundaries that already exist — `_set_status`
      and the `_publish_log(db=db)` commits — so no new `commit()` is introduced on the
      work session (C2). Publish only after commit.
- [ ] `_on_progress` moves onto `_set_progress` and stamps `records_found`,
      `units_done/total/unit`, `last_progress_at`.
- [ ] `_retry_scrape_job` + the watchdog reset the new observations and stage atomically
      with the counters they already reset (C6).
- [ ] Stages are **real boundaries and may repeat**: `preparing, connecting, searching,
      scraping, saving, deduping, exporting, enriching, queuing_contacts, finalizing`.
      No `delivering` (C7).

### Phase 3 — BE: the API stops lying (3 files)
- [ ] `JobResponse`: `records_found`, `units_done`, `units_total`, `progress_unit`,
      `stage`, `stage_label`, `stage_started_at`, `next_retry_at` — every one nullable
      and independently unknown.
- [ ] `stage_progress_pct` only when `units_total > 0 AND units_done > 0`, explicitly
      scoped to `stage`+`progress_unit`. Whole-run `progress_pct` retired from the
      contract (C5). Revisit `min(99, ...)` and `done -> 100`.
- [ ] `estimated_time_remaining` only for a stage with a real denominator and >= 2
      observations, labelled for that stage.
- [ ] SSE: emit `{"type":"progress"}` carrying a **full snapshot** (stage + counters),
      not a stage-only ping; the FE re-syncs from the REST snapshot on reconnect and
      ignores older snapshots (C9). Add the 15s keepalive into the existing lease-aware
      deadline loop without disturbing renewal or the 30-minute expiry (C10).

### Phase 4 — BE: normalize the county adapters (2 sub-phases, ~5 files each)
- [ ] 4a: `base_scraper.report_stage()`; King probate derives the denominator from the
      **actual windows**, distinguishes attempted from completed chunks, reports the
      total before chunk 1, and reports `connecting`/`searching` (C8). Wire the callback
      **before** browser context entry so startup is observable (C7).
- [ ] 4b: the other 8 adapters report honest unknown totals with a correct
      `progress_unit`; fix `king_wa_tax_delinquent.py:512` zeroing the record count;
      stop Pierce reporting parcel counts as pages.

### Phase 5 — BE: tests + contract
- [ ] The 20 states, plus the race/failure cases Codex named: cancel vs watchdog
      recovery, stale callback vs new attempt, publish failure, reconnect mid-transition,
      failed retry publication, empty-but-successful scrape, exact chunk boundaries, and
      57-found vs 2-billed.
- [ ] Assert UNKNOWN never serializes as `0`.
- [ ] Regenerate `schema/openapi.json` in `.venv-schema`, diff vs `origin/main`.

### Phase 6 — FE: the status card (4 files)
- [ ] Activity is the headline. Percentage is secondary, scoped, and only when real.
- [ ] Indeterminate indicator; `prefers-reduced-motion` alternative.
- [ ] Metrics read "Searching..." while UNKNOWN; `0` only on an observed zero.
- [ ] ELAPSED vs ETA labelled by what we actually have.
- [ ] Retry: attempt N of M, countdown from `next_retry_at` worded as **not before**.
- [ ] LIVE / RECONNECTING / WAITING / CONNECTION LOST from the hook's real state.
- [ ] `role="status"` + `aria-live="polite"` on stage transitions only; timer `aria-hidden`.

### Phase 7 — FE: mobile + verification
- [ ] Playwright against a stub API at 320 / 375 / 390 / 430 and desktop, all 20 states.

### Phase 8 — Review
- [ ] Codex `review` + `challenge` on both diffs; verify every finding myself.
- [ ] Security Master Review (§14) twice clean.
- [ ] `docs/BUILD_JOURNAL.md` entry.

---

## Open decision

**C4 — the watchdog can resurrect a cancelled job.** Live bug, pre-existing, adjacent to
this work rather than caused by it. A cancelled run can be flipped back to `pending`,
re-enqueued, re-scraped, re-billed and re-delivered. The user asked explicitly that
cancellation not leave duplicate workers or a non-terminal job, so it is in scope by
intent, but it is its own fix with its own blast radius.

---

## Review

### Shipped

| PR | State | Gate |
|----|-------|------|
| BE **#347** watchdog guarded recovery writes | **MERGED** `8ba7bf8`, deployed | CI full suite green; Codex reviewed and found a hole in the first version, fixed in commit 2 |
| BE **#348** truthful progress | DRAFT | CI full suite + lint + OpenAPI drift green; **Codex gate INCOMPLETE** |
| FE **#158** Live Run UI | DRAFT | tsc + eslint + next build clean; browser-verified; **Codex gate INCOMPLETE** |

### The Codex gate did NOT complete

`codex review` on both diffs was interrupted by an OpenAI usage limit before it
produced any verdict:

```
ERROR: You've hit your usage limit ... try again at 4:02 AM.
Review was interrupted. Please re-run /review and wait for it to complete.
```

Per `.claude/rules/codex-collaboration.md` a build is not cleared until Codex has
reviewed the diff. **Neither #348 nor #158 has been.** Both stay DRAFT. Re-run:

```
cd C:/Users/Windows/bl-wt-liverun     && codex review --base main
cd C:/Users/Windows/bl-wt-liverun-fe  && codex review --base master
```

Codex DID review the PLAN (before any code) and the #347 diff; both of those
produced findings that were verified and folded in. It is only the two final diff
reviews that are missing.

### Security Master Review (§14) — RUN, two passes, clean

Translated to this stack per `.claude/rules/security.md`. Checked against the diff,
not asserted.

| # | Category | Finding |
|---|----------|---------|
| 1 | Authorization | CLEAN. `JobCreate` accepts only `scraper_config_id` + `trigger`; **none of the 8 new columns is client-writable**. All job reads go through ownership-filtered queries (9 sites with `Job.user_id == current_user.id`). |
| 2 | Secrets | CLEAN. None added. |
| 3 | Input validation | CLEAN by construction: the new fields are output-only. `stage` / `progress_unit` are free text at the DB but only the worker writes them, from `JOB_STAGES` / `JOB_PROGRESS_UNITS`. |
| 4 | Error handling | CLEAN. `_set_progress`'s except logs field NAMES only, never values, and returns False rather than surfacing anything. No new `HTTPException` detail. |
| 5 | XSS | CLEAN. `stage_label` is composed server-side from fixed dicts plus integers; an unrecognised `stage` returns None and falls through to status wording, so even a poisoned column cannot emit arbitrary text. Rendered as a React text child. 0 `dangerouslySetInnerHTML`. |
| 6 | SQL injection | CLEAN. `update().values(**kwargs)` is parameterized with keys constrained by a TypedDict; `make_interval(secs => :cd)` is a bound param, not interpolation. |
| 7 | File uploads | N/A. |
| 8 | Rate limiting | CLEAN. No new endpoint. The SSE keepalive adds ~13 bytes / 15s / stream, bounded by the existing 5-stream lease and 30-min cap. |
| 9 | CSRF | CLEAN. No new routes. |
| 10 | PII | CLEAN. Stage name, integer counts, timestamps. No PII. |
| 11 | Configuration | CLEAN. No new table, so no new RLS policy; columns inherit `jobs`' RLS. Migration additive + nullable with `lock_timeout`. |
| 12 | Dependencies | **FINDING — fixed.** See below. |
| 13 | Logging | CLEAN. New warn paths log identifiers and field names, not values. 0 `console.log` added. |
| 14 | Non-negotiables | CLEAN. No user_id filter dropped; no new navigation (SSRF untouched); none of the new fields reach an export path (CSV injection); no secrets; no error silenced as a fix (the two broad excepts are telemetry-only, logged, documented). |

**Finding (cat. 12, Medium, FIXED):** the FE branch was cut from `master` @ `6c435d0`
and master had since moved one commit ahead — **#159 `10d65d7`, "next 16.3.5 + pinned
next-auth, 30 vulnerabilities to 0"**. `git diff master..HEAD` rendered that bump as a
*reversion* on my branch, i.e. merging would have rolled back a security upgrade.
Rebased onto it; the diff is now exactly the 4 intended files. Re-verified against the
upgraded tree (`npm ci`, 849 packages): tsc, eslint and `next build` all clean.

This is the finding that justifies the rule. Nothing in the feature work would have
surfaced it; only running the review did.

### Connector stage gap — CLOSED for the hand-written connectors, OPEN for templates

All 10 **hand-written** connectors now call `report_stage()` (was 3). Codex round 4 caught
what that sentence originally hid: the eight **template** scrapers under
`src/scrapers/templates/` (EagleWeb, Tyler SelfService, LandmarkWeb, AcclaimWeb, AVA/Fidlar,
iDocMarket, Laserfiche, Skagit) do not, and `registry.get_scraper_class()` returns those for
any `scraper_mode == 'ai'` county. Those runs still sit on `connecting` for the whole search.
Owner decision: follow-up PR, deliberately not widened into #348 at the merge gate. `tests/test_scraper_progress_reporting.py`
walks the registry's own module allowlist, so a connector added later is covered
without anyone remembering the test. Verified non-vacuous: removing one call fails it
by name. Targeted suite: 1332 passed, 1 skipped.

### Not done

- **Codex diff-review gate still OPEN.** Re-probed after the gap fix; still
  rate-limited (`try again at 4:02 AM`). Neither PR is cleared.
- Local full-suite run was reaped for low memory at 78%; CI covered it instead.

### Deploy order

**#348 must merge and deploy before #158.** The page reads fields that ship in the
backend. Migration 099 is additive and safe to deploy ahead of the worker: old
workers leave the new columns NULL, which reads as UNOBSERVED.

---

## Close-out (2026-09-22)

Everything under **Not done** above is now done. Both PRs are MERGED and live.

### The Codex gate ran — eight rounds

| Round | Backend | Frontend |
|-------|---------|----------|
| 3 | **P1** Celery time limit swallowed by the telemetry catch-alls; P2 ETA timed off the whole run | 3x P2: stale-label fallback, stalled runs saying "Searching", retry budget not reset |
| 4 | **P1** legacy counters written unguarded beside the guarded write; P2 mixed-unit record estimate; P2 template connectors (deferred) | 2x P2: stalled/retry vs stage_label precedence, queued runs shown as working |
| 5 | P2 measured `0 of N` returned null instead of 0% | 2x P2: activity cues on `isLive`, live region announcing the wrong string |
| 6 | P2 chunk denominator published then wiped by the next `report_stage()` | 3x P2: client-promoted done over stale label, "Trying again now", aria-live noise |
| 7 | P2 `queuing_contacts` above its own gates; P2 terminal job advertising `next_retry_at` | 2x P2: bar animating for stopped runs, Cancel leaving stale activity |
| 8 | P2 time limit must reach the task boundary (completes the round-3 P1); templates again | P2 retry copy promising a schedule |

No P1 after round 4. Two P1s total, both real, both verified against the tree before fixing.
The round-8 backend finding is the interesting one: it showed the round-3 P1 fix was only half
a fix, and that the other half was **pre-existing** — a `SoftTimeLimitExceeded` raised anywhere
in the scrape was already being classified as a permanent error by `is_transient_scrape_error`,
which bypassed `on_failure`'s deliberate "timeouts are RECOVERABLE" path and killed the job
instead of letting the watchdog recover it.

### One thing CI could not have caught

`main` landed `098_results_skip_trace_subject_hash` while this branch was open, and this
branch's migration was also `098`. Duplicate revision id, two alembic heads, and CI's
*Run Migrations* step failing before pytest started — so the Test job read FAILURE with no
change to this branch's code. Renumbered to **099** behind it (`097 -> 098 -> 099`); the two
touch different tables, so ordering carries no risk.

### Verified against a real run

Job `89b92687`, King probate, 06/01-09/20, 2 chunks. Full trace in
`docs/BUILD_JOURNAL.md` (2026-09-22). The three things the handoff asked for:

- **5m18s of "Connecting to the county records system" with no percentage at all.** That is
  the window that used to show a giant `0%`.
- **"Searching county records: Part 0 of 2" at a measured 0%**, then **"Collecting records:
  Part 1 of 2" at 50%.** Both fixes from rounds 5 and 6 are needed for that first line to
  exist at all.
- **Counters CLEARED at every stage change** — saving, exporting and enriching each show the
  record total and no unit counts.

Unplanned but the best evidence in the run: a worker deploy at 23:36:29 UTC killed the first
attempt mid-enrichment. The page stopped claiming "Adding property and mailing details" and
said **"No recent progress reported. Checking on this run."**; the watchdog re-queued it and
reset every observation to **NULL, not 0**. That state had only ever been exercised against a
stub API before.

Found **125**, billed **0** — all duplicates. That one pair is the whole feature: the old model
could only render `Records 0`.

### Still open

- [ ] `report_stage()` in the eight template connectors (owner: follow-up PR).
- [ ] The terminal label reads `Complete: 0 records` off the BILLED count, moments after the
      page said `125 records found`. Pre-existing wording; `records_found` now exists to fix it.
- [ ] A 2-chunk scrape jumps `Part 1 of 2` straight to `saving`, so the last chunk's completion
      is never shown and the percentage is never seen above 50.
- [ ] **C4** (above) — the watchdog resurrecting a cancelled job — remains its own fix.
- [ ] Test config `68ffc13e` is deactivated and renamed `[finished - safe to delete]`.
