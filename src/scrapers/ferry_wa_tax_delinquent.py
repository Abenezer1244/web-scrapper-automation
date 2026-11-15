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
"""
from __future__ import annotations

from src.scrapers.asotin_wa_tax_delinquent import AsotinWATaxDelinquentScraper

RECORD_TYPE = "tax_delinquent"
SOURCE = "ferry_taxsifter_tax"  # dedup._TRUSTED_TAX_SOURCES


class FerryWATaxDelinquentScraper(AsotinWATaxDelinquentScraper):
    """Read Ferry's qualifying delinquent parcels out of pacs_tax_status."""

    COUNTY = "ferry"
    SOURCE = SOURCE
