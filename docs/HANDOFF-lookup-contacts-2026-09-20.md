# HANDOFF: contact lookup action — Phase 1a SHIPPED, 1b/1c next (2026-09-20)

Read this top to bottom before acting. Everything here is verified unless marked otherwise.
Previous handoff (now closed): `docs/HANDOFF-lookup-contacts-2026-09-19.md`.

## 1. The goal

Contacts (phone / email) could only be bought as a side effect of a scrape. To trace 38
already-delivered leads the owner had to build a SECOND scraper with a custom date range and
re-scrape Pierce. The agreed direction (owner approved) is two things:

1. **"Look up contacts" as a first-class action** on leads that already exist: pick a run or a
   tab, see a cost quote, confirm, and the lookups are bought. No re-scrape, no second scraper.
   **This is Phase 1b + 1c and is NOT built yet.**
2. **Fix answer reuse** so it can never hand one owner's contacts to a different owner at the
   same address. **This was Phase 1a. It is DONE, merged, and cut over in production.**

Longer term (Codex design review, agreed but not started): a lookup ledger (Phase 2) and
lead-level contacts so a contact belongs to the lead rather than to one run's row (Phase 3).

## 2. WHERE YOU ARE

**Phase 1a is finished and live.** The cutover completed 2026-09-20 ~10:40 UTC. Skip trace is
running again. Nothing is half-done, nothing is waiting on a deploy.

The next session's job is **Phase 1b** (the action, backend). Nothing blocks it.

## 3. What is DONE and LIVE in production (do not redo)

| What | Where | State |
|---|---|---|
| **Phase 1a: reuse keyed on the OWNER** | BE **#349** `e30e02e` (10 commits) | MERGED + CUT OVER |
| Migration **098** `results.skip_trace_subject_hash` | in #349 | LIVE, index VALID |
| Stripe test-skip hygiene | BE **#350** `4b709a3` | MERGED |
| Frontend security upgrade | FE **#159** `10d65d7` | MERGED + DEPLOYED |

**Phase 1a in one line:** skip-trace reuse is keyed on the SUBJECT a lookup was bought for
(account + address + trace type + the exact names sent to the provider), not on the address
alone. The old address-only key meant one address had one answer inside the 90-day window, so
an heir's lead inherited the deceased owner's phone. Probate made that the ordinary case.

**Verified in production BY THE OBJECTS** (never `alembic_version` — the app role reads it as
empty, so it would "confirm" success on a database where nothing happened):

```
COLUMN  results.skip_trace_subject_hash  varchar(64)  nullable=YES
INDEX   ix_results_skip_trace_subject_hash            valid=True
ROWS    0 of 171,657 carrying a subject hash    (correct: 098 does NOT backfill)
LEGACY address_cache_key warnings   api: 0  worker: 0  beat: 0
SKIP_TRACE_ENABLED  api/worker/beat = true   (read back after the cutover)
```

**Expect the first lookups after the cutover to be PAID, not reused.** Every legacy cache row
is inert and no row carries a subject hash yet; reuse rebuilds as answers land under the v2
key. Bounded by the 90-day TTL, self-healing. Elevated spend for a while is the designed cost,
not a fault.

## 4. What the code now does (the parts a newcomer will get wrong)

Two keys, doing two different jobs. Collapsing them back into one is the mistake the whole
phase exists to prevent:

- **`lookup_subject_key`** (`src/scrapers/enrichment/skip_trace.py`) governs CACHE and REUSE.
  Account + address + city + state + trace_type + first + last, JSON-array serialized (not
  `|`-joined, so no value can fake a field boundary), NFKC + case-fold + whitespace collapse,
  **punctuation preserved**, and **truncated to the pending-row column widths BEFORE hashing**
  so the enqueue read and the ingest write hash identical bytes.
- **`submission_collision_key`** governs what may go out in ONE provider batch. Deliberately
  **GLOBAL, no `user_id`**, and **within-batch only**.

Wrappers: `pending_row_subject_key(row)` (dispatcher, ingest — the row records what was
actually sent) and `payload_subject_key(user_id, payload)` (enqueue, before the row exists).

**Five reuse paths all switched together** — leaving any on the old key keeps serving the wrong
owner: (1) enqueue cache read, (2) dispatcher known-answer sweep, (3) dispatcher in-flight
hold, (4) ingest cache write, (5) both `dedup_hash` reuse passes.

**`address_cache_key` is LEGACY.** It logs a WARNING on every call. Nothing in the runtime path
may call it. Two deliberate callers remain: `scripts/verify_tracerfy_provenance.py` (read-only
forensics, falls back only for pre-098 rows) and nothing else — `legacy_cache_locality` was
DELETED and `scripts/sprint4_enqueue_existing.py` is now a stub with its body removed.

**Owner decisions, all answered 2026-09-19:**
- **D1** advanced (address-only) traces reuse per address within an account; owner isolation
  explicitly does NOT apply, because no name was ever sent. Normal never reuses advanced or
  vice versa (`trace_type` is in the key).
