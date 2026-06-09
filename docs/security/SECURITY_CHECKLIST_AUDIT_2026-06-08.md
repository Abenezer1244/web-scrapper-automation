# BridgeLeads — 24-Item Backend Security Checklist Audit

**Date:** 2026-06-08
**Scope:** Full stack — FastAPI API + Celery workers + Playwright scrapers (`web-scrapper-automation`) and Next.js frontend (`bridgeleads-web`).
**Method:** Two independent reviewers — Claude (6 parallel evidence-gathering agents) + Codex (independent pass). Findings cross-checked per `.claude/rules/codex-collaboration.md` (both-flag → higher severity; disagreement → verify in code, Codex wins if docs silent). Every disputed finding re-verified against source.
**Checklist source:** "Backend Security Checklist" reel (6 categories × 4 controls).

---

## Scorecard

| # | Control | Verdict | Severity | Claude | Codex |
|---|---------|---------|----------|--------|-------|
| **IDENTITY & ACCESS** |
| 1 | Authentication (JWT/sessions/cookies) | ✅ COVERED | Low | COVERED | COVERED |
| 2 | Authorization (roles + multi-tenant) | ⚠️ PARTIAL | **HIGH** | COVERED* | PARTIAL |
| 3 | OAuth 2.0 / SSO | ➖ N/A | Info | N/A | MISSING |
| 4 | Multi-factor Authentication | ❌ MISSING | **HIGH** | MISSING | MISSING |
| **DATA PROTECTION** |
| 5 | Password Hashing | ✅ COVERED | — | COVERED | COVERED |
| 6 | Encryption at Rest | ⚠️ PARTIAL | **HIGH** | PARTIAL | PARTIAL |
| 7 | Encryption in Transit | ⚠️ PARTIAL | Medium | COVERED | PARTIAL |
| 8 | Sensitive Data Masking | ⚠️ PARTIAL | Medium | COVERED* | PARTIAL |
| **INPUT & OUTPUT** |
| 9 | Input Validation & Sanitization | ✅ COVERED | — | COVERED | COVERED |
| 10 | SQL Injection Prevention | ✅ COVERED | — | COVERED | COVERED |
| 11 | XSS Prevention | ✅ COVERED | Low | COVERED | PARTIAL |
| 12 | File Upload Validation | ➖ N/A | Low | PARTIAL | N/A |
| **API SECURITY** |
| 13 | Rate Limiting | ✅ COVERED | Low | COVERED | PARTIAL |
| 14 | CORS Configuration | ✅ COVERED | — | COVERED | COVERED |
| 15 | API Versioning | ⚠️ PARTIAL | Low | N/A | PARTIAL |
| 16 | Hide Internal Errors | ✅ COVERED | — | COVERED | COVERED |
| **INFRASTRUCTURE** |
| 17 | Secrets Management | ⚠️ PARTIAL | **HIGH** | CRITICAL* | PARTIAL |
| 18 | Dependency Vulnerabilities | ⚠️ PARTIAL | Medium | PARTIAL | PARTIAL |
| 19 | DDoS Protection | ⚠️ PARTIAL | Medium | PARTIAL | PARTIAL |
| 20 | Firewall & IP Whitelisting | ⚠️ PARTIAL | Medium | MISSING | MISSING |
| **MONITORING** |
| 21 | Logging | ✅ COVERED | Low | COVERED | COVERED |
| 22 | Alerting on Suspicious Activity | ⚠️ PARTIAL | Medium | PARTIAL | PARTIAL |
| 23 | Audit Trails | ⚠️ PARTIAL | Medium | PARTIAL | PARTIAL |
| 24 | SSRF Posture (scrapers) | ⚠️ PARTIAL | Medium | COVERED | PARTIAL |

`*` = reviewer's initial severity revised after cross-check/verification (see notes).

**Tally:** 9 COVERED · 11 PARTIAL · 1 MISSING · 2 N/A · 1 Info
**Severity:** 0 Critical · **4 High** · 8 Medium · rest Low/clean.

### Remediation status (updated 2026-06-08, uncommitted)
- ✅ **H4** Admin cred hygiene — FIXED (Codex-clean). User action pending: rotate live admin password.
- ✅ **M2** PII in logs — FIXED (Codex-clean).
- ✅ **M1** Redis/Celery TLS verification — FIXED (Codex-clean). Verify Redis connects on Railway deploy.
- 🔶 **H2** MFA — Phases 1–2 DONE + committed (schema/crypto `d9ccd1a`; enrollment endpoints `8150477`), Codex-clean. Remaining: P3 login challenge + frontend, P4 session hardening, P5 admin enforcement + break-glass.
- ⏳ **H3** PII encryption at rest (use `src/utils/crypto.py`) · **H1** RLS enforcement — remaining.
  - H1 note (found during H2): `users` has RLS enabled but **no self-row policy** (027) — under enforcement every `select(User)` (incl. login + MFA `FOR UPDATE`) fails. H1 cutover must add a `users` self-row policy + the `mfa_backup_codes` system grant.
