# Duplicate-scope follow-ups — the three deferred Codex P2s

Branch `fix/dedup-collapse-p2s`, cut from `origin/main` @ `b511797`.
Continues `docs/HANDOFF-duplicate-scope-2026-09-08.md` §7A/§7B.

## Preconditions confirmed before any work

- [x] All 13 ledger invariants return 0 in production; `records_used` 1001/1000 unchanged.
- [x] §7B rev7 gate re-run and **GATE PASS** — all four questions answered
      independently this time, no P1. The hash-equality guard closes the
      weak-hash hole; under-collapsing is safe.

## The scope change: a latent defect in ALREADY-SHIPPED code

Codex's design review found that `collapse_same_run_siblings` (shipped in #265)
and the cross-job dedup pick their winner **independently**:

- the dedup claim's `first_result_id` is whichever row PostgreSQL happened to
  hit first inside the multi-row `ON CONFLICT ... DO NOTHING`;
- the collapse elects its survivor by an actionability/completeness ranking.

Nothing coordinates them, so the claim can name a row the collapse then flags
`is_duplicate` — which invariant #5 forbids, and which matters beyond the
invariant because `_reuse_enrichment_for_duplicates` joins
`results ro ON ro.id = dr.first_result_id` and copies address + settled
skip-trace PII **from** that row.

**Latent, not active:** production has **0** `same_run` rows — the collapse has
never fired on real data (matches §7D). Nothing is corrupted today. It fires on
the next run with same-run siblings, which the §4 audit says is common.

## Plan

### FIX 0 — the claim anchor follows the elected survivor
- [x] In the same transaction as the flag write, repoint
      `delivered_records.first_result_id` to the survivor whenever the current
      anchor is one of this job's losers. Scoped `user_id` + `dedup_hash`.
- [x] Applies to BOTH the existing collapse and the new reconciliation.

### FIX 1 — reconcile survivors AFTER inline enrichment (§7A.1)
- [x] New `reconcile_same_run_survivors(db, job_id, user_id, record_type)`.
- [x] Runs after `_run_inline_enrichment` + the NTS match and **before** the
      refetch/re-export — the only safe point, since the re-export and property
      membership both read from that refetch, and nothing else reads
      `is_duplicate` between enrichment and the plan cap.
- [x] Membership is READ from what exists (`duplicate_reason='same_run'` and
      `duplicate_source_job_id = this job`, plus the same-hash non-duplicate),
      never recomputed by `_collapse_loser_ids` — a row whose address enrichment
      rewrote drops out of that function's eligible set, and "not a loser" would
      then be misread as "winner".
- [x] Membership by `dedup_hash` is stable: the hash is computed once at INSERT
      and never recomputed (the premise the rev7 gate rests on).
- [x] Require **exactly one** existing non-duplicate member per group; a group
      with k != 1 is skipped and logged, never "fixed" — with k members the
      duplicate count would move by k-1 and billing with it.
- [x] Elect by the same ranking order applied to CURRENT values. The
      hash-equality admission test is deliberately NOT re-applied: it decides
      grouping, and grouping already happened.
- [x] `SELECT ... FOR UPDATE` on the group so two elections cannot race.

### FIX 2 — merge source-only facts onto the winner (§7A.2)
Elect first, merge second — ranking reads original row facts only.
- [x] `heirs`: case-insensitive deduplicated union, winner's names first,
      **excluding the winner's own `party_name`**, and only for record types
      where `heirs` is a multi-name list. Fill-only otherwise — for `divorce`,
      `heirs` is the OTHER SPOUSE, and two filings with reversed primary/
      secondary parties would otherwise union the winner's own party in.
- [x] `legal_description`: fill-only, never overwrite. Two different legals must
      not be concatenated into one authoritative-looking description.
- [x] `lead_subtype`: elected by the SAME priority order as
      `PROBATE_SUBTYPE_AGG_SQL`, lifted into ONE shared constant both the SQL
      and the Python read, so they cannot drift.
- [x] Every other `enrichment_data` key: deliberately NOT merged. Generic
      copying mixes two filings into an internally inconsistent object and can
      carry row-state such as the plan-cap exclusion key.
- [x] Merged fields provably do not participate in the ranking key — asserted by
      a test, so a future ranking change cannot silently break idempotency.

### FIX 3 — batch_export job-status filter (§7A.3)
- [x] `AND j.status = 'done'` on the jobs join in `_COMBINED_CTES` — serves both
      the emailed partial-batch CSV and the in-app combined download.
- [x] `done` is the only deliverable terminal status. Production holds only
      `done`/`failed`; the code also writes `cancelled` at force-finalize.
