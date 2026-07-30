> **STATUS — read this first.** Written 2026-07-28, before any of it was executed.
> **Phases 1, 2, 3, 4, 5a and 5b are DONE and live in production.** The "verified defects"
> section below describes the codebase as it was on 2026-07-28; most of those are now fixed.
> For what actually shipped, what was deliberately deferred, and what to do next, read
> **`docs/HANDOFF-responsive-sweep-2026-07-30.md`** — this file is the scope contract, that one is
> the progress log.
>
> One error in this brief, corrected during phase 2: it claims `PhoneCell.tsx` and `EmailCell.tsx`
> both render contact data as plain text. `EmailCell` already had `mailto:` links. Only `PhoneCell`
> lacked `tel:` — now fixed.

# BridgeLeads — Responsive Refactor Brief

You are a Senior Staff Frontend Engineer working on **BridgeLeads**, a production multi-tenant SaaS
that sells motivated-seller real-estate leads. This is **real production software with paying
users** — not a demo. Preserve every existing feature, brand token, and behaviour. Do not redesign.

---

## 0. Who actually uses this, and where

This is the context that decides every judgement call below.

BridgeLeads users are **real-estate wholesalers, flippers and agents**. They are not at a desk.
They are in a car outside a property, at a courthouse, or walking a lot — **on a phone, one-handed,
on cellular**. A wholesaler pulls up today's leads, reads a party name and address, and *taps to
call the owner*. That is the money path.

So the priority order is not "make everything responsive equally":

1. **Lead consumption on a phone** — `/results/[id]` and `/results`. If this is unusable on a
   phone, the product is unusable. Highest value in the whole effort.
2. **Job monitoring on a phone** — `/live/[id]`, `/dashboard`. Checking "did my scrape finish".
3. **Setup flows** — `/scrapers/new` wizard, `/settings`, `/deliver`. Done once, usually at a desk,
   but must not be *broken* on mobile.
4. **Marketing + auth** — public conversion surface. Already largely responsive.
5. **Admin** (`/admin/*`) — internal, desktop-acceptable. Lowest priority; say so rather than
   burning effort here.

**Product-specific mobile wins the generic checklist would never surface:**
- Phone numbers in `PhoneCell.tsx` should be `tel:` links on touch devices, email in `EmailCell.tsx`
  `mailto:`. Tap-to-call is the core action of the product and it is currently a text cell.
- Party names, property addresses and mailing addresses are long strings that **must not be
  truncated into uselessness** on a phone. A wholesaler needs the whole address.
- Skip-trace phone/email are **PII**. Do not add anything that logs, copies to a third party, or
  exposes them in a URL while making them tappable.

---

## 1. The actual stack (do not guess — this is verified)

| Thing | Reality |
|---|---|
| Framework | **Next.js 16**, App Router, route groups `(auth)` `(dashboard)` `(marketing)` |
| Styling | **Tailwind v4, CSS-first** — tokens live in `app/globals.css` under `@theme inline`. A `tailwind.config.ts` exists but v4 semantics apply. |
| Charts | **recharts 3.8** — `LeadsTrendChart`, `TopCountiesBars`, `RecordTypeMix` |
| Motion | **framer-motion 12** |
| UI primitives | shadcn-style, 20 files in `components/ui/` |
| Repo | `bridgeleads-web` (frontend). Default branch is **`master`**, not `main`. |
| Deploy | Vercel, auto-deploys on push; every PR gets a preview URL |

**Current responsive coverage: 54 of 134 `.tsx` files use any responsive prefix (~40%).** That is
the honest baseline. It is not a greenfield and it is not a disaster.

---

## 2. Verified defects — start here, these are real

These were found by reading the code, not by pattern-matching a checklist. Fix these first; they
are worth more than a sweep.

### P1 — `components/ui/dialog.tsx` has no viewport containment
There is **no `max-h`, no `overflow` handling** anywhere in the dialog primitive. Any modal whose
content exceeds the viewport will overflow with no internal scroll and no reachable footer buttons.
On a 320–667px phone this strands the primary action. Every modal in the app inherits this.
Fix once in the primitive: cap at `max-h-[85dvh]`, scroll the body internally, keep header/footer
pinned and the close control always reachable.

