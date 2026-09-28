# Handoff: security audit #5 (2026-09-28), fixes shipped, what is left

**Read this first, then `SECURITY-AUDIT.md` (the "Audit #5" section is at the END of the
file) and the 2026-09-28 "Security audit #5" entry at the top of `docs/BUILD_JOURNAL.md`.**

## 1. Goal

The owner asked for the full 18-check BridgeLeads security audit (tenant isolation, IDOR,
admin authz, plan/quota bypass, Stripe, Tracerfy, scraper jobs, streams, exports, SSRF,
injection, XSS/CSRF/CORS, secrets, logs, errors, rate limits, deps, headers). Audits #3 and
#4 had run the same prompt within 30 hours, so the owner chose **audit #5 = a DELTA**
(`ee601b55..29afc82e`, plus FE bridgeleads-web #165-#167, plus #380/#382 which merged
mid-audit) **and remediation of the open queue**, under `/unlazy` (gated ledger).

## 2. Current state (verified 2026-09-28)

- **Shipped:** PR **#383** (`chore/security-audit5`) MERGED and LIVE, merge commit
  **`447f9649`** at 13:36Z. Pre-merge `quiet.py` all zeros; CI `Test` + `Dependency Audit`
  green on head `78e5ae29`; Railway api/worker/beat SUCCESS on `447f9649`; `/health` 200;
  worker and api boot logs clean. No migration.
- **main head:** `447f9649`.
- **Still OPEN from audit #4 (not touched by this session):** **#374** (S3-03/S4-01 trial
  skip-trace gate: **S3-03 is LIVE in prod until it merges**) and **#378** (S4-03 export
  zone). Both still merge cleanly into `447f9649` (`git merge-tree`, checked at handoff).
  Merge order: #374 then #378. Merge = deploy: run `quiet.py` first (see §7).

### What #383 changed (all live)

| Phase | Finding | Change | Files |
|---|---|---|---|
| 5a | S3-14 (P2) | Browser guard: `route_web_socket` guard, `service_workers="block"`, `--disable-quic` + BOTH WebRTC ip-handling switches, guard fails closed | `src/scrapers/base_scraper.py`, `tests/test_scraper_egress_guard.py` |
| 5b | S3-08 (P2) | `safe_http` + dialer outbox use `pinned_session()`; `validate_scraping_target` refuses userinfo / backslash in the authority | `src/api/middleware/security.py`, `src/utils/safe_http.py`, `src/workers/dialer_outbox.py`, `tests/test_egress_pinning_s3_08.py` |
| 5d | S3-15 (P2) | Tracerfy webhook = trigger only. URL + counts from Tracerfy's `GET /v1/api/queues/` record; not complete / unreachable = up to 5 re-checks x 120 s (one chain per queue, Redis key `tracerfy:ingest-recheck:<qid>`) then an ops alert, never 'errored'; host pinned to `tracerfy.nyc3(.cdn).digitaloceanspaces.com` over HTTPS | `src/workers/tracerfy_ingest.py`, `tests/test_tracerfy_webhook_trust_s3_15.py`, `tests/test_tracerfy_ingest.py`, `tests/test_skip_trace_already_delivered.py` |
| 5e | D5-02 (P3) | `ENTITLEMENT_ENFORCEMENT` unset + `ENVIRONMENT=production` = True (explicit false wins) | `src/config/settings.py`, `tests/test_settings.py` |
| 5f | D5-03 | In-worker SOCKS5 egress proxy for the browser, **behind `SCRAPER_EGRESS_PROXY_ENABLED` (default OFF)**. CONNECT only, ports 80/443/8080/8443, resolve once, refuse if any answer blocked, dial the checked sockaddr | `src/scrapers/egress_proxy.py`, `src/scrapers/base_scraper.py`, `src/config/settings.py`, `tests/test_scraper_egress_proxy.py` |
| docs | | Audit #5 report, delta reports, plan, journal | `SECURITY-AUDIT.md`, `tasks/audit5/delta-claude.md`, `tasks/audit5/delta-codex.md`, `tasks/todo-security-audit5.md`, `docs/BUILD_JOURNAL.md` |

Every regression suite was shown to FAIL on `origin/main` before its fix; every phase ended
at Codex GATE: PASS (all rounds recorded in the commit messages on the phase branches).

## 3. What is left (in priority order)

1. **Owner:** merge **#374** then **#378** (quiet check first). Not done because the owner's
   "push and merge" was taken to mean this session's work only; ASK before merging these.