- ⏳ M3 dep CVE scanning · M4 DDoS · M5 DB/Redis firewall · M6 alerting · M7 audit trail · M8 raw-requests SSRF.

> Per the Codex-collaboration gate, any High in either reviewer = NO-GO for a clean bill until resolved. Four Highs → fix list below.

---

## The 4 HIGH findings (priority fix list)

### H1 — Multi-tenant isolation depends on app-layer filter only (`RLS_ENFORCE=False`) · Control 2
- **Evidence:** `src/db/session.py:288-300` (RLS advisory, not enforcing in prod); every user route does filter by `user_id` (e.g. `src/api/routes/jobs.py:37-39`, `:261-264`). Postgres RLS policies exist but are **not enforced** because the role isn't `FORCE ROW LEVEL SECURITY` in prod.
- **Why High:** A single future query that forgets `WHERE user_id = …` becomes a cross-tenant data leak with no second net. Both reviewers flagged it; documented landmine (`incident_migration_branch_mismatch`, `project_redteam_remediation` memories: `RLS_ENFORCE=True` currently breaks prod boot).
- **Real status:** Today every query *is* filtered (verified across jobs/scrapers/auth/segments/billing). This is a resilience gap, not an active leak.
- **Fix:** Make RLS actually enforce — create a non-BYPASSRLS app DB role, `FORCE ROW LEVEL SECURITY` on tenant tables, flip `RLS_ENFORCE=True` after verifying boot. This is the unfinished HIGH-2 from the prior security remediation.

### H2 — No MFA for any account, including admin · Control 4
- **Evidence:** no `totp`/`mfa`/`webauthn`/`2fa` anywhere; `User` model has no MFA columns; `lib/auth.ts` is credentials-only.
- **Why High:** Multi-tenant SaaS with PII + billing. `admin@bridgeleads.io` is a single-factor account that can see cross-tenant admin funnels.
- **Fix:** TOTP (RFC 6238) + backup codes. Enforce for `is_admin` accounts first; offer to Business/Agency tiers.

### H3 — Owner PII (phone/email) + integration secrets stored plaintext in DB · Control 6
- **Evidence:** `src/db/models.py:187,190,194-195` (phone/email plaintext on `results`), `:430-432` (`skip_trace_cache.raw_response` full Tracerfy JSON), dialer webhook tokens / webhook secrets in plain JSON config. No `pgcrypto`/column encryption anywhere.
- **Nuance (verified):** Supabase provides **disk-level encryption at rest by default**, so the literal "encrypted if the disk is stolen" control is met by the provider. The gap is **app-layer column encryption** — defense against a leaked DB credential or a dump, not disk theft.
- **Why still High:** This is the contact PII of thousands of real property owners (the data we just proved is real). A read-only DB compromise exposes all of it in cleartext.
- **Fix:** App-layer encryption (e.g. `pgcrypto` or app-side AEAD) for `results.phone/email`, `skip_trace_cache.raw_response`, and stored integration secrets/tokens. Key via env/KMS.

### H4 — Privileged admin account uses a weak, hardcoded password · Control 17
- **Evidence:** `admin@bridgeleads.io` / `BridgeLeads2026!` hardcoded in `scripts/diag_is_pro.py:14`, `scripts/ui_county_audit.py:36`, `scripts/saas_county_audit.py:30`, `scripts/test_counties_systematic.py:22`, `scripts/e2e_chelan_probate.py:13`.
- **Verified correction:** Claude's agent called this "CRITICAL — in git history / cloned repos." **That is false.** `git log --all -S "BridgeLeads2026!"` returns nothing, and `git ls-files` shows these 5 scripts are **untracked local files**. The only git-tracked password (`scripts/e2e_all_counties.py:37`) belongs to an **ephemeral random-uuid test account** → Low.
- **Why still High:** A real, privileged production admin account has a weak guessable password (`BridgeLeads2026!`), and `scripts/` is **not gitignored** — one `git add scripts/` from committing the admin cred. Two real problems: weak admin secret + near-miss exposure path.
- **Fix:** (1) Rotate the `admin@bridgeleads.io` password to a strong random secret. (2) Move all script creds to env / `.env.test`. (3) Add `scripts/**_audit.py`, `scripts/e2e_*.py`, `scripts/diag_*.py` patterns to `.gitignore` or strip the literals. (4) Enable MFA on admin (see H2).

---

## MEDIUM findings (batch after Highs)

