# Phase 6 P3: authorization service, read-path enforcement, dedicated flag, tab gaps

Branch `feat/privacy-phase6-p3` (BE). Design: `docs/legal/audit-2026-11/06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md`
sections 5, 6, 10 and 11 (D1-D9 adopted 2026-11-05, pending counsel).

**Nothing activates in P3.** The DB guard (`state = 'pending'` only) and
`IMPLEMENTED_ENFORCEMENT_VERSION = 0` stay until P7. No code in P3 sets the new flag, so every
read path returns exactly what it returns today; the change is a predicate that is always true
in production until P5/P7 write the flag.

## Code map (verified 2026-11-06)

Customer-facing reads that already go through the central predicate (`src/api/lead_actionability.py`):
- `actionable_condition` / `actionable_sql` / `is_actionable`: download, account export, segments,
  batch SQL, batch export, analytics, cv late delivery, R2 job export (`tasks.py` `is_actionable`),
  dialer, skip-trace dispatcher, contact-lookup planner.
- `shown_condition`: results list (lead tabs), tab counts, batch listing.
- `withheld_sql` / `withheld_condition`: anything that SPENDS money on a row.
- `deliverable_before_cap_sql`: what the plan cap ranks.

Gaps (do not use the central predicate):
- `no_address` and `not_qualified` tabs: `list_condition` in `src/api/results_category.py`, and the
  route skips `shown_condition` for them (`jobs.py:680`).
- Contact-lookup confirm proves quoted ids with raw SQL (`jobs.py:1797`), no actionability clause.
- Run breakdown (`src/api/run_breakdown.py`): ONE CASE; `new` is `actionable_sql` verbatim, so a
  row failing a new actionability clause falls to `unclassified` and blocks the snapshot.
- Download existence probe (`jobs.py:2636`, `has_address_condition`): returns no rows, only
  decides 404 vs header-only CSV. No change.

## Slices (each ≤5 files, verified, Codex-reviewed)

### P3a: dedicated flag + central predicate
- [ ] Migration 126: `results.privacy_suppressed_at timestamptz NULL`, `results.privacy_rule_id uuid NULL`,
      CHECK both NULL or both set. Nullable, no default: metadata-only ALTER on PG 11+.
      No FK to `privacy_suppressions` (rule_id is not unique there; rule rows are versioned).
- [ ] `Result` model: the two columns.
- [ ] `lead_actionability.py`: `not_privacy_suppressed_condition()` / `_sql(alias)` / Python twin.
      AND it into `actionable_*`, `is_actionable`, `shown_condition`, `deliverable_before_cap_sql`;
      OR it into `withheld_*` (suppressed = withheld, never bought).
- [ ] `results_category.py`: `list_condition` ANDs the not-suppressed predicate for EVERY category
      (closes the `no_address` / `not_qualified` gap). `download_rows_select` already uses `shown_condition`.
- [ ] Tests (real test DB): a suppressed row is absent from every list category, the CSV, the
      account-export select, `actionable_sql`, `is_actionable`; present-but-unflagged rows unchanged.

### P3b: remaining read gaps
- [ ] Run breakdown: suppressed rows are excluded from the partition (WHERE clause), so no bucket
      and no `unclassified`. Customer-visible counts then never imply a suppressed row exists.
      (Open question for Codex: exclude vs a dedicated bucket.)
- [ ] Contact-lookup confirm: the id-proof SQL adds `NOT suppressed`, so a suppressed row in a quote
      makes it `quote_stale` (nothing bought).
- [ ] Sweep the rest of the `results` readers in `src/api` for any row-returning path without the
      predicate; tests for each one changed.

### P3c: authorization service (no callers until P4/P5)
- [ ] `keys.py`: canonical forms for `property` (the pre-hash string of `compute_property_key`,
      `P|<STATE>|<county>|<parcel>` or `A|...|<addr>`) and `mailing` (normalised address, county-free),
      replacing `_no_canonical_form_yet`. Bump nothing else.