### P1 — `ResultsTable.tsx:54` uses a magic desktop viewport offset
```tsx
<div className="overflow-x-auto max-h-[calc(100vh-340px)]">
```
Two bugs in one line. `100vh` is wrong on mobile browsers (URL bar collapse causes clipping and
jump) — should be `dvh`. And `340px` is a hardcoded assumption about desktop header/toolbar/
pagination stack height that is simply untrue at any other breakpoint. This is the single most
important table in the product.

### P2 — the leads table is the product, and its width is dynamic
`ResultsTable` renders a **variable** column set: base columns plus `hasTaxData`
(Tax Balance Owed, Oldest Tax Year) plus `hasAuctionData` (Auction Date, Default Owed), plus
`dynamicColCount`. So worst-case width is much wider than the default view suggests — and it is
data-dependent, meaning it will look fine in testing and break for a user with tax + auction leads.
Horizontal scroll alone is not a sufficient answer on a phone for the app's core screen.
Decide deliberately between: sticky first column (Party Name) + horizontal scroll, or a stacked
card layout below `sm`. Justify the choice; do not just leave `overflow-x-auto` and call it done.

### P2 — sticky header inside a clipped parent
`ResultsTable` puts `sticky top-0` inside a parent with `overflow-hidden` + a scroll container.
Verify the sticky header actually sticks while scrolling on iOS Safari and Android Chrome — this
combination is a classic silent failure.

### P3 — fixed pixel widths concentrated in marketing
`w-[…px]` / `min-w-[…px]` appear across `(marketing)/pricing`, `(marketing)/coverage`,
`_monopo/*`, plus `NotificationsBell.tsx`, `UserMenu.tsx`, `deliver/page.tsx`. Convert to fluid
or breakpoint-scoped values where they cause overflow. **Do not blanket-replace** — some are
intentional (icon boxes, avatars).

### P3 — tablet gets the desktop sidebar
`Sidebar.tsx:48` is `hidden md:flex fixed`. So from **768px up** the full fixed sidebar shows, and
`MobileDrawer` handles below it. Verify 768–1024px: a fixed sidebar plus content plus a wide table
at 768px is tight. Consider whether the breakpoint should be `lg`.

---

## 3. Breakpoint contract

Adopt these explicitly and use them consistently. Mobile-first: unprefixed = smallest.

| Range | Target | Layout intent |
|---|---|---|
| 320–479 | small phones | single column, drawer nav, stacked cards, full-width inputs |
| 480–767 | large phones | single column, more breathing room |
| 768–1023 (`md`) | tablets | 2-col grids; **decide sidebar vs drawer here** |
| 1024–1439 (`lg`) | laptops | sidebar + content, multi-col |
| 1440–1919 (`xl`) | desktops | current design target |
| 1920+ (`2xl`) | large/4K | cap content width; do not let cards stretch |

**320px is a hard floor.** Nothing may horizontally scroll the page body at 320px.
Wide data tables scroll *inside their own container* — that is intentional and allowed.

Use `dvh`, not `vh`, for anything viewport-height-based. Mobile browser chrome makes `vh` lie.

---

## 4. Scope, in priority order

Work through these as **phases**. Do not start a new phase until the previous is verified.

**Phase 1 — primitives (highest leverage, smallest diff).**
`components/ui/` — `dialog`, `table`, `card`, `dropdown-menu`, `popover`, `input`, `button`.
Fixing containment and touch targets here fixes every consumer at once.

**Phase 2 — the leads path.** `results/page.tsx`, `results/[id]/page.tsx`, `ResultsTable`,
`ResultsToolbar`, `ResultsPagination`, `PhoneCell`, `EmailCell`. Includes tap-to-call/email.

