# Snohomish County coverage audit (all six record types)

Date: 2026-09-13. Code: backend main `ff9ecd6`, frontend master `618a4ae`. Investigation only, no code changed.

> **Pre-repair snapshot.** "Current BridgeLeads status" below is the state at `ff9ecd6`. Since then: tax cap and file-selection repaired (#276, #277), NTS health and silent-empty fixed (#278), three dropped Tribune notices recovered (#281), county GIS mailing held off pending legal review (#284). The recommended statuses are unchanged.
Reviewers: Claude (3 code audits, 2 source-research passes, independent re-verification) and Codex (independent review, reconciled below).

## Verdicts

| Record type | Current BridgeLeads status | Source | Recommended status |
|---|---|---|---|
| Code Violation | Not implemented | PDS monthly complaints PDF (unincorporated only) | NOT CURRENTLY SUPPORTABLE |
| Tax Delinquent | Implemented, fails every Aug 1 to Dec 31 (cap defect) | Treasurer Current Tax List | NEEDS REPAIR + LEGAL REVIEW REQUIRED |
| Probate | Dead inactive connector row (mig 006) | Superior Court (GR 31), Auditor recorder (login + ToS), newspaper Notice to Creditors | NOT CURRENTLY SUPPORTABLE |
| Pre-Foreclosure | Implemented | Snohomish County Tribune Legals PDF (NTS) | PARTIAL COVERAGE ONLY (plus health repair) |
| Death Certificate | Not implemented | WA DOH vital records (PRA-exempt) | NOT CURRENTLY SUPPORTABLE |
| Auction Leads (trustee_sale) | Implemented, same source and event as Pre-Foreclosure | nts_notices cache of the same Tribune PDFs | PARTIAL COVERAGE ONLY (plus health repair) |

## Verified evidence (primary)

- Tax cap defect: `snohomish_wa_tax_delinquent.py:496` keeps only `year < as_of_year`; `:717` + `:588` drop parcels whose oldest year `< tax_cap_min_year(now)`; `tax_filters.py:93-114` gives min year = current year from August. Result: 0 records, `RuntimeError` at `:807`, job FAILED, connector down, stored prior-year rows hidden by view cap (`tax_filters.py:128-133`, King exempt only).
- GR 31(g)(4), verbatim from courts.wa.gov PDF: "The use of court records, distributed in bulk form, for the purpose of commercial solicitation of individuals named in the court records is prohibited."
- County open-data license on the Parcels layer (hub item a69c4dc383114f4a8a30b8d4cccf7823) and Assessor Roll CSV Collection, verbatim: "By proceeding and accessing this data, you agree and represent that you will not use any lists of individuals, or data from which such lists may be compiled, for any commercial purpose." The in-flight branch `fix/mailing-source-snoho-cowlitz` reads `taxprline1/taxprcity/taxprstate/taxprzip` from the same Parcels dataset (gis.snoco.org Hosted/CADASTRAL__parcels).
- County website terms (snohomishcountywa.gov/1965/Website-Information): "not to use high-volume, automated, electronic processes to access or query the database ... not to engage in Data Mining (mass downloading)".
- PDS "Code Enforcement Complaints for the Period of August 2026" (Archive.aspx?ADID=7529, run 09/08/2026, 12 pages): columns Folder#, Violations, SubSite (parcel), Address, In Date; frequent "No Violation"; no owner, no status.
- Tribune Legals 9-9-26: 10 Notices of Trustee's Sale, about 20 probate Notices to Creditors (Snohomish Superior Court case numbers, no parcel). snoho.com legal-notices page answers HTTP 404 but still links the PDF; no publisher terms link found.
- RCW 70.58A.540: vital records "are not subject to public inspection and copying under chapter 42.56 RCW". WA Digital Archives death index ends 2017.

## Codex reconciliation

Adopted (Codex severity): NTS types are not "already supported" (Tribune-only, unmeasured share, silent-empty paths); code_violation cannot represent active violations; GIS/Assessor mailing branch must stay unmerged pending written clearance; taxpayer/grantor are "source-reported", not verified owners; Tax Balance Owed = sum of source `owed` fields as of file date (not payoff); NTS principal owing is not total arrears; Jan-1 `date_recorded` must not be presented as an event date; enrichment exception becoming DONE + billed is unsafe; Tracerfy paid for over-quota rows; FE county health from first unordered row; plan cards say "All record types" without county qualification; parcel|address dedup is not event identity (a later foreclosure on a property already delivered to that user is suppressed).
Rejected with reason: "WNPA terms apply to current Tribune data". BridgeLeads fetches the publisher's own PDFs, not wapublicnotices.com. Residual kept: publisher reuse terms are unlocated, so LEGAL REVIEW covers the Tribune too.
Noted, out of scope: divorce (also Superior Court, same GR 31 bar).

## Not verified (blocked or out of reach)

- Production DB state (connector health, Snohomish row fill rates, ENTITLEMENT_ENFORCEMENT value): read-only prod query was denied by the permission classifier.
- City code-enforcement portals (Everett eTRAKiT, Lynnwood/Mukilteo SmartGov) in a real browser: Playwright MCP failed to connect.
- Tribune share of all Snohomish NTS (needs reconciliation against recorded NTS counts).
