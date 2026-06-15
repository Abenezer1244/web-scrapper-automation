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

- [x] **A. `.rls-cutover-secrets` (§4 item 1)** — verified present + gitignored. USER move/delete steps given.
- [x] **B. R2 / `API_BASE_URL` hardening (§4 item 6)** — DONE (commit `13e42eb`). Prod hard-guard +
      worker-boot error + 6 tests, ruff clean, Codex-gated (SHIP, P2 normalize applied).
- [x] **C. Redis CERT_REQUIRED (§4 item 5)** — no code gap (safe-by-default); documented in M5 §3.3 +
      verification command given to user.
- [x] **D. M4 + M5 security docs (§2)** — DONE (commit `2407374`). 2 research agents + Codex fact-check
      (4 corrections adopted).
- [ ] **E. Tax-filter UI branch (§5)** — ⏸ AWAITING USER: review + merge auto-deploys Vercel. Need confirm.
- [ ] **F. Phase 5 dialer (§5)** — ⏸ AWAITING USER decision (merge generic-webhook push + build UI, or defer).
- [x] **G. Backlog cleanup** — stale §6 items closed with evidence; B/C/D/M4/M5 checked off in BACKLOG.md.

## Review (2026-06-15)
**Done autonomously (branch `security/backlog-sweep-2026-06-15`, 3 commits):**
- **B** `13e42eb` — `_delivery_download_url()` raises in prod if `API_BASE_URL` unset (was silently minting
  broken R2/S3 presign links). Worker boot logs the misconfig. 6 tests, ruff clean. Codex: approach +
  diff both gated, verdict SHIP (1 P2 — ENVIRONMENT normalize — applied).
- **D** `2407374` — `docs/security/M4-edge-ddos-rate-limit.md` + `M5-db-redis-network-posture.md`. Research
  orchestrated via 2 parallel agents; Codex fact-checked, 4 corrections adopted (CF-IP lock must be
  network-layer; Free vs Pro+ WAF tiers; plain Bot Fight Mode can't be path-scoped; Railway IPs change on
  region move). These close the last 2 security-audit checklist items + cover Redis-CERT verification (C).
- **G** — BACKLOG §6 tech-debt was STALE: F821 `submit_btn` gone (ruff clean), batch give-up already sets
  `completed_at` (`batch.py:257`, PR #42), scripts E402 are intentional `# noqa` (won't-fix).

**Skipped per user:** admin-pw rotation, Tracerfy credits.
**Needs user:** A (move secrets file), C (verify Railway var), E (merge → Vercel deploy), F (dialer decision).