**Phase 3 — shell + dashboard.** `Sidebar`, `MobileDrawer`, `TopBar`, `CommandPalette`,
`Breadcrumbs`, `dashboard/page.tsx`, `KpiStrip`, `ScrapersTable`, and the three recharts charts.

**Phase 4 — job + batch views.** `live/[id]` (682 LOC), `batches/[id]`, `scrapers/[id]/records`.

**Phase 5 — setup flows.** `scrapers/new` wizard (612 LOC + `_steps/CountyStep` 544,
`DeliveryStep` 524, `ScheduleStep` 382, `FieldsStep` 254) and `StepIndicator`; `segments`,
`deliver`, `settings`.

**Phase 6 — auth + marketing + errors.** `(auth)/*` + `AuthShell`, `(marketing)/*`,
`error.tsx` / `global-error.tsx` / `not-found`.

**Phase 7 — admin.** `admin/connectors`, `admin/funnel`. Lowest priority.

---

## 5. Rules of engagement (this repo, specifically)

- **Isolate the work.** Other Claude sessions are active in this repo and there are open PRs.
  Use a dedicated git worktree off `origin/master`. Never delete or force-move branches; never
  push to another session's branch.
- **Max ~5 files per phase**, per `CLAUDE.md`. Commit each phase separately with a real message.
- **Do not redesign.** The teal `#007f80` rebrand and the monopo marketing work are recent and
  deliberate. Preserve colours, type scale, motion, and the KPI hero cell. Responsiveness only.
- **Do not touch `_monopo/*` structurally** if a redesign is still in flight — check for open PRs
  touching those files first and coordinate rather than collide.
- **No mock/dummy data. No placeholder components.** Production repo.
- **Accessibility is in scope where it overlaps**: ≥44×44px touch targets, visible focus rings,
  contrast preserved (the codebase already documents AA ratios — e.g. the KPI hero gradient
  comments in `KpiStrip.tsx`; do not regress them).
- **Do not regress the OpenAPI type gate.** If backend types change, `lib/api-types.generated.ts`
  must be regenerated via `npm run gen:api-types`.

---

## 6. Verification — required, not optional

A phase is not done until all of these pass. "It looks fine" is not evidence.

```bash
npx tsc --noEmit          # must exit 0
npx eslint .              # must exit 0 on touched files
npm run build             # catches layout-breaking build errors
```

Then **actually look at it** at 320, 375, 768, 1024, 1440 and 1920px — via the Vercel preview URL
on the PR, or Playwright/Chrome DevTools. Capture before/after evidence for the screens you
changed. Per `CLAUDE.md` "Proving Work": screenshots, logs or test output — not assertions.

**Consult Codex before implementing each phase and have it review each diff** (standing workflow in
`.claude/rules/codex-collaboration.md`). Any Critical/High from either reviewer = do not merge.

---

## 7. Definition of done — checkable, not vibes

For every route in `app/`:
- [ ] No horizontal scroll of the page body at 320px (containers may scroll internally).
- [ ] No clipped or overlapping text; long party names, addresses, emails and URLs wrap or
      truncate *with the full value still reachable*.
- [ ] Every modal fits `85dvh`, scrolls internally, and its primary button is reachable at 320px.
- [ ] Every table is either horizontally scrollable with a sticky identifying column, or stacks
      into cards below `sm` — and the choice is deliberate and documented in the PR.
- [ ] Every form is single-column below `md` with full-width inputs and visible validation.
- [ ] Charts resize without label collision; tooltips work on touch.
- [ ] Touch targets ≥44×44px; visible keyboard focus retained.
- [ ] Content is width-capped at ≥1920px rather than stretching.
- [ ] No `vh` used where `dvh` is correct.
- [ ] `tsc`, `eslint`, `build` all clean; Codex review clean.

---

## 8. What to report back

After each phase: what changed, which breakpoints were verified and how, before/after evidence,
anything deliberately deferred and why. If a screen genuinely cannot be made good on a phone
without a product decision (e.g. the leads table column strategy), **stop and ask** rather than
guessing — that is a product call, not a CSS call.
