"""Built-in county assessor URLs, for when a connector row's assessor_url is NULL.

Moved out of the deleted Claude-based assessor fallback (AI mode removal,
Phase 3); the PACS enrichment in tasks_helpers/enrich.py reads it.
"""

KNOWN_ASSESSOR_URLS: dict[str, str] = {
    "pierce_WA": "https://atip.piercecountywa.gov/app/parcelSearch",
    # Tyler PACS PropertyAccess (same vendor as Chelan/Douglas AcclaimWeb PACS)
    "island_WA": "https://assessor.islandcountywa.gov/propertyaccess/?cid=0",
}
