# BridgeLeads compliance, privacy, security and launch-readiness audit

**Date:** 2026-11-04. **Status:** Phase 1 (investigation) complete. No code changed, nothing deployed.
**Not legal advice.** This is an engineering and evidence audit. Every legal interpretation below is marked
for qualified counsel. Nothing here says BridgeLeads is, or is not, legally compliant.

**Code audited:** backend `origin/main` @ `91398055`, frontend `origin/master` @ `b495637` (fresh worktrees
`audit/compliance-2026-11` in both repos). **Live site:** `https://bridgeleads.io` and
`https://api.bridgeleads.io`, read-only headless Chromium (Playwright, desktop 1366x900 and iPhone 13), plus
DNS lookups. No form was submitted, nobody logged in, and no production data was touched.

**Builds on:** `docs/legal/DATA-INVENTORY-AND-COMPLIANCE-AUDIT.md` (2026-06), `tasks/BRIDGELEADS-COMPLIANCE-AUDIT-PHASE1.md`,
`docs/HANDOFF-compliance-audit-2026-09-17.md`. Each prior claim was re-verified against current code (see
`02-DATA-PROCESSING-INVENTORY.md` section "Prior claims").

**Evidence labels**
- **[V-code]** verified in current code.
- **[V-live]** verified against production.
- **[V-dns]** verified by a public DNS lookup.
- **[Unverified]** needs owner or production confirmation.
- **[Counsel]** needs a legal interpretation.

---

## H. Launch-readiness decision

**Not ready, because of confirmed critical issues.** Four of them, each verified:

1. **Nobody can reach BridgeLeads about privacy, security or legal matters** [V-live, V-dns].
   - The Privacy Policy and Terms route every request to `privacy@`, `security@` and `legal@bridgeleads.com`.
   - `bridgeleads.com` is not the business's domain. It has no MX record and publishes `v=spf1 -all`.
   - `bridgeleads.io` has no MX record and no DMARC record either. Every customer reply to `support@bridgeleads.io` bounces.
   - So the policy's promised deletion and opt-out channel does not exist in practice.
2. **The policy promises rights to non-customers that the system cannot fulfil** [V-code].
   - No route, table, workflow or suppression list exists for property owners.
   - A deleted person comes back on the next scrape.
   - The policy says "We act as a data broker in certain U.S. states. Where required, we register." No registration is on record [Unverified].
3. **Placeholders are live in both legal pages** [V-live].
   - Privacy has 5 (legal entity, security-log retention, country, entity, mailing address).
   - Terms has 6, including inside Limitation of Liability, Indemnification and Governing Law, and a published note to counsel ("[Dispute resolution / arbitration / venue clause to be set by counsel.]").
   - Whether those clauses bind anyone is a counsel question [Counsel].
4. **Several sources ban automated access, and the code solves CAPTCHAs** [V-code; terms quoted from live pages].
   - King recorder (LandmarkWeb), Pierce ARMS, Clark LandmarkWeb and iDocMarket (Columbia) publish terms banning automated or high-volume access, data mining or programmatic download.
   - Pierce ARMS, Pierce ATIP and iDocMarket publish `robots.txt` files that disallow the paths used.
   - The code solves reCAPTCHA through 2Captcha at King's recorder and at Pierce ATIP. The `CAPTCHA_ENABLED` value in production is [Unverified].

Once these are fixed, the rest falls into three groups: **owner decisions** (section B), **counsel review** (section C) and **safe engineering fixes** (section A). Those are below.

---

## B. Risk register (ranked)

Severity is the realistic exposure, not "a document is incomplete". Classes:
- **CV** confirmed violation or defect
- **CG** confirmed technical gap
- **PE** potential legal exposure
- **FE** requires further evidence
- **NA** not applicable

### P0: before any public launch or new paid customer

