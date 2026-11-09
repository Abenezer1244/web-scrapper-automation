# P9: California DROP (Delete Act) ingestion, dormant until the owner registers

Owner: "do all the engineering items yourself with codex" (2026-11-09). D8 (whether
BridgeLeads is a data broker that must register and process DROP) is a COUNSEL decision;
this builds the machinery so that, if counsel says yes, the owner registers, sets the key
and turns it on. Nothing runs without `DROP_API_KEY` and `DROP_ENABLED=true`.

## Facts (primary sources, 2026-11-09; spec vendored: docs/legal/audit-2026-11/drop/databroker_api-v1.2.0.yaml)
- REST API `https://api.drop.privacy.ca.gov` (sandbox `/sandbox`), header `X-API-KEY` issued in
  the Data Broker Portal. `GET /data/download` -> 200 ZIP | 200 no new data | 202 preparing;
  `POST /data/upload` and `/data/amend` (multipart field `files`) -> 202 + JSON
  `{message, acceptedCount, rejectedCount, accepted[], rejected[]}`; 429 -> wait 30 s.
- ZIP: one CSV per selected list, header `Id,Hash`, Base64 SHA-256 of standardized values;
  plus a `Removed` CSV of identifiers withdrawn since delivery.
- Lists BridgeLeads can match: **Email** (trim all whitespace, lowercase) and **Phone** (digits
  only, last 10). We hold no DOB, VIN, MAID or CTV IDs, so NDZ/NameVIN/MAID/CTVID cannot match.
- Report back within 45 days of download: CSV `Id,Status`, codes 2 Exempted, 3 Deleted,
  4 Opted out (one identifier, several consumers: all opted out of sale/sharing), 5 Not found.
- Download at least every 45 days (30 from 2027-01-01, AB 883). No match: keep the minimum
  needed to screen newly collected records before they are sold or shared.

## Design (to pressure-test with Codex before any code)
- Migration 139:
  - `drop_downloads(id, file_name, downloaded_at, list_counts jsonb)`: evidence of cadence.
  - `drop_identifiers(id, list_type email|phone, drop_id, hash, download_id, received_at,
    removed_at, status 2-5 NULL, decided_at, reported_at, report_file)`: one row per DROP row;
    unique (download_id, list_type, drop_id).
  - Owner-only/definer functions in the 125-137 style; worker reaches them by EXECUTE only.
- `src/privacy/drop.py`: spec-exact standardization + `sha256 -> base64` for email and phone;
  ZIP/CSV parsing with size and row caps; status CSV writer; an HTTP client (timeouts, 202 and
  429 handling, no retries on 4xx) that never logs identifiers or the key.
- Matching (worker, after each download): scan stored contacts (results.email/emails,
  phone/phones; decrypted by their column types), compute their DROP hashes, join to the
  pending identifiers. Per identifier:
  - no stored lead carries it -> status 5 Not found; keep the hash to screen new records.
  - one consumer (one traced subject on the matching leads) -> erase: create a privacy request
    (channel 'drop', kind 'delete') + rule from the lead's own anchor (the email/phone) and its
    traced subject, activate it (P6 erasure runs), status 3 Deleted once the erasure completes.
  - several distinct subjects share it -> opt out all of them (opt_out rules), status 4.
  - exemptions (status 2) are an operator decision only, via the CLI.
- Screening new records: `authorize()` gains a DROP check: a row whose email/phone DROP-hash is
  a listed identifier (not removed) is WITHHELD (REVIEW-free: DROP identifiers are exact).
  Read paths, send paths and writers already honour WITHHELD; the enforcer converges.
  Open: results.privacy_suppression_id needs a suppression row id (see Codex question).
- Beat: download every 7 days (well inside 45/30), match, report statuses that are decided,
  alert ops on any identifier undecided after 30 days or any download failure (the regs require
  written notice to CalPrivacy within 45 days of a failed automated connection: alert says so).
- CLI: `drop-status` (counts by status, oldest undecided), `drop-exempt --id ... --code`.
- Flags: `DROP_ENABLED` (default false), `DROP_API_KEY` (env only), `DROP_BASE_URL`
  (default production; sandbox for testing).

## Questions for the Codex consult
- How to represent a DROP-withheld row without a privacy_suppressions row (FK on
  results.privacy_suppression_id), or whether each matched identifier should become a rule.
- Unmatched identifiers: is a hash list of DROP Email/Phone hashes "the minimum necessary"?
- Performance: hashing every stored contact on every download (decryption cost).
- Is creating requests/rules from the worker (no operator) acceptable given 136's
  admin+MFA operator check, or should DROP have its own definer path?

## Codex consult (2026-11-09): plan NOT safe as written (3 Critical, 7 High, 3 Medium)
- A DROP hash (unsalted SHA-256) can never become one of our keyed-HMAC rules, and a
  DROP-only hold cannot use results.privacy_suppression_id (FK to a subject rule).
- One email/phone does not reliably mean one consumer; ambiguous matches go to review and are
  never reported "Deleted".
- Workers cannot impersonate 136's admin+MFA operator: a narrow DROP-specific definer path.
- Subject-based erasure (P6) does not cover "every record holding this identifier": a DROP
  identifier erasure is needed.
- Hashing every stored contact each download is too slow: maintained, indexed SHA-256 digest
  columns (or a digest table) on every contact write, backfilled once.
