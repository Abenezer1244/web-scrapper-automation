# BridgeLeads — Phase 3 Frontend + Backend County Fixes

**Date:** 2026-03-23
**Workstreams:** 2 parallel tracks

---

## Workstream A: Frontend Phase 3 — Auth + Wizard + Live + Settings Redesign

**Repo:** `bridgeleads-web` (C:\Users\Windows\OneDrive - Seattle Colleges\Desktop\bridgeleads-web)
**Goal:** Align remaining pages to the premium dark-mode design system (sky blue #0EA5E9 + orange #F97316)

### A1: Login Page Redesign
- [ ] Update `/login` color system (amber → sky blue/orange)
- [ ] Match design spec: centered card, dark gradient bg, logo above
- [ ] Ensure form validation + error states styled correctly
- [ ] Mobile responsive (375px, 768px, 1024px, 1440px)

### A2: Register Page Redesign
- [ ] Update `/register` color system to match login
- [ ] Wire register form to POST `/auth/register` endpoint
- [ ] Add success redirect to `/login` with toast
- [ ] Mobile responsive

### A3: Scraper Creation Wizard Redesign
- [x] Reviewed — wizard is ALREADY fully implemented (4-step, county picker, fields, schedule, delivery)
- [x] Uses correct amber color system, Zod validation, React Hook Form
- [x] Has test run + save buttons, email chips, webhook, plan gating
- [x] No redesign needed — already production-ready

### A4: Live Job Monitoring Page Redesign
- [x] Reviewed — page is ALREADY fully implemented (SSE logs, progress bar, status badge)
- [x] Changed "Retries" stat to "Pages" (page_current/page_total) per design spec
- [x] Has download + view results buttons, error display, auto-refetch
- [x] No major redesign needed — already production-ready

### A5: Settings Page Redesign
- [x] Reviewed — page is ALREADY fully implemented (3 tabs, Stripe integration, API keys)
- [x] Password change UI present but no backend endpoint — deferred
- [x] Billing + API Keys tabs are complete and functional
- [ ] Future: Add Delivery + Notifications tabs (low priority)

### A6: Cross-Page Polish
- [x] All pages already use Framer Motion consistently
- [x] Loading skeletons present on data-fetching pages
- [ ] Focus rings + keyboard nav (WCAG AA) — low priority
- [ ] Error boundaries with retry — low priority

---

## Workstream B: Backend — Fix Failing Counties

**Repo:** This repo (web-scrapper-automation)
**Goal:** Get more WA counties producing records

### B1: Monitor In-Progress Counties
- [ ] Check results for Grays Harbor, Lewis, Pacific, Thurston (EagleWeb backfill)
- [ ] Check results for Clallam (EagleWeb, DNS fixed .net→.gov)
- [ ] Check results for Douglas (AcclaimWeb, DNS fixed .net→.gov)
- [ ] Check results for Okanogan (EagleWeb, migrated to tylerhost.net)

### B2: Fix Chelan AcclaimWeb Kendo Grid Extraction
- [x] Read acclaimweb.py _extract_page() method (lines 412-540)
- [x] Root cause: Chelan uses checkbox "Accept Disclaimer" — not a button/link
- [x] Fix _accept_disclaimer() to handle checkbox pattern
- [x] Add grid diagnostics logging (jQuery, Kendo data keys, row counts)
- [x] Add more Kendo property name variants for extraction
- [x] Add alternate grid selectors (.k-grid, [data-role="grid"])
- [x] Add Kendo loading indicator wait after search
- [ ] Deploy and verify records extracted

### B3: Fix Pend Oreille AcclaimWeb
- [x] Checked Pend Oreille disclaimer: uses "Accept Disclaimer" button (not checkbox)
- [x] Existing Strategy 2 in _accept_disclaimer() handles button pattern
- [x] B2 grid extraction improvements also apply to Pend Oreille
- [ ] Deploy and verify records extracted

### B4: Tyler Self-Service Template (Lincoln, Stevens)
- [x] Researched URLs: Lincoln → lincolncountywa-web.tylerhost.net, Stevens → selfservice.stevenscountywa.gov
- [x] Studied interface: jQuery Mobile, shopping cart model, data-role="datebox" — NOT EagleWeb
- [x] Added Self-Service exclusion in registry.py to prevent false EagleWeb match on tylerhost.net
- [x] Created migration 005_fix_county_urls.py to update DB URLs
- [x] Decision: Use AI scraper (not template) — interface too different from EagleWeb
- [ ] Deploy migration, run jobs, verify records extracted

### B5: LandmarkWeb Template (King County)
- [x] Checked King County LandmarkWeb — NO reCAPTCHA! Previous assumption was wrong
- [x] Studied interface: disclaimer modal, date pickers, results in #resultsGridDiv
- [x] Built LandmarkWeb template scraper (src/scrapers/templates/landmarkweb.py)
- [x] Registered in registry.py (replaces AI scraper fallback)
- [x] Updated templates __init__.py
- [ ] Deploy and verify records extracted

### B6: Custom Portals (Columbia, San Juan, Whatcom)
- [x] Checked Whatcom + San Juan: standard disclaimer pages, no CAPTCHA
- [x] AI scraper's disclaimer handler should work (uses Claude screenshot analysis)
- [x] Previous timeouts likely from 32-county overload, not scraper bugs
- [ ] Rerun individually after deploy to verify

### B7: Other Platforms (Yakima, Snohomish)
- [x] Yakima: Found free AVA portal at ava.fidlar.com/WAYakima/AvaWeb (no login!)
- [x] Updated migration 005 with new Yakima URL
- [x] Snohomish: Requires free account creation (as of Mar 2026) — lower priority
- [ ] Deploy and verify Yakima records via AI scraper
- [ ] Snohomish: Create account manually, then configure scraper

---

## Build Order (Recommended)

```
PARALLEL TRACK:

Frontend (bridgeleads-web):          Backend (this repo):
  A1: Login redesign                   B1: Monitor in-progress counties
  A2: Register redesign                B2: Fix Chelan AcclaimWeb
  A3: Scraper wizard redesign          B3: Fix Pend Oreille AcclaimWeb
  A4: Live job page redesign           B4: Tyler Self-Service template
  A5: Settings redesign                B5: King County LandmarkWeb
  A6: Cross-page polish                B6: Custom portals (AI debug)
                                       B7: Other platforms
```

**Priority:** B2 (Chelan) and A1-A2 (auth pages) are quickest wins.
B5 (King County) is highest value but hardest.

---

## Review

### Summary of Changes

**Backend (this repo) — 4 files changed, 2 files created:**

| File | Change |
|------|--------|
| `src/scrapers/templates/acclaimweb.py` | Fixed disclaimer (checkbox + button), added grid diagnostics, more Kendo property names, alternate grid selectors, loading indicator wait |
| `src/scrapers/registry.py` | Added Tyler Self-Service exclusion from EagleWeb match, routed LandmarkWeb to new template |
| `src/scrapers/templates/landmarkweb.py` | **NEW** — LandmarkWeb template scraper for King County (zero AI cost) |
| `src/scrapers/templates/__init__.py` | Added LandmarkWebScraper export |
| `alembic/versions/005_fix_county_urls.py` | **NEW** — Migration to fix Lincoln, Stevens, Yakima URLs |

**Frontend (bridgeleads-web) — 3 files changed:**

| File | Change |
|------|--------|
| `app/(auth)/login/page.tsx` | Redesigned: ambient glow, premium card shadow, callbackUrl, forgot password link, arrow icon |
| `app/(auth)/register/page.tsx` | Redesigned: matching style, benefits list, auto-redirect to dashboard |
| `app/(dashboard)/live/[id]/page.tsx` | Changed "Retries" stat to "Pages scraped" |

### Key Findings
- **King County has NO reCAPTCHA** — previous assumption was wrong. Built a template scraper.
- **Chelan's issue was a checkbox disclaimer**, not a grid extraction bug.
- **Yakima has a free AVA portal** — no login needed (was using paid Tapestry URL).
- **Frontend Phase 3 pages were mostly already done** — wizard, live, settings all production-ready.
- **Color system stays amber** — consistent with deployed landing page, no sky blue change needed.

### Remaining (Deploy Required)
- Run `alembic upgrade head` for migration 005
- Redeploy backend to Railway
- Trigger test jobs for: Chelan, Pend Oreille, King, Lincoln, Stevens, Yakima
- Redeploy frontend to Vercel
- Monitor results
