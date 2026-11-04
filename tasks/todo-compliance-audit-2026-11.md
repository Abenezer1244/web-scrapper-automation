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

## Phase 1: BE quick safety fixes (DONE, Codex GO after 2 NO-GO rounds; owner chose synthetic records)
- [x] P1-6: `/scrapers/sample` serves flagged example records. The route ignores cached records and shape-checks
      the cached counts (`5bbd69b5`, `6bb7b319`, `854872b1`). 7 tests, proven to fail on the old route.
- [x] P2-10: top-level `permissions: contents: read`; `@railway/cli@5.71.0` pinned (`5bbd69b5`)
- [x] P2-11: 3 stale docs marked superseded; `nts_columbian.py` comment corrected (`ec930834`)
- Verified: ruff clean; targeted pytest on the guarded local `_test` DB; OpenAPI regenerated (1 description line);
  end-to-end refresh on the test DB

## Phase 2: FE disclosure and tracking fixes (DONE, Codex GO after 2 NO-GO rounds; owner chose the GPC model)
- [x] P1-8: GA4 not loaded under GPC or DNT (`65ea811`)
- [x] P1-8: truthful cookie notice naming GA, GPC and DNT (`65ea811`, `fde39ca`)
- [x] P1-2: DNC notice on batch export, tied to the menu by aria-describedby; dialer copy corrected (`9f511ea`, `fde39ca`)
- [x] P1-7: upfront yearly totals on the pricing cards (`091082e`)
- [x] P2-8: "25 lookup credits" in all 6 places (`091082e`, `fde39ca`, `563fbce`)
- Verified: tsc 0, eslint 0, 126/126 unit tests, next build OK, Playwright 22/22 on desktop and mobile
- Known, not in scope (pre-existing): a repeat visit to the same marketing page in one session sends no
  second GA page_view (`lastSent`); the annual Pro card hides the trial note; `pricingApi.ts` (unused) ignores
  `price_annual`

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

## Review (2026-11-04)

**Done:**
- The audit (Phase 0) and Phases 1-2 are on local branches in both repos.
- Every remediation commit passed the project's checks and a final Codex GO.
- Nothing is pushed, merged or deployed.
- No production data, Stripe, or paid enrichment was touched.

**Still blocking launch:**
- The P0 items: dead mailboxes, owner rights machinery, placeholders, source rights and CAPTCHA.
- All four need the owner, counsel, or Phase 6.

**Next:** owner answers to B1, then approval of Phase 3 (record terms acceptance, staged rollout).

**New since the audit:** Sentry (BE #586) is merged but dormant. Disclose it before setting `SENTRY_DSN`.