| ID | Finding | Class | Evidence | Fix | Who |
|---|---|---|---|---|---|
| P0-1 | Legal contact channels are dead (see H1) | CV | DNS: `bridgeleads.com` has no MX and SPF `-all`. `bridgeleads.io` has no MX and no DMARC. `privacy/page.tsx:156,172,190`, `terms/page.tsx:159`. Senders: `settings.py:205-215` | Owner sets up inbound mail on `bridgeleads.io` (for example Cloudflare Email Routing, which is free on the existing zone). Then we change every legal address to `@bridgeleads.io` and add SPF and DMARC. | Owner, then Eng |
| P0-2 | No rights mechanism for non-customer data subjects, and no suppression against re-import | CG / PE | No route, table or column (grep of `src/`, `alembic/`). The policy (section 8) promises access, deletion and opt-out to "people in our lead records who are not customers". | Build privacy-request intake, a verification workflow, a person-level suppression list checked on insert and on delivery, and an erasure fan-out across `results`, `skip_trace_cache`, `pending_skip_trace_rows`, `nts_notices`, `king_parcel_owners` and R2 exports. (Phase 3 engineering, shape in section A.) | Eng + Counsel (verification standard, exemptions) |
| P0-3 | Unfinished placeholders in the published legal pages | CV | Live text. `privacy/page.tsx:20,141,181,189`; `terms/page.tsx:125,134,150,151,158` | The owner supplies the facts in section B1. Counsel reviews. Then we publish. | Owner + Counsel |
| P0-4 | Automated scraping of sources whose terms prohibit it, and CAPTCHA circumvention | PE | `05-SOURCE-RIGHTS-REGISTER.md`. `captcha.py`. `king_wa_probate.py:303-400,667`. `pierce_atip.py:267,295`. Programmatic clickwrap acceptance, including force-enabling a disabled accept button (`tyler_selfservice.py:177`). `navigator.webdriver` masking and a spoofed Chrome UA (`base_scraper.py:317,405-410`, `browser_identity.py`). | Owner confirms production `CAPTCHA_ENABLED`. Counsel opinion per source (CFAA, WA computer trespass RCW 9A.90, contract). Until then, consider pausing the CAPTCHA-solving paths and the four sources with explicit bans. | Owner + Counsel |
| P0-5 | Data-broker registration status unknown while the policy claims "where required, we register" | FE / PE | `privacy/page.tsx:111`. California Delete Act: registration due Jan 31, and DROP deletion processing has been mandatory for registered brokers since 2026-08-01 with $200/day penalties. VT (amended, effective 2027-01-01), OR, TX (SB 2121, 50%-of-revenue test) and NJ (registration ~2027-03) also have registries. Out-of-state mailing addresses (for example CA owners of WA property) are in the data. | Counsel decides which registries apply. If any apply: register, and wire DROP / deletion into P0-2. | Counsel + Owner |

### P1: high, fix before scaling paid acquisition

