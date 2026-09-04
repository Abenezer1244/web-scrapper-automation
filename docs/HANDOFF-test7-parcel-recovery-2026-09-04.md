# HANDOFF — "Test 7" data quality + King malformed-parcel recovery (2026-09-04)

**Read this whole file before touching anything.** Two related bodies of work: one is MERGED and
DEPLOYED, the other is open in PR #208 with green CI. Production data has already been repaired by
both.

---

## 1. The goal

The user asked to investigate and fix the data-quality issues in the scraper result named
**"Test 7"** — King County WA `probate`, scraper config `test 7`, job `f19f9cc5-0f82-4b56-970c-f70de550f04e`,
121 leads over the window 06/04/2026–09/02/2026.

Three reported symptoms:
1. Several leads showed **`PUBLIC`** as the Party Name.
2. One lead had **no Property Address**.
3. The **July 15 "Washington State Health Department"** lead had **no Mailing Address**.

Standing rules the user set (and that still apply): do not guess; verify the live app, source,
scraper, DB, API and UI; fix root causes not symptoms; use Playwright/Chromium (**never** Claude in
Chrome); work only in an isolated worktree; **consult Codex as an independent reviewer and
independently verify its findings**; test every fix; remove only verified-dead code.

---

## 2. Where the work lives

| | |
|---|---|
| Worktree | `C:/Users/Windows/bridgeleads-worktrees/test7-dq` |
| Current branch | `fix/king-parcel-recovery-r2` (6 commits ahead of `origin/main`, working tree CLEAN) |
| Backend repo | `Abenezer1244/web-scrapper-automation`, base branch `main` |
| Scratchpad (evidence) | `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/2eebd364-c71d-4264-aa21-86f09ac855bd/scratchpad` |

**Local pytest rig** (required — the suite refuses to run without it):
```bash
source "<scratchpad>/testenv.sh"      # isolated DB bridgeleads_test7_test + redis db 12
python -m pytest tests/ -m "not integration" -q -p no:cacheprovider -o addopts=""
python -m ruff check src/ tests/ scripts/
```
`testenv.sh` is in the scratchpad. If it is gone, recreate per
`~/.claude/.../memory/reference_isolated_pytest_db_no_interference.md`. **Do not** reuse
`bridgeleads_test` — other sessions share it.

**Prod access:** `railway run python scripts/<script>.py` from the worktree (runs LOCALLY with prod
env vars; the worktree is already linked to service `worker`).

---

## 3. Current state

### 3a. MERGED + DEPLOYED (do not redo)

- **PR #202** → `b6e29ed`. The original Test 7 fix.
- **PR #207** → `302edd5`. Malformed-parcel recovery. Deploy pipeline green; Railway `api` +
  `worker` both redeployed; `api.bridgeleads.io/ready` returns 200.

### 3b. OPEN — PR #208, branch `fix/king-parcel-recovery-r2`

Six commits fixing everything Codex found in review rounds 2–6:

```
3699fe3 fix(repair): refresh the trace name payload when the party repair rewrites it
986adb5 fix(repair): the parcel repair must not touch the lead's name at all
a5aa640 fix(repair): recompute the trace payload names, and complete a half-fixed row
4ebc65a fix(king): parcel-recovery round-3 review findings
c5b9776 fix(repair): re-point a stranded trace even on an already-recovered row
ad3f875 fix(king): parcel-recovery round-2 review findings
```

**CI: Test SUCCESS, Dependency Audit SUCCESS.** Local: **2210 passed, 2 skipped, 0 failed**, ruff
clean. `mergeable` may read `UNKNOWN` briefly — that is GitHub recomputing after a push, not a
conflict. **Not merged.**

### 3c. Production data — already repaired

The 5 rows for instrument `20260715000926` (source parcel `64116000027`, all party
`REINKE NORMAN LEONARD`, across 5 jobs) now read:

```
party    = REINKE NORMAN LEONARD          (was WASHINGTON STATE HEALTH DEPARTMENT)
property = 11547 CORLISS AVE N 98133      (was 11524 MERIDIAN AVE N 98133 — a STRANGER'S house)
mailing  = 11547 CORLISS AVE N, SEATTLE WA 98133   (was NULL)
enrichment_data.resolved_parcel_id = 6411600027, resolved_by = gis_plus_owner_match
parcel_id = 64116000027  ← UNCHANGED, deliberately (see §5)
```
Both `pending_skip_trace_rows` are `queued` against the corrected address with
`name='NORMAN'/'REINKE'`.

Also applied earlier: **235 rows** re-oriented (25 party corrections, 235 heirs).

Verify any time with: `railway run python scripts/diag_king_bad_parcel_rows.py`

---

## 4. The three root causes (all proven against live sources)