- **D2** the quote shows customer-billed ROWS; the credits-vs-rows gap is logged as its own
  follow-up, not folded in.
- **D3** the global daily cap gets surfaced end to end (that work is in 1b/1c).

## 5. Active files

**Plan (read in full before coding 1b):** `tasks/todo-lookup-contacts.md` — Phase 1b/1c are
specified there in detail and the spec survived 14 Codex rounds. Its `## Review` section
records what shipped.

**Runbook:** `docs/RUNBOOK-lookup-subject-key-cutover.md` — already executed; keep it for the
legacy-cache-row deletion step, which is still outstanding (§8 below).

**Code changed by 1a (all on `main` now):**
```
src/scrapers/enrichment/skip_trace.py    the key helpers; address_cache_key now legacy+logging
src/workers/skip_trace_dispatcher.py     _answer_key, _submission_key, sweep, in-flight hold
src/workers/tracerfy_ingest.py           _attribution_is_safe, cache write, Result settle
src/workers/tasks_helpers/enrich.py      enqueue cache read; _reuse_enrichment_for_duplicates
src/db/models.py                         Result.skip_trace_subject_hash
alembic/versions/098_results_skip_trace_subject_hash.py
tests/test_lookup_subject_key.py         24 unit tests (the D1 matrix, normalization)
tests/test_lookup_subject_reuse.py       14 integration tests, each named for its finding
scripts/{backfill_skip_trace_jobs,sprint4_enqueue_existing,verify_tracerfy_provenance}.py
```

**Worktrees:**
| Path | Branch | State |
|---|---|---|
| `C:/Users/Windows/bl-wt-lookup` | `feat/lookup-contacts-action` | clean, **merged into main** — safe to reuse for 1b after `git rebase origin/main` |
| `C:/Users/Windows/bl-wt-lookup-fe` | `feat/lookup-contacts-action` | clean, **1 commit behind FE master**, nothing built. For 1c. Rebase onto `origin/master` (`10d65d7`) first |
| `C:/Users/Windows/bl-wt-stripeskip` | `chore/stripe-price-skip-guard` | merged; disposable, `git worktree remove` it |
| `C:/Users/Windows/bl-wt-deps-fe` | `chore/security-deps-2026-09-18` | merged; disposable |

## 6. FAILED ATTEMPTS AND DEAD ENDS — do not repeat these

- **The plan passed 13 Codex rounds and was still wrong twice.** Round 14 (consulting Codex
  BEFORE writing code) found two P1s, because rounds 1-13 only ever examined the key design and
  never asked what a subject-keyed batch does on the way BACK. **Consult Codex before code, not
  only after.**
  - **14-A** a subject key alone DOUBLE-CHARGES and answers nobody: provider attribution is
    address-only and refuses a whole group when two answers return for one address.
  - **14-B** the `dedup_hash` passes CANNOT be made owner-safe by recomputing the subject,
    because `party_name` is rewritten by owner recovery AFTER a lookup settles; the recomputed
    source then reads as the CURRENT owner while its stored phone belongs to the PREVIOUS one,
    so the check passes and copies the exact leak it was added to stop. That is why 098 exists.
- **I over-applied the submission key across batches.** Attribution is scoped to ONE
  `tracerfy_queue_id`, so cross-batch holding buys nothing and puts one tenant behind another
  tenant's lookup. `test_another_accounts_lookup_never_holds_or_answers_mine` caught it.
  **Within-batch only.**
- **Codex failed the diff (NO-GO) on a P1 my own fix made worse.** `_attribution_is_safe`
  returned "safe" as soon as only ONE row waited, never checking answer multiplicity — and the
  submission key turns that early return from a corner into the NORMAL path. An existing test
  had the bug pinned as correct under the name "single waiting row is always safe".
- **A test of mine passed clean AND while mutated**, meaning it exercised the wrong pass
  entirely (the first reuse pass, not `later_sql`). **Mutation-test anything that matters.**
  Another test of mine was genuinely vacuous and was deleted rather than dressed up.
- **`indisvalid=false` on a fresh CONCURRENTLY index means BUILDING *or* DEAD.** My first
  post-deploy check reported "failed build, do not re-enable". It was 60s mid-build on a
  171,657-row table and was valid a minute later. Check `pg_stat_progress_create_index` before
  declaring failure (`<scratchpad>/prod_index_state.py`).
- **Nine local test failures in `test_plan_entitlement_audit` / `test_promo_access` were NOT a
  regression** — missing local `STRIPE_PRICE_*`. Cost an hour to prove via a baseline run at
  `origin/main`. **Fixed in #350**, so a local full-suite run is now readable.
- **`codex exec` with a ~60KB prompt dies on Windows** with "Argument list too long". Pipe via
  stdin: `codex exec - ... < prompt.txt`.
