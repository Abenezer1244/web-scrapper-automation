# Secrets Audit — BridgeLeads

- **Scope:** secrets in the working tree, build artifacts, and git history.
- **Worktree:** `C:/Users/Windows/bl-wt-secaudit`
- **Tip:** `60f1b00` on `chore/security-audit-2026-09-16`
- **History range:** 2026-03-15 (`1486861`, initial commit) → 2026-09-16 (`60f1b00`)
- **Commits:** 1,936 reachable + 323 unreachable = 2,259 total; 6,267 blobs
- **Date:** 2026-09-16
- **Mode:** READ-ONLY. No files edited, no history rewritten, no rotation performed, no pytest run.

---

## VERDICT

**NO REAL SECRETS FOUND** — not in the working tree, not in build artifacts, not
anywhere in git history. **No rotation is required for any credential.**

The negative is auditable because the history check was not limited to reachable
commits: every blob in the object store was scanned, which covers the 323
**unreachable** commits left by amends and rebases — the exact place a secret
scrubbed from the tip would still survive.

---

## LINE 1 — WORKING TREE: clean

Every pattern match in tracked files is a placeholder, a synthetic test fixture, a
prefix comparison in code, or a redaction regex. No live credential.

| TYPE | SERVICE | LOCATION | MASKED FRAGMENT | STILL AT TIP? | ROTATE? |
|---|---|---|---|---|---|
| Placeholder | Stripe | `.env.example:32` | `sk_l...` (12 chars, literal `sk_live_...`) | Yes | **No** |
| Placeholder | Stripe | `.env.example:33` | `whse...` (9 chars, literal `whsec_...`) | Yes | **No** |
| Placeholder | Resend | `.env.example:47` | `re_....` (6 chars, literal `re_...`) | Yes | **No** |
| Placeholder | Anthropic | `.env.example:92` | `sk-a...` (10 chars, literal `sk-ant-...`) | Yes | **No** |
| Placeholder | Postgres | `.env.example:3,5,13,14` | `post...` → `bridgeleads:changeme@localhost` | Yes | **No** |
| Placeholder | Redis | `.env.example:17` | `redi...` → `:changeme@localhost:6379` | Yes | **No** |
| Prefix check (code) | Stripe | `scripts/stripe_single_customer_promo.py:83` | `startswith(("sk_live_","rk_live_"))` | Yes | **No** |
| Prefix check (code) | Stripe | `scripts/stripe_founding_code_and_webhook_events.py:147` | `startswith(("sk_live_","rk_live_"))` | Yes | **No** |
| Redaction regex | — | `src/utils/logger.py:37` | `\bsk[_-][A-Za-z0-9_\-]{16,}` | Yes | **No** |
| CI fake | Stripe | `.github/workflows/ci-cd.yml:63,64` | `sk_test_fake`, `whsec_fake` | Yes | **No** |
| Test fixture | Stripe | `tests/test_promo_access.py:43` | `whse...` (51 chars, `whsec_promo_access_signature_secret_…`) | Yes | **No** |
| Test fixture | AWS | `tests/test_skip_trace_url_secrecy.py:35` | `AKIA...` (11 chars — invalid, real AKIA is 20) | Yes | **No** |
| Test DSN | Postgres | `tests/test_db_ssl_connect_args.py:30-59` | `u:p@db.abc.supabase.co` (dummy) | Yes | **No** |
| Test SECRET_KEY | — | `run-audit-tests.sh:20` | `test...` (48 chars, `test_secret_key_for_local_full_pytest_…`) | Yes | **No** |
| Test SECRET_KEY | — | `.superpowers/sdd/task-6-report.md:247,318,404` | `ci-t...` (45 chars, `ci-test-secret-key-minimum-32-…`) | Yes | **No** |
| Public site key | reCAPTCHA | `src/scrapers/enrichment/pierce_atip.py:56`, `src/scrapers/enrichment/parcel.py:95` | `6Lcv...` (40 chars) — public, served in page HTML | Yes | **No** |
| Infra identifier | Supabase | `docs/HANDOFF-responsive-sweep-2026-07-30.md:88` | `xqbr...` (20-char project ref) | Yes | **No** — see F-5 |

`.env.example` **confirmed placeholders-only**: all 84 assignments reviewed
individually. Every value is `changeme`, `...`, `your_*_here`, `<account_id>`,
`CHANGE_THIS_…`, or empty. No real value leaked into the example file.

---

## LINE 2 — GIT HISTORY: clean. Rotation needed for: **NOTHING**

