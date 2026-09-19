# Security review: skip trace for already-delivered leads (2026-09-18)

Scope: everything shipped or staged since the 2026-09-16 pre-launch audit.
- BE #342 (merged, deployed): already-delivered leads skip-traceable; dispatcher idempotency.
- FE #156 (merged, deployed): Already delivered lookup summary + polling.
- BE `feat/skip-trace-provenance`: migration 097 `results.skip_trace_source`, `reused` count,
  tenant-gate tests, retention-clock fix.
- FE `feat/skip-trace-provenance`: "came from an earlier lookup" copy.

Pack: `docs/security/SECURITY_PROMPT_PACK.md` §14 and §15, translated per
`.claude/rules/security.md` (server action -> FastAPI route + Celery task, Zod -> Pydantic,
RLS + mandatory user_id filter).

## §14 Master Review

### Pass 1
| # | Category | Result | Evidence |
|---|---|---|---|
| 1 | Authorization | PASS | No route writes `results`. Every HTTP path to a lookup or contact move checks ownership before the query; `tests/test_skip_trace_tenant_gates.py` asserts 404 AND no side effect for POST /jobs, PATCH /scrapers/{id}, DELETE /jobs/{id}, dialer-replay; each fails with its ownership filter removed. Summary counts are inside the existing job + user filter. |
| 2 | Secrets | PASS | No secrets in either diff; local `.env.local` files deleted, never tracked. |
| 3 | Input validation | PASS | No new inputs. Worker-owned fields in a scraper update are rejected (422 `extra_forbidden`, each named); JobCreate reads two fields. New column has a CHECK. |
| 4 | Error handling | PASS | No new client-facing error paths. |
| 5 | XSS | PASS | FE renders counts as React text; zero `innerHTML` in the diff. |
| 6 | SQL injection | PASS | All values bound; f-strings interpolate only code-literal aliases (`already_delivered_sql("rn")`) or counts in log text. |
| 7 | File uploads | PASS | None added. |
| 8 | Rate limiting | PASS | No new routes; results route already rate limited. |
| 9 | CSRF / origin | PASS | No new routes. |
| 10 | PII | **FINDING (Medium)** | Copying a cached answer stamped `skip_trace_attempted_at = now` (dispatcher sweep, enqueue cache hit). That column is the 365-day PII retention clock, so a copy of an 89-day-old answer would be kept ~454 days after it was obtained. |
| 11 | Configuration | PASS | No new table. Column on RLS-enabled `results`; API role has table-level SELECT. Migration: lock_timeout, CHECK NOT VALID + VALIDATE outside the exclusive lock. |
| 12 | Dependencies | PASS | None added by this change set. (Pre-existing FE advisories: see §15 #15.) |
| 13 | Logging | PASS | New log lines carry counts only; no names, addresses or contacts. |
| 14 | Non-negotiables | PASS | user_id filter on every new query; tenant-keyed cache/coalescing; CSV path (`sanitize_for_csv`) unchanged; no silenced errors (best-effort sweeps log and roll back, rows stay queued). |

Fix: `b429458`: copies carry the cache entry's `fetched_at`; tests pin both paths.

### Pass 2 (after the fix)
All 14 categories re-walked on the full change set including `b429458`. The fix introduces
no new pattern: the only other `attempted_at = now` writes set `errored` (no PII held).
Freshness reads the same column, so a copy's age is now its data's true age. **Clean.**

### Pass 3 (after the FE copy change `cae23cc`)
Text-only change and regenerated types. **Clean.** Two consecutive clean passes on the change set (release status: see the end of §15).

## §15 Pre-Launch (delta against the 2026-09-16 full audit)
Items this change set cannot affect cite the 09-16 audit (`project_prelaunch_security_audit`),
which found no P0. Items it can affect were re-checked.

| # | Item | Result |
|---|---|---|
| 1 | Secrets in env only | PASS (diffs clean) |
| 2 | RLS on every table | PASS (no new table) |
| 3 | Input validation | PASS (see §14 #3) |
| 4 | App-layer authorization + tests | PASS (new tenant-gate tests) |
| 5 | No raw errors to clients | PASS (unchanged) |
| 6 | No dangerouslySetInnerHTML | PASS |
| 7 | Parameterized SQL | PASS |
| 8 | CORS / origin | PASS (unchanged since 09-16) |
| 9 | Auth cookies | PASS (unchanged since 09-16) |
| 10 | Security headers | **FAIL (pre-existing, RELEASE-BLOCKING)**: FE complete (HSTS, CSP, XFO); API responses carry no HSTS. Known since 09-16: `100.64/10` CGNAT is missing from `_TRUSTED_PROXY_NETWORKS`, which also disables the rate limiter. Fix order set on 09-16: close direct origin access first (Cloudflare is bypassable), then trust the proxy range. Owner step. |
| 11 | Rate limiting | **FAIL (pre-existing, RELEASE-BLOCKING)**: same root cause as #10. |
| 12 | HTTPS everywhere | PASS |
| 13 | Uploads | PASS (none) |
| 14 | Log sanitization | PASS (counts only) |
| 15 | Dependencies | **FAIL (pre-existing, RELEASE-BLOCKING, fix on a branch)**: FE prod deps had 3 critical + 3 high advisories (`next` 16.1.7: unauthenticated RCE, middleware/proxy bypass; `next-auth` beta.30). Upgrade on `chore/security-deps-2026-09-18`. BE: pip-audit green in CI. |
| 16 | Admin routes 404 | PASS (unchanged since 09-16) |
| 17 | No console.* in FE code | PASS (diff clean) |
| 18 | No test data in prod | PASS: every verification ran on local databases; nothing seeded in prod. |
| 19 | Spend caps | PASS for this change's cost driver: `SKIP_TRACE_DAILY_ROW_CAP=1000` set in prod (09-16). Other dashboards: owner (CARRYOVER, not introduced here). |
| 20 | Privacy / terms | **OPEN (pre-existing, RELEASE-BLOCKING for a public launch)**: owner item from 09-16 (placeholders in prod Terms); not changed here. |
| 21-23 | DNS, backups, error-log questions | Unchanged since 09-16; owner items (CARRYOVER, not introduced here). |
| 24 | Codex review | See Codex rounds in `tasks/todo-skip-trace-followups.md`. |
| 25 | Journal | Updated in this branch. |

Legend: RELEASE-BLOCKING = must be closed before this counts as a production security
clearance; CARRYOVER = accepted owner item, not introduced or worsened by this change set.
This review clears the CHANGE SET only; it is not a full production security clearance.

**Status: the change set is technically clean; the release is still BLOCKED by pre-existing
§15 failures** (Codex, 2026-09-18):
- #15 (critical FE advisories: `next` RCE, middleware bypass; `next-auth`) is live in
  production today. Fix branch `chore/security-deps-2026-09-18`; must be merged and verified.
- #10 / #11 (API HSTS + rate limiting) wait on the owner's origin lockdown; only then may the
  verified Railway proxy range (incl. `100.64.0.0/10` if confirmed) be trusted.

Codex round 1 on this change set: GATE FAIL on the status wording above (fixed) and an unbounded
VALIDATE in migration 097 (fixed). Rejected with evidence: "batch routes may take foreign ids"
(batch create takes counties/record types; the batches router has no id-bearing write route);
"FE types not regenerated" (committed in `cae23cc`, +7/-1, tsc clean, omitted from the diff sent).

Codex round 2 (2026-09-19): GATE PASS, no P1/P2. Its two P3s (stale follow-up checklist;
this table not separating release blockers from carryovers) were adopted: see the legend above.