- **M1 — Encryption in transit: Redis TLS cert verification disabled** (`src/config/settings.py:247-249`, `ssl_cert_reqs="none"`). TLS is on (`rediss://`) but the cert isn't verified → MITM with network position. *(Codex-only; Claude marked covered. Verified real.)* Memory `feedback_redis_ssl` documents the `"none"` string is required for Upstash connection — so fix = pin Upstash CA bundle rather than disabling verification.
- **M2 — PII/email in logs.** `src/api/routes/auth.py:236` logs `email=` on login failure (enumeration); webhook payloads and delivery logs can carry recipient data. Redaction filter (`src/utils/logger.py:13-21`) covers secrets, not PII. Add email/phone redaction; drop email from `login_failure`.
- **M3 — No automated dependency CVE scanning.** Versions pinned (`requirements.txt`, `package-lock.json` present) but no `pip-audit`/`safety`/Dependabot/SBOM in CI (`.github/workflows/ci-cd.yml`). Add `pip-audit` + `npm audit` gate.
- **M4 — DDoS: app-level + Cloudflare WAF (`infra/terraform/main.tf`) but no documented edge rate-limit rules**, and Redis is a single point for distributed limiting (fails to per-process coarse limiter).
- **M5 — DB/Redis IP allowlisting not verifiable in repo** (infra-level on Railway/Upstash). App SSRF allowlist is strong; network firewall posture is undocumented. Document + restrict.
- **M6 — Alerting: detection without escalation.** Watchdog + canary (`src/workers/scheduler.py:205-372`) log failures; no Sentry/email/Slack/PagerDuty. Failing scrapers/stuck jobs go unnoticed until someone reads logs.
- **M7 — Audit trail not durable.** `audit_log()` (`src/api/middleware/security.py:521-536`) writes to file logs only — no DB audit table, no login-attempt history, no scraper-config change trail. Forensics after compromise is hard.
- **M8 — SSRF: raw `requests.Session()` bypasses central guard in scraper/enrichment paths.** `src/scrapers/templates/acclaimweb.py:906,926,942` (PACS lookup, `allow_redirects=True`) and enrichment helpers fetch without `safe_get`/`validate_scraping_target`. *(Codex-only; Claude said fully covered. Verified real.)* **Moderated to Medium** because the target URL is operator-config (hardcoded `_PACS_URLS` dict), not user-supplied — so it's a defense-in-depth inconsistency, not a user-driven SSRF. Route these through `safe_get` and set `allow_redirects=False`.

## LOW / informational

- **Control 1:** Frontend cookie flags (httpOnly/secure/sameSite) rely on next-auth v5 defaults — make explicit in `lib/auth.ts`.
- **Control 11 (XSS):** Backend returns scraped data raw in JSON (correct for an API); safety depends on frontend escaping. Verified: **no `dangerouslySetInnerHTML`** in `bridgeleads-web`. Covered, residual Low.
- **Control 12:** No user file-upload surface exists (no `UploadFile`/multipart). Tracerfy CSV is a signed-URL download, host-pinned. N/A.
- **Control 13:** Rate limiting solid (`src/api/middleware/rate_limit.py`) with per-IP, auth-zone, Redis + in-process fallback for auth/webhook/stripe; non-critical zones fail open (Low).
- **Control 15:** Routes unversioned (`/auth`, `/jobs`); fine for current maturity. Add `/v1` before any public API.
- **Control 21:** Structured logging + redaction good; logs are file-only (lost on container restart) — ship to a sink when alerting lands.

## Clean (no action)

Control 5 (bcrypt + password history + timing-safe verify), 9 (Pydantic everywhere), 10 (SQLAlchemy bound params, LIKE-wildcard escaping), 14 (CORS explicit allowlist, no wildcard, safe credentials), 16 (global handler → generic 500 + ref id, DEBUG off, no stack traces to client).

---

## Cross-model notes (where the two reviewers diverged)

| Control | Split | Resolution |
|---------|-------|------------|
| 6 Encryption at rest | Claude Critical / Codex High | **High** — provider disk encryption exists; column encryption is the real gap (verified). |
| 7 In transit | Claude COVERED / Codex PARTIAL High | **PARTIAL Medium** — Redis cert verification off is real (verified `settings.py:247`); rest is solid. |
| 17 Secrets | Claude CRITICAL / Codex Medium | **High** — Claude's "in git history" claim disproven by `git log -S`; real issue is weak admin cred + scripts/ not ignored. |
| 24 SSRF | Claude COVERED / Codex PARTIAL High | **PARTIAL Medium** — raw `requests` paths real (verified acclaimweb.py:906), but target is operator-config not user input. |
| 11 XSS | Claude COVERED / Codex PARTIAL | **COVERED Low** — frontend has no `dangerouslySetInnerHTML` (verified). |

The cross-check earned its keep: it caught a false-Critical (would've sent us chasing a git-history cleanup that doesn't exist) and two real gaps Claude alone missed (Redis cert, raw-requests SSRF).
