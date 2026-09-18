# BridgeLeads — legal, privacy, accessibility and consumer-protection audit
## Phase 1: audit only. Nothing was deployed, changed, deleted or published.

Date: 2026-09-16
Scope: `bridgeleads.io` live production site + both repos
(`web-scrapper-automation` backend, `bridgeleads-web` frontend)
Jurisdiction baseline set by owner: **US, with California in scope**
App access decision: **marketing site audited live, dashboard from code**

**This is an engineering and product audit. It is not legal advice and is not a
substitute for counsel.** Nothing here establishes compliance with GDPR, CCPA/CPRA,
the ADA, CAN-SPAM, the TCPA or any other law. Where I write PASS it means only that
the evidence I reviewed did not reveal a gap for that checklist item.

---

# 1. EXECUTIVE SUMMARY

BridgeLeads is in better shape than most pre-launch products on the things that are
usually worst, and worse on a few specific things that are cheap to fix and carry
real exposure.

**Read section 1b first.** A compliance audit of this product already exists in the
repo, dated 2026-06-02, with a severity-tagged risk register and a pre-launch
checklist. It is solid work. Every checkbox in it is still unticked — except that
the two legal documents it told you to have a lawyer review were published anyway,
verbatim, with their `[BRACKET]` placeholders and their own "DRAFT — NOT LEGAL
ADVICE" warning intact. That single act converted three known internal gaps
(unsigned DPAs, no data-broker registration, unenforced retention) into **false
public statements**. That reframing is the most important output of this audit, and
it is why the placeholder finding below ranks so high.

**What is genuinely good, and verified:**

- **There is no tracking to consent to.** The live landing page makes **zero
  third-party network requests**. No analytics, no advertising pixels, no
  session replay, no chat widget, no iframes, no tracking beacons. Fonts are
  self-hosted at build time, so not even Google receives a visitor IP. The page
  sets **no cookies at all**. This removes an entire category of risk and it was
  a deliberate engineering choice, not an accident.
- **Security headers are strong**: CSP `default-src 'self'`, HSTS one year with
  `includeSubDomains`, `X-Frame-Options: DENY`, `nosniff`, referrer policy,
  permissions policy.
- **The Terms of Service are substantively serious.** Section 5 is a better
  acceptable-use clause than most competitors ship: TCPA and Do-Not-Call
  obligations, explicit FCRA non-use, data-broker law, and a
  distressed-owner/vulnerable-person section covering foreclosure-rescue
  statutes, elder financial exploitation and fair housing. Section 6 is a proper
  data-accuracy disclaimer. Someone thought hard about this.
- **The privacy policy does the hard thing correctly**: it separates account
  holders from "individuals whose information appears in the public-record lead
  data (you may be in this group even if you have never used BridgeLeads)". Most
  data-broker-shaped products hide that. It also names ten sub-processors and
  states the company acts as a data broker.
- **No fabricated social proof.** There are no testimonials, no star ratings, no
  customer counts and no borrowed logos anywhere on the live site. A
  `Testimonials.tsx` component exists in the repo but is dead code, not rendered.
- **Accessibility fundamentals on the landing page are good**: one `h1`,
  `lang="en"`, zero `<img>` elements needing alt text, all 18 inline SVGs
  correctly marked `aria-hidden` (no useless verbose descriptions), **zero**
  interactive elements missing an accessible name, no positive `tabindex`, and
  all 33 keyboard tab stops have a visible focus indicator.
- **The DNC *machinery* is honestly built** — `src/api/dialer_filters.py:9-47` has a
  TCPA-safe default and `map_dnc_status` (`dialer_connectors/base.py:67-70`)
  correctly reports "unknown" rather than "clear" for a null flag. But see the
  correction below: it operates on a column that is never populated, so this is
  good scaffolding with no data behind it, not a working control.
- **No hardcoded secrets, and no secret reaches the browser.** The only
  `NEXT_PUBLIC_*` variable in the frontend is a public API base URL. No token is
  stored in web storage. No error boundary renders a message or stack — all five
  render fixed copy plus an opaque `error.digest`, which is exactly the pattern
  CLAUDE.md requires.

**The five things I would fix before taking money from strangers:**

1. **P0 — every legal and privacy contact address is undeliverable, on a domain
   you appear not to own.** The policies tell people to email
   `privacy@bridgeleads.com`, `security@bridgeleads.com` and
   `legal@bridgeleads.com`. The site is `bridgeleads.**io**`. I verified via two
   independent resolvers: `bridgeleads.com` has **no MX record** and publishes
   `v=spf1 -all` on NameBright **parking** nameservers, i.e. it is a parked
   domain that declares it sends no mail. `bridgeleads.io` has **no MX record**
   either. So the designated channel for exercising privacy rights, for
   reporting a security vulnerability, and for legal notice all silently discard
   mail. With California in scope this is the worst finding in the audit, because
   the policy promises non-customers a deletion route that cannot receive their
   request.
2. **P0/P1 — nine unfilled template placeholders are live in production**, and
   they are in the clauses that matter most. Terms section 10 (Limitation of
   Liability) and section 11 (Indemnification) both name `[LEGAL ENTITY NAME]`.
   Section 13 Governing Law says `[state / country]`. And the published page
   contains a visible instruction to your own lawyer:
   "[Dispute resolution / arbitration / venue clause to be set by counsel.]"
   A liability cap and an indemnity that name no beneficiary, with no governing
   law and no venue, are the protections you would actually need if a customer
   misused lead data and someone sued.
3. **P1 — the privacy policy's retention promise is not performed.** Section 7 says
   "Lead records are retained for approximately 365 days, then deleted." The weekly
   purge deletes only `county_records` and `property_list_membership`. The
   `results` table — owner names, property and mailing addresses, and
   **skip-traced phone numbers and email addresses** — is deleted by **no scheduled
   job at all**, and `skip_trace_cache`'s "90 days" is a read-time reuse gate, not
   a deletion (the repo says so itself: "the 90-day TTL only fires on read").

   Two structural facts frame the fix. Against it: neither application role can
   DELETE from `results`, `users` or `skip_trace_cache` — `bridgeleads_app` has
   `REVOKE INSERT, UPDATE, DELETE ON results`
   (`scripts/_cutover_step2_grants_policies.py:45`) and `bridgeleads_system` is
   granted DELETE on exactly six tables, none of them these. So a purge added today
   would fail with `InsufficientPrivilege` until a grant changes. In favour of it:
   the **data model is already built for deletion** — `results.user_id` is
   `ondelete="CASCADE"` (`models.py:762`) and `models.py:514` states that "user_id
   keeps a direct users FK for user-delete" — and ops scripts already delete from
   `results` and `skip_trace_cache` when needed. So this is a missing scheduled job
   plus a deliberate privilege decision, not an architectural rebuild.
4. **P1 — annual billing does not disclose what it charges.** Toggling to
   "Annually" shows "$159/mo", "$399/mo", "$1,199/mo". No annual total appears
   anywhere and nothing says it bills as a single upfront payment. The 20%
   discount math is honest ($199→$159, $499→$399, $1499→$1199); the problem is
   that a buyer cannot see the amount that will hit their card. California's
   automatic-renewal rules are strict about exactly this.
5. **P1 — the site states its core coverage claim three different ways, and the
   nav goes invisible.** Hero: "19 WA COUNTIES DAILY". FAQ: "20+ Washington
   counties". Coverage page: "18 WASHINGTON COUNTIES, LIVE" (and it lists
   exactly 18). Separately, the fixed navigation bar becomes literally
   unreadable at several scroll positions (measured contrast **1.0**), and on
   any phone there is **no way to sign in at all**.

**A sixth item worth flagging (P1, security):** the backend bearer token is sealed
into the next-auth session and is **readable by client-side JavaScript** via
`getSession()`, for a **7-day** session, with **no refresh rotation** (the backend
exposes `POST /auth/refresh`; the frontend never calls it), under a CSP that
permits both `'unsafe-inline'` and `'unsafe-eval'`. Each piece is a defensible
choice; together they mean one XSS yields a live backend token for up to a week.

**Audit completeness, stated honestly.** I dispatched nine parallel sub-audits
(data model, third parties, billing internals, email, cookies/forms, legal pages,
marketing claims, deletion architecture, public records/Tracerfy). **Only one
delivered a report** — a frontend privacy and security sweep, whose central claim
I re-verified myself against the backend's own OpenAPI spec before relying on it.
Seven finished their runs without returning written output, and one was still
running. Everything else in this document I verified first-hand against the live
site or the deployed code. The sections marked **NOT YET ASSESSED** are real gaps
in this audit, not findings of "no problem" — most notably backend tenant
isolation, the email/unsubscribe inventory, the public-record source-terms review,
and whether personal data reaches Anthropic.

**The independent Codex cross-check is blocked.** I attempted it twice. The first
run consumed 72,669 tokens and hit the account's usage limit without returning a
verdict; the second returned zero bytes with the same limit error and the same
retry time (~6:36 PM). **No Codex finding is represented anywhere in this
report.** The prepared prompt covering all 12 finding areas is saved at
`scratchpad/codex-consult-prompt.txt` and can be re-run unchanged.

---

# 1a. CORRECTIONS TO EARLIER VERSIONS OF THIS REPORT

Sub-audits and the Codex cross-check landed after I circulated earlier drafts. Six
things I said were wrong, and I would rather flag them loudly than quietly restate
them.

**From the Codex cross-check (it earned its place — both are mine to own):**

| I said | Actually |
|---|---|
| "Anthropic receives page screenshots containing owner names, on a path that is ON by default" | **Not in production.** The only route to Anthropic (and to Regrid) is `enrich_parcel`, which **has no caller anywhere**. The live pipeline uses `batch_enrich_parcels_gis`. I found the call site and the default-on flag and stopped without checking whether the enclosing function was live. The capability is real and default-enabled, so it is a **latent** risk worth fixing, but no data flows today. See §4 claim 7 |
| "Lead and skip-trace data **cannot** be deleted" | **Overstated.** The retention finding stands — nothing deletes `results` on a schedule — but deletion is demonstrably possible: `scripts/` contains `DELETE FROM results` at three sites (`cleanup_watchdog_dup_results.py`, `cleanup_watchdog_billed_dups.py`, `repro_insert_wedge.py`) and `DELETE FROM skip_trace_cache` in `purge_skip_trace_cache.py`. I had grepped `src/` and never `scripts/`. **And the data model is deliberately built for user-delete**: `results.user_id` is `ondelete="CASCADE"` (`models.py:762`), `User.jobs`/`User.scraper_configs` carry `cascade="all, delete-orphan"` (`:262-263`), and `models.py:514` comments that "user_id keeps a direct users FK for user-delete". So deleting a `users` row *would* sweep that user's lead rows. What is missing is an endpoint and the privilege — **not** the cascade graph. That makes E5 materially less work than I implied |