2. **Egress proxy rollout (D5-03):** code is live but OFF. Before setting
   `SCRAPER_EGRESS_PROXY_ENABLED=true` on the worker, run every county template through it
   (staging or a quiet window, gently). Proven so far only on `atip.piercecountywa.gov`
   (plain + default mode, 200). A portal on a port outside 80/443/8080/8443 will be refused
   and logged `egress proxy refused host:port`.
3. **`.env.example`:** add `SCRAPER_EGRESS_PROXY_ENABLED=false`, and confirm the two lines
   #373 added are placeholders. A permission rule blocks Claude from reading that file:
   **the owner must do this or grant access.**
4. **S4-06 (P2, deferred):** the quota reservation reads `clock_timestamp()` BEFORE the
   user-row lock (`src/workers/tasks.py` ~1784-1839: `_reserved_at` feeds `:at` into
   `window_cte_sql`), so a lock wait across a window boundary grants against the old
   window. Fix idea: after the job CAS, `SELECT 1 FROM users ... FOR UPDATE`, then read the
   clock, then run the grant with that `:at` (keep lock order jobs -> users). **Blocker:** the
   reservation SQL is inline in `run_scrape_job`, and `tests/test_quota_reservation.py`
   tests a COPY of it (`_RESERVE_SQL`), so first extract the reservation into a function
   the task and tests both call. `jobs.reserved_at` is only read by the pre-088 month
   fallback, so the change is safe for current jobs.
5. **5b-ii (P3):** PACS (`src/scrapers/enrichment/pacs.py:137`), AcclaimWeb
   (`src/scrapers/templates/acclaimweb.py:1042,1067`) and Tracerfy submit/fetch
   (`src/scrapers/enrichment/skip_trace.py:630,702`) still validate-then-fetch on a raw
   `requests.Session` (operator-configured or fixed hosts). Move them to `pinned_session()`
   and map `is_blocked_destination` like `safe_http._get` does.
6. **D5-01 (P3, product decision):** `AI_JOB_LIMITS` is enforced only via
   `config_run_eligibility` (POST /jobs); scheduler dispatch and batch fan-out skip it. "ai"
   mode is template detection (no LLM cost). Ask the owner whether the cap matters.
7. **S4-02 (P2):** fixed 300 s run-slot cancel cooldown (`src/db/models.py` ~817-844)
   can be outlived by a cancelled worker. Not in the approved phases.
8. **S3-16 (P2, external):** Tracerfy still posts to the legacy path-secret route
   (`src/api/routes/webhooks.py:169`). Impact now bounded by 5d (body never read). Fix:
   Tracerfy sends `X-Tracerfy-Webhook-Secret`, rotate the secret, delete the route.
9. **Owner infra:** Cloudflare sole ingress (S3-04), GH `production` env protection, admin
   password rotation (S3-17), Tracerfy spend caps (`SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP`).
