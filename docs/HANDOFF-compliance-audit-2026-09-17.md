# HANDOFF — BridgeLeads compliance audit, phase 2 remediation

**Written:** 2026-09-17, end of session. **Read this whole file before touching code.**

---

## 1. The goal

The owner asked for a 20-item legal, privacy, accessibility, billing, marketing and
consumer-protection audit of BridgeLeads (`https://bridgeleads.io`), explicitly
**audit-first**: inventory reality before implementing, never claim legal compliance,
never publish AI-written legal text, then remediate in separated buckets
(engineering fixes / owner decisions / legal drafts / accessibility / data
architecture / marketing copy).

Phase 1 (audit) is **complete**. Phase 2 (remediation) is **partially complete** and
is where you are picking up.

**Two standing constraints from the owner, still in force:**
- Do **not** write legal prose. Placeholders and policy text go to counsel.
- No em dashes in proposed user-facing BridgeLeads copy.

---

## 2. Where you are right now — the one thing blocking everything

**`main` on the backend repo is RED.** CI fails the OpenAPI drift gate:

```
STALE: schema/openapi.json is out of date. Run: python scripts/export_openapi.py
```

**Production is healthy** (`https://api.bridgeleads.io/health` returns
`200 {"status":"ok"}`). This is schema-file freshness, not a functional break.

**How it got red:** BE #335 merged with its `Test` job failing, because **`Test` is
not a required status check** on `web-scrapper-automation` and `gh pr merge --auto`
only gates on required checks. Do not repeat this: always `gh pr checks <n>` and wait,
or get `Test` made required.

**ROOT CAUSE FOUND AND FIXED 2026-09-17 (next session).** It was never a stale
committed schema. `schema/openapi.json` was correct the whole time; regenerating it
in a pinned env produces a byte-identical file. What was stale was the CI *merge*
commit. See the correction in §5.2 and §6 Step 1.

**NEW BLOCKER (owner): GitHub Actions is billing-blocked.** Since ~10:24 UTC every
job fails in ~1s, never starting, with: *"The job was not started because recent
account payments have failed or your spending limit needs to be increased."* This is
an account-level block, not a code problem. **No CI run can go green until the owner
clears it** (GitHub -> Settings -> Billing & plans). Runs before 10:24 executed
normally, so a 1s failure means billing, not a real test failure - always check
`gh run view <id>` annotations before reading a red X as a code defect.

---

## 3. Active branch and worktrees

You are working in a **dedicated git worktree**, not the shared OneDrive checkout.
This is deliberate: another session works in the shared checkout, and both repos were
~100 commits stale there (see §7).

| Path | Branch | State |
|---|---|---|
| `C:/Users/Windows/bl-wt/logpriv` | **`fix/openapi-drift-from-docstring`** @ `66e4d5d` | **your branch — BE PR #336, OPEN** |
| `C:/Users/Windows/bl-wt/fe-a11y` | `fix/landing-main-landmark-and-signin` | merged as FE #152; safe to remove |
| `Desktop/web-scrapper-automation` | detached/shared | **do not switch or pull — another session uses it** |
| `Desktop/bridgeleads-web` | `feat/schedule-day-picker` | someone else's branch; leave alone |

`cd /c/Users/Windows/bl-wt/logpriv` to continue.

**Note:** the `node_modules` junction in `bl-wt/fe-a11y` was already removed. If you
recreate one for typechecking, remove it with `cmd /c rmdir <link>` (never
`Remove-Item -Recurse`, which can follow the junction into the real `node_modules`).

---

## 4. What shipped

| PR | Commit | State | Content |
|---|---|---|---|
| BE **#334** | `4d2b926` | merged, deployed | uvicorn access-log rule scrubbing `/webhooks/tracerfy/{provided_secret}`; `owner_name` removed from `county_gis.py` (3 sites) and `pacs.py` (1), logging `county_key`/`county` instead |
| FE **#152** | squashed to `master` | merged, deployed, **verified live** | `<main id="main">` + skip link on the landing page; footer `Sign in` → `/login` |
| BE **#335** | `6a82748` | merged, deployed, **CI red** | `TRACERFY_LEGACY_PATH_ENABLED` kill switch (410s the legacy route *before* the secret compare); secret-free `tracerfy_legacy_route_used` telemetry |
| BE **#336** | `66e4d5d` | **OPEN, CI red** | route docstring restored byte-for-byte + notes moved to comments; this session's `BUILD_JOURNAL.md` entry |

