# Original Request Fixes — Plan

## Context
The ARMS results table has these columns:
- **#** (row number)
- **Instrument #** / Book-Page
- **Date Recorded** (e.g. 01/02/2026)
- **Document Type** (always PROBATE)
- **Name** (recording party, prefixed with [R]) + **Associated Name** (heir/executor, prefixed with [E]) — in the SAME cell, separated by line breaks
- **Legal Description** (e.g. "FIRWOOD LANE LT 47 (+)") — sometimes contains embedded parcel IDs
- **Status**

Total: 80 records across 4 pages (25 per page)

---

## Fix 1: Heirs/Associated Names Extraction
**Problem:** The Name column contains BOTH the estate name ([R]) and heir/associated name ([E]) in a single cell. Current parser takes the "longest text" which mashes them together.

**Fix:** Parse the Name cell by splitting on [R] and [E] prefixes:
- `[R]` line → `party_name` (the estate or recording party)
- `[E]` line → `heirs` (the heir, executor, or associated name)
- Strip role prefixes like "EST OF", "EXEC", "PER REP", "HEIRS OF" into a separate `role` field

**Files:** `src/scrapers/pierce_wa_probate.py` → `_map_row()`

---

## Fix 2: Legal Descriptions
**Problem:** Legal descriptions ARE present on the results page but the heuristic parser often misses them because the column isn't labeled "legal_description" in the header.

**Fix:** The table has a dedicated "Legal Description" column (column index 7, 0-indexed). Map it by header name instead of heuristic keyword matching. Also extract embedded parcel IDs from this field (e.g. "5000190130" in "SHORT PLAT LT 2 8911300193 (+)").

**Files:** `src/scrapers/pierce_wa_probate.py` → `_extract_records()`, `_map_row()`

---

## Fix 3: Pagination
**Problem:** Only scrapes page 1 (25 records). The Next button exists but `_go_to_next_page()` doesn't find it.

**Fix:** The pagination uses ASP.NET buttons: "Next" (ref=e74), "Last" (ref=e75). The current selectors look for `a:has-text('Next')` but the actual element is `button "Next"`. Fix the selector to match `button:has-text('Next')`.

**Files:** `src/scrapers/pierce_wa_probate.py` → `_go_to_next_page()`

---

## Fix 4: ATIP Enrichment Alternative
**Problem:** ATIP REST API returns HTML (Angular SPA with reCAPTCHA). No direct API access.

**Fix options:**
- **Option A:** Use Pierce County Assessor-Treasurer's property search (different site, may not have CAPTCHA)
- **Option B:** Use the parcel ID embedded in ARMS legal description field + a free property data API
- **Option C:** Use the Pierce County GIS/parcel viewer API which may expose property data without CAPTCHA

Test all three options to find one that works.

**Files:** `src/scrapers/enrichment/parcel.py`

---

## Fix 5: Grouped CSV Export
**Problem:** Records export as flat rows. User wants them grouped by estate/case.

**Fix:** Group records by instrument number prefix or by party_name (estate name). Add a "group" column that identifies related records. Sort by group → date within the CSV.

**Files:** `src/utils/data_exporter.py` → `_build_dataframe()`

---

## Build Order
```
1. Fix 2: Legal descriptions (column mapping) — simplest, foundation for others
2. Fix 1: Heirs extraction ([R]/[E] parsing) — improves data quality
3. Fix 3: Pagination (button selector) — gets all 80 records
4. Fix 5: Grouped CSV — export formatting
5. Fix 4: ATIP enrichment alternative — research + implement
```
