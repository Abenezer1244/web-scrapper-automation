# HANDOFF — UX 3.13 follow-ups (writer retired, C1, C2) — 2026-10-25

Session: web-scrapper-automation-80. Written at 2026-10-25 ~06:40 UTC.

## Goal

Close the owner's "still yours" items left after UX item 3 (3.1-3.19 all live):
1. the 7-day `/records` access-log watch after UX 3.13 retired `GET /scrapers/{id}/records`;
2. the production DDL follow-ups named in the 3.13 plan: retire the `county_records` writer, C1 (API role
   loses `county_records` access), C2 (drop `user_record_views`);
3. the owner-only real >= 1,000 "Complete:" headline check (UX 3.17).

## Current state (verify before trusting: fetch first)

| Item | State |
|---|---|
| Daily cache writer retired | BE #522 `1ed4ac0b` LIVE + VERIFIED 2026-10-20 |
| `/records` watch | CLOSED ON EVIDENCE (owner decision 2026-10-20): 0 API-key holders, 0 hits in all retained logs 10-13..10-20, 8/8 canary completeness, 404 since 10-11 |
| C1 migration 119 | BE #527 `0f9c2083` LIVE + VERIFIED: `bridgeleads_app` SELECT on county_records = 42501 |
| C2 migration 120 | BE #528 `881d95f2` LIVE + VERIFIED 2026-10-22: alembic 120, `user_record_views` gone, `purge_account_data` owner/secdef/ACL unchanged |
| C2 scripts | BE #530 `04ee22ec` LIVE + VERIFIED 2026-10-25 06:35 UTC |
| pillow 13.0.0 | BE #531 `973c48a4` LIVE (audit gate) |
| Docs + journal | **BE #535 OPEN, BEHIND main** (branch `docs/c1-c2-journal`, this file is on it) |

Production head: api / worker / beat all SUCCESS on `04ee22ec` (main). Peers (-0c, -49, -90) have no BE merges pending.

## Active files / worktrees

- `C:/Users/Windows/bl-wt/c1c2-docs` (branch `docs/c1-c2-journal`, PR #535): `docs/BUILD_JOURNAL.md` (2026-10-22
  entry), `docs/product/account-deletion-retention-matrix.md` (2 lines), this handoff.
- Old worktrees, all merged, safe to leave: `bl-wt/retire-daily-scrape`, `bl-wt/retire-docs`, `bl-wt/c1-county-records`,
  `bl-wt/c2-urv`, `bl-wt/c2-scripts`, `bl-wt/pillow-13` (never delete branches: shared OneDrive repo).
- Rigs (outside repo): `C:/Users/Windows/bl-testenv/c1/proof_c1.py`, `proof_c2.py` (psql-emulating provisioner).
- Venv: `C:/Users/Windows/bl-venv-w1` (py3.12, requirements + ruff 0.15.6 + pip-audit; pillow 13 installed).
- Backup: `C:/Users/Windows/bl-backups/user_record_views_2026-10-21.csv` (10 rows, sha256
  `19f129e74938182c2ed311b16bd6ed90ea058c0fe510d5961c1e7cd114982ba8`), owner-only.
- Scratchpad of session 1f8c213a holds read-only prod probes (`c2_state.py`, `c2_post.py`, `g2_baseline.py`,
  `queue_scan.py`, `records_watch.mjs`); they may be gone in a new session.

## Next steps

1. **Merge #535.** `gh pr update-branch 535`, wait for CI (Test runs the full suite, ~35 min), send MERGING NOW to
   peers -0c / -49 / -90 (ListAgents), `gh pr merge 535 --squash --match-head-commit <head via git rev-parse>`,
   check Railway api/worker/beat reach the merge commit (`railway deployment list -s <svc> -p
   e2810be2-5c1b-44f9-837c-77c9cb4450b1 -e production --limit 1 --json`), send VERIFIED.
2. **2026-10-27: G2 fingerprint re-check** (read-only, `railway run -s worker ...`):
   `SELECT count(*), md5(coalesce(string_agg(t::text, E'\n' ORDER BY t.record_hash), '')) FROM public.county_records t;`
   must still be 3,305 / `94d40dac5de3f9a0aaf05ced46a125ba` (the retired writer must not have written). Any change =
   investigate, revert #522.
3. **#529 cap check (for peer -49):** the first King owner sweep after deploy had 0 parcels; on a later tick with work,
   confirm the worker log "Owner recovery: ... N parcel(s) / ..." shows N <= 25 and no ExceedCount.
4. **Owner-only:** open a finished run with >= 1,000 new leads (e.g. the 2,190-lead run of 2026-09-16) and confirm the
   headline reads "Complete: 2,190 new leads".
5. Optional: tick the 3.13 named follow-ups in the FE plan (`docs/ux-audit/todo-item3-batches-B-E.md`, bridgeleads-web)
   as done, with these PR numbers.

## Rules that bit this run (read first)

- Coordinate every BE merge with the peer sessions: MERGING NOW before, VERIFIED after (deploy checked by Railway
  commit hash, never `/health` alone).
- Destructive production writes need the owner's explicit OK (C1 and C2 got it). Code merges: owner allowed.
- Codex PLAN before building and a diff gate after (inline prompt on stdin: `codex exec -c 'mcp_servers={}'
  --skip-git-repo-check - < prompt.txt`; never let Codex read files or run shell).
- Max 5 files per PR. Never hand-type a SHA. Never `git stash`.
- Railway: auto-deploy can skip a merge (it did for #529); fallback `railway redeploy -s <svc> --from-source -y`
  (api first). During its 2026-10-21/22 outage deploys were paused, `railway run`/`ssh` failed and the CLI logged
  out (`! railway login`). A worker build can die in the builder with no log: retry from source.
- `railway logs --lines` max is 5000; Railway logs are partial even inside 7 days.
- Memory reaper kills background jobs while the session is idle: wait in the foreground (< 9 min per call).
- Local test Postgres (`bl-testenv`, :5432) is DOWN (crashed, then reaped); restart only if needed:
  `pg_ctl.exe -D C:/Users/Windows/bl-testenv/data -l C:/Users/Windows/bl-testenv/pg.log -o "-p 5432" start`
  (it hangs the shell; check the port instead of waiting).

## Failed attempts (do not repeat)

- Retrospective `/records` watch: INCONCLUSIVE (Railway retained nothing before 10-13 and had 82 gaps).
- 7-day forward canary watch: dropped after Codex found 7 P1 (and it needed this laptop awake).
- `records_watch.mjs` first runs: npm `railway` shim ENOENT from Node, `--lines 100000` rejected, 990-deployment scope.
- Full local pytest: the shared Postgres crashed under memory pressure; use PR CI for the full suite.
- Running the full base `apply_rls_force.sql` in a rig fails (other tables need converge-script policies).

Memory: `project_ux_audit_2026_09_26.md` (last paragraphs), `landmine_railway_logs_partial_and_capped.md`.
