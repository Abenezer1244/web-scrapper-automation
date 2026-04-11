# Template Architecture Cleanup — 2026-04-11 session 3

**Date:** 2026-04-11
**Goals:**
1. Fix the EagleWeb `record_type` filter so users picking "probate" get probate-only, not a probate+pre_foreclosure mix
2. Stop the `tylerhost.net` → EagleWebScraper misrouting that times out okanogan
3. Add a Tyler SelfService template to unlock 4 counties (grant, okanogan, lincoln, stevens)
4. Document clallam as confirmed-empty (already in memory — no code change)

---

## Phase A — EagleWeb `record_type` propagation fix ✅ SHIPPED (d5a3660)

**Scope:** 1 file. Affects all 12 EagleWeb counties.

**Problem:** `EagleWebScraper.__init__` takes `record_types: list` and the extraction filter at `eagleweb.py:644` OR-combines keywords across ALL types in the list. Users picking "probate" on a connector configured with `['probate','pre_foreclosure']` get BOTH types mixed in their output.

**Root cause:** The registry binds `record_types=connector.record_types` (the full supported list) via `partial()`, then `scrape()` uses `self.record_types[0]` for the search config and filters against the full list. There is no "active type" concept.

**Fix approach:**
- Add `record_type: str | None = None` to `EagleWebScraper.__init__`
- Store `self.active_record_type = record_type or (self.record_types[0] if self.record_types else None)`
- Change `scrape()` line 150 to use `self.active_record_type` instead of `self.record_types[0]`
- Change the filter at line 644 to iterate `_DOC_TYPE_MAP.get(self.active_record_type, [])` only (single type, not the full list)
- `tasks.py:_run_scraper` already inspects the signature and forwards `record_type` if present (`tasks.py:564-576`). No change needed there — `inspect.signature()` of a `functools.partial` omits already-bound kwargs, so `record_type` will be visible.

**Verification (all passed):**
- [x] Probe pacific with record_type="probate" → 16 records (was 18, 2 pre_foreclosure excluded)
- [x] Probe pacific with record_type="pre_foreclosure" → 2 records (was 18, now only real trustee sales)
- [x] 16 + 2 = 18 confirms clean split, zero overlap
- [x] Probe whitman with record_type="probate" → 3 records (30-day, expected low volume)
- [x] Smoke test thurston probate → 30 records (no regression on known-good county)

**Files touched:** `src/scrapers/templates/eagleweb.py` only (tasks.py already supports this via inspect.signature forwarding).

---

## Phase B — Tyler SelfService template (new) ✅ SHIPPED (dff01d2)

**Scope:** 2 files (new template + registry routing). **1 of 4 target counties unlocked.**

**Outcome per county:**

| County | Route | Result |
|---|---|---|
| okanogan | TylerSelfServiceScraper | **✅ 17 probate + 2 pre_foreclosure (30d)** — real data (BENSON/EIFFERT estates, Western Progressive trustee). Previously timed out at 240s. Stays at `degraded` until detail-page parcel fetch is solved. |
| grant | EagleWebScraper (correct — `/grantrecorder/web/` path) | Extracts 11 raw records but 0 parcels. Separate EagleWeb-side issue, not Tyler SelfService. |
| lincoln | TylerSelfServiceScraper | Home page has no "Official Records Search" link. Different product tier or login-gated. |
| stevens | TylerSelfServiceScraper | Same as lincoln. |

**Key findings during recon (16+ passes):**
- Tyler SelfService disclaimer button has `disabled=""` on load, with a JS handler that is SUPPOSED to enable it via `$('#submitDisclaimerAccept').prop('disabled', false)` but the trigger doesn't fire in headless Playwright. Workaround: force-enable via `el.disabled = false; el.removeAttribute('disabled')` and click.
- Search flow: home → "Official Records Search" → `/Web/action/ACTIONGROUP{N}S1` → "Document Type Search" → `/Web/search/DOCSEARCH{N}S3`. `{N}` varies per install (okanogan: 769).
- Form fields: `#field_RecDateID_DOT_StartDate`, `#field_RecDateID_DOT_EndDate`, `#field_selfservice_documentTypes` (autocomplete, left empty), `#field_UseAdvancedSearch` (checkbox, left unchecked).
- Submit button is `<a id="searchButton">`, NOT a `<button>`.
- Search is AJAX: `POST /Web/searchPost/DOCSEARCH{N}S3` then `GET /Web/searchResults/...?page=N`. URL doesn't change.
- Results render into `<li class="ss-search-row" id="searchRowDOC{...}" data-documentid="DOC{...}" data-href="/Web/document/{...}?search=...">`. 100 rows/page.
- Pagination via `<a>next</a>` link — not visible on last page.
- Detail pages require server-side session state we can't replicate by direct `page.goto()` — returns "An error has occurred" with a support GUID. **Parcel enrichment blocked on this.**

