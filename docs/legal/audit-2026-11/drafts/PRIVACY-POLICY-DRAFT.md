> **DRAFT, requires counsel review. Not legal advice. Not published.**

# BridgeLeads Privacy Policy (draft)

**Draft date:** 2026-11-09. **Facts checked against:** backend `main` at `7aff3c76`.
**Replaces:** `docs/legal/PRIVACY-POLICY-DRAFT.md` (2026-06), which lists a record type that is not live
(eviction) and a vendor that was removed (Anthropic).

## Drafting notes (remove before publishing)

- Every product fact below comes from this repository. File references are in `README.md` of this folder.
  Anything the repository cannot tell us is marked `[OPEN: ...]`. Anything that depends on law is marked
  `[COUNSEL: ...]` and is written as a question, not an answer.
- The live policy page lives in the frontend repository, which is not in this worktree. This draft does not
  quote it. Counsel should compare the two.
- **Must match code.** When this text is published, bump `LEGAL_DOCUMENT_VERSIONS["privacy"]` in
  `src/config/constants.py` and move the old value to `PREVIOUS_PRIVACY_VERSION`, as that file explains, so
  signups record the version the person actually saw.
- Statements marked **(conditional)** describe a feature that exists in code but is switched off by default.
  Each must be confirmed live in production, or removed, before publishing.

---

**Last updated:** `[OPEN: publication date]`

## 1. Who we are

BridgeLeads is operated by `[OPEN: legal entity name, entity type and state of formation]`, located at
`[OPEN: business mailing address]` ("BridgeLeads", "we", "us").

BridgeLeads is a business service for real estate professionals. We collect information from public records
held by US counties and cities, organize it into lead lists, and deliver those lists to our business customers.
At a customer's request we can also look up contact details (phone numbers and email addresses) for people named
in those records.

## 2. Two groups of people this policy covers

1. **Customers.** People and businesses who create a BridgeLeads account, and visitors to our website.
2. **People named in lead records.** Property owners and other people who appear in the public records we
   collect. These people did not sign up with us. Sections 4, 8 and 9 explain what we hold about them and the
   choices they have.

## 3. Information about customers and website visitors

| What | Examples | Where it comes from |
|---|---|---|
| Account details | Email address, a hashed password, password history, multi-factor authentication settings and backup codes, hashed API keys | You |
| Sign-in sessions | Session records including the browser user agent | Your browser |
| Security and audit records | IP address, the page or API path requested, the event, your account id | Your requests |
| Rate-limiting data | Keys derived from IP addresses, held for a short time | Your requests |
| Billing | Email address, Stripe customer id, plan and invoices. Card details are collected and held by Stripe, not by us. | You and Stripe |
| Agreement records | Which version of our Terms and this policy you accepted, and when | Signup |
| Email preferences | Whether you opted out of promotional email | You |
| Service emails | Your email address, plan and usage, and in a lockout notice the IP address involved | Our systems |
| Website analytics | See the Cookie Notice: a Google Analytics client id, the page address with query strings removed, the referring site (origin only), page title, device type and approximate location | Your browser |
| Your configurations and uploads | Saved searches, lists and settings | You |

## 4. Information in lead records

### 4.1 Record types

We currently collect these record types: probate, pre-foreclosure, tax delinquent, code violation, divorce,
death certificate and trustee sale. `[OPEN: confirm the published list matches ALL_RECORD_TYPES in
src/config/constants.py at publication]`

Our sources today are in Washington State. `[OPEN: confirm current county coverage from the production
county_connectors table before publishing]`

### 4.2 What a lead record can contain

- Names of people in the record: a party, a decedent, heirs, a spouse or former spouse, a grantor.
- Property address, mailing address, parcel number and legal description.
- The record type, its recording or filing date, and amounts in the record (for example a delinquent tax
  amount, a default amount, or the principal shown in a notice of trustee sale) and an auction date.

### 4.3 Where it comes from

- County recorder and auditor search portals.
- County assessor, treasurer and GIS records, used to find who owns a property and their mailing address.
- Government open-data portals (for example code-violation datasets).
- Legal notices published in newspapers (for example notices of trustee sale).

`[COUNSEL: several sources publish terms that restrict automated or commercial use, and some owner and
mailing lists carry a statutory acknowledgement. See audit file 05 and questions C-2 and C-3 in file 01.
Counsel should decide whether and how this policy describes those sources before it is published.]`

### 4.4 Contact lookups

