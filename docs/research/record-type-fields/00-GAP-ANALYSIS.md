# BridgeLeads Record-Type Field Gap Analysis (2026-06-12)

**Method:** 6 parallel web-research agents (one per record type) on what RE
wholesalers/investors actually want per lead type + competitor field benchmarks
(PropertyRadar, PropStream, BatchLeads/BatchData, All The Leads, DealMachine,
ATTOM, Tax Sale Resources, SuccessorsData), cross-referenced against a verified
codebase audit of what BridgeLeads delivers, pressure-tested by an independent
Codex product consult. Raw per-type reports: `01-probate.md` (saved);
02-pre-foreclosure, 03-tax-delinquent, 04-code-violation, 05-divorce,
06-death-cert summarized below (full text in session transcript 2026-06-12).

---

## The one-sentence finding

BridgeLeads' **source freshness is best-in-class** (county-direct daily scrape vs
competitors' 3–6-month-stale recycled lists) and its **6-type WA bundle is
differentiated** — but per-lead it **feels less actionable** than PropStream/
PropertyRadar because it ships none of the three things every investor filters on
first: **equity, absentee status, and a motivation/urgency signal** — two of which
we can compute *today* from data we already hold.

---

## What BridgeLeads delivers today (verified)

CSV columns: `date_recorded, party_name, heirs, parcel_id, property_address`
(+street/city/state/zip splits)`, mailing_address, legal_description, doc_type,
delinquent_amount, delinquent_bill_year, phone/phone_2/phone_3 + phone_type,
email/email_2/email_3, first_name/last_name`.
Enrichment fills property/mailing address + parcel; **`assessed_value` is captured
into `enrichment_data` but NOT exported.** Skip trace adds ≤3 phones/≤3 emails and
**drops** Tracerfy's age/relatives. `enrichment_data` JSON (code-violation
status/description/inspection, tax billed/paid/account_status, instrument_number,
assessed_value) is shown in the UI but **never written to the CSV**.

**The universal gaps (all 6 types):** no equity/value, no absentee flag (we have
both addresses and never compare them), no vacancy, no per-type motivation score,
no freshness/urgency field.

---

## Per-record-type: what investors want vs. what we ship

Legend: ✅ have & export · 🟡 have but don't export (cheap win) · 🔴 don't capture (new work)

### PROBATE  (highest-converting niche; 8–12% vs 2–4% general)
The structural miss: **we scrape the county RECORDER, investors need the SUPERIOR-COURT DOCKET.**
- 🔴 **Personal Representative / executor name + mailing address** — the ONLY person who can sign; #1 must-have. Not in recorder data; needs court-docket scrape.
- 🔴 **Estate attorney name + phone** — many PRs route everything through counsel; documented referral channel.
- 🔴 **Probate case number / filing date** — verification + the freshness clock.
- 🟡 **Months-since-filing** (computed) and 🟡 **PR-out-of-state flag** (computed) — research calls these "essentially free and the highest-impact prioritization signals in the entire dataset."
- ✅ decedent name, property address, parcel. 🔴 equity, occupancy, # heirs.
- Competitors: All The Leads / US Probate Leads / PropertyRadar 5.0 / ProbateData all ship PR + attorney + case# + skip-traced phones + a distress score. PropStream/BatchLeads only carry a "deceased flag" (no court data) — same tier we're in.