- **No `.env` was ever tracked.** `git log --all --diff-filter=A` over
  `*.env`, `*.env.*`, `*secret*`, `*credential*`, `*.pem`, `*.key`, `*.p12`,
  `*.pfx`, `id_rsa*`, `*service-account*`, `.netrc`, `.npmrc`, `.pypirc`
  returns exactly **two** additions in the whole history:
  - `6e04127` (2026-03-16) — `.env.example`
  - `1ab06a5` (2026-06-05) — `tests/test_config_secret_redaction.py`
- **All seven real-shaped key searches returned ZERO commits** (see LINE 6).
- **Full object-store scan: zero hits.** All 6,267 blobs (reachable +
  unreachable) against 15 real-secret patterns and non-local credentialed DSNs.
- The only `-S` prefix hits (`sk_live_`, `whsec_`, `AKIA`) resolve to the code
  prefix-comparisons, redaction regexes and fakes already listed above.
- Every DSN ever committed is local. Extracted and masked from each commit:

| COMMIT | DATE | MASKED DSN | ROTATE? |
|---|---|---|---|
| `31157c1` | 2026-03-17 | `postgresql+asyncpg://bridgeleads:cha***@localhost:5432` | **No** |
| `4a65de7`, `f80f00b` | 2026-06-30 | `postgresql+psycopg2://bridgeleads:tes***@localhost:5432` | **No** |
| `0987600`, `5475743`, `1839711` | 2026-07-02 | `postgresql+asyncpg://bridgeleads:cha***@localhost:5432` | **No** |
| `d6390ec`, `e323678`, `d8a32c5`, `7ce4412`, `ff9ecd6` | 2026-09-06→09 | `postgresql+asyncpg://bridgeleads:tes***@127.0.0.1:5432` | **No** |
| `31157c1` | 2026-03-17 | `redis://:cha***@localhost:6379` | **No** |

---

## LINE 3 — HARDCODED FALLBACK SECRETS

**Application code: none.** Every secret-bearing setting in
`src/config/settings.py` defaults to `""` — `R2_ACCESS_KEY_ID` (117),
`R2_SECRET_ACCESS_KEY` (118), `R2_API_TOKEN` (120), `STRIPE_SECRET_KEY` (129),
`STRIPE_WEBHOOK_SECRET` (130), `RESEND_API_KEY` (197), `ANTHROPIC_API_KEY` (294),
`CAPTCHA_API_KEY` (301), `REGRID_API_TOKEN` (305), `TRACERFY_API_TOKEN` (336),
`TRACERFY_WEBHOOK_SECRET` (338), `FIELD_ENCRYPTION_KEY` (51), `BLIND_INDEX_KEY` (82).
`SECRET_KEY` (43) has **no default at all** and a validator (92-98) rejecting
<32 chars and known-weak values. No `os.getenv("X", "<plausible secret>")`
anywhere in `src/`, `workers/`, `alembic/`, `scripts/`, `main.py`.

**CI workflow: three.** `.github/workflows/ci-cd.yml`

- **`:264`** — `SECRET_KEY: ${{ secrets.SECRET_KEY || 'migration-ci-placeholder-not-used-in-production!!' }}`.
  48 chars, so it clears the ≥32 validator, and it is **not** in the rejected set
  at `settings.py:95` — it would apply silently. `gh secret list --env production`
  confirms `SECRET_KEY` **is** set (2026-06-12), so the fallback does not fire
  today. Latent, not live.
- **`:261`** — `DATABASE_URL` → `postgresql+asyncpg://unused:unused@localhost/unused`. **Live** (no such secret exists).
- **`:263`** — `REDIS_URL` → `redis://localhost:6379/0`. **Live** (no such secret exists).

---

## LINE 4 — CI/CD

`.github/workflows/ci-cd.yml` is the only workflow.

**Correct:** all 8 secret references use `${{ secrets.X }}` (`:203, 245, 261-264, 271, 272`);
**no `pull_request_target`**, `workflow_run`, or `issue_comment` triggers; **no
`echo`/`printenv`/`set -x`/env dump** anywhere; `build` job scoped to
`contents: read, packages: write`; repo is **private** (`"isPrivate": true`);
no deploy keys; `deploy-production` gated by `if: github.ref == 'refs/heads/main'`
plus `environment: production`.

**Two real weaknesses:**

1. The `production` environment has **`"protection_rules": []`** and
   `"deployment_branch_policy": null` (`can_admins_bypass: true`). Environment-scoping
   `SECRET_KEY`/`BLIND_INDEX_KEY`/`FIELD_ENCRYPTION_KEY` therefore buys **no approval
   gate and no branch restriction**.
