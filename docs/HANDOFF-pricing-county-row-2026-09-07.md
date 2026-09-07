# HANDOFF — branch `fix/pricing-county-row` (PR #235)

**Written:** 2026-09-07 · **Branch:** `fix/pricing-county-row` · **PR:** #235 (OPEN, CI green, MERGEABLE/CLEAN)
**Read this first. It is the complete state. Do not re-derive it from git log.**

---

## 1. THE GOAL

Close out the last open item from a session that shipped the transactional-email overhaul
(PR #233, already merged + deployed). Two things remain:

1. **PR #235** — the `/billing/pricing` comparison table advertises county counts the API
   refuses with HTTP 402. Fix is written and green. **BLOCKED ON A PRODUCT DECISION (see §6).**
2. **A Codex review gate** that could not be run (see §5). Optional, not blocking.

---

## 2. CURRENT STATE

### Branch contents (2 commits off `origin/main`, 0 behind at time of writing)

```
38601a7 docs(entitlements): the TOCTOU note claimed work that had already landed
575c6e0 fix(billing): /pricing advertised county counts the API answers 402 for
```

Working tree is CLEAN. Both commits pushed. PR #235 CI: Test pass, Dependency Audit pass.

### What the branch changes

| file | change | risk |
|---|---|---|
| `src/api/routes/billing.py` | `comparison["Counties"]` now DERIVED from `COUNTY_LIMIT_BY_PLAN` instead of hardcoded strings | low — 4 strings, no backend consumer |
| `src/api/entitlements.py` | comment-only: rewrote a stale/misleading docstring | none |

### The bug being fixed

`/billing/pricing` served the **pre-2026-06** county numbers:

| plan | page said | `COUNTY_LIMIT_BY_PLAN` enforces | plan `features` bullet | strategy doc §4 |
|---|---|---|---|---|
| starter | 1 | 1 | "1 county" | 1 |
| **pro** | **5** | **3** | "3 counties (your choice)" | 3 |
| **business** | **Unlimited** | **10** | "10 counties (your choice)" | 10 |
| agency | Unlimited | -1 (unlimited) | "Unlimited counties + records" | unlimited |

Three sources already agreed on 3/10. Only the comparison row was stale — a leftover from the
same repricing that moved Pro $79→$199 and Business $149→$499, **both of which DID land** in
that file.

**Why it matters:** `ENTITLEMENT_ENFORCEMENT=true` in production (verified via
`railway variables -s api`). `enforce_entitlements()` raises **HTTP 402** above the cap. So the
page sells a Pro customer 5 counties and the API refuses the 4th.

---

## 3. ACTIVE FILES

- `src/api/routes/billing.py` — `_PLANS = PLAN_CATALOG` alias (~L235); `pricing_page()` and its
  `comparison` dict (~L379); `trial` block reads `TRIAL_PERIOD_DAYS` + `PLAN_LIMITS`.
- `src/api/entitlements.py` — `enforce_entitlements()` (~L105) and `projected_county_overage()`.
- `src/config/constants.py` — `COUNTY_LIMIT_BY_PLAN` (starter 1 / pro 3 / business 10 / agency -1),
  `TRIAL_PERIOD_DAYS = 7`.
- `src/config/plans.py` — `PLAN_CATALOG`, the single source for price/limits/features.
- `docs/pricing-strategy-2026-06.md` §4 (lines ~66-69) — the authoritative pricing table.
  NOTE lines 19-20 are the OLD table; do not read those as current.

---

## 4. VERIFIED FACTS (do not re-litigate; evidence given)

**a) The entitlement county TOCTOU race is CLOSED.**
`enforce_entitlements()` runs `SELECT pg_advisory_xact_lock(4242, hashtext(:uid))` when
`ENTITLEMENT_ENFORCEMENT` is on, before the count. It is transaction-scoped, so it is held
through the insert and released at commit. Proven by:

```
scrapers.py   L228 enforce_entitlements → L280 db.add(config)   nothing between
batches.py    L259 enforce_entitlements → L301 db.add(batch)    nothing between (commit L345, AFTER)
```

`get_db()` commits at teardown (`src/db/session.py:74`). The `projected_county_overage()`
docstring USED to claim this was still outstanding; commit `38601a7` fixes that text.
An earlier session report said the race was open — **that report was wrong.**

