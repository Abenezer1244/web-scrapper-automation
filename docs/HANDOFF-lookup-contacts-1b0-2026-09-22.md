# HANDOFF: contact lookup action — Phase 1b-0 SHIPPED to PR, 1b-1 next (2026-09-22)

Read top to bottom before acting. Everything is verified unless marked otherwise.
Previous handoff (still useful for 1a context): `docs/HANDOFF-lookup-contacts-2026-09-20.md`.

## 1. The goal

Contacts (phone / email) can only be bought as a side effect of a scrape. To trace 38
already-delivered leads the owner had to build a SECOND scraper with a custom date range and
re-scrape Pierce. The agreed direction (owner approved) is:

1. **"Look up contacts" as a first-class action** on leads that already exist: pick a run or a
   tab, see a cost quote, confirm, lookups are bought. No re-scrape.
2. **Fix answer reuse** so it can never hand one owner's contacts to a different owner at the
   same address. **This was Phase 1a. DONE, merged, cut over in production 2026-09-20.**

## 2. WHERE YOU ARE

**Phase 1b-0 is code-complete and on PR #354.** It is HARDENING ONLY: no API route, no action
tables, no frontend. It makes the existing paid path safe for the second writer that 1b-2 adds.

**NOT MERGED. No Tracerfy credits spent. Production untouched.**

The owner has THREE open decisions (§8). Do not merge without them.

| | |
|---|---|
| Branch | `feat/lookup-contacts-action` in `C:/Users/Windows/bl-wt-lookup` |
| PR | **#354**, 20 commits ahead of `origin/main`, rebased onto it |
| HEAD | `9d668b2 fix(migration): renumber to 100, behind the 099 that landed on main` |
| Migration | **100** (see §6 — it was 099 and collided) |
| Tests | 313 green on a from-scratch isolated DB; ruff clean |
| Security | Master Review §14 run **12 times**; final two passes clean, GO |
| Codex diff review | PASS (after six NO-GOs) |
| CI | **GREEN** on `9d668b2`: Test pass 14m20s, Dependency Audit pass. Re-check: `gh pr checks 354` |

## 3. What 1b-0 actually does

Migration **100** adds a partial UNIQUE index on `pending_skip_trace_rows (result_id)` WHERE
status IN ('queued','submitting','submitted'), so one lead cannot hold two active skip-trace
claims. Two active claims = the customer charged twice for one lead against a per-lookup vendor.

`src/workers/skip_trace_claim.py` is NEW and is now the single way a lead enters the queue:
- ONE `INSERT ... SELECT ... FROM (VALUES ...) JOIN results JOIN jobs ... ON CONFLICT
  (result_id) WHERE status IN (...) DO NOTHING RETURNING result_id`
- the join re-checks, inside the statement: the row EXISTS, belongs to the claiming TENANT,
  belongs to that JOB, and is still `not_attempted`
- then advances `results` for exactly what it won, and **withdraws (DELETEs) any row it
  inserted whose lead it did not win**, inside the same uncommitted transaction
- **fails closed**: raises `ClaimUnenforcedError` if migration 100's index is missing/wrong
- **asserts the job lock is held** (`ClaimLockNotHeldError`), one job per claim
- chunked at `_INSERT_CHUNK_ROWS = 1000` (statement only, one transaction)
- **does NOT commit** — the caller owns the transaction, which is what lets 1b-2 write its
  dispositions and audit rows in the same one

Callers: `_enqueue_skip_trace_rows` and `scripts/backfill_skip_trace_jobs.py`, both under
`lock_job_for_claim()`.

`_cancel_undeliverable_queued` no longer commits; the dispatcher tick owns that transaction.

The dispatcher gained a **spend guard**: once per tick it checks whether the index is enforced;
if not, it finds leads holding >1 active row, excludes exactly those from submission, and alerts.

## 4. Active files

```
NEW  src/workers/skip_trace_claim.py              the claim, the lock, the enforcement check
NEW  alembic/versions/100_pending_skip_trace_unique_active.py
NEW  tests/test_skip_trace_claim.py               23 tests
     src/workers/tasks_helpers/enrich.py          _enqueue_skip_trace_rows refactored onto the claim
     src/workers/skip_trace_dispatcher.py         sweep de-committed; duplicate spend guard
     scripts/backfill_skip_trace_jobs.py          routed through the claim
     tests/test_skip_trace_over_quota.py          sweep ownership + duplicate guard tests
     tasks/todo-lookup-contacts.md                THE PLAN. Round 15 findings + 1b-0 review
```

