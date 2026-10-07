# HANDOFF: account deletion, P3b (beat purge task) — 2026-10-06 session 2

Read this file, then `tasks/todo-account-deletion.md` (the P3b plan and every Codex round). Rules:
- Codex is in the loop: consult before code, then diff review to GATE PASS.
- Security baseline applies; no mocks (Stripe, Resend and R2 use injected stand-ins).
- Merge = deploy. **Ask the owner before merging any migration.**
- Never switch `ACCOUNT_DELETION_ENABLED` on: the owner does that after P5.

## State
| Piece | State |
|---|---|
| 113 (fence + purge functions) | **LIVE**, BE #476 (5b7a00aa), prod-verified read-only |
| P3b-1 + migration 114 | **PR #477** open. Branch `feat/account-deletion-p3b`. CI green (on the pre-#478 base). Codex GATE PASS. **Needs owner OK to merge** (migration). |
| P3b-2 purge driver | Branch `feat/account-deletion-p3b2` (stacked on p3b). Local, **not pushed**. Codex GATE PASS (7 rounds). 24/24 beat tests. |
| P4 export, P5 frontend | not started |

Worktree: `C:\Users\Windows\bl-wt\profile-be`. Env: `source` the scratchpad `env.sh` (as in the P3 handoff §5).

## Next steps
1. Owner OK → merge #477:
   - main moved (#478), so first rebase on origin/main, re-run the account-deletion and
     skip-trace tests, and push (CI re-runs);
   - announce to the frontend peer `web-scrapper-automation-1c`;
   - verify prod read-only: alembic 114, system has SELECT on account_deletions plus policy
     `account_deletions_system_select`, and the beat runs (worker logs "drive_account_deletions",
     0 rows, no errors);
   - post VERIFIED.
2. Rebase `feat/account-deletion-p3b2` onto main after #477, re-run tests, push, open the PR.
   Its body: no migration; R2 listing via the native API (verified on prod); a summary of
   Codex's 7 rounds. Then CI → owner OK → merge → verify (with no rows the beat is a no-op).
3. A full local suite before merging P3b-2: run it in the foreground, never piped to tail.
   The 3 contact_lookup_schema worker-role failures are pre-existing.
4. Then P4 (`POST /auth/export`) and P5 (the frontend). Full plan in the todo file.

## Facts and landmines from this session
- The worker can only READ `account_deletions` (114); every write goes through 113's definer
  functions.
- Production's R2 S3 keys can't list (Unauthorized). `DataExporter.list_r2_keys` uses the
  native Cloudflare API: 200, `result` list, no cursor seen at 462 objects. The sweep pages by
  deleting a page and listing again until empty.
- The beat only deletes the Stripe Customer when no non-canceled subscription and no
  draft/open invoice exists. Waiting is normal (annual plans); ops is alerted only 400 days
  after completion or on a run of Stripe errors.
- Every due account with work in flight is deferred (drained in batches) before any claim. Past
  day 40 the CCPA deadline wins and the fence covers the rest.
- Heredocs that carry Python patches break: write the patch with the Write tool and run it.
