# Public repo exposure: remediation plan (2026-10-31)

Status: PLAN ONLY. The owner chose "plan only, decide later" for cleanup. Nothing
below the "Done" section has been executed. No secret values appear in this file.

## What happened
- `Abenezer1244/web-scrapper-automation` was **public** from some point after
  2026-09-25 (audit N-01 still listed "repo is private" then) until **2026-10-31**,
  when it was set private with owner approval.
- Brave Search had indexed PR pages (#425, #415) for the query "bridgeleads".

## Done
- [x] 2026-10-31: repo set PRIVATE (`gh repo edit --visibility private`).
      Anonymous checks of the repo API, PR #425, PR #415 and docs contents all return 404.

## Step 1: rotate credentials (OWNER, do first; going private does not undo clones/caches)
- [ ] **Cloudflare API token** (scope per `infra/terraform/main.tf`: DNS + R2 + WAF).
      Cloudflare dashboard → My Profile → API Tokens → Roll. Update wherever it is used
      (Terraform workspace / local tfvars, never committed again).
      Then review Cloudflare → Manage Account → Audit Log back to 2026-03-17 for
      unexpected DNS/WAF/R2 changes. Also note: older GHCR images contained the
      file (`COPY . .`); GHCR is private now, but previously pulled images keep it.
- [ ] **`admin@bridgeleads.io` password**: set a long random password and enable 2FA if
      supported. It is written in `docs/security/SECURITY_CHECKLIST_AUDIT_2026-06-08.md:88`,
      `docs/superpowers/plans/2026-06-19-dashboard-analytics-phase3.md` and
      `tasks/phase-b-doctype-select.md`. It was also printed in a Claude session transcript
      on 2026-10-30.
- [ ] **Account in `docs/HANDOFF-real-owner-location-2026-09-02.md:144-145`**:
      change the password and revoke sessions (bump the token version / sign out everywhere).
- [ ] Consider rotating anything else that is reachable via the exposed origin IPs,
      zone ID and account ID (those IDs are not secrets alone, but they help an attacker).

## Step 2: remove the exposures from the current tree (proposed PR, needs owner go-ahead)
- [ ] Redact the password lines from the three docs above and the HANDOFF doc.
- [ ] Redact personal Gmail addresses and prod user IDs from `docs/BUILD_JOURNAL.md`,
      the five HANDOFF docs and `tasks/todo-entitlement-periods.md`.
- [ ] Keep `infra/terraform/terraform.tfvars` in `.gitignore` (verify it is) and add a
      `terraform.tfvars.example` with placeholders.
- [ ] Add gitleaks to CI (handle the UTF-8 BOM the tfvars file had).

## Step 3: stale branches (221 of 440 remote branches still carry terraform.tfvars)
Option A, lower risk (recommended): delete merged and stale remote branches.
1. List branches carrying the file:
   `for r in $(git for-each-ref --format='%(refname:lstrip=3)' refs/remotes/origin); do git cat-file -e "origin/$r:infra/terraform/terraform.tfvars" 2>/dev/null && echo $r; done`
2. Cross-check against open PRs (`gh pr list --state open`) and the active worktrees
   (`git worktree list`). Never delete a branch that a worktree or open PR uses.
3. Rollback: before deleting, record `git for-each-ref refs/remotes/origin > branches-backup.txt`
   (name + SHA). A deleted branch can be restored with `git push origin <sha>:refs/heads/<name>`
   as long as the objects still exist locally.
4. Delete with `git push origin --delete <branch>` in reviewed batches.

Option B, full purge: `git filter-repo --path infra/terraform/terraform.tfvars --invert-paths`
plus a force-push of all refs. This breaks every clone and worktree (about 17 local
worktrees) and closed PR diffs. Do it only if Option A plus rotation is judged insufficient.
It needs a separate explicit owner go-ahead at execution time.
Note: GitHub keeps PR refs (`refs/pull/*`) and cached views. Fully removing them requires a
GitHub Support request ("remove sensitive data") after the rewrite.

## Step 4: search-engine cleanup
- [ ] The GitHub URLs now 404, so engines drop them on recrawl.
      To speed this up: Google Search Console → Removals → "Outdated content" tool
      (https://search.google.com/search-console/remove-outdated-content) for
      `github.com/Abenezer1244/web-scrapper-automation/pull/425` and `/pull/415`.
      Bing: Webmaster Tools → "Content Removal" tool. Brave: no public removal tool;
      it drops 404s on recrawl.
- [ ] Re-check `site:github.com/Abenezer1244 bridgeleads` in about 2–4 weeks.

## Step 5: fix the open vulnerabilities the docs described (separate tickets)
These were public. Treat them as known to attackers:
- S3-04 / F-01: IP rate limiting ineffective in production (lockout never fires).
- S3-06: Railway origin reachable directly, bypassing Cloudflare (firewall to CF IPs).
- S3-14 / E-4: browser SSRF guard fails open (`base_scraper.py`).
- S3-03 / F-12: trial accounts can run unbillable Tracerfy lookups.
- N-02: plan-change arbitrage.
- F-06: no DMARC.
Verify each against current code before ticketing; some may already be fixed.

## Side effect of going private
- Frontend `npm run gen:api-types` fetches the backend schema from a raw URL and now
  fails. Point it at `gh api` or a local path (CI already uses BACKEND_SCHEMA_TOKEN).
- Actions minutes are now billable (about 2,300 runs to date). Watch the billing page.
