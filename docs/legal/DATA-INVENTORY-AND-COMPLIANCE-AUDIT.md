# BridgeLeads — Data Inventory & Compliance Audit

**Generated 2026-06-02 by codebase audit. NOT legal advice.**
**Method:** read `src/db/models.py`, `src/config/settings.py`, `src/api/middleware/{security,rate_limit}.py`,
`src/api/routes/auth.py`, `src/utils/logger.py`, `requirements.txt`. No analytics, crash-reporting, or device-
fingerprinting SDK exists in the backend (verified by grep for posthog/sentry/mixpanel/ga/amplitude/datadog/
firebase/hotjar/fullstory — all hits were in the unrelated `marketingskills/` folder, not the app).

---

## 1. Data population A — Account holders (your customers)

These are the real-estate investors who register. This is "personal data of users" in the ordinary SaaS sense.

| Data point | Where stored | Code reference |
|---|---|---|
| Email address | `users.email` | `models.py:32` |
| Password (bcrypt hash, never plaintext) | `users.password_hash` | `models.py:33` |
| Prior password hashes (reuse prevention) | `password_history.password_hash` | `models.py:77-84` |
| API key (SHA-256 hash) | `users.api_key_hash` | `models.py:34` |
| Plan, usage counters, quotas | `users.plan / records_used / records_limit / records_period_start` | `models.py:35-43` |
| Skip-trace usage counters | `users.skip_trace_used_this_month / skip_trace_period_start` | `models.py:47-48` |
| Stripe customer ID | `users.stripe_customer_id` | `models.py:44` |
| Trial end / billing dates | `users.trial_ends_at` | `models.py:45` |
| Referral code, referrer link, credit balance | `users.referral_code / referred_by_user_id / referral_credit_cents` | `models.py:53-60` |
| Admin / active / session-revocation flags | `users.is_admin / is_active / revoked_at` | `models.py:61-69` |
| Account timestamps | `users.created_at / updated_at` | `models.py:70-71` |
| **IP address** (login, register, logout, password events, job creation) | Application logs / Loki (NOT a DB table) | `security.py:521-536`, `rate_limit.py:71-102` |
| **Submitted email on failed login** | Application logs | `auth.py:235` (`detail=f"email={body.email}"`) |
| IP address (ephemeral rate-limit key) | Redis (Upstash), short TTL | `rate_limit.py` |
| Payment card / bank details | **Never touches our servers** — held by Stripe | `requirements.txt:51` (stripe SDK) |

**Auth model:** Bearer JWT in the `Authorization` header. No first-party tracking cookies were found in the backend
(cookie references are only in scraper code reading *target* sites, and in the log-redaction filter). → No backend
cookie-consent obligation; confirm the **separate frontend repo** (`bridgeleads-web`, Next.js on Vercel) for any
analytics/cookies that would require a banner.

---

## 2. Data population B — Scraped third parties (the lead lists) ⚠️ PRIMARY RISK

These are **identified individuals who never signed up**: property owners and named parties appearing in county
public records for probate, pre-foreclosure, tax-delinquency, divorce, code-violation, and eviction matters
(record types per `CLAUDE.md`). The record type itself is sensitive — it reveals death of a relative, financial
distress, or marital dissolution.

| Data point | Where stored | Code reference |
|---|---|---|
| Party / owner name | `results.party_name`, `county_records.party_name` | `models.py:142, 248` |
| Heirs (probate) | `results.heirs`, `county_records.heirs` | `models.py:143, 249` |
| Property address | `results.property_address`, `county_records.property_address` | `models.py:146, 252` |
| Mailing address | `results.mailing_address`, `county_records.mailing_address` | `models.py:147, 253` |
| Parcel ID / legal description | `results.parcel_id / legal_description` | `models.py:144-145` |
| First / last name (skip-trace input) | `pending_skip_trace_rows.first_name / last_name` | `models.py:368-369` |
| Mailing name + address breakdown | `pending_skip_trace_rows.mail_*` | `models.py:370-373` |
| **Phone number** (skip-traced) | `results.phone`, `skip_trace_cache.phone` | `models.py:152, 289` |
| **Phone type** (Mobile/Landline/VoIP) | `results.phone_type` | `models.py:153` |
| **Do-Not-Call flag** | `results.phone_dnc_flag`, `skip_trace_cache.phone_dnc_flag` | `models.py:154, 291` |
| **Email address** (skip-traced) | `results.email`, `skip_trace_cache.email` | `models.py:155, 292` |
| Raw skip-trace vendor response | `skip_trace_cache.raw_response` (JSON) | `models.py:293` |
| Arbitrary enrichment payload | `results.enrichment_data`, `county_records.enrichment_data` (JSON) | `models.py:148, 254` |
| Exported lead CSVs (all of the above) | Cloudflare R2 bucket `bridgeleads-exports` | `settings.py:64-74` |

