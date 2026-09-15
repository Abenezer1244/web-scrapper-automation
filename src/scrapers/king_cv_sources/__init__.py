"""King County code-violation sources: one adapter per jurisdiction behind one connector.

`KingWACodeViolationScraper` (src/scrapers/king_wa_code_violation.py) runs every adapter
listed in its SOURCES tuple and merges what they return. Each adapter subclasses
`base.CodeViolationSource` and owns its HTTP, retries, paging and structural canary.

This package root holds only the stable source keys (stored as enrichment_data.source), so
enrichment, skip-trace and plan-cap code can name them without importing the scrapers.

Adding a jurisdiction: write the adapter module, add its key here (and to
PARCEL_AT_SCRAPE_SOURCES when the source prints the King PIN), then append the adapter
class to SOURCES in king_wa_code_violation.py. The customer-facing scope note is built
from the registered adapters' jurisdiction labels, so it follows automatically.
"""

SEATTLE_SDCI = "seattle_sdci_code_violations"
BELLEVUE = "bellevue_code_enforcement"
BURIEN = "burien_code_enforcement"
KINGCO_ACCELA = "kingco_accela_code_enforcement"

# Sources whose case record carries the real 10-digit King County PIN, stored as
# results.parcel_id AT SCRAPE (before insert, so billing identity never changes later).
# Their owner is read from King eRealProperty for that PIN. SDCI is not one of them: its
# PIN is located from coordinates after insert and lives in enrichment_data.kc_pin.
# (King County Accela prints the PIN on the case detail page, read during the scrape.)
PARCEL_AT_SCRAPE_SOURCES = frozenset({BELLEVUE, BURIEN, KINGCO_ACCELA})