- [ ] `src/privacy/authorize.py`:
      - `load_rule_snapshot(session)`: ONE query; effective state per (rule_id, key_version,
        normalization_version) = `active` row present and no `lifted` row; indexed in memory by
        `(anchor_kind, anchor_hmac)` -> [(subject_kind, subject_hmac, rule_id)].
        Any load failure raises (operation retries); never "allow on error".
      - `authorize(snapshot, row) -> Allowed | Suppressed(rule_ids)`: computes the row's anchors
        (property, mailing, phone, email) and subjects (`subjects_from` over party/heir/owner/grantor
        fields with the source's name order) under every key version present in the snapshot.
        Automatic match only on exact anchor AND exact subject HMAC.
        Any `unresolved` subject on a row whose anchor matches a rule => `NeedsReview`, treated as
        suppressed for delivery (D2: whole row; fail closed).
      - Refuses to return `Allowed` for a snapshot with active rules unless `activation_allowed()`?
        No: snapshot loading is read-only; activation is guarded at write time. Note for Codex.
- [ ] Tests: synthetic rules inserted as `pending` are ignored; the effective-state logic is unit-tested
      on in-memory rows (the DB cannot hold `active` until P7).

## Risks
- Plan-cap re-runs: adding the predicate to `deliverable_before_cap_sql` means a row suppressed between
  two runs of the cap shifts ranks. Only matters once P5 writes the flag. Accepted: a suppressed row
  must never consume or hold quota.
- Snapshot counts frozen at delivery can differ from live counts after a later suppression. Accepted.
- Every predicate change is a no-op in production until the flag is written (P5/P7).

## Review
(filled in at the end)

## Codex plan consult (2026-11-06) and how it was reconciled

| Codex finding | Resolution |
|---|---|
| Critical: read-path inventory incomplete (batches counts, jobs stats, cv_late_delivery, cv_mailing_recovery, contact_lookup_action) | Bounded inventory first: `docs/legal/audit-2026-11/07-RESULTS-READ-INVENTORY.md`, every `results` read classified A-D with COVERED / GAP / DEFER-P4/P5. Enforcement slices are cut from it by surface. |
| High: excluding suppressed rows from the breakdown turns them into fake "dropped before save" | Dedicated bucket `privacy_withheld` and snapshot column `jobs.breakdown_privacy_withheld` (NULL on older snapshots reads as 0, the 121 rule). |
| High: snapshot compatibility underspecified | P3c names `SNAPSHOT_COLUMNS`, `_invalid`, `breakdown_from_job`, finalize done-CAS, schemas, completion text. |
| High: migration deploy order (API boot migrates while workers start) | 126 is expand-only and ships ALONE in its own PR; no code reads the columns until it has applied. |
| Medium: no FK; rule_id alone not auditable after rotation | The flag stores `privacy_suppression_id` = the concrete versioned rule row (`privacy_suppressions.id`), with a real FK (RESTRICT) and a both-or-neither CHECK. |
| High: property canonical input ambiguous | P3d: HMAC over the exact pre-hash string of `compute_property_key` (`P|STATE|county|parcel` or `A|STATE|county|address`), exposed by one shared function so the key and the anchor cannot drift. |
| High: effective-state rule underspecified | P3d: deterministic rule per (rule_id, key_version, normalization_version): pending < active < lifted, a later state wins, any other history (lifted without active, duplicate states) is a conflict that fails the snapshot load; loaded in one REPEATABLE READ statement. |
| Medium: fail-closed cases | P3d: an anchor match with an unresolved/unparseable subject, unknown key or normalisation version => NeedsReview (withheld); snapshot or key-library errors fail the whole operation. |
| Medium: deliverable_before_cap / withheld | Adopted as planned; cap rerun test before and after suppression. |
| Medium: slice boundaries | Slices below name exact files. |

## Revised slices
- [x] P3-0 migration 126 alone (+ test): PR #594, merged 9b89e430, applied on production 2026-11-06.
- [x] P3-inv read inventory doc (07-RESULTS-READ-INVENTORY.md).
- [x] P3a model + `lead_actionability` + `results_category` + tests (Codex: High on the breakdown, fixed by pulling P3c in; then GO).
- [ ] P3b gaps from the inventory, by surface (API counts/quote; worker emitters), ≤5 files each.
- [x] P3c run breakdown bucket, folded into dropped_before_save for customers (customer_view).
- [ ] P3d authorization service + property/mailing canonical forms + tests.

## P3d design (draft for Codex consult, 2026-11-06)

No caller until P4 (send paths) and P5 (sweep); nothing activates.

**Anchors.** Automatic matching only on anchors with a robust canonical form:
- `property`: the PARCEL branch of `compute_property_key`, exactly `P|<STATE>|<county>|<PARCEL>`
  (same normalisers and thresholds). `property_identity` exposes `property_key_input()` and
  `compute_property_key` becomes `sha256(property_key_input(...))` (byte-identical output,
  pinned by a test), so the anchor and the stored key cannot drift.
- `phone`, `email`: existing canonical forms.
- The ADDRESS branch (`A|...`) and `mailing` stay REFUSED by `keys.py` (its docstring's reason:
  a weak address form makes rules that silently never match). A row whose only anchor would be
  an address goes to operator review (design s5), never to an automatic decision.

**Snapshot** (`load_rule_snapshot(db)`): one SELECT over `privacy_suppressions`; history per
(rule_id, key_version, normalization_version) must be a prefix of pending -> active -> lifted:
`{pending}` inactive, `{pending, active}` ACTIVE, `{pending, active, lifted}` inactive; any other
set (lifted without active, active without pending) raises. An ACTIVE rule under a key version
that is not live, or a normalization_version other than the current one, raises (fail closed:
the operation retries; it never runs without a rule it cannot evaluate). Indexed in memory:
`(anchor_kind, key_version, anchor_hmac) -> [(subject_kind, subject_hmac, suppression_row_id)]`.
Zero active rules -> a fast path that computes no HMAC.

**authorize(snapshot, row, record_type, county, state) -> Decision**
(`allowed` | `withheld(suppression_ids)` | `review(reason)`; callers treat review as withheld):
1. Row anchors under every key version in the snapshot: property (parcel), each phone, each email.
2. No anchor matched: allowed.
3. Anchor matched: subjects from `party_name` (role party) and `heirs` (role heir) with
   `name_order_for(record_type, county)`. Any subject HMAC equal to a matched rule's subject:
   withheld (D2: whole row). Else any `unresolved` subject: review. Else allowed (the anchor
   matched, but the rule's person is not on this row).
4. Any key/normalisation error: raise.

### P3d Codex consult reconciliation
| Codex | Resolution |
|---|---|
| Critical: an address-only row (no parcel) is silently allowed | A SUBJECT match without an exact anchor match is `review` (withheld): covers address-only rows and a moved/mismatched parcel, without withholding every parcel-less row whenever any rule exists. Rule subjects are also indexed by (kind, version, hmac). |
| High: step 3 underspecified | anchor+subject => withheld (whole row, D2); anchor, no subject match, no unresolved subject => allowed; anchor + any unresolved subject => review; no anchor + subject match => review; neither => allowed. `property_all_persons` is not supported by the schema (scope is 'subject' only). |
| High: subject coverage | P3d reads `party_name` (party) and `heirs` (heir), and contacts from `phone`, `phones[*].number`, `email`, `emails[*]`. Owner-of-record / grantor fields have no generic Result column; an allowlisted row adapter for them is a **P5 prerequisite and an activation blocker** (P7 must not lift the guard without it). |
| High: effective state | Load ALL rows (no state filter) in one statement; validate each history is exactly {pending}, {pending, active} or {pending, active, lifted} with created_at order pending <= active <= lifted; anything else raises `RuleSnapshotError`. |
| High: property_key_input | Added; compute_property_key hashes it; byte-identical regression test. Missing state or county yields no anchor. |
| Medium: error boundaries | `RuleSnapshotError` (snapshot/key library: abort the operation) vs a `Decision` (allowed / withheld / review). |
| Medium: contacts | Exact anchor plus exact subject for automatic suppression; an unreadable contact is no anchor (the subject-match review path still applies). |
