# Test 7 data-quality audit (King WA probate, job f19f9cc5)

Branch: `fix/test7-data-quality`  ·  Worktree: `C:/Users/Windows/bridgeleads-worktrees/test7-dq`

## Verified root causes (live source + prod DB, 2026-09-03)

1. **`PUBLIC` in Party Name** — King LandmarkWeb indexes a death certificate's
   counterparty as the literal placeholder `PUBLIC` / `THE PUBLIC` / `PUBLIC THE`
   (101 of 204 raw rows in the Test 7 window have it in the GRANTEE slot). In 8 rows
   the recorder indexed the parties REVERSED, so the placeholder/agency sits in the
   GRANTOR slot and the DECEDENT in the grantee slot. `orient_probate_party` has no
   rule for `PUBLIC`, and its agency regex only matches the `DEPT OF HEALTH` word
   order — so `WASHINGTON STATE HEALTH DEPARTMENT`, `DEPARTMENT WASHINGTON STATE
   HEALTH` and `WASHINGTON STATE-GOVT` all reach `party_name`. Category C
   (semantic mapping defect), NOT a row/column shift: all 121 stored rows match the
   source on instrument, date, parcel, legal and doc_type.
2. **Missing Property Address** (result `45472c60`, parcel 3751604519) — King's own
   Site Address cell is empty; GIS reports `vacant_no_situs` (vacant single-family,
   ZIP 98001). Category B: source genuinely lacks it. Keep NULL.
3. **July 15 Health Department missing Mailing Address** (result `4eae622c`) — the
   recorder's legal text carries an 11-digit PID `64116000027` (King PINs are 10).
   eRealProperty SILENTLY TRUNCATES to the first 10 digits and serves a DIFFERENT
   parcel (641160-0002, owner SNYDER JACOB). So the lead got the WRONG property
   address and a wrong `assessor_current_owner`, and the mailing lookup found no
   tax account. Category A: application defect (trusting a truncating lookup).

## Plan

- [ ] Phase 1 — `src/scrapers/probate.py`: placeholder-party rule, wider agency
      word orders, `<STATE> STATE-GOVT`, and heirs sanitation. Tests.
- [ ] Phase 2 — `src/scrapers/enrichment/king_county_assessor.py`: verify the
      "Parcel Number" the assessor page echoes matches the PID we requested; gate
      BOTH `_fetch_king_owner` and `batch_enrich_king_county`. Tests.
- [ ] Phase 3 — King probate scraper: never ship a probate lead whose party_name
      resolved to nothing. Tests.
- [ ] Phase 4 — reusable repair script + apply to prod (party/heirs re-orientation,
      wrong-parcel address/owner clearing, cancel the 2 queued skip-trace rows).
- [ ] Phase 5 — Codex diff review, full test suite, E2E verification in the app.

## Codex consult (design, pre-implementation) — verified independently

- [P1] Echo verification must also cover `_fetch_king_owner` / `batch_extract_king_owners`
  (owner-only path + 2 backfill scripts). **CONFIRMED** by reading the code — same URL,
  no echo check. Adopted.
- [P1] Agency/placeholder values in the GRANTEE slot are left in `heirs` untouched when
  the grantor is person-like. **CONFIRMED** (`party, heirs = (g or None), (e or None)`).
  205 prod rows carry `heirs='PUBLIC'`. Adopted.
- [P1] Repair scope too narrow — skip-trace residue. **CONFIRMED, and worse than stated:**
  2 `pending_skip_trace_rows` are sitting in `status=queued` with
  `property_address='11524 MERIDIAN AVE N 98133'` — a stranger's house. If the dispatcher
  drains them, BridgeLeads pays Tracerfy for the wrong property and attaches that
  stranger's phone/email to the REINKE lead. Adopted; repair must cancel them.
- [P2] Reject non-10-digit King PIDs at extraction. **PARTIALLY adopted — documented
  disagreement.** Dropping `parcel_id` drops the whole lead (`if parcel_id:` gate), which
  would destroy a verified-real death-certificate record over a county typo. The brief for
  this task says missing source data stays null/empty, not that the record is discarded.
  Phase 2's echo check already removes the actual harm (wrong address, and therefore the
  skip-trace enqueue, which requires a non-null address). Keeping the row + provenance.