**From the sub-audits:**

| I said | Actually |
|---|---|
| "DNC handling is better than the policy claims" | **Backwards.** The `phone_dnc_flag` column is **never populated** — the sole ingest writer hard-codes it to `None` (`tracerfy_ingest.py:537`) because Tracerfy supplies no DNC feed. The filter machinery is honestly built but has no data behind it, the live path opts into `include_unknown_dnc=True` to avoid returning zero rows, and DNC is not an exported column. So the policy claim is **false** and Terms §5(a) obliges customers to honour an indicator they never receive. Severity goes **up**, not down |
| "The coverage page is hardcoded and cannot self-correct" | **Wrong, and my reasoning was bad.** It server-fetches the live connector list (`coverage/page.tsx:91`). I inferred "hardcoded" from seeing no browser network request, forgetting it is a `force-dynamic` server component whose fetch never reaches devtools. **18 is the trustworthy number**; the hero's 19 and FAQ's 20+ are the hardcoded wrong ones |
| "~20 template counties are blocked by the SSRF allowlist" | **Retracted.** I relayed this from a sub-audit that has since retracted it itself. The allowlist is self-widening — every platform template calls `add_scrape_domain()` on its own `base_url` at construction (`templates/*.py`), Whatcom included (`whatcom_wa.py:61`), and `safe_get` defaults to `require_allowlisted=False` (`src/utils/safe_http.py:55,91,130`). The allowlist is an SSRF control, not a coverage gate. **No static read can tell you how many counties actually fetch today** |
| "Whatcom's host is not allowlisted" | **Wrong** — `whatcom_wa.py:61` allowlists it at module level |

The "scraped daily" claim still fails, but for a better reason than I first gave:
**`ENABLE_DAILY_SCRAPE` defaults to `False`** (`src/config/settings.py:309`) and the
daily task returns immediately without it (`scheduler_helpers/county.py:25-26`).

---

# 1b. THIS GROUND WAS ALREADY COVERED ONCE — AND THAT CHANGES THE FRAMING

**A prior compliance audit already exists in this repo**, dated 2026-06-02:
`docs/legal/DATA-INVENTORY-AND-COMPLIANCE-AUDIT.md` (205 lines, with a
severity-tagged risk register) plus `docs/legal/PRE-LAUNCH-LEGAL-CHECKLIST.md`.
It is good work and it anticipated most of the structural risk. **Every single
checkbox in that checklist is still unticked.**

Its register: **C-1** data-broker obligations not in place (registration +
ongoing deletion/suppression) · **C-2** predatory/distressed-owner targeting and
UDAP exposure · **H-1** no rights mechanism for the scraped data subjects ·
**H-2** TCPA/DNC exposure on skip-traced phones · **H-3** captcha circumvention
and portal-ToS scraping · **M-1** GDPR if any EU/UK customers · **M-2**
sub-processor DPAs and public disclosure missing · **M-3** personal data in
application logs · **M-4** FCRA misuse risk · **M-5** retention enforcement
unverified · **L-1** cookie/ePrivacy banner · **L-2** children/age · **L-3**
breach-notification readiness.

**What has actually been done since:** L-2 was actioned — the age and
business-purpose eligibility language is live. H-2 was partly actioned, and well:
`phone_dnc_flag` plus the TCPA-safe default in `src/api/dialer_filters.py`. And
checklist item 5 "Publish the policies" was *half* done.

**That half is the problem, and it is the finding I would put first.** Item 5
reads: "Attorney-review `PRIVACY-POLICY-DRAFT.md` and `TERMS-OF-SERVICE-DRAFT.md`;
fill all `[BRACKETS]`." It is **unticked**. The drafts carry their own warning:

> "⚠️ DRAFT — NOT LEGAL ADVICE. Must be reviewed by a licensed attorney before
> publication. Generated 2026-06-02 from a codebase audit. Replace every
> `[BRACKET]` with real values."
> — `docs/legal/PRIVACY-POLICY-DRAFT.md:3-6`

The Terms draft adds: "Liability caps and 'as is' disclaimers are only
enforceable to the extent your governing law allows; counsel must confirm"
(`TERMS-OF-SERVICE-DRAFT.md:3-8`). `components/legal/legal-shell.tsx:10-12` names
those drafts as the source of truth and says "Keep this page in sync when the
drafts are finalized by counsel."

They were never finalized. They were published verbatim, brackets included. The
drafts say "Generated 2026-06-02" and the live pages say "LAST UPDATED: JUNE 2,
2026" — **the machine-generated drafts went to production the same day, unreviewed.**

This matters beyond tidiness, because publishing converted three *internal gaps*
into *false public statements*:

| Prior finding, still open | What publishing the draft did |
|---|---|
| **M-2** — DPAs unsigned, checklist item 4 unticked | The live policy §6 now asserts ten sub-processors "each under a data-processing agreement" |
| **C-1** — data-broker registration not in place, checklist item 1 unticked | The live policy §5 now asserts "Where required, we register as a data broker" |
| **M-5** — retention enforcement unverified | The live policy §7 now asserts a 365-day deletion that the code does not perform for `results` |

So the exposure is no longer just "we haven't done X"; it is "we have publicly
stated we did X". That is the difference between an open task and a
misrepresentation, and it is why I rate the placeholder finding as high as I do.

**A fourth, undisclosed sub-processor.** The stack includes **2Captcha**
reCAPTCHA solving — `src/scrapers/enrichment/captcha.py` (including reCAPTCHA
**Enterprise** mode), gated by `CAPTCHA_ENABLED` and `CAPTCHA_API_KEY`
(`src/config/settings.py:272-273`). The prior audit flagged this as **H-3** and
the checklist says to "Read and comply with Tracerfy and 2Captcha contractual
terms". It is a live capability that receives data from the scraping pipeline, and
**it is not among the ten sub-processors the privacy policy discloses.** Whether
it is enabled in production is an environment question the owner must answer.
Programmatically defeating a CAPTCHA on a government portal is squarely a
counsel question, not an engineering one.

**One stale carry-over, flagged so nobody chases it.** The prior audit's M-3 cites
"stop logging raw submitted email on `login_failure` (`auth.py:235`)". That line
no longer contains logging code — `src/api/auth.py:235` is now the
`_CREDENTIALS_EXCEPTION` / `AuthContext` block. The concern may still be valid
elsewhere in the file, but the citation has expired and the current PII-in-logs
position needs re-checking rather than re-reporting.

**Where my audit adds genuinely new information:** the undeliverable contact
domain; the drafts-published-unreviewed finding above; FOUNDING25's
unredeemability; the undisclosed annual charge; the three-way county
contradiction; the invisible nav and absent mobile sign-in; the client-readable
bearer token combination; the unsubstantiated earnings and competitor claims; and
a positive confirmation that **L-1 can be closed** — I verified in production that
there are no tracking cookies or third-party requests at all, which the prior
audit could only assume for the backend and asked someone to confirm for the
frontend.

---

# 2. THE 20-ITEM COMPLIANCE MATRIX

PASS here means: the engineering and product evidence I reviewed did not reveal a
gap. It does **not** mean legally certified.

