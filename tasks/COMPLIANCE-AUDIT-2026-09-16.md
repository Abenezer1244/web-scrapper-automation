# BridgeLeads — legal / privacy / accessibility / consumer-protection audit

Date: 2026-09-16. Phase 1 = AUDIT ONLY. No deploys, no legal text published,
no billing changes, no data changes, no trackers added, no deletions.

Note: `tasks/todo.md` still holds the in-flight Tracerfy skip-trace task, so this
audit gets its own file rather than clobbering it.

Note: the `graphify` MCP server failed to connect this session
(`CONNECTION_CLOSED`), so CLAUDE.md's "query the knowledge graph first" rule
could not be followed. Falling back to direct file reads + `graphify-out/` on disk.

## Scope

Two repos, both real production code:
- BE `Desktop/web-scrapper-automation` (FastAPI / Celery / Playwright / Postgres)
- FE `Desktop/bridgeleads-web` (Next.js 16, next-auth v5 beta) — branch `feat/schedule-day-picker`

## Phase 1 plan

### Inventory (parallel sub-agents, read-only)
- [x] A1 data model + personal-data inventory + retention
- [x] A2 third-party service + SDK inventory (both repos)
- [x] A3 billing behavior vs pricing/Terms disclosure, refunds, money-flow dark patterns
- [x] A4 email inventory + unsubscribe chain trace
- [x] A5 cookies / storage / consent / form consent
- [x] A6 privacy + terms gap analysis, business details, legal-page discoverability
- [x] A7 marketing claim substantiation, testimonials, asset + font licensing
- [x] A8 deletion / DSAR architecture + privacy-relevant security engineering
- [x] A9 public-record sourcing + Tracerfy / enrichment review

### Live product inspection (lead, browser)
- [ ] Marketing site: rendered claims, testimonials, footer business details, legal links
- [ ] Cookie / storage reality check against code findings
- [ ] Alt text + accessible names on icon-only controls
- [ ] Colour contrast, measured, light + dark
- [ ] Keyboard navigation: tab order, focus visibility, dialogs, tables, escape
- [ ] Viewports 320 / 375 / 390 / 430 / 768 / 1024 / 1440
- [ ] Authenticated app — BLOCKED pending credentials decision from owner

### Cross-check
- [ ] Codex independent challenge of the inventories and findings
- [ ] Verify important Codex findings myself before adopting

### Report
- [ ] 25-section final report incl. 20-item compliance matrix and severity ranking
- [ ] STOP. Await owner approval before any implementation.

## Early findings (lead, pre-agent)

- FE `app/(marketing)/_sections/*` (incl. `Testimonials.tsx`) is not imported by
  the live landing page — `page.tsx` renders `_monopo/*`. Live-vs-dead-code status
  is decisive for the testimonial audit; A7 is confirming via git history.
- `_monopo/CookieBanner.tsx` is notice-only: "Accept" and the X both call the same
  `dismiss()`, which writes `localStorage["bl_cookie_ack"]`. No reject control, and
  no consent state is exported or consumed anywhere. Severity depends entirely on
  whether non-essential trackers actually exist (A5/A2 determining).
- FE `package.json` lists no analytics/telemetry package. Needs confirming against
  inline scripts and `next.config.ts` before it can be stated as fact.
- Routes exist for `/privacy` and `/terms`. No cookie-policy route and no
  refund/cancellation route exist in `app/`.
