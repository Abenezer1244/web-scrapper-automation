# HANDOFF — #187/#188 seam verification + two data-honesty fixes (2026-09-03)

Continues `docs/HANDOFF-test2-data-quality-2026-09-03.md`. Read that one first for the Test 2
audit, its seven dead ends and the environment cheat-sheet; everything there still applies.

**Branch `chore/verify-188-kingbudget`, worktree `C:\Users\Windows\bridgeleads-worktrees\test2-verify`,
5 commits, NOT PUSHED, NO PR.** Nothing has been applied to production.

> **UPDATE — both gates are now CLOSED.** The Codex gate ran on `30505ed` / `c553a4a` / `653111e`;
> its findings were verified, adopted or rejected on evidence, and landed in `fe6258b`. The 408-row
> repair has been **APPLIED and independently verified** in production. What remains is the push.
>
> 🛑 **But read `landmine_codex_exec_writes_to_your_worktree` first.** `codex exec` is a full coding
> agent with file-write and shell access: during that review it **edited 334 lines across 3 files in
> this worktree** rather than only reporting. It also caused 2 phantom pytest failures by writing
> while the suite was running. Invoke it READ-ONLY, and review anything it writes as a PR — the
> hardening it authored contained two real defects (see §4).

---

## 1. The question that started this: do #187 and #188 coexist?

**Yes on the code, no on the behaviour.** Answered by running, not by reading.

- Full suite on merged main `1b964d9`: **1854 passed, 2 skipped, 0 failed**; migration 085 applies
  cleanly. Rig reset first (PG dropped+recreated, Redis FLUSHALL, one proxy6543, no `railway run`
  active) because only the first run after resetting BOTH is trustworthy.
- #188 touched `enrich.py` purely additively (+46 lines) and the two changes write different fields.
- **But they interact through `compose_situs()`, and that was a real regression.** See section 2.

---

## 2. Fixed — the seam bug (`3d3a288`, Codex-reviewed)

`compose_situs()` assumed `property_address` is always street-only. True on the GIS path (parts are
parsed, then the address is REPLACED by the street-only assessor line). Not true on the two paths
#188 added:

- the King "Site Address" carries its own trailing ZIP (`2019 SW 318TH PL 4C 98023`) and #188 copies
  that ZIP into `property_zip` **without stripping it**; and
- `scripts/backfill_property_situs_parts.py:131` parses parts *out of* `property_address` via
  `("embedded", parts_from_line(r.property_address))`, then passes that same line back in.

Append-only compose produced `... 4C 98023, 98023`. The duplicated tail pushed the ZIP into the
parsed **street**, the street stopped matching the mailing street, and `absentee_owner` — a
user-facing filter — flipped **False to True on owner-occupied leads**.

**Fix:** compose is now *canonical* rather than append-only — parse the line, let whatever it already
carries win, let the structured parts fill only what is absent, then rebuild as
`street, CITY, ST ZIP`. Detection reuses `parse_property_for_display`, which only emits a VALIDATED
state and ZIP, so `98023 MAIN ST` is not read as carrying a ZIP nor `123 LAKE ST` a city — a
substring test would have got both wrong. #188's intended win (None to confirmed False) is preserved.
`property_address` is deliberately untouched: it is the frozen billing/matching key.

**Production impact: LATENT, no repair needed.** 26 rows show the duplication shape and **0** of them
change value. `property_city` / `property_zip` are set on **0** of 23,284 rows, so #188's structured
situs is still inert. The corruption would have fired on the next King tax job, or on the
not-yet-run phase-5 backfill.

---

## 3. Fixed — placeholder streets fabricating `absentee_owner` (`30505ed`, UNREVIEWED)

Found while cross-checking; unrelated to the seam.

