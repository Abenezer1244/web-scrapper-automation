# Washington State — Tax Delinquent Record Compliance

## Legal framework

**RCW 42.56.070(8)** (Washington Public Records Act) prohibits using
lists of individuals obtained from public records for commercial
purposes. Several Washington county treasurer portals gate their
delinquent-tax parcel lists behind an explicit acknowledgment of this
statute before displaying the list.

BridgeLeads is a commercial lead-generation SaaS. Scraping any county
list that requires acknowledgment of RCW 42.56.070(8) would directly
violate the terms required to view it.

## County-by-county status

| County  | Source                                               | Status       | Notes |
|---------|------------------------------------------------------|--------------|-------|
| King    | `data.kingcounty.gov/resource/dsv3-ct3e` (Socrata)   | **Supported**| Published as general-purpose open data. No RCW 42.56.070(8) click-through. |
| Pierce  | `atip.piercecountywa.gov/app/v2/foreclosure/`         | **Blocked**  | Portal requires user to acknowledge RCW 42.56.070(8) prohibitions before showing the list. Annual publication only (July). |
| Spokane | `spokanecounty.org/804/ForeclosureDistraint`          | **Blocked**  | Per-case lookup only, not a downloadable list. Auction listing gated by equivalent restrictions. |
| Clark   | Treasurer distraint listings                          | **Blocked**  | Equivalent restriction on commercial list use. |

## Action

- King County is the only WA county we support for `tax_delinquent`.
- Do NOT build a Pierce/Spokane/Clark tax-delinquent scraper unless
  legal counsel confirms RCW 42.56.070(8) permits aggregated
  lead-generation SaaS or the county changes its publication policy.
- If a user requests Pierce tax delinquent in the UI, the county
  connector row must set `scraper_class=NULL` with an explanatory
  message so the frontend can show "Not available — see compliance
  notes".
