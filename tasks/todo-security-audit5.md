# Security audit #5: delta + open queue

Report: `SECURITY-AUDIT.md` (Audit #5 section at the end). Ledger: `.unlazy/secaudit5/GATES.md`.

## Phase 1 (review, no production code changed)
- [x] Worktree `C:/Users/Windows/bl-wt-secaudit5` off `29afc82e`; Codex in a detached read-only worktree
- [x] Claude delta review: 12 BE + 5 FE files (`tasks/audit5/delta-claude.md`)
- [x] Codex independent review (`tasks/audit5/delta-codex.md`)
- [x] Codex-only claims re-verified (CX5-01 rejected as live P1 via prod boolean read; S4-02 "fixed" rejected)
- [x] #374 / #378 still merge cleanly
- [x] Owner approves fix phases (G4): all four chosen 2026-09-28

## Proposed fix phases (each: own branch, regression test first, Codex diff review, <=5 files)
- [x] 5a S3-14 (re-rated P2): Playwright guard fails closed; block service workers; intercept WebSocket to private addresses (`src/scrapers/base_scraper.py` + test)
- [x] 5b P2 S3-08: `safe_http` and `dialer_outbox` egress through `pinned_session` (`src/utils/safe_http.py`, `src/workers/dialer_outbox.py`, tests)
- [x] 5c S4-07: no code, re-rated P3 by consensus: PII JSON views into the `export` zone (`segments.py`, `jobs.py`, `batches.py`, test); stacks on #378
- [x] 5d P2 S3-15: Tracerfy download https-only and exact host (`src/workers/tracerfy_ingest.py`, test)
- [~] 5e: D5-02 done; S4-06 and D5-01 deferred with reasons (see report). Was: S4-06 reservation clock after lock; D5-02 production-fail-closed entitlement default; D5-01 AI cap on scheduled/batch paths
- [ ] Owner-only: merge #374 then #378 (merge = deploy), Tracerfy header migration then delete legacy route (S3-16), Cloudflare sole ingress (S3-04), confirm `.env.example` lines

- [x] 5f D5-03 egress proxy (owner chose 'design an egress proxy'), behind SCRAPER_EGRESS_PROXY_ENABLED (default OFF)

## Review
- Delta: clean in both reviews; 2 new P3 (D5-01, D5-02), 1 new residual (D5-03, P1 by Codex's rating). #380/#382 (landed mid-audit) reviewed: clean.
- Fixed on branches (not pushed): S3-14 (5a), S3-08 (5b), S3-15 (5d), D5-02 (5e), D5-03 (5f).
- Re-rated: S3-14 back to P2 (DNS failure already fails closed), S4-07 to P3 (measured, consensus).
- Deferred with reasons: S4-06 (needs the reservation extracted to test it for real), D5-01 (product decision), S4-02 (not in the approved phases).
- Every regression suite failed on origin/main before its fix; every phase ended at Codex GATE: PASS.
- Full local suites and gate evidence: ledger G5-G13.