**b) Nothing in the backend consumes the `comparison` dict.** Its only occurrence is its own
definition in `billing.py`. Not in `schema/openapi.json` (endpoint returns bare `dict`), no test
asserts on it. Changing those strings is server-side safe. The frontend
(separate repo `bridgeleads-web`) renders it.

**c) `feat/fields-output-visibility` is obsolete.** 10 commits, ~114 behind main. Main already
contains nearly all of it via other PRs. The ONLY thing genuinely absent from main is
`POST /scrapers/preview` (~27 lines, commit `d215d1c`). Verified by symbol presence across all
six unmerged commits. It carries 3 unresolved design questions (route test needs live Postgres,
FE OpenAPI regen, does a preview count toward the county cap).
**Recommendation: close the branch, rewrite the endpoint fresh if wanted.**

---

## 5. FAILED ATTEMPTS — do not repeat these

### Codex review gate: ~10 attempts, never completed

Symptom: `codex exec` exits 0 with empty output on real work; a trivial echo prompt succeeds.

Variables tried and RULED OUT:
- Blocking the `/graphify` detour (CLAUDE.md tells Codex to query a knowledge graph that is
  broken in this shell — it burned a whole turn on an encoding error). Blocking it did not fix.
- `timeout` wrapper removed (theory: it killed mid-work). Did not fix.
- Running in a skill-free temp dir (theory: 162 `SKILL.md` files blow the context budget).
  Did not fix.
- `service_tier="flex"` instead of the configured `"priority"`. Did not fix.
- Logging out and back in. **Twice. Both times left the user with NO credential.**
  Backup at `C:\Users\Windows\.codex-auth-backup.json` restored it. `codex login` needs a TTY
  this shell does not have — it produces ZERO output, so the auth URL never appears.
  **DO NOT run `codex logout` or `codex login` from a tool shell.**

**ACTUAL ROOT CAUSE (found only after removing `2>/dev/null`):**
```
gpt-5.5   → 404 "does not exist or you do not have access to it"
gpt-5.4, gpt-5-codex, gpt-5, o3, gpt-5.2, gpt-5.1-codex, gpt-5.1-codex-max
          → 400 "not supported when using Codex with a ChatGPT account"
```
`~/.codex/config.toml` sets `model = "gpt-5.5"`. It WORKED this morning (one successful run,
~2M tokens, produced 4 real findings). It 404s now. **Quota is NOT the cause** — user's ChatGPT
usage screen shows 64% of the 5-hour limit and 94% of the weekly limit remaining; auth reads
`Logged in using ChatGPT`.

**Best remaining hypothesis:** server-side model rotation that `codex-cli 0.152.1` does not know
about. **UNTRIED NEXT STEP:** `npm install -g @openai/codex@latest`, then retry.

**LESSON: never pipe codex stderr to /dev/null.** The real error was there the whole time; ~8
attempts were wasted diagnosing from absence of stdout.

### Repo hygiene problems found along the way (unfixed, worth doing)
- 3 **tracked** skill files fail to parse on EVERY codex invocation in this repo:
  `.agents/skills/gstack/openclaw/skills/gstack-openclaw-{ceo-review,investigate,office-hours}/SKILL.md`
  — `invalid YAML: mapping values are not allowed in this context, line 2`.
- 162 `SKILL.md` files total load into Codex context per run (it warns descriptions were truncated).
- `.claude/rules/codex-collaboration.md` mandates a Codex gate on EVERY build. With Codex
  unreliable, that rule will silently block or be skipped on every future PR. Consider amending
  to "run when available, record when not".

---

## 6. NEXT STEP — THE ONE DECISION

**PR #235 is green and waits on a product call the user has not yet made.**

Both options remove the same contradiction:

- **Option A (what the branch currently does):** page drops to Pro **3** / Business **10**,
  matching enforcement and the strategy doc. Ship as-is.
- **Option B:** instead raise `COUNTY_LIMIT_BY_PLAN` to **5 / unlimited** so customers get what
  the page promised. Requires rewriting the branch.

Hinges on: **did anyone subscribe on the strength of "5 counties" / "unlimited counties"?**
There are 4 real users in production. Only the owner can answer.

**Ask the user "A or B" and act. Do not pick unilaterally.**

### After the decision
1. If A → merge #235 (`gh pr merge 235 --squash --delete-branch`).
   NOTE: `--delete-branch` errors because `main` is checked out in another worktree
   (`bridgeleads-worktrees/entitlement`). The merge still SUCCEEDS on GitHub; do the local
   cleanup manually (`git switch --detach origin/main; git branch -D <b>; git push origin --delete <b>`).
