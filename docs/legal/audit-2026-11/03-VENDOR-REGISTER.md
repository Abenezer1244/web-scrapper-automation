# Vendor and subprocessor register (2026-11-04)

**Contract status:** no signed DPA, MSA or provider agreement was provided for any vendor, so every "Agreement
status" below is **Unverified**. The live Privacy Policy states that each provider operates "under a data-processing
agreement". Until the owner produces the signed documents, that statement is unsupported.

"Vendor offers a standard DPA" reflects my general knowledge of these vendors. Confirm it on each vendor's
current legal page before relying on it. Not legal advice.

| Vendor | Role | Integration verified in code | Personal data | Population | Disclosed in policy? | Vendor offers standard DPA? | Agreement status | Gaps / owner action |
|---|---|---|---|---|---|---|---|---|
| Supabase | Postgres hosting (all data at rest) | Yes (`DATABASE_URL`) | Everything | A + B | Yes | Yes (click-through) | Unverified | Accept the DPA. Confirm region, PITR and backup retention (feeds the deletion promises). |
| Railway | API, workers, beat hosting; logs | Yes (deploy target) | Everything in processing; logs incl. IPs | A + B | Yes | Yes (terms) | Unverified | Confirm the log retention period (feeds the security-log retention placeholder). |
| Vercel | Frontend hosting, Web Analytics, Speed Insights | Yes | Session cookies in transit; analytics (cookieless) | A + visitors | Yes | Yes | Unverified | Accept the DPA. |
| Google (GA4) | Marketing-page analytics | Yes, `G-CGXBSL5TJN` (`lib/site.ts:15`) | Client id cookie, scrubbed URL, device, approximate location | Visitors | Yes | Google Ads/Analytics data processing terms (accept in the GA admin console) | Unverified | Accept the processing terms. Confirm enhanced measurement is off and set the data-retention setting (code comment `site-analytics.tsx:25-30` relies on it). |
| Cloudflare (R2, DNS, proxy) | Export file storage; api proxy | Yes (`S3_*`) | Full lead exports incl. phones | B | R2 only (the proxy is not mentioned) | Yes | Unverified | Accept the DPA. Mention the CDN/proxy. |
| Upstash | Redis rate limiting and queue | Yes (`REDIS_URL`) | IP-derived keys, job payloads | A (+B in task args, sealed) | Yes | Yes | Unverified | Accept the DPA. |
| Stripe | Payments | Yes | Email, user_id metadata, payment data (held by Stripe) | A | Yes | Yes (part of SSA) | Unverified | Confirm portal cancellation config, Stripe Tax decision. |
| Resend | Email delivery | Yes | Customer email, IP (lockout email), signed download links | A | Yes | Yes | Unverified | Confirm the sending domain's SPF/DMARC (no DMARC published). |
| **Tracerfy** | Skip trace (phones/emails) | Yes | **Sends** owner name and addresses. **Receives** phones and emails. Hosts the result CSV at an unauthenticated CDN URL. | B | Yes | Unknown | **Unverified, critical.** The provider agreement governs resale and redistribution rights. | Produce the agreement. Counsel to review permitted purpose, resale/redistribution, retention, suppression and opt-out pass-through, and accuracy. Do not enable new paid enrichment for testing. |
| **2Captcha** | CAPTCHA solving | Yes, gated `CAPTCHA_ENABLED` (default False) | Site key and page URL only (no lead PII) | n/a | **No** | Terms of service | Unverified | Disclose it if it is live, or remove it. The bigger issue is source rights (`05-...`). |
| Grafana Cloud IRM / Better Stack | Ops alert routing, uptime, heartbeat | Yes (`OPS_ALERT_WEBHOOK_URL`, heartbeat) | ids and alert text (no-PII rule in `ops_alerts.py`) | (A ids) | **No** | Yes | Unverified | Disclose them as service providers (low risk). Fix the inconsistent comments (`settings.py` says Grafana, `ops_alerts.py` says Better Stack). |
| Regrid | Property data | **Dead code** (`enrich_parcel` has no callers; `REGRID_ENABLED=False`) | none in practice | n/a | Yes ("optional") | n/a | n/a | Remove it from the policy, or keep it marked "not currently used". |
| Anthropic | (formerly) AI extraction | **Removed** (`ae1724ce`) | none | n/a | No (correct) | n/a | n/a | None. |
| GitHub | Source hosting, CI | Yes | Code. Historically leaked credentials (repo was public until 2026-10-31). | n/a | n/a | Yes | n/a | Confirm credential rotation is complete (memory: owner-pending for one app login). |
| Customer-directed (Zapier, PhoneBurner, customer webhooks) | Delivery at the customer's direction | Yes | Lead PII | B | Partly (section 5, "delivered to that customer") | n/a (the customer's own processors) | n/a | Name customer-directed integrations in the policy. |

## Subprocessor disclosure (proposed public list, pending owner confirmation of agreements)

Supabase (database), Railway (application hosting), Vercel (web hosting and cookieless analytics),
Google (website analytics), Cloudflare (file storage, network), Upstash (rate limiting and queues),
Stripe (payments), Resend (email), Tracerfy (contact lookup), Grafana Labs / Better Stack (operational alerting, no lead
data), and 2Captcha only if it stays in use.
