# Tax-Delinquent County Qualification — Vet-Then-Build Reference

**Purpose:** turn "add another tax-delinquent county" from a one-off scrape into a repeatable, scored gate. Run the [Rubric](#2-the-qualification-rubric) on every candidate BEFORE writing a line of scraper code. If a source fails any gate, the [Decision Tree](#4-decision-tree-build--buy--skip) tells you whether to BUY a feed or SKIP.

**Audience:** the person about to build the next connector. Terse, concrete, builder-to-builder.

---

## 0. Verification Status — READ FIRST

This doc mixes two very different kinds of claim. Do not treat them the same.

- **✅ VERIFIED (codebase):** the gate mechanics, `_TRUSTED_TAX_SOURCES` / `_extract_tax_fields` (`src/workers/tasks_helpers/dedup.py`), the filter columns (`src/api/tax_filters.py`), and the King/Snohomish connector behavior. These were read directly from source and you can trust them.
- **⚠️ UNVERIFIED (desk research — treat as LEADS, not facts):** every external claim — all legal statements (§3 semantics consequences, §7 RCW 61.40 / RCW 42.56.070(8) / hiQ / FCRA / TCPA), every per-county data example in §5 (Nebraska, Virginia Beach, Flathead, Pierce-balance-column, San Diego fee, FL/IL counts), and the entire §6 vendor table. These came from a one-pass research sweep, are **not** confirmed against current source/terms, and several may be stale or wrong. **Confirm each against the live source before acting, and route every legal item to counsel.** Their value is "here's where to look," not "here's what's true."

**Nothing in §5–§7 is a substitute for (a) opening the actual source and (b) legal review.** The framework (§2 rubric, §4 tree, §9 intake) is the durable asset; the examples are disposable scaffolding.

---

## 1. Purpose & Context

Today BridgeLeads supports `tax_delinquent` for exactly **two** WA counties:

| County | Pattern | Access | `source` string |
|---|---|---|---|
| **King** | Open-data portal (Socrata, dataset `dsv3-ct3e`) | HTTP GET JSON, paginated `$offset`/`$limit`, no auth, no browser | `king_county_delinquent_taxes` |
| **Snohomish** | Treasurer bulk file (~45MB pipe-delimited, ~325k rows) | HTTP GET (landing page → resolve rotating doc URL → stream to temp file) | `snohomish_county_delinquent_taxes` |

Only two — not because we ran out of time, but because **existence of public records ≠ lawful, structured, bulk-accessible data that emits a verified dollar amount and a bill year.** Many US counties appear to fail on one of those axes (an informed generalization, not a measured statistic — §0). The amount-owed / months-delinquent FILTER (`src/api/tax_filters.py`) depends on two normalized columns — `Result.delinquent_amount` + `Result.delinquent_bill_year` — and those are only populated when a row's `enrichment_data["source"]` is in the allowlist:

```python
# src/workers/tasks_helpers/dedup.py
_TRUSTED_TAX_SOURCES = frozenset({
    "king_county_delinquent_taxes",
    "snohomish_county_delinquent_taxes",
})
```

`_extract_tax_fields` returns `(None, None)` for any source not in that set. NULL amount/year ⇒ the filter never matches the row. **The allowlist is the load-bearing trust boundary.** Never widen it to a generic "if the keys exist" check — that mis-populates the filter for any scraper reusing the key names with a different meaning (see [§3 Semantics Warning](#3-semantics-warning--amount-owed-is-not-universal)).

A county joins the allowlist only **after** it passes the Rubric AND its amount field's meaning is verified to match King/Snohomish.

---

## 2. The Qualification Rubric

Score every candidate. Each gate gets one of three marks:

- **✅ PASS** — confirmed satisfied.
- **❌ FAIL** — confirmed violated. A ❌ on any **HARD** gate = do **not** build a connector; route to the [Decision Tree](#4-decision-tree-build--buy--skip).
- **⚠️ CONDITIONAL** — unknown / unverified / grey (e.g. ToS not yet confirmed, monitoring weaker than ideal). **A ⚠️ is NOT a pass.** It is allowed to ship only if the open item is *logged* (intake + §7 counsel list) and is not a known ban. King and Snohomish each ship with ⚠️ items (below) — that is the documented exception path, not a loophole. A ⚠️ that resolves to a confirmed ban becomes ❌ retroactively and the source must be pulled.

SOFT gates can be conditionally accepted but must be documented in the intake with a concrete remediation plan (not a hand-wave).

| # | Gate | Pass condition | Type | King | Snoho |
|---|---|---|---|---|---|
| G1 | **Bulk / API accessible** | One HTTP request yields many parcels (API endpoint or bulk file). NOT per-parcel-lookup-only. | HARD | ✅ Socrata JSON | ✅ bulk file |
| G2 | **Legally permissible to automate for commercial resale** | No *confirmed* ToS ban on automated/bulk access for the *specific* surface used; [§7 counsel checklist](#7-non-starters--requires-counsel-sign-off) clear or items logged. Confirmed recorder/portal ToS ban = ❌. | HARD | ⚠️ open-data license but WA commercial-purpose overlay open ([§7-A](#requires-counsel-sign-off)) | ⚠️ bulk-file ToS unverified ([§7-B](#requires-counsel-sign-off)) |
| G3 | **No access-control wall** | No Cloudflare / CAPTCHA / bot-challenge / login-gate / JS-only-render barrier in the path, AND no `robots.txt` disallow or explicit rate-limit/cease-and-desist on the surface used. We do NOT build evasion (compliance line). Respect robots + sane request rate even when the gate is absent. | HARD | ✅ | ✅ |
| G4 | **Emits delinquent AMOUNT OWED** | A positive, machine-readable dollar figure per parcel; meaning verified per [§3](#3-semantics-warning--amount-owed-is-not-universal). NOT a status-only flag. | HARD | ✅ `billed - paid` | ✅ Σ owed across years |
| G5 | **Emits TAX / BILL YEAR** | Integer year the obligation was *billed* (not assessment year, not a current/prior status flag). | HARD | ✅ `bill_year` | ✅ `min(delinquent_years)` |
| G6 | **Emits PARCEL ID** | Fixed-format digit string, directly provided or deterministically derivable; joinable to GIS. NOT address-inferred. | HARD | ✅ 10-digit (Major6+Minor4) | ✅ 14-digit real property |
| G7 | **Emits OWNER + mailing address** | Owner name AND mailing address, from source OR via a *verified, working* parcel→GIS enrichment path (not a hypothetical one). | SOFT | ✅ via eRealProperty/GIS (verified working in prod) | ✅ in file |
| G8 | **Known refresh cadence** | Documented update frequency (live / daily / weekly / monthly / annual). | SOFT | ✅ live API | ✅ monthly |
| G9 | **Monitorable for schema/file change** | Connector can detect a silent format change and fail loud. For **bulk-file** sources this is HARD (field-count + malformed-ratio + zero-output canaries are cheap and the failure mode is silent garbage). For **structured-API** sources (King) the schema is contract-stable, so exception-on-shape-change is the accepted floor — a documented ⚠️, not a ❌. | HARD (bulk) / floor=⚠️ (API) | ⚠️ exception-only (accepted for API) | ✅ 17-field + ≤20% malformed + ≥1-delinquent canaries |

### What the FILTER specifically requires (G4 + G5 are non-negotiable)

The filter only works if the scraper emits, in `enrichment_data`, with these exact names and shapes:

- **`delinquent_amount`** — `Decimal` string (never float), quantized to `0.01`, clamped `[0, 99_999_999.99]`. King/Snoho both serialize via `str(Decimal(...))` to avoid float drift.
- **`bill_year`** — integer, clamped `[1900, current_year + 1]` (input sanitization). **Current-year handling is per-source, by data shape:**
  - **Snohomish** (file lists ALL parcels) MUST exclude current-year (`tax_year < as_of_year`) to isolate the delinquent ones.
  - **King** (dataset is ONLY delinquent receivables) INCLUDES current-year: in WA a missed first-half installment (Apr 30) accelerates the full year to delinquent (RCW 84.56.020), so a current-year King row is a genuine fresh lead. King is ~99% current-year, so excluding it would gut the connector.
  - The **targeting window** ("how recently delinquent") is the user's job, via the scrape date range + the **`max_months` delinquent filter** (e.g. 18 → only parcels first delinquent in the last ~1.5 yrs). `bill_year` = the *oldest* delinquent year per parcel, so `max_months` correctly excludes long-delinquent (likely-already-foreclosing) parcels.
- Months-delinquent is derived at query time: `base = today.year*12 + (today.month-1)`; the filter assumes bills issue ~Jan 1 (`date_recorded = "01/01/{bill_year}"` in both connectors). ⚠️ This Jan-1 assumption is a WA-calendar approximation. Counties on a fiscal July-1 (or other) cycle will skew the months math — for those, normalize the originating bill year so "01/01/{bill_year}" still orders delinquencies correctly, or the months-filter will mis-bucket them.

**RESOLVED STANDARD (2026-06-15, by LLM-council + Codex).** The canonical `delinquent_amount` is the **total unpaid principal on the property-tax bill, per parcel, summed across ALL charge types (real-property levy + special assessments) AND across all delinquent years.** Both connectors now follow this:
- **Snohomish** already did (sum col-16 balance across a parcel's delinquent years).
- **King** was FIXED this date: it previously filtered `receivable_type='D'` (mis-read as "Delinquent" — it is the *Drainage* charge code), capturing ~0.6% of delinquent parcels and reporting a single tiny line. It now sums `(billed - paid)` across all included charge types (R/N/V/U/X/E/F/D/I/C/O/W, excluding A=Abatement) and all delinquent years per parcel. See `src/scrapers/king_wa_tax_delinquent.py::aggregate_delinquent_rows`.

**Penalty + interest are OUT OF SCOPE (deferred).** Neither county exposes them in bulk data (both compute them at payment time), so the figure is **principal only** — label it so. True payoff (principal + penalty + interest) would require a per-parcel live lookup on each county's payment portal (slow/fragile per-parcel scraping we avoid) — a future enhancement, not the current standard. When adding a third source, match THIS standard: sum all delinquent tax-bill charges per parcel across years, principal, exclude credits/abatements, verify it means the same thing before joining `_TRUSTED_TAX_SOURCES`.

G7 (owner/address) is SOFT because King has no owner in the API and we enrich from GIS + eRealProperty (a path verified working in production). A SOFT-fail on G7 means "you must supply a *verified* enrichment path before ship," not "ship it empty" and not "promise to figure it out later."

---

## 3. Semantics Warning — "Amount Owed" Is NOT Universal

**The single most dangerous failure mode is joining a source whose amount means something different from King/Snoho.** "Amount owed" and "months delinquent" are not standardized across 3,000+ counties. Before a source enters `_TRUSTED_TAX_SOURCES`, you MUST verify which of these its amount field actually represents:

| Variant | What it means | Safe to treat as our `delinquent_amount`? |
|---|---|---|
| **Total due** | Full unpaid balance incl. current-year + all prior | ⚠️ Often too broad — may include not-yet-delinquent current year. King/Snoho exclude current year. Verify. |
| **Prior-year balance** | Only delinquent (past-cycle) principal | ✅ Closest to our semantics (Snoho = Σ owed across `tax_year < as_of_year`). |
| **Installment balance** | One unpaid installment of a multi-pay schedule | ❌ Understates true delinquency; needs aggregation across installments first. |
| **Penalties / interest** | Penalty + interest only, principal elsewhere | ❌ Not principal owed; do not use alone. King uses `billed - paid` (principal). |
| **Redemption amount** | Payoff to redeem after tax sale (principal+interest+fees+costs) | ❌ Inflated, post-sale concept; different population entirely. |
| **Lien / certificate amount** | Face value of a sold tax-lien certificate | ❌ Lien-sale concept (FL LienHub), not a live treasurer balance. |
| **Status-only** | Boolean "is delinquent" with no dollar figure | ❌ FAILS G4 outright (PropStream/Tracerfy-style flag). |

**Rule:** the new source's amount must represent **delinquent (prior-cycle) money owed on the parcel** — the live treasurer obligation, not a post-sale or status concept. Within that target, our two existing sources already differ on a tolerated detail: King is `billed - paid` (**principal**), Snoho is the file's balance column (**may include penalty/interest**). So the requirement is NOT "identical to both" (they aren't identical to each other) — it is: (a) the amount is a real prior-cycle owed figure, (b) you **explicitly classify and document** whether it's principal-only or balance-incl-penalty/interest, and (c) it is one of those two accepted shapes (or transformed, documented, to one). Anything in the ❌ rows above (installment-only, penalty/interest-only, redemption, lien-certificate, status-only) does NOT qualify — transform it in the scraper (documented) or do NOT add it to the allowlist. CA's "tax-defaulted" redemption amounts and FL's certificate face values are classic traps. (Reconciling King-vs-Snoho onto one convention is the open product decision noted in §2.)

Likewise **months-delinquent** assumes a ~Jan-1 bill date and integer bill year. A county that bills on a fiscal July-1 cycle, or reports "years delinquent" as a count rather than the originating year, will skew the months math — normalize to the originating *bill year* before emitting `bill_year`.

---

## 4. Decision Tree: BUILD / BUY / SKIP

```
Candidate county/source
│
├─ G1 bulk/API? ──NO──────────────► is it per-parcel vendor portal (Tyler/DEVNET/Aumentum/Manatron)?
│                                     ├─ yes → SKIP (public-records request (state open-records law, not federal FOIA)-only) or BUY a vendor feed
│                                     └─ recorder/portal ToS ban → SKIP (NON-STARTER)
│ YES
├─ G2 legal to automate+resell? ──❌ CONFIRMED BAN──► SKIP (NON-STARTER).
│                                  ──⚠️ GREY/UNVERIFIED──► log §7 counsel item; MAY proceed
│                                       conditionally (like King/Snoho) — do NOT hard-SKIP grey.
│ PASS or ⚠️-logged
├─ G3 anti-bot wall? ──YES (wall)────► SKIP. Never build evasion. Re-route to BUY.
│ NO
├─ G4 amount owed (verified §3)? ──NO──► amount missing → BUY (vendor with $ field) or SKIP.
│ PASS                                   amount present but WRONG semantics → SKIP until transformable.
├─ G5 bill year? ──NO─────────────────► SKIP (filter unusable).
│ PASS
├─ G6 parcel id? ──NO─────────────────► SKIP.
│ PASS
└─► BUILD a custom connector (King = open-data model; Snoho = bulk-file model).
    Then verify §3 semantics, add canaries (G9), and ONLY THEN add to _TRUSTED_TAX_SOURCES.
```

**Deciding factors:**

- **BUILD** when the county itself publishes a free bulk file or open-data API with a real $ amount + bill year, no ToS/bot blocker. Marginal cost per county ≈ one parser + canaries. This is the King/Snoho model and remains the cheapest, most-defensible path for WA and any county with open data.
- **BUY** when (a) the county is per-parcel-portal-only or bot-walled but the lead value is high, or (b) you need national scale faster than per-county builds allow. The catch: **redistribution rights** (see [§6](#6-vendor-evaluation-summary)). Nearly every vendor bars resale on standard terms; you need a negotiated reseller addendum, and most vendors don't even document a delinquent-AMOUNT field.
- **SKIP / DEFER** when no lawful bulk path exists, the amount is status-only or wrong-semantics, or it's bot-walled. Revisit if the county opens data or a vendor relationship lands.

---

## 5. Per-Publication-Pattern Playbook

> ⚠️ **Every county/portal/vendor example below is UNVERIFIED desk research (§0).** Counts ("60+ FL counties", "majority of IL", "all 93 NE counties"), cadences ("daily", "monthly"), prices, field lists, and ToS characterizations are research leads, not confirmed facts. **Open the actual source and confirm before relying on any of them.** Treat the *pattern shapes and gotchas* as the durable takeaway; treat the named examples as starting points to verify.

These five patterns appear to cover most US counties (not proven — a generalization from a one-pass sweep). Effort + gotchas per pattern:

### Pattern 1 — Open-Data Portal (Socrata / ArcGIS Hub / CKAN)
- **Effort:** LOW. HTTP GET JSON/CSV/GeoJSON or REST feature service. No browser. King's model.
- **Examples that already match our field model:** King WA (Socrata), Virginia Beach VA (ArcGIS, **daily**, full fields incl. tax_due/penalty/interest/total/tax_year), Philadelphia PA, Pittsburgh/Allegheny (CKAN, **owner names omitted** → G7 fail), Milwaukee WI (XLSX, monthly), Cook County IL (monthly clerk file), Essex County NY (public CSV builder, selectable by year).
- **Gotchas:** paginate (`$offset`/`$limit`); some portals omit owner name (G7); confirm the dataset is the *delinquent* roll, not the full assessment roll; verify the $ column is prior-cycle principal not total-due (§3); WA RCW 42.56.070(8) commercial-purpose overlay still applies regardless of the portal license ([§7-A](#requires-counsel-sign-off)).

### Pattern 2 — Treasurer Bulk Flat File (the Snohomish model)
- **Effort:** MEDIUM-HIGH. Format-specific parser (pipe-delimited, fixed-width `.dat`, CSV). Snoho's model.
- **Examples:** Snohomish WA (pipe, ~45MB), Flathead County MT (fixed-width `.dat`, implied-decimal amounts, daily), Pierce WA (zipped pipe, weekly — but **delinquency must be inferred from payment fields**), Ventura CA (monthly Excel), Johnston County NC (CSV), Cameron County TX (zipped CSV), San Diego CA ("Delinquent Master Tax File" — **$86 + email request, not free/public**).
- **Gotchas:** rotating download URL (Snoho rotates the doc ID monthly → resolve from landing page each run); **stream to temp file, never load to RAM** (`settings.MAX_DOWNLOAD_BYTES` cap, delete on error); fixed-width/implied-decimal parsing; aggregate per-parcel across multiple delinquent years; exclude personal-property accounts (Snoho: process 14-digit only, skip 7-digit); **mandatory canaries** (G9): field-count, ≤20% malformed-row ratio, ≥1 delinquent row after filtering — these catch silent format changes. The "TRW" (Tax Roll Write) legacy fixed-column format appears in rural/midwest counties with no standard parser.

### Pattern 3 — Vendor-Hosted Portal, Per-Parcel (Tyler iasWorld / DEVNET wEdge / Grant Street / Aumentum / Manatron)
- **Effort:** N/A for scraping — **per-parcel-only, no bulk endpoint, and ToS typically bans bots.** Fails G1 and usually G2.
- **Examples:** Tyler iasWorld Public Access (most large counties); DEVNET wEdge (majority of IL counties — explicit anti-crawler ToS); Grant Street TaxSys/LienHub (~70% of FL — but see Pattern 5); Aumentum, Manatron PropertyMax, Tyler iTax.
- **Gotchas:** the only lawful bulk path is a county public-records request (state open-records law, not federal FOIA) / open-records request → flat file (days of latency, possible fees). DEVNET explicitly routes bulk to the county Treasurer/SoA. **Do not scrape these portals.** Route to public-records request (state open-records law, not federal FOIA)-then-Pattern-2, or BUY.

### Pattern 4 — Statewide Centralized System
- **Effort:** LOW where it exists (one HTTP GET = whole state); rare.
- **Examples:** Nebraska DOR publishes the entire statewide delinquent real-property list (PDF + Excel) each February — **one download covers all 93 counties.** Iowa (iowatreasurers.org) is per-parcel transaction-focused, no statewide bulk. WA has **no** statewide delinquent list (39 counties each own their system). TX Comptroller transparency data excludes parcel delinquency.
- **Gotchas:** annual-only snapshots (NE = once/Feb); field set may not be enumerated until you parse it; verify amount semantics (§3) since it's a county-submission aggregate.

### Pattern 5 — Annual Lien-Sale / Certificate-Sale List (PDF or XLS)
- **Effort:** LOW-MEDIUM (XLS) to HIGH (PDF parsing). Annual cadence only.
- **Examples:** FL LienHub/RealAuction pre-sale spreadsheets each **May** (parcel, owner, gross tax + interest + fees — covers 60+ FL counties); CO/IL/OH/SC county PDFs 10–60 days pre-sale; McLean County IL ($50, ~10 days pre-sale); Riverside CA inventory (HTML table). Mecklenburg NC publishes only in the newspaper (~43k parcels, not downloadable).
- **Gotchas:** **point-in-time** — a parcel paid off before the sale drops off; not a continuous feed. **Amount is often lien/certificate face value or redemption amount, NOT live prior-cycle principal** (§3 trap). Likely the most widely available pattern (a legal tax sale generally requires *some* public notice), but notice medium varies by state — newspaper-only/non-downloadable is common (e.g. Mecklenburg NC) — and it has the weakest semantics match.

---

## 6. Vendor Evaluation Summary (Build vs Buy at National Scale)

> ⚠️ **This entire table is UNVERIFIED desk research (§0).** Coverage numbers, "$ amount field" presence, API availability, redistribution-rights characterizations, and pricing were gathered in one pass from public marketing/docs and **are not confirmed against any current contract or terms of service.** Vendor terms change and are often negotiated per-deal. Use this only to decide *who to send an RFP to*; **confirm every cell with the vendor and route all licensing/resale terms to counsel before committing.** Do not quote these as facts to anyone.

**Headline:** on the research available, no major vendor appears to check all four boxes (national tax-delinquent-AMOUNT coverage + API + SaaS redistribution rights + commercial resale) on standard terms. Resale rights are the hardest constraint — every aggregator defaults to "internal use only" and bars derived-product resale, because their business model *is* the subscription they'd be cannibalizing.

| Vendor | National coverage | Delinquent $ amount field | Bill/tax year | API | SaaS redistribution | Pricing |
|---|---|---|---|---|---|---|
| **ICE / Black Knight** | 3,100+ counties | Not publicly documented (severity only) | Inferred | Yes | **Conditionally YES** — publicly states resellers can build value-added solutions; enterprise contract | Enterprise |
| **ATTOM** | ~3,000 counties | Inferred via `/assessment/detail`; no dedicated field | `taxyear` ✅ | Yes (REST) | **No** standard; custom enterprise addendum negotiable | Annual license |
| **CoreLogic / Cotality** | 99.7% counties | Status/history; $ not documented | Inferred | Yes | **No** (ToC restricts); no public leads VAR | Enterprise quote |
| **First American / DataTree / TaxSource** | 100% counties | Prior-year delinquency; $ not documented | Inferred | Yes (JSON) | **Unknown** — no public VAR | Enterprise quote |
| **PropertyRadar** | "Majority" counties | **YES — documented "Delinquent Amount" + "Delinquent Since Year"** (best-documented) | ✅ | Yes (OAuth, end-user only) | **No** standard; partner/OAuth = shared end-customers only | Subscription |
| **BatchData** | 3,200+ counties | Not in public schema | Not in schema | Yes (REST) | **No** standard; VP-signed Reseller Addendum required | Monthly/annual |
| **TaxNetUSA** | **TX + FL only** | ✅ delinquent bills, direct-from-collector | ✅ | Yes (XML/JSON) | Unknown | Custom quote |
| **Realie.ai** | 3,100+ counties | **No** (not in schema) | `taxYear` only | Yes (<10ms) | Unknown | From $50/mo |
| **PropStream** | All counties | Flag only | No | **No** (SaaS only) | **No — explicitly banned** | $99+/mo |
| **Tracerfy** (current skip-trace vendor) | 50 states | Flag only | No | Yes | White-label mentioned; terms not public | $0.20/hit |

**Reading:**
- **Best resale posture:** ICE (only vendor with a public reseller statement) — but oriented at mortgage servicers/title, so a "RE-investor lead SaaS reseller" contract is a long, expensive sales cycle.
- **Best-documented delinquent-$ field:** PropertyRadar — but its API bars building a competing SaaS. Useful as a *semantics reference*, not a feed.
- **Confirmed delinquent-$ + bill year + direct-from-collector:** TaxNetUSA — but TX/FL only (still the #1/#2 investor markets).
- **Build-vs-buy economics:** custom enterprise/reseller addenda commonly run $25K–$100K+/yr with VP sign-off and flow-down obligations. For WA expansion specifically, vendors add little — King (Socrata) and Snoho (bulk file) are already the deepest WA datasets and are free. **Default to BUILD for any county with open data; reserve BUY for national scale or high-value bot-walled markets.**
- **Do not pursue:** PropStream (bars resale), PropertyRadar standard API (same), Realie.ai unreviewed (unknown terms + no $ field).

**BUY-path diligence (before signing any feed) — "redistribution rights" is necessary but not sufficient. Also nail down:** field-level provenance + a confirmed delinquent-**amount** field with verified §3 semantics; refresh cadence + freshness SLA; **sublicensing / per-tenant use limits** (can each BridgeLeads customer use the derived leads?); deletion / data-subject-request flow-down; audit rights; and any DNC / FCRA / consumer-use restrictions baked into the license. A feed that allows resale but forbids per-end-customer use, or carries deletion obligations we can't honor per-tenant, is not usable. Put each of these in the RFP.

---

## 7. Non-Starters + "Requires Counsel Sign-Off"

> ⚠️ **§7 is non-lawyer desk research (§0). Nothing here is legal advice. Every item below must be confirmed by counsel against current statutes/case law before relied on.** Statute numbers, effective dates, and case holdings may be imprecise or stale.

"Public record" ≠ "permitted to automate for resale." This is the durable principle; the specifics need counsel. As *general background* (verify all of it): the `hiQ v. LinkedIn` line of cases is often read to suggest that scraping *gateless, unauthenticated public* pages is not, by itself, a CFAA violation — but that posture is narrow (Ninth Circuit, preliminary-injunction stage) and **LinkedIn later prevailed on breach-of-contract**, so "public page = legally safe" is the wrong inference. Breach-of-contract (ToS), DMCA §1201 (access-control circumvention), trespass-to-chattels, and state public-records / commercial-use law can each apply independently of CFAA. Commercial actors generally get *higher* scrutiny. **Do not let an engineer infer a green light from this paragraph — it exists to say "stop and ask counsel," not "go."**

### NON-STARTERS (bright-line — do NOT build regardless of technical feasibility)

| # | Non-starter | Basis |
|---|---|---|
| 1 | Bypass Cloudflare / any bot-protection on any county portal (**Spokane**) | CFAA post-block risk; DMCA §1201; ToS breach; project compliance line |
| 2 | Bypass / solve CAPTCHA (third-party or ML) | DMCA §1201; CFAA; ToS; explicit project policy |
| 3 | Scrape **Snohomish Recording portal** (LandmarkWeb / snoco.org) index/images | Explicit ToS ban on automated access, data mining, commercial use |
| 4 | Scrape **King LandmarkWeb recorder** portal | Known ToS ban on automation (documented + confirmed) |
| 5 | Build **Pierce** tax-delinquent via Data Mart `tax_account` | **No balance column** → fails G4; portal almost certainly anti-automation ToS |
| 6 | Serve WA tax-delinquent lists without surfacing the **RCW 61.40** (reportedly a "Solicited Real Property" act, *claimed* eff. 2026-01-01 — verify cite + scope) obligations to users | ⚠️ Users making off-market solicitations may face WA Consumer Protection Act exposure (the "treble damages + fees" figure is a CPA *generalization*, not what RCW 61.40 itself states — confirm with counsel). ToS should educate/disclaim regardless. |
| 7 | Provide skip-traced phones without DNC-scrub tooling / DNC-status flag | TCPA + WA Mini-TCPA (RCW 19.190); $500–$1,500/violation; current `phone_dnc_flag = NULL` is a known gap |
| 8 | Represent data as a "consumer report" / market to FCRA use cases (credit/employment/housing screening) | FCRA CRA classification; adding bankruptcy/lien signals raises this risk |

### Requires Counsel Sign-Off (grey areas — legal review BEFORE building)

| # | Issue | Why |
|---|---|---|
| A | Do King-Socrata API calls = "commercial-purpose request" under **RCW 42.56.070(8)** (verify subsection)? Can the county revoke API access? | ⚠️ *Believed* (uncited — needs the actual AG opinion / case) that sortable electronic property records can be treated as "lists of individuals"; the statute restricts the *agency* not us, but if so it creates API-access *fragility* (county could cut access), not a direct liability. Confirm the authority before treating as real. |
| B | Is the **Snohomish Treasurer bulk file** (direct download, not portal) covered by any ToS or RCW 42.56.070(8)? | No clear ToS found for the bulk-file URL; recorder-portal ToS is a *different* system — verify (this is Snoho's G2 ⚠️) |
| C | Does our aggregation+resale = regulated "data broker" activity (registration + deletion-request duties)? | ⚠️ Several states have data-broker registration regimes (commonly cited: CA, VT, TX, OR — verify which apply, their thresholds, and whether BridgeLeads' volume/activity crosses them). Needs a jurisdiction-by-jurisdiction determination with counsel, not a blanket assumption. |
| D | Copyright status of King/Snoho structured DBs as compilations | 17 U.S.C. §105 covers only *federal* works; thin compilation copyright possible on state DBs |
| E | RCW 61.40 duty on BridgeLeads itself vs. its users | We're not the offeror, but marketing posture affects secondary exposure |
| F | TCPA status of "offers to purchase real property" as solicitations | Courts split; WA's broader rules may close the gap |

**Defensible posture (stay inside this line):** API/bulk-file access from a government open-data program → no anti-bot bypass → no recorder-portal scraping → clear downstream-use disclosures to users → no FCRA-triggering signals. Everything outside requires counsel first.

---

## 8. Worked Examples (the Rubric predicting every known outcome)

| Candidate | G1 bulk | G2 legal | G3 no-bot | G4 amount | G5 year | G6 parcel | G9 monitor | **Verdict** | Why |
|---|---|---|---|---|---|---|---|---|---|
| **King WA** (Socrata `dsv3-ct3e`) | ✅ | ⚠️ §7-A | ✅ | ✅ `billed-paid` | ✅ | ✅ 10-digit | ⚠️ API floor | **PASS (conditional)** | No ❌ on any HARD gate; G2/G9 ⚠️ logged; in allowlist |
| **Snohomish WA** (Treasurer bulk file) | ✅ | ⚠️ §7-B | ✅ | ✅ Σ owed (balance, may incl. penalty/int) | ✅ | ✅ 14-digit | ✅ | **PASS (conditional)** | No ❌ on any HARD gate; G2 ⚠️ logged for counsel; in allowlist |
| **Pierce WA** (Data Mart `tax_account`) | ✅ | — | ✅ | ❌ **no balance column** | — | ✅ | — | **FAIL** | G4 fails — cannot compute amount owed → filter unusable. Weekly bulk file exists but delinquency must be inferred; revisit only if a balance field surfaces |
| **Kitsap WA** | ❌ nothing usable published | — | — | ❌ | ❌ | — | — | **FAIL** | G1+G4+G5 fail — no usable bulk/structured data. SKIP |
| **Spokane WA** | — | ❌ | ❌ **Cloudflare** | — | — | — | — | **FAIL** | G3 fails — bot wall. NON-STARTER #1; never build evasion. Route to BUY if high-value |
| **King LandmarkWeb** (recorder portal) | ❌ per-doc | ❌ **ToS bans automation** | — | n/a (recorder, not tax) | — | — | — | **FAIL** | G1+G2 fail. NON-STARTER #4 |
| **Clark WA** (LandmarkWeb recorder + Treasurer distraint) | ❌ recorder per-doc | ❌ **distraint list = RCW 42.56.070(8) commercial-use block** | — | ❌ recorder path = Federal Tax Lien only (no parcel, 0 leads — mig 066) | ❌ | — | — | **FAIL → ABANDONED 2026-06-21** | Recorder path yields federal liens / mislabeled deeds (BACKLOG §9); real Treasurer distraint list is compliance-blocked. King+Snoho stay the only WA tax sources |

The Rubric reproduces all seven **known WA outcomes**: King PASS-conditional, Snoho PASS-conditional, Pierce FAIL (G4), Kitsap FAIL (G1/G4/G5), Spokane FAIL (G3), King recorder FAIL (G1/G2), Clark FAIL (G1/G2, abandoned 2026-06-21). That is a **sanity check against the cases we already know**, NOT national validation — these are seven hand-picked WA counties (and several FAIL inputs are themselves unverified, §0). The framework is unproven outside this set; treat its first ~5 real out-of-state applications as a calibration period and expect to revise gates.

---

## 9. Source Qualification Intake (copy-paste per candidate)

```markdown
## Tax-Delinquent Source Intake — <County>, <State>

- Candidate source URL:
- Publication pattern (1 open-data / 2 bulk-file / 3 vendor-portal / 4 statewide / 5 lien-sale):
- Vendor/platform (if any) (Socrata / ArcGIS / CKAN / Tyler / DEVNET / GrantStreet / Aumentum / other):

### Rubric (mark each ✅ PASS / ❌ FAIL / ⚠️ CONDITIONAL — see §2; a ❌ on any HARD gate blocks BUILD, a ⚠️ ships only if the item is logged below)
- [ ] G1 bulk/API accessible (not per-parcel-only) [HARD]:        PASS/FAIL/⚠️  — notes:
- [ ] G2 legal to automate + resell (no confirmed ToS ban) [HARD]: PASS/FAIL/⚠️  — ToS URL + counsel item(s):
- [ ] G3 no access-control wall (Cloudflare/CAPTCHA/robots/C&D) [HARD]: PASS/FAIL/⚠️  — notes:
- [ ] G4 emits delinquent AMOUNT owed [HARD]:                      PASS/FAIL/⚠️  — field name + raw sample:
- [ ] G5 emits TAX/BILL YEAR [HARD]:                               PASS/FAIL/⚠️  — field name + raw sample:
- [ ] G6 emits PARCEL ID (fixed-format, GIS-joinable) [HARD]:      PASS/FAIL/⚠️  — format/length + derivation:
- [ ] G7 emits OWNER + mailing address (or VERIFIED enrichment) [SOFT]: PASS/FAIL/⚠️  — source or verified enrichment path:
- [ ] G8 known refresh cadence [SOFT]:                             PASS/FAIL/⚠️  — cadence:
- [ ] G9 monitorable (HARD for bulk / ⚠️-floor for stable API):    PASS/FAIL/⚠️  — field-count / malformed / zero-output plan:

### ⚠️ CONDITIONAL items log (every ⚠️ above MUST be listed here with owner + resolution path, or it's a ❌)
- ⚠️ item:                                                         — open question / counsel ref / when to resolve:

### §3 Amount Semantics Verification (MANDATORY before allowlist)
- What does the amount field actually mean? (total due / prior-year balance / installment /
  penalties+interest / redemption / lien-certificate / status-only):
- Does it match "prior-cycle principal owed" (King billed-paid / Snoho Σ-owed)?  YES / NO
- Transform required to match?  (describe, or N/A):
- Current-year rows excluded?  YES / NO

### Decision
- BUILD / BUY / SKIP:
- If BUILD — connector model (King open-data / Snoho bulk-file):
- If BUY — vendor + redistribution-rights status:
- Proposed `source` string (e.g. `<county>_county_delinquent_taxes`):

### Ship checklist (only after BUILD + §3 verified)
- [ ] Scraper extends BaseScraper; emits delinquent_amount (Decimal str, [0,99999999.99]) + bill_year (int [1900,curr+1])
- [ ] date_recorded = "01/01/{bill_year}" (if county is NOT on a Jan-1 bill calendar, confirm this still orders delinquencies correctly for the months-filter); current-year + future rows excluded
- [ ] Hardcoded unique `source` string in enrichment_data
- [ ] Canaries (G9) wired: field-count + malformed-ratio + zero-delinquent halt
- [ ] Bulk file streamed to temp, MAX_DOWNLOAD_BYTES cap, deleted on error
- [ ] `source` string added to _TRUSTED_TAX_SOURCES (dedup.py) — AFTER §3 verification
- [ ] Registered in registry.py + county_connectors row (ships health=unknown)
- [ ] Tests mirror test_king_tax_delinquent.py / test_snohomish_tax.py
- [ ] Counsel sign-off on any §7 grey item (A–F) that applies
```

---

**File:** `docs/research/record-type-fields/tax-delinquent-county-qualification.md`
**Allowlist source of truth:** `src/workers/tasks_helpers/dedup.py` (`_TRUSTED_TAX_SOURCES`, `_extract_tax_fields`)
**Filter consumer:** `src/api/tax_filters.py` (`Result.delinquent_amount`, `Result.delinquent_bill_year`)
**Reference connectors:** `src/scrapers/king_wa_tax_delinquent.py`, `src/scrapers/snohomish_wa_tax_delinquent.py`