FE #152 was verified by fetching the deployed HTML: `Skip to content`, `id="main"` and
`href="/login"` all present.

---

## 5. Failed attempts — read this so you do not repeat them

1. **Merged a red build.** As in §2. `--auto` does not gate on non-required checks.
2. ~~**The docstring theory for the OpenAPI drift was WRONG.**~~ **CORRECTED
   2026-09-17: the docstring theory was RIGHT. The *branch base* was wrong.**
   The original reasoning (FastAPI publishes a route docstring as its OpenAPI
   `description`, so expanding it in #335 made the schema stale) was correct.
   Restoring it byte-for-byte on the #336 branch did not turn CI green for a
   reason that has nothing to do with the theory: **CI does not build the branch,
   it builds the branch merged with `main`.** #335 was *squash-merged*, so its
   content was already on the #336 branch but its commit was **not an ancestor**.
   Git therefore read main's long docstring as a new addition and #336's
   docstring->comment move as an unrelated edit, and textually auto-merged
   **both** - the merge tree carried the long docstring *and* the comment block
   saying not to put it there. That merge tree generated the long `description`,
   and the gate correctly said STALE.
   **Lesson:** when a fix "does not work" on a PR, reproduce what CI actually
   builds (`git merge origin/main --no-commit --no-ff`), not what you committed.
   A squash-merged upstream PR makes every follow-up branch a silent-conflict
   candidate - see the `auto_merge_is_only_textual` landmine.
3. **I could not regenerate the schema.** `.venv-schema/Scripts/python` points at a
   removed anaconda install (`No Python at 'C:\Users\Windows\anaconda3\python.exe'`),
   the `py` launcher is broken too (`Failed to import encodings module`), and
   `reference_openapi_regen_env_matters` says the regen environment matters, so
   improvising one risks committing a wrong schema. `ruff` from `.venv-schema` works
   (standalone binary). `uv` works: `env -u PYTHONHOME -u PYTHONPATH uv run
   --no-project --python 3.13 python <script>` runs standalone Python fine.
4. **Both checkouts were ~100 commits stale for the whole audit.** `git fetch origin
   <branch> --quiet` did **not** update the ref, so my `git diff origin/master...HEAD`
   returned empty and I told the owner "the code I read is the deployed code". Wrong.
   **Always `git fetch origin && git rev-list --count HEAD..origin/<branch>` before
   any "local == deployed" claim.**
5. **A fix I nearly shipped was theatre.** "`install_global_redaction()` missing in
   the Celery worker" looked like a gap. It is not load-bearing: both PII modules use
   `setup_logger()`, which already attaches the redaction filter
   (`logger.py:119,136`), and an import-time call in the worker would be a silent
   no-op (it attaches to the root logger's *existing* handlers; Celery has attached
   none at import time; Python consults logger-level filters only for records created
   on that logger, never propagated ones). Dropped deliberately.
6. **My nav-contrast finding (D1) was partly a measurement artifact.** My script
   sampled what was painted *behind* the nav, which is wrong once the nav has its own
   opaque `bg-paper-white`. Three reported "invisible nav" zones reduce to one
   (white-on-white, scrollY ~900-3200). Deferred, not fixed — needs re-measuring on
   current `origin/master`, where the landing page has since been rebuilt (`Hero3D`,
   `HowItWorksScrolly`, `DashboardShowcase`, `Integrations`, `Reveal`).
7. **The Playwright MCP browser backend died** mid-session and would not restart
   (`Target page, context or browser has been closed`). Live-site checks after that
   were done with `curl`.
8. **Codex model entitlement:** `gpt-6-astra` (the gstack default) is **not entitled**
   on this account. Use `-c 'model="gpt-5.6-luna"'`. Codex also hit usage limits twice
   before that. Invoke read-only:
   `codex exec "$(cat prompt.txt)" -C <repo> -s read-only -c 'model="gpt-5.6-luna"' -c 'model_reasoning_effort="high"' -c 'mcp_servers={}' < /dev/null`
   Its final answer sometimes lands on **stderr**, not stdout.

**Findings Codex overturned (both mine, both correct to overturn):**
- **Anthropic and Regrid are dead code, not live data transfers.** Both are reached
  only via `enrich_parcel` in `src/scrapers/enrichment/parcel.py`, which **has no
  caller anywhere**. The live pipeline uses `batch_enrich_parcels_gis`. The capability
  is real and default-enabled (`AI_ENRICHMENT_ENABLED=True`), so it is a **latent**
  risk: `ai_assessor.py:104,149` would send assessor-page screenshots (containing owner
  names and mailing addresses) to Claude. Nothing flows today.
- **"Lead data cannot be deleted" was overstated.** `scripts/` contains
  `DELETE FROM results` at three sites and `DELETE FROM skip_trace_cache` at one, and
  `results.user_id` is `ondelete="CASCADE"` (`models.py:762`) — the model is already
  built for user-delete. The retention finding itself stands: nothing deletes `results`
  **on a schedule**.

---

## 6. Next steps, in order

**Step 1 - unstick CI. DONE in code (`11c7cc8`), blocked on billing.**
No schema regeneration was needed or made: the committed `schema/openapi.json` was
already correct. The fix was to merge `origin/main` into the branch and **resolve
`webhooks.py` to the branch's version** (short docstring + comments), which is what
the textual auto-merge got wrong. Verified it loses nothing: against `origin/main`
the branch differs only in two docs files and that docstring move; `settings.py` and
the kill-switch code are byte-identical on both sides.

Verified locally in a faithful env before pushing - python 3.12 + `requirements.txt`,
with the installed package versions **diffed against the CI run's own pip output**
(identical except ruff/uvloop/colorama, none schema-affecting):
```
export_openapi.py --check  =>  OK: schema/openapi.json is up to date.
ruff check src/ tests/     =>  All checks passed!
```
Rebuild that env with:
```
uv venv --python 3.12 /c/Users/Windows/bl-schema
uv pip install --python /c/Users/Windows/bl-schema/Scripts/python.exe -r requirements.txt
```
then run with CI-equivalent env vars (copy them out of `.github/workflows/ci-cd.yml`;
never point `DATABASE_URL` at prod).

**#336 cannot report green until the Actions billing block above is cleared.** The
code is verified; the runner is not being allowed to start.

**Step 2 — make `Test` a required status check** on `web-scrapper-automation`
(owner, repo settings). It has now let one red build onto `main`.

**Step 3 — add `TRACERFY_LEGACY_PATH_ENABLED` to `.env.example`**, per
`.claude/rules/settings.md`. That file is covered by a tooling **deny rule** in this
environment and was deliberately not worked around.

**Step 4 — the retention decision.** This is the only thing blocking the largest
remaining engineering item. The owner must pick:
- **(a)** honour the policy: purge `results` past 365 days
- **(b)** amend the policy to describe actual behaviour (no code)
- **(c)** *recommended*: keep lead rows, purge only skip-traced phone/email past 365 days

If (a) or (c), the shape Codex and I agreed on: a **Celery Beat task under
`bridgeleads_system`** (not an ops script); a **narrow `DELETE ON results` grant for
the system role only** (leave `bridgeleads_app` with none); add `results` to the
worker-delete verification allowlist (`scripts/_cutover_step2_grants_policies.py:107-121`)
so the grant cannot drift silently — a failure this repo has already had; bounded
per-user batches on `user_id` + `created_at`; check whether
`alembic/versions/068_results_user_created_index.py:22-25` (partial to non-duplicates)
needs a full index; and scope must include the skip-trace cache, queue metadata and
**R2 objects** — note `src/utils/data_exporter.py:218-254` has upload and download but
**no delete method at all**.

**Step 5 — the remaining owner/legal items.** All eight are written up as mechanical
steps in `tasks/PHASE2-OWNER-RUNBOOK.md`. Mailbox is first and is the most urgent
non-code item (see §8).

---

## 7. Key files

| File | What |
|---|---|
| `tasks/BRIDGELEADS-COMPLIANCE-AUDIT-PHASE1.md` | the full audit: 20-item matrix, findings, §1a corrections log, engineering plan |
| `tasks/PHASE2-OWNER-RUNBOOK.md` | the 8 remaining items as owner steps, with the Codex cross-check |
| `tasks/COMPLIANCE-AUDIT-FINDINGS-2026-09-16.md` | my first-hand verified findings |
| `docs/BUILD_JOURNAL.md` | session entry appended (committed on `66e4d5d`) |
| `docs/legal/DATA-INVENTORY-AND-COMPLIANCE-AUDIT.md` | **a prior audit already existed** (2026-06-02) with a risk register C-1..L-3 |
| `docs/legal/PRE-LAUNCH-LEGAL-CHECKLIST.md` | **0 ticked / 31 unticked** — read before re-deriving anything |
| `src/api/routes/webhooks.py` | the legacy route + kill switch |
| `main.py:111-150` | the uvicorn access-log redaction filter |
| `src/config/settings.py:337-344` | `TRACERFY_LEGACY_PATH_ENABLED` |

---

## 8. The findings that still matter most (unfixed, owner-blocked)

1. **No MX on `bridgeleads.io`.** The app sends from `leads@bridgeleads.io` and sets
   `Reply-To: support@bridgeleads.io` (`settings.py:201,211`), printed in email
   footers — so **every customer reply bounces**. No SPF, no DMARC; Resend DKIM *is*
   present. And the published policies route privacy/security/legal contact to
   `@bridgeleads.com`, which has **no MX**, publishes `v=spf1 -all`, and sits on
   NameBright parking nameservers — i.e. not the business's domain. Cloudflare Email
   Routing fixes all four addresses in ~10 minutes (the domain is already on Cloudflare).
2. **Nine `[BRACKET]` placeholders live in the published Privacy Policy and Terms**,
   including in Limitation of Liability, Indemnification and Governing Law, plus a
   published note-to-counsel. The source drafts are headed "DRAFT — NOT LEGAL ADVICE"
   and were shipped the same day they were generated.
3. **Retention promise is not performed.** Policy §7 promises 365-day deletion of lead
   records; nothing deletes `results` on a schedule, and neither app DB role holds
   `DELETE ON results`.
4. **The DNC indicator is never populated.** `tracerfy_ingest.py:537` hard-codes
   `phone_dnc_flag=None` (Tracerfy supplies no DNC feed), DNC is not an exported
   column, and the live path passes `include_unknown_dnc=True`. Policy §3 claims a DNC
   indicator; Terms §5(a) obliges customers to honour one they never receive.
5. **Annual billing charges $1,910 / $4,790 / $14,390 upfront** while the pricing page
   shows only "$159/mo" with no total.
6. **`audit_log()` persists `request.url.path`** into the append-only `audit_events`
   table (`security.py:585,596` → `:551`), which the app role cannot read or delete.
   27 callers, **none in `webhooks.py`** — latent, not live. **Never route a
   path-secret endpoint through `audit_log`.**

---

## 9. Hard environment rules

- **NEVER run `pytest` locally.** It has twice wiped the production database. CI runs it.
- **Do not run `codex review`** — it runs pytest on the host test DB.
- **Do not run `codex exec` without `-s read-only`** — it is a coding agent and will
  edit the worktree.
- `railway run` in a fresh worktree fails with "No linked project found".
- Merging to `main` (backend) or `master` (frontend) **deploys** — Railway and Vercel
  respectively.
- `.env.example` is deny-ruled in this environment.
