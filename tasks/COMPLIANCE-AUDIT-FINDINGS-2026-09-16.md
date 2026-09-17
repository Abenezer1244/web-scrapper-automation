# BridgeLeads compliance audit — lead's independently verified findings

Date: 2026-09-16. Companion to `COMPLIANCE-AUDIT-2026-09-16.md` (the plan).
Everything here I verified myself against the **live production site** or the
**deployed code**, not via sub-agents. Phase 1 = audit only; nothing changed.

Owner decisions received: marketing site audited live + dashboard from code;
target jurisdictions = **US with California in scope**.

---

## 1. Tracking / cookies — settled empirically against prod

- `https://bridgeleads.io/` makes **ZERO third-party network requests** (26 resources,
  all first-party). No analytics, no pixels, no iframes, no 1x1 beacons.
- Fonts are build-time self-hosted by `next/font/google`; **no request reaches Google**,
  so no visitor IP is disclosed to it. CSP allows `fonts.gstatic.com` but nothing uses it.
- **No `Set-Cookie` header at all** on the landing page; `document.cookie` empty;
  `localStorage` and `sessionStorage` both empty on load.
- `bl_cookie_ack` is written only when the banner is dismissed — the banner's own
  dismissal is the only browser storage the landing page causes.
- Security headers are strong: CSP `default-src 'self'`, HSTS 1y + includeSubDomains,
  `X-Frame-Options: DENY`, nosniff, referrer-policy, permissions-policy.
  Weakness: `script-src` carries `unsafe-inline` and `unsafe-eval`.
- Net effect: the banner copy "We use cookies to improve your experience" describes
  tracking the site does not do. That is inaccurate copy, NOT a fake-reject banner.
- Banner renders on `/` only — absent on `/pricing`, `/coverage`, `/privacy`, `/terms`.

## 2. Legal pages — unfilled placeholders live in production

Privacy (`/privacy`, "LAST UPDATED: JUNE 2, 2026") — 4 placeholders:
`[Legal entity name]` (opening), `[retention period]` (section 7),
`[country]` (section 10), `[legal entity name], [mailing address]` (section 11).

Terms (`/terms`, same date) — 5 placeholders, in the load-bearing clauses:
- `[LEGAL ENTITY NAME]` in **section 10 LIMITATION OF LIABILITY**
- `[legal entity name]` in **section 11 INDEMNIFICATION**
- `[state / country]` in **section 13 GOVERNING LAW**
- a published note-to-counsel: "[Dispute resolution / arbitration / venue clause to
  be set by counsel.]"
- `[legal entity name], [mailing address]` in section 14

Substantively the Terms are strong: section 5 covers TCPA/DNC, FCRA non-use,
data-broker law, distressed-owner and vulnerable-person protection, fair housing,
and no resale. Section 6 is a proper accuracy disclaimer. Age 18 + business use stated.

Gaps: the Terms never use the word "cancel" and never mention renewal. The only
refund stance is section 3, "Fees are non-refundable except where required by law."

## 3. Contact channel is undeliverable (DNS-verified, two resolvers)

Policies direct users to `privacy@`, `security@` and `legal@bridgeleads.com`.

- `bridgeleads.com`: **no MX record**, TXT `v=spf1 -all`, NameBright parking
  nameservers. A parked domain that explicitly declares it sends no mail, and
  apparently not this business's domain at all.
- `bridgeleads.io`: **no MX record** either.

So the designated channels for exercising privacy rights, reporting a
vulnerability, and serving legal notice all silently discard mail.

## 4. Retention: policy vs code

- `RECORD_RETENTION_DAYS = 365` — `src/config/settings.py:310`. Matches the policy figure.
- `_purge_old_records_impl` — `src/workers/scheduler_helpers/county.py:64-86`,
  weekly Sunday 03:00 UTC. Deletes from exactly two tables: `county_records` and
  `property_list_membership`.
- The only tables any `DELETE FROM` in `src/` ever targets: `delivered_records`
  (6 sites, dedup-claim release, not retention), `property_list_membership`,
  `pending_registrations`, `county_records`.
- Therefore **`results` — the tenant lead rows holding owner names, property and
  mailing addresses, and skip-traced phone numbers and emails — are never deleted
  by any code path.** Policy section 7's "Lead records are retained for
  approximately 365 days, then deleted" is false as applied to the lead and
  contact data that actually matters.
- `SKIP_TRACE_CACHE_DAYS = 90` (`settings.py:289`) is used as a reuse TTL at
  `src/workers/tasks_helpers/enrich.py:189`. It gates re-charging for a repeat
  lookup; it does not delete anything.
- **Grant-drift risk:** `scripts/verify_worker_delete_grants.py` documents that prod
  ALREADY lost `DELETE ON delivered_records` once, surfacing only as caught
  `InsufficientPrivilege` months later. The purge's DELETEs are exposed to the same
  drift, so even the 365-day `county_records` purge may be a silent no-op in prod.
  Owner should run that verifier to confirm.