**Retention controls that already exist (good):**
- `RECORD_RETENTION_DAYS = 365` — `settings.py:186`
- `SKIP_TRACE_CACHE_DAYS = 90` — `settings.py:165`
- R2 exports default to **presigned/streamed URLs, not public** (`R2_ALLOW_PUBLIC_URLS = False`) — `settings.py:71-74`

Verify these retention windows are actually enforced by a running purge job (not just configured), and document them
in the privacy policy.

---

## 3. Sub-processors — third parties that receive personal data

Every external service below receives, stores, or processes personal data on your behalf. Each needs a signed Data
Processing Agreement (DPA) and a public sub-processor disclosure.

| Sub-processor | Data it receives | Population | Code reference |
|---|---|---|---|
| **Supabase** (PostgreSQL) | Everything at rest | A + B | `settings.py:30-31` |
| **Railway** (API + workers) | Everything in processing | A + B | deploy target (`rate_limit.py` proxy notes) |
| **Vercel** (frontend) | Customer account data in transit | A | `settings.py:123` |
| **Upstash Redis** | IP addresses (rate-limit keys), queue | A | `settings.py:35, 227-243` |
| **Cloudflare R2** | Exported lead CSVs (full seller PII) | B | `settings.py:64-74` |
| **Anthropic Claude API** | Scraped page HTML/text for AI extraction — may contain seller PII | B | `settings.py:142-145`, `requirements.txt:35` |
| **Tracerfy** (skip trace) | Owner name + address → returns phone/email | B | `settings.py:160-170`, `requirements.txt` |
| **2Captcha** | Target-site captcha challenges | (target sites) | `settings.py:148-150`, `requirements.txt:38` |
| **Regrid** (property data, optional) | Parcel/address lookups | B | `settings.py:152-154` |
| **Stripe** | Customer billing identity + payment | A | `settings.py:76-100` |
| **Resend** | Customer email + delivered export links | A (+ B in attachments) | `settings.py:102-104` |
| County GIS / assessor sites | (data source, not recipient) | — | `settings.py:156-158` |
| Log aggregation (Loki, referenced) | IP + email from audit logs | A | `logger.py:11` comment |

---

## 4. Risk register (severity-tagged)

Severity follows the project's security-analyst convention (Critical / High / Medium / Low).

### 🔴 CRITICAL

**C-1 — Data-broker obligations not in place (registration + ongoing deletion/suppression).**
Reselling identified individuals' contact data from records about people you have no relationship with makes
BridgeLeads a likely "data broker" under the California Delete Act (SB 362), Vermont (9 V.S.A. §2446), Oregon
(HB 2052), and Texas (SB 2105).

These statutes carry thresholds — they are not uniformly "register before operating." California status flows from
being a CCPA "business" ($25M revenue, 100k+ consumers/households, or ≥50% of revenue from selling/sharing PI);
Texas applies only above 50% indirect-data revenue or data on 50,000+ individuals. Several exclude government-record
"publicly available" info — but skip-traced phone/email is unlikely to qualify as merely public-record data.

Registration is the visible obligation. The harder one is operational: you must be able to delete and suppress a
named individual across `results`, `county_records`, `skip_trace_cache`, `pending_skip_trace_rows`, and exported
files in R2, on request, on a clock. That capability does not exist in the code today (see H-1).
→ **Fix:** Counsel confirms which states you trip; register where required; build the deletion/suppression machinery
(checklist item 1). No document resolves this.

**C-2 — Predatory / distressed-owner targeting and UDAP exposure.**
By design, BridgeLeads finds **people in distress** — probate, pre-foreclosure, tax-delinquency, divorce,
eviction — and hands investors their name, home address, and a skip-traced cell phone + email
(`models.py:142-156`). That is a vulnerable-population targeting tool, and it is the risk most likely to draw a
regulator, a state AG, or the press. Legal surfaces beyond privacy law:
- **Foreclosure-rescue / equity-purchaser statutes** — many states restrict soliciting owners in foreclosure
  (notice, cooling-off, mandated contract forms).
