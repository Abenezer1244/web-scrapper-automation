# Importing a BridgeLeads CSV into BatchDialer

**Verdict (2026-07-01, deep research + Codex cross-check):** the BridgeLeads downloadable
lead CSV is already structurally a right fit for BatchDialer's contact importer. BatchDialer
uses a manual "source column → destination field" mapping screen and its own docs say column
names don't matter ("no matter what the columns are named") — what matters is that the data
is SPLIT into atomic columns, which our CSV is. No format changes are needed; use the mapping
below.

> Research basis: official BatchLeads & BatchDialer help center (help.getbatch.co /
> help.batchservice.com) — see Sources at the bottom. Items the vendor does not document
> (exact phone-format validation, row/file-size limits, unmapped-column behavior) are called
> out as unknowns, not assumed.

## Two CSV layouts (2026-09-15)

Every scraper exports one of two layouts, set per scraper (`deliver.csv_layout`):

- **CRM-ready (`crm_v1`)**: Title Case headers in dialer order (First Name, Last Name, Party
  Name, Property Address/City/State/Zip, Mailing Address/City/State/Zip, Phone 1-3, Email 1-3,
  Parcel ID, County, County State, Record Type, Date Recorded, then record-type columns). New
  scrapers use this layout.
- **Classic (`legacy_v1`)**: the original snake_case headers. Scrapers created before 2026-09-15
  keep it, so a mapping saved against those headers keeps working.

The VALUES are identical in both layouts; only the header text and column order differ. A
mapping saved in BatchDialer is keyed on header text, so switching a scraper's layout means
re-mapping once and saving the new mapping.

## Column mapping (BridgeLeads to BatchDialer destination field)

| CRM-ready header | Classic header | BatchDialer destination | Notes |
|---|---|---|---|
| First Name | `first_name` | First Name | Required by BatchDialer. Blank on purpose when the owner is a business, trust or agency, or when the name cannot be split reliably (see gotchas) |
| Last Name | `last_name` | Last Name | Required. Same blank rule |
| Property Address | `property_street` | Street Address (property) | Street only. Never map Full Property Address / `property_address` (combined addresses are their documented anti-pattern) |
| Property City | `property_city` | City (property) | Can be blank where the county source is street-only |
| Property State | `property_state` | State (property) | |
| Property Zip | `property_zip` | Zip (property) | ZIP+4 (`98499-2817`) can appear; if BatchDialer ever rejects rows, check this first |
| Mailing Address | `mailing_street` | Mailing Address 1 | Blank when the mailing address could not be read confidently (for example a foreign address); the full text stays in Full Mailing Address / `mailing_address` |
| Mailing City | `mailing_city` | Mailing City | |
| Mailing State | `mailing_state` | Mailing State | |
| Mailing Zip | `mailing_zip` | Mailing Zip | |
| Phone 1 | `phone` | Phone 1 | Bare 10-digit (`2065551234`), the safest universal dialer format |
| Phone 2 | `phone_2` | Phone 2 | Map each phone to its own slot, never combined |
| Phone 3 | `phone_3` | Phone 3 | BatchDialer recommends 3-5 phones per contact; we ship 3 |
| Email 1 | `email` | Email | Email 2 / Email 3 to custom fields if wanted |
| everything else | everything else | leave unmapped | Party Name, Parcel ID, County, amounts, dates and signals have no standard destination. Leave them unmapped or create BatchDialer Custom Fields deliberately. Unmapped-column behavior is undocumented by the vendor |

## Upload steps (BatchDialer)

1. Contacts → Contact Lists → **Import Contacts** → upload the BridgeLeads CSV (**CSV only**
   on this screen — don't convert to XLSX).
2. Map columns per the table above (CRM-ready headers match BatchDialer's field names closely;
   Classic headers are snake_case). Map manually the first time and **save the mapping** so
   future uploads are one click. Keep one saved mapping per layout.
3. Choose duplicate handling deliberately: **Keep Old / Keep New / Reject**. BatchDialer
   dedupes **account-wide by phone number** — a lead already in any list can be rejected or
   merged depending on this choice.
4. Scrub options: litigator + duplicate scrub run **by default**; federal-DNC scrub is
   opt-in. (BridgeLeads does not pre-scrub DNC — do this here.)

## Known gotchas

- **Rows without any phone number land in BatchDialer's "Misformed Leads" bucket** (their #1
  documented import issue). BridgeLeads phones come from skip tracing — for dialer-bound
  lists, enable skip tracing on the scraper, or expect no-phone rows to be set aside.
- If "Fields to collect" hides the mailing address on a scraper, `mailing_*` columns are
  deliberately blank in that export.
- **Blank First/Last is deliberate.** BridgeLeads never guesses a name: businesses, trusts,
  government agencies, heirs, and names whose order cannot be trusted export with First/Last
  blank and the full owner in Party Name. BatchDialer lists First/Last as required, so decide at
  import whether to skip those rows or map Party Name to Last Name for them in a separate import.
- **Opening the CSV in Excel first strips leading zeros** from Parcel ID and ZIP (`00501`
  becomes `501`). Upload the downloaded file directly, or use the Excel export, which stores
  those columns as text.
- Vendor-undocumented (verified absent from their docs, do not assume): max rows/file size,
  exact phone-format validation rules, whole-file failure conditions, encoding tolerance.
  BridgeLeads ships UTF-8, RFC-quoted CSV with a header row, which matches everything they do
  document.

## Sources (official help center)

- How to Import your Spreadsheet of Contacts into BatchDialer —
  help.getbatch.co/en/articles/9792739 (required fields; CSV-only; mapping; duplicate options;
  scrub defaults; Misformed Leads)
- What is the format required for importing files — help.getbatch.co/en/articles/9787689
  (split-column requirement; example column set)
- Formatting Your Files for Importing — help.getbatch.co/en/articles/9787505 (separate
  first/last name; headers required)
- BatchDialer Best Practices — help.getbatch.co/en/articles/9868141 (3–5 phones per contact)
- How to Use Custom Fields — help.batchservice.com/en/articles/9787627
- BatchDialer FAQs — batchdialer.com/faq (litigator/DNC scrub)