1. **`PUBLIC` is the King recorder's PLACEHOLDER counterparty** on a death certificate — the
   instrument is recorded "to the public". It sits in the GRANTEE slot on **101 of 204** raw rows in
   one 90-day window; in 8 rows the recorder indexed the parties **REVERSED**, so it landed in the
   grantor slot and reached `party_name`. **Not a row/column shift** — all 121 rows match the source
   exactly on instrument, date, doc_type, parcel and legal.
2. **Missing Property Address** (result `45472c60`, parcel 3751604519) — King's own Site Address
   cell is empty; GIS reports `vacant_no_situs`. **Source limitation. Correctly NULL.**
3. **July 15 lead** — the recorder printed an 11-digit PID (`64116000027`; King PINs are 10).
   🛑 **eRealProperty SILENTLY TRUNCATES an over-length ParcelNbr to the first 10 digits and serves a
   DIFFERENT parcel with HTTP 200, no error.** The lead got a stranger's address, and the mailing
   lookup then found no tax account — which is all the user originally saw.

Also: the SAME agency appears under **three word orders** in one window
(`<STATE> DEPT OF HEALTH`, `<STATE> HEALTH DEPARTMENT`, `DEPARTMENT <STATE> HEALTH`) plus
`<STATE> STATE-GOVT`. Only the first was matched before.

---

## 5. THE LOAD-BEARING DESIGN DECISION

```
Result.parcel_id            = SOURCE identity. IMMUTABLE. Exactly what the recorder printed.
                              Feeds the FROZEN dedup_hash (billing) + source_fingerprint.
enrichment_data.resolved_*  = CANONICAL PROPERTY identity. Used for lookups + display.
```

**Never rewrite `parcel_id`.** Codex's deciding reason: `dedup_hash` is the frozen billing key and
`delivered_records` is keyed by it, so changing `parcel_id` turns a county typo repair into a
billing/idempotency migration — an already-delivered lead looks undelivered and can be **billed a
second time**. Codex listed 13 tables/columns that would have to move atomically.

---

## 6. Active files

**Shipped (merged):**
- `src/scrapers/probate.py` — SHARED BY 8 COUNTY SCRAPERS. Placeholder rule, agency word orders,
  `clean_counterparty()`, `_residue_is_a_party()`, `_agency_phrase_present()`.
- `src/scrapers/enrichment/king_county_assessor.py` — `parcel_page_is_for()` echo check,
  `_read_parcel_page()`, `resolve_malformed_parcel()`, recovery branch in
  `batch_enrich_king_county`.
- `src/scrapers/enrichment/king_parcel_repair.py` — **NEW.** The resolver.
- `src/workers/tasks_helpers/enrich.py` — King branch: `party_names`, write-back gating, provenance.
- `src/scrapers/king_wa_probate.py`, `clark_wa.py`, `templates/{acclaimweb,laserfiche_weblink,skagit_recording}.py`
  — drop a probate row with no party.

**In PR #208:** the same files plus
`scripts/repair_probate_party_and_bad_parcel.py` (the repair; dry-run by default, `--apply` writes,
JSONL journal, idempotent).

**Tests:** `tests/test_probate_party.py`, `test_king_probate_rows.py`, `test_king_assessor_owner.py`,
`test_king_parcel_repair.py`, `test_repair_probate_party_and_bad_parcel.py`.

**Diagnostics:** `scripts/diag_test7_*.py`, `diag_king_bad_parcel_rows.py`, `diag_king_odd_parcels.py`,
`diag_probate_party_scan.py`.

---

## 7. The resolver's guards (do not loosen without re-reviewing)

1. Fires only on a **confirmed** `parcel_page_is_for` mismatch, never a transient error.
2. **Deletion candidates only.** A substituted digit can be a REAL other parcel the county would
   honestly echo, so the guard cannot detect it.
3. Candidate must exist in King's **strict ArcGIS** layer. Zero survivors aborts — which is also
   what a GIS outage looks like, so a transient failure can only cost a repair, never buy a wrong one.
4. A lone survivor is proof **only if the candidate space was exhaustive** (11-digit).
5. Otherwise exactly one candidate must have an assessor owner naming the same PERSON as the lead's
   party, compared as **whole token sequences**, first two positions spelled out and equal.
6. Winner re-verified with `parcel_page_is_for`. Full provenance recorded.

**Negative control that must keep passing:** `64116000027` + party `LAROUX JOHN ALEXANDER` →
**ABSTAINS**.

---

## 8. Failed attempts / traps (read before repeating them)

- 🛑 **I shipped THREE bad name-matchers.** (a) A `(surname, first)` pair collapsed `VAN DYKE MARY`
  and `VAN DYKE JOHN` into the same person. (b) The replacement still matched `REINKE N L` to
  `REINKE NORMAN LEONARD` on initials alone. (c) I then reused `person_tokens()` as a surname
  splitter — which its own docstring says it is not — turning `VAN DYKE MARY` into
  `last='VAN' first='DYKE'`. **If you need a name split, use
  `src/scrapers/enrichment/skip_trace.select_traceable_owner()`. Do not write a fourth one.**
