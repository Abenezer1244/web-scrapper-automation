# HANDOFF — Responsive sweep (+ Supabase outage fallout) — 2026-07-30

Written for a **fresh session with zero context**. Everything below is verified, not remembered.
Two repos are involved. Nothing is mid-edit; all work is merged and deployed.

---

## 1. The goal

Make the **BridgeLeads frontend fully responsive**, mobile-first, 320px → 4K, **without redesigning**
anything. Preserve branding (teal `#007f80`), density, motion, and every feature.

The user brought a generic ChatGPT-written "responsive refactor" prompt and asked for it to be
rewritten to fit BridgeLeads specifically, then executed **phase by phase**, consulting Codex on
every step. That rewritten brief is the source of truth for scope and is reproduced in §7.

**Why this matters (the context that drives every judgement call):** BridgeLeads users are
real-estate wholesalers standing outside a property, one-handed, on a phone. The money path is
*open the lead list → read party name + property address → tap to call the owner*. Priority order
is therefore: leads screen > job monitoring > setup flows > auth/marketing > admin. Admin is
explicitly desktop-acceptable.

---

## 2. Where we are RIGHT NOW

**Phases 1–5b are merged and live in production.** Nothing is in flight. Nothing is broken.

### Frontend — `bridgeleads-web`, default branch `master`
```
4568b7b  phase 5b — touch-unreachable tooltips, deliver + settings   (#92)
53c9589  ← other session: tell apart scrapers that share a name      (#89)
3f81c40  phase 5a — new-scraper wizard                               (#91)
c615d7e  phase 4 — job, batch and records views                      (#90)
deb733e  phase 3 — shell breakpoint, toolbar overflow, touch targets (#88)
2a31ed2  phase 2 — mobile lead cards + tap-to-call                   (#87)
93cca8e  phase 1 — viewport containment + touch targets              (#85)
2761870  ← other session: scraper name mandatory                     (#81)
```

All my PRs are **MERGED**: `#82, #83, #85, #87, #88, #90, #91, #92`.
Vercel auto-deploys `master`; the last production run succeeded.

### Backend — `web-scrapper-automation`, default branch `main`
My backend PRs `#155, #156, #157` are **MERGED**. `main` has since moved on via other sessions
(`#158`–`#161`), so **always `git fetch` before branching.**

### Open frontend PRs that are NOT mine
`#69, #51, #43, #38, #37, #36, #35, #34` — all June-era work from other sessions (Darkmatter
Phase 6, UI polish, landing crossfade). **Do not touch them.** Several touch the same files a
future phase would, so check for overlap before editing marketing or settings-tab files.

---

## 3. How this session started (important — the first half was NOT responsive work)

The user said *"i was trying to login as admin"* and got **"Something went wrong. Please try again."**

**It was not a login bug. The production database had vanished.**

`railway logs --service api` showed:
```
asyncpg.exceptions.InternalServerError:
  (ENOTFOUND) tenant/user bridgeleads_app.xqbrqvodxbursjjjlmjn not found
```
Supavisor rejected **both** DB roles (`bridgeleads_app` for api, `bridgeleads_system` for worker)
and `xqbrqvodxbursjjjlmjn.supabase.co` returned **NXDOMAIN** → the Supabase **project** was
paused/deleted. Dashboard-only fix, no deploy. The user restored it; confirmed by `/auth/login`
returning **401** instead of 500.

🔑 **Reusable diagnosis:** `(ENOTFOUND) tenant/user <role>.<project_ref> not found` = Supabase
project paused/deleted, NOT a credential problem. Confirm in seconds with
`nslookup <ref>.supabase.co` → NXDOMAIN while the pooler host still resolves.

That produced three backend PRs (all merged):
- **#155** — `/health` was a hardcoded `{"status":"ok"}` that returned **200 through the entire
  outage**, so a human hitting a login form was the monitoring. Split into `/health` (liveness,
  dependency-free so you can still deploy mid-incident) and new **`/ready`** (real `SELECT 1`,
  503 when down). Cached 10s behind an `asyncio.Lock` — mandatory, because the async engine is
  `NullPool`, so every probe is a fresh TCP+TLS+auth connection to Supabase and an uncached
  unauthenticated `/ready` would be a connection-exhaustion amplifier.
  🔑 **Redis is deliberately excluded** from readiness: `rate_limit()` fails open to a per-process
  limiter (`src/api/middleware/rate_limit.py:154`), so Redis down ≠ login down.
