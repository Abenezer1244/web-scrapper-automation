# HANDOFF — contact lookup 1b-1c-ii (the QUOTE endpoint), 2026-09-28 (end of session)

> **Status 2026-09-30:** the billing blocker below is FIXED, and **#386 is MERGED + LIVE**
> (`29172543`). ii has been rebased onto it and shipped as its own PR. See
> `tasks/todo-lookup-contacts.md`, "Amendments BUILT", for the record. The sections below
> describe the state on 2026-09-28.

Read this whole file first. Then read, in `tasks/todo-lookup-contacts.md` (search the headings):
1. **"### FINAL 1b-1c contract and build list"** and its **"Amendments from consult r3"** (R1-R6):
   the normative contract for the planner and the quote endpoint.
2. **"### main moved under ii: #374 + #378 ..."** through **"### Amendments BUILT (2026-09-28)"**:
   what changed mid-build, the Codex consult, the OWNER DECISIONS (T and Z), and what was built.
3. **"Carried into 1b-2"** bullets inside the FINAL list (confirm must re-check access + trial room
   under the user-row lock, refuse an unknown payload `v`, write only `quoted_ids`, etc.).

## The goal
Phase 1 of contact lookup: a customer buys skip-trace lookups (Tracerfy; operator pays per
credit: normal 1, advanced 2) for the leads on a results tab. **1b-1c** = the read path the
customer sees first: a pure planner shared with the future worker (**1b-1c-i, LIVE**) and
`POST /jobs/{job_id}/contact-lookups/quote` (**1b-1c-ii, built, not merged**). Next phases:
**1b-2** writers (confirm, worker claim, ledger, settlement), then **1c** frontend.

## Where things are
| Where | What |
|---|---|
| Worktree | `C:/Users/Windows/bl-wt-lookup` (NEVER the OneDrive checkout: its `.env` is PRODUCTION) |
| `main` | `fa658ccf` (#387 docs). 1b-1c-i is live since #384 `9833a7b0` |
| **PR #386** | branch **`feat/lookup-quote-rate-zone`**, head **`acdcadcf`** (pushed). The precursor: `lookup_quote` rate zone + the planner's trial `credit_cap`. 4 files. Codex r4 **GO** on the rebased diff. **CI BLOCKED** (see below) |
| **ii branch** | **`feat/lookup-1b1c-ii-quote`** (LOCAL, **UNPUSHED**), stacked on #386's commits, head = the commit carrying THIS file. 5 files + this handoff. Codex **GO**. Checked out in the worktree now |
| Test env | `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` (gives `$PY`; also `export DEBUG=false`). Local PG16 `bridgeleads_lookup1b_test` (rev 105), local Redis 5.0.14 db 13 |
| OpenAPI venv | **`C:/Users/Windows/bl-schema-venv`** (uv CPython 3.12.12 + `pip install -r requirements.txt`; fastapi 0.141.1, pydantic 2.13.4). The old `.venv-schema` in OneDrive is DEAD (base = removed Anaconda) |
| Prod checks | `C:/Users/Windows/bl-checks/`: `quiet.py` (merge gate), `tab_sizing.py`, `stripe_lookup_rates.py`, `pause_state.py`. Run from the OneDrive dir: `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python C:/Users/Windows/bl-checks/quiet.py` |
| Scratchpad (this session) | `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/dea35045-7705-4390-b863-da33b1cb6e1a/scratchpad/`: `codex_*.txt` prompts + `_out.txt`, `mutate_1b1c_i.py`, `mutate_1b1c_ii_v2.py` (18 endpoint mutations; args: `$PY lo hi`, run in 2 halves), `reg_*.txt` |

## 🛑 BLOCKER: GitHub Actions billing
#386's CI on `acdcadcf` failed in 3 s, every job skipped. Annotation: *"The job was not started
because recent account payments have failed or your spending limit needs to be increased."*
Only the OWNER can fix it (GitHub Settings -> Billing & plans). Its previous head `3a990e0e` was
GREEN (Test 21m47s), but `main` moved (#387, docs only), so the gate needs a new green run.
**Nothing can merge until billing is fixed.** Check first: `gh pr checks 386`; if still
3-second failures, re-run after billing: `gh run rerun <run-id>` or push an empty rebase.

## State: what is LIVE vs built
- LIVE: 1b-1a schema (101), all of 1b-1b (spend cap, pause hash `bridgeleads:skip_trace:pause:v1`,
  verified live in prod this session via `railway ssh`), **1b-1c-i #384** (`src/api/contact_lookup_planner.py`,
  `src/config/lookup_pricing.py`).
- Built, not merged: #386 and ii (below).

## Changes made this session (files)
**#384 (merged):** planner (`classify`, `plan_window`, `plan_tab_window`, `tab_status_counts`,
`count_remaining`), `lookup_pricing` (8c Pro/Business, 5c Agency, `USD`, `PRICING_VERSION
"2026-06"` = the live Stripe metered prices; `included_lookups_remaining` with the billed
window-roll rule), 63 tests incl. parity with the real `_enqueue_skip_trace_rows`, the
1b-1b-iii BUILD_JOURNAL entry.

