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

## Column mapping (BridgeLeads → BatchDialer destination field)

| BridgeLeads column | BatchDialer destination | Notes |
|---|---|---|
| `first_name` | First Name | Required by BatchDialer; must be its own column ✓ |
| `last_name` | Last Name | Required ✓ |
| `property_street` | Street Address (property) | Street only — exactly what BatchDialer wants (never map the full `property_address` — combined addresses are their documented anti-pattern) |
| `property_city` | City (property) | May be blank for counties whose source data is street-only (King/Pierce situs) — that's the county data, not the export |
| `property_state` | State (property) | |
| `property_zip` | Zip (property) | ZIP+4 (`98499-2817`) may appear; if BatchDialer ever rejects rows, this is the first thing to check |
| `mailing_street` | Mailing Address 1 | |
| `mailing_city` | Mailing City | |
| `mailing_state` | Mailing State | |
| `mailing_zip` | Mailing Zip | |
| `phone` | Phone 1 | Bare 10-digit (e.g. `2065551234`) — the safest universal dialer format |
| `phone_2` | Phone 2 | Map each phone to its own destination slot — never combined |
| `phone_3` | Phone 3 | BatchDialer's own best practice is 3–5 phones per contact; we ship 3 |
| `email` | Email | `email_2`/`email_3` → custom fields if wanted |
| everything else | leave unmapped | `party_name`, `parcel_id`, `legal_description`, amounts, signals, etc. have no standard destination. Leave them unmapped (or create BatchDialer Custom Fields deliberately). Unmapped-column behavior is undocumented by the vendor, so don't force-map them |

## Upload steps (BatchDialer)

1. Contacts → Contact Lists → **Import Contacts** → upload the BridgeLeads CSV (**CSV only**
   on this screen — don't convert to XLSX).
2. Map columns per the table above. The headers are snake_case, so expect to map manually the
   first time; **save the mapping** so future uploads are one click.
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
