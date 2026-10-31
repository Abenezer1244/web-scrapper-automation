# Clallam PACS: a 12-digit statewide parcel id is the portal's `id + "0000"` land row

Branch `fix/clallam-land-suffix` (off main `2c0cae53`, which has #575/#576/#578/#579 deployed).

## Problem
Every Clallam lead carries the WA statewide layer's `ORIG_PARCEL_ID`, always 12 characters
(`009-063011550430`). The Clallam PACS portal prints a 16-digit Geographic ID. #575 reads a
row that only EXTENDS our id as `ambiguous` (never equated, never found), so every Clallam
lead now spends its 5 recovery attempts on `ambiguous` and gets no mailing address. Four
leads in production sit in the #576 repair's cohort A, held back until this is solved.

## Evidence (live, read-only, 2026-10-31; no owner names or addresses read out)
- Statewide layer, FIPS 009: 54,516 rows; every id is 12 characters (some carry letters,
  e.g. `152828is0063`, which `normalize_pacs_parcel` already rejects).
- Portal, 4 lead ids + 30 random statewide ids (seeded): 33 of 34 return exactly ONE grid
  row, type `Real`, Geo ID = id + `0000`, no other row. 1 returns no row (a settled
  not-found, unchanged). Situs: 22 of 23 comparable rows agree on house number + street;
  the 23rd agrees on house number with a longer portal spelling; 11 have no situs on one side.
- Non-`0000` suffixes exist: prefix searches show `1000`, `2001`, `5001`-`5004` (all `Real`).
  Base `053002300000`: the portal has `...1000` and `...2001` (no `0000`); the statewide
  layer has ONE row `053002300000`. So a 12-digit id can name several portal parcels, and
  then it is genuinely ambiguous.

## Rule (Clallam only)
A 12-digit query `Q` (zeros kept) is equated with Geo ID `Q + "0000"` ONLY when that row is
the single grid row extending `Q`. Any other row extending `Q` (another suffix, a second
`0000` row) keeps it `ambiguous`. An exact 16-digit caller id is unchanged. Other counties
unchanged (no evidence for their formats).

## Change
1. `PacsSite` gains `land_suffix` / `short_id_len` (Clallam: `"0000"`, 12; others none).
2. `parse_results(html, parcel_key, land_key=None)`: a `land_key` hit counts only when no
   other row extends `parcel_key`; exact hits keep today's behavior.
3. `parse_detail(html, parcel_key, land_key=None)`: the detail page must name exactly one
   Geo ID equal to the key that matched (identity still closes twice).
4. `_resolve_one`/`resolve_mailing` derive `land_key` from the caller's spelling (12 digits,
   zeros kept) for sites that declare it.
5. Repair script: `commit_has_fix(sha, counties)` also requires the land-suffix code in the
   deployed source when `clallam` is in the counties (the default run includes it).
6. Tests: Clallam-shaped fixtures (single `0000` row FOUND; `0000` + `1000` AMBIGUOUS;
   only `1000`/`2001` AMBIGUOUS; detail page naming the 16-digit id FOUND, another id
   MISMATCH; Benton/Jefferson unchanged); repair-script gate test.

## After merge + deploy (owner-visible)
- `repair_pacs_mailing_recovery.py --county clallam` dry-run, then `--apply` with the new sha.

## Independent proof (Codex High: sampling alone is not the identity contract)
The county's own layer `Parcels___Clallam_County` (services3.arcgis.com, fields `GEO_12`, `PROP_ID`):
54,675 rows, every `GEO_12` maps to exactly one `PROP_ID`. That `PROP_ID` is the portal's prop_id on
the `id + "0000"` row in 33 of 33 parcels (4 leads + 29 seeded samples). For the multi-suffix bases it
points at `...2001`, not `...1000`: those stay ambiguous (correctly conservative). The portal's own map
link searches the county map for the 12-digit id.

## Steps
- [x] 1. Live evidence (above).
- [x] 2. Codex plan consult: GO with amendments (4 High, all built): result-set completeness,
      width from the zero-kept spelling, no equivalence across disagreeing callers, matched key into
      the detail check; county-aware repair gate (Medium).
- [x] 3. Code + tests (40 in test_pacs_search_mode, 171 + 236 across PACS/GIS/recovery suites);
      ruff; OpenAPI/import check; 8 mutants all killed. Live run on the branch: the 4 leads found.
- [ ] 4. Codex review to GO.
- [ ] 5. PR, CI, merge, deploy; Clallam dry-run + apply; verify a found mailing.

## Review
- `pacs_parcel.py`: `PacsSite.land_suffix`/`short_id_len`/`land_id()`; `parse_results` returns
  `(outcome, prop_id, matched_key)` with the land rule and `_grid_is_complete`; `_resolve_one` checks
  the detail page against `matched_key`; `resolve_mailing` carries one land id per key.
- `repair_pacs_mailing_recovery.py`: `commit_has_fix(sha, counties)` with structural Clallam checks.
- Not changed: `county_gis`, `mailing_recovery`, the `MailingAnswer` contract.