- **The local full suite was killed twice by the harness for host memory** (~75%). CI is the
  reliable signal. Do not fight it; open the PR and read CI.
- Do not claim a migration is applied by reading `alembic_version` in prod: the app role sees it
  EMPTY. Prove it by the objects.

## 7. How to run things (Windows box)

- Python: `C:/Users/Windows/bl-rescat-venv/Scripts/python` (Anaconda is gone).
- **NEVER bare `pytest`** (the repo `.env` is PRODUCTION). Use an isolated DB:
  `TEST_DATABASE_URL=postgresql+asyncpg://bridgeleads:testpassword@127.0.0.1:5432/<name>_test`
  plus `TEST_DATABASE_URL_SYNC` (psycopg2, same DB), `DATABASE_URL(_SYNC)` = those,
  `REDIS_URL=redis://127.0.0.1:6379/<n>`, `SECRET_KEY=<32+>`, `STRIPE_SECRET_KEY=sk_test_fake`,
  `ENVIRONMENT=test`, then `alembic upgrade head`, then
  `python -m pytest -m "not integration" -q -p no:cacheprovider -o addopts="" <files>`.
  A ready-made env script: `<scratchpad>/env_lookup.sh` (edit the DB name + redis db number).
  The portable PG/Redis live in `C:/Users/Windows/bl-testenv/`.
- Codex (Windows): `codex exec - -C <repo> -s read-only -c 'model_reasoning_effort="high"'
  -c 'mcp_servers={}' -c 'web_search="cached"' < prompt.txt`. `-s read-only` matters: `codex
  exec` is a coding agent and will edit your worktree otherwise.
- Prod read-only: `railway run <python> <script>` from the OneDrive checkout (the linked
  project). Read-only transactions, counts only, no PII in output.

## 8. NEXT STEP (exactly where to resume)

1. **Start Phase 1b** (the action, backend). Read `tasks/todo-lookup-contacts.md` in full
   first — it is specified there and every piece of its concurrency design exists because a
   Codex round found a way to double-charge a customer or strand a row. **Do not simplify it.**
   Its migration is **099** (renumbered; 098 is taken).
   - Reuse `C:/Users/Windows/bl-wt-lookup` after `git rebase origin/main`.
   - **Consult Codex on the 1b plan BEFORE writing code.** That step is what caught both P1s.
2. Phase 1c (frontend) after 1b. Rebase `bl-wt-lookup-fe` onto FE `origin/master` (`10d65d7`).
3. Run the security Master Review (§14 of the pack) after the build, twice, until two
   consecutive clean passes. 1b adds API routes, so §4 (permission checks), §5 (data leaks) and
   §17 (business-rule invariants) matter far more than they did for 1a.

### Outstanding, smaller

- **Legacy cache rows are inert but still stored.** Deleting them is PII hygiene ONLY, never the
  correctness mechanism, and it is a separate ops step with the deleted count recorded. See the
  runbook's last section.
- **Pre-existing, found 2026-09-20, NOT from this work:** beat logs
  `WARNING billing: stripe_price_id for plan 'pro' is 'prod_UANuoAMKafnDJ5'`. A Stripe **product**
  id is sitting in a **price** slot (price ids start with `price_`). This is exactly what
  `tests/test_plan_entitlement_audit.py:380` guards against, and it is plausibly why the local
  suite finds no usable prices. Unverified whether checkout is affected in practice. Worth a look.
- **The D2 follow-up:** advanced traces cost 2 provider credits but bill the customer 1 row. The
  gap is to be recorded per action in 1b and priced separately later.
- **Deferred and logged:** the cross-tenant collision measured below is closed going forward by
  1a's global submission key, but no backfill/repair was done for the rows it already affected.

### Production measurement worth keeping (read-only, counts only, 2026-09-20)

```
same-address groups in one batch: 79       rows in them: 162
  CROSS-TENANT groups           : 70       <- the dominant shape, pre-existing
  groups entirely 'unmatched'   :  2       <- charged, nobody answered
```
That is 14-A having already happened twice at small scale, and the reason
`submission_collision_key` takes no `user_id`. Script: `<scratchpad>/prod_collision_count.py`.

## 9. Session artifacts

Scratchpad: `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/1c298e6f-c1e6-459f-b9fd-51c9ea2e11bc/scratchpad/`
- `codex_1a_consult.txt` / `_out.txt` — round 14, the two P1s that reopened the plan.
- `codex_1a_review.txt` / `codex_1a_rev_out.txt` — the diff review that returned NO-GO.
- `codex_1a_sec.txt` / `_out.txt`, `codex_1a_sec2.txt` / `_out.txt` — the two clean security passes.
- `prod_collision_count.py`, `prod_quiesce_check.py`, `prod_await_098.py`, `prod_index_state.py`
  — all read-only production checks, reusable for 1b.
- `env_lookup.sh` — the isolated pytest rig env.