**Read `tasks/todo-lookup-contacts.md` in full before 1b-1.** Phase 1b-1 and 1b-2 are specified
there, with the Codex round-15 findings (15-1 … 15-17) that each exist because a round found a
way to double-charge a customer or strand a row. **Do not simplify past them.**

## 5. How to run things

- Python: `C:/Users/Windows/bl-rescat-venv/Scripts/python`
- **NEVER bare `pytest`** (repo `.env` is PRODUCTION). Use:
  `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` then
  `"$PY" -m pytest -m "not integration" -q -p no:cacheprovider -o addopts="" <files>`
  DB `bridgeleads_lookup1b_test`, Redis db 13. Portable PG/Redis in `C:/Users/Windows/bl-testenv/`
  (there is **no psql**; create DBs with psycopg2 via `template1`).
- Codex (Windows): `codex exec - -C <repo> -s read-only -c 'model_reasoning_effort="high"'
  -c 'mcp_servers={}' -c 'web_search="cached"' -c 'experimental_use_skills=false' < prompt.txt`
  **`experimental_use_skills=false` matters** — without it Codex loaded an unrelated `/audit`
  UI-accessibility skill mid-review and followed it for a stretch.
- Prod read-only: `railway run --service worker <python> <script>` from the OneDrive checkout.

## 6. FAILED ATTEMPTS AND LANDMINES — do not repeat these

**THE BIG ONE: a clean security pass is evidence about the QUESTIONS ASKED, not about the code.**
Pass 6 came back completely clean. Passes 7, 8 and 10 then each found something the earlier ones
structurally could not, because I changed the angle:
- **Pass 7 (hostile input / DoS):** Postgres caps a statement at **65,535 bind parameters**.
  The claim spent 15 per row, so it broke at **4,368 leads and enqueued nothing**. Production
  holds 100,548 claimable leads. Six passes of correctness review walked past it.
- **Pass 8:** the fix for that introduced the next bug — the chunked insert rebuilds bind
  parameters per chunk while the withdrawal still indexed them by whole-batch position, so it
  would DELETE THE WRONG pending row.
- **Pass 10 (deploy day):** the dispatcher, the thing that actually SPENDS, never checked the
  invariant. Reviewing the code could not find this; the gap was not in the code under review.

**CI caught a migration-number collision that nothing local could.** A `099_job_progress_
observations` landed on `main` mid-review. My isolated DB already had my 099 applied and was
happy; only a from-scratch build shows two Alembic heads. **Always `gh pr checks` before
believing local green.** Renumbered to 100.

**Three of my own tests were worthless until mutation-tested.** One passed both clean AND
mutated (it settled the victim lead before the claim, so nothing was ever stranded and the code
path under test never ran). **Mutation-test anything that matters**: break the fix, confirm the
test fails, revert.

**Two `NameError`s on every enqueue, both from moving code above its local import.** Caught by
ruff, not by tests, because the enqueue's gates return before those lines in most suites.

**A reviewer's suggested fix can be worse than the bug.** Codex's remedy for the unguarded
cache-hit write was a raw-SQL UPDATE. `phone`/`email`/`phones`/`emails` are `EncryptedString` /
`EncryptedJSON` — that writes **plaintext PII**. Always check a suggested fix against the schema.

**`_publish_log` COMMITS** ("Commit BEFORE publish (load-bearing)"). Any transaction-scoped
advisory lock is released by it. That is why the charged-unanswered rule runs twice rather than
moving under the lock.

**A second writer hides in `scripts/`.** Phase 1a had already audited the ops scripts and fixed
`backfill_skip_trace_jobs.py`; it was wrong again here for a different invariant because it
built `PendingSkipTraceRow` itself. A script that writes to a queue is part of that queue's
concurrency design.

**Everything deciding whether a lead may be BOUGHT must be re-evaluated AFTER the job lock.**
Three separate passes found a filter on the wrong side of it: the SQL predicates, the two Python
filters (`street_is_placeholder`, `_is_settled_complaint`), and the charged-unanswered rule.

**Harness:** background shells get reaped under host memory pressure (it killed a Codex run and
a login poller). Waiting on a log string can match text echoed from a file Codex read — wait on
**process exit**, not on a phrase.

## 7. Production facts (read-only, verified 2026-09-20)

```
duplicate ACTIVE pending rows per result_id : 0     -> migration 100 will not abort
pending/results drift outside allowed matrix: 0     -> the quote's classification is sound
results 171,657   not_attempted 170,350 | hit 1,024 | miss 272 | errored 11
not_attempted WITH a property address       : 100,548
   ... of those already-delivered duplicates:  61,442   <- the case that drove this feature
```

