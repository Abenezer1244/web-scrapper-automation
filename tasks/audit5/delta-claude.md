# Audit #5 delta review (Claude)

Range: backend `ee601b55..29afc82e` (20 commits: #373 local-env, #375 run eligibility,
#376 dispatcher interval, #379 migration 105, docs). Frontend `6030491..54bc200`
(bridgeleads-web #165, #166, #167). Reviewed by reading every changed source file
end to end; no production traffic, no production data touched.

## Coverage (every changed source file)

| File | What changed | Security reading | Result |
|---|---|---|---|
| src/api/config_eligibility.py | NEW. Per-scraper run eligibility, one evaluator for POST /jobs and GET /scrapers | Every Job and ScraperConfig query filters `user_id == user.id`; entitlement slot math over the caller's own active configs; connectors are global reference data (not tenant data). Codes and messages carry only the caller's own job id. | clean; see D5-01 |
| src/api/routes/jobs.py | `enqueue_scrape_job` gates now come from `config_run_eligibility` | Same order as before (run slot 409, entitlement 402, AI 402, account 402). The pre-check is a read, but the `uq_jobs_one_active_per_config` IntegrityError handler still closes the race. Config lookup stays user-scoped in the caller. | clean |
| src/api/routes/scrapers.py | `list_scrapers` and `get_scraper` attach `run_eligibility` | Both queries still filter `ScraperConfig.user_id == current_user.id`; eligibility is computed for the caller only. | clean |
| src/api/schemas.py | `ConfigRunEligibilityResponse`; `run_eligibility` field | Exposes can_run, code, message, resumes_at, own job_id, violation_code. No internal state, no other tenant. The `_consistent` validator cannot fire on reachable states (checked against `quota.run_eligibility`: only over_limit carries resumes_at). | clean |
| src/scrapers/registry.py | `pick_connector`, deterministic `(created_at, id)` order | Reference-data selection only; module allowlist unchanged. | clean |
| src/workers/scheduler.py | beat interval from `SKIP_TRACE_DISPATCH_INTERVAL_SECONDS` | Validated 60..599 at boot, so no tight loop against Tracerfy's limit. | clean |
| src/config/settings.py | `SKIP_TRACE_DISPATCH_INTERVAL_SECONDS` + validator | No secret, bounded. | clean |
| src/db/models.py | index declaration for migration 105 | Schema metadata only. | clean |
| src/db_safety.py | NEW. Test-database classifier shared by pytest guard and Alembic | Refuses DSN query redirects (host, hostaddr, dbname, service), hostless/portless DSNs, and libpq ambient redirects. Error strings never include credentials. Hardening. | clean |
| alembic/env.py | `load_dotenv()` removed; refuses non-test targets under ENVIRONMENT=test | Removes the path that let a bare `alembic` run hit production from a checkout `.env` (the prod-wipe class). Handed connection rendered with `hide_password=True`. | clean (hardening) |
| alembic/versions/105_pending_skip_trace_account_spent.py | CONCURRENTLY index with lock/statement timeouts | f-strings interpolate module constants only; no GRANT/REVOKE; aborts rather than drops on a name collision. | clean |
| docker-compose.yml | reads `.env.local` never `.env`; pins DB/Redis to local containers; paid switches off; ports bound to 127.0.0.1; mounts only src and main.py | Committed passwords are documented throwaway local values on loopback-only ports. `.env.local` is ignored by `.gitignore:33` and `.dockerignore:30`. | clean (hardening) |
| app/(dashboard)/scrapers/page.tsx (FE) | Run now reads `run_eligibility`; 409 opens the running job | Messages rendered as React text (escaped); no `dangerouslySetInnerHTML`; navigation target is the caller's own job UUID from a user-scoped 409. Disabling Run now is presentation only; POST /jobs enforces the same evaluator server-side. | clean |
| components/settings/BillingTab.tsx (FE) | plan label from `/billing/usage` | Display only; the server stays authoritative. | clean |
| lib/api.ts (FE) | `runInFlightJobId`, `job_id` in structured errors | Type-checked extraction; no sink. | clean |
| lib/types.ts, lib/api-types.generated.ts (FE) | types | none | clean |

Not read: `.env.example` (2-line change). A permission rule blocks reading it from this session, so its content is UNVERIFIED here. It is a template shipped in the image (`.dockerignore:31`), so the owner should confirm the two new lines are placeholders.

## Findings

| ID | Sev | Category | Location | Evidence | Status |
|---|---|---|---|---|---|
| D5-01 | P3 | Plan entitlement | src/workers/scheduler_helpers/dispatch.py:126,379; src/workers/batch_tasks.py:147; src/api/routes/batches.py:274 | `AI_JOB_LIMITS` is read only in `config_eligibility.py` (POST /jobs, GET /scrapers). Scheduled dispatch and batch fan-out call `quota_block_reason` (account rule) but never the AI monthly cap. The old `enqueue_scrape_job` docstring claimed "manual + scheduled runs"; #375 corrected the docstring, the gap itself predates the delta. Impact is bounded: "ai" mode is recorder-template detection (registry.py:115-141), not a paid LLM call, and the record quota still applies on every path. | CONFIRMED (pre-existing) |

Notes (not findings):
- AI usage is now classified by the CURRENT connector a job would resolve to, so if an operator flips a county's connector mode, that month's history is reclassified. Operator-controlled; no tenant can influence it.
- Related existing item S3-25 (AI limit count-then-insert race) is unchanged.

## Open queue re-confirmed at 29afc82e

| ID | Status | Evidence |
|---|---|---|
| S3-08 | PRESENT | `src/api/middleware/security.py:234,298,388` still validate with `urllib.parse.urlparse`; egress not routed through `pinned_session` |
| S3-14 | PRESENT | `src/scrapers/base_scraper.py:458-460` `except Exception: ... return True` (guard fails open) |
| S3-15 | PRESENT | `src/workers/tracerfy_ingest.py:440` accepts `http`; host check allows any `tracerfy.*.digitaloceanspaces.com` |
| S3-16 | PRESENT, mitigated | legacy route `src/api/routes/webhooks.py:169` still registered; `main.py:132` scrubs uvicorn access lines only (edge proxy still logs the path) |
| S4-07 | PRESENT | segment preview and batch lead views use `zone="general"` (`src/api/routes/segments.py`) |
| S3-03 / S4-01 | FIXED on PR #374, NOT merged | |
| S4-03 | FIXED on PR #378, NOT merged | |

Both PRs still merge into current main without conflict (`git merge-tree`, gate G2).