When a customer asks for it, we send a person's first and last name and the property and mailing addresses to
our contact-lookup provider (listed in the Subprocessor List). The provider can return up to five mobile numbers,
three landline numbers, five email addresses, and the phone type.

**We do not currently screen numbers against any Do-Not-Call registry, and lead records do not carry a
Do-Not-Call indicator.** `[OPEN: owner decision B1-11, whether to buy DNC screening. If screening is added,
rewrite this paragraph.]`

### 4.5 Sensitive circumstances

Some record types reveal sensitive life events: a death, a divorce, or financial distress (foreclosure, trustee
sale, tax delinquency). `[COUNSEL: whether any state law treats these categories, or death-certificate fields,
as sensitive data requiring different handling or disclosure; question C-4 in file 01.]`

## 5. Why we use information

- To provide the service: run searches, build and deliver lead lists, and perform the contact lookups a customer
  requests.
- To bill customers.
- To keep the service secure: detect abuse, investigate incidents, enforce rate limits.
- To send service email (account, security, delivery) and, unless you opt out, promotional email about our
  service. Promotional email carries a one-click unsubscribe link. `(conditional: promotional email is held
  until COMPANY_POSTAL_ADDRESS is set)`
- To measure how our website is used (see the Cookie Notice).
- To honor privacy requests, including keeping a record that prevents a person who opted out or asked for
  deletion from coming back into lead lists (section 8.4).
- To meet legal obligations.

## 6. How information is shared

- **With customers.** Lead records, and contact details a customer paid to look up, are delivered to that
  customer as files and in the app.
- **With tools a customer connects.** A customer can send leads to its own systems, for example a webhook,
  Zapier or PhoneBurner. Those systems are chosen and controlled by the customer.
- **With service providers** who host, store, send or process data for us. They are listed in the Subprocessor
  List.
- **For legal reasons,** when required by law or to protect rights and safety. `[COUNSEL: wording]`
- **In a business transfer.** `[COUNSEL: whether to include, and wording]`
- **A public sample.** Our website shows a small sample of recent leads to visitors who are not signed in.
  `[OPEN: audit item P1-6. Today the sample shows a first name and last initial, city, ZIP+4 and recording
  date. Engineering should reduce it (initials, 5-digit ZIP, month only) before this policy is published, and
  this bullet should then describe what is actually shown.]`

`[COUNSEL: whether delivering lead records and looked-up contact details to paying customers is a "sale" or
"sharing" of personal information under any state law that applies to us, and therefore what this section and
section 8 must call it.]`

We do not sell customer account information.  `[OPEN: owner to confirm]`

## 7. How long we keep information

The Retention Policy draft in this folder lists every store. In summary, as the code behaves today:

| Information | How long |
|---|---|
| Customer account | For the life of the account. After a deletion request the account is removed after 30 days. (conditional: account deletion is off by default) |
| Billing records | Kept for tax and accounting. `[OPEN: period; the account deletion email says 7 years]` |
| Security and audit records (IP address, path) | `[OPEN: owner has not set a period. Today they are not deleted.]` |
| Lead records (names, addresses, record details) | `[OPEN: owner has not set a period. Today lead rows are not deleted; only contact details are removed, see next row.]` |
| Looked-up phone numbers and emails on lead records | Removed 365 days after the most recent lookup attempt. (conditional: RETENTION_PURGE_ENABLED) |
| Our cache of lookup results | Deleted after 90 days. (conditional: same setting) |
| Export files we store | Deleted after 365 days. (conditional: same setting) |
| Your account data export file | Deleted after 7 days |
| Privacy request records | Request records 24 months; the requester's contact details deleted 24 months after the request closes. `[OPEN: adopted as policy (D5) but no deletion job exists yet]` |
| Opt-out and deletion rules | Kept indefinitely, so the person is not added back |
| Backups | `[OPEN: database backup and point-in-time recovery window]` |

Files a customer has already downloaded, or sent to its own tools, are under that customer's control.

## 8. Choices for people named in lead records

### 8.1 What you can ask for

- **Opt out** of your information being delivered to our customers.
- **Delete** your personal information from our records.
- **Access**: ask what personal information we hold about you.
- **Correct** information about you that is wrong.

`[COUNSEL: which of these rights apply under the laws that cover us, whether to offer them to everyone
regardless of state (the current owner default is to offer them to everyone), and how to word them.]`

### 8.2 How to ask

