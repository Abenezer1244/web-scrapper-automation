> **DRAFT, requires counsel review. Not legal advice. Not published.**

# Legal document package, Phase 7 drafts (2026-11)

**Date:** 2026-11-09. **Branch:** `docs/legal-drafts-2026-11`. **Facts checked against:** backend `main` at
`7aff3c76`. Nothing here is published, and no code was changed.

These drafts turn the findings of the November 2026 compliance audit (`../01` to `../08`) into the document
set BridgeLeads needs. They state product facts from this repository only. Every fact the repository cannot
supply is marked `[OPEN: ...]`; every legal judgement is marked `[COUNSEL: ...]` and written as a question.
The frontend repository (where the live policy pages are) is not in this worktree, so the drafts do not quote
the live pages; counsel should compare them.

They supersede `docs/legal/PRIVACY-POLICY-DRAFT.md` and `docs/legal/TERMS-OF-SERVICE-DRAFT.md` (2026-06),
which name a removed vendor (Anthropic) and a record type that is not live (eviction).

## 1. Index

| # | Document | Audience | Status | Main sources |
|---|---|---|---|---|
| 1 | `PRIVACY-POLICY-DRAFT.md` | Public | Draft. Blocked on entity facts, retention decisions, D8, counsel | `../01`, `../02`, `../03`, `../04`, `../06` s.2-3 and s.11; `src/config/constants.py` (`ALL_RECORD_TYPES`, `LEGAL_DOCUMENT_VERSIONS`); `src/config/settings.py`; `src/privacy/deadlines.py`; `src/workers/scheduler_helpers/retention.py` |
| 2 | `TERMS-OF-SERVICE-DRAFT.md` | Public | Draft. Most risk clauses are counsel placeholders | `docs/legal/TERMS-OF-SERVICE-DRAFT.md` (2026-06); `../01` P1-2, P1-3, P1-4, P1-7, P1-9, P1-10, P2-3; `src/config/settings.py` (`PLAN_LIMITS`, `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE`, `BILLING_PAST_DUE_GRACE_DAYS`); `src/config/constants.py` (`TRIAL_PERIOD_DAYS`, `RECORD_TYPES_BY_PLAN`); `alembic/versions/123_legal_acceptances.py`; `src/api/routes/billing.py` (`custom_text`) |
| 3 | `COOKIE-NOTICE-DRAFT.md` | Public | Draft. Do not publish until the site matches it (P1-8) | `../01` P1-8 and area 6; `../02` row 6; `../03` Google and Vercel rows; `docs/BUILD_JOURNAL.md` 2026-10-30 and 2026-11-09 |
| 4 | `ACCEPTABLE-USE-POLICY-DRAFT.md` | Public (part of the Terms) | Draft | 2026-06 Terms s.5; `../01` P1-2, P1-3, section C; `../06` s.2 |
| 5 | `RETENTION-POLICY-DRAFT.md` | Internal | Draft. Most periods are owner decisions | `../04`; `src/config/settings.py` (`RETENTION_PURGE_*`, `SKIP_TRACE_*_RETENTION_DAYS`, `EXPORT_RETENTION_DAYS`); `src/workers/scheduler_helpers/retention.py`; migrations 125, 135, 137; `src/workers/privacy_requests.py` |
| 6 | `BREACH-RESPONSE-PLAN-DRAFT.md` | Internal | Draft. Roles and notice law open | `../01` area 14 and C-12; `../02`; `../03`; `../08` s.C-D; `src/config/settings.py` (ops alert, heartbeat, Sentry, secret names); `src/utils/error_tracking.py`; `src/privacy/authorize.py` |
| 7 | `PRIVACY-REQUEST-PROCEDURE-DRAFT.md` | Internal (operators) | Draft. Usable now for the CLI steps; gaps listed | `scripts/privacy_admin.py`; migrations 125, 135, 136, 137; `src/privacy/deadlines.py`; `src/api/routes/privacy.py`; `src/workers/privacy_requests.py`; `src/workers/privacy_erasure.py`; `../06`; `tasks/todo-privacy-phase6-p8.md`; `tests/test_privacy_admin_cli.py` |
| 8 | `SUBPROCESSOR-LIST-DRAFT.md` | Public | Draft. No vendor agreement on file | `../03`; `src/config/settings.py`; `src/utils/error_tracking.py` |

### Must stay consistent with code

