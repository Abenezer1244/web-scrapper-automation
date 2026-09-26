# Secrets, Supply Chain, CI/CD, DB Privilege and Infra Audit (round 2)

- **Date:** 2026-09-25
- **Backend ref audited:** `origin/main` `fc38e620` (PR #356, migration 101). The audit worktree was
  checked out detached at `fc38e620`; the base the task named (`25a04eaf`) plus the #356 diff
  (`25a04eaf..fc38e620`, 9 files) were both reviewed.
- **Frontend ref audited:** `origin/master` `8332673` (read via git only, not modified).
- **Mode:** read-only. No pytest, no Railway, no network DB, no history rewrite, no secret value
  printed anywhere (every value below is `first4...last3`). `.env` contents in the main checkout
  were not read; only filenames were listed.

---

## VERDICT

**One real secret is committed and live at the tip of `main`: a 40-character Cloudflare API token in
`infra/terraform/terraform.tfvars` (S-1, P1).** It was added 2026-03-17 and has been in every commit
since, including every production Docker image. The 2026-09-16 audit reported history "clean"
because its patterns were upper-case and prefix-based (`sk_live_`, `AKIA`, `SECRET_KEY=`), and a
lower-case HCL key with no vendor prefix fell through. My first pass fell through the same gap.
A case-insensitive rerun caught it.

Everything else in history is a placeholder, a test value or a fake. Details are in the secrets table.
pip-audit is clean (0 known vulns across 94 resolved packages). Migration 101's RLS, grants and
trigger design are sound; the empty-GUC rule is implemented correctly in all three triggers.

| ID | Sev | Title |
|---|---|---|
| S-1 | **P1** | Cloudflare API token (DNS + R2 + WAF scope) committed in tracked `terraform.tfvars`, shipped in every image |
| S-2 | P2 | Owner/DDL BYPASSRLS DSN (`DATABASE_URL_MIGRATE`) sits in the env of every runtime service |
| S-3 | P2 | F-14 still open: prod DSN + unused prod Railway token are repo-level secrets; prod secrets are in the env of a `pip install` step |
| S-4 | P2 | CI `deploy-production` runs bare `alembic upgrade head` with no lock, racing Railway's locked `migrate.py` (latent) |
| S-5 | P2 | Plaintext prod credentials in an institution-managed OneDrive folder (RLS role passwords, admin login in a log) |
| S-6 | P3 | F-18 residue: two stale "prod role has BYPASSRLS" comments survive |
| S-7 | P3 | F-13 residue: `BLIND_INDEX_KEY` guard exists but is lazy, not checked at boot |
| S-8 | P3 | CI supply chain: tag-pinned actions, unpinned `@railway/cli`, FE PR jobs can read a PAT covering the whole BE repo |
| S-9 | P3 | `.gitignore` / `.dockerignore` gaps (dumps, `*.bak`, `memory.db`; the dockerignore is a narrow denylist) |
| S-10 | P3 | Dormant `docker-compose.prod.yml` publishes unauthenticated Prometheus/Loki; dev compose binds DB/Redis on 0.0.0.0 |
| S-11 | P3 | `external_source_health`: no ENABLE RLS and no anon/authenticated revoke in any migration |
| S-12 | P3 | Image and dependency pinning: base image by tag, no `--require-hashes`, OS packages never scanned |

---

## FINDINGS

### S-1 [P1] Cloudflare API token committed in `infra/terraform/terraform.tfvars`

- **Evidence:** `git ls-files infra/terraform` shows `terraform.tfvars` **tracked** at `fc38e620`.
  It was added in `579dec50` (2026-03-17, "fix: resolve all ruff lint errors (CI green)") and never
  changed after that. Line 1: `cloudflare_api_token = "AwLQ...zyR"`. The value is 40 characters from
  `[A-Za-z0-9-]` with Shannon entropy 4.75. That is exactly the shape of a Cloudflare API token, and
  it is not a placeholder. `infra/terraform/main.tf:23-26` describes the variable as a
  "Cloudflare API token with DNS + R2 + WAF permissions" (`sensitive = true`). It is on
  `origin/main` and on at least 2 other remote branches, so it was **pushed to GitHub**.
- **Why nothing caught it:** `.gitignore` lists `infra/terraform/terraform.tfvars`, but that line
  was added **after** the file was tracked, and a gitignore entry never untracks a file. The prior
  audit's history regexes were upper-case and vendor-prefix based. `.dockerignore` excludes only
  `infra/terraform/.terraform`, so the Dockerfile's `COPY . .` puts the token at
  `/app/infra/terraform/terraform.tfvars` in **every Railway image and every GHCR image**.
- **Who can read it today:** every collaborator on the private repo, every clone and worktree
  (29 `bl-wt-*` worktrees plus the OneDrive checkout), the FE repo's `BACKEND_SCHEMA_TOKEN` PAT
  (Contents read on this repo, and readable by FE PR jobs, see S-8), any AI or Codex session that
  reads the repo, and anyone who gains code execution in the worker container (Chromium renders
  attacker-controlled county HTML with `--no-sandbox`, prior F-07).
- **Impact if valid:** DNS edits could hijack `api.bridgeleads.io` or `bridgeleads.io` and
  harvest credentials or bearer tokens. R2 access would expose the exports bucket, which holds lead
  CSVs with PII. WAF permissions would let an attacker switch off the Cloudflare layer.
- **Likely valid?** **Unknown. Treat it as live.** Checking it would mean sending the token to the
  Cloudflare API, which I did not do.
- **Fix, in order:**
  1. Roll or revoke the token in the Cloudflare dashboard (My Profile, API Tokens) **now**.
  2. Review the Cloudflare audit log from 2026-03-17 onward: token use, DNS record changes, R2 access
     keys, WAF and firewall rule changes.
  3. `git rm --cached infra/terraform/terraform.tfvars` (the `.gitignore` entry already exists), and
     supply the token as `TF_VAR_cloudflare_api_token`.
  4. Add `infra/` to `.dockerignore`.
  5. Add a CI secret scanner (gitleaks with its generic high-entropy rule) so the next one is caught.
  6. A history purge is optional once the token is rotated. The owner has to decide on it, because
     it means a force-push across a shared repo. Rotation is what actually closes the exposure.

### S-2 [P2] The owner/DDL BYPASSRLS credential lives in every runtime container

- **Evidence:** `start.sh` runs `scripts/migrate.py` on boot for the API, the worker **and** beat.
  `migrate.py:95-101` prefers `DATABASE_URL_MIGRATE` (the "owner/DDL role"). The cutover repoint
  plan `scripts/_cutover_step4_repoint.py:9-10,68-71` sets `DATABASE_URL_MIGRATE = the current
  postgres sync URL (owner)` on **api, worker and beat**. The Supabase `postgres` owner is BYPASSRLS
  (`scripts/apply_rls_force.sql:56-70` requires the SECURITY DEFINER owners to be BYPASSRLS).
- **Impact:** RLS and the least-privilege roles (`bridgeleads_app` has no DDL and DELETE on two
  tables only; `bridgeleads_system` has no DDL) protect against app-level query bugs. They do
  nothing against process compromise, because the process environment also holds a BYPASSRLS DDL
  credential. The worker is the most exposed process: it renders hostile HTML with `--no-sandbox`
  (prior F-07). One renderer escape gives full read/write/DDL on every tenant. The same process
  also holds the S-1 token on disk.
- **Prerequisites:** code execution or env disclosure in any service.
- **Fix:** run migrations as a Railway **pre-deploy command** or a dedicated one-shot service that
  holds `DATABASE_URL_MIGRATE`, and remove that variable from api, worker and beat. `start.sh`
  already tolerates a missing migrate DSN for worker and beat, since they fail open. The API's
  fail-closed schema gate can check `alembic_version` against head read-only instead of migrating.

### S-3 [P2] F-14 still open, plus prod secrets in the env of a `pip install` step

- **Evidence (live `gh api`, names only):** repo-level secrets are `DATABASE_URL_SYNC` (2026-03-18),
  `RAILWAY_TOKEN_PRODUCTION` (2026-03-18) and `RAILWAY_TOKEN_STAGING`. Both environments
  (`production` and the stray `bridgeleads-production / production`) have `protection_rules: 0`,
  `deployment_branch_policy: null` and `can_admins_bypass: true`. Branch protection on `main`
  requires the Test and Dependency Audit checks, 0 reviews, and admins are not enforced.
  `DATABASE_URL_SYNC` is a **working DDL-capable production DSN**: the last main run
  (`29758107043`, 2026-07-20) shows "Run Migrations: success" with it. `RAILWAY_TOKEN_PRODUCTION`
  is referenced by **no** workflow (a standing, unused production deploy token).
- **New detail:** `ci-cd.yml:321-337` runs `pip install -r requirements.txt` **in the same step**
  whose env holds the prod DSN, `BLIND_INDEX_KEY` and `FIELD_ENCRYPTION_KEY`. `requirements.txt`
  pins top-level packages only; transitive dependencies resolve to the latest compatible release at
  install time, and any sdist runs arbitrary build code. A malicious transitive release would
  therefore see production keys.
- **Mitigating factors:** the repo is private, triggers are `pull_request` (not
  `pull_request_target`), `default_workflow_permissions` is `read`, and Actions on main has not run
  since 2026-07-20.
- **Fix:**
  1. Move `DATABASE_URL_SYNC` into the `production` environment, or delete it (see S-4).
  2. Delete `RAILWAY_TOKEN_PRODUCTION`.
  3. Add required reviewers and a `main`-only branch policy to `production`, and delete the stray
     environment.
  4. Split `pip install` into its own step with no secrets in its env.

### S-4 [P2, latent] CI migrates with bare `alembic upgrade head`, racing the locked path

- **Evidence:** `ci-cd.yml:311-337` (`deploy-production`, on push to main) runs `alembic upgrade
  head` directly. Railway redeploys on the same push, and `start.sh` runs the advisory-locked
  `scripts/migrate.py`. Migration 101's own docstring (lines 54-61) says: "**If anything ever
  migrates with bare `alembic upgrade`, that argument collapses**". Its invalid-index drop
  (`_build_parent_unique`, 101:440-442) is only safe under that lock, so an unlocked runner can drop
  an index that the locked runner is still building CONCURRENTLY.
- **Impact:** integrity and availability, not confidentiality. Races can fail a migration, and the
  API then refuses to boot. This stays dormant while Actions is billing-blocked and activates when
  Actions is restored.
- **Fix:** delete the `deploy-production` migration job (Railway already migrates), or make it call
  `python scripts/migrate.py`. Deleting it also removes the need for the S-3 secrets.

### S-5 [P2] Plaintext production credentials in the institution-managed OneDrive sync folder

- **Evidence (filenames only; contents of these files were not read except as noted):** the primary
  checkout lives under `OneDrive - Seattle Colleges/Desktop/web-scrapper-automation`. That is a
  Microsoft 365 tenant the owner does not administer. It contains:
  - `.rls-cutover-secrets` and `.rls-cutover-repoint.sh`. `.gitignore:151-153` documents these as
    "RLS cutover role passwords (local operator secret)", which means the `bridgeleads_app` and
    `bridgeleads_system` production DB passwords.
  - `scripts/audit_out_ui/run3_thurston_whatcom.log`, which contains a login URL with
    `email=admin%40bridgeleads.io&password=Brid...` (found by the history scan: the file is in the
    **local-only** `stash@{0}` untracked-files commit `568f67f3`, which is not on any remote).
  - The FE checkout's `.env.check` and `.env.local` (F-24).
- **Impact:** tenant administrators, eDiscovery, or anyone who compromises the college account can
  read production DB role passwords and an admin login. Whether the admin password is still valid
  is unknown.
- **Fix:**
  1. Move the secrets to a password manager.
  2. Delete the `audit_out*` logs and drop the stash.
  3. Rotate the admin password, plus the two role passwords if the tenant is not under owner control.
  4. Longer term, move the working copies off the institution's OneDrive.

### S-6 [P3] F-18 residue: stale BYPASSRLS comments

`session.py:386-392` and the `check_rls_role_status` docstring are **fixed** (now 327-339, 432-437).
Two stale claims survive:
- `src/config/settings.py:229-235`: "the production role currently has BYPASSRLS and worker paths
  depend on it ... Enabling it before the cutover WILL break scrapes".
- `src/db/session.py:218-223`: "Today that is masked because the prod role has BYPASSRLS".

These are the same kind of text that misled two reviewers before. Fix: rewrite both to describe the
post-cutover reality.

### S-7 [P3] F-13 residue: `BLIND_INDEX_KEY` guard is lazy

The guard now exists and fails closed (`src/utils/crypto.py:196-217`, production or strict mode, so
F-13 is **fixed**). However, `main.py:49-50` and `src/workers/__init__.py:174-175` prime only the
Fernet `_instance()`, not `_blind_index_secret()`. A missing key would therefore surface as 500s on
the first login or register instead of a refused boot. Fix: call `_blind_index_secret()` next to
`_instance()` in both places.

### S-8 [P3] CI/CD supply chain

- Every action is tag-pinned, none by SHA: `actions/*@v4/v5`, `codecov/codecov-action@v4`
  (third-party, runs in the test job), and `docker/login-action@v3`, `metadata-action@v5`,
  `setup-buildx-action@v3` and `build-push-action@v5` (the build job holds `packages: write`). Repo
  settings are `allowed_actions: all` and `sha_pinning_required: false`.
- `npm install -g @railway/cli` is unpinned, and it runs with `RAILWAY_TOKEN_STAGING` in env
  (staging job).
- The FE repo's `BACKEND_SCHEMA_TOKEN` is a **repo-level** secret available to PR-triggered jobs
  (`ci.yml:38-50`). It grants Contents read on the whole backend repo, which today includes the
  S-1 token.
- Fix: pin to commit SHAs (dependabot `github-actions` ecosystem keeps them current), set
  `sha_pinning_required`, pin `@railway/cli@<version>`, and restrict the FE PAT to push-to-master
  jobs or an environment.

### S-9 [P3] Ignore-file gaps

- `.gitignore` has no rule for `*.sql` dumps, `*.dump`, `*.sqlite*`, `*.db`, `*.bak`,
  `.claude/memory.db` or `.env.example.tmp`-style scratch. `.claude/memory.db` (147 KB) shows as
  untracked and unignored in the primary checkout, and a copy was captured in the local stash commit
  `568f67f3` (a blob scan of it found no secret patterns).
- `.dockerignore` is a denylist that does not exclude `infra/`, `scripts/audit_out*`,
  `scripts/.e2e*.json` (E2E credentials per `.gitignore:118-122`), `.rls-cutover-secrets`,
  `.codex/`, `.context/`, `.superpowers/` or `*.bak`. Railway builds from the GitHub source, so only
  tracked files reach production (S-1 is the live case). A local `docker build` or
  `docker compose build` from the primary checkout would bake all of the above in.
- Fix: add these patterns, or switch `.dockerignore` to an allowlist (`*` then `!src !workers
  !alembic !main.py !start.sh !requirements.txt !scripts/migrate.py ...`).

### S-10 [P3] Compose files expose admin surfaces (dormant)

- `docker-compose.prod.yml` is not used by Railway and was last touched 2026-03-17. If anyone runs
  it, it publishes:
  - Prometheus `9090` with `--web.enable-lifecycle` and no auth (unauthenticated `POST /-/quit`)
  - Loki `3100` unauthenticated (push and query)
  - Grafana `3001`
  - Flower `5555` with basic auth over plain HTTP (and `flower` is not in `requirements.txt`)

  Docker-published ports bypass host firewalls.
- `docker-compose.yml` (dev) binds Postgres `5432` and Redis `6379` on all interfaces. Both are
  password-protected, but they are reachable from the LAN.
- Fix: delete `docker-compose.prod.yml`, or bind every admin port to `127.0.0.1`. Bind the dev
  ports as `127.0.0.1:5432:5432` and `127.0.0.1:6379:6379`.

### S-11 [P3] `external_source_health` has no RLS in migrations

Migration 083 creates the table with no `ENABLE ROW LEVEL SECURITY` and no REVOKE from
`anon`/`authenticated`. Migration 084's docstring asserts "RLS is enabled on this table", which is
probably Supabase's auto-enable or manual drift, and cannot be verified from the repo. The table
holds non-tenant data (per-source health). If Supabase's Data API is enabled and default privileges
grant `anon`/`authenticated` on new tables, an anon-key holder could write a "blocked" row and
silently disable enrichment. Fix: add a migration that enables RLS and revokes from
`anon`/`authenticated` (the 101 pattern), and verify `relrowsecurity` in prod.

### S-12 [P3] Image and dependency pinning

`FROM python:3.12-slim` is pinned by tag, not digest. apt packages are unpinned. `pip install` runs
without `--require-hashes`, and transitive dependencies are unpinned. OS-level CVEs in the image
are never scanned, because pip-audit covers Python only. Fix: add a lock file with hashes
(`pip-compile --generate-hashes`), pin the base image by digest, and add an image scan (Trivy or
Grype) to the build job.

---

## SECRETS-HISTORY TABLE

| Type | Location | Commit (latest touching) | Redacted | Likely valid? | Remediation |
|---|---|---|---|---|---|
| **Cloudflare API token** | `infra/terraform/terraform.tfvars:1` (tracked at tip, in images) | `579dec50` 2026-03-17, still at `fc38e620` | `AwLQ...zyR` (40) | **Unknown, assume YES** | S-1: roll now, audit CF logs, untrack |
| Cloudflare zone id / account id | same file | `579dec50` | `8218...79a`, `8f25...127` (32) | Identifiers, not secrets | Untrack along with S-1 |
| Admin password (URL param) | `scripts/audit_out_ui/run3_thurston_whatcom.log` in **local** stash commit `568f67f3` (not pushed); file still on disk | `568f67f3` 2026-06-19 | `Brid...%21` (18) | Unknown | S-5: rotate, delete log, drop stash |
| Vercel OIDC token (F-24) | FE `.env.check`, untracked, gitignored, **never committed** (verified all FE history + blobs) | n/a | not read | Expired (OIDC ~12h, file dated 2026-03-24) | Delete file |
| Terraform remote-backend state | `infra/terraform/.terraform/terraform.tfstate` | added `579dec50`, removed `1f5e7caf` 2026-06-01 | org `brid...ads`, `token: null` | No secret present | none |

**Excluded as non-secrets (and why):**
- `.env.example`: `changeme`, `your_*_here` and `${VAR}` placeholders.
- CI and test DSNs: `bridgeleads:test...ord@localhost/127.0.0.1` in `ci-cd.yml`, `run-audit-tests.sh`,
  docs and HANDOFFs; `u:***@db.abc.supabase.co`, `{host}` templates and
  `nobody@203.0.113.1` (TEST-NET) in tests.
- `redis://default:x@redis.railway.internal:6379` in `tests/test_pierce_cv_owner.py`: the password
  is the single character `x`.
- `sk_test_fake`, `whsec_fake`, `re_fake`, `whsec_promo_access_...789` (test fixture),
  `test_secret_key_for_local_full_pytest...`, `ci-test-secret-key-minimum-32-...`.
- Fake JWTs `eyJh...123` and `eyJh...678` in `tests/test_log_redaction.py`: payload fails to decode,
  and none carries `role: service_role`.
- TOTP `JBSW...PXP`: the canonical RFC example.
- Datadog RUM `pub4...0e4` in a King County Accela HTML fixture: a third-party public client token.
- Identifier-valued matches: `_TIMING_DUMMY_PASSWORD`, route `/forgot-password`, `_VERIFY_TOKEN_AUDIENCE`,
  `X-Tracerfy-Webhook-Secret` (a header name), `${{ secrets.X }}` references, `settings.X` and
  `os.getenv` references.
- FE `SECRET_PLACEHOLDER = "********"`.
- One blob hit, `VERCEL_OIDC_TOKEN=...eyJ` in `tasks/audit-frontend.md`: a truncated mention in the
  prior audit's prose, with no full JWT in the blob.

---

## PIP-AUDIT

`pip-audit 2.x -r requirements.txt --desc` in a throwaway uv venv (Python 3.13.9, Windows) resolved
**94 packages** and found **0 known vulnerabilities**.

| Severity | Count | Exploitable in prod |
|---|---|---|
| Critical / High / Medium / Low | 0 / 0 / 0 / 0 | none |

Caveats:
- Resolution ran on Windows, so Linux-only extras were not audited: `uvloop` from `uvicorn[standard]`.
  CI's pinned `pip-audit==2.10.0` on ubuntu covers them, but CI on main has not run since
  2026-07-20.
- OS packages are not covered (see S-12).
- Load-bearing pins, **do not bump blindly:** `stripe==11.4.0` (v15 `StripeObject` is not a dict;
  about 17 `.get()` call sites break) and `redis==5.2.1` (kombu requires `<6.5`, and dependabot
  already ignores it). Neither has a known CVE today.

---

## DB PRIVILEGE TABLE

Sources: `scripts/provision_rls_roles.sql` (manual, authoritative), `scripts/_cutover_step2_grants_policies.py`,
`scripts/apply_rls_cutover_policies.sql`, `scripts/apply_rls_force.sql`, and migrations 001-101.
None of this could be checked against the live DB (no network DB access).

**Roles**

| Role | Used by | SUPER | BYPASSRLS | DDL (CREATE on public) | DELETE |
|---|---|---|---|---|---|
| `bridgeleads_app` | API async + API sync DSN | no (verified in script) | no (verified) | no (script aborts if yes) | `mfa_backup_codes`, `pending_registrations` only |
| `bridgeleads_system` | worker/beat sync DSN | no | no | no | `county_records`, `property_list_membership`, `delivered_records`, `mfa_*` (2), `pending_registrations`, `skip_trace_cache` |
| owner (`postgres`) | `DATABASE_URL_MIGRATE` on **api, worker, beat**; the repo secret `DATABASE_URL_SYNC` | no (Supabase) | **yes** | yes | all | 

The owner row is the problem: it holds more privilege than any runtime path needs (S-2, S-3).

**Tables (30 in `models.py`)**

| Table group | RLS enabled | Policies | FORCE (manual script) |
|---|---|---|---|
| 28 tenant and shared tables (`users`, `jobs`, `results`, `job_logs`, `scraper_configs`, `delivered_records`, `pending_skip_trace_rows`, `skip_trace_*` (3), `password_history`, `user_record_views`, `referral_events`, `county_*` (2), `property_list_membership`, `mfa_*` (2), `dialer_deliveries`, `scraper_batches`, `batch_runs`, `audit_events`, `nts_notices`, `notifications`, `pending_registrations`, `stripe_webhook_events`, `public_sample_cache`) | yes (001/018/023/027/029/034/041/043/045/056/058/065/074/095) | GUC tenant policies plus role-targeted `_app`/`_system` | in `tbls[]` |
| `contact_lookup_actions` (**101**) | yes | app: SELECT, INSERT, UPDATE (GUC); system: FOR ALL | in `tbls[]` |
| `contact_lookup_action_results` (**101**) | yes | app: SELECT, INSERT (GUC); system: FOR ALL | in `tbls[]` |
| `contact_lookup_action_events` (**101**) | yes | app: SELECT, INSERT (GUC); system: SELECT, INSERT only | in `tbls[]` |
| `external_source_health` | **not in any migration** | `_system` FOR ALL | no (non-tenant; S-11) |

Migrations 096-100 add columns and indexes only and create no tables. FORCE has no effect on the
owner, because the owner is BYPASSRLS. That makes the 101 comment in `apply_rls_force.sql` ("Without
FORCE, RLS does not apply to the table OWNER") true but moot for this owner.

**Migration 101 review (requested):**
- **Grants.** App: SELECT/INSERT on all three tables, plus column-level `UPDATE (dispatched_at)` on
  the action only. The table-level REVOKE correctly runs before the column GRANT. System:
  SELECT/INSERT/UPDATE on the action and results tables; events stay SELECT/INSERT, and the
  `REVOKE UPDATE` sits after the blanket ALL-TABLES grant in both scripts. PUBLIC, `anon` and
  `authenticated` are revoked. The `$verify$` blocks use `has_column_privilege`, which is correct
  because `role_table_grants` cannot see column grants. **Correct.**
- **Empty-GUC rule.** All three triggers treat `''` as "no tenant" and allow it **only** when
  `current_user = 'bridgeleads_system'` or the role is super/BYPASSRLS. Otherwise they RAISE
  `insufficient_privilege`. An API session (`bridgeleads_app`) cannot pass as the worker by
  clearing the GUC. **Correct.**
- **Tenant path.** Only initial dispositions are allowed. `decided_at`, `at` and `created_at` are
  server-stamped. Events accept only the first `dispatching` hop, checked against the action row
  with `FOR SHARE`. The action UPDATE compares whole-row jsonb minus `dispatched_at`, and allows it
  once. **Correct.**
- **search_path.** The functions are not SECURITY DEFINER. They pin
  `search_path = pg_catalog, public, pg_temp`, and the action lookup is schema-qualified against a
  `pg_temp` shadow table. **Correct.**
- **Composite FKs.** `(id, user_id)` references block cross-tenant pointers. One caveat: nothing
  ties a quoted `result_id` to the action's `job_id`. That is same-tenant only, so it is an
  application-integrity point, not a security finding.

**SECURITY DEFINER functions.** `grant_referral_credit`, `activation_funnel` (029/090) and
`activation_funnel_v2` (091) all pin `search_path = public, pg_temp`, REVOKE from PUBLIC, `anon` and
`authenticated`, and GRANT EXECUTE to `bridgeleads_app` only. **OK.** The INVOKER functions in
023, 049 and 088 have no pinned `search_path`. That is lint-level only, since no role can CREATE in
`public`.

---

## CI TABLE

| Item | State |
|---|---|
| Workflows | BE: `ci-cd.yml` only. FE: `ci.yml` only |
| `pull_request_target` / `workflow_run` / `issue_comment` | none in either repo |
| `permissions:` block | BE: build job only (`contents: read, packages: write`). Repo default `read`, `can_approve_pull_request_reviews: false` (both repos) |
| BE repo secrets | `DATABASE_URL_SYNC` (prod DSN), `RAILWAY_TOKEN_PRODUCTION` (unused), `RAILWAY_TOKEN_STAGING`. Repo-level, so PR test jobs can reference them (S-3) |
| BE `production` env secrets | `BLIND_INDEX_KEY`, `FIELD_ENCRYPTION_KEY`, `SECRET_KEY`. No protection rules, no branch policy |
| Test job env | fakes only (`sk_test_fake`, `whsec_fake`, `re_fake`, local test DSN). No `${{ secrets }}` |
| Third-party actions | tag-pinned, not SHA (S-8). `allowed_actions: all` |
| Secret echo / `set -x` / env dump | none found |
| FE secrets | `BACKEND_SCHEMA_TOKEN` repo-level, used in PR jobs (S-8). FE environments: Preview, Production |
| Deploy keys / webhooks | 0 deploy keys. Hooks list empty or not visible |
| Repo visibility | both private. `allow_forking: true` on BE |

---

## PRIOR FINDING STATUS

| ID | Status | Evidence |
|---|---|---|
| F-13 `BLIND_INDEX_KEY` fail-closed | **FIXED** (residual P3, S-7) | `crypto.py:196-217` raises in production or strict mode. Not primed at boot |
| F-14 prod DSN + Railway token repo-level | **OPEN** | live `gh api` 2026-09-25, unchanged since 2026-09-17 (S-3) |
| F-18 stale RLS comments | **PARTIALLY FIXED** | `session.py:327-339,432-437` fixed. `settings.py:229-235` and `session.py:218-223` still stale (S-6) |
| F-24 `.env.check` / ignores / project ref / unused deploy token | **PARTIALLY OPEN** | `.env.check` still on disk (gitignored, never committed, token expired). Backup/DB ignore gaps remain (S-9). Supabase project ref still in tracked docs (an identifier). `RAILWAY_TOKEN_PRODUCTION` still present and unused |

---

## EXPLICIT NON-FINDINGS

- No `.env`, `.env.*` (except `.env.example`), `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`,
  credentials or service-account file was ever added in either repo, across all refs and reflogs.
- No Stripe live or test secret key, `whsec_` secret, AWS key, Resend key, GitHub token, Anthropic
  or OpenAI key, Slack token, Google key, private key block, or Supabase `service_role` JWT anywhere
  in either repo's history or object store.
- Every DSN with a password in history points at localhost, 127.0.0.1, a TEST-NET address or a
  templated host.
- The frontend repo is clean across all 707 commits and 1,984 blobs. No `NEXT_PUBLIC_*` secret, and
  no `.env*` ever tracked.
- `DEBUG` defaults to False and `ENVIRONMENT` defaults to `production` (fail-safe).
  `/docs`, `/redoc` and `/openapi.json` are disabled unless DEBUG is set. There is no `/metrics` or
  debug route, `/health` returns a static body, and `/ready` gives a coarse status plus a ref.
- CORS is an explicit origin list (no wildcard with credentials).
- The Dockerfile runs as non-root `bridge` (uid 1000). `start.sh` has no debug or reload flags in
  production. `docker-compose.yml` is marked dev-only.
- `.gitignore` covers `.env`, `.env.*`, terraform state, `scripts/.e2e*.json` and
  `.rls-cutover-secrets`.
- pip-audit found 0 known vulnerabilities.

---

## COVERAGE (what was scanned, exactly)

- **Scanners:** gitleaks and trufflehog are **not installed**. I used a custom streaming scanner
  instead (Python, `git log -p --cc` over added lines, values stored only as sha256 plus a redacted
  form), and a full object-store blob scanner (`git cat-file --batch-all-objects`, which covers
  reachable and unreachable blobs, binaries included).
- **History ranges:**
  - Backend: `--all` (494 refs, 2,093 commits, 2026-03-15 to 2026-09-25, including `fc38e620`),
    plus `--reflog --not --all` (140 more commits, including 3 stashes). 870,486 diff lines; 6,553 blobs.
  - Frontend: `--all` (272 refs, 707 commits, 2026-03-17 to 2026-09-22), plus 50 reflog-only
    commits. 292,785 diff lines; 1,984 blobs.
  - Merge commits: covered via `--cc` (conflict resolutions only; merges that introduce content
    outside a conflict are covered by the blob scan).
- **Diff-line regexes:**
  - Vendor prefixes: `(sk|rk)_live_`, `(sk|rk)_test_`, `whsec_`, `pk_live_`, `(AKIA|ASIA)[0-9A-Z]{16}`,
    `re_[A-Za-z0-9]{8}_[A-Za-z0-9]{16,}`, `gh[pousr]_`, `github_pat_`, `sk-ant-`, `sk-(proj-)?`,
    `AIza`, `xox[baprs]-`, `vercel_`, `vc[pk]_`, `-----BEGIN .*PRIVATE KEY-----`.
  - Credentialed URLs: `postgres(ql)?(+driver)?://u:p@`, `rediss?://u:p@`.
  - JWTs: `eyJ..eyJ..sig`, with the payload decoded and checked for `role`.
  - Named assignments, **case-insensitive** (second pass): `SECRET_KEY`, `FIELD_ENCRYPTION_KEY`,
    `BLIND_INDEX_KEY`, `JWT_SECRET`, `RAILWAY_*TOKEN`, `VERCEL_*TOKEN`, `RESEND_API_KEY`,
    `STRIPE_*KEY/SECRET`, `ANTHROPIC_API_KEY`, `TRACERFY_*`, `TWOCAPTCHA*`, `CAPTCHA_*KEY`,
    `SUPABASE_*KEY`, `SERVICE_ROLE*`, `S3_*`, `AWS_SECRET_ACCESS_KEY`, `R2_*`, `DATABASE_URL*`,
    `REDIS_URL*`, and any name containing api_key, apikey, secret, token, password, passwd,
    access_key, private_key, auth_key or credential, with a value of 12+ characters.
  - Every match was then classified by placeholder, test-marker and entropy heuristics, and each
    unexcluded match was reviewed by hand.
- **Blob regexes:** the same vendor, JWT, DSN and private-key set, plus 44-character Fernet-shaped
  literals and `VERCEL_OIDC_TOKEN=eyJ`.
- **Filenames added in history:** `.env*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `*.sql`,
  `*.dump`, `*.sqlite`, `*.db`, `*.bak`, `backup`, `credentials`, `secret`, `.npmrc`, `.pypirc`,
  `.netrc`, `service.account`, `*.jks`, `tfstate`, `.terraform/`.
- **Live GitHub API (names only):** repo and environment secrets, environments, workflow
  permissions, actions permissions, branch protection, deploy keys and hooks for both repos, and
  recent run conclusions.
- **Lesson for the next audit:** an upper-case or prefix-only secret regex misses lower-case
  IaC keys (`*.tfvars`, `*.tf`, YAML). Always scan secret names case-insensitively and add a
  high-entropy generic rule.
- **Not covered:**
  - Live DB catalog state (actual grants, RLS flags, role attributes).
  - Railway, Vercel, Cloudflare, Supabase and Stripe dashboards.
  - Whether the S-1 token and the S-5 admin password are valid.
  - Contents of `.rls-cutover-secrets` and `.env*` files (deliberately not read).
  - The FE `npm audit`.
