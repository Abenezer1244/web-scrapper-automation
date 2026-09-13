# Handoff: "already delivered" / "combined" semantics (D1-D5)

**Date:** 2026-09-13
**Worktree:** `C:/Users/Windows/bridgeleads-worktrees/results-categories`
**Branch:** `investigate/results-categories` (local only, NOT pushed, no PR). HEAD `3e4ce26`.
**Base:** cut from `origin/main` `ff9ecd6`, merged `origin/main` `18fcdf3` in `51c953b`.
**Status:** Phase A (D1 + D5) implemented and tested. Codex diff-review loop at round 6
(rounds 1-5 fixed; see §3 and §8). Phase B (D2 + D3 + D4) designed, NOT started, blocked on owner
approval of billing semantics.

---

## 1. The goal

The owner saw Results headers like `51 new · 4 already delivered · 2 combined` and asked:
what do those counts mean exactly, are they tenant-safe, can users inspect them, and then
"solve all D1-D5 with codex". The rules they set: never guess, trace the real code, verify
against real data, never modify production records, isolated worktree, Codex consulted and
its findings independently verified, no em dash in modified user-facing copy.

## 2. What was established (verified in code + read-only production queries)

- **"Already delivered"** = `results.is_duplicate AND duplicate_reason='prior_run'`. Set when this
  job's `INSERT INTO delivered_records ... ON CONFLICT (user_id, dedup_hash) DO NOTHING` loses
  (`src/workers/tasks.py` dedup step). Key = FROZEN `sha256(parcel|address as scraped)`
  (`legacy_strong_signature`), else `NAME|DATE`. Per user. NOT scoped by county or record type.
  No expiry. "Delivered" really means "claimed by an earlier run that kept the claim".
- **"Combined"** = `duplicate_reason='same_run'`: rows in ONE run sharing a strong hash; one
  survivor (`collapse_same_run_siblings`, trustee_sale in `trustee_sale_finalize.py`). First fired
  in prod 2026-09-10 (180 rows, 6 jobs, 1 user).
- **Tenant isolation holds:** 0 violations on 4 schema-wide checks; `UNIQUE(user_id, dedup_hash)`;
  17,834 hashes held independently by several users.
- **Quota:** duplicates of either kind are never billed (billing = non-dup AND actionable).
- **CSV:** worker export and `/jobs/{id}/download` contain new, actionable rows only.
- **Screenshot jobs** (all user `b6d2095d…`, Agency): `c04f078a` Pierce pre_fc 16/79,
  `6b86ea0b` Pierce code_violation 51/4/2, `b96812ec` Pierce trustee 18/0/1,
  `e2c2caf1` Snohomish trustee 1/13 (all 13 claimed by pre_foreclosure runs, one started 1 min earlier).