**#386 (`feat/lookup-quote-rate-zone`):**
- `src/api/middleware/rate_limit.py`: zone `"lookup_quote": (10, 60)`, in `_FALLBACK_ZONES`.
- `tests/test_lookup_quote_rate_zone.py` (6 tests, real limiter + Redis).
- `src/api/contact_lookup_planner.py`: `CREDITS = {"normal": 1, "advanced": 2}` (pinned equal to
  the worker's `CREDITS_PER_ROW` by a test; restated because importing `src.workers` builds
  Celery), `Window.credit_cap / quoted_credits / over_credit_cap`, `stopped="credit_cap"`,
  `credit_cap` threaded through `plan_window` / `plan_tab_window`, `PLANNER_VERSION = 2`.
- `tests/test_contact_lookup_planner.py`: credit-cap tests + parity with the REAL
  `claim_skip_trace_rows` in trial mode (kept == quoted).

**ii (`feat/lookup-1b1c-ii-quote`):**
- `src/api/routes/jobs.py`: `quote_contact_lookups` + helpers `_lookup_redis` (module-level SYNC
  client, 0.5 s socket timeouts), `_bounded` (`run_in_threadpool` + `wait_for` 1 s), `_quote_key`
  (`bridgeleads:contact_lookup:quote:v2:<user>:<job>:<category>`, one live quote per tab),
  `_lookups_unavailable` (503). Gate order: `wait_for(rate_limit(zone="lookup_quote"))` ->
  job by `(id, user_id)` 404 -> not `done` 409 `run_not_finished` -> plan not in
  `SKIP_TRACE_ADDON_PLANS` = structured 402 -> `run_eligibility` frozen/ended = run-refusal 402
  (`over_limit` does NOT refuse) -> kill switch/token 503 -> `paid_lookup_access()` (lazy import
  from `src.workers.skip_trace_claim`; trial -> `credit_cap = SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE`)
  -> PING (503 before any DB work) -> status counts + window + `count_remaining` -> pause via
  `read_pause_state` (failure = UNKNOWN) -> `SET ... EX 600` payload `v: 2` (503 if it fails).
- `src/api/schemas.py`: `ContactLookupQuoteRequest` (`extra: forbid`), `ContactLookupQuote`
  (incl. `access`, `trial_credit_allowance`, `over_trial_allowance`, `truncated_reason` cap |
  credit_cap | scan_limit), `ContactLookupExcluded`, `ContactLookupPause`,
  `ContactLookupUnavailableResponse`.
- `schema/openapi.json`: regenerated in `bl-schema-venv`: +352, 0 deletions vs main, `--check` OK.
- `tests/test_contact_lookup_quote.py` (26 tests; Redis failure proven with a closed port, a
  blackholed socket, and a private `redis-server --maxmemory 1 --maxmemory-policy noeviction`
  that serves PING/reads and refuses writes).
- `tasks/todo-lookup-contacts.md`: all 1b-1c sections, decisions, build records.

## Verification done
- #386: 6 zone tests, 3/3 zone mutations, 6/6 credit-cap mutations; rate-limit regression 39 passed.
- ii: 18/18 endpoint mutations; regression on the whole stack **923+ passed, 0 failed**
  (every test file that calls `/jobs`, the rate-limit set, lookup/claim/billing/entitlement/beat).
- Codex: #386 r1 GO, r2 NO-GO (P2 window should carry its cap; P3 docstring) fixed, r3 GO, r4 GO
  after rebase. ii: diff review vs the precursor `VERDICT: GO`, no findings.