## 8. OPEN DECISIONS FOR THE OWNER — ask, do not assume

1. **Merge PR #354?** Not merged. Needs CI green first.
2. **Deploy style.** The dispatcher guard makes an UNQUIESCED deploy safe (100 will not abort:
   zero duplicates). A quiesced deploy — the 1a pattern: kill switch off, workers to zero,
   migrate, restart — avoids the mixed-version window entirely. Owner's call.
3. **Per-account daily cap, currently deferred.** `SKIP_TRACE_DAILY_ROW_CAP` is **1000/day
   GLOBAL across all tenants**. With 100,548 addressable leads, ONE customer's 2000-lead action
   consumes two full days of dispatch capacity for every tenant. This probably stops being
   safely deferrable before 1c ships.

## 9. NEXT STEP (exactly where to resume)

1. **`gh pr checks 354`** — was GREEN at handoff (Test pass 14m20s). If it has gone red,
   `main` has moved again; read `gh run view <run-id> --log-failed`. A migration-number
   collision is the likeliest cause, and it is invisible locally (see §6).
2. **Run ONE Codex pass on the POST-REBASE diff.** The 12 security passes reviewed the diff
   BEFORE the rebase onto `origin/main`. The upstream change (`job_progress_observations`)
   touches a different subsystem and interaction is not expected, but that has not been
   verified. Short prompt, `-s read-only`, skills disabled.
3. **Ask the owner the §8 questions.** Do not merge without an answer.
4. **Then Phase 1b-1** (read path, touches no money), per `tasks/todo-lookup-contacts.md`:
   `results.last_trace_outcome` (finding 15-3, migration **101**), the three action tables with
   GRANTS + RLS POLICIES (15-8 — `RLS_ENFORCE` is true in prod and the API is NOBYPASSRLS, so
   without them `GET /contact-lookups/{id}` 500s on deploy day), `results` UNIQUE (id, user_id)
   built CONCURRENTLY (15-9), the disposition vocabulary + transition matrix (15-4),
   `plan_contact_lookup`, and the quote endpoint.
5. **1b-2 last** (confirm + worker claim + ledger + reconciler).

### Carried into 1b-2, do not lose

- The action worker **MUST** call `lock_job_for_claim()` before its own cache-and-claim pass.
  The cache-hit write is an ORM write (encrypted columns, cannot be raw SQL) and cannot see
  another writer's uncommitted pending row. `claim_skip_trace_rows` ASSERTS the lock, so
  forgetting raises rather than races.
- It inherits fail-closed enforcement and one-job-per-claim automatically.
- Codex's standing suggestion, NOT built: a shared claim-context API that takes the lock, does
  the cache and claim work, and requires the action/audit writes before commit, so "caller owns
  the transaction" stops being convention.

### Deploy notes

- **Forward safe. NOT backward safe.** Rolling back past this release with 100 applied is unsafe
  while traffic flows: the previous release's enqueue is not conflict-aware and its commit
  handler discards whole job batches silently. Downgrade 100 with it, or stop the worker first.
- Migration 100 aborts only if duplicate active rows exist. If it ever does, grade duplicates by
  **submission evidence**, never by age: `tracerfy_queue_id` = vendor accepted and charged;
  `status='submitting'` with no queue id = UNKNOWN outcome; `submitted_at` alone = local attempt
  only (the dispatcher stamps it BEFORE contacting Tracerfy). Quarantine any group holding an
  unknown-outcome row.

## 10. Other open items (unchanged from the previous handoff)

- **PR #351** (the 2026-09-20 handoff doc) — status unknown, check.
- Deleting the now-inert legacy skip-trace cache rows: PII hygiene only, separate ops step.
- **The D2 gap:** an advanced trace costs 2 provider credits but bills 1 row. To be recorded per
  action in 1b-2 and priced separately later.
- **PRE-EXISTING, not from this work:** beat logs a Stripe **product** id sitting in a **price**
  slot for plans 'pro' and 'business' (`prod_...`, prices start `price_`). Unverified whether
  checkout is affected. `tests/test_plan_entitlement_audit.py:380` guards this class.

## 11. Session artifacts

Scratchpad: `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/6eb81967-0c93-4b2d-8b98-35d4798e5d5b/scratchpad/`
- `codex_1b_consult.txt` / `_out.txt` — the pre-code plan consult, nine P1s
- `codex_1b0_review{,2..7}.txt` / `_out*.txt` — the six NO-GO diff reviews and the PASS
- `codex_1b0_sec{1..12}.txt` / `_out.txt` — the twelve Master Security Review passes
- `prod_1b_precheck.py` — the read-only production pre-check (§7 numbers)