### PRE-FORECLOSURE (NOD/NTS)  — biggest actionability gap
The structural miss: the **NTS document image carries trustee/auction/default data we don't parse.**
- 🔴 **Auction / trustee-sale date** — "the most critical date"; every urgency calc anchors here. WA NTS docs contain it by statute.
- 🔴 **Default amount / reinstatement amount, original loan amount + date, foreclosing lender, trustee name + TS#** — all on the recorded NTS; we capture none.
- 🔴 **Estimated equity / equity %** — "equity is king," 30% is the industry floor. Needs AVM + lien data.
- 🟡 **Days-to-auction** (computed once we have auction date) — powers Hot/Warm/Standard tiers.
- ✅ borrower name, property address, recording date, doc_type. 🟡 absentee flag.
- Competitors: PropertyRadar is the WA gold standard (trustee, TS#, opening bid, postponements, 15-min auction-day updates, equity). We currently ship ~3 of their ~20 NTS fields. **This is the type where we look weakest.**

### TAX-DELINQUENT  — closest to competitive (King/Snohomish)
- ✅ **amount owed, bill year** (exported + filterable) — already ahead of most.
- 🟡 **absentee flag** (mailing≠property) and 🟡 **WA 3-year foreclosure-eligibility flag** (`oldest_delinquent_year ≤ year-3`) — the latter is a unique WA signal no national tool has.
- 🟡 **months/years-delinquent** (computed from bill_year) and 🟡 **delinquency-to-value ratio** (needs assessed_value export).
- 🔴 free-and-clear flag, vacancy, scheduled auction date, payment-plan status.
- Investor bands: skip <$1.5k, sweet spot $3–25k; 2yr+ primary, 3yr+ (WA) hot. "free-and-clear + delinquent" is the gold stack.
- Competitors: PropStream/BatchLeads/PropertyRadar/DealMachine all ship absentee+equity+years bands. ListSource has NO tax-delinquent filter at all.

### CODE-VIOLATION  — we capture rich data and throw it away
The miss is **export, not capture** — it's all sitting in `enrichment_data`.
- 🟡 **violation type/category, description, status (open/closed), last-inspection** — ALL scraped into `enrichment_data`, NONE exported. Status=open is the core qualifier; type is the #1 signal-quality field.
- 🟡 **violation severity tier** (computed: condemned/vacant/fire/structural = Tier1 vs grass/trash = Tier3 noise) and 🟡 **open-violation count per parcel**.
- 🔴 fine/lien amount, condemned/placard flag, abatement/demolition order.
- ✅ property address, case date, parcel (enriched). 🔴 owner name (Seattle SDCI API has no owner — needs assessor join).
- Competitors: PropStream/BatchLeads treat code-violation as a binary flag (shallow). GoliathData goes deep but isn't WA-specific. **We could be the deepest WA code-violation source by just exporting what we already scrape + an assessor owner join.**

### DIVORCE  — fundamentally needs a build to be viable
The structural miss: **divorce filings have NO property address; the value-add IS the name→property match, which we don't do.**
- 🔴 **Matched property address** (cross-ref both spouse names → assessor ownership). PropertyRadar got 550 matches vs PropStream's 13 in one county test (42×). Without this, a divorce row is two names + a date = unworkable.
- 🔴 both party names (we get recorder grantor/grantee, not the court petition), case#, filing date, real-property-involvement flag.
- 🟡 absentee (one spouse moved out = strong signal), equity, stacked-distress (join to our own tax/code lists).
- WA = community-property: match on EITHER spouse name. 90-day cooling-off = every filing has ≥90-day window.
- Reality check: hardest of the 6 (two parties must agree, low property-match rate, high sensitivity). Our current 10 divorce results ≈ confirms it's not working as-is.

### DEATH-CERTIFICATE / PRE-PROBATE  — quietly our best-architected type
We're already ahead here: LandmarkWeb records death certs as instruments where **grantor=decedent, grantee=heir, legal=parcel** — the parcel match competitors pay 3 weeks to compute is embedded for free.
- ✅ decedent name, heir/grantee name, parcel, recording date, doc_type.
- 🟡 **feed `heirs` (grantee) into skip trace** the way we feed `party_name` — the heir is the actual decision-maker (workflow change, no schema).
- 🟡 **heir-out-of-state flag**, 🟡 equity/free-and-clear (≈60% of estate props are free-and-clear), 🟡 tax/code cross-ref join on parcel.
- 🔴 surviving-spouse/JTWROS filter (joint tenancy = no motivation, ~30–40% of mail wasted), probate-filed cross-reference, informant name/address (not on recorder doc).
- ⚖️ **Compliance note:** WA vital records are PRA-exempt + commercial-list-barred (RCW 70.58A.520/.540). We're on firm ground BECAUSE we use the recorder (public instruments), NOT vital statistics — keep it that way; rate-limit; never bulk-pull from WADOH.

---

## Cross-cutting opportunities (ranked, Codex-aligned)

### Tier 0 — cheap wins, data we already hold (ship first)
1. **`absentee_owner` flag** = `mailing_address` state/addr ≠ `property_address`. Single most-used investor filter, every type. Add `out_of_state_owner` too.
2. **Export `assessed_value`** (already in `enrichment_data`). "Table stakes."
3. **Export code-violation `status`/`description`/`type`/`last_inspection`** + derive `violation_severity_tier`. Turns our weakest-looking export into a category leader.
4. **Export tax `billed/paid/account_status`**; derive `months_delinquent`, `tax_balance_ratio` (needs #2), `wa_foreclosure_eligible` (3-yr).
5. **Computed scores:** `contactability_score` (phones/emails present), `freshness_days`, `stacked_distress_count` (same `property_key` across our own lead types — we already built the overlap machinery for Lists!).
6. **Feed `heirs` into skip trace** for death-cert leads.

### Tier 1 — new scraping/enrichment, high value (next)
7. **Parse NTS document images** → auction date, default/reinstatement amount, trustee, TS#, lender. Codex: "changes the product from 'interesting lead' to 'actionable lead'." Biggest single lift for pre-foreclosure.
8. **Probate superior-court docket scrape** → PR name+address, attorney, case#, filing date. What probate buyers expect; we're sourcing the wrong system today.
9. **Equity estimate** = assessed_value (or AVM) − open-lien/mortgage debt. Enables the #1 filter across probate/pre-foreclosure/tax/divorce. Higher effort (needs deed/lien scrape or an AVM provider).

### Tier 2 — harder / lower certainty
10. **Divorce name→property match** (makes divorce viable at all; medium-high effort, noisy).
11. Vacancy (USPS), per-type `lead_score`/`motivation_score`, probate-filed cross-ref for death leads, retain skip-trace age/relatives if Tracerfy contract permits.

---

## What we already do that's worth marketing
- **County-direct daily freshness** — competitors recycle 3–6-mo-old national data; pre-foreclosure/probate leads go stale in 24–48h. This is the product.
- **6-type WA bundle** incl. Seattle code-violations (under-served by national tools) + death-cert pre-probate (uncommon, earliest-in-funnel).
- **King/Snohomish amount-owed** on tax leads (most tools only flag delinquency, no dollar figure).
- **Parcel/GIS-normalized addresses** — cleaner than raw recorder exports.

---

## Open questions for the brainstorm
- Sequence: ship all Tier-0 computed/export fields as one "lead-quality" release, or per-type?
- Build order on Tier 1: NTS-image-parse (pre-foreclosure) vs probate-docket — which type's customers are loudest?
- Equity: build deed/lien scraping, or buy an AVM/lien feed (ATTOM/Estated)? Buy-vs-build.
- Divorce: invest in the property-match build, or deprioritize the type until the high-value types are maxed?
- Scoring: transparent weighted score (defensible, explainable) vs a black-box "BatchRank"-style number?