10. **Forward note (#382):** when Phase 1c adds an API reader for the skip-trace pause
    state (`src/utils/skip_trace_pause_state.py`), it must read only the caller's own
    `<user_id>` field plus `global`/`account_default`, never iterate the hash.

S4-07 was re-rated P3 by consensus (measured: `general` ~30k decrypted rows/min vs
`export` ~1M); no code. Optional owner choice: lower JSON `page_size` cap (public API change).

## 4. Failed attempts and dead ends (do not repeat)

- **S3-14 severity:** I first reported Codex's P1 to the owner; the premise was false
  (DNS failure already fails closed at `security.py:189-193`). Re-rated P2 with evidence.
- **5d took 7 Codex rounds.** Each fix moved the trust problem: clamp the count (could
  still lower it) -> provider record with a fallback to the body (any Tracerfy customer's
  CSV URL passes a host pin) -> retries (an outage errored a genuine batch) -> body-URL
  precheck (a JSON number raised TypeError and marked the REAL queue errored) -> a Celery
  retry found its own chain claim and gave up -> `LEAST(stored, provider)` under-billed
  adopted queues stored at 0. Final: body never read; provider value written as given.
- **D5-03 as an HTTP proxy:** Codex design gate FAILED it (request smuggling, Host
  mismatch, "TURN may bypass"). Switched to SOCKS5 and MEASURED: on Chromium 151 headed and
  headless, page loads, fetch, WebSockets and a TURN-over-TCP candidate all reached the
  SOCKS proxy, none went direct.
- **S4-07 fix as specified** (move JSON views into `export`): rejected after measuring.
- **Tests that looked green but proved nothing:** the first service-worker test passed on
  main (Playwright "block" makes `register()` RESOLVE with nothing installed; SW scripts go
  through CONTEXT routes, not page routes). `chrome://version` is invalid in headless
  shell; CDP `Browser.getBrowserCommandLine` refuses without `--enable-automation`.
- **Headed Chrome ignores `--force-webrtc-ip-handling-policy`;** it needs the plain switch
  too. Production runs HEADED under Xvfb.
- **CI caught:** my test subclasses setting `_plain_browser = True` broke
  `tests/test_pierce_cv_owner.py::test_only_the_atip_owner_lookup_opts_into_the_plain_browser`
  (owner rule: only `AtipOwnerPlainBrowser`). Tests now parametrize over that real class.
- **A test hit the REAL Tracerfy API (401)** once ingest called `fetch_queues`: stub it in
  any test that sets `TRACERFY_API_TOKEN`.
- **`codex exec` with `-s read-only`** worked for this session's reviews (inline diffs).
  `codex review` RUNS pytest on your test DB: do not use it.
- **Local full suites** were killed twice (once by the 600 s harness timeout, once by
  low-memory reaping). Each time an orphan pytest survived; kill ONLY your own (check the
  parent command line: another session's pytest runs under `timeout 595`).

## 5. Where things are on disk

| Path | Branch | State |
|---|---|---|
| `C:/Users/Windows/bl-wt-secaudit5` | `chore/security-audit5-delta` | audit docs + ledger `.unlazy/secaudit5/` (GATES.md, check scripts, testenv*.sh, suite-batch*.sh); merged into #383 |
| `C:/Users/Windows/bl-wt-secaudit5-ship` | `chore/security-audit5` | = PR #383, merged |
| `C:/Users/Windows/bl-wt-secaudit5a/-5b/-5d/-5e/-5f` | `fix/security-audit5{a,b,d,e,f}-...` | local-only phase branches (history of each review round); all contained in #383 |
| `C:/Users/Windows/bl-wt-secaudit5-codex` | detached `29afc82e` | Codex's read-only review worktree (DELTA.diff, prompts, codex5.log) |
| `C:/Users/Windows/bl-wt-secaudit5-handoff` | `docs/handoff-security-audit5` | this handoff |

Rule (memory `feedback_no_branch_delete_shared_onedrive`): never delete or force-move
branches in this shared repo; a fast-forward or a merge commit is fine.

## 6. Test environment

- Python: `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe` (matches CI pins).
- Env files: `C:/Users/Windows/bl-wt-secaudit5/.unlazy/secaudit5/testenv[-b|-d|-e|-f].sh`,
  each with its own `_test` DB (`bridgeleads_secaudit5{,b,d,e,f}_test` on local PG
  `bridgeleads:testpassword@127.0.0.1:5432`) and Redis index (5,6,7,8,9). `source` one,
  then `$PY -m pytest <files> -q -p no:cacheprovider -o addopts=""`. A fresh DB needs
  `$PY -m alembic upgrade head` first (the suite runner's `init` does it).
- Full suite: `bash .unlazy/secaudit5/suite-batch.sh <worktree> init`, then `00`..`15`, in
  the BACKGROUND (a batch can exceed 10 min), one suite per DB at a time.
- Never run bare pytest (memory `bare_pytest_uses_prod_env`).

## 7. Merging (merge = deploy)

`railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python.exe C:/Users/Windows/bl-checks/quiet.py`
from the OneDrive repo must be all zeros; branch protection is strict (up to date with
main, `Test` + `Dependency Audit` green); merge with
`gh pr merge <n> --merge --match-head-commit <full sha>`; then verify
`railway deployment list --service {api,worker,beat} --json` shows SUCCESS on the merge
commit and the boot logs are clean. Never `--admin`.

## 8. Next step

Ask the owner whether to merge #374 then #378 now (quiet check first). Then start S4-06
on a new branch off `main`: extract the reservation into one function, point
`tests/test_quota_reservation.py` at it (drop the SQL copy), write the lock-wait
boundary test (hold the user row in one connection, cross the window end, release, and
assert the grant lands in the NEW window), prove it fails, then move the clock read after
the lock. Codex consult before, Codex diff review after (see `.claude/rules/codex-collaboration.md`).
