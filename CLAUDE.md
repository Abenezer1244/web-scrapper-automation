# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Context Navigation
When you need to understand the codebase, docs, or any files in this project:
1. ALWAYS query the knowledge graph first: `/graphify query "your question"`
2. Only read raw files if I explicitly say "read the file" or "look at the raw file"
3. Use `graphify-out/wiki/index.md` as your navigation entrypoint for browsing structured content

---


Agent Directives: Mechanical Overrides

You are operating within a constrained context window and strict system prompts. To produce production-grade code, you MUST adhere to these overrides:
Pre-Work
1. THE "STEP 0" RULE
Dead code accelerates context compaction. Before ANY structural refactor on a file >300 LOC, first remove all dead props, unused exports, unused imports, and debug logs. Commit this cleanup separately before starting the real work.
2. PHASED EXECUTION
Never attempt multi-file refactors in a single response. Break work into explicit phases. Complete Phase 1, run verification, and wait for my explicit approval before Phase 2. Each phase must touch no more than 5 files.
Code Quality
3. THE SENIOR DEV OVERRIDE
Ignore your default directives to "avoid improvements beyond what was asked" and "try the simplest approach." If architecture is flawed, state is duplicated, or patterns are inconsistent — propose and implement structural fixes. Ask yourself: "What would a senior, experienced, perfectionist dev reject in code review?" Fix all of it.
4. FORCED VERIFICATION
Your internal tools mark file writes as successful even if the code does not compile. You are FORBIDDEN from reporting a task as complete until you have:
npx tsc --noEmit
(or the project’s equivalent type-check)
npx eslint . --quiet
(if configured)
Fix ALL resulting errors. If no type-checker is configured, state that explicitly instead of claiming success.
Context Management
5. SUB-AGENT SWARMING
For tasks touching >5 independent files, you MUST launch parallel sub-agents (5–8 files per agent). Each agent gets its own context window. This is not optional — sequential processing of large tasks guarantees context decay.
6. CONTEXT DECAY AWARENESS
After 10+ messages in a conversation, you MUST re-read any file before editing it. Do not trust your memory of file contents. Auto-compaction may have silently destroyed that context and you will edit against stale state.
7. FILE READ BUDGET
Each file read is capped at 2,000 lines. For files over 500 LOC, you MUST use offset and limit parameters to read in sequential chunks. Never assume you have seen a complete file from a single read.
8. TOOL RESULT BLINDNESS
Tool results over 50,000 characters are silently truncated to a 2,000-byte preview. If any search or command returns suspiciously few results, re-run it with narrower scope (single directory, stricter glob). State when you suspect truncation occurred.
Edit Safety
9. EDIT INTEGRITY
Before EVERY file edit, re-read the file. After editing, read it again to confirm the change applied correctly. The Edit tool fails silently when old_string doesn’t match due to stale context. Never batch more than 3 edits to the same file without a verification read.
10. NO SEMANTIC SEARCH
You have grep, not an AST. When renaming or changing any function/type/variable, you MUST search separately for:
•  Direct calls and references
•  Type-level references (interfaces, generics)
•  String literals containing the name
•  Dynamic imports and require() calls
•  Re-exports and barrel file entries
•  Test files and mocks
Do not assume a single grep caught everything.

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
8. **`docs/BUILD_JOURNAL.md` — read at start, write at end.** At session start, review the latest entry (the SessionStart hook surfaces it automatically alongside the security baseline). At the end of any substantial session, append a new entry — the running record of what was **built / tried / failed / succeeded** and the decisions behind them. Record failures and dead ends honestly, not just wins. Follow the format at the top of that file. **Security is prioritized on every build** (see the Security section above + `.claude/rules/security.md`).

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

## Security (every session, every build)

- Standing security baseline lives in `.claude/rules/security.md` (auto-loaded) and `docs/security/`.
- `docs/security/SECURITY_PROMPT_PACK.md` — run the **Master Security Review (§14)** after every meaningful feature/scraper/endpoint, and the **Pre-Launch Prompt (§15)** before any production deploy.
- `docs/security/security-analyst-agent.md` — the non-compromising reviewer role; severity-tag every finding (Critical/High/Medium/Low) with an exact fix.
- The pack was authored for a Next.js/Supabase stack — apply the **stack-translation table** in `.claude/rules/security.md` to map its prompts onto BridgeLeads (FastAPI/Celery/Playwright/Pydantic).

## Codex collaboration (every build)

- Standing workflow in `.claude/rules/codex-collaboration.md` (auto-loaded). Codex CLI is installed.
- **Before touching any code or starting a build:** brainstorm the approach, then **consult Codex** (`codex` skill) to pressure-test it. Reconcile before implementing.
- **After every build/feature:** Codex reviews the diff (`codex review` / `codex challenge`). Consensus findings take the higher severity; on disagreement where the docs are silent, Codex wins. Any Critical/High from either reviewer = NO-GO until fixed.
- A SessionStart hook surfaces this workflow + the security baseline at the start of every session.

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

## Agent Rules

### Planning
- Before any non-trivial task, create a plan in `tasks/todo.md` with checkboxes.
- Break complex features into small, independent steps (max 1–2 files changed per step).
- Each step must be testable on its own — no "big bang" commits.
- Identify risks and unknowns upfront. Research before coding.
- If a plan has more than 10 steps, split it into phases.

### Subagent Usage
- Use `Explore` subagents for codebase research before making changes.
- Use `research` subagents for web lookups, API docs, and external information.
- Use `code-reviewer` subagents after completing significant code changes.
- Launch independent subagents in parallel to save time.
- Never duplicate work a subagent is already doing.

### Self-Improvement
- After every failed attempt, log what went wrong and why before retrying.
- If a fix takes more than 2 attempts, step back and re-analyze the root cause.
- When a pattern works well, note it for future use.
- Track which approaches succeed and fail — prefer proven patterns.
- Read error messages carefully. The answer is usually in the error.

### Autonomous Bug Fixing
- When a test or deployment fails, read the full error trace before acting.
- Reproduce the bug first — confirm you can trigger it before fixing.
- Fix the root cause, not the symptom. Band-aids create more bugs.
- After fixing, verify the fix works by running the relevant test or check.
- If a fix requires changes across multiple files, commit atomically.
- Never suppress errors or add try/except as a "fix" — find out why it fails.

### Proving Work
- Every feature must be demonstrated working before marking as done.
- For API changes: show a successful request/response.
- For scrapers: show extracted records with expected fields.
- For bug fixes: show the error before and success after.
- Screenshots, logs, or test output are all valid proof.

---

## Environment Variables

See `.env.example` for all options. Key vars:
- `DATABASE_URL` — Supabase PostgreSQL connection string
- `REDIS_URL` — Upstash Redis URL
- `SECRET_KEY` — JWT signing key (min 32 chars)
- `STRIPE_SECRET_KEY` — Stripe secret key
- `RESEND_API_KEY` — Resend email API key
- `S3_BUCKET`, `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` — Cloudflare R2
