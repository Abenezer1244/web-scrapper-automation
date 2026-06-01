# Plan: SSRF scraper cluster (HIGH-3, HIGH-4, HIGH-5) + scraper-side DNS resolution

**Date:** 2026-06-01
**Driver:** `docs/security/REVIEW-2026-06-01.md` HIGH-3/4/5 + the deferred scraper-side
half of HIGH-1 (DNS rebinding). User approved bundling scraper-side `resolve=True` here.
**Rule:** brainstorm → Codex partner → implement → verify (don't break live scrapes) →
Codex review. Max 5 files per sub-phase.

## Verified site map
- **HIGH-5 (8 `page.goto` bypasses):** eagleweb:156, king_wa_probate:444 (reCAPTCHA last-resort),
  king_county_assessor:109, whatcom_wa:137, acclaimweb:136+311, ava_fidlar:329, landmarkweb:170
- **HIGH-3:** eagleweb:750 `_requests.get(detail_href, cookies=...)` — scraped URL + cookie forwarding
- **HIGH-4:** county_gis:161/201/431 `requests.get(endpoint)` (endpoint = DB `gis_endpoint`);
  validate `gis_endpoint` at connector creation (scrapers route + schema). (236/513 use a
  constant `_WA_STATEWIDE_ENDPOINT` — safe.)
- **Out of scope (constant URLs):** king_wa_code_violation:67, king_wa_tax_delinquent:66,
  pierce_wa_code_violation:75, national:57 — fixed third-party endpoints, not DB/scraped.

## Sub-phases (each ≤5 files, verify + Codex review between)
- [ ] **2a — Foundation:** `base_scraper.py` — `resolve=True` on `navigate()` + `probe()`
  (+ `probe` no-redirect + Location recheck); add `safe_goto(url, wait_until, timeout_ms)`
  (validate resolve=True → goto → redirect recheck) and `safe_get(url, *, same_host_as=None,
  cookies, headers, timeout)` (validate resolve=True → no-redirect → optional host match).
  Tests. **Foundation everything else reuses.**
- [ ] **2b — HIGH-5 (eagleweb/king group) + HIGH-3:** eagleweb (goto:156 → safe_goto;
  detail-href:750 → safe_get(same_host_as=base_url)), king_wa_probate (guard goto:444),
  king_county_assessor (goto:109), whatcom_wa (goto:137). [4 files]
- [ ] **2c — HIGH-5 remainder:** acclaimweb (136,311), ava_fidlar (329), landmarkweb (170). [3 files]
- [ ] **2d — HIGH-4:** county_gis (validate endpoint via safe_get), scrapers route
  (validate `gis_endpoint` at creation like `base_url`), schemas (structural). [3 files]

## Verification
- Each sub-phase: targeted tests + `ruff` + Codex review of the diff.
- Confirm `resolve=True` doesn't break a real scrape (run one live county scrape after 2a/2b).
- Two clean Codex passes before marking the cluster done in REVIEW-2026-06-01.md.

## Review
_(filled in after execution)_
