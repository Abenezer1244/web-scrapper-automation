# HANDOFF: contact lookup after 1c (LIVE) → close out Phase 1 (2026-10-03)

Read this whole file first. Then, in `tasks/todo-lookup-contacts.md` (search the headings):
`## Phase 1b-2e`, `### 2e MERGED + LIVE`, `## Phase 1c - the action, frontend`, `### 1c BUILD SPEC`,
the consult sections (AS1-AS9, AT1-AT2, AU1-AU3, AV1) and `### 1c BUILT, REVIEWED AND LIVE`.

## 1. The goal
Phase 1 of contact lookup: a customer buys skip-trace (phone/email) lookups for the leads on a
results tab. Provider Tracerfy; the operator pays per credit (normal 1, address-only 2).
The whole chain is now LIVE in production:
1. quote `POST /jobs/{id}/contact-lookups/quote` (1b-1c)
2. confirm `POST /jobs/{id}/contact-lookups` → 202 (2d, BE #432)
3. worker `lookup_contacts(action_id)` (2b) + reconciler (2c) + billing (O-C)
4. status `GET /jobs/{id}/contact-lookups/{action_id}` + list `GET /jobs/{id}/contact-lookups` (2e, BE #435)
5. the frontend page (1c, FE #209): "Look up contacts" button → quote/confirm dialog → progress panel.

**What is left of Phase 1 is close-out only** (§7). Nothing is half-built.

## 2. Where things are
| Where | What |
|---|---|
| BE worktree | `C:/Users/Windows/bl-wt-lookup`. **NEVER the OneDrive checkout for code: its `.env` is PRODUCTION.** (Prod read-only checks run FROM the OneDrive dir, see below.) |
| BE branch to continue on | **`docs/lookup-1c-spec`** = **BE PR #440** (OPEN, docs only: the plan + this handoff). |
| FE repo | `bridgeleads-web`, branch `master` (= Vercel deploy). Main checkout: `C:/Users/Windows/OneDrive - Seattle Colleges/Desktop/bridgeleads-web`. |
| FE worktrees (mine, all MERGED, safe to leave) | `C:/Users/Windows/bl-fe-lookup1c` (#209), `bl-fe-tooltip` (#206), `bl-fe-typesregen` (#208). Each has a `node_modules` junction to the main checkout. **Never delete/force-move branches in the shared OneDrive repo** (memory rule). |
| BE test env | `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` (gives `$PY` = `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe`), then `export DEBUG=false BL_TEST_REDIS_SERVER=C:/Users/Windows/bl-testenv/redis/redis-server.exe`. Local PG16 `bridgeleads_lookup1b_test`, Redis db 13. |
| Scratchpad of the last session | `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/79ada66a-d0cc-4495-bd9f-8de923962fb9/scratchpad/`: `mut_2e.py` + `mut_one_2e.py` (mutation runner, baseline cached by source sha), `2e_chunk_*` (61-file regression list), `stub_1c.mjs` (FE stub API :8125, `POST /__s` scenario patch, `GET /__log`), `drive_1c.mjs` (73-check Playwright drive) + `drive_1c_tail.mjs`, all `codex_*` prompts + `_out.txt`. |
| Prod checks (read-only, run from the OneDrive dir) | `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python C:/Users/Windows/bl-checks/quiet.py` (merge gate: all 4 counts 0). The owner allowed me to run it in this flow. Deploy check: `railway deployment list --service {api,worker,beat} --json` (parse with `$PY`, there is NO `jq`), `curl https://api.bridgeleads.io/health`, unauth GET of a new route = 401. FE deploy: `gh api repos/Abenezer1244/bridgeleads-web/commits/<sha>/status` → Vercel `success`. |

## 3. State (2026-10-03)
**LIVE (merged under the standing rule, deploy verified):**

| PR | Merge | What |
|---|---|---|
| BE #432 | `bd17ef2d` | 2d confirm: lookups PURCHASABLE |
| BE #435 | `b5dd93ee` | 2e status + list routes (+ the 2d/2e BUILD_JOURNAL entry) |
| FE #208 | `f6d99ca` | `lib/api-types.generated.ts` regen for #435 (unblocked EVERY FE PR's "API types in sync" gate) |
| FE #206 | `b34dbea` | no lookup-time promise: ContactStatus tooltip + run-page notice |
| FE #209 | `9ee1f9b` | 1c: the page (4 files) |

**OPEN:**
- **BE #440** (`docs/lookup-1c-spec`): docs only. 2e MERGED record; RESTORES the `## Phase 1c` heading my 2e
  plan edit dropped (the single "deletion" in #435; neither review caught it); 1c spec + consults + LIVE
  record; this handoff. Needs: CI green on the exact head → quiet → merge (standing rule) → verify.
- **Owner check NOT done:** the authenticated results page in production (admin login needs MFA; I could
  only verify the Vercel deploy + that the live shared chunk carries `/contact-lookups/quote`). Ask the
  owner to open a finished run, click "Look up contacts", read the quote. A quote buys nothing. **Never
  confirm a purchase in production without the owner.**

## 4. Active files
- BE: `src/api/routes/jobs.py` (quote ~1152, confirm ~1483, 2e "Contact lookups: the status" section:
  `_OUTCOME_BUCKET`, `_CUSTOMER_REASON`, `UnmappedDispositionError`, `_canonical_lookup_id`,
  `_lookup_read_rate_limit`, `_lookup_pause`, `_ACTION_STATUS_SQL`), `src/api/schemas.py`
  (`ContactLookupReason/Outcomes/Status/Summary/List`), `tests/test_contact_lookup_status.py` (28 tests),
  `tasks/todo-lookup-contacts.md`, `docs/BUILD_JOURNAL.md` (2026-10-02 entry: 2d + 2e).
- FE: `lib/api.ts` (`quoteContactLookups`, `confirmContactLookups`, `listContactLookups`, `getContactLookup`
  + type aliases), `app/(dashboard)/results/[id]/page.tsx` (mounts button + progress; `runDelivered =
  job.status === "done"`), `.../_components/ContactLookupDialog.tsx` (`ContactLookupButton`, fresh-plan gate,
  fixed refusal copy, `inDialogRefusal` 409/410 only, `toastFailure`, busy + alive refs, expiry timer),
  `.../_components/ContactLookupProgress.tsx` (query keys `["contact-lookups", jobId, category]` and
  `["contact-lookup", jobId, category, actionId]`; 15 s poll, 60 s after a 429, `BACKOFF_GATES`; one
  `role=status` region; results invalidated on outcome change).

## 5. Changes made in this session (beyond the PR table)
- 2e: Codex consult r1-r4 → PLAN: GO (r1 added the LIST route and the customer outcome vocabulary);
  diff review r1 NO-GO (malformed run id body; route-not-SQL test) → r2 GO; mutation 32/32; regression
  61 files 1,487 passed.
- 1c: consult r1-r5 → PLAN: GO; diff review r1 NO-GO (state after close, error routing, action key, 404
  under cached data, billable 0, prototype keys), r2 NO-GO (a failed poll hidden under cached data), r3
  NO-GO (P1 tooltip promise → #206; P2 focus/reconnect skipping the 429 backoff), r4 GO, post-rebase GO.
- Memory updated: `project_lookup_1b1a_2026_09_25.md` (10-02 later + 10-03 sections) and its MEMORY.md line.

## 6. Failed attempts / traps (don't repeat)
- **A schema change on BE main breaks EVERY FE PR's CI** ("API types in sync"). After any BE merge that
  changes `schema/openapi.json`, open an FE regen-only PR (`npm run gen:api-types`) right away.
- **My 2e plan edit silently dropped a heading** (an Edit whose old_string ended at the heading and whose
  new_string did not re-add it). After editing a plan, grep the headings you expect.
- **No `jq` in Git Bash:** my first CI monitor and a deploy-wait loop were silently useless. Parse JSON with `$PY -c`.
- **Heredocs with apostrophes / `$` broke the Bash tool** twice: write the content with the Write tool to
  the scratchpad, then insert it with a short `$PY` script (check CRLF: `nl = "\r\n" if "\r\n" in s`).
- **React Query test traps:** its focus listener is on `window` (a synthetic `visibilitychange` on
  `document` never reaches it); a reconnect is an offline→online CHANGE; the app's 30 s `staleTime` hides a
  focus refetch inside 30 s. My first two "proofs" of the 429 gate were vacuous; the mutant survived until fixed.
- **Playwright:** a forced second click on a button that detached waits 30 s (bound it with `timeout: 500`);
  `head -N` of the driver cut its login block; a test that inherits stub state from a previous run is vacuous
  (make every scenario set its own state).
- **`next dev` leaves an orphan `start-server.js` child after TaskStop.** Find it by port, confirm its PARENT
  is your worktree's `next dev`, then kill both. Delete the worktree's `.env.local` after every drive.
- **A shared Redis db was FLUSHDB'd by another session mid-run** (peer -76, now on db 6). Noise only adds
  failures; re-verify each mutation catch against its expected test.
- `git stash` is shared across worktrees: don't use it to compare (I did once; it popped back clean).

## 7. NEXT STEP (where I stopped), on branch `docs/lookup-1c-spec`
1. **#440:** wait for CI on the exact head (BE CI ~25 min) → quiet → "merging" to every peer
   (`ListAgents`) → `gh pr merge 440 --merge --match-head-commit <sha>` → verify (docs: Railway may
   redeploy; api/worker/beat SUCCESS, /health 200) → "verified". If main moved: rebase, byte-identical diff
   proof (`git diff OLDBASE...OLDHEAD | grep -v '^index '` vs `git diff origin/main...HEAD | grep -v
   '^index '`, `cmp`), Codex re-check, CI again.
2. **Ask the owner** for the authenticated prod check of the 1c page (§3). Record the result in the plan.
3. **Ask the owner** whether to write a BUILD_JOURNAL entry for 1c (+ #206/#208), and where.
4. Then Phase 1 is COMPLETE. Candidates for next (ASK the owner, don't pick): the malformed-`job_id` → 500
   on the other `/jobs/{job_id}` routes (GET/DELETE `/jobs/{id}`, `/results`, `/logs`, `/export-url`,
   `/download`) → move them onto `_canonical_job_id`; the stale "DELETE=False on every table" comments in
   `scripts/deactivate_test_batch_configs.py` / `purge_test_batch_configs.py`; a dedicated backend rate
   bucket for lookup reads only if real 429s appear (AS3).

## 8. Rules (binding, from the owner)
- **Codex in the loop on every step, FOREGROUND:** pre-code consult to `PLAN: GO`; three-dot diff review to
  `GATE: GO`; after every rebase a byte-identical diff proof + a Codex re-check. Invocation:
  `codex exec "$(cat prompt)" -s read-only -c 'model_reasoning_effort="high"' -c 'mcp_servers={}'
  --skip-git-repo-check < /dev/null > out 2>&1`; open every prompt with "Do NOT load any skill, do NOT run
  /graphify or any preamble. Read-only: ..." and tell it not to run tests; read the verdict after the LAST
  `tokens used` line. Consensus findings take the higher severity; Codex wins a disagreement the docs don't settle.
- **Tests:** real PG + Redis, no mocks (pass-through spy OK; fault injection labelled and proven to have run).
  FE has no test runner: tsc, eslint, next build, the dash script, a bundle grep, and a real-browser drive.
- **Mutation runner per PR:** commit first, `df -h /c`, anchors asserted, restore verified by hash,
  foreground slices of at most 3 mutants.
- **Regression** in 7-8 file chunks, output to files, only after Codex GO.
- **5-file rule** per PR (the plan counts; a handoff rides outside). **A merge is a deploy.**
- **Standing merge rule:** quiet.py all 4 counts 0, CI green on the EXACT head, Codex GO, main/master
  unchanged, `--match-head-commit`. Never `--admin`. "merging" / "verified" to EVERY peer from `ListAgents`.
- Before killing any process, confirm it is YOURS. Ask the owner before any production write and before any
  BUILD_JOURNAL entry.