2. `DATABASE_URL_SYNC` (the **production** DB DSN used by `alembic upgrade head`,
   `:262,:275`) and `RAILWAY_TOKEN_PRODUCTION` are **repo-level** secrets, readable
   by any job in any triggered workflow rather than gated behind the environment.
   Since the workflow runs `on: pull_request`, a PR editing `ci-cd.yml` to reference
   it in the `test` job would exfiltrate it. Mitigated by the repo being private and
   by `pull_request` (not `_target`) withholding secrets from fork PRs — so this
   needs a collaborator or a compromised account.

Does CI need production credentials? Only `deploy-production` does, to run
migrations. That is legitimate, but it argues for environment-scoping + required
reviewers, not repo-scope.

---

## LINE 5 — STRAY FILES

**Tracked: none.** `git ls-files` matched no `.pem`, `.key`, `.p12`, `.pfx`,
`.jks`, `.ppk`, `.crt`, `.cer`, `.sqlite*`, `.db`, `.bak`, `id_rsa*`,
`id_ed25519*`, `service-account*.json`, `credentials*`, `.netrc`, `.npmrc`,
`.pypirc`. The only tracked dotenv file is `.env.example`.

**`.gitignore` is correct:** `.env` (`:32`), `.env.*` (`:33`), `!.env.example`
(`:34`). `git check-ignore -v .env` → matches `.gitignore:32`. No real `.env`
exists in the audit worktree.

**Diagnostic scripts: none embed a credential.** Every `scripts/` entry reads from
env or merely rewrites a DSN *scheme* (`postgresql+psycopg2://` → `postgresql://`).
The nine `print()` calls referencing `DATABASE_URL`/`DSN` print the variable
**name**, never a value. `scripts/_creds.py:51` prints shell instructions only.

**Untracked and NOT gitignored** (scanned — all three clean, zero credentials,
including `strings` over the 147 KB SQLite file):

- `tasks/todo.pr80.bak` (3,977 bytes)
- `live-landing.json` (6,730 bytes)
- `.claude/memory.db` (147,456 bytes)

The content is clean, but there is no guardrail stopping a future `.bak` or
agent-memory DB that *does* carry a pasted credential from being committed.

---

## LINE 6 — SCOPE SEARCHED (methodology)

Tip `60f1b00`; history range **2026-03-15 → 2026-09-16, all 2,259 commits
(1,936 reachable + 323 unreachable), all 6,267 blobs**.

**Working tree** — `git grep` across all tracked files:
`sk_live`, `sk_test`, `rk_live`, `pk_live`, `whsec_`, `AKIA`, `ASIA`,
`-----BEGIN {RSA,OPENSSH,} PRIVATE KEY-----`, `xoxb-`, `xoxp-`, `ghp_`, `gho_`,
`ghs_`, `github_pat_`, `AIza`, `SG.`, `eyJ…\.ey` (JWT), `postgres://`,
`postgresql://`, `redis://`, `rediss://`, `mongodb://`, `amqp://`,
`re_[A-Za-z0-9]{15,}`; generic
`(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\s*[:=]\s*"<literal ≥12>"`;
base64/hex literals ≥32 chars; `os.getenv`/`os.environ.get` with non-empty
defaults on secret-named vars; managed-service hostnames (`supabase.co`,
`upstash.io`, `railway.app`, `.rds.amazonaws`, `r2.cloudflarestorage`).

**History — file additions** (`git log --all --diff-filter=A --name-only`):
`*.env`, `*.env.*`, `.env`, `*secret*`, `*credential*`, `*.pem`, `*.key`,
`*.p12`, `*.pfx`, `id_rsa*`, `*service-account*`, `.netrc`, `.npmrc`, `.pypirc`.

**History — literal pickaxe** (`git log --all -S`): `sk_live_`, `rk_live_`,
`whsec_`, `AKIA`, `BEGIN PRIVATE KEY`, `BEGIN RSA PRIVATE KEY`,
`BEGIN OPENSSH PRIVATE KEY`, `xoxb-`, `ghp_`.

