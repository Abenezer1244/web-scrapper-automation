"""Ferry County (WA) — Tax Delinquent leads from the per-parcel tax cache.

Ferry's Treasurer announces its annual tax-foreclosure sale but posts no parcel list
(2026: sale 2026-11-24, no list on the county site). Every parcel's TaxSifter
Treasurer page shows its 5-year statements and Balance Due, so workers/pacs_tax_crawler
walks Ferry's inventory into ``pacs_tax_status`` through taxsifter.resolve_tax and this
scraper only reads that table.

Everything else is Asotin's contract (scrapers/asotin_wa_tax_delinquent): prior-year
balances only, the 95%-fresh coverage gate (never a false zero), and parcels missing
an owner, property address or mailing address held, not delivered. The amount is
TaxSifter's Balance Due (taxes + assessments + fees), which excludes accrued interest.

Differences from Asotin (Codex Review C):
* TaxSifter stamps its data date ("Data current as of"), which can lag the fetch; the
  crawler settles an answer only when that date is within 14 days
  (pacs_tax_crawler._AS_OF_SLACK_BY_COUNTY), so a frozen source goes stale, never fresh.
* FAIL_ON_HELD: most Ferry parcels are rural with no situs address (7 of 12 sampled).
  Until the owner decides how such land is delivered, a run that would hold any
  qualifying parcel fails with the count instead of returning a shorter list.
* party_name is TaxSifter's "Owner Name" (the assessor roll's owner block), not the
  Treasurer grid's Taxpayer column; it is not independently verified as title holder.
"""
from __future__ import annotations

from src.scrapers.asotin_wa_tax_delinquent import AsotinWATaxDelinquentScraper

RECORD_TYPE = "tax_delinquent"
SOURCE = "ferry_taxsifter_tax"  # dedup._TRUSTED_TAX_SOURCES


class FerryWATaxDelinquentScraper(AsotinWATaxDelinquentScraper):
    """Read Ferry's qualifying delinquent parcels out of pacs_tax_status."""

    COUNTY = "ferry"
    SOURCE = SOURCE
    FAIL_ON_HELD = True