- **Privacy Policy and Terms:** publishing either means bumping `LEGAL_DOCUMENT_VERSIONS` in
  `src/config/constants.py` (privacy is `2026-11-08`, terms `2026-10-01` today), because signups record the
  accepted version in `legal_acceptances`.
- **Privacy Policy s.8 and the Procedure:** must match the D1 verification flow in migration 137 (opt-out is
  verified by the email link; delete, access and correct go to `in_review`), the deadlines in
  `src/privacy/deadlines.py`, and the email copy in `src/workers/privacy_requests.py` ("We will respond by
  <date>").
- **Procedure:** must match the CLI commands and the lifecycle rules in migration 136. Changes to either must
  update the Procedure in the same pull request.
- **Retention Policy and Privacy Policy s.7:** every "code today" statement must stay true, and every adopted
  period needs a job that enforces it before it is published.
- **Subprocessor List:** Sentry must be added before `SENTRY_DSN` is set; 2Captcha listed only if
  `CAPTCHA_ENABLED` is on.

## 2. Open facts (owner must supply)

Grouped; the document numbers in brackets show where each appears.

**Company**
1. Legal entity name, entity type, state of formation. [1, 2, 7]
2. Business mailing address (also needed for `COMPANY_POSTAL_ADDRESS`, which holds all promotional email until
   set). [1, 2, 7]
3. Publication dates for each document. [1-4, 8]
4. Trademark clearance for "BridgeLeads". [2]
5. Inbound email provider for privacy@, security@, legal@, support@, who reads each, and its retention. The
   brief for this package says routing is live; the repository still records it as pending (see section 4). [1, 5, 6, 8]

**Production configuration** (code defaults are off; confirm each)
6. `PRIVACY_INTAKE_ENABLED` and `NEXT_PUBLIC_PRIVACY_FORM_ENABLED`, and the form URL. [1, 7]
7. `PRIVACY_ENFORCEMENT_VERSION` on api, worker and beat (journal says 1 since 2026-11-09). [7]
8. `ACCOUNT_DELETION_ENABLED`, `ACCOUNT_EXPORT_ENABLED`. [1, 2, 5, 7]
9. `RETENTION_PURGE_ENABLED` and `RETENTION_PURGE_DRY_RUN`. [1, 5]
10. `CAPTCHA_ENABLED`, `SENTRY_DSN`, `OPS_ALERT_EMAIL`, `OPS_ALERT_WEBHOOK_URL`, `OPS_HEARTBEAT_URL`. [6, 8]
11. `RLS_ENFORCE=true` and non-bypass database roles (audit P1-12). [1]
12. Current county coverage (production `county_connectors` dump). [1]
13. Which ops alerting vendors are live: Grafana Cloud IRM, Better Stack, or both. [6, 8]

**Retention and records**
14. Retention period for lead rows (code keeps them indefinitely). [1, 5]
15. Retention for `audit_events`, Railway and Loki logs, `job_logs`. [1, 5, 6]
16. Billing record period (deletion email says 7 years; accountant to confirm). [1, 5]
17. Backup and point-in-time recovery window. [1, 5]
18. Google Analytics data retention setting; Google Signals and enhanced measurement state. [3, 5]
19. Periods for `pending_skip_trace_rows`, R2 exports, `nts_notices`, `king_parcel_owners`, the Tracerfy link. [5]
20. Retention of `legal_acceptances`, promotional opt-outs, `privacy_email_outbox`, `privacy_erasures`. [5]
21. A legal-hold mechanism (none in code). [5, 6]

**Product and process gaps**
22. Public sample endpoint reduced before publishing (audit P1-6). [1]
23. Contact-lookup acknowledgement before the first lookup (audit P1-3). [2, 4]
24. Do-Not-Call screening decision (B1-11). [1, 2, 4]
25. GPC handling and the consent model for analytics (B1-10); live cookie inventory including the sign-in
    cookie name and lifetime. [3]
26. Refund and chargeback policy (B1-5); cancellation path; annual-plan downgrade behaviour (P1-9, unverified);
    sales tax (B1-12). [2]
27. Plan table: in the Terms or only on the pricing page. [2]
28. Access-report tool, correction process, erasure-progress command, reply templates, fixed lists of
    verification-level and decision codes, a CLI search for the person's parcel. [1, 7]
29. Deletion of request records after 24 months (D5 adopted, no job). [1, 5, 7]
30. Incident roles, contacts, incident-record location, review cadence, tabletop exercise, communication
    templates, key-rotation runbooks. [6]
31. Vendor agreements (DPAs and the Tracerfy agreement) and each vendor's data region and security contact.
    [1, 2, 6, 8]
32. Whether customers outside the US are accepted (B1-9). [1, 3]

## 3. Counsel questions

### Owner decisions adopted 2026-11-05, pending counsel (design `../06` section 11)

| # | Decision | Adopted default | Where it appears |
|---|---|---|---|
| D1 | Verification | Opt-out: email link only. Delete and access: email link plus name and property-address match. Agents: signed permission. | 1 s.8.3; 7 Step 2 |
| D2 | Co-owners | Withhold the whole lead row from delivery | 7 Step 3 |
| D3 | Erasure | Delete: blank person data, keep a nameless property record for billing. Opt-out: suppress, no erasure. | 1 s.8.4; 7 Step 6a |
| D4 | Entities | Estate, trustee and heir covered; pure companies declined with a fixed code | 1 s.8.4; 7 Step 3 |
| D5 | Retention of request records | Records 24 months; requester contact deleted 24 months after close; rules indefinitely | 1 s.7; 5 s.2.3; 7 s.10 |
| D6 | Tracerfy | Ask for deletion where possible; never report vendor deletion as done unless confirmed | 1 s.8.5; 7 Step 6a |
| D7 | Customer notification | None until counsel confirms a basis | 1 s.8.5; 2 s.5; 4 s.5; 7 s.9 |
| D8 | Data broker registration and DROP | Not built until counsel decides; intake is ready for a `drop` channel | 1 s.8.7; 6 s.7 q5; 7 s.2 |
| D9 | Privacy-choices page copy | Drafted by engineering, for counsel review | 1 s.8; form copy and email copy in the frontend and `src/workers/privacy_requests.py` |

### D8 in detail: data broker status and DROP

Is BridgeLeads a data broker under the California Delete Act, Vermont, Oregon, Texas (SB 2121) or New Jersey
law, given Washington-sourced records that include owners with out-of-state mailing addresses and the sale of
looked-up phone numbers and emails? Must it register, and by when? If registered in California, it must
retrieve and process DROP deletion requests (the audit and the P8 plan note a duty to retrieve at least every
45 days from 2026-08-01, which counsel should confirm). The previously published policy said "where required,
we register", with no registration on record. Nothing about registration should be published until this is
decided.

### Other counsel questions by document

**Privacy Policy (1)**
- Whether delivering lead records and looked-up contacts to customers is a "sale" or "sharing" under any
  applicable state law, and how sections 6 and 8 must describe it.
- Which rights to offer, to whom (everyone or by state), and the response deadlines (owner targets: opt-out 15
  business days, others 45 calendar days, `src/privacy/deadlines.py`), including extension rules and
  receipt-confirmation duties.
- Sufficiency of the D1 verification standard and of agent authority checks.
- Whether keeping suppression rules indefinitely after deletion is permitted (D5).
- How to describe sources whose terms restrict automated or commercial use, and lists subject to RCW
  42.56.070(8) acknowledgements (audit C-2, C-3).
- Sensitive categories: death, divorce, financial distress, death-certificate fields (audit C-4).
- Rights of customers (business users) under state privacy laws; EU and UK customers (B1-9).
- Business-transfer and legal-disclosure clauses; change-notice method.

**Terms (2) and AUP (4)**
- Renewal, annual-plan and cancellation disclosures (ROSCA; CA ARL and NY GBL 527-a for B2B), audit C-10.
- Liability cap, warranty disclaimer, indemnity, governing law, venue, arbitration, class waiver (C-9).
- Whether "continued use means acceptance" works, or re-acceptance is needed (C-9).
- Scope of the licence to lead data; resale, team and affiliate use; survival after termination.
- Pass-through terms required by the Tracerfy agreement (C-6).
- TCPA, DNC, RCW 80.36.390 and CEMA exposure as a vendor, and the exact lookup acknowledgement (C-8).
- FCRA position (C-7). Distressed-owner, elder-exploitation and UDAP controls (C-5).
- Whether customers must honor privacy requests for copies they hold (D7).
- Which laws to name in the AUP; whether sensitive record types need extra rules or gating (C-4).

**Cookie Notice (3)**
- Whether opt-in consent is needed for analytics for any audience served; California wiretap-style claims
  about analytics tags (P1-8).

**Retention (5)**
- The correct clock for contact-data retention (last lookup, first lookup, or lead creation), and any limit in
  the Tracerfy agreement.
- Record-keeping periods for privacy requests and acceptance records; legal-hold triggers.

**Breach Response (6)**
- Which state breach laws apply and what each requires; confirm the audit's reading of RCW 19.255.010.
- Whether exposed public-record data alone, or encrypted data with an unexposed key, is notifiable.
- Customer, vendor, regulator and payment-network notice duties (C-12).

**Procedure (7)**
- Whether an emailed opt-out may be verified by a reply from the same address, or the requester can be asked
  to use the web form.
- Access report scope (categories or specific pieces), and whether it lists customers who received the data.
- What "correction" means for data copied from public records.
- What to say after verification when no records are found.
- Proof required before lifting an opt-out; extension notice wording.

**Subprocessor List (8)**
- Advance-notice and objection rights for new subprocessors.

### Carried over from the audit (`../01` section C)

C-1 data broker status (D8 above); C-2 automated access to King LandmarkWeb, Pierce ARMS, Clark LandmarkWeb and
iDocMarket, CAPTCHA solving, programmatic clickwrap and webdriver masking; C-3 RCW 42.56.070(8); C-4 sensitive
record types; C-5 distressed-owner outreach; C-6 Tracerfy contract; C-7 FCRA; C-8 TCPA/DNC; C-9 Terms
enforceability; C-10 negative option; C-11 verification standard and exemptions; C-12 breach notice.

## 4. Where the repository contradicts itself

1. **Operator deadlines vs the deadline rules.** `scripts/privacy_admin.py record` sets the deadline to now
   plus `--deadline-days` (default 45 calendar days, UTC) and stores no `deadline_rules`, while
   `src/privacy/deadlines.py` (used by the web form) gives opt-outs 15 business days in California time with a
   rules version. An emailed opt-out recorded with the default gets 45 days. `event --extend-days` also counts
   from now, not from the existing deadline. (Handled by a manual step in the Procedure; engineering fix
   recommended.)
2. **Admin UI vs CLI.** Design `../06` section 9 specifies an admin interface (admin plus MFA step-up) that
   reviews candidates and "generates an access report". Only the CLI exists, and it has no access-report or
   correction command.
3. **Correction.** Design `../06` section 3 says a correction request "corrects, or suppresses the field".
   Migration 136 refuses rules for correction (and access) requests, and no field-level suppression exists.
4. **D5 retention.** Adopted as policy (24 months for request records), but no job deletes
   `privacy_requests` or `privacy_request_payloads`. Same pattern as `account_deletions` and
   `account_exports` ("24 months" in the design, no job; `../02` row 15, `../04`).
5. **Lead-row retention.** The previously published policy promised deletion of lead records after about 365
   days (`../01` P2-1, `../04`). `src/workers/scheduler_helpers/retention.py` records the owner decision to keep
   lead rows and remove only purchased contact data.
6. **Mailboxes.** This package's brief says mail routing for privacy@, security@, legal@ and support@ is live.
   The repository still records it as pending: `docs/BUILD_JOURNAL.md` 2026-11-09 ("owner: Cloudflare Email
   Routing"), `tasks/todo-privacy-phase6-p8.md` ("domain has no MX yet") and the `PRIVACY_INTAKE_ENABLED`
   comment in `src/config/settings.py`. The repository is probably just out of date; confirm.
7. **Ops alert vendor.** `src/config/settings.py` names Grafana Cloud IRM as the production webhook and
   heartbeat; `../08` section C and `../03` describe the webhook as Better Stack (`../03` already flags this).
8. **Missing file reference.** `../01` P2-2 says a fact-corrected policy draft is in `06-DRAFT-PRIVACY-POLICY.md`.
   No such file exists; `../06` is the privacy-requests design. This package's `PRIVACY-POLICY-DRAFT.md` fills
   that gap.
9. **P8 checklist.** `tasks/todo-privacy-phase6-p8.md` leaves every slice checkbox (P8a to P8f) unticked while
   its Review section, the journal and `main` show them built.
10. **Old drafts.** `docs/legal/PRIVACY-POLICY-DRAFT.md`, `docs/legal/TERMS-OF-SERVICE-DRAFT.md` and
    `docs/legal/README.md` (2026-06) list eviction records and Anthropic as a provider; both are wrong today
    (`ALL_RECORD_TYPES`; Anthropic removed in `ae1724ce`).
11. **Do-Not-Call.** The previously published Privacy Policy and Terms (`../01` P1-2) describe a Do-Not-Call
    indicator; `tracerfy_ingest.py` never populates one. These drafts say plainly that none is provided.
