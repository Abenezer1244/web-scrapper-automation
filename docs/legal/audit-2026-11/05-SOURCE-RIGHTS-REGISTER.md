# County source-rights register (2026-11-04)

Not legal advice. Facts and quoted terms for counsel.

**Method**
- The connector list was rebuilt from code (`registry.py`, templates, migrations) and `tasks/phase-b-execution-plan.md`. The authoritative list is the production `county_connectors` table, which the owner must dump (B1-3).
- Terms and `robots.txt` were read from public pages only. Nothing was bypassed.
- **Spot-checked 2026-11-04 by direct fetch:**
  - Pierce ATIP `robots.txt` (`Disallow: /`, allows only `/app/v2/...search`)
  - Pierce ARMS `robots.txt` (`Disallow: /`)
  - iDocMarket `robots.txt` (`Disallow: /`)
  - the King LandmarkWeb terms text
- Other quotes come from a research pass. Counsel should re-read the live pages.

## Cross-cutting conduct (code facts)

| Conduct | Evidence |
|---|---|
| reCAPTCHA solved via 2Captcha (incl. Enterprise) | `enrichment/captcha.py`. Call sites: King recorder (`king_wa_probate.py:195-201,303-400,~667`, which injects the token and patches `grecaptcha.getResponse`) and Pierce ATIP (`pierce_atip.py:267,295`). Gate `CAPTCHA_ENABLED`; the production value is unknown. |
| Clickwrap terms accepted by automation | LandmarkWeb `SetDisclaimer()` (`templates/landmarkweb.py:143-180`), AcclaimWeb checkbox (`acclaimweb.py:256-340`), Tyler SelfService **force-enables a disabled accept button** (`tyler_selfservice.py:7-12,177`), Whatcom "I Agree". |
| Automation masking | `--disable-blink-features=AutomationControlled`, `navigator.webdriver` override (`base_scraper.py:317,405-410`). The default UA is a spoofed Windows Chrome/120 (`browser_identity.py`). Several HTTP paths spoof Chrome. Only `skip_trace.py` and the probes identify as BridgeLeads. |
| No `robots.txt` evaluation | None in the scraper base. |
| Pacing | Default 300 ms (`POLITE_DELAY_MS`). Accela 3 s with caps. ATIP 0.5 s. PACS per-county lease with cooldown on 403/429. |
| No login anywhere | A Snohomish login-required class remains in code, commented "NOT PUBLIC"; confirm it is unused. |

## Register

Risk ratings:
- **H** = explicit prohibition, or a circumvention control.
- **M** = terms unread, or a statutory-list question.
- **L** = open data with a permissive licence.

