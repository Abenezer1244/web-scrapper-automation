# Security audit #4, Phase 4b

Report: `SECURITY-AUDIT.md` (Audit #4 section, on PR #374). Owner approved 2026-09-27: 4b-i now from `origin/main`;
4b-ii after #374 merges; S4-07 as its own phase after 4b. Merging deploys, so stop before merge.

## Plan (reconciled with the Codex consult, 2026-09-27)
State at planning: #374 OPEN, not merged, CI green; `origin/main` still `f80f79ce`. Codex consult verdict
"PLAN NEEDS CHANGES" (6 findings, all folded in below). Folding them in makes 6 files, over the 5-file phase
limit, so 4b splits in two. The S4-03 half does not depend on 4a; the log half needs 4a's `report=`.

Completeness sweep: only `jobs.py`, `batches.py`, `segments.py` build CSVs. Jobs' two are already `export`;
the four below are the rest. Frontend calls all four only from a button click (never polled).

### 4b-i: S4-03 export zone (branch from `origin/main`, independent of #374; 3 code files + 1 test)
- [ ] `src/api/routes/batches.py`: `download_batch` (733), `download_batch_run` (854) -> `zone="export"`.
      The limit stays BEFORE the owner lookup, so a 404 still spends budget.
- [ ] `src/api/routes/segments.py`: `intersection_export` (709), `union_export` (835) -> `zone="export"`.
- [ ] `src/api/middleware/rate_limit.py`: comment only. Lists the 6 export routes and states the real
      ceiling: a job export is `/export-url` + `/download` = 2 tokens, so 10 job exports/min (Codex #2).
- [ ] `tests/test_audit4_export_zone.py` (real DB + real Redis, no mocks). REGRESSION (fail on old code,
      each mutation-checked):
      1. each of the 4 routes alone: 20 pass, the 21st in the minute is 429.
      2. shared budget: 20 exports mixed across job + batch + segment routes, then the 21st on each new route
         is 429.
      3. rate limit before owner 404 (Codex #3): both batch routes with a nonexistent / other tenant's id:
         calls 1-20 are 404, call 21 is 429.
      4. Redis down (unreachable client, as `test_audit3_download_and_limits.py` does): batch and segment
         exports still 429 after 20 (old: `general` fails open).
      CONTROLS (pass on old code too, guard against over-reach):
      5. a job `export-url` -> `download` pair spends 2 tokens: the 11th pair's first call is 429 (pins the
         documented ceiling).
      6. 21+ calls to a segment preview and `/jobs/{id}/results` are not 429 (still `general`).

### 4b-ii: held-lead live-log line (stacks on 4a; branch after #374 merges; 2 code files + 1 test)
- [ ] `src/workers/tasks_helpers/enrich.py` `_enqueue_skip_trace_rows` (~2696): `report = {}` passed to the
      claim; ONE customer line published AFTER the enqueue commit (`_publish_log` commits). Ops count:
      `lost = max(0, len(to_claim) - len(claimed) - report.get("held", 0))`. Copy (no em dashes, no promise
      that held leads are looked up later, because nothing re-queues them: enqueue is job-scoped,
      enrich.py:2333):
      - trial: "Contact lookups were not run for N lead(s): your free trial includes up to {allowance}
        lookup credits, and not enough remain for these leads. Paid plans include contact lookups for new
        leads."
      - trial, allowance 0: "Contact lookups were not run for N lead(s): your free trial does not include
        contact lookups. Paid plans include contact lookups for new leads."
      - frozen: "Contact lookups were not run for N lead(s): your account is frozen because a payment did
        not go through."
      - ended: "Contact lookups were not run for N lead(s): your paid plan has ended."
- [ ] `src/workers/skip_trace_claim.py`: docstring only. Remove "a run after the customer subscribes looks
      it up" (4a claim, untrue: nothing re-queues a held lead of an earlier job).
- [ ] `tests/test_audit4_held_lookup_log.py` (real DB, drives `_enqueue_skip_trace_rows` like
      `test_skip_trace_enqueue_after_delivery.py`). REGRESSION: trial allowance 2 + 5 leads -> one JobLog
      line naming 3 leads; allowance 0 wording; frozen and ended wording; held rows stay `not_attempted`
      with no pending queue row; `lost` ops log does not count held rows (caplog). CONTROL: paying account
      gets no held line. No em dash in any line.

### New finding from the consult (NOT in 4b; owner decision)
- **S4-07 (P2, Codex consult #1, driver-verified):** five JSON views decrypt up to ~500 PII rows per call
  and sit in `general` (60/min, fails open): `/segments/intersection`, `/segments/union`
  (segments.py:650,798), `/jobs/{id}/results` (jobs.py:509), batch lead views (batches.py:1007-1087).
  Fix: a separate `bulk_read` zone (Codex's preferred option), after checking the frontend's paging and
  filter call rate so a normal browse is not throttled.

### Verification (each of 4b-i and 4b-ii)
- [ ] ruff; new tests; full suite on a fresh `_test` DB, FOREGROUND batches (`suite-batch.sh init`, 0..5)
- [ ] OpenAPI: no schema change expected; confirm with a diff
- [ ] Security Master Review (x2 clean), Codex review until GATE PASS, PR, STOP before merge