| # | Item | Status | One-line basis |
|---|------|--------|----------------|
| 1 | Privacy Policy | **PARTIAL / NEEDS LEGAL REVIEW** | Exists, well-structured, correctly separates the two data populations — but 4 live placeholders, a false retention claim, and an undeliverable contact address |
| 2 | Terms of Service | **PARTIAL / NEEDS LEGAL REVIEW** | Strong acceptable-use and accuracy sections; 5 live placeholders in the liability, indemnity and governing-law clauses; no cancellation or renewal terms at all |
| 3 | Refund / cancellation policy | **MISSING / NEEDS LEGAL REVIEW** | Only "Fees are non-refundable except where required by law" (Terms §3). No refund page, no cancellation clause, nothing linked from pricing or checkout |
| 4 | Cookie Policy | **PARTIAL** | No dedicated page, but verified there are **no non-essential cookies to disclose**, on the marketing site *or* in the app (the app's four storage keys are a banner flag, a dismissed-banner flag, a validated referral code and a sidebar preference). The privacy policy's cookie treatment is adequate for what actually exists |
| 5 | Cookie Consent | **PARTIAL (low severity)** | Banner is notice-only: "Accept" and the X do the same thing, there is no reject, and no consent state is consumed. Harmless *because nothing is tracked* — but the copy claims a purpose the site does not have, and the banner only renders on `/` |
| 6 | Form Consent | **NOT YET ASSESSED** | Sub-audit did not report; I did not verify the register form myself |
| 7 | Data Minimization | **NOT YET ASSESSED** | Sub-audit did not report. Partial signal only: retention is effectively unbounded for lead/contact data (item 20) |
| 8 | Third-Party SDK Audit | **PARTIAL** | Frontend verified clean: zero third-party runtime requests, no analytics package in `package.json`, only `NEXT_PUBLIC_API_URL` exposed. But **2Captcha is an undisclosed eleventh sub-processor** (`src/scrapers/enrichment/captcha.py`), and whether Anthropic and Regrid are real integrations is still unresolved. Backend vendor inventory not completed |
| 9 | Dark Pattern Audit | **PARTIAL** | Found: undisclosed annual total, "TALK TO SALES" linking to `/register`, unquantified "LIMITED SPOTS" urgency, and sign-in removed on mobile while the only CTA is "START FREE TRIAL". Cancel-flow not assessed |
| 10 | Hidden Fee Audit | **PARTIAL** | Overage rates ($0.08 / $0.08 / $0.05) *are* disclosed in the comparison table, though the Business card omits them. The real gap is the annual charge amount. Overage cap unverified |
| 11 | Testimonial / review audit | **PASS** | No testimonials, ratings, customer counts or third-party logos anywhere on the live site. `_sections/Testimonials.tsx` is dead code, never rendered |
| 12 | Unsupported marketing claims | **MISSING** | Three-way county contradiction; earnings-style claims ("one closed deal covers a year", "$5k–$30k"); a named competitor's price; "~30 seconds" onboarding; "seven portal templates" when there are 8 |
| 13 | Image alt text | **PASS** (landing) | Zero `<img>` elements; all 18 SVGs correctly `aria-hidden`; zero interactive elements without an accessible name. Dashboard not assessed |
| 14 | Colour contrast | **FAIL** | P1: fixed nav measures **1.0** (invisible) across ~2,300px of scroll and again at two later positions. Plus 4 further failures at 4.06 and 4.35 against a 4.5 requirement. Dark mode not assessed |
| 15 | Keyboard navigation | **PASS** (landing) | 33 tab stops, every one reachable and every one with a visible focus indicator; no positive `tabindex`; FAQ uses real buttons with `aria-expanded`/`aria-controls`. Dashboard not assessed |
| 16 | Business details | **MISSING** | No legal entity name, no postal address, no phone, and **no support contact of any kind** on the marketing site. Footer is `© 2026 BRIDGELEADS` plus Privacy and Terms |
| 17 | Age / children policy | **PASS / NEEDS LEGAL REVIEW** | An eligibility provision already exists: Terms require "at least 18 and using the Service for business purposes"; privacy states the service "is not directed to consumers or to anyone under 18". **No parental-consent infrastructure is needed** — do not build any |
| 18 | Email unsubscribe | **NOT YET ASSESSED** | Sub-audit did not report. Noted for follow-up: `src/workers/onboarding_emails.py` sends day-1, day-3 and trial-expiry nudges, which is the category most likely to need an unsubscribe |
| 19 | Font / image / asset licensing | **PARTIAL** | Six families via `next/font/google` (DM Sans, Noto Serif, Geist, Inter, JetBrains Mono, Raleway) — all self-hosted at build, none verified individually. Low image exposure: the landing page ships no raster images. Full inventory not completed |
| 20 | Data deletion requests | **MISSING / NEEDS LEGAL REVIEW** | **Confirmed against the backend's own OpenAPI spec**: 69 operations, only 2 `delete` ops (cancel job, delete scraper), no account-deletion, no subject-access export, and no owner suppression list. Lead and skip-trace contact data is never purged. The policy promises non-customers a deletion route that has neither a working mailbox nor a capability behind it |

---

# 3. COOKIE / TRACKING INVENTORY (verified against production)

Method: loaded `https://bridgeleads.io/`, enumerated `performance.getEntriesByType('resource')`,
compared every hostname against the page origin, and inspected response headers,
`document.cookie`, `localStorage`, `sessionStorage`, `<script>`, `<iframe>` and
1×1 images.

| Finding | Result |
|---|---|
| Third-party hosts contacted | **0** (26 resources, all first-party) |
| Cookies set on the landing page | **none** — no `Set-Cookie` header, `document.cookie` empty |
| `localStorage` on load | **empty** |
| `sessionStorage` on load | **empty** |
| `bl_cookie_ack` | written **only** on banner dismissal — the banner's own dismissal is the sole storage it causes |
| Analytics / advertising / session replay | **absent** (searched for GA/gtag, GTM, Meta pixel, Hotjar, Clarity, PostHog, Mixpanel, Segment, Amplitude, Plausible, Fathom, Vercel Analytics, Intercom, Crisp, Drift) |
| Tracking pixels / beacons | **none** (no 1×1 images) |
| Iframes | **none** |
| Google Fonts at runtime | **no** — `next/font/google` self-hosts at build time, so no visitor IP reaches Google. CSP permits `fonts.gstatic.com` but nothing uses it |
| Security headers | CSP `default-src 'self'`; HSTS `max-age=31536000; includeSubDomains`; `X-Frame-Options: DENY`; `nosniff`; `strict-origin-when-cross-again`; `permissions-policy: camera=(), microphone=(), geolocation=()` |
| CSP weakness | `script-src` includes `'unsafe-inline'` and `'unsafe-eval'` (common for Next.js, still worth tightening) — P3 |

**Consequence for items 4 and 5.** The cookie banner is not deceptive — there is
nothing being tracked behind a fake "Accept". But its copy, "We use cookies to
improve your experience", describes behaviour the site does not have, and its
"Accept" button implies a choice that has no effect. The accurate options are
either to remove the banner, or to reword it to describe the single
strictly-necessary authentication cookie. The authenticated app's cookies (the
next-auth session, CSRF and callback cookies and their flags) were **not
assessed** — that was the sub-audit that did not report.

---

# 4. PRIVACY POLICY GAP ANALYSIS

Live at `/privacy`, "LAST UPDATED: JUNE 2, 2026", 759 words.

**Placeholders live in production (4):**

| Placeholder | Where |
|---|---|
| `[Legal entity name]` | opening paragraph, the sentence identifying who operates the service |
| `[retention period]` | §7 — "Security logs are retained for [retention period]." |
| `[country]` | §10 — "We are based in [country]" |
| `[legal entity name], [mailing address]` | §11 Changes & Contact |

**Claims checked against code:**

| # | Claim (§) | Verdict | Evidence |
|---|---|---|---|
| 1 | "Lead records are retained for approximately 365 days, then deleted" (§7) | **FALSE for lead/contact data** | `RECORD_RETENTION_DAYS = 365` (`src/config/settings.py:310`) is real, but `_purge_old_records_impl` (`src/workers/scheduler_helpers/county.py:64-86`) deletes only `county_records` and `property_list_membership`. `results` is never deleted |
| 2 | "Skip-trace results are cached for up to 90 days" (§7) | **MISLEADING — confirmed by the repo's own words** | `SKIP_TRACE_CACHE_DAYS = 90` (`settings.py:289`) is a *reuse* TTL at `src/workers/tasks_helpers/enrich.py:189`. `scripts/purge_skip_trace_cache.py` states it outright: *"the 90-day TTL only fires on read."* Stale rows are never reused but are never deleted, and neither application role even holds DELETE on `skip_trace_cache`. A reader would infer the data is gone at 90 days; it is not |
| 3 | "We do not use third-party advertising, analytics, or session-replay trackers" (§2) | **ACCURATE** | Independently confirmed: zero third-party requests in production. Note the hedge "in our application backend" is narrower than the frontend evidence supports — you can state this more strongly than you currently do |
| 4 | Sub-processors "each under a data-processing agreement" (§6) | **LIKELY FALSE + INCOMPLETE** | Ten named: Supabase, Railway, Vercel, Cloudflare R2, Upstash, Anthropic, Tracerfy, Regrid, Stripe, Resend. The repo's own `PRE-LAUNCH-LEGAL-CHECKLIST.md` item 4 — "Sign Data Processing Agreements with" exactly these vendors — is **unticked**, so the assertion was almost certainly untrue when published (owner must confirm). Separately the list is **incomplete**: **2Captcha** receives pipeline data (`src/scrapers/enrichment/captcha.py`) and is not disclosed |
| 5 | "We act as a data broker in certain U.S. states. Where required, we register as a data broker" (§5) | **LIKELY FALSE — highest legal priority** | The repo's own checklist item 1 is a 🔴 block titled "Data-broker registration — DO BEFORE SELLING", naming the California Delete Act (SB 362, CPPA, annual, **deadline Jan 31**), Vermont 9 V.S.A. §2446, Oregon and Texas SB 2105 — **all unticked**, including "Engage a privacy attorney to confirm data-broker status (it is very likely 'yes')". So the published sentence asserts something the team's own checklist says has not been done |
| 6 | Skip tracing returns "a phone number, phone type, a Do-Not-Call indicator" (§3) | **FALSE — and this is the correction I most want you to read** | **No DNC indicator is ever produced.** The only ingest writer hard-codes `phone_dnc_flag=None` (`src/workers/tracerfy_ingest.py:537`); `enrich.py:204,1217` only copy that null forward from cache. The code says so in three places: "Tracerfy returns no DNC" (`scheduler_helpers/dialer.py:176`), "DNC feed (phone_dnc_flag is unknown for most leads)" (`dialer_connectors/phoneburner.py:11`), and "include_unknown_dnc=True because skip-trace leaves phone_dnc_flag NULL" (`src/api/routes/jobs.py:413`). DNC is **not** among the exported CSV columns (`src/utils/lead_export.py` — zero `dnc` hits). **The live path deliberately bypasses the TCPA-safe default**, because with an always-null column the safe filter would return zero rows. So the policy claims a protection that does not exist, and **Terms §5(a) obliges the customer to "honor any Do-Not-Call indicator"** they are never given. This is prior-audit **H-2**, still fully open |
| 7 | "Anthropic: AI-assisted data extraction" and "Regrid: property data enrichment (optional)" (§6) | **OVER-DISCLOSED — both are unreachable in production.** A latent risk, not a live transfer | **Resolved, and this reverses my own earlier answer — Codex caught it.** Both vendors are reached only through `enrich_parcel` in `src/scrapers/enrichment/parcel.py`: Regrid at `:61-63`, Anthropic at `:73-75`. **`enrich_parcel` has no caller anywhere** in `src/` or `scripts/` — it is exported from `enrichment/__init__.py` and never invoked. The live enrichment pipeline uses `batch_enrich_parcels_gis` instead (`src/workers/tasks_helpers/enrich.py:266,283`), which is ArcGIS/county GIS and involves neither vendor. The worker's only reference to the AI module is importing the `_KNOWN_ASSESSOR_URLS` constant (`enrich.py:385`), not the AI call. So **no page data currently reaches Anthropic and no parcel data reaches Regrid.** I had this wrong: I found the call site and the default-on flag and stopped, without checking whether the enclosing function was live. Codex's objection — "their payloads expose identifiable page content when invoked, but that alone does not establish a live Anthropic disclosure" — was correct.

