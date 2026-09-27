# Audit 3, leaf 1.1: architecture, trust boundaries, and secrets (AS-)

- Backend audited: worktree `bl-wt-secaudit3` at `786efcf0` (what production runs). `origin/main` has since moved to
  `33ac27fe` (PRs #362, #363: batch-dispatch fix, tests, journal). None of those 8 files touches config, CI, the
  Dockerfile or secrets (`cmd:git diff --stat 786efcf0 origin/main`).
- Frontend audited: fresh detached worktree `C:/Users/Windows/bl-web-audit3-leaf11` at `origin/master` `e42d5d0`.
- Mode: read-only. No secret value was printed or stored anywhere. Every value in this report is redacted as prefix
  plus last 3 characters, or is named by its git blob hash.
- Production probes: 54 unauthenticated GETs in total (36 for sensitive files, 1 for `/login`, 17 for JS chunks),
  spaced 1.2 s apart. I also sent anonymous read-only requests to `ghcr.io` (GitHub's registry, not a production host).

**Headline: the Cloudflare API token from audit #2 (N-01) is publicly downloadable.** The CI build job pushes every
`main` build to `ghcr.io/abenezer1244/web-scrapper-automation`. That package accepts anonymous pulls. 358 of its 362
tags were built from commits before the untracking fix, and I pulled one anonymously: its source layer contains
`app/infra/terraform/terraform.tfvars`, and the file is byte-identical to the token file in git. Nothing in the repo
shows that the token has been rotated. See AS-1.

---

## Architecture inventory

Each layer below was checked against code or config. Where only an operator dashboard could confirm a layer, the
entry says so.

| Layer | Verified implementation | Evidence |
|---|---|---|
| Frontend | Next.js 16.3.5 with Auth.js (`next-auth` 5.0.0-beta.32) on Vercel. `bridgeleads.io` and `app.bridgeleads.io` are served by Vercel (`server: Vercel`, live). No Supabase client and no DB access in the FE. The only env reads are `NEXT_PUBLIC_API_URL`, `API_URL` and `NODE_ENV` | FE `package.json`, FE `lib/auth.ts:6`, live headers |
| FE edge gate | `proxy.ts` (Next 16 middleware) redirects every non-public path to `/login` unless `req.auth.user` exists (fails closed on an Auth.js config error) | FE `proxy.ts:5-60` |
| API | FastAPI (`main.py`) behind Cloudflare (`server: cloudflare` live). 9 routers: auth, scrapers, jobs, billing, webhooks, segments, batches, notifications, analytics. Docs and OpenAPI are off unless DEBUG. The global handler returns `{detail, ref}` only | `main.py:56-61`, `main.py:86-94`, `main.py:105-112` |
| Database | PostgreSQL on Supabase. Async engine uses `DATABASE_URL` (app role). Sync engine uses `DATABASE_URL_SYNC` (system role, workers). `DATABASE_URL_MIGRATE` (owner/DDL) is read by `alembic/env.py` and `scripts/migrate.py` | `src/config/settings.py:30-32`, `alembic/env.py:22`, `scripts/migrate.py:101` |
| Auth provider | Home-grown. Backend HS256 JWTs signed with `SECRET_KEY` (1 h access, refresh with a separate audience, iss/aud pinned). API keys `bl_...` are stored as SHA-256 hashes. Passwords use bcrypt (12 rounds). Auth.js on the FE keeps the backend tokens in an encrypted 7-day JWT cookie. `session.accessToken` is exposed to client JS; the refresh token is not | `src/api/auth.py:61-74`, `src/api/auth.py:135-249`, FE `lib/auth.ts:233-272` |
| Authorization model | `get_auth_context`/`get_current_user`, `require_plan`, entitlement gate. RLS GUC `app.current_user_id` is set per transaction (belt) and every query filters by `user_id` (suspenders) | `src/api/auth.py:286`, `src/api/auth.py:405-432`, `src/db/session.py:231-247` |
| Admin system | No separate admin app. `is_admin` users pass `require_admin` (404 for others). `require_admin_mfa` adds a JWT-only, 15-minute MFA step-up for connector creation. Admin routes: `/billing/activation-funnel`, `/scrapers/connectors` (POST), and the `include_all` view | `src/api/auth.py:477-535`, `src/api/routes/billing.py:120-121`, `src/api/routes/scrapers.py:396-400`, `src/api/routes/scrapers.py:939-942` |
| Workers | Celery (`src.workers`) on the Railway `worker` service. Beat is a separate `beat` service (PersistentScheduler). Both run `scripts/migrate.py` on boot | `start.sh:53-85` |
| Queue / Redis | Upstash Redis as Celery broker and result backend. TLS cert verification is on by default (`REDIS_SSL_CERT_REQS=required`) | `src/workers/__init__.py:15-16`, `src/workers/__init__.py:60-91`, `src/config/settings.py:35-40` |
| Scheduled work | Beat entries: dispatch scheduled jobs and batches (60 s), watchdog, quota and dedup sweeps, skip-trace dispatcher, retention purge, and others | `src/workers/scheduler.py:80` |
| Scrapers | One plugin per county, extending `BaseScraper`. Registry is DB-driven. `validate_scraping_target()` is the SSRF allowlist. Connector domains are loaded at boot | `src/api/middleware/security.py:206`, `main.py:34-35` |
| Browser automation | Playwright Chromium, headless, launched with `--no-sandbox` (prior F-07). Xvfb is available for headed mode | `src/scrapers/base_scraper.py:278-281`, `start.sh:62-77` |
| Object storage | Cloudflare R2 over S3-compatible presigning (`R2_ENDPOINT_URL`/`R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY`). Public URLs need an explicit `R2_ALLOW_PUBLIC_URLS` | `src/config/settings.py:116-126`, `src/utils/data_exporter.py:333-381` |
| Stripe | Checkout, portal and change-plan use a server-side `STRIPE_SECRET_KEY`. `/billing/webhook` verifies with `stripe.Webhook.construct_event` and refuses to run if the secret is shorter than 20 characters | `src/api/routes/billing.py:1744-1757` |
| Tracerfy | Skip-trace dispatcher (worker) calls Tracerfy with `TRACERFY_API_TOKEN`. Webhook `/webhooks/tracerfy` checks the `X-Tracerfy-Webhook-Secret` header. The legacy `/webhooks/tracerfy/{provided_secret}` puts the secret in the URL path and is still enabled by default | `src/workers/skip_trace_dispatcher.py:43`, `src/api/routes/webhooks.py:48-73`, `src/api/routes/webhooks.py:169`, `src/config/settings.py:345` |
| Email | Resend (`RESEND_API_KEY`) for delivery, onboarding, auth mail, and ops alerts (`OPS_ALERT_EMAIL`) | `src/workers/delivery.py:22`, `src/workers/ops_alerts.py:5-14` |
| Logging | `setup_logger` with a global redaction filter (auth headers, password/api_key/token assignments, JWTs, `sk_`, cookies, https basic-auth, emails, phones). A uvicorn access-log filter strips `token=` and the Tracerfy path secret | `src/utils/logger.py:32-53`, `main.py:114-162` |
| Monitoring | `/health` (liveness, static body) and `/ready` (DB round-trip, coarse body). `monitoring/` and `infra/grafana`, `infra/alertmanager` hold Prometheus/Grafana config, used only by the dormant `docker-compose.prod.yml`. Ops alerts go out by email | `main.py:177-221`, `docker-compose.prod.yml:113-164` |
| Deploy platform | Railway (Dockerfile builder, `start.sh` routes by `RAILWAY_SERVICE_NAME`: api, worker, beat). Vercel for the FE | `railway.toml:1-8`, `start.sh:53-99` |
| CI/CD | BE `ci-cd.yml`: test, pip-audit, build and push to **GHCR**, and a `deploy-production` job that runs `alembic upgrade head` with production secrets on every push to `main`. Actions is running again: the latest main runs ended 2026-09-26/27. FE `ci.yml`: lint, tsc, and an OpenAPI drift check using the `BACKEND_SCHEMA_TOKEN` PAT | `.github/workflows/ci-cd.yml:244-337`, `live:gh run list` (run 36278615909 for `786efcf0`, "Run Migrations: success") |
| DNS / CDN | Cloudflare, managed by Terraform (`infra/terraform/main.tf`, Terraform Cloud remote backend, org `bridgeleads`). The token scope is "DNS + R2 + WAF" | `infra/terraform/main.tf:9-26` |
| Inbound webhooks | Stripe (HMAC-signed) and Tracerfy (shared secret) | as above |
| CSV / Excel export | `DataExporter.export()` handles CSV, JSON and Excel. `sanitize_for_csv()` neutralizes formulas. Downloads use a 60-second JWT download token (`SECRET_KEY`) or an R2 presigned URL | `src/utils/data_exporter.py:98-214`, `src/api/middleware/security.py:457`, `src/api/download_tokens.py:21-35` |
| Scheduled delivery | Beat dispatches jobs. Delivery goes out by email (Resend, with a revocable app download link when `API_BASE_URL` is set) and by customer webhook | `src/workers/delivery.py`, `src/config/settings.py:243` |
| API access | Customer API keys (`POST /auth/api-key`, which requires the current password and a JWT session) authenticate as `Bearer bl_...`. API keys cannot pass `require_admin_mfa` | `src/api/auth.py:296-338`, `src/api/auth.py:463-467` |
| Zapier / webhook integrations | Customer job webhooks and the generic dialer webhook ("webhook/Zapier catch-hook") are HMAC-signed with a per-config secret. Delivery re-runs the SSRF check (`validate_outbound_webhook`) and uses a pinned-IP session. PhoneBurner is a host-pinned connector with a customer OAuth token. Those secrets are stored in `deliver` JSON (see AS-8) | `src/workers/webhook_delivery.py:53-55`, `src/workers/webhook_delivery.py:137-345`, `src/workers/dialer_connectors/phoneburner.py:14-29`, `src/api/schemas.py:513-551` |

## Trust boundaries

Flow: Browser -> API -> Auth -> DB -> Worker/Queue -> County sources -> Enrichment -> Tracerfy -> Results ->
Export/Delivery. The enforcement point at each hop:

| Hop | Boundary | Enforcement point (server side) |
|---|---|---|
| Browser -> FE (Vercel) | Untrusted client | FE `proxy.ts` session gate (UX only, not a security control). The Auth.js cookie is encrypted with `AUTH_SECRET` (Vercel env) |
| Browser/FE -> API | Untrusted client to Cloudflare to Railway. The origin is also reachable directly (prior F-28, not re-probed here) | CORS allowlist `main.py:68-84`, `SecurityHeadersMiddleware`, rate limiting in `src/api/middleware/rate_limit.py` |
| API -> Auth | Bearer JWT or API key | `get_auth_context` `src/api/auth.py:286-338` (HS256, iss/aud pinned, API key looked up by SHA-256 hash), `require_plan`, `require_admin`, `require_admin_mfa` `src/api/auth.py:432-535` |
| Auth -> DB | Tenant boundary | Per-transaction RLS GUC (`src/db/session.py:231-247`) plus an explicit `user_id` predicate on every query. Roles: `bridgeleads_app` (API) and `bridgeleads_system` (worker). Owner/DDL only through `DATABASE_URL_MIGRATE` |
| API -> Worker/Queue | Redis broker (TLS verified). Messages carry ids only | The worker re-reads the owner from the DB. Credentials are assembled from the DB inside the worker and never enter Celery messages (`src/workers/dialer_outbox.py:186-190`) |
| Worker -> County sources | **Untrusted input** (HTML/PDF from third parties) rendered in Chromium with `--no-sandbox` | `validate_scraping_target()` allowlist `src/api/middleware/security.py:206`, route interception in `base_scraper.py:456`. Renderer isolation is weak (F-07), and the same process env holds the DB DSNs and the API keys |
| Worker -> Enrichment (county GIS/assessor) | Untrusted responses | `src/utils/safe_http.py:100-140` (`safe_get`, bounded reads, SSRF checks) |
| Worker -> Tracerfy | Paid third party, spend boundary | Server-side `TRACERFY_API_TOKEN`. Inbound webhook checked by constant-time shared secret (`src/api/routes/webhooks.py:51-73`). No per-account spend cap (prior F-12, not in this leaf) |
| Tracerfy/Stripe -> API (inbound) | External caller | Stripe: HMAC `construct_event` (`billing.py:1756`). Tracerfy: shared secret (header, or URL path on the legacy route, AS-9) |
| Results -> Export | PII at rest | Fernet field encryption on PII columns (`src/db/models.py:86-103`, `:852-862`, `:1240-1289`), `sanitize_for_csv()` on export |
| Export -> Delivery | Customer-chosen destinations | R2 presigned URLs or 60 s download tokens. `validate_outbound_webhook` plus a pinned-IP session for webhooks. PhoneBurner is host-pinned |
| Build/CI -> Artifacts | Supply chain and publication | `.dockerignore` (denylist, `infra/` added at `.dockerignore:46`). **The GHCR package is publicly pullable (AS-1, AS-2).** Repo-level prod DSN secret (AS-4) |

## Check 1: database credentials

Where DB credentials live (names only):
- Runtime: `DATABASE_URL`, `DATABASE_URL_SYNC` and `DATABASE_URL_MIGRATE` are read only from the environment through
  pydantic settings (`src/config/settings.py:30-32`), `alembic/env.py:22` and `scripts/migrate.py:101`. No
  hardcoded DSN exists in `src/`, `alembic/` or `main.py`. I found no code path that logs a DSN
  (`cmd:grep (log|print)(...DATABASE_URL|REDIS_URL|dsn...)` returned no match).
- Compose: `docker-compose.yml:10` and `docker-compose.prod.yml:7-9,164` use `${VAR}` interpolation only.
- `.env.example`: placeholders only (`changeme`, host `localhost`).
- CI: `.github/workflows/ci-cd.yml:85,113-116` use the CI service container's `testpassword@localhost`. The
  production job reads `${{ secrets.DATABASE_URL_SYNC }}` at `ci-cd.yml:324`.
- Docs, scripts and tests: every DSN with a password points at `localhost`, `127.0.0.1`, or the TEST-NET address
  `203.0.113.1` (`tests/test_readiness.py:28`), or uses a `${...}`/`{host}` template. Tree and history scans found no
  exception (Check 3, Check 13).
- Service-role keys: no Supabase `service_role` JWT, `sb_secret_`, or `SUPABASE_*KEY` usage in either repo. The
  mentions are in the generic prompt pack under `docs/security/` and in SQL comments.
- Frontend: no DB variable, no Supabase client, and no DSN in the FE repo (`cmd:git grep -iE
  'DATABASE_URL|postgres|supabase|service_role'` matched prose only). A live scan of `/login` and 17 of its 20
  production JS chunks (874 KB) found no key, DSN, `AUTH_SECRET`, Railway host or Supabase host. **DB credentials
  are server-only.** The negative control is the same scanner, which caught the token blob in history (Check 13).

GitHub secrets (names only, `live:gh secret list` and `live:gh api .../environments`):

| Repo | Scope | Names |
|---|---|---|
| web-scrapper-automation | repo-level | `DATABASE_URL_SYNC` (2026-03-18), `RAILWAY_TOKEN_PRODUCTION` (2026-03-18, referenced by no workflow), `RAILWAY_TOKEN_STAGING` |
| web-scrapper-automation | env `production` (0 protection rules, no branch policy, admins can bypass) | `BLIND_INDEX_KEY`, `FIELD_ENCRYPTION_KEY`, `SECRET_KEY` |
| web-scrapper-automation | env `bridgeleads-production / production` (stray, 0 rules) | none |
| web-scrapper-automation | variables, dependabot secrets, deploy keys, hooks | none, none, 0, none |
| bridgeleads-web | repo-level | `BACKEND_SCHEMA_TOKEN` (2026-09-13) |
| bridgeleads-web | envs `Preview`, `Production` | no secrets |

`ci-cd.yml:311-337` (`deploy-production`) passes the repo-level prod `DATABASE_URL_SYNC` together with
`BLIND_INDEX_KEY` and `FIELD_ENCRYPTION_KEY` into a step that runs `pip install -r requirements.txt` (unhashed
transitive dependencies) and then a bare `alembic upgrade head`. This job **ran successfully on 2026-09-26** for
`786efcf0` (run 36278615909). Audit #2 called it "dormant while Actions is billing-blocked", which is no longer
true. See AS-4 and AS-5.

## Check 2: public .env and repo files on production hosts

Live, one GET each, 1.2 s apart (`live:` results):

| Host | 12 paths (`/.env`, `/.env.local`, `/.env.production`, `/.env.development`, `/.env.backup`, `/.env.old`, `/.env.example`, `/.git/config`, `/.git/HEAD`, `/config.json`, `/.DS_Store`, `/backup.zip`) |
|---|---|
| `api.bridgeleads.io` | all 12 returned **404**, `application/json`, 22 bytes (the FastAPI not-found body), `server: cloudflare`. No env-like or git-like content |
| `bridgeleads.io` | all 12 returned **307** to `/login?callbackUrl=...`, 15 bytes, `server: Vercel` (auth gate). No file body served |
| `app.bridgeleads.io` | all 12 returned **307** to `https://bridgeleads.io/login?...`, 15 bytes |

The Vercel 307 is the `proxy.ts` gate, so it hides whether a file would be served to a signed-in user. Negative
control: what Vercel can serve statically is the build output plus `public/`. At `origin/master`, `public/` holds 5
SVGs only. No `.env*`, `.pem`, `config.json`, `.zip` or `.DS_Store` is tracked in the FE (`cmd:git ls-files`).
Next.js does not serve the project root.

Ignore files:
- FE `.gitignore` covers `.env*`, `*.pem`, `.vercel`, `.superpowers/`, `graphify-out/`. There is no `.vercelignore`.
  Vercel builds from git, so ignored files never reach it. `next.config.ts` has no `productionBrowserSourceMaps`
  and no `env:` block, so no server env var is inlined. CSP `connect-src` is limited to `self`,
  `*.bridgeleads.io` and `*.stripe.com`.
- BE `.gitignore` covers `.env`, `.env.*` (except the example), `infra/terraform/terraform.tfvars`
  (`.gitignore:60`), `scripts/.e2e*.json`, `.rls-cutover-secrets` (`:146`) and `scripts/audit_out*/`
  (`:165-167`). It does not cover `.claude/memory.db`, which sits untracked and unignored in the OneDrive checkout.
- BE `.dockerignore` now excludes `infra/` (`.dockerignore:46`, commit `150421e5`), plus `.env*`, `tests`, `docs`,
  `tasks` and `.claude`. It is still a denylist, and `scripts/` (about 200 diag and ops scripts) ships in the image.
  Because Railway and GHCR build from git, only tracked files enter an image. The public GHCR images are covered in
  AS-1 and AS-2.

## Check 3: hardcoded secrets in the current tree (both repos)

Tools:
1. `detect-secrets` 1.5.0 in my own uv venv (`cmd:detect-secrets scan --all-files`). BE: 63 hits. FE: 1 hit.
2. My own scanner (`secscan.py`, scratch). It strips U+FEFF anywhere and normalizes CRLF/CR before matching. Vendor
   rules: Stripe `sk_/rk_/whsec_/pk_live_`, AWS `AKIA/ASIA`, GitHub `gh?_`/`github_pat_`, Resend `re_x_y`, JWT
   `eyJ.eyJ.sig`, PEM private keys, Slack, Anthropic, OpenAI, Google `AIza`, Supabase `sb_secret_/sbp_`, Cloudflare
   origin-CA, Vercel and npm tokens, DSNs with passwords (postgres, redis, amqp, mysql, mongodb), and https basic
   auth. It also matches case-insensitive assignments to any name containing
   secret/token/password/api_key/access_key/private_key/credential/signing/encryption/blind_index/dsn/`_key`, with
   entropy and placeholder classification.
3. A generic high-entropy pass: tokens of 30 to 90 characters, mixed case plus digits, entropy of at least 4.3.

Positive control: the same scanner, run over the object store, flags the BOM-prefixed lower-case
`cloudflare_api_token` blob (`4550e8b9`) that two earlier audits missed.

Results on tracked files (BE `786efcf0`, 1 untracked task file included; FE `e42d5d0`):

| Class | Hits | Examples |
|---|---|---|
| Real credential | **0** | none |
| Test placeholder | all remaining BE hits | `testpassword@localhost/127.0.0.1` (CI, HANDOFF docs, `run-audit-tests.sh:15-21`), `ci-test-...` SECRET_KEY (`ci-cd.yml:118`), `sk_test_fake`/`whsec_fake`/`re_fake` (`ci-cd.yml:121-149`), `SecurePass1!`-style test passwords (`tests/test_auth.py`), fake JWTs and a fake `sk-...` in `tests/test_log_redaction.py:22-30`, `whsec_promo_...789` (`tests/test_promo_access.py:43`), RFC TOTP example (`tests/test_pii_crypto.py:121`), synthetic samples in `tests/test_no_committed_credentials.py:68-73`, and a `.env.example` DSN placeholder |
| Public by design | 4 | reCAPTCHA **site key** `6Lcv...KcT` (`src/scrapers/enrichment/pierce_atip.py:56`, `parcel.py:95`), Accela page tokens in county HTML fixtures, the example `ref` hex in `main.py:202` and `schema/openapi.json:7201` (mirrored in FE `lib/api-types.generated.ts:4984`) |
| Identifiers, not secrets | several | Stripe price, product, meter and promo ids in docs, Vercel project id `prj_...sRR` in the journal, header name `X-Tracerfy-Webhook-Secret`, `_TIMING_DUMMY_PASSWORD` constant, bcrypt `_DUMMY_HASH` |
| FE | 0 real | 1 detect-secrets hit (the `ref` example) and 5 placeholder assignments (`lib/api.ts` field names) |

I also scanned untracked, unignored files in both OneDrive checkouts (235 BE files and 2 FE files, excluding
`.env*` and the rls secrets file): 0 candidates. The checked-in guard `tests/test_no_committed_credentials.py:61-102`
(tracked-file scan, BOM-tolerant, plus a "tfvars not tracked" test) exists on `786efcf0`. CI has no dedicated
secret-scan step (AS-10).

## Check 13: full git history (both repos)

Method: blob scan of the entire object store with `git cat-file --batch-all-objects`. That covers every version of
every file ever committed, deleted files, stashes and unreachable objects. BE: 6,682 blobs. FE: 2,004 blobs. I used
the same rules as Check 3 plus the generic entropy pass. Filenames ever added were checked with `git log --all
--reflog --diff-filter=A` for `.env*`, `*.tfvars`, `*.pem`, `*.key`, `*.p12`, `id_rsa`, `tfstate`, `*.sql`,
`*.db` and `*.bak`. Every hit was mapped to commits with `git log --all --reflog --find-object`.

| Secret | Type | Path | First commit | Last commit / removal | In HEAD (`786efcf0`)? | Where it still lives | Likely valid? (repo evidence only) | Recommendation |
|---|---|---|---|---|---|---|---|---|
| `AwLQ...zyR` (40), blob `4550e8b9` | Cloudflare API token, "DNS + R2 + WAF" (`infra/terraform/main.tf:23-26`) | `infra/terraform/terraform.tfvars` (UTF-8 BOM) | `579dec50` 2026-03-17 | untracked by `150421e5` 2026-09-25 (ancestor of `786efcf0`) | **No** (`origin/main` tree clean) | tip of **226 of 231** GitHub branches (`cmd:git ls-remote --heads` then `cat-file -e`); history of `main`; **358 public GHCR images** (AS-1); every Railway image built before 2026-09-25 (not verifiable); on disk in the OneDrive checkout | **Assume valid.** The owner task "Rotate the Cloudflare API token (N-01)" is still unchecked in `tasks/todo-security-2026-09-25.md:11`, and `tasks/todo-security-remaining.md` lists it as not done | Rotate now and review the CF audit log (AS-1, AS-3) |
| `Brid...%21` (URL-encoded, 16 decoded chars), blob `a1ba0339` | Production admin password (`ad***@bridgeleads.io`) in a login URL | `scripts/audit_out_ui/run3_thurston_whatcom.log` | local stash `568f67f3` 2026-06-19 | never on a remote (`cmd:git log --remotes -- <path>` returns 0) | No | local `refs/stash`, and the file is still on disk in OneDrive | Unknown, and nothing shows a rotation (`todo-security-2026-09-25.md:12` unchecked) | Rotate the admin password (AS-6) |
| same password (hash-equal to the one above), blob `8a5d8c55` | Production admin password in a headed Playwright script (`const PASSWORD = "Brid...26!"`) | FE, a deleted local script, never committed | unreachable blob (not reachable from any ref or reflog) | never pushed | No | FE `.git` object store in OneDrive until `git gc` prunes it | same as above | **New in audit 3.** Rotate. Run `git gc --prune=now` in the FE checkout after rotating (AS-6) |
| zone id `8218...79a`, account id `8f25...127` | Cloudflare identifiers | same tfvars | `579dec50` | same | No (`.tfvars.example` has placeholders) | same | Identifiers, not credentials | none |
| Terraform state | remote-backend stub | `infra/terraform/.terraform/terraform.tfstate` | `579dec50` | removed `1f5e7caf` 2026-06-01 | No | history | No secret value present (0 hits) | none |

No other credential appears in either repo's history. There is no Stripe, AWS, GitHub, Resend, Anthropic, OpenAI,
Slack, Google, Supabase, JWT-with-role or private-key material, and no non-local DSN password. No `.env*` other
than `.env.example`, and no `*.pem`, `*.key` or `id_rsa`, was ever added to either repo. The FE history is clean
apart from the unreachable blob above. Audit #2 reported "FE clean across all blobs". The blob above is either newer
than that scan or was missed by it (audit #2 counted 1,984 FE blobs; I scanned 2,004).

**N-01 current status, as the brief asked:**
- Untracked on `main`: **yes**, since `150421e5`. `infra/` is excluded from the Docker context.
- Still in history: **yes**. It is on `main`'s history and at the **tip** of 226 of 231 GitHub branches, so a fresh
  clone of any of those branches checks the file out.
- Inside images built from `main`: **yes, and they are public.** All 358 `main-<sha>` GHCR tags built before
  `150421e5` are anonymously pullable. I confirmed the file in `main-c23523e` (layer 10: `app/infra/terraform/
  terraform.tfvars`, git-blob-sha1 `4550e8b9`, BOM present). Post-fix images (`latest` = `e5b82187`,
  `main-786efcf`, `main-e5b8218`) contain no `infra/` entry.
- Rotated: **no evidence of it in the repo**.

---

## Findings

| ID | Severity | Category | Component | Evidence | Prereqs | Impact | Remediation | Regression test | Status |
|---|---|---|---|---|---|---|---|---|---|
| AS-1 | P0 | Secret exposure (public) | GHCR package `ghcr.io/abenezer1244/web-scrapper-automation` built by `ci-cd.yml:244-289` | live:anonymous `ghcr.io/token` then `tags/list` returned 200 with 362 tags (a control on a nonexistent repo returned 403 DENIED). live:image `main-c23523e` layer 10 contains `app/infra/terraform/terraform.tfvars`, whose git-blob-sha1 equals `4550e8b9` = git:579dec50:infra/terraform/terraform.tfvars. cmd:358 of 362 tags are built from commits that are not descendants of `150421e5` | None. Anyone on the internet can `docker pull` | A Cloudflare token scoped to DNS + R2 + WAF (`infra/terraform/main.tf:23-26`) is public. If valid: DNS hijack of `bridgeleads.io` and `api.` (token and password interception, MX injection), read and write of the R2 exports bucket (every tenant's lead CSVs with PII), and WAF disable | 1) Roll the Cloudflare token now. 2) Set the GHCR package to Private (Package settings, Change visibility) and delete every version built before `150421e5`. 3) Review the Cloudflare audit log, R2 access keys, DNS and WAF changes back to the first public image. 4) Treat R2 S3 keys created with this token as exposed | CI step that fails if an anonymous `ghcr.io` token can list the package's tags. The existing `tests/test_no_committed_credentials.py` covers only the tracked tree | REPRODUCED (file retrieved anonymously and hash-matched; the token was not used) |
| AS-2 | P2 | Source-code disclosure | GHCR public package, still published on every push to `main` | live:`latest` built 2026-09-27T03:36Z from `e5b82187`. Layer 11 (`COPY . .`, `Dockerfile:62`) holds 547 files: all of `src/`, `alembic/`, `scripts/`, `main.py`, `start.sh`. `ci-cd.yml:251` grants `packages: write` | None | The private backend (auth logic, SSRF allowlists, rate-limit design, admin identity, the full scraper and ops toolkit) is public on every build. It hands attackers a white-box map, and any future committed secret goes public on the next build | Make the package private (same step as AS-1), or stop pushing to GHCR (Railway builds from git and does not use these images; only the dormant `docker-compose.prod.yml:31` references them) | Same anonymous-visibility check as AS-1 | REPRODUCED |
| AS-3 | P1 | Secret in VCS history | `infra/terraform/terraform.tfvars` (N-01 re-verification) | git:579dec50 added it. git:150421e5 untracked it on `main`. cmd:`git ls-remote --heads origin` shows 226 of 231 branch tips still carry the file. `tasks/todo-security-2026-09-25.md:11` rotation box is unchecked | Read access to the private repo, any clone (the local worktrees and the OneDrive checkout), or the FE `BACKEND_SCHEMA_TOKEN` PAT | Same token as AS-1, reachable by everyone who can read the repo. The code fix does not remove it from branch tips | Rotation (AS-1) closes it. Then delete the stale remote branches, or purge history only if the owner decides to force-push | `tests/test_no_committed_credentials.py:100` (tfvars untracked on the tested ref) | CONFIRMED (prior N-01: code side FIXED, secret still exposed and not rotated) |
| AS-4 | P2 | Secret placement / CI | `ci-cd.yml:311-337` `deploy-production`, repo-level secrets | live:`gh secret list` shows `DATABASE_URL_SYNC` and `RAILWAY_TOKEN_PRODUCTION` at repo level. `production` env has 0 protection rules and no branch policy. live:run 36278615909 (2026-09-26, `786efcf0`) "Run DB migrations: success". `ci-cd.yml:336` runs `pip install -r requirements.txt` in the step whose env holds the prod DSN plus `BLIND_INDEX_KEY` and `FIELD_ENCRYPTION_KEY` | Push access, or a compromised transitive PyPI release | A working production DSN (used for DDL migrations) and both encryption keys reach arbitrary build code on every main push. `RAILWAY_TOKEN_PRODUCTION` is an unused standing deploy token. Audit #2 called this dormant, but it is now active | Delete the `deploy-production` job (Railway already migrates), or move `DATABASE_URL_SYNC` into a protected `production` env with reviewers and a `main`-only policy, and split `pip install` into a step with no secrets. Delete `RAILWAY_TOKEN_PRODUCTION` and the stray environment | A workflow lint that fails if `secrets.*` appears in a step that runs `pip install` | CONFIRMED (prior S-3/F-14: OPEN, now active) |
| AS-5 | P2 | Deploy integrity | `ci-cd.yml:337` bare `alembic upgrade head` vs `start.sh:55,84,96` advisory-locked `scripts/migrate.py` | live:run 36278615909 ran the unlocked migration on 2026-09-26, while Railway redeployed the same push | A push to `main` that carries a migration | An unlocked runner races the locked one. Migration 101's invalid-index drop is only safe under the lock, so a race can drop an index being built CONCURRENTLY or abort the boot, and the API then refuses to start | Make the CI job call `python scripts/migrate.py`, or delete the job | A test that no workflow file contains a bare `alembic upgrade` | CONFIRMED (prior S-4: OPEN, now active) |
| AS-6 | P2 | Plaintext credential on disk | OneDrive checkouts (institution-managed) | cmd:`ls` shows `scripts/audit_out_ui/run3_thurston_whatcom.log`, `.rls-cutover-secrets`, `infra/terraform/terraform.tfvars` still on disk. git:568f67f3 (local stash) holds the admin password. cmd:FE blob `8a5d8c55` (unreachable) holds the same admin password (sha256-equal, `ad***@bridgeleads.io`) | Access to the college M365 tenant, eDiscovery, or the synced device | Production admin login (whether MFA on that account blocks reuse was not verified), production RLS role passwords, and the CF token | Rotate the admin password. Move the log, `.rls-cutover-secrets` and the tfvars into a password manager. Drop `stash@{0}` and run `git gc --prune=now` in both repos (owner decision, shared checkout). Move the working copies off OneDrive | none practical (local state) | CONFIRMED (prior S-5: OPEN, plus the new FE blob) |
| AS-7 | P2 | Least privilege | Owner/DDL DSN in runtime services | `start.sh:55,84,96` runs `scripts/migrate.py` on api, worker and beat. `scripts/migrate.py:101` prefers `DATABASE_URL_MIGRATE`. `src/scrapers/base_scraper.py:281` runs Chromium with `--no-sandbox` in the same worker | Code execution or env disclosure in any service (worker renders hostile county HTML) | A BYPASSRLS DDL credential in every process makes RLS and role separation irrelevant after one renderer escape | Run migrations in a Railway pre-deploy command or a one-shot service, and remove `DATABASE_URL_MIGRATE` from api, worker and beat | Boot check that refuses to start the worker if `DATABASE_URL_MIGRATE` is set | SUSPECTED (the code path is confirmed; Railway service env could not be read) |
| AS-8 | P3 | Secrets at rest | `scraper_configs.deliver`, `scraper_batches.deliver` | `src/db/models.py:413`, `src/db/models.py:505` are plain `JSON`. They hold `phoneburner_access_token`, `webhook_secret` and `dialer_webhook_secret` (`src/api/schemas.py:513-527`). PII columns use `EncryptedString`/`EncryptedJSON` (`src/db/models.py:86`, `:861`) | DB read access (a backup, the owner DSN from AS-4/AS-7, or Supabase dashboard access) | Customers' PhoneBurner OAuth tokens (read and write on their CRM contacts) and webhook HMAC keys are exposed in cleartext on any DB read, even though PII is encrypted | Store these keys in an `EncryptedJSON` sub-field or a separate encrypted column, with a backfill | Test that a raw SELECT of `deliver` never contains the stored token text | CONFIRMED |
| AS-9 | P3 | Secret in URL / logs | Tracerfy legacy route | `src/api/routes/webhooks.py:169` `POST /webhooks/tracerfy/{provided_secret}`. `src/config/settings.py:345` sets `TRACERFY_LEGACY_PATH_ENABLED = True` by default. The access-log scrubber only covers `uvicorn.access` (`main.py:158`) | Read access to Cloudflare, Railway edge or app logs | The Tracerfy webhook secret sits in edge logs. With it, a caller can inject forged completion callbacks that start an ingest | Move Tracerfy to the header, rotate `TRACERFY_WEBHOOK_SECRET`, set the flag false, then delete the route | Test that the legacy route 410s by default | CONFIRMED in code (prod flag value not visible; prior F-22: OPEN) |
| AS-10 | P3 | Supply chain / CI | `ci-cd.yml`, FE `ci.yml` | `ci-cd.yml:198` `codecov/codecov-action@v4` and every action tag-pinned rather than SHA-pinned. `ci-cd.yml:303` `npm install -g @railway/cli` unpinned. FE `BACKEND_SCHEMA_TOKEN` is repo-level with Contents read on the whole BE repo (which exposes AS-3). No gitleaks or equivalent CI step | Compromise of an action tag or an npm package | Code execution in jobs that hold registry write or Railway tokens | Pin actions by SHA, pin `@railway/cli@<ver>`, add gitleaks with generic rules, and scope the PAT to an environment | CI job that fails on an unpinned `uses:` | CONFIRMED (prior S-8: OPEN) |
| AS-11 | P3 | Misleading security docs | RLS comments | `src/config/settings.py:232` ("production role currently has BYPASSRLS"), `src/db/session.py:221` ("the prod role has BYPASSRLS") | none | Stale text has misled reviewers before (project landmine), which can cause a wrong risk call | Rewrite both comments to describe the post-cutover roles | none | CONFIRMED (prior S-6: OPEN) |
| AS-12 | P3 | Fail-closed config | Blind-index key | `main.py:49-50` and `src/workers/__init__.py:175` prime only `_instance()`. `_blind_index_secret()` (`src/utils/crypto.py:227`) is checked lazily | Missing `BLIND_INDEX_KEY` in an env | Boot succeeds and login/register fail with 500s instead of the service refusing to start | Call `_blind_index_secret()` at boot next to `_instance()` | Boot test with the key unset in production mode | CONFIRMED (prior S-7: OPEN) |
| AS-13 | P3 | Artifact hygiene | `.dockerignore`, `.gitignore`, compose | `.dockerignore:46` now excludes `infra/` (partial fix) but remains a denylist that ships `scripts/`. `.gitignore` lacks `.claude/memory.db`. `docker-compose.yml:14,25` bind 5432 and 6379 on all interfaces. `Dockerfile:1` pins by tag, not digest | A local `docker build` from the OneDrive checkout, a LAN peer, or a stray `git add -A` | Local secrets or DB files could enter an image or commit. Dev DB and Redis reachable from the LAN | Switch `.dockerignore` to an allowlist, ignore `*.db` and memory files, bind dev ports to 127.0.0.1, pin the base image digest | none | CONFIRMED (prior S-9, S-10, S-12: partially fixed or OPEN) |
| AS-14 | INFO | Log redaction gap | `src/utils/logger.py:39` | Basic-auth credential redaction matches only `https?://`, not `postgres://` or `redis://` DSNs. I found no code path that logs a DSN (`cmd:grep` over `src`, `alembic`, `main.py`, `scripts/migrate.py`) | A future log line that includes a DSN | A DSN password could be logged in cleartext | Extend the regex to any `scheme://user:pass@` | Unit test with a postgres DSN | CONFIRMED |

## Not verified

- **Whether the Cloudflare token (AS-1/AS-3) or the admin password (AS-6) is still valid.** Checking either would
  mean using the credential against Cloudflare or production login, which the brief forbids. The only evidence is
  that the repo's owner-action checkboxes remain unchecked.
- **When the GHCR package became public** and whether it has been pulled. Download stats and the audit trail live in
  the GitHub package UI, and `gh` lacks the `read:packages` scope (HTTP 403). Only 1 of the 358 pre-fix images was
  opened. The others are inferred from their commit ancestry and the `.dockerignore` of that era (which excluded only
  `infra/terraform/.terraform`).
- Railway service variables (AS-7: whether `DATABASE_URL_MIGRATE` is set on api, worker and beat), Railway's
  retained deployment images, Vercel env (`AUTH_SECRET`), Terraform Cloud workspace variables and state, and the
  Cloudflare audit log. I have no access to any of these dashboards.
- The contents of `.rls-cutover-secrets`, `.env*`, the FE `.env.local` and `.env.check`, and the on-disk
  `terraform.tfvars`. These were deliberately not read. Their existence was confirmed by `ls` and `git status
  --ignored`, names only.
- Direct-to-origin (Railway) probing around Cloudflare (prior F-28) was not repeated. `/.env`-style probes went
  through Cloudflare only.
- 3 of the 20 `/login` JS chunks and all chunks of authenticated pages were not fetched, to stay inside the
  60-request budget. The FE source scan covers them statically.
- Whether the Tracerfy legacy route is enabled in production (AS-9). Only a POST would show that, and the brief
  allows GET, HEAD and OPTIONS only.
- The live DB catalog (roles, grants, RLS flags) and `external_source_health` RLS (prior S-11). That needs
  production DB access, which I did not attempt.
