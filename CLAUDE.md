# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## ALERT

**THIS IS NOT A MOCK OR TEST OR DUMMY PROJECT. IT IS A REAL WORLD ENTERPRISE LEVEL SAAS SO NEVER ADD MOCK, TEST, OR DUMMY CODE.**

---

## Working Instructions

When reading files, read the whole file chunk by chunk to ensure nothing is missed.

1. First think through the problem, read the codebase for relevant files, and write a plan to `tasks/todo.md`.
2. The plan should have a list of todo items that you can check off as you complete them.
3. Before beginning work, check in with the user to verify the plan.
4. Then begin working on the todo items, marking them as complete as you go.
5. At every step, give a high level explanation of what changes were made.
6. Make every task and code change as simple as possible. Avoid massive or complex changes. Every change should impact as little code as possible. Everything is about simplicity.
7. Finally, add a review section to `tasks/todo.md` with a summary of the changes made and any notes.

---

## Project Overview

**BridgeLeads** — a multi-tenant SaaS that automates motivated seller lead generation for real estate investors. Scrapes county public records daily, enriches with property data via parcel lookup, and delivers clean CSV lead lists on a schedule.

**Target users:** Real estate wholesalers, flippers, and agents across the US
**Product docs:** `docs/product/` — vision, architecture, security audit, devops, frontend design, UX spec

---

## Tech Stack

| Layer | Choice |
|-------|--------|
| Scraping | Playwright (headless Chromium) + BeautifulSoup |
| Backend API | FastAPI (async) |
| Job queue | Celery + Redis |
| Database | PostgreSQL via Supabase (RLS enabled) |
| Migrations | Alembic |
| Export storage | Cloudflare R2 (S3-compatible) |
| Email delivery | Resend |
| Billing | Stripe |
| Frontend | Next.js 14 (separate repo) |
| Hosting | Railway (API + workers) + Vercel (frontend) |

---

## Project Structure

```
web-scrapper-automation/
├── main.py                        # FastAPI app entry point
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example                   # All env vars documented
├── alembic/                       # DB migrations
├── src/
│   ├── config/
│   │   └── settings.py            # Pydantic settings (reads from .env)
│   ├── db/
│   │   ├── models.py              # SQLAlchemy models (6 tables)
│   │   └── session.py             # Async + sync engines
│   ├── api/
│   │   ├── auth.py                # JWT + API key auth, plan enforcement
│   │   ├── schemas.py             # Pydantic request/response models
│   │   ├── middleware/            # Rate limiting, SSRF firewall, security headers
│   │   └── routes/                # auth, scrapers, jobs endpoints
│   ├── scrapers/
│   │   ├── base_scraper.py        # BaseScraper (Playwright only — Selenium dropped)
│   │   ├── {county}_{state}_{type}.py  # One file per county+record type
│   │   └── registry.py            # County connector registry (DB-driven)
│   └── utils/
│       ├── data_exporter.py       # CSV / JSON / Excel export + S3 upload
│       └── logger.py              # Colored console + file logging
├── workers/
│   ├── tasks.py                   # Main Celery job (state machine)
│   ├── scheduler.py               # Beat: dispatch, watchdog, canary, reset
│   └── delivery.py                # Email delivery via Resend
├── tests/
├── docs/
│   └── product/                   # vision, architecture, security, devops, UX
└── tasks/
    └── todo.md                    # Build plan + progress
```

---

## Key Architectural Rules

- **Multi-tenancy:** every DB query must filter by `user_id`. PostgreSQL RLS is belt, query filter is suspenders.
- **No Selenium:** scraping engine is Playwright only.
- **SSRF protection:** never navigate to user-supplied URLs without passing through `validate_scraping_target()`.
- **CSV injection:** all scraped data must pass through `sanitize_for_csv()` before export.
- **Secrets:** all config via env vars. Never hardcode. Pydantic validator raises if SECRET_KEY < 32 chars.
- **Job state machine:** PENDING → QUEUED → PROBING → SCRAPING → ENRICHING → DONE/FAILED/CANCELLED. Every transition logged.

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in: DATABASE_URL, REDIS_URL, S3, JWT_SECRET, STRIPE_SECRET_KEY, RESEND_API_KEY
docker-compose up
```

## Running Tests

```bash
pytest
pytest --cov=src tests/
```

## Adding a New County Scraper

The scraper system is county-agnostic. Each county is a plugin — adding one requires no changes to core infrastructure.

1. Create `src/scrapers/{county}_{state}_{record_type}.py` extending `BaseScraper`
2. Implement `scrape(date_from, date_to) -> list[ScrapedRecord]`
3. Register in `src/scrapers/registry.py`
4. Insert row into `county_connectors` table
5. Done — scheduler, watchdog, canary, and all job infrastructure pick it up automatically

**Supported record types:** probate, pre_foreclosure, tax_delinquent, divorce, code_violation, eviction
**Expansion path:** WA counties → top 10 investor states → national

## Environment Variables

See `.env.example` for all options. Key vars:
- `DATABASE_URL` — Supabase PostgreSQL connection string
- `REDIS_URL` — Upstash Redis URL
- `SECRET_KEY` — JWT signing key (min 32 chars)
- `STRIPE_SECRET_KEY` — Stripe secret key
- `RESEND_API_KEY` — Resend email API key
- `S3_BUCKET`, `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` — Cloudflare R2
