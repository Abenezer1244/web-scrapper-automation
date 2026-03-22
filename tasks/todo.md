# BridgeLeads — Fix 19 Failed WA Counties

## Analysis

### Group 1: EagleWeb counties — JUST RERUN (9 counties)
These failed because they ran before the EagleWeb template fix was deployed.
The template now works (proven: Benton = 4,528 records).

Counties: Benton*, Grant, Grays Harbor, Island, Jefferson, Kitsap, Lincoln, Pacific, Stevens
*Benton already succeeded in a separate test run.

**Action:** Rerun all 9 via API. No code changes needed.

### Group 2: DNS/Unreachable (3 counties)
- Clallam: `erecording.clallam.net` — DNS failure
- Douglas: `edocs.douglascountywa.net` — DNS failure
- Okanogan: `selfservice.co.okanogan.wa.us` — DNS failure

**Action:** Find correct/updated URLs for these counties, or mark as temporarily down.

### Group 3: AcclaimWeb (2 counties)
- Chelan: `acclaim.co.chelan.wa.us/acclaimweb` — reachable
- Pend Oreille: `aptitudeweb.pendoreille.org/AcclaimWeb` — reachable

AcclaimWeb is Tyler Technologies (like EagleWeb) but different UI.
Need to study the interface and either build a template or fix the AI scraper for it.

**Action:** Navigate to AcclaimWeb sites, study form structure, build template or tune AI.

### Group 4: Custom portals (3 counties)
- Columbia: `idocmarket.com` — third-party service
- San Juan: `apps.sanjuancountywa.gov/Auditor/DigitalResearchRoom` — custom
- Whatcom: `recording.whatcomcounty.us/Disclaimer` — custom "Digital Research Room"

**Action:** AI scraper should handle these. Debug why AI scraper failed on each.

### Group 5: Other platforms (2 counties)
- Yakima: `tapestry.fidlar.com/Tapestry2/` — Fidlar Tapestry platform
- Snohomish: `snoco.org/RecordedDocuments/` — LandmarkWeb, requires account creation

**Action:**
- Yakima: Study Fidlar Tapestry, build template or fix AI
- Snohomish: Requires account — mark as needing manual setup or automate registration

## Plan

### Step 1: Rerun EagleWeb counties (9 counties) — immediate
- [x] Rerun Grant, Grays Harbor, Island, Pacific, Thurston, Clark, Lewis, Whitman, Okanogan
- [x] Spokane: DONE (5,653 records), Kitsap: DONE (5,076), Benton: DONE (4,528), Jefferson: DONE (1,355)
- [ ] Monitor results — Grant + Island currently scraping, 7 more pending
- [ ] Expected: ~1000-5000 records per county
- Note: Lincoln + Stevens are Tyler Self-Service (not EagleWeb) — need separate template

### Step 2: Fix DNS/unreachable (3 counties)
- [x] Research correct URLs for Clallam, Douglas, Okanogan
- [x] Clallam: erecording.clallamcountywa.gov/recorder/web/ (was .net → .gov)
- [x] Douglas: edocs.douglascountywa.gov/AcclaimWeb (was .net → .gov)
- [x] Okanogan: okanogancountywa-web.tylerhost.net/Web (migrated to Tyler hosting)
- [x] Update DB with working URLs
- [x] Dispatch jobs for Clallam and Douglas (Okanogan already in queue)
- [ ] Monitor results — Clallam (EagleWeb), Douglas (AcclaimWeb/AI), Okanogan (EagleWeb)
- [x] Cancelled 4 stale Pierce jobs blocking queue

### Step 3: Fix AcclaimWeb (3 counties: Chelan, Douglas, Pend Oreille)
- [x] Research AcclaimWeb interface (Kendo UI, DatePicker, Grid)
- [x] Build AcclaimWeb template scraper (src/scrapers/templates/acclaimweb.py)
- [x] Register in registry.py (auto-detects /acclaimweb in URL)
- [x] Updated __init__.py exports
- [ ] Deploy to Railway (commit + push)
- [ ] Trigger jobs for Chelan and Pend Oreille (Douglas already in queue)

### Step 4: Fix custom portals (3 counties)
- [ ] Debug AI scraper on Columbia, San Juan, Whatcom
- [ ] Check if AI scraper needs more wait time or different navigation
- [ ] Rerun

### Step 5: Fix other platforms (2 counties)
- [ ] Study Yakima Fidlar Tapestry
- [ ] Handle Snohomish account requirement
- [ ] Rerun

## Build Order
```
1. Rerun 9 EagleWeb counties          (Step 1 — immediate, no code)
2. Fix 3 DNS counties                 (Step 2 — URL research)
3. Fix 2 AcclaimWeb counties          (Step 3 — may need template)
4. Fix 3 custom portal counties       (Step 4 — AI scraper debug)
5. Fix 2 other platform counties      (Step 5 — platform-specific)
```
