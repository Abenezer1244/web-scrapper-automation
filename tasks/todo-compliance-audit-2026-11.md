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

## Phase 3: record Terms/Privacy acceptance (3a-3c DONE 2026-11-04, Codex GO; 3d pending)
The design changed after the Codex consult: user columns became an append-only table,
the server owns the versions, and acceptance comes from the verifier.
- [x] 3a-i BE migration 123 `legal_acceptances` (`f6304eec`, `86f6d585`). Append-only by
      trigger (BLD22) and by grants; DB-clock `accepted_at`; fenced; RLS; kept after purge.
      Retention period is an owner/counsel item in the matrix.
- [x] 3a-ii BE writer (`82ca14b4`). Records only exactly-current versions, in the account's
      transaction; the verified flow records from verify-email only.
- [x] 3a-iii BE `/auth/config` publishes `legal_versions` as OpenAPI constants (`d85afc4a`)
- [x] 3b FE BFF allowlist, `lib/legal-versions.ts` typed against the backend schema,
      verify-email checkbox (`d993bf4`, `99d1968`)
- [x] 3c FE register sends the versions in the legacy flow (`0db6b7e`)
- Verified:
  - BE: 107 auth/registration tests; the role path with throwaway roles in a rolled-back
    transaction; migration up/down/up.
  - FE: tsc, eslint, 130 unit tests.
  - E2E (local stack, desktop + mobile): both flows write rows; unticked is blocked;
    focus lands on the box.
  - Drift guard proven (TS2322 on a mismatched version).
- **Rollout order: the BE PR merges and deploys FIRST, then the FE PR.** FE CI regenerates
  types from BE main; an FE-first deploy would send fields the old BE silently ignores.
- [ ] 3d (later, its own approval): require acceptance on both endpoints behind a flag once
      telemetry shows every signup sends the fields; re-acceptance for existing users and
      on version bumps; add acceptances to the account export. Owner/counsel: whether to
      split Terms and Privacy into two checkboxes; whether to store IP/user agent.
- Local test helpers (not committed): `C:/Users/micha/bl-wt/pg6543_forward.py` (sync tests
  need :6543), `verify_123_roles.py`, `e2e_db.py`.

## Phase 4: CAN-SPAM opt-out for commercial email (DONE 2026-11-04; Codex: GO for the email work)
Design changed after the Codex consult: opt-outs are keyed by the address's email_hmac in their own
table, not a users column, so they survive deletion and re-registration.
**Owner decision 2026-11-04:** commercial email is HELD until COMPANY_POSTAL_ADDRESS is set.
- [x] 4a migration 124 `marketing_email_suppressions` (append-only, BLD23 trigger, HMAC only)
- [x] 4b signed address-bound token (400 d) + GET confirm / POST opt-out (form or RFC 8058 one-click);
      hidden from OpenAPI
- [x] 4c welcome, day-1, day-3 and trial-ending are commercial. Each gets List-Unsubscribe headers,
      a footer link and the postal address, a send-time suppression check, and is HELD with an ops
      alert while API_BASE_URL or the address is unusable. Send functions return outcomes.
- [x] 4d beat skips accounts pending deletion and counts only real sends
- [x] Codex review fixes:
  - per-address rate limit (no 429s from shared provider IPs);
  - the welcome send runs in the threadpool;
  - form rel=noreferrer (verified: no Referer);
  - streamed 4 KiB body cap; strict bare-origin base;
  - cutover step2 re-grant revoked for 123/124.
- Verified:
  - 162 tests incl. peer #590's;
  - E2E on the local API: browser confirm, then POST, then a `link` row; curl one-click gives a
    `one_click` row; a bad token gives 400;
  - rendered email checked at phone width.
- Reasoned disagreement (Codex ACCEPTED): check-then-send race. A lock cannot un-send an in-flight
  email; CAN-SPAM allows 10 business days.
- Not changed, tracked as P2-13: the June cutover script would over-grant older post-cutover
  tables (account_exports, job_degradations, pending_email_changes, deletion tables) if re-run.
- Polish (P3): the confirm page is unstyled because the API's global CSP drops inline styles, as on
  the download-error page.
- Counsel: confirm the commercial/transactional classification and the indefinite suppression
  retention.

## Phase 5: checkout and billing disclosures (DONE 2026-11-04; Codex GO after 5 NO-GO rounds)
- [x] 5a: Checkout `custom_text.submit` states the renewal terms read from the Stripe Price
      (amount, currency, cadence), the monthly lookup allowance, and the per-unit overage only when
      a metered Price is on the session. No claim about cancellation timing.
      - A Stripe read failure or the wrong Price kind (usage_type, scheme, no amount) fails closed (503).
      - A catalog mismatch pages ops, hourly re-sent per Price.
      - Reads happen before the per-user lock.