**History — regex pickaxe for REAL-shaped values** (`git log --all -G`), all
seven returning **zero commits**: `sk_live_[A-Za-z0-9]{16,}`,
`rk_live_[A-Za-z0-9]{16,}`, `whsec_[A-Za-z0-9]{20,}`, `sk_test_[A-Za-z0-9]{16,}`,
`AKIA[0-9A-Z]{16}`, `re_[A-Za-z0-9]{8}_[A-Za-z0-9]{20,}`, `sk-ant-[A-Za-z0-9_-]{20,}`.
Plus: 44-char base64 Fernet keys (`"[A-Za-z0-9_-]{43}="`), literal `SECRET_KEY=`
assignments, and credentialed `postgres`/`redis` DSNs.

**Full object store** (`git cat-file --batch-all-objects --batch`, 6,267 blobs,
reachable **and** unreachable): `sk_live_|rk_live_|pk_live_[A-Za-z0-9]{12,}`,
`whsec_[A-Za-z0-9]{18,}`, `sk_test_[A-Za-z0-9]{12,}`, `AKIA|ASIA[0-9A-Z]{16}`,
`re_[A-Za-z0-9]{8}_[A-Za-z0-9]{16,}`, `sk-ant-[A-Za-z0-9_-]{20,}`,
`xox[bpsa]-[A-Za-z0-9-]{10,}`, `gh[pousr]_[A-Za-z0-9]{30,}`,
`github_pat_[A-Za-z0-9_]{30,}`, `AIza[A-Za-z0-9_-]{30,}`,
`-----BEGIN [A-Z ]*PRIVATE KEY-----`; and separately all non-local credentialed
DSNs across `postgres(+driver)`, `redis`/`rediss`, `mongodb(+srv)`, `amqp(s)`.
**Zero hits on both passes.**

**CI/CD:** `.github/workflows/ci-cd.yml` read in full; GitHub API for
environments, environment secrets, repo secrets, Actions variables, deploy keys,
repo visibility.

**Docs:** `docs/`, `tasks/`, all `*.md` for credential-shaped pastes
(placeholders filtered out).

**Untracked:** primary repo stray files, incl. `strings` over `.claude/memory.db`.

### Not covered (stated honestly)

- Secret material held in the Railway / Vercel / Supabase / Stripe dashboards —
  not observable from the repo.
- Whether the values behind the CI secret *names* are themselves strong.
- F-4's exfiltration path was **not** executed; it rests on GitHub's documented
  repo-secret visibility, not on a live test. Classified UNVERIFIED on that point.

---

## FINDINGS

### F-1 — Hardcoded `SECRET_KEY` fallback in the migration job
**CLASSIFICATION:** PARTIAL-WEAK CONTROL · **SEVERITY: P3** (latent; masked today)
**EVIDENCE:** `.github/workflows/ci-cd.yml:264`. 48-char literal clears the ≥32
validator and is absent from the rejected set at `src/config/settings.py:95`.
`SECRET_KEY` is present in the `production` environment (set 2026-06-12), so the
fallback does not currently fire.
**FIX:** Delete the `|| '...'` default. A security-critical setting must fail the
job, not substitute a repo-readable constant. Same for `:261` and `:263`.

### F-2 — `BLIND_INDEX_KEY` has no production fail-closed guard
**CLASSIFICATION:** CONFIRMED VULNERABILITY (defense-in-depth gap) · **SEVERITY: P2**
**EVIDENCE:** `src/utils/crypto.py:192-200`. `_build_fernet()` (same file, 78-83)
correctly refuses the HKDF fallback when `PII_ENCRYPTION_STRICT` or
`ENVIRONMENT == "production"`. `_derive_blind_index_key()` has **no equivalent
guard** — a blank `BLIND_INDEX_KEY` silently HKDFs from `SECRET_KEY`. Combined
with F-1, a window with both absent would compute every `email_hmac` under a key
**published in this repo**, enabling offline account-existence confirmation and
locking users out — exactly the failure the comment at `:265-270` of the workflow
was written to prevent.
**FIX:** Add the same `raise RuntimeError(...)` guard to
`_derive_blind_index_key()` before the HKDF return.

### F-3 — `production` GitHub environment has zero protection rules
**CLASSIFICATION:** PARTIAL-WEAK CONTROL · **SEVERITY: P2**
**EVIDENCE:** `gh api repos/{owner}/{repo}/environments` →
`"protection_rules": []`, `"deployment_branch_policy": null`,
`"can_admins_bypass": true`.
**FIX:** Add a deployment branch policy limiting the environment to `main`, plus
required reviewers for production migrations.

