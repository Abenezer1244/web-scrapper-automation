# Privacy Policy — DRAFT

> **⚠️ DRAFT — NOT LEGAL ADVICE. Must be reviewed by a licensed attorney before publication.**
> Generated 2026-06-02 from a codebase audit (see `DATA-INVENTORY-AND-COMPLIANCE-AUDIT.md`).
> Replace every `[BRACKET]` with real values. This draft deliberately covers **both** the people who create accounts
> **and** the people whose information appears in the lead data, because BridgeLeads collects both.

**[LEGAL ENTITY NAME] ("BridgeLeads," "we," "us")**
Effective date: [DATE]
Contact: [PRIVACY CONTACT EMAIL] · [MAILING ADDRESS]

---

## 1. Who we are and what BridgeLeads does

BridgeLeads is a business-to-business service that compiles real-estate lead information from **public records**
(such as county recorder, assessor, and court filings) and related data sources, and delivers it to our
business customers (real-estate investors and professionals). This policy explains what personal information we
process, why, and the choices available to you.

This policy describes two groups of people:
- **Customers / account users** — people who register for and use BridgeLeads.
- **Data subjects in lead records** — individuals whose information appears in public records we compile (for
  example, property owners). You may be in this group even if you have never used BridgeLeads.

## 2. Information we collect from customers / account users

When you create or use an account, we collect:
- **Account data:** email address and a securely hashed password. *(`users.email`, `users.password_hash`)*
- **API credentials:** a hashed API key if you create one. *(`users.api_key_hash`)*
- **Subscription & usage data:** plan, record and skip-trace usage counters, trial and billing dates, referral
  code and referral relationships. *(`users` table)*
- **Payment data:** processed by **Stripe**. We store only a Stripe customer identifier — we never receive or store
  your full card number. *(`users.stripe_customer_id`)*
- **Security & log data:** IP address and request metadata recorded for authentication events, security auditing,
  and rate-limiting (abuse prevention). *(audit log + rate limiter)*

We do **not** use third-party advertising, analytics, or session-replay trackers in our application backend.

## 3. Information in lead records (data about third parties)

To provide the service, we compile information from public records and licensed data sources. Depending on the
source and your customer's configuration, a lead record may include:
- Name(s) and, for probate records, named heirs;
- Property address, mailing address, parcel identifier, and legal description;
- Record type and date (e.g., pre-foreclosure, tax-delinquency, probate, divorce, code violation, eviction);
- Where a customer enables enrichment / skip tracing: a phone number, phone type, a Do-Not-Call indicator, and/or
  an email address obtained from a third-party skip-trace provider.

We obtain this information from government public records and from the third-party providers listed in Section 6.
We do not knowingly collect this information directly from the individual.

## 4. Why we process information (purposes)

- To provide, operate, secure, and bill the service;
- To compile and deliver lead lists to the business customer who requested them;
- To prevent fraud and abuse and to comply with law;
- To communicate service and account messages.

[If you rely on GDPR "legitimate interests" for any EU customer or data subject, state it here and complete a
Legitimate Interests Assessment with counsel.]

## 5. How information is shared

We share information only as needed to run the service, with the sub-processors in Section 6, and:
- with the **customer** who configured the relevant lead job (lead records are delivered to that customer);
- to comply with law, legal process, or to protect rights and safety;
- in connection with a corporate transaction (merger, acquisition), subject to this policy.

**We are a data broker in certain U.S. states.** Where required, we register as a data broker and honor the rights
described in Section 8. [Confirm registration status with counsel — see PRE-LAUNCH-LEGAL-CHECKLIST item 1.]

## 6. Sub-processors

We use the following service providers, each under a data-processing agreement:

| Provider | Purpose |
|---|---|
| Supabase | Database hosting |
| Railway | Application/worker hosting |
| Vercel | Frontend hosting |
| Cloudflare R2 | Export file storage |
| Upstash (Redis) | Rate-limiting / queueing |
| Anthropic | AI-assisted data extraction |
| Tracerfy | Skip-trace (phone/email) lookups |
| Regrid | Property data enrichment (optional) |
| Stripe | Payment processing |
| Resend | Transactional & delivery email |

A current list is maintained at [SUB-PROCESSOR LIST URL].

## 7. Data retention

- Lead records are retained for approximately **[365] days**, then deleted. *(`RECORD_RETENTION_DAYS`)*
- Skip-trace results are cached for up to **[90] days**. *(`SKIP_TRACE_CACHE_DAYS`)*
- Account data is retained for the life of the account and as required by law/tax rules thereafter.
- Security logs are retained for [RETENTION PERIOD].

## 8. Your privacy rights

Depending on where you live (e.g., California/CCPA-CPRA, other US state laws, and the EU/UK GDPR for applicable
individuals), you may have the right to **access, correct, delete, or port** your information, and to **opt out of
the "sale" or "sharing"** of personal information.

**This includes people who are in our lead records but are not customers.** To exercise any right — including
requesting deletion of your information from our lead data — contact **[PRIVACY/DSAR EMAIL]** or use
**[DO-NOT-SELL / DELETION REQUEST URL]**. We will verify and respond within the timeframes required by law.

[Engineering note: a deletion request must purge matching rows in `results`, `skip_trace_cache`, `county_records`,
`pending_skip_trace_rows`, and any exported files in R2. This mechanism must exist before launch — see audit H-1.]

We do not sell information about individuals under 18, and the service is not directed to consumers or to anyone
under 18.

## 9. Security

We protect information using encryption in transit, hashed credentials, access controls, rate-limiting, and secret
redaction in logs. No method is perfectly secure. To report a vulnerability or suspected incident, contact
[SECURITY EMAIL].

## 10. International transfers

We are based in [COUNTRY] and process data in the United States. If we serve individuals in the EU/UK, transfers
rely on [Standard Contractual Clauses / other mechanism].

## 11. Changes

We will post changes here and update the effective date. Material changes will be communicated to account holders.

## 12. Contact

[LEGAL ENTITY NAME], [MAILING ADDRESS], [PRIVACY CONTACT EMAIL].
EU/UK representative (if applicable): [NAME/ADDRESS].
