# Backlog Sweep — Session 2026-06-15

(Prior thread "NTS PDF crawlers + Snoho pre_foreclosure scraper" is DONE — captured in memory
`project_record_type_lead_quality_2026_06_12` and `docs/BUILD_JOURNAL.md`.)

Working the remaining `tasks/BACKLOG.md` items **1-by-1**, Codex consult-before / review-after, orchestrated agents.
**Skipped per user:** admin-password rotation (§4 item 2) and Tracerfy credits (§4 item 4).

## Findings from recon (changes the plan)
- **Tech debt (§6) is STALE:** F821 `submit_btn` no longer exists (ruff clean on `king_wa_probate.py`);
  `batch_recovery_sweep` already sets `completed_at` (`scheduler_helpers/batch.py:257`); `scripts/` E402 are
  intentional `# noqa` (sys.path inserts). → backlog cleanup only.
- **Redis CERT (§4 item 5):** already `CERT_REQUIRED` by default everywhere via `settings.redis_kwargs()`.
  → ops verify + optional prod-boot warning if set to `none`.
- **R2 presign (§4 item 6):** `_delivery_download_url` (`tasks_helpers/status.py:27-32`) silently falls back to
  the broken presign path if `API_BASE_URL` unset on the worker. → add a prod guard.

## Plan (sequential)

- [ ] **A. `.rls-cutover-secrets` (§4 item 1)** — verify present + gitignored (DONE: both true). Give user exact
      move-to-pw-manager + delete steps. *USER action; nothing to code.*
- [ ] **B. R2 / `API_BASE_URL` hardening (§4 item 6)** — Codex consult → make `API_BASE_URL` required worker
      config in prod (no silent fallback to broken presign). Implement + Codex review. *CODE.*
- [ ] **C. Redis CERT_REQUIRED (§4 item 5)** — ops verification step + prod-boot warning if `REDIS_SSL_CERT_REQS=none`. *Small CODE + ops.*
- [ ] **D. M4 + M5 security docs (§2)** — write `docs/security/M4-edge-ddos-rate-limit.md` (Cloudflare WAF) +
      `docs/security/M5-db-redis-network-posture.md` (IP allowlisting). Orchestrate research agents. Codex review. *DOCS.*
- [ ] **E. Tax-filter UI branch (§5)** — review `bridgeleads-web feature/tax-filter-columns-label`, then merge to
      master (⚠️ master auto-deploys Vercel — confirm before merge). *FRONTEND merge.*
- [ ] **F. Phase 5 dialer (§5)** — surface merge decision to user. *DECISION.*
- [ ] **G. Backlog cleanup** — mark stale §6 items done, check off completed items, update `docs/BUILD_JOURNAL.md`.

## Review
_(filled at end)_
