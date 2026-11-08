# Phase 6 P7: activation (design sections 8, 10)

Owner approved starting P7 on 2026-11-08. Branch `feat/privacy-phase6-p7` (off origin/main fb0c9c63).
This file is the PLAN. Nothing in production changes until the owner approves it, and the
final switch (`PRIVACY_ENFORCEMENT_VERSION=1` on Railway) is a separate owner action.

## What exists today (verified 2026-11-08)

- Three locks keep every rule dormant (`src/privacy/activation.py`):
  1. DB: migration 125 `ck_privacy_suppressions_activation_guard` = `state = 'pending'`.
  2. Code: `IMPLEMENTED_ENFORCEMENT_VERSION = 0` (required 1).
  3. Env: `PRIVACY_ENFORCEMENT_VERSION = 0` on every Railway service.
- Readers already understand `pending -> active -> lifted` (`authorize.py:69`), the enforcer
  stamps by generation (count of active rows), the erasure driver (P6) acts on `privacy_erasures`
  rows and only for active, never-lifted deletion rules.
- NOTHING can write a privacy row: no role holds INSERT on the four 125 tables, no API, no script.
- `tests/test_privacy_script_inventory.py` arms with P7: CI fails while any of 33 listed operator
  scripts prints lead subjects without the privacy filter.
- No public intake (that is P8). Requests arrive by email/post and an operator records them.

## Decisions for the owner (recommendation first)

- **O1. How an operator records and activates a request.**
  - (a) RECOMMENDED: a CLI, `scripts/privacy_admin.py`, run with `railway run` as the migration
    role. Every invariant lives in DB functions (below), so the CLI is thin. No new HTTP surface.
  - (b) Admin API endpoints behind `require_admin_mfa` plus an admin page in the FE repo. Larger,
    and better built with P8's intake.
- **O2. The 33 gated scripts.** RECOMMENDED: delete the one-off diagnostics nobody imports
  (about 22, listed in P7a), and add the privacy filter to the ones still imported or tested
  (about 11). Deleting is reversible through git history.

**Owner answers 2026-11-08:** O1 = CLI first; O2 = delete unused, filter the rest.

**P7a classification** (kept = imported or tested by code, or named in a runbook/handoff as a
tool to run; the rest are referenced only by docs/comments):
- Filter (12): backfill_king_code_violation_owner, backfill_king_code_violation_mailing,
  backfill_assumed_mailing, backfill_pierce_statewide_mailing, backfill_property_situs_parts,
  king_rpacct_mailing, king_taxbill_mailing_check, repair_king_property_situs,
  release_stranded_dedup_claims, repair_nts_ts_number, verify_tracerfy_provenance,
  purge_test_batch_configs.
- Delete (21): diag_king_tax_owner_results, diag_preforeclosure_party_names,
  backfill_preforeclosure_party_names, diag_xcheck_followup, diag_xcheck_king_and_canary,
  diag_king_ts_repair_plan, diag_test7_rows, diag_test7_source_capture, diag_king_bad_parcel_rows,
  diag_probate_party_scan, repair_stale_absentee_owner, repair_placeholder_absentee,
  diag_nts_null_downgrade, diag_nts_pointer_integrity, diag_nts_recent_matches, diag_test11_dq,
  diag_match_key_collisions, diag_skip_trace_match_audit, verify_entitlement_deploy,
  diag_nts_stored_vs_source, rerun_pierce_address_recovery.
- Each delete commit = 3 scripts + the test list + the 08 inventory (5 files), so every commit
  keeps the list-equals-inventory test green. Retired names move to a "Retired in P7" note
  outside the gated section of 08.

## Design refinements while building (2026-11-08)
- Prod check (read-only, Supabase MCP): `postgres` has `rolbypassrls = true` and owns
  `privacy_erasures` and `results` (both FORCE RLS), so the CLI-as-migration-role can write them.
