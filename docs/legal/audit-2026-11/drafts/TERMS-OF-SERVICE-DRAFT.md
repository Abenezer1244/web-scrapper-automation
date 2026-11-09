> **DRAFT, requires counsel review. Not legal advice. Not published.**

# BridgeLeads Terms of Service (draft)

**Draft date:** 2026-11-09. **Facts checked against:** backend `main` at `7aff3c76`.
**Replaces:** `docs/legal/TERMS-OF-SERVICE-DRAFT.md` (2026-06). That draft named Anthropic as a provider
(removed from the product) and listed eviction records (not live).

## Drafting notes (remove before publishing)

- Product facts come from this repository; sources are listed in `README.md` of this folder.
- `[OPEN: ...]` marks a fact the owner must supply. `[COUNSEL: ...]` marks a legal choice. Every clause
  that limits liability, shifts risk, or chooses law or forum is a counsel decision; the wording here is a
  starting point only.
- The Acceptable Use Policy is a separate draft in this folder and is incorporated by reference (section 6).
- **Must match code.** Acceptance is recorded per version in `legal_acceptances` (migration 123). When this
  text is published, bump `LEGAL_DOCUMENT_VERSIONS["terms"]` in `src/config/constants.py` (currently
  `2026-10-01`). Whether existing customers must re-accept is open (engineering Phase 3d, counsel question
  on the change clause).
- Plan names, quotas, trial terms and prices change in code. Before publishing, re-check every number in
  section 4 against `src/config/settings.py` (`PLAN_LIMITS`, `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE`) and
  `src/config/constants.py` (`TRIAL_PERIOD_DAYS`, `RECORD_TYPES_BY_PLAN`), or move them to the pricing page.

---

**`[OPEN: legal entity name]` BridgeLeads Terms of Service**
**Last updated:** `[OPEN: publication date]`

## 1. Agreement

These Terms are an agreement between `[OPEN: legal entity name, entity type, state]` ("BridgeLeads", "we")
and the business or person who creates an account or uses the BridgeLeads service ("Customer", "you"). You
accept these Terms by checking the box at signup. We record which version you accepted and when.

You must be at least 18 and use the service for business purposes. `[COUNSEL: whether to state that the
service is not offered to consumers, and the effect of that statement.]`

## 2. The service

BridgeLeads collects information from public records (for example probate, pre-foreclosure, tax delinquent,
code violation, divorce, death certificate and trustee sale records), organizes it into lead lists, and
delivers the lists to you. At your request we look up phone numbers and email addresses for people named in
leads, using a third-party provider. We may add, change or remove features, data sources, counties and record
types. `[COUNSEL: whether removal of a source or record type during a paid term needs notice or a remedy.]`

## 3. Accounts and security

- Keep your password, multi-factor codes and API keys confidential. You are responsible for activity under
  your account.
- Tell us at security@bridgeleads.io if you suspect unauthorized use.
- One account is for one customer. `[OPEN: owner to confirm seat and sharing rules]`

## 4. Plans, trial, billing

### 4.1 Plans

Plans differ in monthly record limits, the record types included, and included contact lookups, as shown on
our pricing page at the time you subscribe. `[OPEN: decide whether to state the plan table here or only on
the pricing page. Code today: Starter 50 records per month (probate only), Pro 1,000, Business 5,000, Agency
unlimited; Pro includes probate, pre-foreclosure, tax delinquent and trustee sale; Business and Agency include
every live record type.]`

### 4.2 Free trial

A new account can start a 7-day trial of the Pro plan without a payment card. The trial includes 25 contact
lookup credits for the life of the account. **The trial does not turn into a paid subscription by itself.**
You choose whether to subscribe when it ends.

### 4.3 Paid subscriptions

- Subscriptions are billed monthly or annually in advance through Stripe.
- Contact lookups beyond the lookups included in your plan are billed as metered usage at the rates shown at
  the time of use.
- **Renewal.** `[COUNSEL: auto-renewal disclosure and consent wording for monthly and annual plans (audit
  P1-7, question C-10). Stripe Checkout already shows renewal text at the submit button
  (`custom_text` in `src/api/routes/billing.py`); this clause must match it word for word.]`
- **Annual plans.** `[OPEN: show the annual total charged up front. Counsel: renewal reminder duty.]`
- **Cancellation.** `[OPEN: how to cancel (Stripe customer portal or Settings) and when cancellation takes
  effect. COUNSEL: wording.]`
- **Plan changes.** `[OPEN: audit P1-9 says an annual downgrade may forfeit prepaid time with no credit;
  unverified. Engineering must confirm the actual behaviour before this clause is written.]`
