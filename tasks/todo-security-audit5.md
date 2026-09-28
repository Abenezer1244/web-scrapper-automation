# Security audit #5: delta + open queue

Report: `SECURITY-AUDIT.md` (Audit #5 section at the end). Ledger: `.unlazy/secaudit5/GATES.md`.

## Phase 1 (review, no production code changed)
- [x] Worktree `C:/Users/Windows/bl-wt-secaudit5` off `29afc82e`; Codex in a detached read-only worktree
- [x] Claude delta review: 12 BE + 5 FE files (`tasks/audit5/delta-claude.md`)
- [x] Codex independent review (`tasks/audit5/delta-codex.md`)
- [x] Codex-only claims re-verified (CX5-01 rejected as live P1 via prod boolean read; S4-02 "fixed" rejected)
- [x] #374 / #378 still merge cleanly
- [ ] Owner approves fix phases (G4)

## Proposed fix phases (each: own branch, regression test first, Codex diff review, <=5 files)
- [ ] 5a P1 S3-14: Playwright guard fails closed; block service workers; intercept WebSocket to private addresses (`src/scrapers/base_scraper.py` + test)
- [ ] 5b P2 S3-08: `safe_http` and `dialer_outbox` egress through `pinned_session` (`src/utils/safe_http.py`, `src/workers/dialer_outbox.py`, tests)
- [ ] 5c P2 S4-07: PII JSON views into the `export` zone (`segments.py`, `jobs.py`, `batches.py`, test); stacks on #378
- [ ] 5d P2 S3-15: Tracerfy download https-only and exact host (`src/workers/tracerfy_ingest.py`, test)
- [ ] 5e P2/P3 S4-06 reservation clock after lock; D5-02 production-fail-closed entitlement default; D5-01 AI cap on scheduled/batch paths
- [ ] Owner-only: merge #374 then #378 (merge = deploy), Tracerfy header migration then delete legacy route (S3-16), Cloudflare sole ingress (S3-04), confirm `.env.example` lines

## Review
(filled after the fix phases)