| ID | Finding | Class | Evidence | Fix | Who |
|---|---|---|---|---|---|
| P1-1 | **RCW 42.56.070(8) "lists of individuals for commercial purposes"** | PE | Snohomish Treasurer bulk list. King eRealProperty owner names (checkbox acknowledgement on the page). PACS / Clark PIC / GIS mailing blocks. All gated by an owner "clearance" dated 2026-10-02 and `COUNTY_GIS_RESTRICTED_MAILING_ENABLED`, with no counsel memo in the repo. | Counsel memo before relying on these. The owner confirms the production flag value. | Counsel |
| P1-2 | DNC indicator promised but never populated. NULL counts as dialer-ready. DNC not exported. | CV (disclosure) / PE | `tracerfy_ingest.py:885` writes `phone_dnc_flag=None`. `jobs.py:689-693,2612` use `include_unknown_dnc=True`. `lead_export.py` has no DNC column. Privacy section 3 discloses "a Do-Not-Call indicator". Terms section 5(a) says to "honor any Do-Not-Call indicator". | Correct the policy text (Eng draft, counsel approves). Add the DNC notice to batch export and remove the dialer-delivery claim that "your dialer performs the final DNC scrub" (Eng). Decide whether to buy a DNC scrub (Owner). | Eng + Owner |
| P1-3 | No TCPA / permitted-use acknowledgement before contact lookup | PE | `ContactLookupDialog.tsx`, `FieldsStep.tsx:212-232`. Backend lookup schema has no acknowledgement (`schemas.py:2389-2481`). A results-page banner exists (`results/[id]/page.tsx:1351-1366`). | A one-time clickwrap acknowledgement, versioned and stored, before the first lookup or skip-trace run. Exact wording from counsel. | Eng + Counsel |
| P1-4 | Terms acceptance is never recorded (**fixed on local branches, Phase 3a-c**: append-only `legal_acceptances`, recorded at signup and verify; enforcement 3d pending; ships BE first) | CG | The signup checkbox is enforced client-side only (`register/page.tsx:49-51,521-571`). The payload has no terms fields (`:232-243`). `users` has no column. | Send `terms_version` at registration. Persist `terms_version` and `terms_accepted_at` (plus `privacy_version`). Reject registration without them. | Eng |
| P1-5 | Promotional emails have no unsubscribe, no List-Unsubscribe header, no postal address, and no preference store (**fixed on local branch, Phase 4**: one-click opt-out, address-keyed suppression; commercial mail HELD until the owner sets COMPANY_POSTAL_ADDRESS) | CV (CAN-SPAM 15 U.S.C. 7704(a)(3),(a)(5)) | Day-1 nudge, day-3 activation and the trial-ending upsell (`onboarding_emails.py:248-411`, `scheduler_helpers/onboarding.py`). `email_layout.py` footer. A repo-wide grep finds no "unsubscribe" anywhere. | Add a marketing opt-out column, a signed one-click unsubscribe route and RFC 8058 `List-Unsubscribe` headers on promotional mail only, and skip opted-out users. The postal address needs owner input. | Eng + Owner (address) |
| P1-6 | Unauthenticated `/scrapers/sample` publishes real people's first name + last initial, city, ZIP+4 and recording date | CV (data minimisation) | Live GET returns 5 Pierce records, for example `{"party_name":"<first> H.","mailing_address":"SUMNER, WA, 98390-1231","date_recorded":"09/01/2026"}`. `public_cache.py:44-73`. ZIP+4 plus date plus county is plausibly re-identifiable against the recorder index. | Initials only, 5-digit ZIP, month-level date. Or synthetic display data. | Eng |
| P1-7 | Auto-renewal and cancellation terms missing from Terms and checkout | PE | Terms section 3 has no renewal, trial-conversion or cancellation terms. No `custom_text` or consent at Stripe Checkout (`billing.py:1192-1211`). The marketing pricing page shows "$159/mo billed yearly" with no upfront annual total ($1,910 / $4,790 / $14,390; `PlanCardsGrid.tsx:9-13`). ROSCA (15 U.S.C. 8403) applies to online negative-option sales. FTC Click-to-Cancel was vacated (8th Cir. 2025-07-08). CA ARL likely does not reach B2B [Counsel]. | Show the annual total. Add renewal and cancel text at checkout via `custom_text`. Add renewal and cancellation clauses to Terms (counsel). Add annual renewal reminders (`invoice.upcoming`). | Eng + Counsel |
| P1-8 | GA4 sets `_ga` cookies on the first page view with no consent or GPC check, and the banner text is false | PE / CV (banner accuracy) | [V-live] `_ga` and `_ga_CGXBSL5TJN` are set with no interaction on /, /pricing, /privacy and /terms (desktop and mobile). `site-analytics.tsx:86-91`. `CookieBanner.tsx:46` says "We use cookies to keep you signed in", on the landing page only. No `globalPrivacyControl` handling anywhere. Disclosure is otherwise accurate (privacy section 2). | Honour GPC and Do-Not-Track before loading gtag. Correct the banner copy. The owner decides opt-in consent (needed only if EU/UK visitors are in scope; also reduces CA CIPA wiretap-suit exposure [Counsel]). | Eng + Owner |
| P1-9 | Annual-plan downgrades forfeit prepaid time with no credit | PE (UDAP) | Reviewer claim: `billing.py:1296-1298,1614-1620`, `billing_entitlement.py:296-303`. **[Unverified by me; needs a Stripe test-mode reproduction before any change.]** | Schedule the downgrade at `current_period_end` (SubscriptionSchedule). | Eng (after repro) |
| P1-10 | Sales tax not collected | FE / PE | No `automatic_tax` (`billing.py:1192`). WA taxes many digital automated services [Counsel / accountant]. | Accountant decision, then Stripe Tax. | Owner + accountant |

