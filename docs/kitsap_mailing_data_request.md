# Kitsap County — Taxpayer Mailing Data Access Request (draft)

**Why this doc:** Kitsap leads (EagleWeb recorder, parcel ids from the documents) have
no owner mailing source. The Assessor publishes exactly what we need as weekly bulk
files on https://www.kitsap.gov/assessor/Pages/DataDownload.aspx (`Parcels.txt`,
`Property_addresses.txt`, … tab-delimited, "updated weekly"), but:

1. **The files sit behind an Azure WAF JavaScript challenge** (verified 2026-10-03:
   `Parcels.txt` and its `.pdf` layout return the "Azure WAF" challenge page to an
   automated client). We do not script around bot protection.
2. **The terms require a written agreement to sell.** The Assessor's Public Records
   Access Policy (https://kitsap.gov/assessor/Pages/DISCLAIMER1.aspx) says:
   *"Washington State law, RCW 42.56.070 section 9, prohibits the use of lists of
   individuals for 'commercial purposes'"* and *"No one is permitted to sell this
   information except in accordance with a written agreement with Kitsap County."*

So the only correct path is an approved arrangement with the County. This is the
outreach draft and the technical asks. **Owner action** (not something the code can do).

> **Legal note — read before sending.** RCW 42.56.070(9) bars an agency from providing
> lists of individuals *requested for commercial purposes*. Kitsap may lawfully decline
> to provide taxpayer names/mailing addresses for a lead-list product at all, agreement
> or not. Involve counsel on the use-case description below; do not misstate the
> purpose to obtain the data.

---

## Who to contact
- **Kitsap County Assessor's Office** — owner of the bulk download files.
  Public Records Access Policy page above; the County's designated public records
  officer (RCW 42.56.580) handles formal requests.
- Ask for the person who administers the **DataDownload** page and for whoever can
  sign a **data-use agreement** on the County's behalf.

## What to request (in priority order)
1. **A written data-use agreement** covering commercial use of the parcel / taxpayer
   files for a subscription lead product (the clause on their policy page). Ask what
   terms, fees and restrictions apply.
2. **An approved automated path** to the weekly files: an allowlisted egress IP (our
   Railway worker), a service account, or an SFTP drop, so the WAF challenge is not
   in the way. Weekly cadence matches their publication.
3. **The file layouts** (`parcels.pdf`, `Property_addresses.pdf`) — the columns for
   taxpayer name, mailing street, city, state, ZIP, and the parcel/account id format
   (`acct_no` 14 digits, often shown dashed `012302-2-005-2007`).
4. **Fallback:** pricing for a periodic manual export if automation is not offered.

## What we would do with it (for the agreement)
- Fill the **owner mailing address** for Kitsap parcels that already appear in public
  recorder documents (probate, pre-foreclosure, code enforcement), keyed by parcel
  number. We never infer a parcel from a name.
- Keep only the mailing block; no bulk resale of the file; delete superseded weekly
  snapshots. Restate whatever retention / attribution terms the County requires.

---

## Draft email

> **Subject:** Data-use agreement request — Assessor parcel/taxpayer bulk files
>
> Hello,
>
> We operate a Washington-based service that helps real-estate professionals work from
> public county records. Your Assessor's DataDownload page publishes weekly parcel files
> (Parcels.txt and related), and your Public Records Access Policy notes that selling
> this information requires a written agreement with Kitsap County.
>
> We would like to request such an agreement. Our use: for parcels that already appear
> in Kitsap County recorded documents, we would add the taxpayer mailing address from
> your parcel file, keyed by parcel number. We would not redistribute the file itself.
>
> Could you tell us:
> 1. Whether Kitsap County offers a data-use agreement for this purpose, and its terms
>    and fees;
> 2. Whether an approved automated download path is available (an allowlisted IP,
>    service account or SFTP), since the public page now presents a browser challenge
>    to automated clients;
> 3. The current file layouts for the parcel and address files.
>
> We understand RCW 42.56.070(9) and will follow whatever conditions the County sets,
> including declining if this use is not permitted.
>
> Thank you,
> [Name], [Company], [Contact]

---

## When an agreement exists (engineering, not before)
A loader for the County-provided file, the Snohomish Assessor Roll pattern
(`src/scrapers/enrichment/snohomish_assessor_roll.py`): weekly snapshot, index by
parcel, `resolve_mailing(parcel_ids) -> {pid: MailingAnswer}`, registered in
`county_gis._BULK_MAILING_SOURCES` and `_BULK_MAILING_LICENSE_RESTRICTED`. Kitsap's
two dashed parcel formats are already handled by `county_gis._format_kitsap`.