- **Elder financial-exploitation laws** — some owners are elderly; pressured investor outreach can trigger these.
- **UDAP** (FTC Act §5 + every state's mini-UDAP statute) — if customer outreach scripts are deceptive or
  high-pressure.
→ **Fix:** (a) ToS acceptable-use now requires compliance with foreclosure-rescue and elder-protection laws and
bans deceptive scripts (ToS §5(d)). (b) Counsel must review the **business model**, not just the documents — this
is a product risk, not a policy risk. (c) Consider gating or excluding the most sensitive record types.

### 🟠 HIGH

**H-1 — No rights mechanism for the scraped data subjects.**
Under CCPA/CPRA (and GDPR Art. 14 for any EU data subjects), the *people in the lists* may have rights to notice,
deletion, and to opt out of sale/sharing. There is **no code path** for a non-customer to submit a deletion / "Do
Not Sell or Share My Personal Information" request. A data broker must provide one (and, in CA, honor universal
DROP deletion requests from 2026).
→ **Fix:** Build an intake (email alias + a form/endpoint), a process to delete matching `results` /
`skip_trace_cache` / `county_records` / R2 rows, and document it in the privacy policy.

**H-2 — TCPA / Do-Not-Call exposure on skip-traced phones.**
The product stores `phone_dnc_flag` (`models.py:154`). TCPA exposure is real for automated/prerecorded/artificial-
voice calls and texts to mobile numbers (statutory damages $500–$1,500 per call/text), and scraped numbers carry
no consent. The federal National-DNC angle is narrower: it turns on "telephone solicitation" (47 CFR 64.1200(f)),
which targets calls encouraging the *called party's* purchase — so "I want to buy *your* house" outreach maps less
cleanly to federal DNC. State mini-TCPA laws may be broader. The risk is genuine; its contours depend on call type
and state. Capturing the DNC flag is good; how customers use these numbers is the exposure.
→ **Fix:** (a) ToS clause requiring TCPA / state mini-TCPA / DNC compliance + indemnity (ToS §5(a)). (b) Consider
flagging or withholding `phone_dnc_flag = true` numbers in exports.

**H-3 — Captcha circumvention + portal-ToS scraping.**
`CAPTCHA_ENABLED` / 2Captcha (`settings.py:148-150`) actively defeats an access control. Combined with scraping
county portals that may forbid automated access in their terms, this raises CFAA / breach-of-contract / reputational
risk that is distinct from "public data is fair game."
→ **Fix:** Counsel review of each target portal's ToS; document which sources are truly open vs. access-controlled.
Not a documentation fix.

### 🟡 MEDIUM

**M-1 — GDPR applies to EU/UK customers if you have any.**
The scraped data subjects are US property owners (EU GDPR likely does not reach them), but **EU/UK customers** make
you a controller of *their* account data: lawful basis, sub-processor DPAs, US-transfer SCCs, and a DSAR process.
→ **Fix:** Either geofence signups to US/CA only at launch, or implement the GDPR baseline. Decide explicitly.

**M-2 — Sub-processor DPAs + public disclosure missing.**
No signed DPAs / public sub-processor list for the §3 vendors (Tracerfy, Anthropic, Stripe, Resend, Cloudflare,
Supabase, etc.). Required by CCPA service-provider rules and GDPR Art. 28.
→ **Fix:** Sign each vendor's DPA; publish a sub-processor list (checklist item 4).

**M-3 — Personal data in application logs / Loki.**
IP addresses are logged on every audit event, and the **submitted email is logged on failed login**
(`auth.py:235`). Logs are personal data.
→ **Fix:** Define log retention + access control; consider hashing/truncating IP and dropping the raw email from
`login_failure` detail.

**M-4 — FCRA misuse risk.**
Lead lists are generally *not* FCRA "consumer reports" — but only if not used for credit, insurance, employment, or
tenant-screening eligibility. Skip-traced data makes misuse easy.
→ **Fix:** ToS prohibition on FCRA-regulated uses (drafted).

**M-5 — Retention enforcement unverified.**
`RECORD_RETENTION_DAYS` / `SKIP_TRACE_CACHE_DAYS` are configured but I did not confirm a purge job runs.
→ **Fix:** Confirm a scheduled deletion task exists and runs; otherwise data is retained indefinitely contrary to policy.

### 🟢 LOW

**L-1 — Cookie / ePrivacy banner.** Backend uses Bearer JWT, no tracking cookies found. Confirm the separate
frontend repo for analytics/cookies before deciding on a banner.

**L-2 — Children's data (COPPA/age).** B2B product; low risk. Add a "not directed to anyone under 18 / not a
consumer-eligibility tool" clause (drafted).

**L-3 — Breach-notification readiness.** Most states + GDPR require breach notification on a clock. Have an incident
runbook that maps to the sub-processors in §3. (`docs/security/` already exists — extend it.)

---

## 5. What this audit did NOT cover

- The **frontend repo** (`bridgeleads-web`, separate) — its analytics, cookies, marketing pixels, and consent UI.
- Marketing/email-list consent (CAN-SPAM / CASL) for your own customer mailings.
- The actual contractual terms of Tracerfy / 2Captcha / county portals (read them — they bind you).
- Whether configured retention jobs actually run (verify operationally).