| P1-11 | Tracerfy result CSVs are reachable via unauthenticated CDN URLs (traced phone numbers) | CG | Code says so itself (`retention.py:173-175`). The clear is gated by `RETENTION_PURGE_ENABLED` (completed queues 30 days). Added from the Codex consult. | Owner confirms the purge is live. Shorten the completed-queue window to ~1 day. Ask Tracerfy for authenticated or expiring links and file deletion. | Eng + Owner |
| P1-12 | RLS enforcement in production must be verified | FE | `RLS_ENFORCE` defaults to False (`settings.py:241`). A production boot with it off logs an error and sends an ops alert, deliberately non-blocking so the RLS rollback still works (`session.py:405-425`). The journal records production enforcement verified 2026-09-17. Codex rated this Critical; the repo design note governs the code shape, so it is a launch-gate **verification**, not a code change. | Owner confirms `RLS_ENFORCE=true` and non-BYPASSRLS roles on api and worker before launch. | Owner |

### P2: medium

| ID | Finding | Class | Evidence | Fix |
|---|---|---|---|---|
| P2-1 | Retention claims vs reality | CV (disclosure) | Policy: "Lead records ... approximately 365 days, then deleted". Code: lead rows are never deleted. The PII purge only NULLs phone and email, is off by default (`RETENTION_PURGE_ENABLED=False`, `DRY_RUN=True`, `settings.py:482-483`), and per my 2026-10-30 note was enabled on the Railway worker [Unverified now]. Never purged: `job_logs`, `audit_events` (IP, path), `pending_skip_trace_rows` (plaintext names), `nts_notices`, `king_parcel_owners`. The retention clock can be reset by error re-stamps (`retention.py:16-38`). | The owner picks the windows (section B1). Then make the policy match the code exactly, and add purges for the never-purged tables. |
| P2-2 | Privacy policy factual inaccuracies | CV (disclosure) | It lists "eviction" (not live). It omits `death_certificate` and `trustee_sale`. It omits 2Captcha (live call sites), Better Stack / Grafana (ops alerts, no PII by design) and Tracerfy's CDN download link. "each under a data-processing agreement" is [Unverified]. It lists Regrid (dead code path). The DNC claim is in P1-2. | Fact-corrected draft in `06-DRAFT-PRIVACY-POLICY.md` (Phase 2). |
| P2-3 | Chargebacks and refunds not handled | CG | Only 5 Stripe event types are handled. `charge.dispute.created` and `charge.refunded` are ignored (`billing.py:1932-1957`). | Ops alert on dispute (A). The refund entitlement policy is an owner decision. |
| P2-4 | `users` RLS policy is `USING (true)` for the app role | CG | `scripts/apply_rls_cutover_policies.sql:135-137`. The app-layer filters are correct (verified by the reviewer). | Self-row policy plus SECURITY DEFINER lookups. A separate security phase with its own migration review. |
| P2-5 | Tracerfy legacy path-secret webhook still enabled by default | CG | `settings.py:356` `TRACERFY_LEGACY_PATH_ENABLED=True`. Edge proxies log paths. | Repoint Tracerfy to the header route, set False, rotate the secret (Owner + Eng). |
| P2-6 | `EMAIL_VERIFICATION_ENABLED=False` default; a trial gives 25 paid lookups to any email | CG (cost / abuse) | `settings.py:229`. | Gate trial lookups on verified email. |
| P2-7 | Account deletion and account export ship default-off | FE | `ACCOUNT_DELETION_ENABLED` and `ACCOUNT_EXPORT_ENABLED` are False (`settings.py:474,477`). The production value is [Unverified]. The export omits the audit/IP history, sessions and billing. | The owner confirms. Then widen the export scope. |
| P2-8 | Trial disclosure mismatch (**fixed on FE branch `563fbce`**; the FAQ already disclosed it; "25 lookups" was corrected to "25 lookup credits" in 6 places) | CV (advertising) | The pricing page says "7-day Pro trial" and the Pro card says 250 lookups. The trial actually allows 25 lookups for the account's whole lifetime (`settings.py:394`). | Disclose "25 contact lookups during the trial". |
| P2-9 | "Straight from the counties, never a resold list" | PE (advertising) | `data.ts:292,299`, `about/page.tsx`. Phones and emails come from Tracerfy, a licensed provider. | Qualify the claim (owner copy decision). |
| P2-10 | CI token scope | CG | `ci-cd.yml` has no top-level `permissions:`. Actions are not SHA-pinned. `@railway/cli` is unpinned in a job holding a deploy token. | `permissions: contents: read`. Pin the CLI. |
| P2-13 | The one-time RLS cutover script, if re-run, would grant the worker INSERT/UPDATE on tables added after the cutover (`account_exports`, `job_degradations`, `pending_email_changes`, deletion tables) | CG | `scripts/_cutover_step2_grants_policies.py` blanket `GRANT ... ON ALL TABLES` with no take-back for those tables (fixed for 123/124 only). Codex review, Phase 4. | Add per-table take-back blocks mirroring `provision_rls_roles.sql`, or mark the script one-time-only and fail if re-run. |
| P2-11 | Stale audit/policy docs contradict code | CG | `connector-audit-2026-04-10.md`, `wa-tax-delinquent.md`, `wa-court-records.md`, `nts_columbian.py:5` (the robots claim is now false). | Mark superseded and point to the new source register. |

