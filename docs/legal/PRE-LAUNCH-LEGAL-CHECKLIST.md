# BridgeLeads — Pre-Launch Legal Checklist (manual steps)

**Generated 2026-06-02. These are actions a person must take — code cannot complete them.**
Ordered by how badly they can block or sink a launch. Items 1–3 are the ones first-time builders miss.

---

## 1. 🔴 Data-broker registration — DO BEFORE SELLING

BridgeLeads compiles and sells personal information about individuals it has no direct relationship with. That is
the textbook definition of a **data broker** in several states. Registration is a prerequisite to operating, not a
nicety.

- [ ] Engage a privacy attorney to confirm data-broker status (it is very likely "yes").
- [ ] **California:** register under the Delete Act (SB 362) with the CPPA. Annual; deadline **Jan 31**. Plan for
      the DROP universal-deletion mechanism (2026+).
- [ ] **Vermont:** register under 9 V.S.A. §2446. Annual; deadline **Jan 31**.
- [ ] **Oregon** and **Texas** (SB 2105): check registration thresholds and register if required.
- [ ] Stand up the consumer deletion / opt-out intake the registrations require (also audit finding **H-1**).

## 2. 🟠 Trademark clearance — DO BEFORE PUBLIC BRANDING

If "BridgeLeads" is already trademarked by someone else in a related class, you can be forced into a full rebrand
after launch (cease-and-desist), losing domain, marketing, and app-store listings.

- [ ] **USPTO (United States):** search "BridgeLeads" and close variants in the federal trademark database
      (`https://tmsearch.uspto.gov`) — focus on Class 35 (advertising/business/data services) and Class 42
      (software/SaaS). Also search for live "Bridge…" lead-generation marks.
- [ ] **IP Australia** (if launching in Australia): search the Australian Trade Mark Search
      (`https://search.ipaustralia.gov.au/trademarks/search/quick`).
- [ ] **Common-law / domain check:** Google + state business registries + the `.io`/`.com` domain you use.
- [ ] If clear, consider filing your own application (USPTO Class 35/42) to secure the mark.
- [ ] If NOT clear, decide on a name change **before** any public launch, paid marketing, or app-store submission.

> Note: I cannot perform the trademark search for you — it requires querying the live USPTO/IP Australia registries
> and a legal judgment on class and likelihood of confusion. This is a manual step. If you want, I can draft the
> exact search queries and the classes to file in.

## 3. 🟠 Scraping & source-legality review

- [ ] Have counsel review each county portal's Terms of Use against your scraping, and specifically the use of
      **2Captcha** (`CAPTCHA_ENABLED`) to bypass access controls (audit finding **H-3**).
- [ ] Read and comply with **Tracerfy** and **2Captcha** contractual terms — they bind your downstream use of the
      data they return.
- [ ] Document which data sources are genuinely open-public vs. access-controlled.

## 3b. 🔴 Distressed-owner targeting / UDAP review (audit C-2)

The business model targets people in distress (foreclosure, probate, divorce, eviction). This is a *product* risk,
not just a documentation one — and the one most likely to attract a regulator, state AG, or press attention.

- [ ] Have counsel review the model against **state foreclosure-rescue / equity-purchaser statutes** in the states
      you operate (notice, cooling-off, and contract-form rules vary widely).
- [ ] Confirm **elder financial-exploitation** and **fair-housing** exposure given that some owners are elderly or
      protected-class.
- [ ] Assess **UDAP / FTC Act §5** exposure for the outreach your customers run on your data.
- [ ] Decide whether to exclude or gate the most sensitive record types, and finalize the strengthened ToS §5(d).

## 4. 🟡 Vendor DPAs & sub-processor disclosure (audit M-2)

- [ ] Sign Data Processing Agreements with: Supabase, Railway, Vercel, Cloudflare, Upstash, Anthropic, Tracerfy,
      Regrid, Stripe, Resend.
- [ ] Publish a public sub-processor list (referenced from the privacy policy).
- [ ] If serving EU/UK customers: ensure SCCs / UK addendum are in place (audit M-1).

## 5. 🟡 Publish the policies

- [ ] Attorney-review `PRIVACY-POLICY-DRAFT.md` and `TERMS-OF-SERVICE-DRAFT.md`; fill all `[BRACKETS]`.
- [ ] Publish both at stable URLs and link them from signup, footer, and any app-store listing.
- [ ] Add the "Do Not Sell or Share My Personal Information" / deletion-request link (required for data brokers).
- [ ] Record click-through acceptance of the ToS at registration (timestamp + version).

## 6. 🟡 Operational compliance (audit M-3, M-5, L-3)

- [ ] Confirm the retention purge jobs actually run (`RECORD_RETENTION_DAYS`, `SKIP_TRACE_CACHE_DAYS`).
- [ ] Reduce personal data in logs: stop logging raw submitted email on `login_failure` (`auth.py:235`); consider
      truncating/hashing IPs; set log retention.
- [ ] Write a breach-notification runbook mapped to the sub-processors in the audit §3.
- [ ] Decide whether to withhold or visibly flag Do-Not-Call numbers in exports (audit H-2).

## 7. 🟢 App-store specifics (only if you ship a mobile/desktop app store build)

- [ ] Apple App Privacy "nutrition label" / Google Data Safety form — declare the data types in the audit §1–§2.
- [ ] Account-deletion path (Apple requires in-app account deletion).
- [ ] Note: BridgeLeads is currently a web SaaS (FastAPI + Next.js). If there is no native app-store submission,
      items here are N/A — but the privacy policy and data-broker registration still apply.

---

### Honest bottom line

The documents in this folder get you the *visible* legal basics (privacy policy, ToS). The items above — especially
**data-broker registration (1)** and **trademark clearance (2)** — are the ones that actually block launches and
trigger fines/cease-and-desists, and they require a lawyer and live registry searches that no document or code can
substitute for.
