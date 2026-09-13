# HANDOFF: Snohomish coverage audit and repairs (session 2026-09-13)

> **CLOSED 2026-09-13 (follow-up session).** Everything in section 4 landed:
> backend #284 `3aefcce`, #281 `b878796` (squash-merged by the old session's watcher after CI
> Test=SUCCESS); Railway api + worker SUCCESS at `b878796`, a descendant of `3aefcce`, so it covers #280, #281 and #284. Frontend #130
> `678703e`: the owner set `BACKEND_SCHEMA_TOKEN` (the first pasted value was not a valid token and
> the fetch answered 401; the second worked), and the now-working drift check caught one comment
> line from backend #282, regenerated in `821f6fd`. Master CI and Vercel green on `678703e`.
> Still open: the post-deploy checks in section 5 step 3 (today's 10:45 UTC Tribune crawl ran
> BEFORE the 10:58 deploy, so the two REF- rows arrive with the next crawl) and every owner
> decision in section 7. Sections 4 and 5 below are the historical record, not current state.

Written at the end of a long session whose context ran out. Everything below was verified
in-session unless marked UNVERIFIED. Read this file fully before acting.

## 1. Goal

The owner asked for a full Snohomish County (WA) coverage audit of all six BridgeLeads record
types (Code Violation, Tax Delinquent, Probate, Pre-Foreclosure, Death Certificate, Auction
Leads), then approved "go with your recommendation", then "approved and fix all". The goal is
that BridgeLeads only claims Snohomish support it can truthfully, lawfully and reliably deliver,
and that every defect the audit found is fixed, reviewed by Codex, merged and deployed.

Standing rules that applied the whole time (from CLAUDE.md / .claude/rules and owner): isolated
worktrees, no mock/dummy code, Codex consult before code and Codex diff review after (any P1 =
NO-GO, Codex wins on disagreement unless evidence shows its baseline is wrong), fail loud, never
fabricate data, no em dashes in user-facing copy, journal entry per build, merge to `main`
deploys backend (Railway api + worker), merge to `master` deploys frontend (Vercel).

## 2. Audit verdicts (unchanged, no code will change these)

Full report: `C:/Users/Windows/bridgeleads-worktrees/snoho-coverage-audit/docs/audits/snohomish-coverage-audit-2026-09-13.md` (detached worktree, uncommitted).

