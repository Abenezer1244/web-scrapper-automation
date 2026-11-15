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
has NAME ("LAST, FIRST M"), PARCEL_ID (17-digit), Prop_ID, BILLING1/2+CITY/ST/ZIP,
FULL_ADD+SITE_CITY. Live: decedent in 26-4-00122-02 -> exactly one parcel; decedent in
26-4-00117-02 -> none (held). Estate owners also appear as "NAME (ESTATE)", "NAME ESTATE C/O ...".

## Plan
Phase 1 (pure, no DB) — parser + matcher
- [x] `src/scrapers/sources/probate_nwmarket.py`: `parse_probate_notice(text)` -> decedent,
      cause_no, first_published, personal_reps, claims_address; `is_asotin_probate` (cause
      suffix -02 AND Asotin court phrase)
- [x] `src/scrapers/enrichment/asotin_parcels.py`: owner-name -> parcels from the county
      layer; exact LAST, FIRST [MIDDLE-INITIAL] match (+ "(ESTATE)"/"ESTATE" and "& co-owner"
      forms); all parcels of that exact owner, never fuzzy; SSRF-validated host-pinned GET
- [x] tests + fixtures (real notice text + real layer JSON, names redacted) — 41 pass, ruff clean
Owner decision 2026-11-14: "Name + family tie" (over Codex's stricter rule): exact name AND one tie
(owner segment estate-marked / rep named on parcel / rep shares decedent's whole surname / billing = claims
address in full). Codex consult: never set parcel from name alone; hold everything else.
Phase 1 Codex review: 4 High + 4 Medium, then 2 High + 1 Medium, then 3 partial -> all fixed, final GO (3b992f0e).
Live: 26-4-00122-02 -> 1 lead (rep_shares_surname); 26-4-00117-02 -> exact match, held no_family_tie.
Phase 2 must also (Codex consult): parse probate on whole advert text BEFORE the NTS county filter; version
the Redis seen-key so already-seen adverts are replayed once; mark seen only after both pipelines commit.

Phase 2 — storage + crawl (DONE 2026-11-14)
- [x] 2a migration 160 (158-159 held by feat/chelan-nts-ocr) `probate_notices` + RLS + CHECK
      (withheld = nameless) + `privacy_erase_probate`; model ProbateNoticeRow; erase.py store
      "probate" (subject via tied parcels, decedent as party). Codex: 2 Medium + 1 Low fixed.
- [x] 2b `workers/probate_ingest.py` + crawl hook: probate on whole advert before NTS filter,
      match before write, notice-level privacy gate -> tombstone, outage never replaces an
      answer, 30-day re-check (stale-safe, backoff, deadline), 180-day retention run first,
      seen-key v2 replay, failed lookup not memoised. Codex: 3 High + 3 Medium fixed -> GO.
- Live (local DB, rolled back): 26-4-00122-02 matched (1 parcel), 26-4-00117-02 held.
Phase 3 — scraper + connector
- [x] `AsotinWAProbateScraper` (src/scrapers/asotin_wa_probate.py) reads matched
      probate_notices by first_published window (parcels were tied at crawl time);
      one lead per tied parcel; parcels missing parcel / owner / address / mailing HELD
- [x] connector row INACTIVE (migration 161, probate, assessor_url), registry allowlist
- [x] Codex review: NO-GO (2 High, 2 Medium) -> fixed -> GO; last Medium closed.
      Fixes: no owner-of-record copy in enrichment (C/O string can name the PR); empty
      run fails while in-window notices are 'failed' (layer pending); windows starting
      before the 180-day retention fail; street-only property_address (billing key
      parcel|address must not drift when a city appears).
- Owner note (not changed, app-wide): enrichment (tasks_helpers/enrich.py) shows the
  assessor's owner string as Current Owner on every probate lead; for an estate parcel
  that string can read "... ESTATE C/O <rep> PR". Codex: not a blocker for this connector.