- [x] Per-child `record_count` -> 0 for TERMINAL non-done children; still
      counted for in-flight ones, where it is honest progress.
- [x] Rewrite the comment at `batches.py:622`, which justifies counting a failed
      child's rows *because* batch_export has no status filter. This change is
      what makes that premise false. Evidence it is then unreachable: segments
      and analytics already filter on done, and **0** non-done jobs hold an
      `export_key` in production (48/48 done jobs do), because `export_key` is
      written only inside the mark-done transaction.

## Verification
- [ ] `bash C:/Users/Windows/bl-testenv/run-full-pytest.sh <worktree>` — never bare pytest.
- [ ] Prove any failure against a clean `origin/main` worktree before believing it.
- [x] Codex diff review + challenge; any Critical/High = NO-GO.
- [ ] Re-run `scripts/diag_verify_repair_invariants.py` (13 checks, all 0).

## Review

Four fixes, not three. Codex's design review found that the collapse shipped in
#265 and the cross-job dedup elect their winner independently — the claim's
`first_result_id` is whichever row PostgreSQL reached first inside the batched
`ON CONFLICT DO NOTHING`, while the collapse ranks by actionability — so the
claim could name a row the collapse had just flagged `is_duplicate`. That is
invariant #5, and it also feeds `_reuse_enrichment_for_duplicates`, which copies
address and settled skip-trace PII FROM the anchored row. Confirmed **latent**:
production holds 0 `same_run` rows, so the collapse has never fired on real data.
Fixed in both collapse paths (FIX 0).

### What changed

| Area | Change |
|---|---|
| `tasks_helpers/dedup.py` | `survivor_sort_key` + `auction_survivor_sort_key` + `sort_key_for`; `_collapse_groups`; `_merged_survivor_fields`; `_repoint_claim_anchor`; `reconcile_same_run_survivors` |
| `workers/tasks.py` | passes `record_type` to the collapse; runs the reconciliation after enrichment and before the refetch |
| `trustee_sale_finalize.py` | `_sibling_groups`; anchor repoint + field merge; ranks via the shared auction key |
| `batch_export.py` | `j.status = 'done'` on the combined-export jobs join |
| `api/routes/batches.py` | `_child_lead_count` returns 0 for failed/cancelled children; the comment that justified the old behavior is rewritten |
| `utils/lead_export.py` | `PROBATE_SUBTYPE_PRIORITY` + `probate_subtype_rank`; the agg SQL is now built from them |

### Things that turned out not to be true

- **`_collapse_groups` cannot be reused for reconciliation.** Enrichment rewrites
  `property_address`, so a member stops satisfying the hash-equality admission
  test and vanishes from the result — and "not returned as a loser" is not
  "elected winner". Membership is read from the existing flags instead.
- **The reconciliation must not use one ranking for everything.** Auction Leads
  elects the soonest-auction row by a 2026-07-03 product decision. Ranking it by
  actionability silently overrode that. Caught by Codex in the diff review, after
  I had written a docstring claiming a single shared ranking and then broken it
  for exactly one record type.
- **`heirs` cannot be blindly unioned.** For `divorce` it holds the other spouse,
  and two filings can reverse primary/secondary. The union is gated to probate
  AND drops the survivor's own `party_name`.
- **An empty union is not "does not apply".** `_merge_heirs` returned `None` for
  both, so when the exclusion removed the only name, the fill-only fallback
  copied it straight back — making the survivor their own heir, the exact
  corruption the exclusion existed to prevent. Also Codex, also in the diff review.
- **The comment at `batches.py:622` was load-bearing.** It justified counting a
  failed child's rows *because* batch_export had no status filter. Closing that
  gap is what made it false, so it had to change in the same commit.

### Gates

- rev7 (§7B): **GATE PASS**, four questions answered independently, no P1.
- Design consult: two rounds, REVISE → all findings adopted.
- Diff review: **GATE PASS**, no P1; two P2s, both real, both fixed and
  re-confirmed by Codex.
- Codex could not verify whether any caller passes `record_type=None`; closed
  here — `ScraperConfig.record_type` is `nullable=False` and both call sites pass
  it. The default exists only to keep the pre-existing 3-arg test signature.

### Notes for the next session

- The local rig was shared with another session running integration tests, so
  this ran against an isolated `bridgeleads_p2s_test` DB on Redis db 15 rather
  than resetting the shared one.
- The harness memory watchdog killed several background test tasks while the
  underlying pytest kept running. A "killed" task here does not mean the process
  stopped — check for a stray `pytest` before relaunching.
