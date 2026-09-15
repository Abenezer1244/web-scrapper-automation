# King property follow-ups (2026-09-15)

Branch `fix/king-property-followups`. Owner said "run fix and work with codex on all" after the
tax_delinquent dry run (12,306 candidates / 7 jobs: fill_condo 2,977, fill_gis 29, unresolved 9,300).

## Proven
- Beat starvation: beat + worker redeployed 00:58, 01:10, 01:19, 01:28 UTC (each < 20 min). Beat's
  PersistentScheduler file is not on a volume, so every deploy resets interval entries to a full period
  (celery 5.6.3 probe: fresh 1200 s interval -> next=1200; crontab(minute="11-59/20") -> next mark).
  `recover-deferred-property` has never fired in prod. Hourly interval entries starve the same way on
  busy merge days.
- 0 prod rows carry `property_lookup_deferred` (no King job since the deploy).
- owner_recovery.py never reads property_address or the owner-location flags (no overlap with the repair).

## Plan
- [x] A0 Codex consult on A + B (inline prompt, no repo access); reconcile (see Codex reconciliation)
- [ ] A1 Tax repair apply via guarded harness `kp_data/tax_apply_guarded.py` (count gate, before/after
      images, lock_timeout 5 s); peers told (no collision). `--dry` run: 12,306 / 3,006 fills, clean.
- [ ] A2 Verify: re-run dry run writes 0 fills; per-job property counts before/after; billing untouched
- [x] B1 Beat: interval entries >= 10 min -> wall-clock crontab, staggered (mailing 3-59/10, owners
      5-59/15, property 7-59/20, min 2 min apart; hourly at :17/:25/:39/:41/:57); comments say why
- [x] B2 `tests/test_beat_schedule.py` (14): no slow interval, fresh boot runs within its step, replay of
      the real 4 boots, old 1200 s starves, stagger, cadence, UTC. 5 mutations all fail a test.

## Codex reconciliation (2026-09-15)
- Adopted: count gate, durable before/after images, lock/statement timeouts, post-apply comparison;
  offsets spread for the shared lease; P3 comment date (09-15 UTC, not 09-14).
- Disproved: owner flags "overwrite truth" (all writers derive them via compute_owner_flags from
  property+mailing; these rows were computed with an empty property); stale whole-column
  enrichment_data writers (owner/mailing sweeps and the requeue script merge in SQL); two beat
  instances double-firing (Railway logs: old beat stops 39-50 s before the new one starts).
- Not changed: lease oversubscription (rates unchanged, pre-existing; empty ticks exit fast); 300 s
  entries stay intervals (a deploy costs one short period; aligning marks bunches paid dispatch).
- Prod: first-ever `recover_deferred_property` tick 01:49:32 UTC succeeded, 0 parcels.
- [ ] B3 ruff + targeted tests + full suite; Codex diff review; PR
- [ ] B4 After deploy: confirm a `recover_deferred_property` tick succeeds in worker logs

# King pre-foreclosure data quality: job 85692303 (2026-09-14)

Branch `investigate/king-prefc-dq` (worktree `C:/Users/Windows/bl-wt-kingprefc`).
Owner decisions 2026-09-14: this session builds the shared King property fixes with Codex; "Default
Owed" is RENAMED; Phase 1 (K1+K2) approved. Owner/completion copy went to the tax session (#298 merged),
so K4 is dropped here. Owner sweep (T3) and parcel-echo gate belong to the tax session.

## Proven (read-only prod + live King sources)
- Job 85692303, user ba64975a, config bc9d1ac8 "test8", 06/15-09/13/2026, doc_types NTS, 157 rows,
  155 new (2 same_run dups), billed 153. Skip trace off.
- ALL 157: date/party/parcel 100%, property 69.4%, mailing 98.7%, auction 0, default 0, phone/email 0.
  parcel+no property 48; parcel+mailing+no property 46; no auction+no default 157; property+mailing 109.
- Property: King GIS has 0 features for all 47 missing parcels. 45 = condo UNIT pins; 2 = 12-digit
  recorder PIDs. eRealProperty (the only live unit source) was NOT ADMITTED (lease busy; worker log
  `attempted=0 deferred=155 phase1_outcomes=not admitted (source busy)`). Deferral marker + recovery are
  mailing-only (enrich.py:925-931, mailing_recovery.py:155/175), so property is never retried.
- Regression boundary: property 98.7% on 09-02 (eRP filled 42/42), 70.3% 09-07 (breaker), 69.4% 09-13 (lease).
- 4 reps verified on eRealProperty = EXTR_CondoUnit2 unit address. 8135200390 sold 7/13/2026 (owner
  LYUBARSKY), NTS party is the old borrower.
- Auction/default: 0/155 in nts_notices (1 King paper, 37 notices). Recorded NTS has both, but King
  Recorder terms (re-verified live) forbid automated access/image mining. Not a parser defect.
- "Default Owed" = NTS Section IV principal/sum owing, not Section III arrears.
- Leading zeros: not a cause. Merge (_write_match) is event-atomic by design; PR #213 already COALESCEs
  notice re-crawls. Repair in place cannot touch dedup_hash, billing CAS, or Tracerfy enqueue.
- Repair scope: 103 rows / 49 parcels / 8 jobs: 44 condo extract, 2 via 10-digit PIN, 3 blank in extract.

## Plan
Phase 1 (commit 06fa78e, local, not pushed):
- [x] K1 Condo unit situs from EXTR_CondoUnit2: complete "STREET, CITY, ST ZIP" only when complex GIS
      ZIP == unit ZIP; published street kept, UnitNbr never appended; else snapshot-stamped condo_unit_status
