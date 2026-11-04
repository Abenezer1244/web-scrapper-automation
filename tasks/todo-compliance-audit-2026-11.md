# Compliance audit 2026-11: implementation plan

**Audit:** `docs/legal/audit-2026-11/01..05`.
**Worktrees:**
- BE: `C:/Users/micha/bl-wt/compliance-be`, branch `audit/compliance-2026-11`
- FE: `C:/Users/micha/bl-wt/compliance-fe`, branch `audit/compliance-2026-11`

**Rules:**
- Each phase touches at most 5 files.
- Codex consult before each phase, Codex review after.
- Owner approval before each phase.
- No push, merge or deploy without approval.
- No production DB, Stripe live or paid-enrichment actions.

## Phase 0: investigation (DONE 2026-11-04)
- [x] Fresh worktrees from origin/main and origin/master; both shared checkouts left untouched
- [x] Live site audit (Playwright headless, desktop + mobile), DNS check
- [x] BE data lifecycle, BE security/billing, FE tracking/legal, law research, source-rights research
- [x] Spot-verified key subagent claims (DNC NULL, sample endpoint live, flag defaults, robots/terms quotes)
- [x] Executive report + risk register, data inventory, vendor register, retention matrix, source register, SBOMs
- [x] Codex consult on the register and this plan (pre-implementation): AGREE, reconciled below

## Phase 1: BE quick safety fixes (Category A, ≤5 files)
- [ ] P1-6: minimise `/scrapers/sample` (initials only, 5-digit ZIP, month-level date) in `public_cache.py`, with a test
- [ ] P2-10: top-level `permissions: contents: read` in `ci-cd.yml`; pin `@railway/cli`
- [ ] P2-11: mark the 3 stale compliance docs superseded and fix the `nts_columbian.py:5` comment
- Verify: ruff, targeted pytest against the guarded test DB only, OpenAPI drift check

## Phase 2: FE disclosure and tracking fixes (Category A, ≤5 files)
- [ ] P1-8: do not load GA4 when `navigator.globalPrivacyControl` or DNT is set (`site-analytics.tsx`)
- [ ] P1-8: correct the CookieBanner copy (it currently claims cookies are only for sign-in)
- [ ] P1-2: DNC notice on batch export; remove "your dialer performs the final DNC scrub" (`DeliveryStep.tsx`)
- [ ] P1-7: show the upfront annual total on the marketing pricing cards (`PlanCardsGrid.tsx`)
- [ ] P2-8: disclose "25 contact lookups during the trial" (`data.ts`)
- Verify: tsc, eslint, next build, Playwright desktop + mobile (no GA request with GPC on)

## Phase 3: record Terms/Privacy acceptance (BE + FE, ≤5 files)
- [ ] Migration: `users.terms_version`, `terms_accepted_at`, `privacy_version`
- [ ] Registration schema + route require and persist them (reject if missing)
- [ ] FE register payload sends the versions; a single source of the version constant
- Verify: tests, OpenAPI regen, FE build

## Phase 4: CAN-SPAM opt-out for promotional email (BE, ≤5 files)
- [ ] Migration: `users.marketing_emails_opt_out_at`
- [ ] Signed one-click unsubscribe route (RFC 8058 POST + GET confirmation page)
- [ ] `List-Unsubscribe` / `List-Unsubscribe-Post` headers and a footer link on promotional mail only
- [ ] Onboarding nudges and trial-ending mail skip opted-out users
- Blocked for the postal address in the footer: owner B1-1

## Phase 5: checkout and billing disclosures (BE, ≤5 files)
- [ ] Stripe Checkout `custom_text.submit`: renewal amount, interval, cancel path (test mode only)
- [ ] `charge.dispute.created` / `charge.dispute.closed`: ops alert (no entitlement change until owner B1-5)
- [ ] Annual-downgrade forfeiture (P1-9): reproduce in Stripe test mode first; fix only if reproduced

## Phase 6: data-subject rights for non-customers (design first, then ≤5-file slices)
- [ ] Design doc + Codex consult: intake (form + mailbox), verification, `privacy_requests` table, person-level
      suppression keys (normalised name + parcel / address hash), checks on insert and delivery, erasure fan-out
      (results, skip_trace_cache, pending_skip_trace_rows, nts_notices, king_parcel_owners, R2), audit trail,
      45-day SLA alerting, DROP-ready hooks
- [ ] Implementation slices (each its own approval)

## Phase 7: legal document package (drafts only, not published)
- [ ] Privacy Policy, Terms, Cookie notice, AUP, Retention Policy, Breach Response Plan, Privacy Request Procedure,
      Subprocessor list. Fact-grounded and marked "DRAFT, requires counsel"; unresolved facts left as explicit gaps.

## Owner-blocked (cannot be done in code)
- MX + SPF + DMARC on bridgeleads.io, then switch all legal addresses (P0-1)
- Legal facts for the placeholders (P0-3)
- Production flag values + `county_connectors` dump (B1-3)
- Vendor agreements, Tracerfy contract (B1-6)
- Retention windows (B1-4), refund policy (B1-5), consent model (B1-10), DNC scrub (B1-11), tax (B1-12)

## Codex consult log
**2026-11-04, pre-implementation** (read-only, gpt-5.6-luna). Verdict: **AGREE**, not ready.

Adopted:
- **P1-11 Tracerfy CDN URLs:** adopted.
- **P1-12 RLS production verification:** downgraded from Codex Critical to an owner verification. The repo design note governs the code shape: the boot already alerts and stays non-blocking for rollback (`session.py:405-425`).
- **P2-12 ops-alert text:** adopted.
- **Transit processors in the inventory:** adopted.
- **GDPR and sensitivity wording:** softened.

Plan changes:
- **Gate reorder:** owner verification of production flags, RLS, CAPTCHA, Tracerfy terms and inbound mail comes first. Those items are owner-only and run in parallel with Eng Phase 1.
- **Phase 1:** removing real records from `/scrapers/sample` changes an API contract. Check every FE consumer first. The owner chooses synthetic records or a minimised display.
- **Phase 2:** the consent model (opt-in vs GPC opt-out) is an owner decision. GPC honouring and removing false copy are safe either way.
- **Phase 3:** compatibility rollout. Add nullable columns, then the BE accepts the optional fields and stores them, then the FE sends them, then the BE enforces. Decide what happens for existing users who accepted before versioning (prompt at next login).
- **Phase 4:** counsel classifies each template as commercial or transactional. Tokens must be idempotent, and the scheduler re-checks opt-out at send time.
- **Phase 6 design must handle:**
  - person / parcel / record scope chosen explicitly, never inferred
  - confidence matching plus manual review
  - an atomic, fail-closed suppression check at every ingest, enrichment, cache-reuse, export, sample, dialer and webhook boundary
  - shared tables (`king_parcel_owners`, `nts_notices`) treated per policy, not blindly deleted
  - the suppression register itself encrypted, access-controlled and retention-bound
  - no confirming that a record exists to an unverified requester
- **Five-file limit:** Codex says it is unsafe for cross-cutting work. Phases 3 and 6 will be split into sequential slices, each verified, rather than raising the limit.

## Review
(filled in at the end)