- 🛑 **My fix for one Codex finding repeatedly CAUSED the next.** Rounds 3→4→5 were each triggered by
  my own previous fix. Re-read the surrounding code after every tightening.
- 🛑 **A CONFLICTING PR runs ZERO CI** and reads as "untested", not blocked. PR #205 hit this because
  #202 was squash-merged while the branch kept its individual commits. Fix: cut a fresh branch off
  the new `main` and cherry-pick. **Never force-push / delete branches in this shared OneDrive repo.**
- 🛑 `main` moved **four times** mid-session (#201, #203, #200, #206). Always `git fetch origin main`
  and re-check mergeability before assuming CI ran.
- 🛑 The **2Captcha key is DEAD** (`ERROR_KEY_DOES_NOT_EXIST`). King's search still returns all rows
  without a solved token, so scraping is not blocked — but the captcha path is unprotected. 👤 needs
  rotation.
- 🛑 Bash **heredocs kept breaking** on quoting (`unexpected EOF looking for matching '`). Use the
  Write tool for any multi-line file, not `cat <<'EOF'`.
- 🛑 I wasted real context on **brittle test assertions**: `assert "x" not in sql` kept matching text
  inside SQL `--` comments. Strip comments before asserting, or assert on behaviour.
- ✅ Codex ran out of quota **three times**. A bounded background retry loop
  (`scratchpad/codex_retry.sh`) is the pattern that worked.

---

## 9. Codex review history

Design consults: 2 (party orientation; then the parcel A-vs-B design).
Diff reviews: rounds 1–6 on the parcel work — **FAIL, FAIL, FAIL, FAIL, FAIL, PASS**. Earlier Test 7
work: 4 rounds, ending PASS. Every finding was independently verified in the code before being
adopted; one [P2] was declined with written reasoning (rejecting non-10-digit PIDs at extraction
would DROP a verified-real lead).

**Round 6 returned GATE: PASS.** Its remaining [P2] — a stale trace-name payload after a party
repair — was fixed in `3699fe3` (the last commit) **and has NOT itself been Codex-reviewed.**

---

## 10. NEXT STEPS — start here

1. **Confirm PR #208 CI is green and merge it.** `gh pr view 208`; merge with `--squash`; do NOT
   pass `--delete-branch`. Then watch the `main` CI/CD run and confirm Railway `api` + `worker`
   redeploy and `api.bridgeleads.io/ready` returns 200.
2. **Optional: Codex round 7** on commit `3699fe3` only (the trace-name refresh). Prompt scaffold:
   `scratchpad/r6.txt` — adapt it. This is the only change in the PR without its own review pass.
3. **Re-run the repair after deploy** to confirm a clean no-op:
   `railway run python scripts/repair_probate_party_and_bad_parcel.py --apply`
   Expect `changed: 0, written: 0, already_recovered: 5, traces_repointed: 0`.

### Known limitations / open items (none blocking)

- ⏭️ **UI still shows the recorder's `parcel_id`.** Codex's recommendation: display
  `Parcel: 6411600027` with `Recorder PID: 64116000027` as provenance. `resolved_parcel_id` is
  already in the results payload. **Frontend change in the sibling repo `Desktop/bridgeleads-web`
  (branch `master`)** — not done.
- ⏭️ **3 non-probate rows** (2 King parcels, `012603938700` / `259900081003`) still carry an address
  obtained through the truncating lookup. Correct today, unverifiable in principle. The repair
  REPORTS them and refuses to clear them; `--record-types` widens the scope if a human decides to.
- ⏭️ Test 7 shows **120 of 121** rows in the UI. That is correct: `actionable_condition()` requires a
  property OR mailing address, and one row (parcel 3751604519, vacant) has neither.
- 👤 Rotate the 2Captcha key.

---

## 11. Final Test 7 audit result (post-repair, all 121 leads)

| Field | Result |
|---|---|
| Source fidelity | **121/121 exact** (instrument, date, doc_type, parcel, legal) |
| Party name | 0 null, 0 placeholder/agency, **121/121 trace to a real source party** |
| Parcel ID | 120 well-formed; 1 preserved verbatim as the county printed it |
| Property address | 103 agree with King GIS, 16 agree with eRealProperty (condo units are absent from the GIS polygon layer), 1 legitimately NULL, 1 recovered. **0 disagreements** |
| Mailing address | 103 owner-occupied, 16 absentee, recovered where available |
| Auction date / Default owed | 0 — correct, probate carries neither at the source |
| Phone / Email | 0 — skip trace queued |