### P3: low

- The CSV formula-prefix set omits `|` and full-width `＝＋－＠` (`security.py:458`).
- Download tokens are labelled "single-use" but are not consumed (`jobs.py:2339`).
- Dead CSP grants: fonts.gstatic.com, my.spline.design, js.stripe.com.
- No `/.well-known/security.txt`.
- Test tooling ships in the production image.
- `stripe==11.4.0` held back.
- GSAP's "standard no-charge" licence and Remotion's company licence (dev only) need a licence check.
- Loki retention is undefined.

### Notes from the Codex consult

- **Ops-alert content (Codex, Medium, added as P2-12):** user-controlled scraper names and exception text can reach alert email and webhooks (`delivery.py:240-260`, `ops_alerts.py:262`). Redact names and addresses before emitting.
- **Transit processors (Codex, added to the inventory):** the Vercel `/api/backend` proxy and the Cloudflare proxy carry authorization headers, cookies, IPs and downloads.

### Likely not applicable (counsel to confirm; Codex: avoid categorical conclusions)

- **GDPR / UK GDPR for property owners.** The data subjects are US property owners, and nothing targets or monitors people in the EU (Art. 3(2)). Customer-side applicability depends on whether EU/UK customers are accepted (owner decision B1-9) [Counsel].
- **WA My Health My Data Act** for foreclosure, probate, tax and code records: these are not health data. **Edge case:** `death_certificate` records. Confirm the scraped fields carry no cause of death [Counsel].
- **FCC one-to-one consent rule:** vacated (11th Cir., *Insurance Marketing Coalition v. FCC*, 2025-01-24).
- **CFPB FCRA data-broker rule:** withdrawn 2025-05-15 (90 FR 20568).
- **FTC Click-to-Cancel rule:** vacated (8th Cir., 2025-07-08). An ANPRM was issued 2026-03-11. ROSCA still applies.
- **WA comprehensive privacy law:** none enacted as of 2026 (HB 1671 was not passed).
- **Volume-threshold state privacy laws** (CO, VA, OR, CT): unlikely at current scale, but must be monitored. Texas TDPSA still bars a small business from selling *sensitive* data without consent [Counsel].
- **Children's data:** a B2B, 18+ service.

---

## A. Review areas (status per area)

**Status key:** **R** = red (confirmed critical issue), **A** = amber (gap or exposure), **G** = green (control verified).

