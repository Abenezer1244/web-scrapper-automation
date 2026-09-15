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

# Cases the jurisdiction settled as not needing anything from the owner: SDCI's completed
# complaints and duplicates of another complaint (owner decision 2026-09-13), and King
# County Accela cases voided or closed with no violation. They stay delivered, rank last
# under the plan cap, and never get a paid skip trace. Exact status values, scoped by
# source: "Closed" (SDCI, Bellevue) and Burien's "CLOSED" stay ordinary cases, as SDCI's
# "Closed" always has, and an unknown status is never treated as settled.
SETTLED_STATUSES: dict[str, frozenset[str]] = {
    SEATTLE_SDCI: frozenset({"Completed", "Open Duplicate"}),
    KINGCO_ACCELA: frozenset({"Void", "No Violation Found", "Case Opened No Violation Ltr",
                              "No Further Action Required"}),
}


def is_settled(source: object, status: object) -> bool:
    return isinstance(status, str) and status in SETTLED_STATUSES.get(source, frozenset())


def settled_sql(ed: str) -> str:
    """SQL twin of is_settled over the enrichment_data expression ``ed`` (a json column).

    Splices only the module constants above, which contain no quote characters.
    """
    clauses = []
    for source, statuses in sorted(SETTLED_STATUSES.items()):
        values = sorted(statuses)
        if "'" in source or any("'" in v for v in values):
            raise ValueError(f"settled status for {source} cannot be spliced into SQL")
        listed = ", ".join(f"'{v}'" for v in values)
        clauses.append(f"({ed}->>'source' = '{source}' AND {ed}->>'status' IN ({listed}))")
    return "(" + " OR ".join(clauses) + ")"