- **Refunds.** `[OPEN: owner decision B1-5, refund policy and what happens to access after a refund or a
  chargeback.]`
- **Taxes.** `[OPEN: owner and accountant decision B1-12. The service does not collect sales tax today.]`
- **Failed payments.** If a payment fails, you keep full access for 7 days while Stripe retries. After that,
  new billable work stops until payment succeeds. Your data and past exports are not deleted because of a
  failed payment.
- **Price changes.** `[COUNSEL: notice period and wording.]`

## 5. Your data and the lead data

- **Your content.** You keep ownership of your configurations, lists and uploads. You let us use them to run
  the service for you.
- **Lead data.** We grant you a non-exclusive, non-transferable right to use the lead data delivered to you for
  your own internal business purposes, subject to these Terms and the Acceptable Use Policy. `[COUNSEL: whether
  any resale or sharing with team members or affiliates is allowed; whether the right survives termination for
  data already downloaded.]`
- **Contact data.** Phone numbers and email addresses come from a third-party provider and are subject to the
  terms that provider places on us. `[OPEN: the Tracerfy agreement has not been reviewed (audit B1-6, C-6).
  This clause may need specific pass-through restrictions.]`
- **Privacy requests from people in lead data.** When a person named in our records opts out or asks for
  deletion, we stop delivering records naming that person, including in future deliveries to you.
  `[COUNSEL: whether customers must also act on such requests for copies they already hold, and whether we
  must notify them (owner decision D7).]`

## 6. Acceptable use

Your use must follow the Acceptable Use Policy at `[OPEN: URL]`, which is part of these Terms. In short: you
are responsible for complying with telemarketing, Do-Not-Call, texting, email, fair credit reporting,
foreclosure-rescue, elder-protection, fair-housing and privacy laws when you use lead data.

**We do not screen phone numbers against any Do-Not-Call registry, and leads do not carry a Do-Not-Call
indicator.** You must screen numbers yourself before calling or texting. `[OPEN: owner decision B1-11.
COUNSEL: question C-8 and the wording of the acknowledgement shown before the first contact lookup (audit
P1-3).]`

## 7. Accuracy

Lead data comes from government records, newspapers, automated collection and a third-party contact provider.
It can be incomplete, out of date or wrong. A record type describes the source document; it is not a statement
about any person's circumstances. Verify information before relying on it.

## 8. Third-party services

The service uses third-party providers (see our Subprocessor List). Integrations you connect (for example
webhooks, Zapier or PhoneBurner) are your choice and are governed by their own terms.

## 9. Our property

We own the service, its software and design, and the BridgeLeads name and logo. `[OPEN: trademark clearance
(audit B1-7)]`. You may not copy, scrape, reverse engineer or resell the service.

## 10. Suspension and termination

- You can stop using the service and delete your account at any time. `[OPEN: confirm
  ACCOUNT_DELETION_ENABLED is on in production; otherwise say "by contacting support".]`
- We may suspend or end your access if you breach these Terms or the Acceptable Use Policy, if required by
  law, or to protect the service or other people. `[COUNSEL: notice and cure wording.]`
- After an account deletion request, your account and its data are removed after 30 days, except records we
  must keep (for example billing records). See the Privacy Policy.
- Sections that by their nature should continue (for example 5, 7, 11 to 14) continue after termination.

## 11. Disclaimer of warranties

`[COUNSEL: warranty disclaimer wording and enforceability.]`

## 12. Limitation of liability

`[COUNSEL: cap amount and exclusions. The 2026-06 draft proposed fees paid in the 12 months before the claim;
this is a placeholder, not a recommendation.]`

## 13. Indemnity

`[COUNSEL: scope. The 2026-06 draft had you indemnify us for claims arising from your use of lead data, your
breach of the Acceptable Use Policy, and your violation of telemarketing, credit reporting and privacy laws.]`

## 14. Governing law and disputes

`[OPEN: governing law and venue. COUNSEL: arbitration, class-action waiver, small-claims carve-out.]`

## 15. Changes to these Terms

`[COUNSEL: question C-9. Whether "continued use means acceptance" is enforceable, or whether we must ask
customers to accept the new version. The code records each accepted version, so either approach is
possible.]`

## 16. Notices and contact

- Legal notices: legal@bridgeleads.io and `[OPEN: mailing address]`.
- Support: support@bridgeleads.io. Privacy: privacy@bridgeleads.io. Security: security@bridgeleads.io.

## 17. General

`[COUNSEL: entire agreement, assignment, severability, no waiver, force majeure, export controls if any.]`
