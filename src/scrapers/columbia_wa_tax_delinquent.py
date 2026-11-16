"""Columbia County (WA) — Tax Delinquent leads from the per-parcel PACS tax cache.

Columbia's Treasurer publishes only the yearly tax-foreclosure minimum-bid list (2
parcels for the 2026 sale), not a delinquent roll. workers/pacs_tax_crawler walks
the county's 5,477 statewide-layer parcels (FIPS 013) through its PACS tax tables;
this reader is Asotin's, with Columbia's county and source: the same prior-year rule,
95% coverage floor and owner / house-numbered situs / mailing hold.
"""
from __future__ import annotations

from src.scrapers.asotin_wa_tax_delinquent import AsotinWATaxDelinquentScraper


class ColumbiaWATaxDelinquentScraper(AsotinWATaxDelinquentScraper):
    """Read Columbia's qualifying delinquent parcels out of pacs_tax_status."""

    COUNTY = "columbia"
    SOURCE = "columbia_pacs_tax"  # dedup._TRUSTED_TAX_SOURCES
