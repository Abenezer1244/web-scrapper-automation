# BridgeLeads — Legal & Compliance Pack

**DRAFTS — generated 2026-06-02. Not legal advice. Not launch-ready until a licensed attorney reviews them.**

Produced by auditing the actual codebase (`src/db/models.py`, `src/config/settings.py`, `src/api/middleware/`,
`src/scrapers/`, `requirements.txt`). Every data-collection claim is grounded in a code reference. `[BRACKETS]`
mark facts only you can supply (legal entity, jurisdiction, address, contact email).

## Read this first

BridgeLeads processes **two separate populations of personal data**:

1. **Your customers** — the investors who sign up. Standard SaaS privacy/ToS coverage handles this.
2. **The people in the lead lists** — owners in probate / pre-foreclosure / tax-delinquency / divorce / eviction,
   plus their **skip-traced phone numbers and emails**. They never signed up, never consented, and will mostly
   never know you hold their data.

Population #2 is where the launch-blocking exposure lives. Reselling identified individuals' contact data at scale
makes BridgeLeads a likely **"data broker"** under a growing set of US state laws:

| Law | Requirement | Deadline |
|---|---|---|
| California Delete Act (SB 362) / CCPA-CPRA | Register with the CPPA; honor universal deletion via DROP (2026) | Annual, by Jan 31 |
| Vermont 9 V.S.A. §2446 | Register + meet security standards | Annual, by Jan 31 |
| Oregon HB 2052 | Register before collecting/selling/licensing brokered data on OR residents | Per statute |
| Texas SB 2105 | Register to do data-broker business in TX | Per statute |

These laws carry **thresholds**, so a lawyer must confirm which you actually trip: California status flows from
being a CCPA "business" ($25M revenue, 100k+ consumers/households, or ≥50% of revenue from selling/sharing PI);
Texas applies only above 50% indirect-data revenue or data on 50,000+ individuals. Given skip-traced phone/email
resale at scale, the likely answer in several states is yes. This is item #1 on the
[PRE-LAUNCH-LEGAL-CHECKLIST](./PRE-LAUNCH-LEGAL-CHECKLIST.md).

## Documents

| File | What it is |
|---|---|
| [DATA-INVENTORY-AND-COMPLIANCE-AUDIT.md](./DATA-INVENTORY-AND-COMPLIANCE-AUDIT.md) | Every data point, every sub-processor, severity-tagged risk register (code-grounded) |
| [PRIVACY-POLICY-DRAFT.md](./PRIVACY-POLICY-DRAFT.md) | Draft covering both data populations |
| [TERMS-OF-SERVICE-DRAFT.md](./TERMS-OF-SERVICE-DRAFT.md) | Draft ToS: liability limits, acceptable use, customer compliance duties (TCPA/FCRA/distressed-owner) |
| [PRE-LAUNCH-LEGAL-CHECKLIST.md](./PRE-LAUNCH-LEGAL-CHECKLIST.md) | Manual steps: data-broker registration, trademark, DPAs, sub-processor list |

## What these drafts do not claim

- **That you are "legally covered."** You are not until a data-broker/privacy attorney has reviewed both the
  business model and these drafts.
- **That GDPR is the biggest risk.** It probably isn't — the scraped data subjects are US owners in US public
  records, so GDPR likely reaches only your EU/UK customers, if any. US data-broker, TCPA, and distressed-owner
  exposure is larger and nearer.
- **That scraping these portals is settled-legal.** Captcha-solving via 2Captcha (`CAPTCHA_ENABLED`) circumvents
  an access control — a different legal posture from passively reading public data. Flagged in the audit (H-3) as
  a business-risk item for counsel, not something a document fixes.