- **#156** — pypdf `6.13.3 → 6.14.2`, four crafted-PDF DoS CVEs. **Reachable**, not theoretical:
  `src/scrapers/sources/nts_pdf.py:88` calls `extract_text()` on externally-fetched county PDFs,
  the exact CVE trigger. The 25 MB and `max_pages` caps bound memory but cannot break an infinite
  loop inside one page. It was also failing `pip-audit` on **every** PR.
- **#157** — `docs/BUILD_JOURNAL.md` entry for the above.

Then a small FE task (`#82`, remove the KPI cards' coloured top strip) surfaced `#83`: adding
`/ready` changed `schema/openapi.json`, which the frontend generates
`lib/api-types.generated.ts` from under a CI drift gate — so **every** frontend PR started failing
until the types were regenerated. **Remember this coupling: any backend schema change requires
`npm run gen:api-types` in the frontend.**

---

## 4. Conventions established across phases 1–5b — FOLLOW THESE

These were decided with Codex and are now consistent across the whole app. A future phase that
breaks them will look wrong.

| Rule | Why |
|---|---|
| Touch sizing via **`pointer-coarse:`** only, never breakpoints | Small viewport ≠ touch. Pseudo-element hit-area expansion was **rejected**: a negative-inset `::after` reserves no layout space, so in dense rows, `button-group`s and `overflow-hidden` table cells neighbouring controls steal or clip each other's taps. |
| **`dvh`**, never `vh` | Mobile browser chrome makes `vh` lie |
| **`min-w-0`** on flex children that must shrink; **`shrink-0`** on ones that must not | A flex child defaults to `min-width:auto` and **refuses to shrink below its content** — this was the root cause of the TopBar overflow |
| **Cap-then-scroll** (`max-h` + `overflow-y-auto`) for anything that can exceed the viewport | Applied to dialog, dropdown, popover, batch download menu |
| **`flex-col sm:flex-row`** instead of letting `justify-between` squeeze | |
| Page padding **`p-4 sm:p-8`** | `p-8` leaves only 256px at 320px |
| Breakpoints: sidebar at **`lg`** (not `md`) | 240px sidebar at 768px left only 528px of content |
| Mobile-card treatment **only when a table separates identity from contact/action** | Phase 2 (leads) yes; Phase 4 (records, 7 cols, no contact col) deliberately no |

🔑 **The shared `btn-amber` / `btn-ghost` / `input-base` classes ALREADY meet 44px.** Only
hand-rolled raw controls have needed raising. Three separate phases found the real defect was
markup bypassing the design system — check for that pattern first.

---

## 5. Verification method — and its one real gap

`bridgeleads-web` has **no test runner**. So each phase was verified by:
```bash
npx tsc --noEmit          # must exit 0
npx eslint <paths>        # must exit 0
npm run build             # must be clean
```
…plus **grepping the compiled CSS in `.next`** to prove each Tailwind class actually emitted.

🛑 **That last step is not optional and it earned its keep.** In phase 1 the first attempt used
`max-h-[min(85dvh,calc(100dvh-2rem))]`, which **Tailwind silently dropped** — zero rules emitted.
It would have shipped looking correct and doing nothing. Flattened to `max-h-[85dvh]`.

Use Python, not grep, to check emitted CSS — the `\:` escaping in Tailwind's output defeats shell
quoting and I got false zeros three times:
```python
import pathlib
css="".join(p.read_text(errors="ignore") for p in pathlib.Path(".next").rglob("*.css"))
print(css.count("max-height:85dvh"), css.count("pointer-coarse\\:h-11"))
```

### ⚠️ THE GAP — tell the user about this
**Nothing in this sweep has been looked at on a real device or in a browser.** Six phases passed
type-check, lint, build and compiled-CSS checks, and every Vercel preview was green — but that
proves the CSS *rules exist*, not that the screens *read well*. The user was asked three times to
spot-check a preview and has not yet done so. The highest-value single check is: **tap a locked
record-type chip in the wizard on a phone** — that explanation was previously impossible to see
and is now the whole point of phase 5b.

---

## 6. What changed, phase by phase (with the reasoning, so you don't undo it)

### Phase 1 — `components/ui/` primitives (#85)
- `dialog.tsx` had **no height cap and no overflow at all** → a modal taller than the viewport ran
  off-screen with its **primary button unreachable**. Now `max-h-[85dvh]` + internal scroll.
- `dialog.tsx` `w-full` → `w-[calc(100vw-2rem)]`: at 320px it rendered flush to both screen edges.
- `dropdown-menu.tsx` / `popover.tsx`: `overflow-hidden` with no cap → long menus (county picker)
  extended past the viewport unreachably. Capped to Radix's
  `--radix-*-content-available-height` + `collisionPadding={8}`.
- `button.tsx`: every size was 24–36px → raised under `pointer-coarse:`. Sizing is **graduated,
  not flat-44**: `xs`/`icon-xs` land at 36px because forcing 44 would reflow dense table rows.

### Phase 2 — leads screen (#87)
- The table is **11 `nowrap` columns** with tax+auction data (>1400px vs 320px). Scrolling to Phone
  scrolled Party Name off — you couldn't tell whose number you were dialing. **Correctness bug.**
- New `app/(dashboard)/results/[id]/_components/LeadCards.tsx` replaces the table below `sm`.
- `PhoneCell.tsx` — the number was a plain `<span>`; now a **`tel:` link** (digits-only href).
  `EmailCell.tsx` already had `mailto:` and was left alone.
- `max-h-[calc(100vh-340px)]` → `sm:max-h-[70dvh]` (the 340px was a hardcoded desktop guess).
- 🛑 **Codex caught a P2 that falsified my own PR claim.** I wrote "nothing is dropped"; the card
  was missing `assessor_current_owner` + title badges, `legal_description`, assessed value / year
  built / land use / sq ft, and `instrument_number`. Fixed, and **parity is now verified
  mechanically** by extracting every `row.*` field each file references and diffing the sets.
  **If you edit either view, re-run that diff.** `formatDocType` was moved to `_lib.ts` so both
  views share one implementation.

### Phase 3 — shell (#88)
- Sidebar `md` → `lg`. **Six coupling points**; miss one and 768–1023px breaks with a dead 240px
  gutter, missing drawer, or hidden hamburger: `Sidebar.tsx:48`, `ShellMain.tsx:20`,
  `TopBar.tsx:19`, `TopBar.tsx:32`, `MobileDrawer.tsx:43`, `MobileDrawer.tsx:59`.
  `CommandTrigger`'s `md:` split is intentionally left alone (search affordance, not sidebar).
- TopBar overflow: added the `min-w-0` / `shrink-0` chain. Breadcrumbs now show **only the current
  crumb below `md`** — earlier crumbs aren't links, so they spent scarce width while the current
  location got pushed out.
- Trial banner keeps its CTA visible and tappable (revenue surface); only the qualifier copy drops.
- Raw shell controls raised to 44px on coarse: `CommandTrigger`, `NotificationsBell`, `UserMenu`
  avatar, `MobileDrawer` close, `NavItem` rows, drawer settings row.
- `NotificationsBell` hardcoded `width: 340` — **wider than a 320px viewport**. Now clamped.

### Phase 4 — job / batch / records (#90)
- `live/[id]` stats row was `grid-cols-3` with no breakpoint (~58px content per card at 320px)
  → `grid-cols-2 sm:grid-cols-3`, third card spans both columns.
- `batches/[id]` custom download menu: absolute div, `overflow-hidden`, **no height cap** → items
  outside the viewport with no scroll. Given the cap-then-scroll contract.
- `components/log-stream.tsx`: fixed `h-80` + long unbroken mono strings with no wrapping rule
  → `min(20rem,50dvh)` + `break-words` + `overflow-wrap:anywhere`.
- `p-8` → `p-4 sm:p-8` on all three pages.
- **Deliberately NOT done:** card treatment for the records table (7 cols, no contact column).

### Phase 5a — wizard (#91)
- `StepIndicator` was ~504px vs ~288px usable at 320px, no wrap, no scroll. Below `sm` it now
  collapses to `"<current step name>" + "Step 2 of 4"` + a segmented progress bar. Horizontal
  scroll was rejected (progress shouldn't require discovery); dots-only was rejected (loses which
  step you're on).
- County rows: `min-w-0 truncate` on the name, `shrink-0` on the trailing badge/dot.
- Stacking on the Single/Batch toggle, date inputs, time controls, email+Add row, webhook header.

### Phase 5b — unreachable content (#92)
- `Chip.tsx` tooltip was `opacity-0` + `group-hover:opacity-100` + `pointer-events-none` → **never
  appeared on touch**, and worst on **locked** chips whose button was natively `disabled` and thus
  unfocusable. Now uses the shared `Popover` (phase-1-hardened) with **`aria-disabled` instead of
  `disabled`**. No call sites changed.
- `CountyStep` had the identical bug on the Batch button → replaced with static helper text.
  This *does* change desktop behaviour (text instead of hover tooltip) — a deliberate trade.
- 🔑 **Audited the whole codebase for the pattern:** `PhoneCell.tsx` and `scrapers/page.tsx` both
  already carry `[@media(hover:none)]:opacity-100`, so their hover reveals are **correct** — leave
  them alone. No other real instances.
- `deliver/page.tsx`: `emails.join(", ")` had **no cap at all**; added truncation + shrink chain.
- `settings/page.tsx`: I claimed it was "already responsive" — **wrong**. Layout was fine but tab
  buttons were ~40px, padding was `p-6 lg:p-8`, and it used stale `flex-shrink-0`. All corrected.
- 🛑 **Codex caught a regression I introduced:** the Single/Batch toggle is wrapped in
  `selectedState && (...)` but my replacement text wasn't, so a free-tier user with no state
  selected would see a floating batch upsell with no control above it. Fixed in `50c97a4`.

---

## 7. NEXT STEPS — in order

### 7a. Phase 5c — `segments/page.tsx` mobile cards  ← START HERE
`app/(dashboard)/segments/page.tsx` (550 lines). Codex verified its table at **lines 480–521**
separates identity (**502–506**) from contact fields (**511–514**). By the rule from phases 2 and 4
that means it needs the **mobile-card treatment**, not horizontal scroll. Model it on
`LeadCards.tsx`. Also: raw date inputs at **348–358** and **363–373** are below touch size.
Estimated 1 new component + 1 file edit.

### 7b. Phase 6 — auth, marketing, error pages
`app/(auth)/*` + `_components/AuthShell.tsx`, `app/(marketing)/*`, `error.tsx`,
`global-error.tsx`, `not-found`. ⚠️ **Check the open Darkmatter Phase 6 PRs (#34–#38) and #69
first** — they touch marketing and settings files and would collide.

### 7c. Phase 7 — admin
`admin/connectors`, `admin/funnel`. Lowest priority; desktop-acceptable by the brief.

### 7d. Not yet audited at all
`components/settings/*` — **1917 lines across 7 files** (`BillingTab` 516, `security-tab` 463,
`AccountTab` 267, `ReferralsTab` 179, `ApiKeysTab` 180, `NotificationsTab` 166,
`DeliveryTab` 146). Scoped out of 5b to respect the file budget. Note **PR #35** (Darkmatter
settings) is open against these.

### 7e. Carried over from the outage work — needs a human, not code
1. 👤 **Nothing polls `/ready`.** It makes an outage *observable*, not *alerting*. Needs an external
   uptime monitor on `https://api.bridgeleads.io/ready` alerting on 503 for 1–2 min. Requires an
   account → ops action. **Until this exists, the next outage surfaces exactly as this one did.**
   🛑 Do **not** wire it to the in-repo Prometheus stack: there is **no `/metrics` endpoint** and
   `prometheus_client` is not in `requirements.txt`, so `monitoring/prometheus.yml`'s `fastapi`
   job is dead config. (Codex suggested it; I checked and overrode it on evidence.)
2. **`start.sh:61`** runs `python scripts/migrate.py || exit 1` **before** uvicorn, so during a DB
   outage a new deploy cannot boot at all — precisely when you'd want to ship. Worth deciding
   whether migrations belong in a release step.
3. **Deferred P2s from phase 4:** batch child rows + run-history rows can still squeeze below `sm`;
   `BatchLeadsTable.tsx` is a defensible card candidate (its Contact column sits far right).

---

## 8. Failed attempts / dead ends — don't repeat these

| What | Outcome |
|---|---|
| `max-h-[min(85dvh,calc(100dvh-2rem))]` | **Tailwind silently emitted nothing.** Nested `min(…, calc(…))` with a comma isn't parsed. Use flat values. |
| Pseudo-element hit-area expansion for touch targets | Rejected by Codex with codebase-specific reasoning: no reserved layout space → neighbouring controls steal/clip taps in `button-group`s and `overflow-hidden` cells. Use `pointer-coarse:`. |
| Including **Redis** in `/ready` | Wrong. `rate_limit()` fails open, so Redis down ≠ login down. Would be a false red on the main customer path. |
| Wiring `/ready` into the in-repo Prometheus stack | Codex's suggestion; **factually impossible** — no `/metrics` endpoint, `prometheus_client` absent. |
| Claiming "nothing is dropped" in the lead cards | False. Verify field parity mechanically (§6, Phase 2). |
| Claiming `settings/page.tsx` was "already responsive" | False. Layout was fine; touch targets and conventions were not. |
| Two local **full pytest** runs (backend) | Both invalid — **self-inflicted**: I started portable Postgres as a child of a background job, so the harness reaping the job killed Postgres mid-run (`pg.log`: `terminating any other active server processes`). 137 then 54 failures, all connection-shaped. **Don't start the test Postgres from inside a background job.** |
| `curl -w '/path …'` in Git Bash | Path-converted to `C:/Program Files/Git/...`, producing a meaningless reading. Use `export MSYS_NO_PATHCONV=1`. |
| `grep -c 'sm\\:hidden'` on compiled CSS | False zeros from backslash escaping (3×). Use Python (§5). |
| `git worktree remove` on Windows | `Permission denied` while a handle is held; git deregisters but leaves the directory. `rm -rf` then `rmdir`. |

---

## 9. Environment / workflow facts

- **Two repos, different default branches:** frontend `bridgeleads-web` → **`master`**;
  backend `web-scrapper-automation` → **`main`**.
- **Other Claude sessions are active in both repos.** Always work in a **dedicated worktree off a
  freshly fetched default branch**, and **never delete or force-move branches** (shared OneDrive
  repo; a concurrent session can advance your tips). Additive worktree + push only.
- **FE worktree `node_modules`:** create a junction to the main checkout —
  `cmd /c mklink /J "<worktree>\node_modules" "<main>\node_modules"`. It worked from Git Bash this
  session (verify with `ls node_modules/.bin/tsc`); a memory note claims PowerShell
  `New-Item -ItemType Junction` is required, so check rather than assume.
- **Squash-merging a stack duplicates commits.** Merge the base PR, then `git merge origin/master`
  into the next branch **and verify the earlier phase's changes survived** before retargeting.
  `git merge-base --is-ancestor` will always say "not merged" after a squash — check PR state.
- **Codex:** `codex review` takes **either** a prompt **or** `--base`, never both. Use
  `-c mcp_servers={}` and `< /dev/null`. Long reviews exceed 600s — run in background.
- **My worktree this session:** `C:/Users/Windows/bridgeleads-worktrees/responsive` (frontend).
  Branches all merged; safe to delete the worktree, but leave the branches.
- Backend full local pytest: `bash C:/Users/Windows/bl-testenv/run-full-pytest.sh [worktree]`.

---

## 10. The tailored brief (scope source of truth)

The BridgeLeads-specific responsive brief written at the start of this work — user context and
priority order, the real stack, verified defects, the breakpoint contract, per-phase scope, the
verification requirement and the definition of done — is committed alongside this handoff:

**`docs/RESPONSIVE-REFACTOR-BRIEF.md`**

Read it before starting a new phase. It is the scope contract; this handoff is the progress log.

Two notes on reading it, since phases 1–5b have since executed it:
- Its "verified defects" section lists what was true on 2026-07-28. **Most are now fixed** — see §6
  here for what shipped. Its per-phase scope in §4 is still accurate for phases 5c–7.
- One claim in it is **wrong** and was corrected during phase 2: it says `PhoneCell.tsx` and
  `EmailCell.tsx` both render contact data as plain text. `EmailCell` already had `mailto:` links.
  Only `PhoneCell` lacked `tel:`, and that is now fixed.

## 11. Standing user preferences observed this session

- Consult Codex **before** implementing and **review every diff** with it — not just as a PR gate.
- Fix root causes, never symptoms. Never guess; verify against the real system.
- Work in an isolated branch/worktree because other terminals are active.
- Clean up dead code as part of the work.
- The user replies in very short messages ("go", "merge", "proceed"). Treat them as approval for the
  thing just proposed — and if a proposal has two parts ("merge then continue"), honour both, but
  **"merge only" means stop after merging.**