| # | Area | Status | Summary | Main IDs |
|---|---|---|---|---|
| 1 | Data lifecycle map | A | Mapped end to end (`02-...`). The main gaps are owner-side rights and indefinite retention. | P0-2, P2-1 |
| 2 | Privacy policy | R | 5 placeholders, a dead contact domain, and inaccurate DNC/retention/record-type/processor statements. | P0-1, P0-3, P1-2, P2-1, P2-2 |
| 3 | Terms of service | R | 6 placeholders. No renewal, trial or cancellation terms. Acceptance not recorded. Good acceptable-use, FCRA and TCPA clauses exist. | P0-3, P1-4, P1-7 |
| 4 | GDPR / UK GDPR | G / A | Likely not applicable to owner data. Customer-side depends on the EU/UK signup decision. | B1-9 |
| 5 | US privacy and data-broker law | R | Possible CA Delete Act / DROP, VT, OR, TX, NJ broker obligations. No rights machinery. | P0-2, P0-5 |
| 6 | Cookies and tracking | A | GA4 cookies before any choice. GPC not honoured. False banner copy. Otherwise accurately disclosed. Vercel tools are cookieless. No ad pixels or session replay (verified live). | P1-8 |
| 7 | Lawful basis | A | See `02-...` section "Purposes and legal authority". There is no documented authority for owner contact enrichment. | P0-4, P1-1 |
| 8 | Marketing consent and outreach | R | Our own promotional emails lack unsubscribe and address. The customer TCPA/DNC posture rests on Terms section 5(a) and banners, not a stored acknowledgement. | P1-2, P1-3, P1-5 |
| 9 | Access, correction, deletion | R / A | Customers: deletion and export are built (default-off). Owners: nothing. | P0-2, P2-7 |
| 10 | Retention | A | Retention matrix in `02-...`. Several tables are kept indefinitely. The policy overstates deletion. | P2-1 |
| 11 | DPAs | A | No signed DPA is evidenced for any vendor. Status register in `03-...`. | `03-VENDOR-REGISTER` |
| 12 | Third-party disclosure | A | All outbound transfers are mapped. 2Captcha and ops tooling are undisclosed. Anthropic was removed (verified). | P2-2 |
| 13 | Application security | G / A | Strong auth, session, webhook, idempotency, SSRF and CSV controls (verified). Gaps: users RLS, legacy webhook path, CI scope, email verification off. | P2-4, P2-5, P2-6, P2-10 |
| 14 | Breach response | A | Alerting exists (Grafana IRM pages, Better Stack uptime, heartbeat; per ops notes). No runbook mapping to notice duties. WA: consumers within 30 days, and the AG within 30 days if more than 500 WA residents (RCW 19.255.010). Runbook draft is a Phase 2 deliverable. | P0-1 (no security inbox) |
| 15 | Intellectual property | A | One author identity in both repos (no third-party contributors besides dependabot). No LICENSE / proprietary notice. Trademark clearance not done. Domain `bridgeleads.com` is not owned (for sale). Assets: one logo, marketing images, Google fonts self-hosted (OFL). | B1-7 |
| 16 | Open-source licences | G | Backend: 108 packages. Copyleft only LGPL psycopg2-binary (dynamic linking) and MPL certifi. FE production: no GPL/AGPL; LGPL libvips via sharp; GSAP custom licence. `pip-audit`: none found. `npm audit` (prod): 0. SBOMs in `sbom/`. | P3 |
| 17 | Payments, trials, subscriptions | A | Trial needs no card and never auto-charges (good, verified). Gaps: annual total not shown, no renewal disclosure or consent at checkout, no annual reminders, disputes and refunds unhandled, possible annual-downgrade forfeiture, no tax. | P1-7, P1-9, P1-10, P2-3, P2-8 |
| 18 | Public-record source rights | R | 4 sources with explicit automation bans. CAPTCHA solving. RCW 42.56.070(8) list exposure. Register in `05-...`. | P0-4, P1-1 |
| 19 | Contact enrichment (Tracerfy) | FE | The Tracerfy agreement was not provided. Redistribution and resale rights, permitted purposes and retention terms are unverified. `raw_response` is kept (encrypted) up to 90 days. | B1-6, C-6 |
| 20 | FCRA | G / A | Terms section 5(b) prohibits eligibility uses. No marketing for screening was found. Residual risk only. | C-7 |
| 21 | Implementation plan | - | `tasks/todo-compliance-audit-2026-11.md` (phased, approval-gated). | - |

