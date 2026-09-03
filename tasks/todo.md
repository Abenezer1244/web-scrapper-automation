# Plan — backfill convergence + real property_state (post-#188 cross-check)

Branch `fix/backfill-convergence-and-situs-state` off `origin/main` @ `1b964d9`.
Found by cross-checking the #188 build against its own production evidence, not by a
new user report. Codex consulted on the design before any code was written.

## What the cross-check found (all MEASURED against prod, not assumed)

1. **The backfill does not converge.** 6 `--apply` runs of rule K wrote 180 rows but touched
   only **39 distinct ids**; 23 ids appear in all 6 runs. When the assessor's real mailing IS
   the property (owner-occupied), the write leaves `mailing LIKE property || '%'` still true,
   so the row stays a candidate and the `ORDER BY created_at,id` + `[:30]` head never advances.
   The handoff's "repeat until candidates: 0" can never terminate.
2. **The handoff's remaining-work figure is wrong.** Not "217 candidates, 180 done, ~37 left".
   Measured now: **King 847 candidates, only 30 stamped → ~817 actually remain.**
   Pierce 1218 candidates / 1175 stamped → 43 remain. Snohomish 0.
3. **The backfill NULLs `property_state` on every row it touches.** It calls
   `compute_owner_flags(r.property_address, ...)` with no structured situs, so the state is
   parsed from the FROZEN street-only line and always comes back NULL — which also forces
   `out_of_state_owner` NULL. Blast radius: **1,286 prod rows** (1,229 pierce_county_gis,
   39 king_assessor_tax_bill, 18 none_no_source) all `property_state IS NULL`,
   `out_of_state_owner IS NULL`. The query is `sc.state='WA'`-scoped, so the state is knowable
   with certainty. This defeats the stated goal of audit item 4.
4. Codex P2 confirmed in code: `enrichment_data` is `Column(JSON)` (models.py:659), not JSONB —
   the `||` merge needs an object guard and an explicit cast back to `json`.
5. Codex P3 (`LIKE` treats `%`/`_` in property_address as wildcards) is real but has
   **zero** live impact: 0 of 23,284 property_address values contain either character.

## Codex findings ADOPTED
- [P1] Terminal/retry stamp so skips cannot pin the LIMIT-30 head (`mailing_backfill_status`).
- [P1] Keep `mailing_source` = provenance only; workflow state gets its own key.
- [P2] `jsonb_typeof` object guard + explicit `::json` cast back on the merge.
- [P2] Stronger write guard + require `rowcount == 1`, count conflicts separately.
- [P2] `property_state` from `sc.state`, never parsed from the street-only line.
- [P3] Replace `LIKE` with a metacharacter-free prefix comparison.

## Codex finding REJECTED, with evidence
- Codex's *stricter* predicate (require `=` or a `,` delimiter after the street) **drops 9 real
  candidates** in prod — including `'20508 ISLAND PKWY'` vs `'20508 ISLAND PKWY E, LAKE TAPPS…'`,
  which is precisely the truncated-situs case Test 1 defect #3 was about.
  Measured: LIKE 20,277 / STRICT 20,268. Adopting only the equivalent form:
  `left(upper(mailing), length(property)) = upper(property)` → 20,277, **0 lost / 0 gained**.

## Todo
- [x] 1. `_CANDIDATES`: metacharacter-free prefix predicate (proven equivalent) + `btrim <> ''`
      + exclude rows already carrying a terminal `mailing_backfill_status`.
- [x] 2. `decide()`/main: every selected row leaves a durable state — resolved / confirmed_same /
      not_found / retry_later; run-level ABORT stays for source-health (never stamp 30 retries
      when King is globally blocked).
- [x] 3. `_UPDATE`: object-guarded jsonb merge cast back to `json`; guard on
      `property_address` too; require `rowcount == 1`, report conflicts.
- [x] 4. Pass the real `property_state` (`sc.state`) + structured situs parts into
      `compute_owner_flags` so item 4's whole point actually lands.
- [x] 5. Repair script/mode for the 1,286 already-stamped rows whose `property_state` was nulled.
- [x] 6. Unit tests for convergence (a confirmed_same row must NOT be re-selected), the
      predicate equivalence, the json merge guard, and the state fix.
- [x] 7. Codex adversarial review of the diff; resolve to consensus; ruff + full related suite.
- [ ] 8. 👤 Decide the King scope (817 rows ≈ 28 runs ≈ 2h of a source that has IP-blocked us).

## Review

Shipped in one commit. `scripts/backfill_assumed_mailing.py` + its tests only — no runtime
code, so nothing in the request path changes.

**Codex round 2 (adversarial review of the diff) — 5 findings, 4 adopted, 1 rejected:**
- [High] retry_later rows still pin the ordered head — **ADOPTED** (found independently at the
  same time): bounded `--max-attempts` (default 3), retries sort after untried rows, exhausted
  rows go `failed_terminal`.
- [High] the K global-abort was a result-SHAPE heuristic, so an all-absent batch could stall for
  ever — **ADOPTED**: 'found'/'none' now count as real answers, 'not_attempted' takes a bounded
  retry stamp, and only an all-transport-failure batch aborts — which now exits **2** so it can
  never stall silently.
- [Medium] jsonb `||` merges but does not delete, so a re-decided row kept a `mailing_source` it
  no longer had a claim to — **ADOPTED**: the merge drops the old provenance/error keys first.
  Verified on prod: after re-deciding to not_found, `mailing_source` is NULL.
- [Medium] --repair-flags compared only 2 of the 4 flags before skipping — **ADOPTED**: compares
  all four.
- [Critical] "compute_owner_flags does not accept the structured-situs kwargs" — **REJECTED,
  false.** It does (src/utils/address_intel.py, added by #188); Codex saw only the script diff.
  The call also executed successfully against prod. Verified before rejecting.

**Verification.** 67 tests pass, ruff clean. The unit tests assert on SQL *strings*, which
cannot catch a runtime SQL error, so every new statement was additionally executed against the
LIVE production DB inside a transaction that always rolls back: `_CANDIDATES` (1,218 Pierce
candidates), `_UPDATE`/`_STAMP` rowcount 1, the stale-address guard rowcount 0, `enrichment_data`
still `json_typeof=object`, stale provenance dropped, `_REPAIR` in scope — then ROLLBACK, with
0 stamps left behind (re-checked in a fresh session).

**Proof the core bug is fixed:** on a real prod row the flags went
`property_state NULL -> 'WA'` and `out_of_state_owner NULL -> False`.

**Not done / handed back:** the King run itself (847 undecided rows ≈ 29 paced runs) is a
resource decision for the user — see todo 8. Pierce (1,218) and --repair-flags (1,286) are cheap
and ready to run.