## 5. Marketing claims — internal contradictions found live

- County count stated **three different ways**: hero "19 WA COUNTIES DAILY",
  FAQ "20+ Washington counties", coverage page "18 WASHINGTON COUNTIES, LIVE"
  (and it lists exactly 18). The coverage page is hardcoded, not API-driven
  (only `/api/auth/session` was requested), so it cannot self-correct.
  The true count is a **DB fact** in `county_connectors`, not verifiable from code.
- Record types: landing says "six types"; the coverage page and `ALL_RECORD_TYPES`
  (`src/config/constants.py:175`) both say **7**. Privacy section 3 also lists
  `eviction`, which is not in `ALL_RECORD_TYPES` (roadmap only).
- "Seven portal templates": there are **8** on disk in `src/scrapers/templates/`.
- Pricing: the annual toggle shows "$159/mo" etc. with **no annual total and no
  statement that it bills as one upfront payment**. The 20% discount math is
  correct ($199→$159, $499→$399, $1499→$1199).
- Pricing intro "Every plan reaches all supported counties" contradicts the same
  page's 1 / 3 / 10 / unlimited county caps.
- "TALK TO SALES" (Agency CTA) links to `/register`, not to sales.
- Earnings-style claims: "One closed deal covers a year", "One closed wholesale
  deal is worth $5k-$30k", "pays for itself on a single contract".
- Competitor price claim: "below PropStream's $0.12".
- "LIMITED SPOTS" urgency on FOUNDING25 with no stated number.
- No testimonials anywhere on the live site. `_sections/Testimonials.tsx` is dead code.

## 6. Accessibility (live, measured — colors resolved via canvas, not regex)

Genuinely good: `lang="en"`, one h1, **zero `<img>` elements** (all 18 SVGs correctly
`aria-hidden`), **zero** interactive elements without an accessible name, no positive
`tabindex`, 33 tab stops all reachable with a **visible focus indicator on every one**,
FAQ items are real buttons with `aria-expanded`/`aria-controls`, cookie banner is
keyboard-reachable and labelled, and no horizontal overflow at 320px.

- **P1 — the fixed nav becomes invisible at several scroll depths.** The nav switches
  white→black text at ~scrollY 3200-4200, but the page's dark/light bands do not
  align with that threshold. Measured contrast of the nav wordmark against what is
  actually painted behind it (nav hidden, sampled at 13 scroll positions):
  **1.0 at scrollY 900 / 1600 / 2400 / 3200** (white on white, ~2,300px of scroll),
  **1.0 at 6400** (black on black), **1.25 at 11800**. Affects the whole nav bar,
  including SIGN IN.
- **P1 — no way to sign in on a phone.** `Sign in` is `hidden md:block`
  (`_monopo/Nav.tsx:46`), absent below 768px; nav links are `hidden lg:flex`
  (line 35), absent below 1024px; and **there is no hamburger menu anywhere** in
  `Nav.tsx`. Verified: zero `/login` links present in the DOM at 320px, including
  the footer. The only visible CTA is START FREE TRIAL.
- P2 — 4 measured contrast failures besides the nav: muted `#6d6d6d` on the black
  integrations band = **4.06** (needs 4.5) at 12px and 14px; teal/black pairs in
  the sample-dashboard preview = **4.35**.
- P2 — 8 tap targets are 16px tall at 320px (footer links + wordmark), below the
  WCAG 2.2 SC 2.5.8 minimum of 24x24.
- P3 — **no `<main>` landmark and no skip link** on the landing page (the legal
  pages do have `<main>`). The hero h1 is duplicated verbatim as an h2. Body text
  as small as 10-11px in the footer and cookie banner. `/pricing` and `/coverage`
  reuse the generic page title.

## 7. Caveats on this audit

- `git diff origin/master...HEAD -- "app/(marketing)" app/layout.tsx` is **empty**,
  so the marketing code I read IS the deployed code, despite the local FE branch
  being `feat/schedule-day-picker`.
- CLAUDE.md documents a top-level `workers/` directory but the real path is
  `src/workers/`. Sub-agents were briefed with the stale path, which is why the
  retention findings above were verified by me directly.
- The `graphify` MCP server failed to connect this session (`CONNECTION_CLOSED`),
  so CLAUDE.md's "query the knowledge graph first" rule could not be followed.
- Another session (`terminal 1`) is busy in this same worktree — see the
  `two_agents_one_worktree` landmine. This audit wrote only these two task files.
- An early grep for retention logic was flooded by TTL noise in the auth middleware
  and truncated before reaching `purge_old_records`, briefly suggesting no retention
  existed. Re-run with narrower scope. Recorded because the repo has a standing
  landmine about exactly this.
