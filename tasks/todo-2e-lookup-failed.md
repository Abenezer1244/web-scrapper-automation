# 2e: Q4 (F-009) "Lookup failed"

Two phases, both small:
- **A (BE):** `AlreadyDeliveredContacts` gains a `removed` bucket for `purged` rows, which
  today land in `not_looked_up`. No migration.
- **B (FE):** one shared contact-status resolver for phone and email; types regenerated from
  BE main after A merges.

Branches: FE `feat/lookup-failed-2e` from `origin/master` (2b9ae40) in
`C:/Users/Windows/bl-fe-breakdown`; BE `feat/lookup-failed-2e` from `origin/main` in a new
worktree `C:/Users/Windows/bl-wt/lookup2e`. The BE plan copy goes in BE
`tasks/todo-2e-lookup-failed.md`.

## Goal
One lead's phone and email must never disagree about what the contact lookup did, and no
surface may claim "None found" or "not looked up" for a lead that was looked up. Today an
`errored` row shows Phone "Error" and Email "None found". "None found" claims the lookup
succeeded and found nothing (F-009).

## Facts (investigated 2026-10-01 UTC, BE `origin/main` 3d13939b; re-checked at aceeb2fa)
Re-check 05:10Z: BE main moved only by docs (#415), and none of the cited source files changed
(`git diff --stat 3d13939b aceeb2fa` over all of them = empty). FE master moved to 14089b2
(#177, #178); none of the plan's FE files changed. Rebase FE onto it before building.
- `results.skip_trace_status` has **7** values, not the contract's 6:
  `not_attempted | queued | submitted | hit | miss | errored | purged`
  (`src/config/constants.py` `SkipTraceStatus`; writers: `skip_trace_claim.py:750`,
  `skip_trace_dispatcher.py:929/1083/1098/1345/1468/1575/1728`, `tracerfy_ingest.py:911/973`,
  `enrich.py:2267/2311`, `retention.py` (`purged`)).
- `purged` = the row WAS a hit and the retention sweep deleted its contact PII
  (`schemas.py:1700`: "treat it as no contact data, not as never traced"). Its lists are
  NULL, so today BOTH cells say "None found": the same false claim as F-009. Retention ships
  OFF in code (`RETENTION_PURGE_ENABLED=False`); the prod value was not read (a prod read was
  refused in this session), so `purged` rows may or may not exist yet.
- The BE summary computes `not_looked_up` as a REMAINDER
  (`src/api/routes/jobs.py:735`: `already_delivered_count - sum(found, none_found, looking,
  failed)`), so every `purged` row is reported as "not looked up". Fixed in phase A.
- The 1b-2 lookup work (#406, #411) added NO results status. Its dispositions
  (`unmatched_unbilled`, ...) live on worker-only tables and are not on `ResultRow`.
  `last_trace_outcome` is still written by nothing and is not in the API.
- `errored` is never retried automatically and never quotable: the claim path takes only
  `not_attempted` (`skip_trace_claim.py:79`), and the planner refuses every non-
  `not_attempted` status (`contact_lookup_planner.py:132`). Whether an errored lookup was
  charged is unknowable today, so the copy must not say either way.
- Contact writers: Tracerfy ingest writes scalars and arrays together
  (`tracerfy_ingest.py:905-911`); the cache copy (`enrich.py:2304-2311`) copies
  `cached.phones/emails` verbatim and derives `hit` from the SCALARS, so a `hit` row can carry
  a scalar with an empty or NULL array (an older cache entry). A `miss` never carries a
  scalar from any writer.
- Only `ResultRow` carries `skip_trace_status`. It renders through `PhoneCell` and
  `EmailCell`, used by `ResultsTable` (desktop) and `LeadCards` (mobile). Batch leads and
  Segments carry only scalar phone/email and show a neutral "N/A".
- `batches/[id]/page.tsx:373` says "Contacts (phone/email) keep filling in" unconditionally,
  although a batch can run with skip trace off and failed/removed contacts never fill in.
- `lib/types.ts:269` `SkipTraceStatus` is dead (no importer) and lists 6 values.

## Design

### Phase A (BE)
- `src/api/schemas.py` `AlreadyDeliveredContacts`: add `removed: int = 0  # 'purged': looked
  up, contacts later deleted for age`, with the field description; fix the `not_looked_up`
  comment (drop "or purged").
- `src/api/routes/jobs.py:705-735`: add a `removed` bucket (`purged`) and count
  `not_looked_up` explicitly (its exact filter is below) instead of as a remainder (Codex r2 P2: a remainder files any future status as
  "not looked up"). New `unknown` field = `already_delivered_count` minus all six counted
  buckets (statuses this code does not know). Additive fields; the `user_id` filter and the
  single statement are unchanged.
- ONE contact-presence predicate, defined once in `jobs.py` next to the buckets (Codex r4
  P1), copied from the existing canonical rule for these encrypted columns
  (`dialer_filters.py:32-40`: blanks are normalised to NULL at bind), but with an explicit
  whitespace class instead of `trim()`, because PostgreSQL `trim()` strips only spaces while
  JS `String.trim()` strips tabs and newlines too (Codex r5 P1). A value "has content" iff it
  contains a character outside `[ \t\n\r\f\v]`:
  `has_contact = coalesce(phone, '') ~ '[^ \t\n\r\f\v]' OR coalesce(email, '') ~ '[^ \t\n\r\f\v]'`
  (an ARE bracket with these escapes, bound as a parameter, never interpolated; null-safe,
  so its negation is exact).
  Buckets are DISJOINT: `found` = `hit` OR (`not_attempted` AND `has_contact`);
  `not_looked_up` = `not_attempted` AND NOT `has_contact`.
- Legacy answered rows (Codex r3 P1): `not_attempted` with `has_contact` counts in `found`,
  not `not_looked_up`. The FE uses the SAME row-level rule (see
  phase B), so a row whose cell shows contacts can never be summarised as "not looked up".
  Scalars only: `phone`/`email` are always the primary of their arrays (ingest and the cache
  copy write them together), and SQL cannot tell an encrypted `[]` from a non-empty array.
  A NULL presence test on these columns is established practice (`analytics.py:193`,
  `segments.py:250`, `dialer_filters.py:39`, `retention.py:97`); it is not a filter on a PII
  value.
- `src/api/schemas.py`: `unknown: int = 0  # a status this API version does not know`;
  `found` / `not_looked_up` descriptions updated for the legacy rule.
- `schema/openapi.json` regenerated (`.venv-schema`, per memory).
- Tests in `tests/test_skip_trace_already_delivered.py`, through the authenticated endpoint
  `GET /jobs/{id}/results?category=already_delivered` (Codex r2 P2):
  - an already-delivered `purged` row counts in `removed`, NOT in `not_looked_up`;
  - a `not_attempted` row with no contacts counts in `not_looked_up`; a `not_attempted` row
    with a legacy scalar `phone` (and one with only `email`) counts in `found`; a
    `not_attempted` row whose phone and email are legacy plaintext `''`, `'  '`, `'\t'`,
    `'\n'`, `'\r\n'`, `'\f'`, `'\v'` (raw SQL UPDATE, bypassing the bind; one row each) counts in `not_looked_up` and NOT in `found` (Codex r4 P1);
    a row with an unrecognised status
    (written directly, the column is a free String(16)) counts in `unknown`;
  - the seven buckets sum to `already_delivered_count`;
  - tenant isolation: another account's `purged` / `not_attempted` rows on its own job do not
    move this account's counts, and this account cannot read the other job (404).
  - RED on unfixed main (fields absent; `purged` lands in `not_looked_up`).

### Phase B (FE)
`app/(dashboard)/results/[id]/_components/ContactStatus.tsx` (new):
- `contactState(status: string, hasValues: boolean): ContactState`, pure:

  | status | values | state | shows |
  |---|---|---|---|
  | `queued` / `submitted` | any | `processing` | spinner + "Processing" (amber), existing batching title |
  | `errored` | any | `failed` | "Lookup failed" (red) |
  | `purged` | any | `removed` | "Removed" (muted) |
  | `hit` / `miss` | non-empty | `values` | the values |
  | `hit` / `miss` | empty | `none_found` | "None found" |
  | `not_attempted`, row has a scalar `phone` or `email` (legacy answered) | per channel | as `hit` | values, or "None found" for the empty channel |
  | `not_attempted`, no scalar on the row | any | `not_looked_up` | "Not looked up", existing `SKIP_TRACE_NOT_RUN_TITLE` |

  The legacy rule is ROW-level and identical to phase A's `found` predicate, so the cells
  and the summary always agree (Codex r3 P1). The resolver therefore takes the row's
  effective status: `effectiveStatus(row) = status === "not_attempted" &&
  (nonBlank(row.phone) || nonBlank(row.email)) ? "hit" : status`, where
  `nonBlank(v) = /[^ \t\n\r\f\v]/.test(v ?? "")`, the exact FE mirror of the BE
  `has_contact` class (Codex r4 P1, r5 P1). `channelValues` uses the same `nonBlank` for scalars and drops blank array entries.
  | anything else | non-empty | `values` | the values |
  | anything else | empty | `unknown` | neutral "N/A", no claim |

  "any" rows follow the contract literally: a status that says in flight, failed or purged
  wins over stale values.
- **Which values a channel has** (`channelValues(row, channel)`, status-aware, Codex r1 P2):
  - a non-empty array -> the array;
  - otherwise, the scalar ONLY when the EFFECTIVE status is `hit` (the cache copy can leave a
    paid `hit` with a scalar and an empty/NULL array; legacy answered rows map to `hit`);
  - otherwise none. So `miss` + `[]` + a stale scalar is "None found", honouring
    `[] = traced, none found` without hiding a paid hit.
- The cells render values ONLY when `state === "values"` (Codex r2 P2): a stale non-empty
  array on a `queued` / `errored` / `purged` row is never shown.
- `<ContactStatus state channel />` renders every non-value state, so phone and email cannot
  drift again. Titles (no em dashes):
  - failed: "The contact lookup for this lead did not complete. It is not retried
    automatically."
  - removed: "This lead was looked up, and its contact details were deleted after the
    retention period."
  - none_found: channel-specific, as today.
- `PhoneCell` / `EmailCell`: call `channelValues` + `contactState`, render values or
  `<ContactStatus>`. Email "Pending" becomes "Processing" with the spinner (contract: one word
  for both channels). Phone "Error" becomes "Lookup failed".
- `lib/types.ts`: replace the dead 6-value `SkipTraceStatus` with the 7-value union, used by
  the resolver (the API field stays `string`; unknown values hit the fallback).
- `DeliveredLookupSummary.tsx`: render `removed` as "N contacts removed after the retention
  period" and `unknown` as "N with an unrecognised lookup status" (neutral, no claim); include
  both in the "anything to report" guard.
- Types (Codex r2 P3): after phase A merges, take its merge SHA with `git rev-parse`, then
  `git -C <BE wt> show <SHA>:schema/openapi.json > tmp.json;
  npx --no-install openapi-typescript tmp.json -o lib/api-types.generated.ts`. Before the FE
  merge, run the same against the CURRENT `origin/main` as the drift check; if main moved the
  schema, regenerate from it.
- `batches/[id]/page.tsx:373`: replace "Contacts (phone/email) keep filling in, so re-download
  later for the latest." with "Download again later for the latest contact details." (no
  claim that they WILL fill in).
- No retry affordance (contract: not until `last_trace_outcome` can rule out re-buying a
  charged lookup).
- `phase-3.0-contracts.md` Q4: rewrite the whole section to match the resolver (seven values
  in "Today", `purged`, legacy `not_attempted` with values, the unknown fallback, the
  status-aware scalar rule, the `removed` summary bucket), mark SHIPPED, and update the Q4 row
  of the summary table.

Files: A = `schemas.py`, `jobs.py`, `openapi.json`, one test file (+ BE plan copy).
B = `ContactStatus.tsx` (new), `PhoneCell.tsx`, `EmailCell.tsx`, `DeliveredLookupSummary.tsx`,
`batches/[id]/page.tsx` (code, 5) + `lib/types.ts`, `api-types.generated.ts` (types) +
contracts doc. B is over the 5-file guideline only by the two type files and a doc.

## Owner questions (defaults in bold, proceed with them unless overridden)
1. Retry on "Lookup failed"? **No** (contract).
2. `purged` copy: **"Removed"** with the retention title. Rejected: "None found" (false) and
   "N/A" (hides that a paid lookup happened).
3. `not_attempted` copy: **"Not looked up"** (contract) replaces today's "N/A". It is longer
   in every row of a skip-trace-off run; F-020/F-025 (item 3) own the N/A-volume problem.
4. Phase A is a BE deploy (Railway, quiet check, merge slot). **Do it** rather than drop
   `purged` from scope.

## Verification
- [x] A: migrate the test DB 107 -> 108 first; new test RED on main, GREEN on branch; full
      suite in 8 parts (one background job each, exit files); security review x2; Codex diff
      `origin/main...HEAD` GATE: PASS; quiet.py = 0; merge; Railway SUCCESS on the merge SHA,
      `/health` 200, worker logs clean.
- [x] B: `npx --no-install tsc --noEmit`, `npx --no-install eslint <changed files>`,
      `npm run build`; types drift gate regenerated from BE main.
- [x] B: stub API + Playwright (rig from session 81fad222 `fe2d/`): one run with a row per
      state (7 statuses x values/empty; `miss` + `[]` + stale scalar; `hit` + `[]` + scalar;
      `hit` + NULL arrays + scalar; `not_attempted` + NULL arrays + scalar (legacy), phone-only
      and email-only; `not_attempted` with blank / whitespace-only scalars, one each of
      space, tab, newline, CRLF, form feed, vertical tab ("Not looked up");
      `queued` / `errored` / `purged` each with a stale non-empty array, which must NOT show;
      one unknown status; every case for phone AND email), at 1440 (table, wait on `tr` hasText) and 390 (cards). Assert each
      phone AND email cell text and title; assert phone and email show the same state word on
      every non-value row; assert the summary line names `removed`. RED first: the same
      assertions against `origin/master` fail on the `errored` email and `purged` rows.
- [ ] Real-row check (needs owner OK for a read-only prod query: counts by
      `skip_trace_status`, then one real `errored` row in the app). If none exists, say so and
      rely on the stub, labelled as such (UX-AUDIT F-009 verification clause).
- [x] B: Codex diff review on `origin/master...HEAD` until `GATE: PASS`; merge FE PR; Vercel
      status success for the merge SHA.
- [x] BUILD_JOURNAL entry (BE docs PR, merged when quiet).

## Todo
- [x] Codex PLAN review until `PLAN: GO` (r1-r5 NO-GO, r6 GO)
- [x] Owner confirms (2026-10-01, in session: "start", i.e. the four defaults above)
- [x] A: BE bucket + test (RED, GREEN) + openapi, gates, merge, verify (#420, 6d32c8e2)
- [x] B: `ContactStatus.tsx` + types; wire PhoneCell / EmailCell; summary; batches copy
- [x] B: contracts doc
- [x] B: gates + stub proof (RED on master, GREEN on branch)
- [x] B: Codex diff GATE: PASS, PR, merge, Vercel verify (bridgeleads-web #182, 4a4f5248)
- [x] Journal (this PR)

## Review (shipped)
Full review: bridgeleads-web `docs/ux-audit/todo-2e-lookup-failed.md` "Review", and the
2026-10-01 BUILD_JOURNAL entry. Verification items above: A done (the plan said migrate to
108; main had reached 109 by then, so the test DB went 107 -> 109); B done; the real-row
check was NOT done (a prod read was refused in-session); journal in this PR.

Correction found by the journal fact-check: `purged` does NOT strictly mean "was a hit". The
sweep (`retention.py` `_ELIGIBLE`) purges any aged row with a contact column set, outside
queued/submitted, so a `miss` with `[]` arrays can become `purged` too. The UI's "Removed"
tooltip ("its contact details were deleted") is therefore over-specific for such a row.
Follow-up: neutral copy, e.g. "This lead was looked up, and its lookup data was deleted after
the retention period."

## Reconciliation with Codex plan r1 (NO-GO)
- [P1] `purged` vs `not_looked_up` in the summary: ADOPTED, Codex's first option (phase A).
- [P2] empty arrays vs scalars: ADOPTED IN PART. An array-only rule would show "None found" on
  a paid `hit` from the cache copy (`enrich.py:2304-2311` derives `hit` from scalars). The
  status-aware rule above gives `miss` + `[]` "None found" and keeps a `hit`'s scalar.
- [P2] polling is page-local for new leads (`results/[id]/page.tsx:163-180`): REAL, but older
  than 2e and not about how a status reads; recorded as a follow-up, not built here.
- [P2] batches copy: ADOPTED.
- [P2] contracts doc must be rewritten whole: ADOPTED.
- [P3] Segments desktop `??` vs mobile truthiness on empty email: deferred to item 3 (batch
  B-E), recorded below.

## Reconciliation with Codex plan r2 (NO-GO)
All five ADOPTED: explicit `not_attempted` count + `unknown` bucket (P2); NULL-array and
legacy-scalar cases (P2); values rendered only in state `values`, stale-array cases (P2);
authenticated endpoint test with tenant isolation (P2); types pinned to the phase A merge
SHA, then a separate drift check against current main (P3).

## Reconciliation with Codex plan r3 (NO-GO)
[P1] legacy `not_attempted` + values shown in the cell but summarised as "not looked up":
ADOPTED. One row-level predicate (`not_attempted` with a scalar phone or email = answered),
used by the BE `found` bucket and the FE `effectiveStatus`, plus endpoint and stub cases.

## Reconciliation with Codex plan r4 (NO-GO)
[P1] BE `IS NOT NULL` vs FE truthiness on blank legacy scalars: ADOPTED. One null-safe
`has_contact` (trim, from `dialer_filters.py`), disjoint `found` / `not_looked_up`, FE
`nonBlank` mirror, blank and whitespace cases in both test layers.

## Reconciliation with Codex plan r5 (NO-GO)
- [P1] PG `trim()` vs JS `trim()`: ADOPTED. One explicit class `[ \t\n\r\f\v]` on both sides
  (regex), with a raw-value case per whitespace character in both test layers.
- [P2] stale facts pin: ADOPTED. Re-checked against BE aceeb2fa and FE 14089b2; no cited file
  moved. The date is UTC (the review prompt's local date is 2026-09-30).

## Codex plan r6: PLAN: GO
"Round-5 regex and facts re-checks are consistent; no real defects found."

## Follow-ups (not 2e)
- BE: `last_trace_outcome` writers, then a retry for `not_submitted` only.
- FE: run-wide pending count for new-lead lookups (polling is page-local today).
- FE: Segments desktop email `??` -> truthiness, matching `SegmentCards`.

## Review
(after build)
