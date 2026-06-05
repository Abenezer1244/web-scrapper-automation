# Non-King Tax Data Spike — Pierce / Snohomish / Kitsap (2026-06-05)

**Question (Phase 4 deferred unknown, spec line 187):** do Pierce/Snohomish/Kitsap
treasurer sources expose **amount owed** + **delinquency age** for tax-delinquent
properties, accessibly enough to power the Phase 4 amount/months filters beyond King?

**Method:** web research of each county treasurer's published data. No code built.

## Findings

| County | Amount owed? | Delinquency age? | Access | Verdict |
|---|---|---|---|---|
| **King** (live) | ✅ structured | ✅ bill_year | Socrata open-data API | Baseline — clean API, already shipped. |
| **Snohomish** | ✅ "Current Tax List" (all parcels, taxes due) | ✅ 1/2/3-year delinquent parcel lists (age buckets) | Downloadable lists (treasurer public records) | **BEST non-King candidate** — structured amount + age, no login. Worth a build. |
| **Pierce** | ⚠️ per-parcel only (ATIP portal); "Tax Warrants" = names+case# (no amount) | Derivable per-parcel | ATIP portal per-parcel lookup; no bulk amount+age feed | **PARTIAL** — amount obtainable but only via per-parcel scrape (expensive); no clean bulk delinquent+amount list. |
| **Kitsap** | ❌ foreclosure/tax-title/surplus only | Foreclosure-stage only | PDF docs (surplus list, tax-title list) | **WEAK** — only foreclosure-stage PDFs, not general delinquency-with-amount. |

**No public API** for any of the three; data is downloadable docs / portals / web pages
(vs King's Socrata API).

## Recommendation
- The spec's risk ("non-King data may not exist cleanly") is **confirmed**: King is the
  only clean API. 
- **If extending tax filters beyond King, do Snohomish next** — it publishes structured
  amount (Current Tax List) + age (1/2/3-year delinquent lists), no login. A scraper would
  parse those lists into `enrichment_data.delinquent_amount` + a derived bill-year/age, then
  the existing Phase 4 columns + filters work unchanged (gating is data-driven).
- **Pierce**: only worth it if per-parcel ATIP lookups are acceptable (slow/expensive); no
  bulk feed. Defer.
- **Kitsap**: not viable for amount/age filtering at the general-delinquency stage; only
  foreclosure-stage data exists. Defer.
- Backend already gates the tax filters on the presence of structured data, so adding
  Snohomish later requires **no API/UI change** — just a scraper that populates the columns.

## Sources
- Pierce ATIP portal: https://atip.piercecountywa.gov/
- Pierce Tax Bills & Payments: https://www.piercecountywa.gov/748/Tax-Bills-Payments
- Snohomish Treasurer Public Records: https://www.snohomishcountywa.gov/5568/Treasurer-Public-Records
- Snohomish Requests For Foreclosure Information (1/2/3-yr delinquent lists): https://snohomishcountywa.gov/3399/Requests-For-Foreclosure-Information
- Kitsap Tax Foreclosure: https://www.kitsap.gov/treasurer/Pages/Foreclosure.aspx
