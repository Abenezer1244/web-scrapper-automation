# BridgeLeads — Outstanding Work Backlog

**Single canonical checklist of everything not yet done.** Last updated 2026-06-09.
Detail lives in: the security audit (`docs/security/SECURITY_CHECKLIST_AUDIT_2026-06-08.md`),
the H3 spec (`docs/superpowers/specs/2026-06-08-h3-pii-encryption-design.md`),
`docs/BUILD_JOURNAL.md`, and the auto-memory index. Tags: 🔴 High · 🟠 Medium · 🔵 Low ·
👤 needs USER/ops action · 🧭 open decision.

---

## 1. H3 — PII-at-rest encryption (code DONE; gates + deploy left)
Two branches off `main`, unmerged. Stage 1 = contact PII + additive email_hmac. Stage 2 = User.email cutover.

- [x] 🔴 Codex-gate `security/h3-pii-encryption` (Stage-1) — CLEAN (2026-06-09). 1 P1 found + fixed (`2bbebf7`: email_hmac backfill strict-mode safe via `is_encrypted` guard), re-gate clean.
- [x] 🔴 Codex-gate `security/h3-email-cutover` (Stage-2) — RESOLVED (2026-06-09). vs `main`: 2 P2 fixed (`2bf127d`: prod-env key guard + preflight `sys.exit(1)`) + 2 P1 — #1 fixed (`ef34e88`: pass `BLIND_INDEX_KEY`/`FIELD_ENCRYPTION_KEY` to the prod migration runner), #2 (048 NOT NULL rolling deploy) is the in-isolation artifact the two-branch split exists to solve — **empirically CLEAN when reviewed `--base security/h3-pii-encryption`** (the post-Stage-1-merge diff). ⚠️ Re-gate Stage-2 `--base main` AFTER it is rebased on merged Stage-1; expect clean. Rebase will lightly conflict on `backfill_user_email_hmac.py` (both branches edited the same block) — keep the combined `is_encrypted` guard + `sys.exit(1)`.
- [ ] 👤 Provision `FIELD_ENCRYPTION_KEY` + `BLIND_INDEX_KEY` in Railway (BEFORE Stage-1 deploy)
- [ ] 👤 Also add `FIELD_ENCRYPTION_KEY` + `BLIND_INDEX_KEY` as GitHub **production-environment secrets** (BEFORE Stage-2 merge) — the `deploy-production` migration job now passes them to `alembic upgrade head` so migration 048 reconciles `email_hmac` under the SAME key the app uses (else fail-closed or user lockout). Use the identical key values as Railway.
- [ ] 🔴 Merge + deploy **Stage 1**
- [ ] 👤 Run `backfill_pii_encryption.py` (contact PII) until `changed 0`
- [ ] 👤 Run `backfill_user_email_hmac.py` until `OK to deploy P5 (0 NULL, 0 collisions)`
- [ ] 🔴 Merge + deploy **Stage 2** (only after the two backfills above)
- [ ] 👤 Run `backfill_user_email_encrypt.py` until `encrypted 0`
- [ ] 👤 Run `verify_pii_encryption.py` → must print `ALL CLEAR`
- [ ] 👤 Set `PII_ENCRYPTION_STRICT=true` in Railway + redeploy
- [ ] 🟠 Encrypt/rotate `SkipTraceQueue.download_url` (signed PII CSV URL) — H3 deferred residual

## 2. Security audit checklist — remaining (`SECURITY_CHECKLIST_AUDIT_2026-06-08.md`)

- [x] 🟠 **M3** — pip-audit + CycloneDX SBOM gate added (`dependency-audit` job; `build` needs it). Bumped all 8 vulnerable deps → 26 vulns cleared, audit 0 (fastapi 0.115.6→0.136.3, cryptography→46.0.7, PyJWT→2.13.0, requests/lxml/python-multipart/python-dotenv, pytest stack→9). npm audit = frontend repo (separate). **On `security/audit-m3-m8`, Codex-CLEAN.**
- [x] 🟠 **M8** — acclaimweb PACS lookup SSRF-hardened (validate_scraping_target+HTTPS+trust_env=False+allow_redirects=False). pacs.py/skip_trace.py were already hardened (M8 note's other sites = false positives).
- [x] 🟠 **CI was broken** (bonus, found during M3) — GH Actions workflow never ran (invalid YAML at the f-string `OK:` step → 0 jobs, "workflow file issue"). Fixed (block scalar + pinned ruff==0.15.6). Resurrecting it surfaced 80 ruff errors → all resolved (incl. a real bug: `select` NameError in `tasks.py` since_last_run). **⚠️ NOT merged: open PR for CI to validate the big dep bump first.**
- [ ] 🟠 **M6** — alerting/escalation (Sentry / email / Slack) on watchdog+canary failures (currently log-only)
- [ ] 🟠 **M7** — durable DB audit trail (login attempts, scraper-config changes) — `audit_log()` is file-only
- [ ] 🟠 **M4** — documented edge DDoS rate-limit rules (Cloudflare WAF) + distributed-limiter resilience
- [ ] 🟠 **M5** — document + restrict DB/Redis IP allowlisting (infra posture)
- [ ] 🔴 **H1** — RLS enforcement (`RLS_ENFORCE=True`) — **DO LAST**, prod-boot landmine. Needs `users` self-row policy + app grants on `mfa_backup_codes` + `mfa_break_glass_codes` (tracked in `provision_rls_roles.sql`)

## 3. Open security/privacy DECISIONS (need a call before coding)

- [ ] 🧭🔴 **`SkipTraceCache` is global (no `user_id`)** → cross-tenant PII reuse by address. Decide: per-tenant cache key, or accept. (memory: `project_dedup_enrichment_reuse_2026_06_06`)
- [ ] 🧭🟠 **DNC compliance** — no DNC feed (`phone_dnc_flag` always NULL); dialer push uses "not-known-DNC". Decide source/posture. (memory: `project_lead_targeting_milestone`)

## 4. Pending USER / ops actions

- [ ] 👤🔴 Rotate live `admin@bridgeleads.io` password + set Railway `BRIDGELEADS_ADMIN_PASSWORD`
- [ ] 👤🟠 Verify Redis `CERT_REQUIRED` on Railway (the `REDIS_SSL_CERT_REQS=none` escape from M1)
- [ ] 👤🟠 Tracerfy: add credits + re-scrape ~334 already-`errored` leads; migrate Tracerfy auth → header + rotate secret

## 5. Unmerged / unfinished features (non-security)

- [ ] 🔵 **Phase 5 dialer** — built but UNMERGED on `feature/phase5-dialer` (generic webhook push). Decide merge + all-phase frontend UI + offline backfills + optional native connectors
- [ ] 🔵 **Multi-contact segments** — segments are primary phone/email only; surfacing all 3 contacts was an optional follow-up

## 6. Tech debt

- [ ] 🔵 `src/scrapers/templates/king_wa_probate.py:~698` — F821 `submit_btn` dead ref (captcha-retry, masked by try/except)
- [ ] 🔵 Legacy `scripts/` lint debt (E402/F401) — not CI-gated (CI lints only `src/`+`tests/`), but present
