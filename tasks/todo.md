# Real owner-location data (audit items 3 + 4) — 2026-09-02

Branch: `feat/real-owner-location` · worktree `bridgeleads-worktrees/real-owner-location` off `origin/main` (`0bb74bc`)

User decision: "it should not assume the owner lives [at the property]… real data everywhere."
Option B on both items (Codex-recommended).

## Phase 1 — item 3: stop writing assumed mailing addresses (≤5 files)
- [ ] `county_gis.py`: statewide (single + batch) returns `mailing_address=None`; drop `_statewide_mailing`; generic-config `_parse_gis_response` fallback → None
- [ ] `ai_assessor.py`, `national.py`, `parcel.py` (ATIP): no situs-as-mailing fallback
- [ ] tests updated/added; Codex review
## Phase 2 — item 3 backfill (prod)
- [ ] NULL provably-assumed mailing rows (no real mailing source for that county/record_type) + recompute flags; evidence file; Codex review; run
## Phase 3 — item 4 schema
- [ ] migration 085: `results.property_city`, `results.property_zip`; model; `compute_owner_flags` accepts structured situs parts
## Phase 4 — item 4 fill at scrape/enrich time
- [ ] capture the scraper's full situs (notice "commonly known as") before GIS overwrites the street; statewide/Pierce/King situs city+zip where the SOURCE has them; insert + end-of-job flags use the parts
## Phase 5 — item 4 backfill (prod)
- [ ] fill city/zip for existing leads from real sources; recompute flags; Codex review; run

## Review
(pending)
