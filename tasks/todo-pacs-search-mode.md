# PACS parcel mailing: send the search mode; read "Geographic ID"; never settle an id we cannot search

Branch `fix/pacs-search-mode` (off main `75ea4e39`).

## Problem (found 2026-10-31 from `pacs_clallam: 3 unparsed pages in a row; layout drift`)

Reproduced live with the module's own functions:
- The parcel postback never sent `propertySearchOptions$searchType`. Clallam, Grant, Jefferson,
  Chelan and Island answer with the empty form and "You must enter search criteria." Only Benton
  (no owner mode) worked. Whatcom was 302-ing to its own error page (outage; already deferred).
- Clallam and Island head the identity column "Geographic ID"; `geo\s*id|parcel` missed it.
- `normalize_pacs_parcel` returns None for ids with letters (Island `R03213-049-3470`), and
  `resolve_mailing` recorded PARCEL_NOT_FOUND (settled) without asking.
- The owner-name search had the same mode omission.
Impact: `unparsed` is charged, so queued leads in those counties spent their 5 attempts on the
error page and gave up; live jobs got no PACS mailing there either.

## Codex
- Consult: GO with revisions: explicit mode only when offered (Grant defaults to `owner`), defer
  when the form lacks it; regex fix; unsupported ids defer; prefix-only matches never settle;
  unreadable identity cells are unparsed; owner path gets `owner` mode; remediation is a separate
  owner-approved repair (the requeue script skips these rows).
- Deviation (accepted at review): a prefix-only match is AMBIGUOUS (charged, capped at 5), not
  source_unavailable, because the page was fetched and an uncharged rotation would re-search
  every tick forever.
- Diff review: GO (P2: short data rows -> unreadable; Benton owner end-to-end test), both done;
  re-review GO.

## Done
- [x] `pacs.search_type_field`; parcel search posts `account-number`, name search posts `owner`.
- [x] `_GEO_HEADER_RE` accepts "Geographic ID".
- [x] parse_results: unreadable cell / truncated data row -> UNPARSED; prefix-only -> AMBIGUOUS.
- [x] Letter-bearing ids defer (SOURCE_UNAVAILABLE, no request); empty/all-zero still settle.
- [x] Fixtures from the live Clallam/Grant pages (grid cells redacted; ViewState checked clean).

## Review
- Tests: tests/test_pacs_search_mode.py + tests/test_pacs_parcel.py; 11 fail on the unfixed code.
  25 related enrichment files: 619 passed; PACS files 125 passed; ruff, OpenAPI, import clean.
- Live (script, not committed): with the fix Clallam `0630115504300000` -> found with an address;
  Benton still found.

## Not done (owner decisions / follow-ups)
- Remediation: leads that gave up (5 charged `unparsed` attempts) or were settled
  `parcel_not_found` from a letter-bearing id need a dry-run-first repair (production write).
- Island search format for letter-bearing ids (a stripped `R032130493470` returns no rows).
- Which Clallam spelling our leads carry (16-digit settles cleanly; 12-digit is now ambiguous).