- REVIEW release must be visible to `authorize()` itself: the dispatcher and dialer call
  `authorize()` at send time instead of reading the flag. So 136 adds an append-only
  `privacy_review_releases(rule_id, result_id, actor)` and a 129-style definer reader for the
  worker role; `RuleSnapshot` loads it and a REVIEW whose every candidate rule is released for
  that row id becomes ALLOWED. WITHHELD (exact) is never releasable: re-enforcing re-flags it.
  "Confirm" needs no operation (a flagged row stays flagged; erasure never touches REVIEW).
- Every write function checks the actor is an admin with MFA enabled (`users.is_admin`,
  `users.mfa_enabled`), and records it: `privacy_suppressions.actor_user_id` (new, NOT VALID
  CHECK so it binds new rows only).
- The DB cannot know the live key versions or the normalisation version. The CLI therefore
  activates, lifts and releases inside ONE transaction that also loads the post-change rule
  snapshot (`load_rule_snapshot`) and commits only if it loads: a rule the loader would reject
  can never be committed (generalises Codex's mailing Critical). 136 also adds a CHECK that a
  non-pending row's anchor is property/phone/email.
- Lift is done in SQL in one function: lifted rows, then unflag results (`privacy_checked_gen = 0`),
  drop King tombstones, clear NTS rule ids, returning the result ids; the CLI re-runs
  `enforce_rows` on them before commit.
- Queued work at activation: the CLI flags the rule's candidates at once (`enforce_rows`);
  queued Tracerfy rows and dialer sends are re-authorized at send time (P4) and withdrawn there.

## Slices (each: <= 5 files per commit, ruff clean, tests on TEST_DATABASE_URL, Codex GO)

### P7a. Clear the script gate (no behaviour change for the product)
- [ ] Classify all 33: imported or tested (filter) vs unreferenced one-off (delete). Evidence = grep.
- [ ] Delete the unreferenced ones, and remove each from `HIGH_SCRIPTS` and the 08 inventory in
      the same commit (the list-equals-inventory test enforces this).
- [ ] Filter the rest: their result reads go through `actionable_sql`/`not_privacy_suppressed`.
      Shared-store reads (King, NTS) skip tombstoned/inactive rows.
- [ ] Positive control still trips (`test_the_gate_would_fail_if_armed_today`).

### P7b. Migration 136: the write side and the new guard
- [ ] Replace the CHECK with `state IN ('pending', 'active', 'lifted')`.
- [ ] Owner-only functions (EXECUTE revoked from PUBLIC and every runtime role; no runtime role
      gains any privilege, so 134's API-role audit stays clean):
  - `privacy_record_request(kind, channel, deadline_at, email_hmac, contact_enc, details_enc)`
    -> request id + `received` event.
  - `privacy_record_event(request_id, event_type, verification_level, decision_code, deadline_at, actor_user_id)`
    (seq = max + 1 under the request's row lock).
  - `privacy_add_rule(request_id, anchor_kind, anchor_hmac, subject_kind, subject_hmac, key_version, normalization_version)`
    -> rule id, state `pending`. Refuses a request that is not `verified`.
  - `privacy_activate_rule(rule_id, actor_user_id)`: only from pending, never twice, never after
    lift; writes the `active` row(s) for every version row of the rule, a `decided` event, and for
    a `delete` request the `privacy_erasures` row, all in one transaction.
  - `privacy_lift_rule(rule_id, actor_user_id, decision_code)`: only from active.
- [ ] Downgrade refuses while any non-pending row exists.

### P7c. The operator CLI (if O1 = a)
- [ ] `scripts/privacy_admin.py` subcommands: `record`, `verify`, `candidates`, `add-rule`,
      `activate`, `lift`, `status`. Subjects and anchors are built with `src/privacy/subjects.py`
      and `keys.py` (the same code the enforcer uses), never typed in as HMACs.
- [ ] `candidates` prints counts and result ids only (WITHHELD vs REVIEW), never names, so the
      CLI is not itself a new PII emitter.
- [ ] `activate` calls `assert_activation_allowed()` first; the DB function is the second check.

### P7d. Raise the code key
- [ ] `IMPLEMENTED_ENFORCEMENT_VERSION = 1`; update `test_privacy_activation.py`.
- [ ] Pre-raise audit, layer by layer against design section 6, with file:line evidence in this
      file (read paths, send paths, write paths, enforcer, erasure, scripts).
- [ ] End-to-end test on the test DB: record -> verify -> add rule -> activate -> enforcer flags
      the row -> every reader hides it -> erasure driver blanks it (delete) -> lift -> rows return
      (opt-out only; erased data does not come back).

#### Pre-raise audit (2026-11-08), design section 6 layer by layer
| Layer | Where (evidence) | Shipped |
|---|---|---|
| Authorization service | `src/privacy/authorize.py` `authorize()`, fail-closed loader (129 reader) | P3 #597, P7c releases |
| Storage flag | `results.privacy_suppressed_at` / `privacy_suppression_id` (126), `privacy_checked_gen` (130) | P3 #594, P5 |
| Read paths, every tab | `src/api/lead_actionability.py` `not_privacy_suppressed_*`; 07 inventory gaps 1-8c closed, 9 optional (claim re-checks) | P3 #595/#596, P5g |
| Dialer outbox by id | `src/workers/dialer_outbox.py:211`, `webhook_delivery.py:597`, `scheduler_helpers/dialer.py:305` | P4 #601 |
| Tracerfy dispatcher | `src/workers/skip_trace_dispatcher.py:1497` (rules before any claim) | P4 #601 |
| Tracerfy ingest quarantine | `src/workers/tracerfy_ingest.py:583` | P4 #601 |
| Pre-insert filter | `src/workers/tasks.py:1190` | P5 #603 |
| Shared-store writers | `owner_recovery.py:532` (King tombstone), `nts_crawler.py:700` (`_upsert_notice`) | P5 #606 |
| Recovery writers, late delivery, cache copies | guarded UPDATEs; `cv_late_delivery.py:463` | P5 #607 |
| Enforcer sweep | `src/workers/privacy_enforcer.py` (beat) | P5 #603 |
| Erasure | migration 135, `src/privacy/erase.py`, `src/workers/privacy_erasure.py` | P6 #610 |
| Operator scripts | 21 retired, 12 filtered; gate armed and green (`test_privacy_script_inventory.py`) | P7a |
| Activation, lift, review | migration 136, `scripts/privacy_admin.py` | P7b/P7c |
| Activation withdraws queued work | `privacy_admin._withdraw_queued`: the dispatcher's `_cancel_undeliverable_queued` (queued lookups of withheld leads) + the rule's unsent dialer rows skipped; the dispatcher's own tick and the outbox's send-time check stay as backstops | P7c (Codex P7c/d review, High) |

`IMPLEMENTED_ENFORCEMENT_VERSION` has no runtime reader other than `assert_activation_allowed()`,
which only the CLI calls: raising it changes no worker or API behaviour. `PRIVACY_ENFORCEMENT_VERSION`
stays 0 in every environment until the owner sets it.

### P7e. Release (standing Phase 6 rule: merge when CI green AND Codex GO)
- [ ] Merge, verify Railway: `Running upgrade 135 -> 136`, api/worker/beat SUCCESS, /health 200.
- [ ] STOP. Report to the owner. Setting `PRIVACY_ENFORCEMENT_VERSION=1` on api, worker and beat
      is the owner's call (lock 3). Until then the CLI can record requests and pending rules only.

## Gaps found while planning (Claude, 2026-11-08)
- Nothing ever clears a flag: `enforce.py:19` "the flag is never cleared here; lifting is P7".
  The lift must unflag rows whose `privacy_suppression_id` is the lifted rule's active row, and
  reset their `privacy_checked_gen` to 0 so the enforcer re-decides them (another rule may still match).
- REVIEW matches are flagged exactly like WITHHELD (`enforce.py:5-6`). Without an operator
  decision they stay hidden forever. P7 needs a review step: `candidates` lists REVIEW rows by
  id, and the operator either confirms (extends the rule) or releases the row. Erasure still
  never touches REVIEW (design section 5).

## Codex consult (2026-11-08), reconciled. All adopted into the slices unless noted
| Codex finding | Sev | Verified | Change |
|---|---|---|---|
| `mailing` anchor allowed by 125 but the loader raises on it (`authorize.py:181`): one activation breaks rule loading everywhere | Critical | yes | 136: `privacy_add_rule` refuses anchors other than property/phone/email |
| No REVIEW resolution path | High | yes (same as mine) | `privacy_resolve_review(result_id, rule_id, decision)`: `confirm` keeps the flag; `release` clears it and records a per-row release that the enforcer honours, so the next tick does not re-flag it |
| Activation does not withdraw queued work (design section 6) | High | yes | after activate, the CLI runs the enforcer for the rule's candidates at once and withdraws unsent pending skip-trace rows and dialer outbox rows for them; send-time checks stay the final guard |
| `add_rule` must refuse access/correct requests | High | yes | only `opt_out` and `delete` |
| Lift must lift every key-version row of the rule, under a lock | High | yes | lock all rows of `rule_id`; one `lifted` row per active version |
| Activate/lift preconditions race without locks | High | yes | `SELECT ... FOR UPDATE` on the request and every row of the rule |
| `record_event` must validate the lifecycle | High | yes | refuse events after `closed`/`withdrawn`, a second `decided`, `verified` before `received` |
| Positive-control test depends on scripts staying unfiltered | High | yes | the control uses a temporary unfiltered file, not the repo |
| Generation "decreases on lift" | (High) | **no** | it counts every `active` row ever written (`authorize.py:191`); a lift adds a `lifted` row and keeps the `active` one, so it never decreases. But a lift does not raise it either, hence the explicit unflag below |
| Lift must re-decide, not just clear (a row can match two rules) | High | yes | readers show a row the moment its flag clears, so lift clears the flag and re-runs `enforce_rows` on those ids against the post-lift rules in the SAME transaction |
| Deletion rules must never be lifted; erased rows never restored | High | yes | `lift` refuses `delete` rules |
| Lift does not restore King tombstones / NTS inactive rows | Medium | yes | opt-out lift: drop the King tombstone and reactivate NTS rows tied to that rule only; the next crawl or recovery refills |
| Script gate is a source-marker check | Medium | yes | accept for P7 (deleted scripts are the bulk); filtered scripts get one test each that runs their query on the test DB with a flagged row |
| `actor_user_id` is self-claimed in a CLI | Medium | yes | the CLI requires `--operator-email`, resolves it to an admin user with MFA enrolled, and refuses otherwise. Railway access stays owner-only. Admin API with MFA step-up is the P8 path |
| `privacy_erasures` insert must be idempotent, delete-only | Medium | yes | `INSERT ... ON CONFLICT (rule_id) DO NOTHING`, only for `delete` |
| DELETE privilege | Medium | n/a | 136 grants nothing to any role |

## Open questions for the Codex consult (answered above)
- Lifting an opt-out: who clears `results.privacy_suppressed_at`? (Enforcer re-decides by
  generation, but generation counts active rows, so a lift may not bump it.)
- Should `activate` refuse while the erasure driver is mid-run for the same rule? (It cannot be:
  the driver only sees rules that are already active.)
- Key rotation: a rule with rows under two key versions activates all of them atomically.

## Review
- P7a, P7b, P7c, P7d built and committed; Codex: consult reconciled, P7a/P7b NO-GO -> fixed,
  P7c/P7d NO-GO (queued-work withdrawal) -> fixed, round 2 GO (one Low, the 08 doc, fixed).
- Rejected with evidence: rotation rows sharing HMACs; generation decreasing on lift.
- Tests: every new/affected privacy and script test green locally; full suite run before push.
- Left for the owner: merge verification, then `PRIVACY_ENFORCEMENT_VERSION=1` per environment
  (lock 3), the legal mailbox, counsel review of D1-D9. P8 needs approval.
