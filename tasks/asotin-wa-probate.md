# Asotin County, WA — probate from Lewiston Tribune notices to creditors

Branch `feat/asotin-wa-probate-notices` (from main d7188fe2). Owner said "proceed" 2026-11-14.

## Why this source
The recorder index (Helion) is behind reCAPTCHA. RCW 11.40.020 requires every WA estate's
notice to creditors to be published in a county legal newspaper; Asotin's run in the
Lewiston Tribune, hosted as one PDF per advert on nwmarket.com — the SAME feed the daily
`crawl_nts_nwmarket_asotin` already reads end to end (every recent advert PDF, 20 s
crawl delay). Probe 2026-11-14 (30 adverts, 4 days): 2 Asotin probates
(cause 26-4-00122-02, 26-4-00117-02), plus Spokane (-32) and Idaho (CV29/CV35) ones to reject.

Notice fields (both live samples): cause no "NN-4-NNNNN-02", court "Superior Court of
Washington for Asotin County", decedent ("In re the Estate of: NAME, Deceased."),
"Date of First Publication", personal representative(s), "Address for Mailing or Service".

## Property match
Asotin PACS has NO owner-name search mode (Account Number / Address / DBA only) — not used,
and not worked around via DBA. The county's own public ArcGIS layer
`services6.arcgis.com/VsJGTl1jmCYBc0EX/.../Parcels/FeatureServer/0` (13,651 rows, Query
capability, linked from asotincountywa.gov/179/Parcel-Map "search for parcels by owner")
has NAME ("OZARD, COLLEEN J"), PARCEL_ID (17-digit), Prop_ID, BILLING1/2+CITY/ST/ZIP,
FULL_ADD+SITE_CITY. Live: decedent COLLEEN J. OZARD -> exactly one parcel; JEANETTE L.
CAMPBELL -> none (held). Estate owners also appear as "NAME (ESTATE)", "NAME ESTATE C/O ...".

## Plan
Phase 1 (pure, no DB) — parser + matcher
- [ ] `src/scrapers/sources/probate_nwmarket.py`: `parse_probate_notice(text)` -> decedent,
      cause_no, first_published, personal_reps, claims_address; `is_asotin_probate` (cause
      suffix -02 AND Asotin court phrase)
- [ ] `src/scrapers/enrichment/asotin_parcels.py`: owner-name -> parcels from the county
      layer; exact LAST, FIRST [MIDDLE-INITIAL] match (+ "(ESTATE)"/"ESTATE" and "& co-owner"
      forms); all parcels of that exact owner, never fuzzy; SSRF-validated host-pinned GET
- [ ] tests + fixtures (real notice text/PDF + real layer JSON, names redacted)
Phase 2 — storage + crawl
- [ ] migration 158 `probate_notices` (+RLS system-only, privacy erase coverage), model
- [ ] crawl hook: in the existing nwmarket advert loop, Asotin probate blocks -> upsert by
      cause_no (no new requests to nwmarket)
Phase 3 — scraper + connector
- [ ] `AsotinWAProbateScraper` reads probate_notices by first_published window, resolves
      parcels at scrape time; HOLDS notices with 0 matched parcels or missing owner /
      parcel / property address / mailing; one lead per matched parcel
- [ ] connector row INACTIVE (probate), registry allowlist, assessor_url