- [x] K2 12-digit recorder value = tax ACCOUNT number (739,983/739,983 AcctNbr[:10]==PIN). Exact unanimous
      extract match, recorder-sourced rows only; STR check dropped (it cannot prove the minor)
- [x] Guarded DB writes, ORM sync/reload, eRP page may not overwrite an account resolution
- [x] Tests (27, mutation-checked), full suite 3241 passed, ruff clean, Codex design + 2 diff rounds
- [x] Real-data dry run: 45/48 blank rows filled in job 85692303, 4/4 equal eRealProperty
Phase 2 (awaiting go):
- [ ] Merge origin/main (#298) and re-verify
- [ ] K3 Property deferral marker + bounded lease-aware property recovery sweep; stop mailing recovery
      retrying 12-digit PIDs (use the resolved PIN)
- [ ] K5 Rename "Default Owed" in FE (bridgeleads-web) + export header, no em dash
- [ ] K6 Repair script: dry-run first, fill-only guarded UPDATE, apply only on approval
- [ ] K7 UI verification in Chromium after deploy

## Codex disagreements (recorded)
- Round 2 P1 "phase-2 mailing blocked by the guard": disproved. Phase 2 returns phase 1's seeded row
  (king_county_assessor.py:678, results[pid] writes), so resolved_parcel_id is top-level; test
  test_a_mailing_page_seeded_with_the_same_pin_is_applied proves it.
- Round 1 P1 "all later eRealProperty ORM writes should be atomic": pre-existing, outside this diff;
  Codex round 2 agreed it is not a blocker. Reported to owner.
- Round 2 P2 "db.flush() before the guard": both call sites run right after a commit with nothing
  pending; noted, not changed.
# King Code Violation: Parcel ID N/A + Party Name semantics (2026-09-14)

Branch `investigate/king-code-violation-parcel`, worktree `C:/Users/Windows/bl-wt-kingcv` (off main `dbe8b44`).
Status: DIAGNOSIS DONE (read-only). NO code written. Awaiting owner decisions + plan approval.

## Proven findings
- Only King CV job ever: `fbd872b6` (config `74db2d63` "sdfdhgfj", 08/13-09/12/2026, done, manual,
  skip_trace_enabled=false). 1,782 rows = 1,057 new (`record_count`) + 722 already delivered (+3 weak).
- Completeness: date 100%, party_name non-null 100% but REAL OWNER 0%, parcel_id 0%, kc_pin 1,389 (77.9%),
  property address 1,779 (99.8%), mailing 1,298 (72.8%), case id (recordnum) 100%, status 100%,
  violation category only inside party_name, phone/email 0%.
- Source: Seattle SDCI `data.seattle.gov ez4a-iug7`. City of Seattle ONLY. No parcel/owner/mailing field.
- Parcel = Case B. PR #283 locates PIN (strict point-in-polygon + street + ZIP, units rejected) into
  `enrichment_data.kc_pin`; never `parcel_id`, correctly: `_collapse_groups` (dedup.py:634) and reuse gate
  (enrich.py:156) require signature(current parcel_id, address) == stored dedup_hash; writing parcel_id = double billing.
  Defect = the located PIN is never surfaced to API/UI/export.
- Mailing: 1,265 king_rpacct + 33 king_tax_bill, 100% via kc_pin. Legit.
- Party name: scraper builds `"{recordtypedesc or recordtype} - {addr}"` on purpose. recordtypedesc (the
  category) is stored nowhere else. No King source in use has owner names (RPAcct extract is NoName; GIS layer none).
- Tracerfy: 0 pending rows for ANY code_violation ever; 0 spend. Gate works today only by string sniffing the label.
- Dedup: billing identity is address (parcel NULL); 1,782 cases on 1,462 addresses; separate cases at one property collapse by design.
- Codex GATE FAIL (verified): coverage labeling P1; party_name feeds source_fingerprint (tasks.py:892) and weak NAME|DATE hash (3 rows) P1.

## Owner decisions (2026-09-14)
- Parcel ID: show located PIN for STRICT (street + ZIP) matches only; never write parcel_id.
- Coverage: label code violations as Seattle (King County).
- Skip trace: code_violation rows with no real owner stay excluded (explicit record-type policy).
- Party name: owner asked "don't King County display real party names?" -> VERIFIED yes: eRealProperty
  Detail.aspx?ParcelNbr=9138100481 shows Name "7011 ROOSEVELT WAY NE LLC"; existing lease-guarded,
  parcel-echo-checked `batch_extract_king_owners` can resolve it by kc_pin.

## Review (2026-09-14, owner said "Proceed with all")
- [x] Phase 1 backend `2c80364`: scraper semantics, located tiers, API/CSV located parcel, skip trace gate. Full suite green (4 batches), ruff, OpenAPI check. Codex r4 GATE PASS.
- [x] Phase 2 live owner pass (enrich.py code_violation block, existing owner-only path).
- [x] Phase 3 repair script + prod DRY-RUN only (tiers exact 1,159 / street_only 141 / condo 89 / none 393; 936 PINs). Sample owners 6/6; 5/5 verified at county sources. APPLY NEEDS OWNER APPROVAL.
- [x] Phase 4 FE `3fe82ac` (bridgeleads-web): Parcel fallback, Violation + Case #, coverage notes. tsc/eslint clean, Playwright desktop + mobile, CSV via local API. Codex GATE PASS.
- [ ] Merge BE, deploy, then FE; then approved repair `--owners --apply` via Redis public URL wrapper.

## Proposed Phase 1 (backend, <=5 files) - DONE (see Review)
- [ ] Scraper: stop writing the label into party_name; store `violation_category` (recordtypedesc) +
      `violation_type` (recordtype); `raw_html_hash` from recordnum (fingerprint no longer reads party_name).
      Addressless weak-hash rows (3/month) handled per Codex before merge.
- [ ] Read-side located parcel on ResultRow + export (strict matches only, with source), never parcel_id/property_key/dedup.
- [ ] Export `code_violation_type` prefers category.
- [ ] Explicit record-type skip-trace policy: code_violation without a resolved owner is never traced.
- [ ] Tests for each; ruff + full suite on isolated DB; Codex gate.
## Phase 2: live owner pass for King CV (kc_pin strict -> batch_extract_king_owners -> party_name + owner_source), budgeted
## Phase 3: historical repair dry-run (category via Socrata refetch by recordnum; owners via paced eRealProperty on ~1,200 PINs; label party_name cleared only where owner resolved or with approval). Apply needs approval.
## Phase 4: FE columns (Violation Type, Case ID, Parcel) + Seattle label; Playwright desktop/mobile

# King data quality: tax owner/situs, pre-foreclosure situs/auction, CV parcel/party (2026-09-13)

Branch `investigate/king-tax-owner-situs` (worktree `C:/Users/Windows/bl-wt-kingtax`). DIAGNOSIS ONLY.
Status: diagnosis reported, Codex consulted; AWAITING OWNER APPROVAL before any code or prod write.

Jobs: tax `b2f2ecd5` (16,847 rows, 840 deliverable), prefc `85692303` (155), CV `fbd872b6` (1,060).

Root causes (evidence in session report):
1. eRealProperty phase 1 (the only live source of owner name + condo unit situs) was denied by
   SourceAdmission ("not admitted (source busy)") for every King job 12:51-13:31 UTC 2026-09-13;
   the denial writes only `mailing_lookup_deferred`, and only on rows missing mailing, which the RPAcct
   extract had already filled. Recovery sweep is mailing-only. Result: no retry for owner/situs.
2. King GIS parcel layer has no features for condo UNIT PINs (45/47 prefc, 3,358 tax gaps);
   EXTR_CondoUnit2 has the unit address (verified = eRealProperty Site Address).
3. 12-digit recorder PIDs (2 prefc rows) never match GIS/RPAcct; first 10 digits are the real parcel.
4. Auction/default: not in the LandmarkWeb index or any cached NTS notice (0/155). Document gap, not parser.
5. CV: source has no parcel/owner; kc_pin resolved for 809/1,060 but hidden; party_name is a case label.

Proposed phases (not started):
- [ ] P1 Condo unit situs from EXTR_CondoUnit2 in live King enrichment (fill-only, provenance key)
- [ ] P2 Owner/situs deferral markers independent of mailing + bounded owner/situs recovery sweep
      (lease-aware, delivered rows first, no quota/Tracerfy side effects)
- [ ] P3 12-digit PID -> 10-digit parcel resolution with legal-description STR check (beside parcel_id)
- [ ] P4 Honest UI/log states: Pending for owner/property when a lookup is deferred; fix completion copy
- [ ] P5 CV: surface kc_pin as a separate resolved parcel field; violation type/status columns; party_name decision
- [ ] P6 Backfill dry-run (scope below), then apply only on approval

## 2026-09-14 King TAX follow-up (same job b2f2ecd5; no newer King tax job exists)

Verified today (read-only prod + live source, 6 eRealProperty GETs at 5s, lease free, source healthy):
- Delivered 840 (600 billed + 240 no-address rows that bypass the cap): date 0, party 0, parcel/balance/
  oldest year 840, property 308 (37%), mailing 624 (74%), phone/email 0 (skip tracing off on this config).
- Balance + oldest year: 6/6 exact vs Socrata dsv3-ct3e (principal only; feed has no penalty/interest).
- Owner name exists on eRealProperty for 6/6. Site address: 1 full (already in BL), 1 condo unit (GIS gap),
  2 street-only vacant land, 2 blank. Leading-zero parcels GIS-matched correctly: NOT a cause.
- Date NULL is correct (receivable roll has no event date; fabricated 01/01/<year> removed in #214).
- New defects: (a) `enrich.py:959` counts ALL deferred pids as "mailing still being looked up" (16,859)
  though 16,576 had mailing; completion line repeats it. (b) owner-only pass requires mailing_address, so
  no-mailing rows (216 of the delivered 840) never get an owner lookup. (c) plan cap ranks by
  `party_name, date_recorded, id` (`tasks.py:1702`): which rows get billed depends on which ~240 random
  parcels the owner pass reached. Circular with "enrich delivered rows first" (Codex P1, verified).
- Party % history: Jun23 11.8, Aug10 0, Sep2 0, Sep4 1.2, Sep7 10.0, Sep13 0. Long-standing capacity
  limit (per-parcel page, 1 req/s, 240s owner budget), made total on 09-13 by lease denial.

Revised tax plan (Codex gate FAIL on the old P2; corrected below). T0-T2 BUILT, T3-T7 not started:
- [x] T0 Owner chose largest balance first. PR #300 (stacked on #298): plan_cap.mark_over_quota_rows ranks
      tax_delinquent by delinquent_amount DESC, older year, parcel, id; King tax lookups walk the same order.
      Full suite 3,232 passed, Codex PASS. Not merged (merge = prod deploy, awaiting owner).
- [x] T1 Copy: per-field counts (found / still deferred / no source value) for mailing and owner; no
      "pending" for rows that already have the value. No em dash in new copy.
- [x] T2 (owner half) Owner retry state per lead, reason retryable vs settled not_on_record (only when the
      page echoes the parcel); owner pass no longer gated on mailing_address. Situs half NOT built.
      PR #298 (CI green, not merged). 8 new tests; full suite green (9 billing
      failures were local env, pass with CI STRIPE_PRICE_* vars); ruff clean. Codex gate: fixed name
      guard + digit-free ids; OPEN: positive owners not gated on parcel echo (pre-existing, both paths).
      Owner: this session lands #298; session 86 (bl-wt-kingprefc) owns condo/12-digit PID/situs/sweep.
- [ ] T3 Bounded owner/situs recovery sweep in the worker (acquires the real lease), delivered rows
      first, fill-only, barred from cap/billing/delivery/Tracerfy paths.
- [ ] T4 Condo unit situs (= old P1). T5 property_address_status: street_only / no_site_address so the
      UI can say so instead of N/A (never substitute mailing).
- [ ] T6 FE: tax_delinquent shows "Tax year" instead of an empty Date column.
- [ ] T7 Historical repair dry-run for recent King tax delivered rows; apply only on approval.

# SSE "Too many concurrent streams (max 5)" (2026-09-13)

Branches: BE `investigate/sse-stream-cap` (worktree `bridgeleads-worktrees/sse-stream-cap`),
FE `investigate/sse-stream-cap` (worktree `bridgeleads-worktrees/fe-sse-stream-cap`).
Status: Phase 1 (backend) DONE and verified locally, not pushed. Phase 2 (frontend) awaiting approval.

## Proven findings (local uvicorn + private Redis 6391 + isolated `_test` DB + real Chromium)
- Source: `src/api/routes/jobs.py` `stream_logs` -> `event_stream()`, `_MAX_SSE_PER_USER = 5` hardcoded,
  Redis INCR counter `sse_count:{user_id}` (TTL 120s, refreshed only on INCR). Per user, across all tabs/jobs/replicas.
- Transport: fetch + ReadableStream reading `text/event-stream` (not EventSource), Redis Pub/Sub `job_logs:{job_id}`.
- LEAK: on any client disconnect, the finally block's `await r.delete(conn_key)` raises CancelledError
  (anyio re-cancels), so `decr` never runs and `r.aclose()` is cancelled. Only server-ended streams release a slot.
  Chromium: ONE tab reloaded 4x -> 5th load rejected with 1 real subscriber. Navigate away, close tab, close browser: all leak.
- Self-sustaining lockout: every rejected attempt re-arms the 120s TTL.
- Bypass: streams older than 120s outlive the counter key; user held 10 live streams with cap 5.
- Cancelled jobs publish no terminal event, so their stream stays open up to 30 min.
- Scrape unaffected: worker never reads publish results/subscriber counts; status page polls GET /jobs/{id} every 3s (3 polls/10s observed while capped).
- FE: rejection is HTTP 200 + `{"type":"error"}` without `level` -> rendered as `[INF]` log line; hook exits
  normally with `isConnected` still true -> false LIVE badge, no retry, no reconnect. `timeout`/`cancelled` unhandled.
- Authorization OK: ownership check on Job.id + user_id, replay filtered by user_id, channel is job-scoped.

## Phase 1 (backend) - DONE
- [x] Lease ZSET `sse_leases:{user_id}` in new `src/api/sse_leases.py`: atomic Lua admit, renew only unexpired leases,
      Redis TIME as the one clock, shielded + time-bounded release and Pub/Sub close; expiry is the backstop.
- [x] Admission before the stream; over cap = HTTP 429 + Retry-After (no fake log line). Finished jobs take no lease.
- [x] `SSE_MAX_STREAMS_PER_USER` (default 5, validated >= 1) in settings + `.env.example`.
- [x] Stream re-checks job status every 60s (and once at open) so cancelled / recovered jobs end their stream.
- [x] Found during build: each open stream held a Postgres connection "idle in transaction" (NullPool) for up to
      30 min. Request transaction now committed before streaming; stream reads use short-lived RLS sessions.
- [x] Found by Codex r2 (P1): a line could be lost between replay and subscribe. Worker `_publish_log` now commits
      before publishing; the stream subscribes first, then replays.
- [x] Logs: sse opened / rejected / closed with reason and lifetime (user and job ids, no tokens).
- [x] Tests `tests/test_sse_leases.py` (14): cap per user, no overshoot under concurrency, release, crash expiry,
      refusals do not extend lockout, renew guard, 429 + Retry-After, finished job no lease, other tenant 404,
      real-socket disconnect release, 10 refreshes, terminal event, cancelled-without-event, worker ordering.

## Review (Phase 1)
- Files: `src/api/sse_leases.py` (new), `src/api/routes/jobs.py`, `src/workers/tasks_helpers/status.py`,
  `src/config/settings.py`, `.env.example`, `tests/test_sse_leases.py`, `schema/openapi.json` (regenerated: route
  description only). 6 source files, one over the 5-file phase guideline because the Codex P1 fix needed the worker.
- Mutation-verified: removing the shield, the renew score guard, or commit-before-publish each fails a test.
- Full suite on the branch: 3093 passed. 16 failures reproduce identically on pristine `origin/main` 8cc709f in the
  same env (anthropic wheel truncated by Windows MAX_PATH 263 > 260; missing Stripe price env) = environmental.
- Chromium E2E (local API + FE master, unchanged): 10 reloads -> 1 lease; navigate away -> 0; 6th stream -> 429 x4
  while job polls keep returning 200; close one -> Retry admitted; user B admitted while A at cap; terminal event
  releases only that job's streams; iPhone 13 viewport 8 open/reload/leave cycles -> 1 lease; all closed -> 0.
  Postgres "idle in transaction" during an open stream: 1 before, 0 after.
- Codex: r1 GATE PASS (7 P2/P3), r2 GATE FAIL (replay gap P1, fixed), r3 GATE PASS.
- Deferred follow-ups (Codex P2/P3, all pre-existing): unbounded replay size; terminal events are not persisted
  (status check closes within 60s, page status poll is 3s); publish failure after commit only reaches viewers on
  reconnect; no shutdown close for the module Redis client (same as rate_limit); TIME/EVAL two round trips.
- Local rig note: the current FE on 429 retries 3x then shows "Disconnected. Retry" (honest but not the target UX;
  Phase 2). Chrome's 6 connections per host (HTTP/1.1) means 5 streams in ONE browser profile starve its other
  API calls locally; production protocol still unverified.

## Phase 2 (frontend) - DONE, bridgeleads-web PR #132 (backend PR #297)
- [x] Backend add-on (PR #297 `e269532`): CORS `expose_headers=["Retry-After"]`; the app could not read it cross-origin. Test + mutation check.
- [x] Hook rewrite (`hooks/use-log-stream.ts`): states connecting/live/paused/failed/ended; one restart path + generation
      counter; 429 or legacy 200 refusal -> paused, retry >= Retry-After with 15/30/60s backoff + jitter; terminal ends;
      timeout / unterminated end reconnects; jobFinished stops retries, live stream swapped for a slot-free replay after 5s
      (also armed on admission-before-finish); hidden 30s / pagehide releases, visible / pageshow resumes; events validated.
- [x] UI: "Live updates paused. Your scrape is still running." + "Retry live updates" (stalled run: "Live updates paused.");
      header indicators status only; one retry control. No em dashes.
- [x] Codex: consult, then 4 review rounds (3 FAIL, all fixed) -> GATE PASS. Declined with evidence: 401 special path;
      id-less log lines (backend `LogLine.id: str` required).
- [x] Chromium E2E (P1-P12) all pass; focused checks: render once across 10 reloads, cancel-while-paused replay 3/3.

## Review (Phase 2)
- Not verified: WebKit / iOS Safari (Playwright WebKit on Windows would not keep the local 127.0.0.1 auth session).
- Not verified: production edge HTTP/1.1 vs HTTP/2.
- ⏭️ After #297 merges: regenerate FE `lib/api-types.generated.ts` (route description changed; FE CI gate reads BE main).
- ⏭️ Merge order: either is safe (FE handles old 200 refusal and new 429).

---

# Mailing follow-ups after #283 (2026-09-13)

Branch `feat/mailing-followups` (worktree `C:/Users/Windows/bl-wt-rpacct`, from `origin/main` @ `a55308f`).
Owner approved all follow-ups, turning the Snohomish/Cowlitz restricted mailing flag on, and a UI check.

## Done without code
- [x] #286 journal merged `a55308f`
- [x] `COUNTY_GIS_RESTRICTED_MAILING_ENABLED=true` on worker + api (deploys 42aeeba1 / 267fe7ff SUCCESS); prod env reports Snohomish + Cowlitz as mailing sources; 0 rows created while the flag was off
- [x] Snohomish Test 5 (job 425d49ce, 4 rows): county taxpayer mailing equals the stored value for all 4. No write: Snohomish GIS rows never carry `mailing_source`, so these are now indistinguishable from sourced rows and confirmed

## Phase 1 (#288 `2f1cb5c`)
- [x] A. `no_coordinates` terminal kc_pin_status (Codex: a SQL filter still loops on junk strings); 15 prod rows stamped, backfill converged to 0
- [x] B. Auto skip trace skips code_violation `Completed` / `Open Duplicate`; `Closed` still traced

## Phase 2 (#289 `2945ebd`, #291 `345ee16`)
- [x] Found: the King tax-bill parser kept 2 lines, so 3-line addresses lost city/state/ZIP. `parse_mailing_block` (real state code or Canadian postal required)
- [x] Code-violation party names use " - " (King + Pierce); 1,836 existing rows rewritten (all strong parcel|address identity)
- [x] Truncation repair from the Assessor extract: 3,511 rows written, 0 guard skips
- [x] `scripts/king_taxbill_mailing_check.py` (renamed from the plan's `_verify`): 2 runs, 209 rows written (146 echo, 30 truncated, 33 cv); live spot-check 3/3
- [x] D. Results page UI check (owner login, never stored): 44/50 page-1 rows show mailing, 0 stuck Pending

## Review
- King code violations with mailing: 1,265 -> 1,298 of 1,782. Situs echoes left: 3 of 149. Unsourced King mailings without a postal code: 112 of 48,094, almost all county placeholders ("ADDRESS UNKNOWN").
- Remaining tax-bill unknowns are real non-answers: 91 cv parcels have no tax account; 104 truncated rows show a placeholder on the county's own bill.
- Environment, not code: the harness killed the first tax-bill run's Playwright driver for low memory (nothing written); reran detached. Locally the shared King lease failed open (`redis.railway.internal` unresolvable); reran through the public Redis URL so the lease was really shared.
- Codex: every design consult and diff review ran; all P1s reconciled before build, diff gates PASS, P2s fixed.

---

# Single-user 3-month Agency promotion through Stripe

Branch: `feat/stripe-promo-access` · worktree `C:/Users/Windows/bridgeleads-worktrees/stripe-promo` (from `origin/main` @ `ff9ecd6`, includes #268)

## Findings (current architecture, read from origin/main)

| # | Question | Answer | Where |
|---|---|---|---|
| 1 | Checkout Session creation | `POST /billing/checkout`: advisory lock 4243, re-read user, resolve/adopt customer, **refuse if any non-terminal subscription exists**, expire open sessions, re-check, then `Session.create(mode="subscription")` with the plan price + metered skip-trace price | `billing.py` `create_checkout` |
| 2 | Subscription creation | Only via Checkout. `/billing/change-plan` modifies, never creates | `billing.py` |
| 3 | Customer id storage | `users.stripe_customer_id`, persisted after the Session exists; bound again in `checkout.session.completed` if NULL; mismatch refuses | `billing.py` |
| 4 | Subscription id storage | `users.stripe_subscription_id`, written by `activate_paid_plan` / `apply_plan_change`, cleared by `end_subscription` | `billing_entitlement.py` |
| 5 | Price to plan mapping | `_PRICE_TO_PLAN` from `PLAN_CATALOG` (monthly + annual ids). Licensed item found via `_plan_item_price_id` | `billing.py`, `plans.py` |
| 6 | `checkout.session.completed` | Retrieves the subscription, maps the plan price, locks user `FOR UPDATE`, `activate_paid_plan(status=subscription.status)`. **Never reads `payment_status`, amount, or PaymentIntent** | `billing.py` |
| 7 | Subscription webhooks | `updated` re-reads from Stripe, `apply_plan_change`; `deleted` downgrades to Starter. **`customer.subscription.created` is not handled** | `billing.py` |
| 8 | $0 invoices | No amount checks anywhere in `src/`. `invoice.payment_succeeded` only clears dunning | grep: no `amount_*`/`discount`/`payment_status` reads |
| 9 | Promotion codes in Checkout | **Already enabled**: `allow_promotion_codes=True` on every checkout (founding coupon `FOUNDING25` exists) | `billing.py` |
| 10 | Discount affects entitlement? | **No.** Plan, records limit, county limit, record types all derive from `users.plan`, which derives only from the Price | `constants.py` |
| 11 | Monthly vs annual | Both map to the same plan; entitlement window is always monthly; interval switch through `change-plan` sets `billing_cycle_anchor="now"` | `billing.py` |
| 12 | Entitlement audit (#268) | Added the second-subscription guard, `change-plan`, anchor/quota rules. The promo must go through that guard, not around it | merged |

### Stripe behaviour verified against docs.stripe.com (billing/subscriptions/coupons)
- `duration=repeating, duration_in_months=3`: monthly subscription gets the first 3 invoices discounted.
- **Yearly subscription: "the discount applies to the entire year if the subscription renews within the N-month window."** A 3-month 100% coupon on Agency annual = a free year ($14,390). This is the conflict to guard.
- Promotion codes support `customer` restriction, `max_redemptions`, `expires_at`; coupons support `applies_to.products`, `max_redemptions`, `redeem_by`.
- Customer portal plan switching is disabled on this account (per code comment), so the portal cannot re-apply codes.

### Risks found
- R1 (High): annual Agency checkout with the 3-month code = full year free.
- R2 (High): an Agency monthly promo holder calling `change-plan` to annual inside the 3 months issues an annual invoice inside the discount window = full year free.
- R3 (Medium, pre-existing): webhook Redis dedup key is claimed BEFORE handling and never released on failure. If `checkout.session.completed` raises (e.g. Stripe retrieve blip), Stripe's retry is skipped as "already processed". A $0 subscription created active may never send a later `updated`, so the customer stays unactivated.
- R4 (Medium): a coupon without `applies_to` would also zero the metered skip-trace overage for 3 months. Mitigated by config (`applies_to = Agency product`), since skip-trace is its own product.

## Plan

### Phase 1 (code, 3 files) - DONE, 27 tests, every guard mutation-proven
- [x] Checkout: annual sessions NEVER offer the code box (`allow_promotion_codes = interval != "year"`); monthly unchanged; `payment_method_collection="always"` explicit. (First built as a per-customer promotion lookup; replaced after Codex r1 showed it was a check-then-act race: a code issued while the Session is open still worked.)
- [x] change-plan: 409 `promotional_pricing_active` on a switch TO annual while a repeating (<12 mo) discount is still running on the subscription; checked before any side effect.
- [x] Webhook claim: `processing` (300s) until handler + `db.commit()` succeed, then `done` (3 days); released on exception; an in-flight duplicate gets 409 so Stripe retries instead of treating it as delivered. No migration.
- [x] `checkout.session.completed`: activates only an `active`/`trialing` subscription.
- [x] `customer.subscription.created`: routed through the update handler, gated on the RE-READ status; a failed re-read raises (retry), never trusts the body.
- [x] `customer.subscription.updated`: an UNRECORDED subscription changes nothing unless `active`/`trialing` (was: an `incomplete` Agency sub ranked as an upgrade). The recorded subscription keeps every transition.
- [x] `customer.subscription.deleted`: always asks Stripe for a surviving active/trialing subscription on a sold price; rebinds to it through the update handler instead of downgrading; list failure raises (retry).
- [x] Ops alerts: annual subscription carrying a short repeating coupon; checkout completing while a different recorded subscription is still live.
- [x] `scripts/stripe_single_customer_promo.py`: args only, refuses live key without `--live`, verifies customer `metadata.user_id`, refuses customers with a live subscription, validates an existing same-code promotion's shape, idempotency key covers every material parameter.
- [x] ruff (CI pin 0.15.6) clean; no em dashes added.

Codex r1 (post-build) rejected/deferred with reasons: change-plan read-then-modify race (only our operator can attach coupons; portal plan updates disabled); durable webhook ledger / claim lease (needs a migration and a webhook redesign the owner ruled out; reported as risk).
Codex consult rejected (false on the code): email-only customer adoption; missing signature verification.

### Phase 2 (Stripe TEST mode, needs a `sk_test_` key)
- [ ] Test clock customer, Agency monthly Checkout with the code: $0 due, subscription active, entitlement Agency/-1/-1.
- [ ] Second redemption refused; different customer refused; annual Checkout shows no code box for the holder.
- [ ] Replay the real signed events: no quota reset, no duplicate.
- [ ] Advance clock past 3 months: discount removed, invoice 4 charges $1,499, plan unchanged, no reset.
- [ ] Cancel: `deleted` downgrades normally. Checkout again while active: 409.

### Phase 3
- [ ] Codex diff review + Master Security Review §14; review section below; journal entry.

## Decisions to confirm with the user
1. Skip-trace overage during the 3 months: billed normally (recommended, `applies_to` Agency product only) or free?
2. Code redeem-by window (recommended 14 days).
3. Does the recipient already have a BridgeLeads account? A Stripe subscription?
4. A Stripe test-mode secret key for Phase 2.

## Review
**Phase 1: done and gated.** Codex consult + 6 review rounds; final round GATE: PASS. Every finding was
checked against the code before adoption (2 rejected as false). 32 promo tests, each guard
mutation-verified. Full CI target 2899 passed / 2 skipped / 0 failed; billing integration 145 passed;
ruff (0.15.6) clean; no em dashes added.

Files: `src/api/routes/billing.py`, `scripts/stripe_single_customer_promo.py` (new),
`tests/test_promo_access.py` (new), `docs/BUILD_JOURNAL.md`, this file. Nothing committed.

**Phase 2: NOT run.** No Stripe test key was provided (`C:/Users/Windows/bl-testenv/stripe-test.env`
absent). Verifier ready in the session scratchpad. Do not create anything in live Stripe until it passes.

Open for the owner: FOUNDING25 is no longer enterable on annual checkout; enable
`customer.subscription.created` on the webhook endpoint; verify the endpoint API version.
Remaining risks: no durable webhook ledger (claim lease 300s, commit ambiguity); Session-create race
without idempotency key (pre-existing; ops alert added); change-plan vs. operator Dashboard coupon race.

### Phase 2 (owner follow-up, same day): FOUNDING25 kept, risks closed, Stripe sandbox verified
- [x] Annual app gates removed; single-customer coupon scoped to the Agency MONTHLY product (script refuses products with yearly prices, reads `applies_to` back). Codex consult: FAIL -> reconciled PASS.
- [x] Durable webhook ledger (migration 095 + RLS grants/policies mirrored in provision/cutover/force SQL; exercised under real roles in a rolled-back transaction).
- [x] Post-create checkout re-check; legacy plan price aliases; invoice webhooks on API 2025-03-31+ (`parent.subscription_details`).
- [x] Codex post-build PASS + 2 delta PASS. Suite 2909 passed / 2 skipped; billing integration 145 passed / 10 skipped.
- [x] Stripe sandbox e2e: 43/44 pass, 0 fail, 1 skipped (test clock needs the sandbox claimed).
- [ ] 3-month test-clock run (owner: claim sandbox `acct_1UF5cBIoeMQyAQ5z` before 2026-09-20).
- [ ] LIVE (owner approval): Agency monthly product + price, env swap + `STRIPE_LEGACY_PLAN_PRICES`, enable `customer.subscription.created`, run the script with `--live`.

### Phase 3 (2026-09-13, branch `chore/stripe-followups`): handoff section 9 step 1
- [x] FOUNDING25 promotion code created LIVE (owner approved): `promo_1UFBJtHE9wT1C7yZS3O21fcH` on coupon
      `FOUNDING25`, no customer/expiry/code cap (coupon cap 25 is the limit). Read back active; re-run reports EXISTS.
      Script `scripts/stripe_founding_code_and_webhook_events.py` (dry run by default, `--apply --live`).
- [x] `invoice.payment_succeeded` NOT enabled (owner had approved; withheld on review). Codex consult FAIL (3 High
      on `_handle_payment_succeeded`: stale late event clears dunning / writes `active` without a Stripe re-read,
      no `FOR UPDATE` against `invoice.payment_failed`, overage invoices on the same subscription count as plan
      payment). Reconciled: recovery already clears dunning via `customer.subscription.updated` ->
      `apply_plan_change` (billing_entitlement.py:265-269), so the event is redundant today. Codex agreed; PASS for FOUNDING25 only.
      Codex's Medium (`coupon=` removed on clover) withdrawn: stripe 11.4.0 pins `2024-12-18.acacia`.
- [ ] Follow-up before ever enabling `invoice.payment_succeeded`: harden both invoice handlers (row lock,
      Stripe subscription re-read, plan-line vs overage invoice). Same stale-event gap exists today in
      `_handle_payment_failed` (writes `past_due` without a re-read).
- [ ] 3-month test-clock run: owner has NOT claimed sandbox `acct_1UF5cBIoeMQyAQ5z` yet (deadline 2026-09-20).
- [ ] After 2026-09-16: remove the Redis cutover dual-read.

### Phase 4 (owner-approved, DONE on `fix/stripe-webhook-ordering`): harden invoice + subscription webhook ordering
Codex design consult on the first draft ("invoice events delegate to _handle_subscription_updated"): FAIL, adopted:
delegation drags in plan changes, first-observer quota reset, scraper reconciliation and alerts; and the row lock is
taken AFTER the Stripe re-read, so a slow stale read can still overwrite a newer one. That last point is ALSO true of
the live `customer.subscription.updated` path today.

Decision: dunning follows the subscription's CURRENT Stripe status (Stripe's own retry/dunning settings decide
past_due), not "any unpaid invoice". `invoice.payment_succeeded` stays disabled; recovery is owned by
`customer.subscription.updated`.

- [x] 1. `_handle_subscription_updated`: take `User ... FOR UPDATE` BEFORE the Stripe re-read, so the last writer
      always holds the newest read (fixes the live stale-overwrite race). No behaviour change otherwise.
- [x] 2. `_handle_payment_failed`: lock user first, then re-read the subscription and the invoice from Stripe (a
      failed read raises: no ledger row, Stripe retries). Start grace + `past_due` only if the invoice's subscription
      is the recorded one AND is currently past_due/unpaid. Never changes plan, limits, window, reconciliation or
      alerts. Send the failure email/in-app notice only if the invoice is still `open` (a stale failure for a since
      paid/void/uncollectible invoice is silent). Non-subscription invoices: email as today, no state change.
- [x] 3. Remove `_handle_payment_succeeded` and its dispatch branch (never ran in prod; if the event is ever enabled
      by accident it is ignored). `mark_payment_succeeded` removed only if nothing but its own tests uses it.
- [x] 4. Tests `tests/test_billing_invoice_webhooks.py` (real test DB; only Stripe reads patched): stale failure while
      sub now active = no grace, no email; failure while past_due = grace; invoice now paid = no email; Stripe read
      failure raises with no state change and no ledger row; non-subscription invoice = email, no state; unrecorded
      subscription = nothing; ordering test proving the lock precedes the re-read. Update `test_notification_payment.py`.
- [x] 5. Verify: billing test files in foreground batches on a `_test` DB (C:/v312 venv), ruff 0.15.6, Codex review
      gate, security Master Review. PR; no live Stripe change needed.
Known, accepted (pre-existing, P2): an email sent before a failed commit can repeat on Stripe's retry.

#### Phase 4 review
- Built as planned, plus two Codex gate adoptions: `_handle_subscription_deleted` also locks the user row before its
  Stripe survivor lookup (same stale-write class, High), and an invoice event with no id raises instead of notifying
  (Medium). Tests went into `tests/test_promo_access.py` (not a new file) to stay inside the per-phase file budget;
  `tests/test_billing_webhook_gap.py` needed a real session because the handler now locks before its early return.
- Deviation from the plan text: the "invoice still open" check applies to non-subscription invoices too (a stale
  failure for a since-paid one-off invoice is also silent).
- Evidence: mutation checks caught all 5 guards; billing files 103 passed; full `not integration` suite in 4 batches on
  an isolated `_test` DB: 3,098 passed, 3 failed only in `test_session_refresh_contract.py`, which passes alone (7/7,
  order/state dependent, untouched by this diff). ruff 0.15.6 clean. Codex gate FAIL -> reconciled -> delta PASS.
- Deferred (Codex Medium, pre-existing module-wide): synchronous Stripe calls with default timeouts and the failure
  email run while the user row lock is held. Follow-up: bounded Stripe timeouts + post-commit notification.

### Phase 5 (owner: "do it yourself", 2026-09-13, branch `fix/stripe-timeouts-post-commit-notify`)
Codex Medium deferred from #290: Stripe calls ran on SDK defaults (80s x 3 attempts) and the payment-failed email
was sent inside the webhook transaction.
Codex design consult on the first draft (SQLAlchemy after_commit session events): FAIL, adopted: the listener is
synchronous on the event loop and fires on nested savepoint commits; returning notifications as data and scheduling
them after the route's commit is simpler. Dropped: masking emails in delivery.py logs (already masked by the
`setup_logger` redaction filter).
- [x] `STRIPE_TIMEOUT_SECONDS=10`, `STRIPE_MAX_NETWORK_RETRIES=1` (settings + .env.example); `src/config/stripe_client.py`
      `configure_stripe()` at billing-route import and in `src/workers/__init__.py`. SDK retries reuse the idempotency key.
- [x] `_handle_payment_failed` returns its notifications; `stripe_webhook` runs them as BackgroundTasks after commit,
      each guarded so an email failure cannot swallow the in-app notice.
- [x] Tests + mutation checks (send-before-commit, unguarded runner, ignored timeout: all caught). 107 + 159 billing and
      entitlement tests pass; ruff clean; `export_openapi.py --check` OK.
- [x] Codex review gate: PASS, no findings (residual: a notification can be lost if the API dies after commit; accepted).
- [ ] PR, CI, merge, deploy check.
Trade accepted: at-most-once notification (a crash between commit and send loses one email, logged) instead of a
duplicate email after a failed commit.