The Snohomish tax bulk file encodes "no situs on file" as the literal word **UNKNOWN**, so
`property_address` reads `UNKNOWN UNKNOWN, GRANITE FALLS WA 98252`. The connector already guards this
exact hazard (`_has_street`, whose docstring warns that a city-only value "manufactures
confident-looking wrong signals") — but it tests for **emptiness**, and a placeholder WORD walks
straight through it. `UNKNOWN UNKNOWN` can never equal a real mailing street, so every such row
became a confident `absentee_owner = TRUE` about a property whose address we do not have.

Measured read-only in production, NOT assumed: **408 rows**, every one `snohomish/tax_delinquent`,
all TRUE; the entire street segment is UNKNOWN tokens in all 408 (runs of 1, 2 and 3); and **0** rows
carry UNKNOWN inside an otherwise real street.

**Fix:** `_addresses_differ` returns `None` when either side's street segment is nothing but
placeholder tokens. Narrow on purpose, twice over:

- **Only the STREET is judged.** In `UNKNOWN UNKNOWN, GRANITE FALLS WA 98252` the locality is real,
  so `property_state`, `owner_state` and `out_of_state_owner` keep their computed values. Only
  `absentee_owner`, the one flag that compares streets, becomes None.
- **Only the token actually measured.** `UNAVAILABLE` / `NONE` / `N/A` were considered and REJECTED:
  0 occurrences in production. Inventing an unmeasured guard on *this* connector is precisely the
  mistake that once false-aborted 14.79% of real Snohomish rows.

**Repair script (`653111e`): `scripts/repair_placeholder_absentee.py`.** Dry-run verified against
production with no writes: `{"candidates":408,"placeholder":408,"to_clear":408,"stale":0,"left_alone":0}`,
all snohomish/tax_delinquent. Guarded UPDATE, JSONL evidence per row, dry-run by default, and the
real decision calls the SAME `_street_is_placeholder()` the worker uses so script and worker cannot
diverge. **NOT APPLIED.** After the Codex gate:

    railway run --service worker python scripts/repair_placeholder_absentee.py --apply

**Deliberately NOT done:** the scraper still STORES the fake situs string. Blanking it at ingestion
changes `property_address`, the frozen billing key skip trace bills off, so that ingestion change is
drafted but held for the Codex gate rather than decided alone.

---

## 4. User decisions taken this session

> **§4.1 addendum — the two defects in Codex's ATIP hardening (fixed in `fe6258b`).** Both are the
> same over-broad-guard mistake its own comments warn about. (a) `_is_person_key` applied the
> address-component veto only to a bare `"name"`, so `owner_address` / `owner_city` /
> `taxpayer_addr` counted as PEOPLE — harvesting a STREET into the exclusion list and then deleting
> it from `mailing_address` (measured: mail became `"PUYALLUP, WA, 98372"`). (b) the excision used a
> bare substring test, so the name `LEE` was cut out of the street `LEELAND ST`, yielding
> `"123 LAND ST"` — a **fabricated address**, worse than the leak the guard exists to prevent. Now
> the veto covers every person token (`addressee`/`attn` excepted, since "addressee" contains
> "addr") and excision is word-boundary anchored. 4 regression tests pin both.

1. **Pierce ATIP — KEEP, plus a hard guard.** Done (`c553a4a`, hardened + corrected in `fe6258b`). The boundary previously
   held only because nothing happened to read `row["name"]`. Now `_assert_address_only()` fails
   loudly on any non-address key, and `_drop_person_line()` strips an addressee name from the mail
   block. Verified read-only: all 31 ATIP-enriched production rows carry a real street and no name,
   so it strips nothing today; it exists because an assessor mail block conventionally MAY lead with
   the addressee. **Still open, and not a code question:** the data CATEGORY is fine (addresses, the
   same class already stored from the GIS `Delivery_Address`), but the ACCESS METHOD solves a
   reCAPTCHA the county put on its own portal. That is a terms-of-use judgement for the user.
2. **King owner names — GATE LIFTED** by the user. Memory `project_king_owner_names_gate_2026_07_30`
   has been rewritten; older "stays blocked / stop chasing it" notes are STALE. See section 5.

---

## 5. King owner names — the real state (measured, and NOT what the old handoff says)

The previous handoff says "384 rows ship nameless". **The true scale is far larger.**

| Measure | Count |
|---|---|
| King `tax_delinquent` rows total | **34,552** |
| `party_name` NULL / placeholder | **32,648** |
| real owner name present | **1,904** (the partial backfill before the IP rate-block) |
| have `parcel_id`, so lookup is possible | 34,552 |
| have both property and mailing address | 18,212 |

Per job: `960abfdf` 2026-09-02 → 384 rows, 384 nameless · `33de90c8` 2026-08-10 → 18,214 rows, all
nameless · `b2c2cd68` 2026-06-23 → 15,954 rows, 14,050 nameless.

**There is NO code-level gate.** `_extract_owner_name()` already exists in
`src/scrapers/enrichment/king_county_assessor.py`, and `enrich.py` already swaps the owner into
`party_name` for tax_delinquent behind a dual gate (`record_type` belt + `is_tax_placeholder_party`
suspenders). The "gate" was the *decision not to run a backfill*. Lifting it therefore means running
a backfill of roughly 32,648 parcels against King eRealProperty — which is exactly the operation
that got **IP-rate-blocked** at 877/15,954 (`incident_king_assessor_rate_block_2026_07_30`: small
samples do NOT predict sustained rate, and it is resumable). **Design this with Codex first; it is an
operations problem at least as much as a coding one.** Do not start a bulk backfill while a
migration ALTERs `results` (`incident_backfill_blocks_migration`).

---

## 6. Previous handoff item 6 (King time budget in prod) — resolved as "still unverified", not broken

The latest King job `960abfdf` logs the OLD message, `Address enrichment failed — leads delivered
without enriched fields`, and wrote **0** `mailing_lookup_deferred` markers. That is **not** a failure
of the #187 fix: the job ran `2026-09-02 09:37 UTC` and #187 landed `2026-09-03 01:24 UTC`, about 16
hours later. **No King tax job has run since the deploy.** The next one is the verification —
expect `King County mailing lookup stopped early: N parcels requested, ...` and non-zero deferred
markers.

---

## 7. New dead ends and gotchas (add to the seven in the previous handoff)

8. **Codex ran its probes against the SHARED CHECKOUT**, which sits on the stale
   `feat/fields-output-visibility` (70+ commits behind). Its `parse_property_for_display` output
   contradicted the real one (`zip: None` versus `98023`) and it then reasoned from that. **Always
   re-verify a Codex measurement in your own worktree.** Its two substantive findings this session
   (a stale conflicting ZIP, and the `WY`-as-`WAY` street/state parse collision) were reproduced and
   are **pre-existing, not regressions** — absentee is True both before and after the change — and 0
   of 23,284 production rows parse to a non-WA state, so both were documented rather than
   speculatively "fixed".
9. **`results` has no `county` column.** Join `jobs` then `scraper_configs` for county / record_type.
10. Forgetting `TEST_DATABASE_URL` trips the prod-wipe safety abort. Set it *and* `DATABASE_URL`.
11. The `python - <<'PY'` heredoc still mangles backslashes (previous dead end 6): `\s` inside a
    non-raw patch string raises a SyntaxWarning, and a long markdown heredoc can fail to parse
    outright. Write patch/doc content with the Write tool, and verify a regex with `cat -A`.

---

## 8. Next steps, in order

1. ✅ **DONE — Codex gate** on `30505ed` / `c553a4a` / `653111e`, landed as `fe6258b`. Its most
   valuable finding was real and serious: `_enqueue_skip_trace_rows` gates only on
   `property_address IS NOT NULL`, and `address_cache_key()` hashes the ADDRESS — so
   `UNKNOWN UNKNOWN, UNKNOWN WA` (**328 distinct parcels**), `UNKNOWN, UNKNOWN WA` (48) and
   `UNKNOWN, WA` (25) each collapse to ONE cache key. A single Tracerfy result would have been
   copied onto all 328 unrelated leads. Nothing had been traced yet (all 408 `not_attempted`,
   0 phones), so the gate is preventative. Codex's 19 extra placeholder tokens were MEASURED at 0
   occurrences and rejected. Two defects in Codex's own hardening were found and fixed (§4).
2. ✅ **DONE — repair applied**: `updated: 408, stale: 0`, exit 0. Independently re-queried:
   0 rows still TRUE, 408 now NULL, `owner_state` kept on 407, `out_of_state_owner` kept on 30,
   `property_address` untouched on all 408, and the 2,432 non-placeholder TRUE rows did not move.
   Evidence JSONL in the scratchpad (`repair_applied.jsonl`).
3. **Push the branch, open the PR, confirm CI.** ← the next actual step.
4. Decide the held ingestion change: blank the fake Snohomish situs at source, or leave it.
5. Design the King owner-name backfill with Codex — scope is 32,648 parcels against a rate-limiting
   source, not 384.
6. Answer the ATIP access-method question (terms of use); no code change can settle it.
7. Still open from the previous handoff: items 3, 4 and 5 (ZIP-less Pierce match key, legacy
   `PRE-FORECLOSURE` doc_type, shared checkout stuck on a stale branch) plus 7 and 8.
