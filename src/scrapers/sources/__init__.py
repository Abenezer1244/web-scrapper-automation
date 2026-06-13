"""External structured-data sources that enrich existing leads (not county scrapers).

Distinct from src/scrapers/enrichment/ (parcel/assessor lookups keyed on a single
lead): a `source` ingests a third-party feed (e.g. legal-newspaper foreclosure
notices) and matches it onto leads we already scraped. First source: the Tacoma
Daily Index Notice-of-Trustee-Sale parser (Pierce County pre-foreclosure auction
data — the auction date / default amount / trustee that the county recorder grid
never exposes).
"""
