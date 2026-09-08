# Fix: onboarding next_action routes 404 (dead `/dashboard/*` prefix)

## Reproduced (live, prod)
- Fresh account `memiki70+bl404repro@gmail.com` registered + verified on prod.
- `GET https://api.bridgeleads.io/auth/onboarding` returns
  `next_action.route = "/dashboard/scrapers/new"`.
- Playwright/Chromium against `https://app.bridgeleads.io`: clicking the
  onboarding "New Scraper" CTA lands on `/dashboard/scrapers/new` and renders the
  404 page. Confirmed at 320 / 375 / 390 / 430 / 1440.

## Root cause
The Next.js app puts every signed-in page under the route GROUP
`app/(dashboard)/...`. A parenthesised segment is NOT part of the URL, so the real
paths are `/scrapers/new`, `/results/<id>`, `/scrapers`. The backend's
`onboarding_status_for_user` hardcodes a literal `/dashboard` prefix that the
frontend has never served. Four of the five `next_action.route` values are dead.

| next_action | emitted route | real route |
|---|---|---|
| create_scraper | `/dashboard/scrapers/new` | `/scrapers/new` |
| run_scrape | `/dashboard/scrapers/<id>` | `/scrapers` (Run now lives on the list row; no per-scraper detail page exists) |
| wait_for_scrape | `/dashboard` | `/dashboard` (valid) |
| download_export | `/dashboard/jobs/<id>` | `/results/<id>` (job detail page) |
| complete | `/dashboard/scrapers/new` | `/scrapers/new` |

## Todo
- [ ] Consult Codex on the approach before writing code
- [ ] Add `src/config/frontend_routes.py` (single definition of the app paths)
- [ ] Point `onboarding_status_for_user` at those helpers
- [ ] Reuse the helpers in `src/workers/onboarding_emails.py` (already correct, but
      duplicated literals are what drifted)
- [ ] Tests: every onboarding state emits a known-good route; no emitted route
      starts with `/dashboard/`
- [ ] Run pytest via `bl-testenv/run-full-pytest.sh` (never bare pytest)
- [ ] Codex review of the diff
- [ ] Playwright re-verify against a locally served frontend + patched API
- [ ] Review section