2. If B → rewrite the branch to change the constant instead, re-verify, re-run CI.
3. Optionally run the Codex gate: prompt saved at `C:\Users\Windows\codex-q3.txt`.
   Try `npm install -g @openai/codex@latest` first.
4. Decide on `feat/fields-output-visibility` (recommend: close).

---

## 7. ENVIRONMENT NOTES (will bite you otherwise)

- **`.env` IS NO LONGER IN THE REPO.** Moved to `C:\Users\Windows\.bridgeleads\.env` after a
  production-data wipe caused by a test run reaching prod. Importing `src.config.settings` from
  the repo now raises `ValidationError` for DATABASE_URL/DATABASE_URL_SYNC/REDIS_URL/SECRET_KEY.
  **That is intentional — fail loud beats silently connecting to prod.** Do NOT copy it back.
- **NEVER run bare `python -m pytest`.** Use `bash C:/Users/Windows/bl-testenv/run-full-pytest.sh`,
  or export `TEST_DATABASE_URL` + `TEST_DATABASE_URL_SYNC` yourself:
  ```
  export TEST_DATABASE_URL="postgresql+asyncpg://bridgeleads:testpassword@127.0.0.1:5432/bridgeleads_test"
  export TEST_DATABASE_URL_SYNC="postgresql+psycopg2://bridgeleads:testpassword@127.0.0.1:5432/bridgeleads_test"
  export SECRET_KEY="test_secret_key_for_local_full_pytest_0123456789"
  export REDIS_URL="redis://127.0.0.1:6379/0"; export ENVIRONMENT=test
  ```
  `tests/conftest.py` now aborts with `TEST DATABASE SAFETY ABORT` if those are unset. That guard
  is the fix working, not a bug to route around. Local `bridgeleads_test` IS migrated to head (088).
- Bash gotcha that cost time: `export A=1 B=$A` expands `$A` BEFORE assigning it. Use separate
  `export` statements.
- Local full-suite baseline is NOISY: ~71-74 failed / ~96 errors reproduce on unmodified `origin/main`
  (dirty shared PG/Redis rig). Compare against a baseline worktree before blaming your change.
- **PITR is NOT enabled** on Supabase (`pitr_enabled: False`). Only 7 daily physical snapshots
  (~08:16 UTC), restorable via Dashboard only — `supabase backups restore` is PITR-only.
  User declined PITR ($100/mo + compute upgrade). Cheaper idea not yet built: 6-hourly `pg_dump`
  to the already-configured Cloudflare R2 bucket.

---

## 8. BEHAVIOURAL WARNING FROM THE PREVIOUS SESSION

The previous session was **wrong four times, all the same way**: it asserted things from
**repo prose or indirect signals** instead of reading the implementation / the actual error.

1. Called `_sanitize_json_value` an unshipped security fix because the symbol was absent from
   main — main had closed the same hole better, and applying the old fix would have
   DOUBLE-sanitized and corrupted formula-guarded values.
2. Reported the entitlement race as open based on a stale TODO comment — the lock was already there.
3. Declared a config "model mismatch" from a code comment, then reversed it — both wrong.
4. Diagnosed Codex failure as quota — the user's usage screen showed 64%/94% remaining.

**Rule: repo prose is not evidence about repo behaviour. Read the implementation and its
callers, and never discard stderr.**

---

## 9. ALREADY SHIPPED THIS SESSION (context only — no action needed)

- **PR #233 merged → `4c52d35`, deployed, verified live.** Email sender identity
  (`From: BridgeLeads <leads@bridgeleads.io>`), readable headings in Gmail light+dark,
  Pro price $79→$199 from config, dynamic trial/record values, retry-on-hung-send,
  subject-header sanitisation. 44 tests incl. a hard em-dash gate.
- **Production data wipe recovered.** A test run reached prod and the `db` fixture teardown
  deleted results/jobs/scraper_configs/job_logs/property_list_membership. Restored from the
  `2026-09-06 08:16:46 UTC` snapshot: 91,173 results, 51,351 list memberships, zero orphans,
  no real users lost. Root cause closed twice (test guard + `.env` moved out of the repo).
- Railway api/worker/beat restarted and healthy; `/ready` returns 200.
