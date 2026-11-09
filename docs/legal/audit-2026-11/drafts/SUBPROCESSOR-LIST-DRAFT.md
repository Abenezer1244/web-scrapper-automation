> **DRAFT, requires counsel review. Not legal advice. Not published.**

# BridgeLeads Subprocessor List (draft)

**Draft date:** 2026-11-09. **Facts checked against:** backend `main` at `7aff3c76`.

## Drafting notes (remove before publishing)

- Built from `03-VENDOR-REGISTER.md` and re-checked against `src/config/settings.py` and
  `src/utils/error_tracking.py`.
- **No signed data processing agreement or provider agreement is on file for any vendor** (audit B1-6).
  Do not publish any statement that vendors operate "under a data processing agreement" until the owner
  produces them. The previously published policy made that claim.
- The public list (section 1) should contain only vendors that are live in production. Section 2 lists
  vendors that exist in code but are switched off, so the owner can confirm their production state.
- `[OPEN: data region of each vendor. All are believed to process in the US; unverified per vendor settings.]`

---

**Last updated:** `[OPEN: publication date]`

## 1. Service providers that process personal information for us

| Provider | What they do for us | Personal information involved | Whose | Location |
|---|---|---|---|---|
| Supabase | Database hosting | All stored data | Customers and people in lead records | `[OPEN]` |
| Railway | Hosting for our API and background workers; application logs | All data in processing; logs include IP addresses | Both | `[OPEN]` |
| Vercel | Website hosting; cookieless web analytics and speed measurement; request proxy for the app | Session cookies and requests in transit; page-view data | Customers and visitors | `[OPEN]` |
| Google (Google Analytics 4) | Website analytics | Analytics client id, page address without query strings, device, approximate location | Visitors | `[OPEN]` |
| Cloudflare | File storage (R2) for lead exports and account exports; DNS; network proxy | Export files, which can include names, addresses, phone numbers and emails; requests in transit | Both | `[OPEN]` |
| Upstash | Redis for rate limiting and the job queue | IP-derived keys; job data (sealed) | Both | `[OPEN]` |
| Stripe | Payments and billing | Email, customer id, plan, invoices, payment details (held by Stripe) | Customers | `[OPEN]` |
| Resend | Sending email (service, promotional, privacy-request confirmations) | Recipient email; message content, which can include signed download links and, in a lockout notice, an IP address | Customers and privacy requesters | `[OPEN]` |
| Tracerfy | Contact lookups (phone numbers and emails) | We send names and property and mailing addresses; we receive phone numbers, emails and phone type. Tracerfy hosts each result file at a link that needs no sign-in; we clear our copy of the link after 30 days (conditional). | People in lead records | `[OPEN]` |
| Grafana Labs (Grafana Cloud IRM) | Incident alert routing and heartbeat | Alert text with ids and counts; designed to carry no lead data | Customers (ids) | `[OPEN]` |
| Better Stack | Uptime monitoring and alert routing (per ops notes) | As above | Customers (ids) | `[OPEN]` |
| `[OPEN: inbound email provider for privacy@, security@, legal@, support@]` | Receiving email to our addresses | Anything a sender writes, including privacy requests | Anyone who writes to us | `[OPEN]` |

`[OPEN: Grafana vs Better Stack. settings.py names Grafana Cloud IRM as the production alert webhook; the
audit and ops_alerts notes name Better Stack. Confirm which are live and list only those.]`

## 2. In code, switched off by default (confirm before listing)

| Provider | What it would do | Personal information | State in code |
|---|---|---|---|
| 2Captcha | Solves CAPTCHAs on two county portals | The portal's site key and page address only; no lead data | Off unless `CAPTCHA_ENABLED=true`. `[OPEN: production value. COUNSEL: question C-2 on source access applies regardless.]` |
| Sentry | Error tracking | Exception type and stack frame locations only. Messages are replaced with "[redacted]"; request data, user, local variables and breadcrumbs are dropped (`src/utils/error_tracking.py`). | Off unless `SENTRY_DSN` is set. Before setting it: add Sentry to this list and the policy, and accept its DPA. |

## 3. Not subprocessors

- **Customer-chosen destinations.** Webhooks, Zapier and PhoneBurner receive leads only when a customer connects
  them. They act for the customer, not for us. The Privacy Policy says so.
- **Regrid.** In code but not used (`REGRID_ENABLED=false`, no caller). Do not list.
- **Anthropic.** Removed from the product. Do not list.
- **GitHub.** Hosts our source code and runs CI. It does not receive customer or lead data in normal
  operation. `[OPEN: owner to confirm no production data is used in CI.]`

## 4. Changes

`[COUNSEL: whether customers must get advance notice of a new subprocessor and a right to object, and how
(for example email to account owners, or a change log on this page).]`