- Online at `[OPEN: URL of the request form, /privacy/request]`. (conditional: PRIVACY_INTAKE_ENABLED)
- By email to privacy@bridgeleads.io.
- By mail to `[OPEN: mailing address]`.

You will be asked for your name, an email address, and the address or parcel number of the property. These
help us find the right records. We use the email address only to confirm the request and reply.

An authorized agent may ask on your behalf. We will ask for your signed permission before we act.

### 8.3 How we confirm it is you

- **Opt-out:** you click a confirmation link we email to you.
- **Deletion and access:** you click the confirmation link, and we also check that the name and property
  address match our records.
- **Agents:** we review your signed permission.

Until a request is confirmed we do not say whether we hold any information about you.

`[COUNSEL: whether this verification standard (owner decision D1) is sufficient and lawful.]`

### 8.4 What happens next

- **Opt-out:** we stop delivering records that name you to customers, and we stop contact lookups about you. We
  keep a protected record (a one-way code, not your name or address) so you are not added back when we collect
  new public records.
- **Deletion:** everything in an opt-out, and we also erase your name and other personal details from our
  stored records, lookup cache and stored export files. We keep a record of the property itself without your
  name, for billing records.
- **Access:** we send a report to the email address you confirmed. `[OPEN: no tooling builds this report yet;
  it is prepared by hand. Counsel to decide categories vs specific pieces.]`
- **Correction:** `[OPEN: no correction process exists in code. Owner and counsel to decide what a correction
  does for public-record data.]`

People named only as a company (for example an LLC or corporation) are not covered by these requests. People
named as an estate, trustee or heir are covered. `[COUNSEL: owner decision D4]`

### 8.5 What we cannot do

- Records a customer already downloaded, or already sent to its own tools, before your request took effect.
- Information our contact-lookup provider already holds. We will ask the provider to delete it where our
  agreement allows, and we will not tell you it was deleted unless the provider confirms it.
- A delivery that was already in progress at the moment your request took effect.

`[COUNSEL: whether we must notify customers who previously received your information (owner decision D7:
not until counsel confirms a basis).]`

### 8.6 How long it takes

We will respond within `[COUNSEL: deadline]`. Our current internal targets are 15 business days for opt-outs
and 45 calendar days for other requests, counted from the day after we receive the request.

### 8.7 Data broker registration

`[COUNSEL: owner decision D8. Whether BridgeLeads is a data broker under California, Vermont, Oregon, Texas or
New Jersey law, whether it must register, and whether it must process deletion requests from California's
Delete Request and Opt-out Platform (DROP). Do not publish any statement that we are, or are not, registered
until this is decided. The previously published wording "where required, we register" is not supported by any
registration on record.]`

## 9. Choices for customers

- **See and change** your account details in Settings.
- **Download a copy** of your account data (profile, configurations, batches, runs and lead files) as a ZIP
  file. (conditional: ACCOUNT_EXPORT_ENABLED)
- **Delete** your account in Settings. Your account is removed after 30 days, and you can cancel the deletion
  during that time. (conditional: ACCOUNT_DELETION_ENABLED)
- **Opt out** of promotional email with the link in any promotional email.
- Or email support@bridgeleads.io or privacy@bridgeleads.io.

`[COUNSEL: whether any state privacy law gives customers (business users) additional rights that must be
listed here, and whether we accept customers outside the US (owner decision B1-9).]`

## 10. Cookies and similar technologies

See the Cookie Notice. `[OPEN: link]`

## 11. Security

We protect information with measures including encryption of sensitive fields at rest, database row-level
security separating customers' data, multi-factor authentication for accounts, and restricted operator access.
No system is perfectly secure. If you believe you found a security problem, email security@bridgeleads.io.

`[OPEN: confirm RLS_ENFORCE is on in production (audit P1-12) before publishing the row-level security
claim.]`

## 12. Children

BridgeLeads is a business service for adults. We do not knowingly collect information from children through
accounts. Lead records come from public records and are not collected from the people named in them.
`[COUNSEL: wording]`

## 13. Where information is processed

We and our service providers process information in the United States. `[OPEN: confirm each vendor's data
region]`

## 14. Changes to this policy

We will post the new version here with a new "Last updated" date. `[COUNSEL: whether and how to notify
customers of material changes, and whether re-acceptance is required (engineering Phase 3d).]`

## 15. Contact

- Privacy: privacy@bridgeleads.io
- Security: security@bridgeleads.io
- Legal: legal@bridgeleads.io
- Support: support@bridgeleads.io
- Mail: `[OPEN: legal entity name and mailing address]`