## Decisions made (owner, this session)
- O1 token gate on the api service: api and worker already carry both vars (verified booleans).
- O4 the quote covers the WHOLE tab; view filters never apply (request forbids extra fields).
- Journal entry for 1b-1b-iii went into #384.
- **T** trials: cap at the FULL lifetime allowance (upper bound). Rejected: an exact API-readable
  counter (a money-path migration) and refusing trials.
- **Z** rate zone: its own `lookup_quote` zone, shipped as the precursor PR #386.

## Failed attempts / traps hit (don't repeat)
- Codex consult took 6 rounds to PLAN: GO; then main moved (#374/#378) and changed binding facts
  (who may buy; `export` became the shared CSV-download bucket) AFTER ii was built. Always
  re-check main's changes after a rebase against the plan's facts, not only for conflicts.
- A trial parity test seeded rows in ONE transaction: shared `created_at` -> window ordered by
  random UUID -> credit total varied; it passed once by luck. Give each row its own `created_at`.
- Keyset `tuple_(created_at, id) > tuple_(v1, v2)` bound the uuid as VARCHAR -> `uuid > varchar`
  error. Bind with `literal(v, Column.type)`.
- `from src.api.middleware import rate_limit` returns the FUNCTION (package re-export); use
  `importlib.import_module("src.api.middleware.rate_limit")`.
- A Bash call past 600 s moves to background and its piped output is LOST: write pytest output
  to a file, keep chunks < ~8 min (the 3 rate-limit files alone take ~7.7 min).
- A timed-out mutation run can orphan pytest on Windows; ANOTHER session (`67fa093f`) also runs
  pytest (its own DB `bridgeleads_eligibility_test`, Redis db 7, stuck in `test_auction_coverage`).
  Before killing anything, match YOUR exact test file in the command line. I nearly killed theirs.
- A mutation that makes the request hang (the unbounded limiter) is reported CAUGHT by the
  runner's 150 s timeout; run the 18 in two halves (`0 9`, `8 18`).
- Local Redis is 5.0 (no ACLs); `CLIENT PAUSE` is server-wide and would stall the other session's
  suite: use the private maxmemory-1 redis-server for write-failure tests.
- A Bash `ls .env` from the worktree was denied (the rule resolves to the OneDrive prod `.env`).
  Don't touch `.env` files.
- Transient "auto mode classifier gave no verdict" errors: none of the calls apply; retry once,
  stop before 10.

## Next steps (in order)
1. `gh pr checks 386`. If billing is fixed: re-run CI on `acdcadcf` (or fetch + rebase onto
   current main -> Codex three-dot re-check -> `git push --force-with-lease` -> CI).
2. Merge gate for #386 (standing rule, memory `feedback_standing_merge_ok_lookup_queue`):
   `quiet.py` all zeros, CI green on the EXACT head, Codex GO, main unchanged since the rebase:
   `gh pr merge 386 --merge --match-head-commit <full sha>`. Never `--admin`. Then verify
   api/worker/beat on the merge commit (`railway deployment list --service X --json`,
   `meta.commitHash`) and the boot logs.
3. ii: `git fetch; git rebase origin/main` (the #386 commits drop out), Codex three-dot re-check
   vs `origin/main`, re-run `tests/test_contact_lookup_quote.py` + planner tests, confirm the
   OpenAPI `--check` in `bl-schema-venv`, push, open the PR (5 files + this handoff, the #376
   precedent), CI, merge gate, deploy verify.
4. After ii: BUILD_JOURNAL entry for this session (ask the owner where it lands), then plan
   1b-2 (confirm + worker claim + ledger) with a Codex consult before code.

## Working agreements (binding)
- Codex in the loop on EVERY step, FOREGROUND: `timeout 590 codex exec "$(cat prompt.txt)" -c
  'model_reasoning_effort="high"' -c 'mcp_servers={}' --skip-git-repo-check < /dev/null > out.txt`.
  Prompts start with "Do NOT load any skill, do NOT run /graphify or any preamble" and "Read-only
  ... Do NOT run the test suite". Review the THREE-DOT diff after fetch + rebase, every time.
- Codex wins where docs are silent (unless it breaks money safety); record reconciliations in the plan.
- 5-file rule per PR (plan counts); phase gate: owner approval before the next phase.
- Merge IS deploy. No mocks (real PG/Redis; Tracerfy only via the `http://` rejection trick).
- Files are CRLF in the index-LF repo: use the Edit tool or byte-level Python with the file's
  own newline; never `git stash` (shared across worktrees): commit WIP instead.