- Codex note adopted: the repair must pass the stored `doc_type` into
  `orient_probate_party` or it bypasses the Transfer-on-Death guard.
- Codex note adopted: parse the `Parcel Number` cell label-specifically, never "first
  10-digit number on the page".

## Progress

- [x] Phase 1 — `src/scrapers/probate.py` placeholder + agency word orders + heirs rule (`6c906c2`)
- [x] Phase 2 — King assessor parcel-echo verification, both call sites (`5d07415`)
- [x] Phase 3 — never ship a party-less probate lead (`6c60c3c`, extended to 4 more scrapers in `023daf0`)
- [x] Phase 4 — repair script + APPLIED to prod (`2898cd6`, `023daf0`, `cc4c843`, `0007fea`, `ecfd2ac`)
- [x] Phase 5 — Codex diff review (3 rounds), full suite, E2E in the live app

## Review

**Test 7 passes the data-quality audit.** All 121 leads re-verified against King County's own
systems after the repair.

| Field | Result |
|---|---|
| Source fidelity (instrument, date, doc_type, parcel, legal) | **121/121 exact**, 0 mismatches |
| Party name | 0 null, 0 placeholder/agency, **121/121 trace to a real source party** |
| Heirs | 0 placeholders; every non-null value traces to the source grantee |
| Parcel ID | 120 well-formed 10-digit King PINs; 1 preserved verbatim as the county printed it |
| Property address | 103 agree with King GIS, 16 agree with eRealProperty (condo units absent from the GIS layer), 1 legitimately NULL (vacant parcel), 1 correctly NULL (malformed county parcel). **0 disagreements** |
| Mailing address | 1 NULL (the malformed-parcel row); 103 owner-occupied, 16 absentee |
| Auction date / Default owed | 0 — correct: probate records carry neither at the source |
| Phone / Email | 0 — skip trace still queued (119 queued, 2 not_attempted) |

**Root causes, all three confirmed against the live source:**

1. `PUBLIC` = the King recorder's PLACEHOLDER counterparty on a death certificate (101/204 raw
   rows carry it in the grantee slot). In 8 rows the parties were indexed REVERSED, so it reached
   `party_name`. **Category C** — semantic mapping defect, not a row/column shift. Confirmed NOT a
   shift: all 121 rows match the source exactly on every other field.
2. Missing Property Address (result `45472c60`, parcel 3751604519) — King's Site Address cell is
   empty and GIS reports `vacant_no_situs`. **Category B** — the source genuinely lacks it.
3. July 15 Health Department (result `4eae622c`) — the county's legal carries an 11-digit PID;
   eRealProperty silently truncated it and served a DIFFERENT parcel. **Category A** — application
   defect. The lead had a WRONG property address, not merely a missing mailing one.

**Notable:** the wrong address had already enqueued 2 skip-trace rows against a stranger's house.

## Codex review — 3 rounds, 9 findings, all independently verified

Round 1 (design) 4 findings · Round 2 (diff) GATE FAIL, 5 findings · Round 3 GATE **PASS**,
2 findings · Round 4 GATE **PASS**, **0 findings** — confirmed both round-3 fixes and that the
repair script is safe against live customer data.

Adopted: 8 of 9. One [P2] declined with reasoning — rejecting non-10-digit PIDs at extraction
would DROP a verified-real death-certificate lead over a county typo, and the echo check already
removes the harm. 🔑 My own fix for finding #6 introduced finding #7 (the residue guard over-fired
on retained entity parties) — tightening one guard opened another.

## Not done / limitations

- The correct parcel for the July 15 lead is almost certainly `6411600027` (assessor owner
  REINKE NORMAN L, 11547 CORLISS AVE N — matching the decedent), but choosing which digit to
  delete from `64116000027` is a guess and `parcel_id` feeds the FROZEN `dedup_hash`. Left as the
  county printed it. **Recovering it is a human decision.**
- 3 non-probate rows (2 King parcels) keep an address obtained through the truncating lookup.
  Correct today (the assessor owner corroborates the lead's party) but unverifiable in principle.
- The 2Captcha key is dead (`ERROR_KEY_DOES_NOT_EXIST`). The King search still succeeded without
  a solved token, but the captcha path is unprotected.
- **No dead code was removed** — nothing in the touched paths was verified unused.
