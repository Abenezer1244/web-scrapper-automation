# Washington State Court Records — Eviction & Divorce Scraping

## Summary

Bulk-scraping eviction (Unlawful Detainer) and divorce (Dissolution)
records from Washington state court systems is **not possible via
legitimate public means**. Both of the state's two court portals
gate bulk queries behind barriers that cannot be worked around
without either a paid vendor contract or a violation of the
portal's terms of use.

## Investigation

### Path 1: Pierce County LINX

**URL:** `https://linxonline.co.pierce.wa.us/linxweb/Main.cfm`
**Platform:** ColdFusion / CFML
**Blocker:** Per-name search only.

The LINX search form (`Search.cfm`) requires either a
`litigant_name` or `cause_num` to return results. A date range
(`start_year`/`end_year`) + case type (`cause_type=2` for civil) is
not sufficient — the form rejects empty name submissions and
returns back to the search page. LINX is designed for per-case
lookup, not bulk extraction.

Attempted workaround: wildcard `a` as litigant name. Form bounces
back to search (silent reject). No SQL-style wildcard support.

### Path 2: Odyssey Portal (everyone except Pierce + King)

**URL:** `https://odysseyportal.courts.wa.gov/odyportal`
**Platform:** Tyler Technologies Odyssey
**Blocker:** Smart Search requires **registration / sign-in**.

The dashboard landing page shows `Register / Sign In` as the first
action. Unauthenticated Smart Search returns 401/redirect. The
"Party Name Search" documentation explicitly describes Odyssey as
a party-based lookup system, not a case-type enumeration.

Creating an account does NOT grant bulk API access — it grants
interactive UI access. Registered users still perform per-name or
per-case-number lookups. There is no "all civil cases filed this
week" endpoint.

### Path 3: King County DJA

**URL:** `https://dja-prd-ecexap1.kingcounty.gov`

We already use DJA for **probate** via the Department of Judicial
Administration's death certificate search path. The DJA system
does support case-type-based enumeration for certain record
types (that's how our probate scraper works) but **not for
Unlawful Detainer or Dissolution**. Those require individual
case-number or party-name lookups.

## Paid alternatives (not pursued in the current scope)

| Provider | Product | Pricing model | Notes |
|---|---|---|---|
| Tyler Technologies | JIS Link | Subscription + per-query fees | Requires court-approved business purpose; application process |
| Tyler Technologies | re:Search WA | Subscription | Attorney-focused case management, not bulk data export |
| CourtListener (Free Law Project) | Bulk RECAP | Free for federal, WA state not covered |
| Data broker | e.g. PropStream, BatchLeads, Black Knight | Per-lead fees | Opaque sourcing; expensive per-lead |

## Decision

**BridgeLeads will not attempt to scrape Unlawful Detainer or
Dissolution records from the public WA court portals.** The
combination of (a) per-name search gating on LINX, (b) registration
gating on Odyssey, and (c) the fact that both portals are
explicitly designed as per-case lookup tools means any attempt at
bulk extraction would:

1. Require either wildcard iteration (generating abusive traffic
   patterns that would justifiably be rate-limited or banned), or
2. Operating a registered account across thousands of automated
   lookups (likely a terms-of-use violation), or
3. Reverse-engineering the internal JIS Link API (contract
   violation if we had one; unauthorized access if we don't).

None of these are acceptable for a production SaaS.

## What this means for the product

Eviction and divorce coverage for Washington will be either:
- **Deferred until** BridgeLeads is large enough to negotiate a
  JIS Link or equivalent vendor contract, at which point we can
  ingest via their sanctioned bulk feed.
- **Partially supplied** via recorder-office proxies: some divorce
  decrees are separately recorded with the county auditor
  (see Pierce ARMS `_87` "Decree of Dissolution") and appear in
  our existing probate/divorce scrapers as a filtered subset.
  This gives us a partial view but misses any divorce that was
  filed but not yet recorded.
- **Marketed honestly** to customers: the county picker on
  `/scrapers/new` should list eviction and divorce as "coming
  soon" for non-recorder counties rather than giving the
  impression that full coverage exists.

## Related compliance notes

- `docs/compliance/wa-tax-delinquent.md` — RCW 42.56.070(8)
  blocks tax_delinquent for most WA counties
- `docs/compliance/connector-audit-2026-04-10.md` — Sprint 6.3
  health audit of the county connector fleet