- Screening must happen before persistence and sale (every contact writer + authorize()).
- Status 3 only after the internal erasure barrier (second pass, artifacts); vendor and
  customer copies tracked separately; 202 upload is not "reported".
- Standardization: email removes ALL whitespace (Unicode) then lowercases; phone keeps all
  digits if fewer than 10; the spec shows both `Id,Hash` and `ID,Hash`: resolve with a fixture.
- The failure-notice duty is a counsel-configured workflow with an auditable record, not a log.
Codex's simpler design: ledger (downloads, identifiers, per-item deadlines, Removed); indexed
versioned digests maintained on writes; identifier->result match table; a dedicated DROP
withheld state on results (honoured everywhere); exact matches withheld before sale; an audited
DROP erasure for deterministic matches, operator review otherwise; statuses only after their
barrier. Scope comparable to Phase 6 P3-P6 together.

## Foundation built (2026-11-09, branch feat/privacy-drop) -- dormant
Owner's choice after the consult: "Foundation now, rest later". No change to how any lead
is read, sent, written, withheld or erased. Nothing runs unless `DROP_ENABLED=true` AND
`DROP_API_KEY` is set (both default off/empty).
- [x] `src/privacy/drop.py`: spec-exact Email/Phone standardization + `base64(sha256(utf8))`
      (None when a value standardizes to nothing); bounded in-memory ZIP parser (size,
      members, uncompressed bytes, rows, line length; refuses paths/traversal, nested
      archives, non-DROP names, two files of one DataType, bad header/Id/Hash, one Id with
      two hashes); header case-insensitive (`ID,Hash` / `Id,Hash`); Removed is
      `ID,Hash,ListType`; NDZ/MAID/NameVIN/CTVID counted and ignored; status CSV writer
      (`Id,Status`, codes 2-5, UTF-8, CRLF) and status file naming; `DropClient` for
      download/upload/amend (X-API-KEY, Accept, 200 ZIP / 200 JSON / 202, 429 and 5xx and
      timeouts -> `DropRetryLaterError`, other 4xx typed, redirects refused, proxies
      ignored, size-capped, base URL restricted to DROP production/sandbox, loopback http
      only in development/test environments). Never logs the key, an Id or a hash.
- [x] Migration 139: `drop_downloads` (+ `list_files`, the per-list CSV names the status
      file must reuse; a no-new-data pull is a row with NULL file_name, as cadence
      evidence) and `drop_identifiers` (CHECKed Id/hash shape, `due_at` = download + 45 d,
      unique (download, list, Id), index (list, hash)). RLS on, no table grant to any role;
      definer functions (bridgeleads_system EXECUTE only): `drop_begin_download`,
      `drop_add_identifiers` (<= 5000, idempotent), `drop_mark_removed` (list+Id+hash),
      `drop_last_pull`, `drop_pending_hashes` (keyset, for the report). Re-grant block in
      scripts/provision_rls_roles.sql. Downgrade refuses while ledger rows exist.
- [x] `src/workers/privacy_drop.py::pull_drop_lists`, beat `pull-drop-lists` DAILY with a
      7-day spacing (deviation from "weekly": a 202 or a failure is retried the next day,
      not a week later; the pulls themselves stay weekly). Records in one transaction;
      on any failure an ops alert (error class + HTTP status only) noting the possible
      written-notice duty to CalPrivacy (counsel to confirm), then re-raises.
- [x] `scripts/drop_match_report.py`: read-only sizing, counts only (pending, matched,
      unmatched, leads hit per list); privacy-suppressed leads excluded.
- [x] Tests: tests/test_privacy_drop.py, tests/test_privacy_drop_ledger.py,
      tests/test_privacy_drop_worker.py (real test DB, loopback HTTP server).

### Deliberately NOT built (needs the Codex design above, and counsel's D8)
- Withholding DROP-matched rows (a dedicated DROP-withheld state honoured on every read,
  send and writer path; screening before persistence and sale).
- Erasure of DROP-matched records (an audited DROP-identifier erasure; operator review for
  ambiguous matches).
- Status decisions and reporting (upload/amend of `Id,Status` files after each barrier;
  tracking of the 45-day due dates). NOTE: DROP answers 409 to a new download while the
  previous batch's statuses are unreported, so once a ZIP has been pulled every later
  pull fails with 409 and alerts until reporting exists. Do not enable before that.
- Digest columns maintained on every contact writer (the report hashes in batches instead).
- The failure-notice workflow itself (the alert is not the notice).

### Spec ambiguities found (v1.2.0)
- Download header: the schema says all list files use `ID,Hash`; uploads use `Id,Status`.
  Parsed case-insensitively.
- The upload example row `680,5` violates the spec's own 12-char Base62 WorkItemId; the
  writer refuses it.
- Upload CSV line endings are unspecified (CRLF used).
- "Whitespace" (email) is undefined: Python `str.isspace()` (Unicode whitespace; NOT
  zero-width U+200B/U+FEFF). Lowercasing is unspecified as ASCII or Unicode: full Unicode.
- "Digits" (phone) is unspecified: ASCII 0-9 only.
- Removed rows carry ID, Hash and ListType; whether Ids are unique across lists or
  downloads is unstated: removal matches list + Id + hash.
- 202 has no default Retry-After (60 s used); 429's example is 30 s (used when absent).
- Whether a 200 "no new data" counts as the 45-day "download": recorded as a pull.
- Upload 202 means queued for validation; row-level results come only by email/webhook
  notification, with no API to fetch them.
