# Security Rules

**Standing security baseline for BridgeLeads. Applies to every session and every build.**

## Source pack
- `docs/security/SECURITY_PROMPT_PACK.md` — 29-section operational prompt pack (Master Review §14, Pre-Launch §15).
- `docs/security/security-analyst-agent.md` — the Security Analyst role: severity-tagged findings, never compromises non-negotiables.

> Both files were authored for "Abro" (Next.js 15 / Supabase server-actions). The **threat categories transfer 1:1** to BridgeLeads; the **concrete examples do not**. Translate as you apply them — see the stack map below.

## Stack translation (Abro → BridgeLeads)
| Pack says (Abro) | Read as (BridgeLeads) |
|---|---|
| Server action under `/app/actions/` | FastAPI route in `src/api/routes/` + Celery task in `workers/` |
| Zod schema in `/lib/schemas/` | Pydantic schema in `src/api/schemas.py` |
| `getUser()` / `requireUser()` | JWT + API-key auth in `src/api/auth.py` |
| Supabase RLS + app-layer check | RLS (belt) + mandatory `user_id` query filter (suspenders) |
| `dangerouslySetInnerHTML` ban | `sanitize_for_csv()` + no raw HTML rendering anywhere |
| Service-role key scoping | `SECRET_KEY` / S3 / Stripe / Resend keys via env only |
| Apify ingestion sanitizer | Scraper output → `validate_scraping_target()` (SSRF) → `sanitize_for_csv()` |
| `next.config.js` security headers | `src/api/middleware/` (rate limit, SSRF firewall, security headers) |

## When to run which section
- **After every meaningful feature / scraper / endpoint:** run the Master Security Review (§14). Run it twice — until two consecutive passes come back clean.
- **Touching auth / billing / exports / scraper targets:** run the matching §-section before commit (see the pack's "When to consult which section" table).
- **Before any production deploy to Railway/Vercel:** run the Pre-Launch Prompt (§15).
- **Every new dependency:** run the SBOM check (§12) before adding.

## Non-negotiables for this project
- Every DB query filters by `user_id` (RLS is not enough on its own).
- Never navigate to a user-supplied URL without `validate_scraping_target()` (SSRF).
- All scraped data passes `sanitize_for_csv()` before export (CSV injection).
- No secrets in code — env vars only; `SECRET_KEY` ≥ 32 chars.
- Never silence errors as a "fix"; no mock/dummy code in this real production project.
- Errors returned to clients carry a reference id, never a stack trace / raw DB error.