| County | Record types | Source | Access | CAPTCHA / clickwrap | Published terms (quote) | Statutory basis | Risk |
|---|---|---|---|---|---|---|---|
| King | probate, death_certificate, pre_foreclosure, divorce | recordsearch.kingcounty.gov (LandmarkWeb) | Playwright, 90-day windows | **2Captcha (if enabled).** Clickwrap by JS. | "You agree not to use high-volume, automated, electronic processes to access or query the database ... not to engage in Data Mining (mass downloading)"; "not been compiled or linked for commercial purposes" (verified) | RCW 65.04 / 36.22; RCW 42.56.070(8) | **H** |
| Pierce | probate, pre_foreclosure, divorce | armsweb.co.pierce.wa.us (ARMS) | Playwright | Terms gate | robots `Disallow: /` (verified). Terms ban data mining and "high volume, automated, electronic processes"; "not ... for commercial usage". | RCW 65.04; 42.56.070(8) | **H** |
| Pierce | enrichment (address); Tacoma code-violation owner name | atip.piercecountywa.gov `/api/pcAtipSummary` | HTTP + **reCAPTCHA Enterprise via 2Captcha** | CAPTCHA | robots disallows everything except `/app/v2/*search` (verified), so the `/api` path the code calls is disallowed. The footer cites RCW 42.56.070(8) (per a code docstring). | RCW 42.56.070(8) | **H** |
| Clark | probate, pre_foreclosure, divorce | e-docs.clark.wa.gov (LandmarkWeb) | Playwright | Clickwrap by JS | Same wording as King | RCW 65.04; 42.56.070(8) | **H** |
| Columbia | probate, pre_foreclosure | idocmarket.com/COLWA1 (Tyler) | Playwright | none | Terms s.2: access "manually and not programmatically"; "systematically download or programmatically download" prohibited. robots `Disallow: /` (verified). | RCW 65.04 | **H** |
| Snohomish | tax_delinquent | Treasurer "Current Tax List" bulk file | HTTP | none | Site-wide: "not been compiled or linked for commercial purposes"; data-mining ban. The Assessor and Treasurer pages require an RCW 42.56.070(8) acknowledgement. | RCW 84.56; **42.56.070(8) list** | **H** |
| King | owner name/mailing enrichment (tax_delinquent etc.) | blue.kingcounty.com eRealProperty | HTTP per parcel | Checkbox acknowledgement on the page | State law "prohibits the use of lists of individuals for 'commercial purposes'"; "Users must check a box acknowledging these prohibitions" (the page cites (9)) | RCW 42.56.070(8)/(9) | **H** |
| Many | owner mailing (PACS, Clark PIC, Thurston, TaxSifter, Snohomish/Cowlitz/Douglas GIS) | assessor / GIS | HTTP per parcel / ArcGIS | none | Clark PIC: "RCW 42.56.070(8) prohibits releasing and/or using lists of individuals gathered from this site for commercial purposes." Snohomish GIS terms: will not use lists "for any commercial purpose". | RCW 42.56.070(8) | **H**. Gated by `COUNTY_GIS_RESTRICTED_MAILING_ENABLED`; owner "clearance" 2026-10-02 with no counsel memo. |
| Snohomish | pre_foreclosure, trustee_sale | snoho.com Tribune legal-notice PDFs | HTTP, **spoofed browser UA to pass a 403** | none | Terms not found. The code chose this source because the recorder bans automation. | RCW 61.24.040; copyright | M |
| King | trustee_sale | queenannenews.com legal-notice PDF | HTTP | none | Not found | RCW 61.24.040 | M |
| Pierce | trustee_sale | tacomadailyindex.com | HTTP | none | robots allows. Terms page 404. | RCW 61.24.040 | L/M |
| Clark | trustee_sale | classifieds.columbian.com | HTTP | none | robots now carries content signals (`use=reference`, `ai-train=no`) and an EU rights reservation. The code comment says "robots.txt 404", which is out of date. | RCW 61.24.040 | M |
| Whatcom | probate, pre_foreclosure, divorce, tax_delinquent | recording.whatcomcounty.us (Helion) | Playwright | Clicks "I Agree" | Agreement by navigation. No automation language found. | RCW 65.04 | M |
| Skagit | probate, pre_foreclosure, divorce | classic.skagitcountywa.gov | Playwright | "By using this search, you are agreeing to Skagit County's terms" (linked page 404) | Unknown | RCW 65.04 | M |
| Benton, Clallam, Grant, Island, Jefferson, Kitsap, Thurston, Whitman (+others) | probate, pre_foreclosure | Tyler EagleWeb | Playwright / HTTP (spoofed UA) | Disclaimer pages | Not retrieved | RCW 65.04 | M |
| Chelan, Douglas | probate, pre_foreclosure | Tyler AcclaimWeb | Playwright | Checkbox by script | Not found | RCW 65.04 | M |
| Okanogan, Lincoln, Stevens | probate, pre_foreclosure | Tyler SelfService | Playwright | **Force-enables a disabled accept button** | Stevens: accuracy and indemnity only | RCW 65.04 | M |
| Cowlitz | probate, pre_foreclosure | Laserfiche WebLink | Playwright | none | Not found | RCW 65.04 | M |
| King (uninc.) | code_violation | aca-prod.accela.com/KINGCO | Playwright, 3 s pacing; **stops on any wall** | none | None found | RCW 42.56 | L/M |
| King | tax_delinquent (base) | data.kingcounty.gov `dsv3-ct3e` | Socrata API | none | Licence PUBLIC_DOMAIN. The owner name is added from eRealProperty (H, above). | RCW 84.56 | L |
| Seattle | code_violation | data.seattle.gov `ez4a-iug7` | Socrata API | none | PUBLIC_DOMAIN | RCW 42.56 | L |
| Tacoma | code_violation | Tacoma ArcGIS | REST | none | "no legal restrictions on use or reuse" | RCW 42.56 | L |
| Bellevue, Burien | code_violation | ArcGIS / Cityworks | REST | none | Not found | RCW 42.56 | L/M |
| WA courts (Odyssey, LINX) | none | not scraped | n/a | n/a | Registration required; no bulk (GR 31) | GR 31 | n/a |

## Superseded claims (docs that now contradict code)

- `docs/compliance/connector-audit-2026-04-10.md`
  - Cites `ai_scraper.py` and "AI-mode", which were renamed to "template" in migrations 108/109.
  - Says Snohomish is a terminal blocker; it now has three live non-recorder connectors.
  - Its connector counts are April figures.
- `docs/compliance/wa-tax-delinquent.md`
  - Says King is the only tax_delinquent county; Snohomish was added (migration 040).
  - Calls King "open data", but the owner names come from eRealProperty (RCW acknowledgement).
- `docs/compliance/wa-court-records.md`
  - Says King probate uses DJA; the code uses the recorder's Death Certificate category.
  - Says divorce is "not attempted"; recorded decrees are scraped in 5 counties.
- `src/scrapers/.../nts_columbian.py:5`
  - Says "robots.txt 404 — no restriction", which is out of date.

## What counsel needs from the owner

The production `county_connectors` dump; the production values of `CAPTCHA_ENABLED` and `COUNTY_GIS_RESTRICTED_MAILING_ENABLED`;
and any written advice behind the 2026-09-14 (Tacoma owner name) and 2026-10-02 (RCW 42.56.070 "cleared for all counties")
decisions.