**The latent risk is still worth recording, because the capability is fully built and default-enabled.** If anyone wires `enrich_parcel` into the pipeline, `ai_assessor.py:104,149` take **full-page PNG screenshots of county assessor pages** and pass them as `images=[screenshot]` to `ask_claude` (`src/scrapers/ai/client.py:33-38`, model `claude-sonnet-4-6`). The prompt is parcel-number based, but an assessor page renders owner name and mailing address, so those would travel inside the image — and the gate is already open: `AI_ENRICHMENT_ENABLED: bool = True` (`settings.py:282`), versus `AI_SCRAPER_ENABLED: bool = False`. Four words of policy disclosure would not cover that. This is the same "built but never wired" pattern the repo has hit before |
| 8 | Record types listed (§3) | **WRONG IN BOTH DIRECTIONS** | It claims `eviction`, which is not in `ALL_RECORD_TYPES` and has no connector; and it **omits `death_certificate` and `trustee_sale`, both live and plan-gated** (`src/config/constants.py:175-195`). Under-disclosing two live categories — one of which is death records — is the more serious half |
| 11 | "How information is shared … only with §6 sub-processors and the customer" (§5) | **INCOMPLETE — an undisclosed third path** | Lead rows including skip-traced phones are pushed **outbound to PhoneBurner** (`src/workers/dialer_connectors/phoneburner.py:29,55`) and to **any customer-supplied webhook URL** (`generic_webhook.py`, `webhook_delivery.py`). Neither PhoneBurner nor 2Captcha appears in the §6 sub-processor list |
| 12 | Purposes of processing (§4) | **UNDERSTATED** | Omits that the product *derives* new personal attributes — `absentee_owner`, `out_of_state_owner`, `owner_state` (`src/db/models.py:836-838`), `contactability_score`, `wa_foreclosure_eligible`, `lead_subtype` (`src/utils/lead_export.py:52-71`) — and omits **LLM-based extraction** (`src/scrapers/ai/client.py:11-29`), which confirms Anthropic is a real integration and not merely a disclosed-but-unused vendor |
| 13 | "a phone number … and/or an email address" (§3) | **UNDERSTATED** | Up to **three phones and three emails** per owner (`models.py:800-801`), plus the **full Tracerfy `raw_response` payload retained** (encrypted) at `models.py:1102-1103`. Retaining the entire vendor response is a larger footprint than the policy describes |
| 9 | Security: "encryption in transit, hashed credentials, access controls, rate-limiting, and secret redaction in logs" (§9) | **UNDER-CLAIMED (good)** | It notably does *not* claim encryption at rest, which is the right instinct. Application-layer field encryption appears to exist in this codebase but was not verified in this pass |
| 10 | Rights apply to "people in our lead records who are not customers" (§8) | **POLICY IS CORRECT, MECHANISM IS BROKEN** | This is the right commitment and it is the one most undermined by the dead mailbox and the absent deletion capability |

**Missing topics given what the product actually does:** no security-incident /
breach-notification statement; no description of how a non-customer would be
*verified* before a deletion request is honoured (§8 promises verification but
describes no method); no stated legal basis or opt-out mechanism for the skip
tracing of people who never interacted with the company; no retention period for
skip-traced contact data distinct from lead records.

---

# 5. TERMS GAP ANALYSIS

Live at `/terms`, same date, 817 words, 14 sections.

**Placeholders live in production (5), concentrated in the load-bearing clauses:**

| Placeholder | Section | Why it matters |
|---|---|---|
| `[LEGAL ENTITY NAME]` | **§10 Limitation of Liability** | The liability cap protects an unnamed party |
| `[legal entity name]` | **§11 Indemnification** | The indemnity runs to an unnamed party |
| `[state / country]` | **§13 Governing Law** | No governing law is chosen |
| `[Dispute resolution / arbitration / venue clause to be set by counsel.]` | **§13** | A visible note to your lawyer is published to customers. It also signals the document was never reviewed |
| `[legal entity name], [mailing address]` | §14 Contact | No entity, no address |

**Present and genuinely strong:** §1 service description with a right to change
sources and counties; §2 account and credential responsibility; §4 customer data
licence and internal-business-use restriction; **§5 acceptable use** (TCPA and
DNC registries and calling-time rules, honour any DNC indicator, obtain consent
before calling or texting; FCRA non-use for credit/insurance/employment/tenant
screening with an explicit "not a consumer reporting agency"; CCPA/CPRA and
data-broker compliance; distressed-owner and vulnerable-person protection
covering foreclosure-rescue statutes, elder financial exploitation and fair
housing; no deceptive or high-pressure outreach; no resale or redistribution;
no re-scraping); **§6 data-accuracy disclaimer** including the important line
that record types "describe source records, not any conclusion about an
individual"; §7 IP; §8 third-party services; §9 warranty disclaimer; §10
liability cap at 12 months of fees; §11 indemnity; §12 termination with survival;
§14 change process. Age 18 and business-purpose eligibility is stated up front.

**Absent:**

| Topic | Status |
|---|---|
| Cancellation | **The word "cancel" does not appear anywhere in the Terms.** For an auto-renewing subscription this is the most significant omission |
| Renewal / auto-renewal | **Not mentioned.** No advance-notice commitment before an annual charge |
| Refunds | Only "Fees are non-refundable except where required by law" (§3) |
| What happens to delivered leads and exports after termination | §12 says access ends; silent on data already delivered, or an export window |
| Trial terms | Referenced only indirectly; the 7-day no-card trial is described on the pricing page, not in the Terms |
| Service availability / SLA | Absent (defensible at this stage; the warranty disclaimer covers it) |
| How pricing changes are notified | §3 says "We may change pricing on notice" without defining notice |

Note that §3 says plans and skip-trace pricing "are as described at signup",
which makes the pricing page **legally load-bearing**. Every inaccuracy in
section 7 below is therefore also a Terms problem.

---

# 6. BILLING AND HIDDEN FEE AUDIT

Verified by driving the live pricing page, including toggling the billing period.

**Plans as published (monthly → annual):**

| Plan | Monthly | Annual (shown as /mo) | Records | Counties | Skip-traces incl. | Overage |
|---|---|---|---|---|---|---|
| Starter | Free | Free | 50 | 1 | none | not available |
| Pro | $199 | $159 | 1,000 | 3 | 250 | $0.08 |
| Business | $499 | $399 | 5,000 | 10 | 1,000 | $0.08 |
| Agency | $1,499 | $1,199 | unlimited | unlimited | 2,000 | $0.05 |

**The 20% discount claim is arithmetically honest** — 159/199, 399/499 and
1199/1499 are each 20.0%. Credit where due; this is the kind of claim that is
usually wrong.

**Findings:**

1. **P1 — the annual charge is never disclosed.** The annual view shows only a
   per-month figure. No annual total ($1,908 for Pro at 12 × $159), and no
   statement that it is billed once upfront. The FAQ repeats "Pay annually and
   save roughly 20%" without a total either. What Stripe actually charges was
   **not verified** (that sub-audit did not report), so the direction of the
   problem is certain but the exact charge is not.
2. **P1 — FOUNDING25 is very likely not redeemable by anyone.** I initially
   suspected this promo was scoped to a single customer; **that was wrong** and I
   am correcting it. FOUNDING25 is designed as an open, all-customers,
   all-products coupon: 25% off, `duration="forever"`, `max_redemptions=25`, with
   no `customer=` and no `applies_to` restriction
   (`scripts/stripe_pricing_migration_2026_06.py:43-45,122-128`). The
   single-customer promo is a *separate* offer on an unmerged branch that never
   deployed.

   The actual defect is worse. Checkout opens with `allow_promotion_codes=True`
   (`src/api/routes/billing.py:607`), which accepts only a Stripe **Promotion
   Code** object — not a raw coupon id. Nothing at HEAD creates one:
   `PromotionCode` appears nowhere in `src/` (I verified; the only FOUNDING25
   references are the coupon id and cache key at `billing.py:293-313`). A fix
   script exists on the unmerged branch `chore/stripe-followups`, and its own
   docstring states the live finding: *"The FOUNDING25 coupon exists … and
   /billing/plans advertises it, but no Promotion Code was ever created on it.
   Checkout only accepts promotion CODES, so nobody could redeem it."* Whether it
   was ever run against the live key is not determinable from here.
   **Owner must check Stripe** for an active Promotion Code with
   `code = "FOUNDING25"` on that coupon.

   Independently of that: `max_redemptions=25` means the offer dies after 25
   uses, and the "LIMITED SPOTS" banner is **hardcoded**
   (`app/(marketing)/pricing/page.tsx:67-69`, `_monopo/Pricing.tsx:47-49`) while
   the backend already computes `spots_remaining`
   (`src/api/routes/billing.py:299-358`) — the frontend deliberately dropped the
   counter (`_monopo/pricingApi.ts:13,20`). So the banner will keep advertising a
   dead code, and the honest number it needs is already available.
3. **P2 — overage disclosure is inconsistent.** The comparison table discloses
   $0.08/$0.08/$0.05 properly, but the Business plan *card* omits its overage
   rate while the Pro card includes it. Whether metered overage is **capped** is
   **unverified** — if uncapped, a customer can run up unbounded charges, which
   should be disclosed before purchase.
4. **P2 — "TALK TO SALES"** on the $1,499 Agency plan links to `/register`, not
   to any sales contact. There is no way to talk to sales anywhere on the site.
5. **P2 — "Every plan reaches all supported counties"** in the pricing intro
   contradicts the same page's 1 / 3 / 10 / unlimited county caps. The FAQ
   clarifies it means "a count, not a fixed list", but the headline sentence as
   written is false.
6. **P3 — "White-label (coming soon)"** is listed as an Agency feature. Labelled,
   so disclosed, but it is an unbuilt feature on a paid tier's feature list.
7. **P3 — "Starter is free forever"** is a durable promise worth making
   deliberately rather than accidentally.
8. **No refund or cancellation information appears on the pricing page, in the
   footer, or at any point before purchase.** With California in scope, the
   automatic-renewal rules expect the recurring nature, the total charge and the
   cancellation method to be clear and conspicuous before the customer is billed.

---

# 7. MARKETING CLAIM AUDIT