- [x] 5b: the four `charge.dispute.*` lifecycle events page ops post-commit (ids, amount JPY-safe,
      reason, status; no PII). Cooldown keyed by event + dispute. No entitlement change.
- [x] 5c (FE): the plan-change dialog was FALSE ("Stripe credits the unused part"). It now states
      three cases, matching `_plan_change_billing` and the anchor rules:
      - same-cycle upgrade: prorated now;
      - downgrade: no credit; allowances run to the monthly reset;
      - cycle switch: billed today; credit only when the new plan is the same size or bigger.

      It also states the renewal terms. Annual is offered only with its amount. The overage
      sentence is narrowed.
- [x] The dialog's price is binding:
      - change-plan requires `expected_price`;
      - a missing value returns 409 price_unconfirmed, a stale one 409 price_changed;
      - change-plan verifies the Price strictly (a catalog mismatch is refused, not charged);
      - the overage Price is validated.
- Verified: 562 tests across every billing, stripe, checkout, plan, promo, entitlement and webhook
  suite; FE tsc, eslint, 130 unit tests, build.
- Not done: 5c-reproduction of the annual-downgrade forfeiture needs a Stripe TEST-mode key and an
  owner policy decision (credit, forfeit, or defer to period end). The dialog is truthful about
  today's behaviour meanwhile.
- Owner (live Stripe dashboard):
  - subscribe the webhook to the 4 dispute events;
  - set the Terms URL if consent_collection is wanted;
  - verify the Customer Portal allows cancellation, and its timing.
- Rollout: BE then FE. Between the two deploys an old-frontend tab gets 409 on plan switch; no
  money moves.

## Phase 6: data-subject rights for non-customers (DESIGN DONE 2026-11-04; build blocked on decisions)
- [x] Code map of every place person data enters and leaves (subagent; choke points spot-verified)
- [x] Design v1 written; Codex design review: directionally sound, NOT safe as written (1 Critical, 5 High)
- [x] Design v2 (`docs/legal/audit-2026-11/06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md`) folds in every finding:
  - exact guarantee semantics;
  - durable send authorization;
  - structured subjects and versioned keys;
  - a dedicated flag;
  - an explicit erasure inventory with writer gates;
  - an activation guard;
  - decisions first.
- [ ] D: owner/counsel decisions (section 11 of the design). The owner chose to build the
      decision-independent groundwork first ("Phase 6 groundwork"); nothing goes live.
- [x] **P1 groundwork (Codex GO after 8 rounds):** `src/privacy/keys.py` and `src/privacy/subjects.py`.
  - The keyring: one settings snapshot; fallback only in listed dev/test environments with PII strict mode off;
    secrets of 32+ bytes; every kind's value must be canonical; addresses refused until their normaliser exists.
  - Subjects are read only in the source's declared order (`name_order_for`), reusing the export's
    `lead_formatting` rules. Each one is a person (one reading), a typed entity, or unresolved (review, no key).
  - Covered: shared surnames go to review; mixed person/entity cells split; nothing non-empty is dropped;
    estate and fiduciary capacity are marked, not decided (D4).
  - A pinned fingerprint ties `NORMALIZATION_VERSION` (n3) to the reused rules.
  - Codex rounds, in order: both-order collisions, entity overmatch, splitting, provenance; strict mode,
    shared surnames, canonical kinds, mixed cells; per-part guards, dropped inputs, addresses; part
    cleanup; weak parts, raw entity text, orphan markers; all-entity cleanup, estate grammar, version; underscore, empty slots.
- [x] **P2 groundwork (Codex consult then review):** migration 125 plus models, grant scripts, schema tests;
      `PRIVACY_ENFORCEMENT_VERSION` and `src/privacy/activation.py`.
  - Four tables: an immutable request envelope, an erasable encrypted payload, an append-only `seq`-ordered
    event trail, and append-only rule rows.
  - The DB activation guard (`state = 'pending'` only) plus the two-key app guard (code version AND setting).
  - UUID keys, RLS on with no policies; every role revoked in the migration and both grant scripts.
  - Consult changes adopted:
    - payload split from the envelope;
    - no DELETE-refusing triggers (retention is D5);
    - `actor_user_id` is not an FK to `users` (113 purge);
    - `seq` ordering;
    - rotation-aware UNIQUE;
    - no sequences;
    - a real RLS test.
- [ ] P3-P9: blocked on D (verification, co-owner, erasure scope, entity policy, retention, Tracerfy, notification).
- Interim until P7 ships: a manual procedure. Re-import cannot be prevented until then; counsel must know.

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
