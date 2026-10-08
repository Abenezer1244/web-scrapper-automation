# HANDOFF: account deletion + data export. P1-P4 live (flags off); next is P5, the frontend. 2026-10-08

Read this whole file first, then the files in §5. Then follow CLAUDE.md (both repos):
- **Codex in the loop on every step:** a design consult before code, then diff review until GATE PASS
  (`.claude/rules/codex-collaboration.md` in the backend repo).
- **Security baseline:** `.claude/rules/security.md`.
- **Merge = deploy:** Vercel deploys frontend `master`; Railway deploys backend `main`.
- **Ask the owner before merging ANY PR.** Announce every merge to the peer session (§6).
- **Never switch on `ACCOUNT_EXPORT_ENABLED` or `ACCOUNT_DELETION_ENABLED`.** The owner does that
  after P5 is live and verified.

## 1. The goal

Profile follow-up 5, "Delete my account + Export my data", built in phases:

| Phase | What | State |
|---|---|---|
| P1 | migration 112: lifecycle schema, `bridgeleads_purge` role, request/restore functions | LIVE (BE #472) |
| P2a | `POST /auth/account/delete` + `/auth/account/restore` (behind `ACCOUNT_DELETION_ENABLED`) | LIVE (BE #473), flag OFF |
| P2b | 403 gate for pending accounts, worker refusal, download-link belt | LIVE (BE #474) |
| P3 | migration 113 fence + purge functions (#476), beat + migration 114 (#477), purge driver (#479) | LIVE |
| P4 | data export: migration 115 (#480), shared download query (#481), worker (#482), routes (#484), FE types (#247) | **LIVE, flag OFF** (this session) |
| **P5** | **frontend: Settings > Account > "Your data"** | **NOT STARTED: next** |
| — | owner switches on both flags | after P5 is live + verified |

Owner decisions that bind P5 (final):
- 30-day undoable grace period; billing cancels at period end, no refund (the dialog must say so).
- **Decision A (2026-10-07): an account scheduled for deletion can download NO export, even one
  made before the request.** The delete dialog must say "download your data first". A user who
  forgot restores, downloads, then asks again. Deletion also cancels an export still being built,
  so the dialog warns when one is pending/building.
- One export per 24 h (a failed one does not count); the link lasts 7 days; caps 250k lead rows /
  200 MB (over the cap the export fails with code `too_large`: show "contact support").
- Downloaded files cannot be recalled (policy wording is a counsel item; the dialog says it plainly).

## 2. Where everything is

- **Frontend worktree for P5: `C:\Users\Windows\bl-wt\regen-fe`**, branch **`feat/account-your-data`**,
  cut from `origin/master` @ `93c672d` (has the regenerated types). `node_modules` there is a
  Windows junction to `C:\Users\Windows\bl-wt\profile-fe\node_modules` (do not `npm install` into it;
  do not delete it with a recursive delete, which would follow the junction).
  - Do NOT use `bl-wt/profile-fe` for P5: it is on another branch (`feat/api-key-revoke-ui`).
  - Frontend repo: `Abenezer1244/bridgeleads-web` (GitHub), default branch `master`.
- **Backend worktree:** `C:\Users\Windows\bl-wt\profile-be` (now on `docs/handoff-account-p5`, which
  holds only this file). Backend `main` @ `0f69bf9c`. No backend change is expected for P5; if one
  is needed, branch from `origin/main`.
- **Desktop checkouts:** never for code. The Desktop backend repo is only for `railway` commands.
- **Helper scripts:** `C:\Users\Windows\bl-wt\tools\` (`source env.sh` for the backend test env):
  `purgesim.py` (migration simulation), `prod_verify_115.py` (read-only prod check of the export
  table), `prod_verify_481.py`, `prod_export_size.py`, `q.py`. Prod read-only runs: from the Desktop
  repo, `railway run --service worker C:\Users\Windows\bl-profile-venv\Scripts\python.exe -u <script>`.

## 3. The backend P5 talks to (all live)

`GET /auth/me` (allowed even while pending): `deletion_state` (`null` | `pending` | `purging` |
`deleted`) and `deletion_purge_after`.

Deletion (behind `ACCOUNT_DELETION_ENABLED`):
- `POST /auth/account/delete` {current_password, mfa_code?, confirm_email} -> 200 {purge_after};
  400 wrong password / bad 2FA code / email mismatch; 409 already being deleted; 404 when the flag
  is off. On success every session and the API key are revoked (the user is signed out
  everywhere), schedules paused.
- `POST /auth/account/restore` {current_password, mfa_code?} -> 204; 404 nothing pending; 409
  deletion already started. Never flag-gated. Schedules stay paused after a restore.
- A pending account can sign in again, but every other request is **403** "This account is
  scheduled for deletion. Restore it to continue." Only `GET /auth/me` and the restore pass.

Export (behind `ACCOUNT_EXPORT_ENABLED`; 404 when off):
- `POST /auth/export` {current_password, mfa_code?} -> 202 AccountExportResponse; 400 password/2FA;
  403 pending deletion; 409 one is already being prepared; 429 + `Retry-After` within 24 h.
- `GET /auth/export` -> the latest AccountExportResponse or `null`. Fields: id, status (`pending`
  | `building` | `ready` | `failed` | `expired`), requested_at, ready_at, expires_at, size_bytes,
  last_error (`deletion_requested` | `too_large` | `build_failed` | `upload_failed`, failed only),
  next_allowed_at.
- `GET /auth/export/{id}/url` -> {url} (relative `/auth/export/{id}/download?token=...`, valid 60 s);
  404 unless your own ready, unexpired export. Same pattern as the job CSV download
  (`/jobs/{id}/export-url`): mint the URL, then navigate the browser to API base + url.
- The worker builds within ~1 minute of the request (beat every minute) and emails the link.
- Types are generated in `lib/api-types.generated.ts` (`AccountExportRequest`,
  `AccountExportResponse`, `AccountExportUrlResponse`, the deletion request/response types).

## 4. What P5 must build (likely shape, check it with Codex and the owner first)

1. Replace the placeholder in `components/settings/AccountTab.tsx` (~line 141: "Your data ... email
   support@bridgeleads.io") with a real section, shown only when the flags are on (the backend
   answers 404 when off: treat 404 on `GET /auth/export` as "feature off" and keep today's email
   text; no fake button, no mock data).
2. **Export:** button -> password (+ TOTP when `mfa_enabled`) dialog -> POST; then status from
   `GET /auth/export` (poll while pending/building), the download button (mint `/url`, then open),
   expiry date, "next export available at", and clear copy for each failure code (`too_large` ->
   contact support).
3. **Delete dialog:** what happens (30-day grace, subscription cancels at period end, no refund,
   schedules paused, signed out everywhere, data deleted after the purge date, downloaded files
   cannot be recalled), "download your data first" (and "an export is being prepared" when one is
   pending/building), typed email confirmation, password (+ TOTP). On 200: sign out locally (the
   tokens are already revoked) and land on a "scheduled for deletion on <date>" page.
4. **Grace state:** when `/auth/me` says `pending`, every other call 403s. Show a full-page/banner
   state "This account is scheduled for deletion on <deletion_purge_after>" with a Restore action
   (password + TOTP dialog -> `POST /auth/account/restore` -> reload). Make sure the app does not
   spin on 403s or show generic errors elsewhere while pending.
5. Wire API calls in `lib/api.ts` beside `getMe` and friends, using the generated types; errors via
   `lib/errors.ts` (`getFriendlyError`, `toastError`).

Frontend verification (no test runner, see memory `fe_verify_without_test_runner`):
`./node_modules/.bin/tsc --noEmit`, `./node_modules/.bin/eslint . --quiet`, `node --test lib/*.test.mjs`
for pure helpers, then drive the real UI against a local API (memory
`reference_local_fullstack_fe_be_verify`) with both flags on LOCALLY (never in prod), at phone and
desktop widths. Measure touch targets in a fresh tab (memory
`landmine_playwright_fullpage_drops_pointer_coarse`).

## 5. Files to read for P5

| File | Why |
|---|---|
| `docs/product/account-deletion-and-export.md` (backend) | The design; §2 flow, §3 export + decision A, §4 owner decisions |
| `tasks/todo-account-deletion.md` (backend) | The full plan + every Codex round, incl. P4 ("P4 plan", owner decisions, P4a/b/c logs) |
| `components/settings/AccountTab.tsx` (frontend) | Where "Your data" lives today (placeholder) |
| `components/settings/EmailChangeDialog.tsx` (frontend) | The existing password + TOTP step-up dialog pattern to copy |
| `lib/api.ts`, `lib/errors.ts`, `lib/types.ts`, `lib/api-types.generated.ts` (frontend) | API client, error handling, types |
| The job CSV download in the frontend (search `export-url`) | The mint-URL-then-open download pattern |
| `src/api/routes/auth.py` + `auth_helpers/account_export.py` / `account_deletion.py` (backend) | Exact status codes and messages |

## 6. Changes made in the last session (2026-10-07/08)

All squash-merged with owner OK, Codex GATE PASS, CI green, prod-verified read-only (VERIFIED on each PR):
- **BE #480 (c0fc56ec) migration 115 `account_exports`.** Key derived `exports/{user_id}/account/{id}.zip`
  (never stored, inside the P3 purge sweep); app INSERT(user_id)+SELECT own, worker SELECT+UPDATE
  system columns, nobody DELETE (24-month request log); fence trigger; CHECKs incl. a fixed
  `last_error` allowlist. Codex 3 rounds. `purgesim.py` extended (privilege matrix, leak probe).
- **BE #481 (867b85cc)** `download_rows_select()` (results_category.py) + `config_export_options()`
  (lead_export.py) extracted from `GET /jobs/{id}/download`; prod: 25/25 jobs identical rows.
- **BE #482 (8a1a4fb4)** `src/workers/account_export.py` + beat `build_account_exports` (60 s,
  advisory lock 7_115_000_001, soft 1500 / hard 1560 s, lease 30 min). Codex 3 rounds.
- **BE #484 (0fb3fc14)** the `/auth/export` routes + dedicated download verifier + `stream_from_r2`;
  setting `ACCOUNT_EXPORT_ENABLED` (+ caps). OpenAPI +300/-0. Codex 2 rounds.
- **FE #247 (93c672d)** types regen. **BE #485 (0f69bf9c)** build journal entry.
- Tests: `tests/test_account_exports_schema.py`, `test_account_export_worker.py` (14),
  `test_account_export_routes.py` (13); mutation-checked guards in the worker and the verifier.
- Prod right now: alembic 115; 0 account_exports rows; 0 account_deletions rows; both flags unset
  (= false) on api and worker.

## 7. Failed attempts / landmines (do not repeat)

- **Stacked PRs get NO CI** until their base is `main`. After the base squash-merges: retarget
  (`gh pr edit N --base main`), `git rebase --onto origin/main <old base branch>`, force-push with
  lease (that push triggers CI). A retarget alone runs nothing.
- **Branch protection requires up-to-date branches:** when the peer merges first, update and re-run
  CI (~30 min) before merging.
- **asyncpg errors have no `.diag`**: match a constraint by name in `str(exc.orig)`.
- **Patch scripts:** quote escaping broke one generated script (nothing applied). Write patch scripts
  with the Write tool. Some backend files are CRLF, `src/api/routes/auth.py` is LF: check before
  patching bytes.
- **A failed rebase/`&&` chain can silently skip later steps** (an uncommitted rename blocked a
  rebase and the next heredoc never ran). Check `git status` before rebasing.
- **Background shells can be reaped on low memory**; do not restart them automatically, poll in the
  foreground (<= 10 min) instead. `sleep` is blocked: use `timeout N tail -f /dev/null`.
- **Local test runs share one test DB**; never run two pytest processes at once. Backend tests:
  `$PY -m pytest <files> -q -p no:cacheprovider -m "integration or not integration" > log 2>&1; echo $?`.
- **Codex:** `codex exec -c 'mcp_servers={}' --skip-git-repo-check -s read-only - < prompt.txt` from a
  scratch dir, header "DO NOT run shell, read files, or use git", pass prior dispositions with
  evidence, diff with three dots (`origin/main...HEAD` / `origin/master...HEAD`).

## 8. Next steps, in order

1. Read §5. Write the P5 plan into the backend `tasks/todo-account-deletion.md` (P5 section) or a
   frontend `tasks/` file. Use `superpowers:brainstorming` for the UX.
2. Codex design consult on the plan; fold findings in; **check the plan with the owner before code.**
3. Build in `bl-wt/regen-fe` on `feat/account-your-data` (<= 5 files per PR; split export UI and
   delete/grace UI if needed). Verify (§4), Codex diff review to GATE PASS, PR to `master`, CI +
   Vercel preview green.
4. Owner OK -> announce to the peer -> merge -> verify prod (flags still off: the section must show
   today's email text, nothing broken) -> post VERIFIED.
5. Tell the owner P5 is live; the owner switches on `ACCOUNT_EXPORT_ENABLED` then
   `ACCOUNT_DELETION_ENABLED` (api + worker). Offer a read-only check after each flip.
6. Append a build-journal entry (backend `docs/BUILD_JOURNAL.md`, docs PR) at the end.

**Peer session:** `web-scrapper-automation-1c` (frontend/UX work, also merges backend PRs). Before any
merge: `ListAgents` + `SendMessage` "MERGING NOW ... hold merges", then "VERIFIED" after the prod
check. It is working on FE2 (`feat/ux-item3-3.10a-fe2-measured`, results pages); coordinate if P5
touches the same files.

## 9. Open items for counsel / owner (not blocking P5)

Tracerfy deletion path; California coverage / data-broker status; WA sales-tax location fields;
FCRA prohibited-use clause; downloaded-files policy wording; Supabase backup window; the published
privacy contact (`bridgeleads.com`) does not receive mail (fix before homeowner suppression, the
next project after this one).
