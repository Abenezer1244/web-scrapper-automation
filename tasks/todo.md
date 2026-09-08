# Fix: onboarding next_action routes 404 (dead `/dashboard/*` prefix)

## Reproduced (live, production)
- Registered + verified a controlled fresh account on prod
  (`memiki70+bl404repro@gmail.com`), Pro trial, 6 days remaining.
- `GET https://api.bridgeleads.io/auth/onboarding` returned
  `next_action.route = "/dashboard/scrapers/new"`.
- Playwright/Chromium against `https://app.bridgeleads.io`: the onboarding
  "New Scraper" CTA carried `href="/dashboard/scrapers/new"`, landed there, and
  rendered the 404 page. Reproduced at 320 / 375 / 390 / 430 / 1440.
  Network showed `404 /dashboard/scrapers/new` plus two RSC prefetch 404s.

## Root cause
The Next.js app keeps every signed-in page inside the route GROUP
`app/(dashboard)/...`. A parenthesised segment contributes NOTHING to the URL, so
the served paths are `/scrapers/new`, `/results/<id>`, `/scrapers`. The backend's
`onboarding_status_for_user` hardcoded a `/dashboard` prefix that the frontend has
never served. Not a bad href: the frontend renders whatever route string the API
hands it, and every one of the frontend's own nine scraper-creation links was
already correct.

| next_action | was | now |
|---|---|---|
| create_scraper | `/dashboard/scrapers/new` | `/scrapers/new` |
| run_scrape | `/dashboard/scrapers/<id>` | `/scrapers` (no per-scraper page exists; Run now is a list row) |
| wait_for_scrape | `/dashboard` | `/dashboard` (unchanged, real page) |
| download_export | `/dashboard/jobs/<id>` | `/results/<id>` (no /jobs page exists) |
| complete | `/dashboard/scrapers/new` | `/scrapers/new` |

## Done
- [x] Reproduce live on prod, fresh account, all viewports
- [x] Trace the CTA to its source (server-supplied `next_action.route`)
- [x] Audit every backend-emitted frontend path
- [x] `src/config/frontend_routes.py` as the single definition
- [x] `onboarding_status_for_user` uses it
- [x] `onboarding_emails.py` uses it (was already correct, but duplicated)
- [x] `tests/test_onboarding_routes.py`: every onboarding state, plan gating,
      unauthenticated, and a literal transcription of the frontend page list
- [x] Codex review round 1
- [x] Fix Codex findings: `/signup` referral link, overpromising CTA labels,
      circular test oracle, trial-less "Pro trial" fixture
- [x] `ruff check src/ tests/` clean (CI's exact command, ruff 0.15.6)
- [x] Full pytest suite on an isolated DB: 2666 passed, 2 skipped, 0 failed
- [x] Playwright end-to-end against the fixed API response
- [x] Codex review round 2: no P1, no P2, two P3 test gaps, both fixed and
      mutation-verified

## Also found and fixed (same defect class)
`GET /billing/referral` handed the referrer `<app>/signup?ref=<code>`. `/signup`
is not a page and is not public, so a prospect following a shared link was
redirected to `/login` and the ref code was dropped. Verified on prod:
`/signup?ref=ABC123` -> 307 to login, `/register?ref=ABC123` -> 200, and the
register page reads `?ref=` at mount. Now `/register?ref=<code>`.

## Known, pre-existing, NOT fixed here
`first_export_downloaded` is set from `job.export_key`, which the worker writes
when it marks the job done, before anyone downloads anything. So a successful job
skips the `download_export` state entirely and jumps to `complete`, and the
`download_export` branch only fires for a done job with no export (whose export
endpoint 404s by design). Milestone naming, not routing. Worth its own change.

## Review
The bug was one class: the backend inventing a frontend URL from a folder name.
Four of five onboarding routes and the referral share link were dead. The fix is
worth less than the guard: `tests/test_onboarding_routes.py` holds its own literal
copy of the frontend's page list, so a route that does not correspond to a real
`page.tsx` fails, and any `/dashboard/`-prefixed route fails outright.

## Test-run note
Two intermediate suite runs showed scattered failures in files this diff does not
touch (tax cap, brute-force lockout, break-glass, mailing recovery), each run a
DIFFERENT set. Cause was contention on the shared local Postgres, not the change:
another agent's pytest was running, and a leftover uvicorn of mine held
connections to the same DB. With those gone, the 134 tests in the affected files
passed, and the full suite came back 2664 passed / 0 failed.

## Mutation evidence
Each guard was proven by breaking the code and watching the suite go red:
- reintroduce the `/dashboard` prefix in session.py -> 4 failed
- revert billing.py to the `/signup` share URL -> 1 failed (this one PASSED before
  the round-2 endpoint test, which is exactly why Codex flagged it)
- make `job_detail` return `/results/<id>/download` -> 2 failed

## Pre-existing, confirmed, left alone
`PUBLIC_APP_URL` is never declared in `Settings`, so `billing.py` always takes its
hardcoded `https://app.bridgeleads.io` fallback. Predates this change.