### F-4 — Production DB credential is repo-scoped, not environment-scoped
**CLASSIFICATION:** CONFIRMED VULNERABILITY (exfil path UNVERIFIED) · **SEVERITY: P2**
**EVIDENCE:** `gh secret list` (repo scope) returns `DATABASE_URL_SYNC`,
`RAILWAY_TOKEN_PRODUCTION`, `RAILWAY_TOKEN_STAGING`. `DATABASE_URL_SYNC` is the
production DSN at `ci-cd.yml:262,:275`.
**FIX:** Move `DATABASE_URL_SYNC` and `RAILWAY_TOKEN_PRODUCTION` to the
`production` environment scope and delete them from repo scope.

### F-5 — Real Supabase project ref in a tracked doc
**CLASSIFICATION:** CONFIRMED VULNERABILITY (info disclosure) · **SEVERITY: P3**
**EVIDENCE:** `docs/HANDOFF-responsive-sweep-2026-07-30.md:88` — live 20-char
project ref (`xqbr...`), which names the DB host. No credential accompanies it;
the doc itself records the project as paused/deleted. Every other doc correctly
uses `<ref>` (e.g. `docs/BUILD_JOURNAL.md:3489`).
**FIX:** Replace with `<ref>`. **No rotation needed.**

### F-6 — Log redaction misses several credential families
**CLASSIFICATION:** PARTIAL-WEAK CONTROL · **SEVERITY: P3** (theoretical)
**EVIDENCE:** `src/utils/logger.py:31-52` covers `sk[_-]`, JWT, `authorization`,
`password`, `api_key`, `token`, cookies, and **`https?://` basic-auth only**. No
pattern for `whsec_`, `re_`, `AKIA`, `xox*`, `gh*_`, or credentials inside
`postgres://` / `redis://` URLs. Verified no code logs a DSN *value* — the nine
`scripts/` matches print the variable **name** — so this is a completeness gap,
not an active leak.
**FIX:** Broaden the basic-auth pattern from `https?://` to any scheme; add the
missing prefixes.

### F-7 — Backup / DB / scratch files are not gitignored
**CLASSIFICATION:** PARTIAL-WEAK CONTROL · **SEVERITY: P3**
**EVIDENCE:** `git check-ignore -v` reports **NOT IGNORED** for
`tasks/todo.pr80.bak`, `live-landing.json`, `.claude/memory.db`. All three
scanned clean.
**FIX:** Add `*.bak`, `*.db`, `*.sqlite*`, `.claude/memory.db` to `.gitignore`.

### F-8 — Unused standing production deploy token
**CLASSIFICATION:** PARTIAL-WEAK CONTROL · **SEVERITY: P3**
**EVIDENCE:** `RAILWAY_TOKEN_PRODUCTION` exists as a repo secret (2026-03-18) but
no workflow references it — only `RAILWAY_TOKEN_STAGING` is used (`:245`). A
credential with no consumer attracts no rotation pressure.
**FIX:** Delete it, or document its out-of-band use.

### F-9 — Stray duplicate GitHub environment
**CLASSIFICATION:** INFO
**EVIDENCE:** An environment literally named `bridgeleads-production / production`
(id 13125601932) exists beside `production`. API confirms it holds **0 secrets**.
**FIX:** Delete it.

---

## CONFIRMED SECURE CONTROLS (keep these)

- **`.env` was never committed** — only two credential-shaped additions in the
  entire history, both benign (`.env.example`, a redaction test).
- **`.gitignore` correct**: `.env`, `.env.*`, `!.env.example`; verified via
  `git check-ignore -v`.
- **`.env.example` is 100% placeholders** — all 84 assignments reviewed.
- **No hardcoded fallback secrets in application code** — all default `""`;
  `SECRET_KEY` required + validated (≥32 chars, weak-value rejection).
- **No tracked credential-shaped files**; no deploy keys; repo **private**.
- **CI secret hygiene**: all refs via `${{ secrets.X }}`; no
  `pull_request_target`; no env echo; least-privilege `permissions:` on `build`.
- **Log redaction filter** (`src/utils/logger.py`) with tests
  (`tests/test_log_redaction.py`), incl. PII masking for email and labelled phone.
- **User-supplied webhook secrets stripped from API responses**
  (`tests/test_config_secret_redaction.py`) with presence-flag pattern
  (`*_set: true`) instead of value echo.
- **`FIELD_ENCRYPTION_KEY` fails closed in production** (`src/utils/crypto.py:78-83`),
  a deliberate fix for the 2026-06 incident that stranded 61 users' email as
  undecryptable. `ENVIRONMENT` defaults to `"production"` (`settings.py:229`), so
  the guard engages even when the var is unset. F-2 is the one gap in this pattern.