| Claim | Where | Verdict | Recommendation |
|---|---|---|---|
| "19 WA COUNTIES DAILY" | landing hero | **Contradicted** | The coverage page says 18 and lists 18. Derive all three from `county_connectors` |
| "20+ Washington counties" | landing FAQ | **Contradicted, worst of the three** | Overstates by at least 3 and the "+" implies more |
| "18 WASHINGTON COUNTIES, LIVE" | coverage page | **ACCURATE — and this is the one to trust** | **Correction to my earlier reading.** The page is *not* hardcoded: it server-fetches the live connector list and renders `` `${counties.length} Washington counties, live.` `` with `` `${totalTypes.size} record types` `` (`coverage/page.tsx:91,105-107`). I wrongly inferred "hardcoded" from seeing no browser network request, but it is a `force-dynamic` **server component**, so the fetch never appears in devtools. It also degrades gracefully ("Coverage temporarily unavailable." / "Coverage is expanding."). So 18 and 7 are DB truth at render time; the hero's 19 and the FAQ's 20+ are the hardcoded wrong ones, and the fix is to source them from this same endpoint |
| "scrapes six types of county records" | landing | **Wrong** | `ALL_RECORD_TYPES` has **7**; the coverage page says 7 |
| "Seven portal templates" | landing | **Wrong** | There are **8** files in `src/scrapers/templates/` (acclaimweb, ava_fidlar, eagleweb, idocmarket, landmarkweb, laserfiche_weblink, skagit_recording, tyler_selfservice) |
| "configure most new US county sources in ~30 seconds" | landing + coverage | **Overstated** | True only for a county whose portal matches one of the 8 templates; `_detect_template` fails closed otherwise, and CLAUDE.md's own process for a new county is a 5-step engineering task. Qualify to "counties on supported recorder platforms" |
| "Request a county and start getting leads tomorrow" | coverage | **Overstated** | A concrete delivery promise that depends on template match |
| "Every county below is scraped daily" | coverage:105 | **OVERSTATED — two solid grounds, and one I have to withdraw** | (a) ~~The daily job is flag-gated off by default~~ — **withdrawn as evidence.** `_scrape_county_daily_impl` does return immediately `if not settings.ENABLE_DAILY_SCRAPE` (`scheduler_helpers/county.py:25-26`) with a `False` default (`settings.py:309`), but **a `settings.py` default is not the production value** — the env var almost certainly sets it. This project has a standing landmine for exactly this error, in which reading staged flag defaults as prod state produced a false P0 twice. It remains a one-line thing for the owner to confirm on the Railway worker, but it is **not** evidence the claim is false. (b) Per-customer scrapes run on each config's own frequency (manual/daily/weekly/monthly), so a county with no active daily config is not scraped daily. (c) `health_status` is a weak proxy for "works": the canary probes only a 1-day window so low-volume rural counties flip to `degraded` on empty days, and migration `081:41-44` seeds trustee_sale rows `health_status='healthy'` **without any probe** so they appear in the picker immediately. (b) and (c) stand on their own; the honest verdict is "overstated", not "false" |
| "One closed deal covers a year" / "One closed wholesale deal is worth $5k–$30k" / "pays for itself on a single contract" | pricing | **Unsubstantiated earnings claims** | Highest-risk claim class for a product sold to investors. Remove, or qualify with a basis and a no-guarantee disclaimer |
| "below PropStream's $0.12" | pricing FAQ | **Unsubstantiated competitor claim** | Comparative pricing needs a dated source; competitor prices change |
| "Stale data and wrong numbers are the #1 complaint about every cheaper tool" | pricing FAQ | **Unsubstantiated superlative** | Remove or cite |
| "THE FRESHEST WA DISTRESS-LEAD ENGINE" | pricing | **Unsubstantiated superlative** | Puffery, but soften |
| "277K+ RECORDS SCRAPED" / "129K+ RECORDS ENRICHED" | landing stats, `_monopo/data.ts:9,11` | **Hardcoded design-mockup placeholders — REMOVE or verify** | The file admits it: `data.ts:6-7` comments *"277K+/129K+ carried from the design — still worth verifying against prod."* Their origin is `docs/landing-page-prompt.md:38,78` — a **creative brief**, which also says "22 WA counties", a **fourth** county figure. These are numbers from a design comp published as metrics |
| "~15m DELIVERY SPEED" / "Contact data arrives in ~15 minutes" | `data.ts:12,26` | **Contradicted by the architecture** | Skip-trace is asynchronous: beat runs every 5 min and submits at most 2 batches per tick because *"Tracerfy rate-limits batch POSTs to 10 per 5 min"* (`src/workers/scheduler.py:142-149`), results return later by webhook, and `dialer-push-sweep` waits for settlement. A fixed ~15m is not achievable under load. Replace with a measured p50/p95 or drop it |
| "62% leads enriched" | rendered landing page, inside the block labelled **"sample preview"** | **Labelled as a sample — low severity, but confirm** | No `62%` literal exists in `app/(marketing)/`; `"leads enriched"` lives only at `app/(dashboard)/dashboard/page.tsx:199`, so the figure is computed or comes from a shared dashboard component rather than marketing copy. Separately, the public `GET /scrapers/sample` payload carries a **hardcoded `enrichment_rate: "95%+"`** (`scheduler_helpers/public_cache.py:96-102`, duplicated at `src/api/routes/scrapers.py:80`) sitting among stats that *are* DB-computed — compute it or delete it |
| Sample dashboard ("Snohomish 312", "King tax delinquent 7,913") | landing | **Appropriately labelled** | Marked "sample preview" and "SAMPLE STATUS"; shows aggregates, no personal names. `refresh_public_sample_cache` states all PII redaction happens server-side before caching — good design, redaction correctness not re-verified |