| Type | Verdict |
|---|---|
| Code Violation | NOT CURRENTLY SUPPORTABLE (PDS monthly PDF = complaints incl. "No Violation", no owner/status, unincorporated county only, county anti-automation terms) |
| Tax Delinquent | NEEDS REPAIR (now repaired, see PRs #276/#277) + LEGAL REVIEW (taxpayer names, RCW 42.56.070(8)) |
| Probate | NOT CURRENTLY SUPPORTABLE (GR 31(g)(4) bars bulk court records for commercial solicitation; recorder login + ToS; newspaper Notice to Creditors has no parcel) |
| Pre-Foreclosure | PARTIAL COVERAGE ONLY (Snohomish County Tribune NTS only) |
| Death Certificate | NOT CURRENTLY SUPPORTABLE (RCW 70.58A.540 exempts vital records from PRA) |
| Auction Leads (trustee_sale) | PARTIAL COVERAGE ONLY; SAME source/notices as Pre-Foreclosure (owner product decision still open) |

## 3. Shipped and deployed (merged to main/master)

| PR | Repo | Merge SHA | What |
|---|---|---|---|
| #276 | backend | 264a565 | Snohomish tax: `tax_cap_min_year` floored at `today.year-1` (was 0 parcels Aug 1-Dec 31); scraper was reading the APRIL tax file (page switched data link to absolute URL, relative-only regex picked the description twin) -> host-pinned regex, raise on twin, as-of staleness guard (62d), date-level splice check, no fabricated `01/01/<year>` date. Live: 0 -> 1,751 parcels. |
| #277 | backend | e18517e | `months_delinquent` + months filter counted from May 1 (RCW 84.56.020), clamped at 0; `min_months=0` no bound + explicit NOT NULL. Cap unchanged (0 differing days 2020-2035). King display shifts 4 months. |
| #278 | backend | 18fcdf3 | trustee_sale fails an EMPTY result when the county crawler heartbeat (`external_source_health` key `nts_crawl:<county>`, written only via `mark_source_healthy`, never probed by canary) is >3 days old (fallback: max(fetched_at) >15d). Snohomish pre_foreclosure fails on unreadable PDF / notices found but none parsed; TransientScrapeError on transport errors. |
| #129 | frontend | b4a5fc4 | New Scraper wizard: county readiness + record-type chips from each connector row's own health (was first-row-wins). Vercel deploy succeeded. |
| #280 | backend | 41c7a1c | Tracerfy no longer paid for undelivered leads: skip-trace enqueue moved AFTER the plan cap in `run_scrape_job`; dispatcher claims only rows whose job is `done` and lead is queued/non-duplicate/not over_quota (SQL-side, `FOR UPDATE OF` queue); per-tick sweep sets undeliverable queued rows to new status `cancelled`. |

Railway api + worker were verified at 18fcdf3 (includes #276-#278). #280 deploy NOT separately verified (UNVERIFIED).

## 4. In flight at handoff time (HISTORICAL: all merged, see the CLOSED note at the top)

### PR #281 (backend) `fix/nts-parser-formats`
- Worktree: `C:/Users/Windows/bridgeleads-worktrees/nts-parser` (branch `fix/nts-parser-formats`, head `a478854`, rebased onto main 57857ad and force-pushed with lease).
- State at handoff: OPEN, CI re-running after the rebase (previous run Test=SUCCESS before a BUILD_JOURNAL conflict made it DIRTY).
- What it does: recovers 3 dropped Snohomish trustee sales from the 9-9-26 Tribune (Affinia notice with no TS number; Burns Law commercial notice cut in two by the splitter at "I. NOTICE OF TRUSTEE'S SALE"; worded date with "o'clock"). Snohomish now parses with `parse_snoho_notice` (King's `parse_king_notice`, but APN-only identities rejected). A real TS upsert retires exactly its `REF-<recording number>` twin; a retired twin is not reactivated by re-crawling an older issue (incl. postponement).
- Files: `src/scrapers/sources/nts_pdf.py`, `nts_tacoma_index.py`, `nts_king_pdf.py`, `src/workers/nts_crawler.py`, `src/scrapers/snohomish_wa_pre_foreclosure.py`, `scripts/backfill_nts_pdf_archive.py`, `scripts/repair_nts_ts_number.py`, `scripts/diag_nts_stored_vs_source.py`, `tests/test_nts_parser_formats.py` (11), `tests/test_nts_surrogate_retire.py` (9), `tests/test_nts_pdf_archive_backfill.py`, `tests/fixtures/nts_snoho_tribune_2026-09-09.pdf`, `docs/BUILD_JOURNAL.md`.
- Verified: 431 related tests pass; recovered REF-202411260448, REF-202211100430, REF-202303210039; 0 changed fields on previously valid notices across 7 PDFs + 17 Pierce/King/Clark fixtures. Codex 4 rounds, GATE PASS.

### PR #284 (backend) `fix/gate-gis-mailing-license`
- Worktree: `C:/Users/Windows/bridgeleads-worktrees/gis-gate`.
- State at handoff: OPEN, CLEAN, Test=SUCCESS. Ready to squash-merge.
- Owner decision (2026-09-13): hold Snohomish + Cowlitz county-GIS owner/taxpayer MAILING (added by another session's #275) OFF pending legal review; the Snohomish parcel dataset license says users "will not use any lists of individuals, or data from which such lists may be compiled, for any commercial purpose" (RCW 42.56.070(8)). Property-address enrichment is kept. Cowlitz's license was NOT separately verified (gated with Snohomish per owner).
- Implementation: `mailing_license_restricted` + `situs_only_out_fields` on the two configs in `src/scrapers/enrichment/county_gis.py`; `_effective_gis_config()` strips mailing keys unless `settings.COUNTY_GIS_RESTRICTED_MAILING_ENABLED` (default False; added to `src/config/settings.py` and `.env.example`); every config read goes through it incl. explicit `gis_endpoint` overrides and the recovery sweep. Tests: `tests/test_county_gis_license_gate.py` (new) + autouse enable fixture in `tests/test_gis_mailing_recovery.py` and `tests/test_requeue_gis_mailing_recovery.py`. 179 GIS/mailing tests pass. Codex 2 rounds, GATE PASS.
- After merge, Snohomish/Cowlitz mailing shows N/A again. Intended.

### A background watcher was started (task in the old session)
It polls #281 then #284 and squash-merges each only if `Test=SUCCESS`. It may or may not have finished (an earlier watcher was killed for low memory). The next session must CHECK, not assume:
`gh pr view 281 --json state,mergeStateStatus,mergeCommit` and same for 284.

### PR #130 (frontend, bridgeleads-web) `fix/ci-private-backend-schema`
- Worktree: `C:/Users/Windows/bridgeleads-worktrees/web-county-health` (branch checked out there).
- The backend repo went PRIVATE, so FE CI's `npm run gen:api-types` (unauthenticated raw.githubusercontent) has 404'd on every master run since 2026-09-09. PR moves lint + type-check first, fetches the schema via GitHub API with secret `BACKEND_SCHEMA_TOKEN`, and regenerates `lib/api-types.generated.ts` (was missing `/billing/change-plan` from backend #268; additive, tsc clean).
- BLOCKED ON OWNER: create a fine-grained PAT with Contents: Read-only on `Abenezer1244/web-scrapper-automation`, then `gh secret set BACKEND_SCHEMA_TOKEN -R Abenezer1244/bridgeleads-web`, re-run CI, merge. Do NOT put a broad personal gh token into the secret.

## 5. Next steps (in order)

1. Check #281 and #284 states. For each still open: if `mergeStateStatus` is DIRTY, rebase in its worktree (conflicts are almost always `docs/BUILD_JOURNAL.md`: take `--ours` = main's version, re-insert this PR's entry right after the first `---` separator, keep CRLF), push with `--force-with-lease`, wait for CI `Test=SUCCESS`, then `gh pr merge <n> --squash` (retry on GitHub 5xx/GraphQL errors; REST fallback `gh api -X PUT repos/Abenezer1244/web-scrapper-automation/pulls/<n>/merge -f merge_method=squash`).
2. Verify deploy: `railway deployment list --service worker --json` and `--service api` from the main checkout; the newest SUCCESS `meta.commitHash` must equal the merge SHA.
3. Post-deploy notes: the next Snohomish Tribune crawl (daily ~10:45 UTC) will insert `REF-202411260448` and `REF-202211100430` into `nts_notices`; the Snohomish tax connector should return to healthy once the hourly canary re-probes it (do not hand-flip health).
4. Remind owner about #130 secret; merge #130 after CI passes.
5. Update memory file `project_snohomish_full_coverage_audit_2026_09_13.md` (in the Claude projects memory dir) with the final merge SHAs for #280/#281/#284/#130.

## 6. Failed attempts / landmines hit (do not repeat)

- Read-only production DB queries via `railway run` were DENIED by the auto-mode classifier. Do not work around it; ask the owner if prod numbers are needed.
- All project venvs point at a removed Anaconda; Python 3.13 install is broken. Use `source C:/Users/Windows/bl-testenv/env-taxcap.sh` (venv `C:/Users/Windows/bl-testenv/venv-taxcap`, Python 3.11, isolated DB `bridgeleads_taxcap_test`, Redis db 14) BEFORE any pytest. Never run bare pytest (it reads the production .env and has wiped prod twice).
- Always run pytest with `-p no:cacheprovider -o addopts=""`. Full-suite runs get OOM-killed while other sessions run tests; run the related test files instead and let CI run the full suite. 7 `test_plan_entitlement_audit` failures are local-env only (they pass in CI).
- The shared local Postgres restarts sometimes ("database system is starting up"): rerun, do not debug.
- Codex CLI: pass prompts via stdin (`codex exec -c 'model_reasoning_effort="high"' -c 'mcp_servers={}' --skip-git-repo-check - < file`); inline prompts over ~30KB fail with "Argument list too long". Tell it "DO NOT run shell, read files, git, or edit". Check `git status` before/after. Codex once said GATE PASS while tagging a [P1] (treat as NO-GO) and twice raised P1s from a wrong baseline (measure and re-gate; it withdrew).
- GitHub had transient GraphQL errors and 502s on merge; retry.
- FE worktrees have no node_modules; junction (PowerShell `New-Item -ItemType Junction`) from `C:/Users/Windows/bridgeleads-worktrees/fe-pierce-retry-stuck/node_modules` (lockfile matched). The main FE checkout's node_modules lists empty. Run `node_modules/.bin/tsc.cmd --noEmit` and `eslint.cmd` via PowerShell. FE has no test runner.
- Git Bash mangles `origin/master:path` specs; prefix with `MSYS_NO_PATHCONV=1`.
- Editing Python files via heredoc-embedded Python broke once on quoting; write the patch script to the scratchpad and run it. Files use CRLF: read bytes, normalize, write back preserving CRLF, and `assert old in s` before replacing.

## 7. Open owner decisions (not code)

- Snohomish pre_foreclosure and trustee_sale come from the same Tribune notices (a user buying both gets mostly duplicates): retire, alias, or relabel.
- Legal review: county GIS mailing license (#284 gate), taxpayer names in the Treasurer tax list, Tribune publisher reuse terms (none found).
- Code Violation / Probate / Death Certificate for Snohomish stay unsupported unless written authorization or a lawful licensed source is obtained.
