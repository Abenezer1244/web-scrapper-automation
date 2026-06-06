# DNC Scrubbing Spike — Thread 2 of 3 (2026-06-05)

**Question:** Should BridgeLeads scrub leads against Do-Not-Call lists and populate
`phone_dnc_flag` itself (today always NULL → the downstream dialer scrubs)? What
data source, what cost, and is it legally BridgeLeads' job or the customer's?

**Method:** Bounded web research (no code). Findings below.

## Findings

### 1. Federal DNC registry access is per-SAN, licensed, not redistributable
- Access requires a **Subscription Account Number (SAN)** tied to a *Seller*; telemarketers
  must download + scrub against the registry **every 31 days**.
- **FY2026 cost:** $82 per area code, max **$22,626** for all area codes nationwide; **first 5
  area codes free**; exempt orgs (some charities/political) get it free.
- The registry data is **licensed for the subscriber's own compliance** — a lead-gen platform
  generally **cannot pull the federal list and redistribute DNC status to customers**. That's
  why commercial scrub-as-a-service vendors exist.

### 2. Legal liability sits with the CALLER, not the lead generator
- "The legal liability falls on the company that made the call, not the company that generated
  the lead." The **calling party (BridgeLeads' customer) bears ultimate TCPA liability** and must
  do its own 31-day scrub.
- BUT a lead generator can still face exposure if it **knowingly sells non-compliant leads** or
  fails documentation. So BridgeLeads-side scrubbing is a **risk-reducing value-add**, not a
  legal necessity for BridgeLeads itself.

### 3. Commercial scrub vendors (the buildable path)
| Vendor | Product | Notes |
|---|---|---|
| **DNC.com** (Contact Center Compliance) | DNCScrub® (200k rec/min or sub-second real-time API), Litigator Scrub® (TCPA serial plaintiffs), wireless/VoIP scrub | RESTful API, documented (docs.dncscrub.com), volume-tiered pricing (quote-based) |
| **Gryphon.ai** | In-path real-time call blocking on their own telephony | Heavier — gets in the call path; more than a flag |
| **PossibleNOW** | DNCSolution® 3.0 | Enterprise compliance suite |
- All require a **paid account + budget + a quote** (pricing isn't public). No account ⇒ nothing
  to build (no-mock rule).

## Recommendation
- **Liability is the customer's, not BridgeLeads'.** So DNC scrubbing is a **premium differentiator**,
  not a compliance gap BridgeLeads must close. The current honest model (`phone_dnc_flag` NULL +
  `dnc_scrubbed:false` → the dialer/customer scrubs) is **legally defensible as-is**.
- **If we build it:** integrate **DNC.com's real-time API** (licensed, covers federal+state+wireless+
  litigator, RESTful) — populate `phone_dnc_flag` at enrichment time via an env-keyed, SSRF-safe vendor
  client (mirror `webhook_delivery.py`/`safe_http` patterns), cache results (TTL), and STILL surface
  "you must run your own 31-day scrub" (we reduce risk, we don't make the customer compliant).
- **Do NOT** pull the federal registry directly and redistribute (license/redistribution problem) and
  **do NOT** manage per-customer SANs (heavy, data still can't be centralized).
- **Verdict: DEFER unless a paying customer asks for it** — it's value-add, gated on a vendor account +
  budget the business must commit. Build = ~M (one vendor client + enrichment hook + cache + tests),
  blocked only on the vendor decision, not engineering.

## Sources
- FTC — 2026 telemarketer DNC fees: https://www.ftc.gov/news-events/news/press-releases/2025/08/telemarketer-fees-access-ftcs-national-do-not-call-registry-increase-2026
- FTC — Q&A for Telemarketers & Sellers (DNC provisions): https://www.ftc.gov/business-guidance/resources/qa-telemarketers-sellers-about-dnc-provisions-tsr-0
- DNC.com — DNCScrub® / Litigator Scrub® / API docs: https://www.dnc.com/ , https://docs.dncscrub.com/api-reference/litigator/overview
- Gryphon.ai automated DNC/TCPA: https://gryphon.ai/our-advantage/automated-dnc-tcpa-compliance/
- ActiveProspect — Do-Not-Call rules guide (liability on the caller): https://activeprospect.com/blog/do-not-call-rules/
- ClickPoint — scrub leads against federal/state DNC (2026): https://blog.clickpointsoftware.com/scrub-leads-against-federal-and-state-dnc-lists