The defects found (D-numbers are the owner's):

| | Defect | Status |
|---|---|---|
| D1 | 419 "already delivered" rows whose original never had an address (never delivered/billed); claims never expire | **FIXED in Phase A** (claim transfer) |
| D2 | Cross-record-type suppression (auction lead hidden by a pre-foreclosure delivery) | Phase B, not started |
| D3 | Same-run collapse merges distinct events/people on one parcel (two death certs, two code cases) | Phase B, not started |
| D4 | A new event on an already-claimed property is suppressed forever | Phase B, not started |
| D5 | Skip trace enqueued before survivor re-election and plan cap | **FIXED in Phase A** |

## 3. Commits on the branch (oldest first)

| SHA | What |
|---|---|
| `6b91a88` | D5: enqueue moved from end of `_run_inline_enrichment` to `tasks.py` after re-election + plan cap; export rows reloaded with `populate_existing` for every plan; dispatcher withdraws rows whose lead became duplicate/over-quota |
| `cc113c4` | D1: `transfer_undelivered_claims` (dedup.py) moves a claim to the run that delivers the lead; old anchor -> `duplicate_reason='superseded'`; wired after reconcile, before the refetch/cap |
| `9ea903b` | Results API excludes `superseded` from counts and `duplicate_sources`; invariant checker knows the value |
| `51c953b` | merge origin/main (#275-#278; #275 widened mailing recovery, which strengthens "move, never release") |
| `9e3a688` | Codex review r1: release claims on both in-worker cancellation exits; dispatcher re-read locked; per-hash failure isolation (`_transfer_one_claim`); tenant filter on the claim UPDATE |
| `b0a97fc` | Codex review r2: cancellation release re-checks `status='cancelled' AND billing_applied_at IS NULL` inside the DELETE (a stale attempt must never strip a DONE job's claims) |
| `0bb0ff9` | Codex review r3: a failed post-enrichment refetch now fails the job before billing for EVERY plan (unlimited used to bill a count the file did not match); dispatcher buys lookups only for jobs in `done`; `FOR SHARE SKIP LOCKED` (locked rows deferred to next tick, no lock inversion vs purge cascade) |
| `3e4ce26` | Codex review r4: `sweep_stranded_dedup_claims` (status.py) + beat entry `sweep-stranded-dedup-claims` (scheduler.py, every 5 min): releases claims of failed/cancelled jobs that never billed. Prod dry-run: releases nothing today |

## 4. Active files (branch diff vs merge-base, 13 files)

- `src/workers/tasks.py` — `_release_claims_of_cancelled_job`; transfer call after reconcile;
  skip-trace enqueue after cap + `populate_existing` reload; refetch-failure = re-export failure.
- `src/workers/tasks_helpers/dedup.py` — `NO_ADDRESS_NOT_BILLED_SINCE`, `transfer_undelivered_claims`,
  `_transfer_one_claim`.
- `src/workers/tasks_helpers/enrich.py` — no longer enqueues skip trace.
- `src/workers/tasks_helpers/status.py` — `sweep_stranded_dedup_claims`.
- `src/workers/scheduler.py` — `sweep_dedup_claims` task + beat entry.
- `src/workers/skip_trace_dispatcher.py` — `_partition_still_deliverable` (buy/withdraw/left-queued),
  `_cancel_undeliverable` (pending `cancelled`, result back to `not_attempted`).
- `src/api/routes/jobs.py` — `superseded` excluded from header counts and duplicate_sources.
- `src/db/models.py` — `duplicate_reason` comment lists `superseded` (comment only, no migration).
- `scripts/diag_verify_repair_invariants.py` — known reason set.
- Tests: `tests/test_claim_transfer.py` (new, ~28 tests incl. thread race, sweep matrix),
  `tests/test_skip_trace_enqueue_after_delivery.py` (new), `tests/test_skip_trace_dispatcher_claim.py`,
  `tests/test_duplicate_provenance.py`.
- Plan file: `tasks/todo-results-categories.md` (untracked; update its Review section).

No migration. No OpenAPI change. No frontend change.

## 5. Key design decisions and why

- **D1 moves the claim, never deletes it.** Deleting was Codex round-1 FAIL: `mailing_recovery`
  can later fill the old row's address, the old run's LIVE `/download` would ship it, and the next
  run would deliver + bill it again. Transfer keeps exactly one holder; the old row is hidden forever.
- **Transfer eligibility** (all under the claim row lock): claim hash strong, proven from the
  parcel/address stored ON THE CLAIM (result rows are rewritten by enrichment); anchor row exists
  (NULL anchor = purged source = cannot disprove delivery, 44,866 such claims keep suppressing);
  anchor not actionable; anchor job `failed`/`cancelled`, or `done` with
  `billing_applied_at >= 2026-09-03 12:05:28 UTC` (merge of #191). Evidence for the cutoff: no job
  holding unactionable non-dup rows was billed between 2026-09-02 09:38 (old rule, billed all) and
  2026-09-04 09:32; first clearly new-rule job billed 2026-09-07 01:35. Only 22 identities qualify today.
- **D5 dispatcher** buys only for `done` jobs, non-dup, not over-quota; `SKIP LOCKED` defers rows
  being written. Accepted residual (Codex agreed P3): a watchdog re-run capping a row in the ms
  between the `submitting` commit and the POST.
- **D2/D3/D4 cannot ship piecemeal.** Codex round 2: D3 without D4 bills two events twice in one
  run but suppresses the second forever across runs. Round 1: a v2 claim key built from the scraped
  address / event tokens causes mass re-billing when an event id appears on a later scrape.

## 6. Failed attempts and dead ends (read before redoing)

- **Design gate took 3 Codex rounds, all FAIL until amended.** Round 1 killed: v2 key in the same
  `dedup_hash` column, claim DELETE for D1, tax keyed by `oldest_tax_year` (partial payments shift it;
  222 prod rows). Round 2 killed D3-alone and exposed the cutoff guess + weak-hash transfer.
- **Codex diff review r1-r4 each found real P1s** (listed in §3). Several were PRE-EXISTING gaps
  adjacent to the change (cancelled jobs never released claims; unlimited-plan refetch failure billed
  a mismatched file; traces bought for failed jobs). All fixed, each with a mutation-proven test.
- **Rejected Codex claims (verified wrong):** "enqueue is at-least-once" (row insert + status flip
  commit together); "provider idempotency missing" (dispatcher already commits a durable
  `submitting` claim and never resubmits unknown outcomes); "first export reachable" (`export_key`
  only written by the done-CAS).
- **Venv in the scratchpad broke on MAX_PATH** (`anthropic` submodule path >260 chars). Use
  `C:/Users/Windows/bl-rescat-venv` (Python 3.12, requirements + ruff 0.15.6).
- **Shared test Postgres crashed at 02:55** (`0xC0000142`, DLL init failure under memory
  pressure) during a mutation run; restarted with PowerShell `Start-Process pg_ctl`. Other sessions
  use it too; check `C:/Users/Windows/bl-testenv/pg.log` before assuming the DB is up.
- **`split -` fails on Windows bash** ("cannot determine file size"): write the list to a file first.
- **A mutation that removes `SKIP LOCKED` hangs the race test** instead of failing; don't run it.
- Explore agent wrongly said `dedup_hash` includes `date_recorded`; it only does in the weak branch.

## 7. How to run things

```bash
# isolated tests (own DB bridgeleads_rescat_test + redis db 14); add --migrate after pulling migrations
bash C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/191ca99a-cd92-4bc4-aaa6-d8c91f960c4c/scratchpad/pt.sh tests/test_claim_transfer.py
```
If that scratchpad is gone, recreate `pt.sh`: export `TEST_DATABASE_URL`/`_SYNC` =
`postgresql+asyncpg|psycopg2://bridgeleads:testpassword@127.0.0.1:5432/bridgeleads_rescat_test`,
`DATABASE_URL`=same, `REDIS_URL=redis://127.0.0.1:6379/14`, `SECRET_KEY`=any 32+ chars,
`STRIPE_SECRET_KEY=sk_test_fake`, `ENVIRONMENT=test`, `PATH=/c/Users/Windows/bl-rescat-venv/Scripts:$PATH`,
`cd` worktree, `python -m pytest -m "not integration" -q -p no:cacheprovider -o addopts="" "$@"`.
Needs Postgres :5432, proxy :6543, Redis :6379 (start Redis with PowerShell `Start-Process
C:\Users\Windows\bl-testenv\redis\redis-server.exe --port 6379`). NEVER bare `pytest` (prod `.env`).

- Full suite: list `tests/test_*.py` to a file, `split -n l/4`, run the 4 batches in the foreground.
- Lint: `C:/Users/Windows/bl-rescat-venv/Scripts/ruff.exe check src/ tests/`
- Codex diff gate (from the worktree, ~5-10 min): `codex review --base origin/main -c 'model_reasoning_effort="high"' -c 'mcp_servers={}' < /dev/null`
- Read-only prod queries: `railway run --service worker <python-with-psycopg2> script.py` from the
  MAIN checkout (Railway link is per-directory), `conn.set_session(readonly=True)`.

**Known test noise (not this branch):** 7 failures in `tests/test_plan_entitlement_audit.py`
(identical on clean main; local Stripe price ids not configured) and an intermittent
`test_auth.py::test_brute_force_lockout_after_five_failures` under load (passes alone and as its file).
Last full run before the sweep commit: 2,993 passed.

## 8. Where it stopped, and the next steps

**In flight at handoff:** full suite + Codex review round 5 on HEAD `3e4ce26`
(outputs `…/scratchpad/batch_a?.log` and `…/scratchpad/codex_review5_out.txt`).
Round 5 finished; its result is recorded below.

ROUND 5 (done): full suite on `3e4ce26` = 3,002 passed, only the 7 known entitlement failures. Codex found one [P1]: the transfer accepted a failed/cancelled anchor job even if it had BILLED (a job can bill and later be marked failed by a watchdog retry). Fixed in the handoff commit: failed/cancelled anchors now also require `billing_applied_at IS NULL`, with a mutation-proven test (`test_a_run_that_billed_before_it_was_marked_failed_keeps_its_claim`); `tests/test_claim_transfer.py` 31 passed. **Round 6 has NOT been run and the full suite has NOT been re-run after this last fix. Do both first.**

Next steps, in order:

1. If round 5 reports a P1/P2: verify it against the code (Codex has been right on every P1 so far,
   wrong on some P2 framing), fix with a mutation-proven test, full suite, re-run the gate. Repeat
   until the gate returns no P1/P2 (project rule: any Critical/High = NO-GO).
2. Security Master Review (§14 of `docs/security/SECURITY_PROMPT_PACK.md`) on the diff, twice clean.
3. Update `tasks/todo-results-categories.md` Review section; append `docs/BUILD_JOURNAL.md` entry.
4. **Ask the owner** before pushing. Then push `investigate/results-categories`, open a BE PR to `main`
   (repo `Abenezer1244/web-scrapper-automation`), watch CI. No migration, so deploy is safe; the new
   beat task starts on the worker/beat redeploy.
5. After deploy, verify in prod (read-only): `scripts/diag_verify_repair_invariants.py` still all 0;
   the sweep released 0 on its first ticks; the next Pierce/King runs of the 22 eligible identities
   show `superseded` anchors and promoted rows billed once.
6. **Phase B (D2+D3+D4), owner approval required first**, billing consequences: same property in two
   lists = two charges; a new instrument/case/TS number on a delivered property = a new charge; two
   cases on one parcel in one run = two charges. Codex-agreed staged program in
   `tasks/todo-results-categories.md` (identity spec + fixtures -> additive `delivered_events` ledger
   with privileged migration -> shadow mode -> D2 cutover for new properties -> D3+D4 together with
   unknown-token fallback to property suppression).
7. **Original UX request, not built yet:** Results header counts as clickable filter chips
   (`?category=new|already_delivered|combined` on `GET /jobs/{id}/results`, job+user scoped,
   paginated, search inside category), per-row provenance (reason, source run, source availability,
   "combined from N"), tooltips (no em dash), mobile cards at 320/375/390/430/768, CSV button
   labelled "Download new leads". FE repo `C:/Users/Windows/OneDrive - Seattle Colleges/Desktop/bridgeleads-web`
   (branch `master`; its working tree is on another agent's branch, use a worktree). Playwright MCP
   failed to connect in this session; use Playwright CLI/Chromium, never Claude in Chrome.
