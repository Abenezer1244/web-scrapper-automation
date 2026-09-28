# Audit #5 Codex independent review

### CX5-01 - P1 - CONFIRMED

Location: `src/config/settings.py:187-194`; `src/api/config_eligibility.py:225-233`; `src/api/entitlements.py:495-519`

Exploit scenario: `ENTITLEMENT_ENFORCEMENT` defaults to `False`, including when `ENVIRONMENT` defaults to production. County and record-type violations are then logged but allowed, permitting downgraded users to continue billable runs.

Fix: Fail closed in production: require an explicit production setting or default enforcement to `True`.

### CX5-02 - P2 - CONFIRMED

Location: `src/utils/safe_http.py:122-134,166-180`; `src/workers/dialer_outbox.py:40-41,242-261`

Exploit scenario: URLs are validated/resolved before being fetched through a separate Requests session. DNS rebinding or parser differentials can redirect outbound PII or credentials. Generic webhook delivery uses `pinned_session`, but dialer delivery does not.

Fix: Route every validated egress path through the pinned transport and validate the exact connected address.

### CX5-03 - P1 - CONFIRMED

Location: `src/scrapers/base_scraper.py:414-460`

Exploit scenario: Any unexpected validation exception returns `True`, allowing Playwright navigation to continue. An SSRF guard failure therefore becomes an allow decision.

Fix: Fail closed on all validation exceptions; permit only explicitly handled benign schemes.

### CX5-04 - P2 - CONFIRMED

Location: `src/api/routes/webhooks.py:154-217`; `src/config/settings.py:345`

Exploit scenario: The preferred header-authenticated endpoint is improved, but the legacy secret-in-URL endpoint remains enabled by default. URL secrets can enter access logs or referrers. Worker locking makes duplicate queue processing idempotent, but ingress replay/authentication exposure remains.

Fix: Disable the legacy route, rotate the secret, and require freshness/signature protection where supported.

### CX5-05 - P2 - CONFIRMED

Location: `src/api/routes/segments.py:661,709,807,835`; `src/api/routes/jobs.py:449,560`; `src/api/routes/batches.py:975,1033,1073`

Exploit scenario: JSON preview/results/batch-lead endpoints decrypt PII under the general rate-limit zone, while only exports use the export zone. Repeated paginated requests can therefore extract substantial PII at the less restrictive rate.

Fix: Place all PII-bearing JSON views in the export-sensitive zone and enforce per-response/decryption limits.

### CX5-06 - P2 - CONFIRMED

Location: `src/workers/tasks.py:1784-1797,1816-1825`

Exploit scenario: The reservation timestamp is captured before the job-row CAS and user-row lock. Lock contention crossing an entitlement-window boundary can classify the reservation against the wrong quota window.

Fix: Capture the timestamp after acquiring the reservation lock, or derive it from the locking statement’s `clock_timestamp()`.

The delta review found no additional confirmed IDOR/job-ownership, admin-authz, raw-SQL injection, migration/grant, production-secret, or frontend XSS/CSRF issue. React renders backend messages as text; migration 105 uses fixed identifiers and bound values.

## Open queue

| ID | Status | Evidence |
|---|---|---|
| S3-08 | CHANGED | `src/workers/webhook_delivery.py:137,372-379` now uses `pinned_session`, but `src/utils/safe_http.py:122-134` and `src/workers/dialer_outbox.py:40-41,255-261` still use non-pinned Requests transport after validation. |
| S3-14 | PRESENT | `src/scrapers/base_scraper.py:456-460` returns `True` on unexpected validation exceptions. |
| S3-15 / S3-16 | CHANGED | Header auth is constant-time at `src/api/routes/webhooks.py:154-166`; worker idempotency/locking is at `src/workers/tracerfy_ingest.py:571-601`. Legacy URL-secret auth remains enabled by default at `src/api/routes/webhooks.py:169-217`. |
| S4-07 | PRESENT | General-zone limits remain at segment, job-result, and batch JSON endpoints listed above, while PII decryption occurs in those paths. |
| S4-02 | FIXED | `src/db/models.py:817-844` makes recently cancelled jobs hold slots for 300 seconds; `src/api/config_eligibility.py:144-154` applies that predicate. |
| S4-06 | PRESENT | `src/workers/tasks.py:1784-1785` reads `clock_timestamp()` before the job reservation CAS at `1791-1797` and user lock at `1816-1825`. |

DONE