**Testimonial audit result: clean.** No testimonial, rating, review, customer
count or third-party logo appears anywhere on the live site. The pull quote
("We don't just deliver data. We deliver the hours you used to spend digging
through county portals.") is unattributed brand copy, not a customer statement,
which is the correct way to do that. `app/(marketing)/_sections/Testimonials.tsx`
exists but `page.tsx` renders `_monopo/*` instead, so it is dead code. Worth
deleting so it can never be wired up by accident.

---

# 8. ACCESSIBILITY AUDIT

Measured on the live site at 1440, 768 and 320 px. Colours were resolved through
a canvas round-trip rather than parsed from strings, because computed values come
back as `lab()` in this codebase and regex-parsing them produces nonsense.

**P1 — the fixed navigation bar becomes invisible at multiple scroll depths.**
The nav switches from white to black text somewhere between scrollY 3200 and
4200, but the page's alternating light and dark bands do not align with that
threshold. I measured the wordmark against what is actually painted behind it, by
hiding the nav and sampling the element underneath, at 13 scroll positions:

| scrollY | Nav text | Painted behind | Contrast |
|---|---|---|---|
| 0, 400 | white | `#07211f` dark | 16.85 ✅ |
| **900, 1600, 2400, 3200** | **white** | **`#ffffff`** | **1.0 — invisible** |
| 4200, 5200 | black | white | 21 ✅ |
| **6400** | **black** | **`#000000`** | **1.0 — invisible** |
| 7600, 9000, 10500 | black | white | 21 ✅ |
| **11800** | **black** | `#07211f` dark | **1.25 — effectively invisible** |

That is roughly 2,300px of continuous scroll where the entire nav — wordmark,
OVERVIEW, FEATURES, COVERAGE, PRICING, SIGN IN and the primary CTA — is white on
white, plus two further dead zones. This is WCAG 1.4.3 and it is also just a
visible bug.

**P1 — there is no way to sign in on a phone.** `Sign in` carries
`hidden md:block` (`_monopo/Nav.tsx:46`), so it is absent below 768px. The nav
links carry `hidden lg:flex` (line 35), absent below 1024px. **There is no
hamburger menu anywhere in `Nav.tsx`.** I confirmed at 320px that the DOM
contains **zero** links to `/login`, including in the footer. An existing
customer on a phone must type the URL by hand, while the only visible call to
action is "START FREE TRIAL". (My first automated check reported a hamburger;
that was a false positive from the FAQ accordions' `aria-expanded` — the manual
enumeration is correct.)

**P2 — four further measured contrast failures** (of 57 unique
colour/size/background combinations):

| Sample | Size | Foreground | Background | Ratio | Needs |
|---|---|---|---|---|---|
| "Integrations" | 12px | `#6d6d6d` | `#000000` | **4.06** | 4.5 |
| "Export clean files or push leads…" | 14px | `#6d6d6d` | `#000000` | **4.06** | 4.5 |
| "+ New Run" | 12px | `#000000` | `#007f80` | **4.35** | 4.5 |
| "completed runs" | 11px | `#007f80` | `#000000` | **4.35** | 4.5 |

**P2 — tap targets.** At 320px, 8 controls are 16px tall (the footer links and
the wordmark), below the WCAG 2.2 SC 2.5.8 minimum of 24×24.

**P3 —** no `<main>` landmark and no skip link on the landing page (the legal
pages do have `<main>`); the hero `h1` is duplicated verbatim as an `h2` in the
next section; body text drops to 10–11px in the footer and cookie banner;
`/pricing` and `/coverage` both reuse the generic site title.

**What is good, and measured:** no horizontal overflow at 320px (document
scrollWidth 310 < 320); every one of 33 tab stops reachable with a visible focus
indicator; no positive `tabindex`; tab order follows visual order; FAQ items are
real `<button>`s with `aria-expanded` and `aria-controls`; the cookie banner is
keyboard-reachable, correctly uses `inert` and `aria-hidden` when hidden, and its
close button has an `aria-label`; all 18 SVGs are `aria-hidden`; zero `<img>`
elements means no alt-text debt at all on the landing page.

**Not assessed:** the authenticated dashboard (tables, dialogs, dropdowns, row
expansion, pagination, notifications), dark mode, and the 375/390/430/1024
viewports. Note for whoever does the dark-mode pass: forcing `dark` by adding the
class is unreliable in this app because the theme provider re-syncs and strips
it, silently producing impossible ratios. Set `localStorage.theme = "dark"`,
reload, then assert the class is present before measuring.

---

# 9. DATA DELETION ARCHITECTURE

The two populations must not be conflated, and the architecture makes the
distinction sharp:

- **Category A — customer/account data**: the paying investor's email, password
  hash, API key hash, Stripe customer reference, usage counters, scraper configs,
  schedules, deliveries.
- **Category B — public-record and skip-traced data about third parties** who
  never signed up: owner names, probate heirs, property and mailing addresses,
  parcel identifiers, and skip-traced phone numbers and email addresses.

**What I established:**

- The complete set of tables any `DELETE FROM` in `src/` ever targets:
  `county_records`, `property_list_membership`, `pending_registrations`, and
  `delivered_records` (6 call sites, dedup-claim release, not retention).
- The weekly purge (`_purge_old_records_impl`,
  `src/workers/scheduler_helpers/county.py:64-86`, Sundays 03:00 UTC) deletes
  `county_records` older than 365 days and `property_list_membership` by
  `last_seen_at`.
- **`results` is never deleted.** That is where the Category B personal data that
  customers actually receive lives, including skip-traced contact details.
- **No customer account-deletion endpoint exists — confirmed against the backend's
  own `schema/openapi.json`** (the authoritative spec, not the frontend's generated
  copy). The API has **69 operations** and exactly **two** `delete` operations:
  `cancel_job_jobs__job_id__delete` and `delete_scraper_scrapers__scraper_id__delete`.
  Neither touches an account. The complete `/auth/*` surface is api-key,
  change-password, config, forgot-password, login, login/break-glass, login/mfa,
  logout, logout-all, me, mfa/{disable,enable,setup,status},
  notification-preferences, onboarding, profile, refresh, register, reset-password,
  verify-email. There is no delete, close, deactivate or anonymize path.
- **No data-access / portability path** for a customer's own account data. The only
  export operations are product features over lead data
  (`download_export_jobs…`, `get_export_url_jobs…`, segments union/intersection),
  not a subject-access export.
- **The database privileges make the published retention promise unimplementable,
  not merely unimplemented.** From the role provisioning in
  `scripts/_cutover_step2_grants_policies.py`:
  - `bridgeleads_app` (the FastAPI role): `REVOKE DELETE ON users, scraper_configs,
    jobs, user_record_views` (line 43) and `REVOKE INSERT, UPDATE, DELETE ON
    results, job_logs, county_records, referral_events, …` (line 45). Its only
    DELETE grants are `mfa_backup_codes` (35) and `pending_registrations` (42).
  - `bridgeleads_system` (the Celery role) holds DELETE on **exactly six tables**:
    `county_records` (57), `property_list_membership` (58), `delivered_records`
    (69), `mfa_backup_codes` + `mfa_break_glass_codes` (71), `pending_registrations` (73).
  - **Neither application role can DELETE from `users`, `results` or
    `skip_trace_cache`.** A purge of lead or contact data would fail with
    `InsufficientPrivilege`; it requires the owner/DDL role
    (`DATABASE_URL_MIGRATE`) or the Supabase console.

  This is a clean cross-confirmation from two directions: I independently found
  `DELETE FROM` in code for exactly four tables, and those are four of the six the
  worker is granted. Code and grants agree. So the policy's 365-day deletion
  promise is not a missing cron job — it is contrary to the database's deliberate
  posture. Whether that posture is right is a decision for you; publishing a
  deletion promise against it is not.

- **There is not even a deactivation path.** `User.is_active` exists
  (`src/db/models.py:228`) and is *read* as an auth filter in eight places, but it
  is **never written `False`** anywhere in the codebase.

- **`scripts/purge_skip_trace_cache.py` is not a retention job** — it is a
  one-time 2026-06-10 migration cleanup for the per-tenant cache-key cutover, and
  its own docstring states the point directly: *"the 90-day TTL only fires on
  read."* Stale cache rows are simply never reused; they are not deleted. That is
  the repo confirming my reading of the §7 "cached for up to 90 days" claim.

- **`audit_events` is append-only and its IP data is unpurgeable.**
  `REVOKE SELECT, UPDATE, DELETE ON audit_events FROM bridgeleads_app` (line 51)
  makes it insert-only to the API role, which is good tamper-evidence design — but
  request IPs accumulate there with no purge job and no grant to remove them.
  The policy's §7 retention line for security logs is the one reading
  `[retention period]`.

- **No owner-initiated suppression or do-not-contact list.** Searching `src/` for
  `do_not_contact`, `suppress`, `opt_out`, `blacklist` and similar returns only the
  Tracerfy-sourced `phone_dnc_flag` and the token blacklist. `phone_dnc_flag` is a
  vendor DNC signal, **not** a mechanism by which a property owner who contacts
  BridgeLeads can be excluded from future lists. So the privacy policy's promise to
  non-customers has neither a working mailbox nor a capability behind it.
- Side finding (P3): the frontend's `lib/api-types.generated.ts` is **stale** versus
  the backend spec — it omits `/auth/profile` and `/auth/verify-email`. The repo has
  a CI drift gate for exactly this; worth checking why it did not catch this.
- **A silent-failure risk compounds this**: `scripts/verify_worker_delete_grants.py`
  documents that production **already lost** `DELETE ON delivered_records` once
  through grant drift, and that the failure surfaced only as caught
  `InsufficientPrivilege` exceptions discovered months later. The purge's DELETEs
  are exposed to the same drift, so even the 365-day `county_records` purge may
  currently be a no-op in production. **Run that verifier** — it is report-only
  without `--apply`.

**Why Category B deletion is architecturally hard here, and must not be rushed:**
a property owner asking for deletion is not a tenant-scoped operation. If lead
rows are shared or dedup-claimed across tenants, deleting one tenant's rows
neither satisfies the request nor is safe for other customers who paid for that
data. And data already exported, emailed, or pushed to a customer's CRM or
webhook is **unrecoverable** — BridgeLeads has no technical ability to recall it.
Any deletion workflow therefore needs a policy decision about scope (suppress
future inclusion vs delete historical rows vs both) before any code is written.
I deliberately designed nothing here, per your instruction.

**Unverified and needed before designing anything:** whether Category B rows are
global or per-tenant; FK `ondelete` behaviour per table; whether R2 export objects
are per-user-prefixed and therefore deletable; backup and PITR retention (a
Supabase console fact, not a code fact); and whether any suppression /
do-not-contact list exists at all.

---

# 9b. SECURITY / PRIVACY ENGINEERING FINDINGS

From a frontend sweep whose central claim (no deletion endpoint) I re-verified
myself above. Treat the rest as strong leads with file:line citations rather than
as findings I personally confirmed, and note the caveat that the frontend findings
come from branch `feat/schedule-day-picker`; the marketing tree matches production
but the dashboard may not.

**Clean, and worth stating:** no hardcoded secrets found (`sk_live`, `whsec_`,
`AKIA`, `-----BEGIN`, literal credential assignments all returned nothing); the
only `NEXT_PUBLIC_*` variable in the entire frontend is `NEXT_PUBLIC_API_URL`,
which holds a public base URL; no Stripe or Supabase publishable key is referenced
at all; no token is written to `localStorage`, `sessionStorage` or a non-httpOnly
cookie; no error boundary renders `error.message` or `error.stack` (all five render
fixed copy plus `error.digest`, which is exactly the "reference id, never a stack
trace" pattern CLAUDE.md requires); and no `.env` file is tracked in git.

One nice detail: `lib/api.ts:676-689` passes the log-stream token in an
`Authorization` header rather than a query string, so it cannot leak into access logs.

**Complete client-storage inventory for the authenticated app** (this closes the
item-4 gap for the dashboard):

| Key | Mechanism | Contents | Sensitive? |
|---|---|---|---|
| `bl_cookie_ack` | localStorage | `"1"` banner acknowledgement | no |
| `bl.quota.banner.dismissed` | localStorage | `"1"` banner dismissed | no |
| `bl.referral.code` | sessionStorage | referral code from `?ref=`, regex-validated `/^[A-Z0-9]{1,16}$/` | no |
| `bl_sidebar_collapsed` | cookie (JS-written, so necessarily not httpOnly) | `"1"`/`"0"` UI preference, `samesite=lax`, `secure` on HTTPS | no |

**P1 — the backend bearer token is reachable from client-side JavaScript, for up to
7 days, with no rotation.** The backend access token is sealed into the next-auth
JWT session (`lib/auth.ts:96`) and read back in the browser via `getSession()`
(`lib/api.ts:36-42`). Session `maxAge` is 7 days (`lib/auth.ts:82`) with no
`jwt.maxAge` set. `POST /auth/refresh` **exists** in the backend spec but the
frontend never calls it, so there is no rotation; expiry is handled by a hard
sign-out on the next 401 (`lib/api.ts:72-76`). Combined with a CSP that permits
both `'unsafe-inline'` and `'unsafe-eval'` (`next.config.ts:56`), a single XSS
yields a live backend bearer token with a long window. This is the standard
Auth.js-beta pattern, so it is not a coding error, but the combination of
*client-readable token + no rotation + 7-day life + permissive CSP* is the one
security item I would raise before launch. The backend's actual token TTL should be
checked against that 7-day session.

**P2 — middleware is the sole authentication gate.** `app/(dashboard)/layout.tsx`
does not call `auth()`; it only reads a sidebar cookie. All gating lives in
`middleware.ts:17-35`. The matcher is correctly broad (`/((?!_next/static|_next/image|favicon.ico|public/).*)`)
so the authenticated routes really are covered, but there is no defence in depth.

**P2 (latent) — the public-route allowlist uses prefix matching.**
`middleware.ts:24` uses `PUBLIC_ROUTES.some(r => pathname.startsWith(r))`, which is
not path-segment aware. Any future route merely *beginning* with an allowlisted
string would be silently public (`/api/auth-debug`, `/privacy-export`,
`/termsheet`, `/registrations`). Not currently exploitable — `app/api/` contains
only the next-auth handler — but it is a trap for the next person.

**P3 — `trustHost: true`** (`lib/auth.ts:8`) with no explicit `cookies` block, so
`httpOnly`/`secure`/`sameSite` are Auth.js defaults that are never asserted in
code; an upstream default change would be invisible. Mitigated by
`next.config.ts` `serverActions.allowedOrigins`.

**P3 — two residual paths where backend text reaches the screen unfiltered.**
`lib/errors.ts:71-73` passes a backend `detail` string straight through when it is
short, single-line and clears a 14-pattern leak filter — a semantic leak (an
internal id, an enumeration hint) could still surface. And
`components/log-stream.tsx:112` renders backend job-log text verbatim in the live
log view; whether that text is sanitized upstream is **unverified**, and it matters
because scraper logs could contain third-party personal data.

**P3 — repo hygiene:** a stray note file sits inside a route directory,
`app/(dashboard)/scrapers/new/Login Screen Security • Vibe-Coding Guid.txt`.

**Tenant isolation: strong, and this is the part of the codebase I would hold up
as the model.** No IDOR-shaped route and no cross-tenant read were found. Three
mechanisms compound:

1. Tenant routes use `Depends(get_rls_db)`, which sets the `app.current_user_id`
   GUC so Postgres RLS applies, rather than plain `get_db`.
2. Every tenant read carries an explicit `user_id` predicate **even on tables
   already bound through a joined parent** — `jobs.py:53-57` comments on this
   deliberately: "every joined table carries its own `user_id` predicate in this
   codebase". That is the belt-and-suspenders posture CLAUDE.md asks for, actually
   honoured.
3. Every id-taking route loads the row with the owner filter in the same `WHERE`
   and 404s on miss — verified across jobs (`:276,291,335,621,779,992`), batches
   (via `_owned_batch()` at `:427-437`, and `_run_for` filtering *both*
   `BatchRun.user_id` and `ScraperBatch.user_id` at `:453-454`), scrapers
   (`:386,405,527-529,957,1116-1131`), notifications (`:67-69`), analytics
   (`:62,65,93`) and segments (`:516,562,657,685`).

Tenant integrity is also DB-enforced in one place worth noting: `ScraperConfig`
has a **composite FK** `(batch_id, user_id)` → `scraper_batches(id, user_id)`
(`models.py:442-451`), so a config can never point at another tenant's batch.

Admin surface is small and centrally gated: exactly two admin routes exist, with
no inline `is_admin` check surviving anywhere — `POST /scrapers/connectors` behind
`require_admin_mfa` (`scrapers.py:739`) and the activation-funnel report behind
`require_admin` with an IP-keyed limiter running *before* the admin gate
(`billing.py:35,52`). `require_admin` returns **404** rather than 403, which
avoids confirming the route exists.

The unauthenticated `GET /scrapers/sample` is a deliberate, verified-safe
exception: it uses plain `get_db` but reads only the `public_sample_cache`
singleton, never a tenant table, with all redaction upstream in the beat task
(`scheduler_helpers/public_cache.py:27-33,60-76`).

**P2 (confirmed) — one revocation gap, and it lands squarely on the deletion
work.** `src/api/routes/jobs.py:973` resolves the user as
`select(User).where(User.id == user_id)` with **no `User.is_active`**, where every
other user-resolution site pins it (`auth.py:358`, `login.py:54,159,255,435`,
`password.py:112,178`). This is the download-token path; worker-minted tokens have
a longer TTL and emailed links live 48h (`delivery.py:106`). It does check the
`jti` blacklist and `is_revoked_by_user_logout_all` (`jobs.py:940-958`), so
stamping `revoked_at` kills a token — but `is_active=False` alone would not. It is
not a tenant leak and it is **currently unexploitable because `is_active` is never
written `False`**. It matters because the obvious way to build account deletion is
a soft-delete via `is_active=False`, and this route would keep serving that user's
export downloads. Fix it as part of E5, not as standalone security work.

**P3 (confirmed safe, noted for consistency) —** two `job_logs` count queries
(`jobs.py:458-475`) are not owner-filtered, but ownership was already proven at
`:335` with a 404 on miss, so they are not exploitable. Worth aligning only
because the same file does the opposite 150 lines later (`:628-641` joins `JobLog`
through `Job` with a `user_id` filter, commenting that without it "RLS … would be
bypassed").

**Log redaction is good, with one hole — and the hole is third-party names.**
`src/utils/logger.py:31-54` carries 11 redaction patterns, attached per-handler in
`setup_logger()`: Authorization, `password=`, `api_key=`, `token=`, JWTs, `sk_*`,
Cookie/Set-Cookie, basic-auth-in-URL, **email local-parts** (`:43`, so
`j***@gmail.com`), and **labelled phone numbers** (`:50-53`). The phone rule is
deliberately label-scoped with a comment explaining why: a bare 10-digit match
would eat county parcel IDs, which are also 10 digits. That is a careful engineer.

**P2 (confirmed) — there is no rule for person names, and four scraper log
statements interpolate one.** I verified all 11 patterns; names are the single PII
class with zero coverage.

```
src/scrapers/enrichment/county_gis.py:189  _logger.info("GIS name-based fallback succeeded for %s", owner_name)
src/scrapers/enrichment/county_gis.py:196  _logger.info("WA statewide name search succeeded for %s", owner_name)
src/scrapers/enrichment/county_gis.py:285  _logger.warning("GIS name search error for %s: %s", owner_name, ...)
src/scrapers/enrichment/pacs.py:176        _logger.warning("PACS name lookup failed for %r: %s", owner_name[:30], ...)
```

These are identifiable non-customers, at INFO/WARNING, written to console **and**
to a daily file under `settings.LOGS_DIR`. The handler is a plain
`logging.FileHandler` (`logger.py:126`) — **not** rotating, no `maxBytes`, no
`backupCount`. So third-party names accumulate in a file with no rotation, no
retention limit and no deletion path, while the published policy promises the
underlying lead records are deleted at 365 days.

**The fix has an in-repo precedent**, which is the cleanest kind: log the parcel id
or the `Result` id instead of the name. `tasks_helpers/enrich.py:1261-1263` already
does exactly that, with the comment "never the homeowner's party_name". Adding a
name-redaction regex is the wrong approach — names are unbounded and a pattern
would either miss or over-match.

**P3 (confirmed) — the redaction backstop does not run in the worker.**
`install_global_redaction()` is called in `main.py` only;
`src/workers/__init__.py` logs via a bare `logging.getLogger("worker.bootstrap")`.
Those specific bootstrap lines carry no obvious PII, but the owner-name logging
above happens **in the worker** — so the process that logs names is the one the
backstop does not cover.

**P3 (confirmed, mitigated) — customer emails are logged at ~13 sites** where the
codebase's own keyed-HMAC `email_fingerprint()` helper (`logger.py:12-25`) exists
to prevent it: `workers/delivery.py:242,278,280,329,331,344,385,387`,
`workers/onboarding_emails.py:38,44,46,233`,
`workers/scheduler_helpers/billing.py:471`. All use `setup_logger()`, so the local
part is masked and only the domain survives. `delivery.py:329` also logs a client
IP beside the email.

**Tracerfy's logging is genuinely careful** — the one path I expected to be worst.
The webhook logs payload **keys only**, never the body, explicitly because the body
carries a signed `download_url` (`routes/webhooks.py:102-107`); an invalid-secret
attempt logs only `len(provided or "")`, never the value (`:65-67`); and
dialer-push responses are redacted to `"<redacted: dialer push>"` because a
rejecting endpoint may echo submitted lead fields back, with the same redaction
applied to the Celery retry exception so it cannot reach the result backend
(`webhook_delivery.py:238-245,346-350`).

**Logs do not leave the box by anything in the repo (confirmed).** No Loki,
Promtail, Datadog, Sentry or Logtail configuration exists; `monitoring/` holds only
`prometheus.yml` and `alerts.yml`, which are metrics, not logs. Logs go to stdout
(captured by Railway) plus the local file. **Railway-side log retention, access
control and any forwarding configured in that console are UNVERIFIED** and the
owner must check there.

## Webhooks — one real finding, and otherwise the strongest area of the codebase

**P1 (confirmed) — a live shared secret is written in cleartext to access logs on
every Tracerfy delivery.** The legacy route
`POST /webhooks/tracerfy/{provided_secret}` (`src/api/routes/webhooks.py:169-183`)
is still mounted and carries the secret in the **URL path**. Its own docstring
admits it: *"LEGACY: secret in the URL path. Deprecated — the path secret leaks
into access logs."*

Neither redaction mitigation covers a path segment: `main.py:112` is
`_TOKEN_RE = re.compile(r"token=[A-Za-z0-9_\-\.]+")`, which matches only a
`token=` query parameter, and `logger.py:31-54` has no URL-path rule (I read all
11 patterns).

The detail that raises this above the docstring's own framing: the docstring says
*"Current Tracerfy traffic sends no header, so this branch is inert until
migration"* — meaning **Tracerfy is actively using the path route today**. So this
is not a dormant legacy risk; the live secret is being logged on every real
delivery, right now. And it compounds with the log finding above — that file is a
plain `FileHandler` with no rotation and no deletion, so the credential
accumulates in a file nothing ever removes.

The fix is already written in the docstring and is safe because the header is
authoritative when present. **Order matters:** migrate Tracerfy to the header
route first, *then* rotate `TRACERFY_WEBHOOK_SECRET` (the old value is already in
the logs), then delete the legacy route.

**Everything else here is done well, and I want to say so specifically:**

- **Stripe: verified and fails closed.** `construct_event` on the raw body with
  the `stripe-signature` header declared as a required `Header(...)`, so a request
  without it is rejected by FastAPI validation before the handler
  (`routes/billing.py:641,671-681`). Misconfiguration also fails closed — an unset
  or under-20-character secret raises 503 rather than skipping verification
  (`:661-662`). Rate limiting runs *before* the HMAC so a bogus-signature flood is
  cheap to shed (`:669`). Replay is closed by an atomic
  `redis.set(key, "1", nx=True, ex=259200)` keyed on `event.id` (`:694-701`) — a
  3-day TTL matching Stripe's retry window, and the comment records that the prior
  get-then-setex pattern was racy and caused duplicate plan updates. No
  unauthenticated billing write exists.
- **Tracerfy header route: constant-time and fails closed.**
  `hmac.compare_digest` on UTF-8 bytes (`webhooks.py:40-42`), 503 if the env var
  is unset, 401 on mismatch, and it logs only `len(provided or "")` — never the
  value (`:51-71`).
- **Outbound webhooks are properly guarded — a customer cannot exfiltrate lead PII
  to an arbitrary address.** `validate_outbound_webhook`
  (`middleware/security.py:258-272`) requires HTTPS, caps URL length, then
  delegates to `validate_scraping_target(resolve=True)`, which blocks
  `169.254.169.254`, `metadata.google.internal`, `instance-data` and loopback
  aliases (`:89-104`), 20 CIDRs covering RFC1918, loopback, link-local, CGNAT and
  IPv6 `fe80::/10`/`fc00::/7` (`:55-85`), normalizes IPv4-mapped IPv6 so
  `::ffff:169.254.169.254` cannot dodge the v4 ranges (`:106-116`), and **fails
  closed on any host it cannot IDNA-canonicalize** (`:218-220`). Because
  `resolve=True` checks the *resolved* IPs, a public hostname pointing at a
  private address is caught. Best of all it is **re-validated immediately before
  the POST** (`workers/webhook_delivery.py:253`), which defeats DNS rebinding
  after config-save, and a blocked target returns without raising so Celery does
  not retry a permanent config error. Pydantic enforces HTTPS at save time too
  (`schemas.py:450-464`). This is textbook SSRF defence.
- **Signed export URLs: no leak path found.** The signed R2 URL travels in the
  body only to a destination that just passed the guard above, and
  `allow_redirects=False` (`webhook_delivery.py:295`) means a 30x `Location` is
  never followed — a 3xx is treated as a permanent non-retryable failure
  (`:308-320`), so a receiving endpoint cannot bounce the request and its body to
  an attacker host.

---

# 10. PRODUCT DECISIONS YOU NEED TO MAKE

I did not invent any of these, and I deliberately left every one blank rather
than guess.

1. **Legal entity name** — fills 5 placeholders across both documents.
2. **Business postal address** — required in both contact sections.
3. **Governing law and venue** — Terms §13, plus the arbitration decision your
   counsel note defers.
4. **A domain you actually control for contact mail.** Either add MX to
   `bridgeleads.io` and move the addresses to `@bridgeleads.io`, or acquire
   `bridgeleads.com` (it is parked at NameBright, so it is likely purchasable).
   Until then, pick working addresses for privacy, security and legal.
5. **A support contact** for the marketing site — there is currently none of any
   kind, and "TALK TO SALES" goes to the signup form.
6. **Refund stance**, stated deliberately: keep "non-refundable except where
   required by law", or offer a window. Then say so before purchase, not only in
   §3.
7. **Cancellation terms**: immediate or end-of-period, and what happens to leads
   already delivered.
8. **Security-log retention period** — fills the §7 placeholder.
9. **The real retention decision.** Either implement deletion for `results` and
   skip-trace contact data to match the published 365/90 days, or change the
   policy to describe what you actually do. Both are defensible; the current
   mismatch is not.
10. **Whether to register as a California data broker** — the policy already says
    you do "where required".
11. **FOUNDING25 scope**: genuinely limited (state the number) or open (drop
    "LIMITED SPOTS").
12. **Whether annual plans bill upfront**, and the exact total to publish.
13. **Whether skip-trace overage is capped**, and at what.
14. **Confirm DPAs** exist with all ten named sub-processors, or amend §6.
15. **Confirm Anthropic and Regrid** are actually used, and if Anthropic is,
    whether third-party personal data may reach it.
16. **The county number** — pick the source of truth and derive all three
    surfaces from it.
17. **Whether to keep the cookie banner at all**, given nothing is tracked.

---

# 11. ITEMS REQUIRING A LAWYER

1. **Data-broker registration** (California CPPA and the other states with
   registries). The product matches the definition and the policy already asserts
   registration. Highest priority.
2. **The skip tracing of people who never interacted with the company** — notice,
   legal basis, and what a deletion or opt-out obligation actually requires.
   This is the core legal question of the business model.
3. **Probate and death-certificate data**, which are the most sensitive
   categories being processed.
4. **California automatic-renewal law** as applied to the annual plans and the
   trial-to-paid conversion.
5. **The limitation of liability, indemnity, governing law and dispute clauses**
   — currently unenforceable as written, with an unnamed beneficiary.
6. **Earnings and ROI claims** on the pricing page (FTC exposure when selling to
   investors).
7. **The comparative pricing claim** naming PropStream.
8. **Whether §5's compliance obligations are adequately flowed down** to
   customers, and whether more than contractual assurance is needed given the
   product's output is phone numbers for distressed homeowners.
9. **Breach-notification obligations**, which the privacy policy does not address.
10. **The FCRA non-use position** — asserted in §5(b), worth confirming the
    product design supports it.

---

# 12. ENGINEERING IMPLEMENTATION PLAN (proposed, not started)

Nothing below has been implemented. Awaiting your approval, and I would split it
into the buckets you named.

**Bucket D — accessibility fixes (safe, mechanical):**
- D1 (P1) Make the nav legible at every scroll position. The robust fix is a
  solid or backdrop-blurred nav background rather than trying to align a
  text-colour swap with the band boundaries.
- D2 (P1) Add a mobile menu, or at minimum surface `Sign in` below 768px and add
  it to the footer.
- D3 (P2) Raise `#6d6d6d` on black to at least 4.5:1; fix the two 4.35 pairs.
- D4 (P2) Bring the 16px tap targets to 24×24 minimum.
- D5 (P3) Add a `<main>` landmark and a skip link; de-duplicate the hero
  heading; give `/pricing` and `/coverage` their own titles.

**Bucket F — marketing and copy corrections (no legal drafting):**
- F1 (P1) Single-source the county count; fix 19 / 20+ / 18.
- F2 (P1) Show the annual total and the billing frequency.
- F3 (P1) Remove or qualify the earnings claims and the competitor price claim.
- F4 (P2) Fix "six types" → 7, "seven templates" → 8, and the
  "Every plan reaches all supported counties" contradiction.
- F5 (P2) Point "TALK TO SALES" somewhere that is sales.
- F6 (P2) Qualify "~30 seconds" and "leads tomorrow" to supported platforms.
- F7 (P3) Delete the dead `_sections/` tree so it cannot be wired up later.

**Bucket A — engineering, pending your decisions:**
- A1 (P0) Configure MX and working mailboxes, then correct the addresses in both
  documents.
- A2 (P1) Run `scripts/verify_worker_delete_grants.py` and confirm the purge is
  not silently failing in production.
- A3 (P1) Decide retention, then either implement `results` / skip-trace purging
  or amend the policy.
- A4 (P2) Reword or remove the cookie banner to match reality.
- A5 (P3) Tighten CSP `script-src` if Next.js permits it.

**Bucket A additions (safe, ordered — do A6 first):**
- A6 (P1) **Retire the legacy Tracerfy path-secret route.** Order: point Tracerfy
  at `POST /webhooks/tracerfy` with the `X-Tracerfy-Webhook-Secret` header →
  rotate `TRACERFY_WEBHOOK_SECRET` (the old value is already in the logs) → delete
  `webhooks.py:169-183`. The header is already authoritative when present, so
  step 1 is non-breaking.
- A7 (P2) **Stop logging third-party owner names.** Replace `owner_name` with the
  parcel id or `Result` id at `county_gis.py:189,196,285` and `pacs.py:176`,
  following the existing precedent at `tasks_helpers/enrich.py:1261-1263`.
- A8 (P2) Give the log file a rotation and retention policy — it is a plain
  `FileHandler` today (`logger.py:126`), and it holds both the names above and,
  until A6 lands, a live secret.
- A9 (P3) Call `install_global_redaction()` in the Celery worker bootstrap, since
  the worker is the process that logs names.
- A10 (P3) Use the existing `email_fingerprint()` helper at the ~13 sites that log
  `user.email` directly.

**Bucket E — privacy and security architecture (needs design discussion first):**
- E1 (P1) Shorten the token exposure window: either wire up `POST /auth/refresh`
  for rotation, or cut the 7-day session, or stop exposing the backend token to
  client JS by proxying API calls server-side. Worth a design conversation, not a
  quick patch.
- E2 (P2) Add a server-side `auth()` check in `app/(dashboard)/layout.tsx` so
  middleware is not the only gate.
- E3 (P2) Make the public-route check path-segment aware instead of `startsWith`.
- E4 (P2) Assert the next-auth cookie flags explicitly rather than inheriting
  defaults.
- E5 — Deletion / DSAR workflow: **design only after** the scope decisions in
  section 10 items 9 and 17. Needs: an owner suppression list (does not exist), a
  verification method for non-customer requesters, and an explicit position on
  already-delivered data, which is technically unrecallable. Three constraints
  that must shape the design rather than be discovered during it:
  (a) **a privilege change is required** — neither application role can DELETE
  from `users`, `results` or `skip_trace_cache`, so decide deliberately whether to
  grant that or to route deletions through an audited ops path;
  (b) if you implement soft-delete via `is_active=False`, **fix `jobs.py:973`
  first** or deactivated users keep downloading exports;
  (c) `audit_events` is insert-only to the app role by design, so log-retention
  deletion needs its own decision and its own privilege.

**Bucket B/C — your decisions and counsel's drafting**, per sections 10 and 11.
I will not write legal text.

---

# 13. WHAT THIS AUDIT DID NOT COVER

Stated plainly so it is not mistaken for a clean bill of health:

- **Form consent** (item 6) — including whether the register form requires
  affirmative Terms and Privacy acceptance and whether any marketing checkbox is
  pre-checked.
- **Data minimisation** (item 7) — the per-field inventory of customer PII, and
  which personal-data columns are encrypted at the application layer vs plaintext.
- **Backend third-party and dependency inventory** (item 8) — including whether
  Anthropic and Regrid are real integrations and what data crosses each boundary.
  If Anthropic is called in the extraction pipeline, whether third-party personal
  data reaches an LLM API is an open and material question.
- **Email inventory and unsubscribe** (item 18) — end to end.
  `src/workers/onboarding_emails.py` (day-1, day-3 and trial-expiry nudges) is the
  one I would start with, since that is the category most likely to need an
  unsubscribe. Also unresolved: what domain outbound mail is sent *from*, given
  neither domain has an MX record for replies.
- ~~Backend tenant isolation~~ — **now covered**: clean, no IDOR, no cross-tenant
  read. Still open within it: whether skip-traced contact data is shared across
  tenants (a billing-fairness question as well as a privacy one) — the cache key
  became per-tenant on 2026-06-10, but `Result`-level sharing was not confirmed.
- ~~Webhook authentication and outbound SSRF~~ — **now covered**: Stripe and
  Tracerfy both verified and fail closed, outbound SSRF defence is textbook. One
  P1: the legacy path-secret route.
- ~~PII in backend logs~~ — **now covered**: one P2 (third-party owner names) and
  two P3s.
- **Public-record source terms and the full Tracerfy data flow** — the
  highest-legal-risk area, only partially characterised here.
- **The authenticated dashboard** for accessibility (tables, dialogs, dropdowns,
  pagination, notifications).
- **Dark mode** contrast.
- **Asset and font licence provenance** per asset.
- ~~The independent Codex cross-check~~ — **now done.** Blocked twice on the
  account usage limit, then completed on the third attempt (59,159 tokens, high
  reasoning effort, prompt at `scratchpad/codex-consult-v2.txt`). It was pointed at
  the four unresolved questions plus three conclusions to attack, rather than at
  ground already settled.

  **It overturned two of my findings and was right both times** — the dead
  Anthropic/Regrid path, and "cannot be deleted" (see §1a). That is the cross-check
  doing exactly its job: both errors were mine, both were the same failure mode
  (finding a call site and not checking whether the caller was live, and grepping
  `src/` without `scripts/`), and neither of the nine sub-audits caught either.

  It did **not** dispute the tenant-isolation conclusion, the DNC finding, the
  legal-placeholder findings, the undeliverable contact domain, the billing
  disclosure findings, or any severity rating. Its own remaining open item was the
  same one I have: it could not establish a live Anthropic disclosure, which is now
  resolved as "no live transfer, latent capability".

---

# 14. REMAINING RISKS

- The true county count, record counts and enrichment counts are **database
  facts** I could not query. Three published numbers disagree; at most one is right.
- Whether the 365-day purge currently runs at all in production depends on a
  grant that has drifted before.
- Whether Stripe's live configuration matches the published prices, discounts and
  trial terms is unverified — the dashboard is not readable from here.
- Whether DPAs exist with the ten named sub-processors is unverified.
- Backup and PITR retention is a Supabase console setting, invisible to code, and
  interacts with any deletion promise you make.
- The local frontend branch is `feat/schedule-day-picker`, but
  `git diff origin/master...HEAD` for the marketing tree is empty, so the code I
  read is the deployed code. Findings for the **dashboard** may drift from
  production on this branch.
- CLAUDE.md documents a top-level `workers/` directory; the real path is
  `src/workers/`. The sub-audits were briefed with the stale path, which is part
  of why their reports should be re-run rather than trusted if they arrive late.
- Another session was active in this same worktree during the audit. This audit
  wrote only files under `tasks/`.

---

**STOPPING HERE, as instructed.** Nothing deployed, no legal text published, no
billing touched, no customer data changed, no trackers added, nothing deleted.