**Files touched:**
- `src/scrapers/registry.py` — reorder _detect_template to check EagleWeb's `recorder/web` path before the tylerhost.net subdomain check, add SelfService detection for `tylerhost.net` + `selfservice.` subdomain
- `src/scrapers/templates/tyler_selfservice.py` — new 300-line template

**Problem:** `_detect_template` at `src/scrapers/registry.py:159-169` routes any `tylerhost.net` URL to EagleWebScraper. But Tyler SelfService (also on tylerhost.net) is a completely different platform — its "I Accept" button stays `disabled` until a JS timer elapses, so EagleWeb's click strategy times out. Affects grant, okanogan, lincoln, stevens.

**Two-part fix:**

### B.1 — Reconnaissance (MUST DO FIRST)
Before writing any template code, visit the portals headfully and capture the DOM. Tyler SelfService has a different disclaimer, different search form, different results structure. I'll run a Playwright recon script that:
1. Navigates to the okanogan Tyler SelfService URL
2. Waits until the disclaimer button enables (poll for `[disabled]` to go away)
3. Clicks it, captures the next page URL + form fields
4. Dumps all input IDs, button text, and any results-table structure

### B.2 — Template implementation
Based on recon findings, write `src/scrapers/templates/tyler_selfservice.py` modelled after `eagleweb.py` with:
- Disclaimer: wait for enabled + click (NOT immediate click)
- Search form: use whatever IDs recon revealed
- Results: use whatever table/list structure recon revealed
- Date chunking: reuse the 7-day chunking pattern

### B.3 — Registry wiring
- Update `_detect_template` to match `tylerhost.net` → TylerSelfServiceScraper
- Ensure the existing `selfservice_patterns` check still catches `/web/user/disclaimer`, `/web/search/`, `selfservice.` URLs (or remove that dead branch since it falls through to AI)

**Verification:**
- [ ] Okanogan 30-day probate probe → records > 0 with parcels
- [ ] Grant 30-day probate probe → records > 0 with parcels
- [ ] Lincoln 30-day probate probe → records > 0 with parcels
- [ ] Stevens 30-day probate probe → records > 0 with parcels
- [ ] If any of the 4 fail: document root cause, leave degraded, ship the ones that work

**Files touched:** `src/scrapers/templates/tyler_selfservice.py` (new), `src/scrapers/registry.py`

**Risk:** HIGH. Writing a new scraper template for an unfamiliar portal can fail in unexpected ways — disclaimer variants, session cookies, results rendering. I'll recon before writing code so the template is grounded in real HTML, not guesses.

---

## Phase C — Clallam + docs wrap-up ✅ COMPLETE

**Scope:** DB update, memory. No code.

- [x] Already updated memory `project_wa_county_matrix.md` with clallam=confirmed empty (session 2)
- [ ] If Phase B unlocks okanogan/grant/lincoln/stevens, promote each to `health_status=healthy` in DB
- [ ] Update `tasks/progress.md` WORKING list
- [ ] Update memory matrix
- [ ] Commit + push + verify on live API

---

## Execution order

1. **Phase A** (EagleWeb record_type fix) — well-defined, low risk, high impact on existing counties
2. **Phase B.1** (Tyler recon) — understand the portal before writing code
3. **Phase B.2** (template) — write based on recon
4. **Phase B.3** (registry wire-up)
5. **Phase C** (promote + docs)

Each phase commits separately so Phase A can ship even if Phase B fails.

## Out of scope

- Reviving the 12 hidden-down counties (landing-page URL fixes) — different effort, different session
- Mason's EagleWeb DOM mismatch — one county, separate fix
- Chelan's single-date picker — AcclaimWeb template fix, separate session