---

## B1. Owner decision checklist (facts only you can supply)

1. Legal entity name, entity type and state of formation. Business mailing address (needed for the policies and the CAN-SPAM footer).
2. Working inboxes. Enable inbound mail on `bridgeleads.io` for `privacy@`, `security@`, `legal@` and `support@`, or name different ones.
3. Production values of `CAPTCHA_ENABLED`, `COUNTY_GIS_RESTRICTED_MAILING_ENABLED`, `RETENTION_PURGE_ENABLED` / `_DRY_RUN`, `ACCOUNT_DELETION_ENABLED`, `ACCOUNT_EXPORT_ENABLED`, `EMAIL_VERIFICATION_ENABLED`, `TRACERFY_LEGACY_PATH_ENABLED`. Also a current `county_connectors` dump (county, record types, base_url, active).
4. Retention windows: lead rows (the policy says 365 days, which code does not do), security logs / `audit_events`, job logs, raw skip-trace responses, and backups / PITR.
5. Refund policy, and what happens to access after a refund or dispute.
6. Signed agreements: Tracerfy (permitted purpose, resale and redistribution rights), and DPAs with Supabase, Railway, Vercel, Google, Cloudflare, Upstash, Stripe, Resend and 2Captcha. The policy currently claims "each under a data-processing agreement".
7. Trademark search status for "BridgeLeads". Whether to buy `bridgeleads.com` or stop referencing it.
8. Whether the earlier "RCW 42.56.070 cleared" decisions (2026-09-14, 2026-10-02) rest on written counsel advice.
9. Customer geography: accept EU/UK signups or not.
10. Consent model for GA4: opt-in banner, or GPC-respecting opt-out.
11. Whether to buy DNC scrubbing, or keep "not screened" with a hard acknowledgement.
12. Sales tax: accountant's view on WA and other nexus.

## C. Questions for counsel (each with its evidence pointer)

1. **Data-broker status:** CA Delete Act (does "business" import the CCPA thresholds, including the 50%-of-revenue-from-selling prong?), VT, OR, TX (SB 2121) and NJ (P.L.2026 c.25), given WA-sourced records that include out-of-state owners. See P0-5.
2. **Source access:** may we keep automated access to King LandmarkWeb, Pierce ARMS, Clark LandmarkWeb and iDocMarket given their published terms, `robots.txt` and our CAPTCHA solving, programmatic clickwrap and webdriver masking? Exposure under CFAA, RCW 9A.90 and contract. See `05-...`.
3. **RCW 42.56.070(8):** does the restriction on agencies reach our downstream commercial reuse of owner names and mailing lists obtained from pages that require acknowledging the prohibition (King eRealProperty, Clark PIC, PACS, Snohomish Treasurer list)? Note that the subsection numbering differs ((8) vs (9)) across sources.
4. **Sensitive record types:** death certificates (MHMDA edge case; RCW 70.58A), divorce decrees and heirs. Should any be gated or excluded?
5. **Distressed-owner outreach:** WA Distressed Property Conveyances Act (RCW 61.34), state UDAP, elder-exploitation laws. Is Terms section 5(d) enough, or are product controls needed?
6. **Tracerfy:** does the contract permit resale and redistribution of phones and emails to our customers, and on what terms (retention, permitted use, suppression pass-through)?
7. **FCRA:** confirm the product is outside "consumer report" given the current marketing and Terms section 5(b).
8. **TCPA / DNC / WA RCW 80.36.390 / CEMA (amended 2026-06-11):** is BridgeLeads exposed as a vendor? Exact wording for the lookup acknowledgement.
9. **Terms enforceability:** liability cap, indemnity, governing law, venue / arbitration, and the "continued use constitutes acceptance" change clause.
10. **Negative option:** ROSCA disclosures for annual plans. Does CA ARL / NY GBL 527-a reach B2B subscribers?
11. **Verification standard and exemptions** for owner deletion requests (public-record and First Amendment considerations).
12. **Breach notice:** confirm the multi-state matrix and vendor notice clauses.
