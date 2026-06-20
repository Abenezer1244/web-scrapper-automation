# Death-certificate audit + consolidation + multi-tenant hardening (2026-06-19)

Worktree `../bridgeleads-deathcert-harden`, branch `chore/deathcert-multitenant-harden` off `origin/main` (`242eee3`, post PR #74).
Scope: **death-certificate party orientation + reliability + multi-tenancy across ALL probate connectors**. Never touch pre_foreclosure/tax/divorce/code_violation orientation logic.

## User decisions (this session)
- **Fix scope = Consolidate + harden ALL** — migrate EagleWeb (11 counties), Skagit, Pierce onto the single shared `src/scrapers/probate.py` `orient_probate_party`. ONE death-cert rule everywhere. Phased ≤5 files, Codex review per phase.
- **Live test = `railway run --service api`** — real prod env (DB/REDIS/SECRET + CAPTCHA for King). Non-persisting harness `scripts/live_verify_probate_hardened.py`. Hits real portals.

## The problem (why this campaign)
On a Certificate of Death the recorder indexes the ISSUING AUTHORITY (WA Dept of Health / "STATE OF WASHINGTON") as grantor, decedent in grantee. A scraper copying `party_name = grantor` surfaces the agency as the lead. PR #69/#72 fixed this but left **THREE divergent implementations**:
1. `probate.py::orient_probate_party` — shared, 3 guards (clark, king, whatcom, acclaim, laserfiche/cowlitz, tyler/okanogan)
2. `eagleweb.py::_strip_filing_agency` — own copy (benton clallam grant island jefferson kitsap lewis pacific spokane thurston whitman)
3. `skagit_recording.py::_is_filing_state_party` — own copy (skagit)
4. Pierce — NO orientation (deferred)
5. idocmarket/columbia, landmarkweb, ava_fidlar — decedent=grantor (verify no-op)

Divergence = drift risk: a fix to one rule never reaches the other counties. Consolidation makes the shared module the single source of truth.

## Connector inventory (21 probate connectors)
- **EagleWeb (11):** benton clallam grant island jefferson kitsap lewis pacific spokane thurston whitman
- **Acclaim (2):** chelan douglas  -- already shared
- **manual:** king OK / pierce DEFERRED / clark OK / whatcom OK
- **1-offs:** idocmarket=columbia / laserfiche=cowlitz OK / tyler=okanogan OK / skagit OWN

## Phases
- [x] **P0 Setup** — worktree, plan, env check (railway 4.33.0 + .env present)
- [ ] **P1 Codex approach consult** — pressure-test the consolidation plan (esp. EagleWeb behavior parity + Pierce [R]/[E] structure) BEFORE coding
- [ ] **P2 Orchestrated code audit (Agent tool, cross-verified)** — fan out by group; each finding verified by a 2nd agent from a different angle; multi-tenant-infra unit; base_scraper unit; completeness critic. Output: severity-ranked findings.
- [ ] **P3 Live test (railway run, all 21)** — non-persisting hardened harness, 45d window; assert party orientation (no agency/court/state/org), doc-type sanity, count. Capture death-cert specific samples.
- [ ] **P4 Reconcile** audit x live -> confirmed fix list (severity-tagged)
- [ ] **P5 Fix (phased <=5 files, Codex review per phase, ruff clean)**
  - A. Verify/extend shared `orient_probate_party` covers EagleWeb + Skagit behavior (Dept of Licensing/Revenue, filing-state variants). Add unit tests on real samples from both.
  - B. Migrate EagleWeb -> shared module (parity-tested, keep behavior >= current)
  - C. Migrate Skagit -> shared module
  - D. Wire Pierce ([R]/[E] structure-aware)
  - E. Secondary hardening: false-empty reliability (captcha/timeout -> RAISE not silent-0), doc-type word-boundary, idocmarket/landmarkweb/ava_fidlar verify-no-op
- [ ] **P6 Re-verify** — re-live-test changed counties + 2-user tenant-isolation check (test DB)
- [ ] **P7 Ship** — Codex final review, commit, push, PR, CI green, BUILD_JOURNAL + memory

## Codex approach verdict (P1, consult complete)
Consolidation onto ONE module = RIGHT for agency/death-cert orientation; keep portal parsing / role semantics / doc-type matching / placeholder cleanup per-template. Refinements folded in:
- **Pierce = gate on LIVE [R]/[E] death-cert samples.** Don't assume [R]=grantor/[E]=grantee. Order bug: `_map_row` drops on required-party (:510) BEFORE doc_type set (:525). → wire ONLY if live data confirms agency-in-[R]; else keep deferred (documented).
- **Regex: comma-form person fast-path BEFORE org rejection.** "LAST, FIRST" = person-like unless strong-org syntax present. Agency tokens PHRASE-based (FUNERAL HOME, BUREAU OF, DEPT OF REVENUE/LICENSING, VITAL RECORDS/STATISTICS, CORONER, MEDICAL EXAMINER) — NEVER standalone. Prevents false-DROP of real decedents.
- **Per-segment agency drop** valid only after `normalize_party_text` → " / " (eagleweb+skagit yes; Pierce no).
- **Gate shared call to per-row PROBATE-typed records** (helper now collapses ESTATE OF; global call would mutate non-probate rows before the type filter). landmark/ava: use MATCHED row type.
- **idocmarket: leave untouched** (no-op; would needlessly change ESTATE-OF output).
- **TOD guard runs strip_estate_caption** (probate.py:187) — document+test as intended.
- **Op risk:** `raw_html_hash` from `record.to_dict()` → changing party/heirs re-hashes already-scraped rows (dedup/output churn). Expected; note to user.
- **De-risk: fixture tests BEFORE migration.**

## Cross-verification doctrine ("verify each other's jobs")
Every audit finding produced by agent A is re-checked by agent B from a DIFFERENT angle (live data vs static read, or a second independent reader). Codex reviews every fix phase. Consensus -> higher severity; Codex wins on silent-doc disagreement. Any Critical/High from either reviewer = NO-GO.

## Findings — P2 audit (6 agents, cross-corroborated)

### Death-cert orientation (the consolidation target)
- **[High] eagleweb.py:95-114,786-805 — lone-state residue not stripped.** "WA DEPT OF HEALTH"→"WA" survives as party (no `_LONE_STATE_RE`). Shared module fixes. + **[Med] unguarded grantee promotion** (agency/court/company grantee can become lead; no `is_person_like_party`). + must pass `desc` (record.doc_type is None at that point) so TOD guard + Clallam abbrev codes resolve. ESTATE-caption collapse = NEW behavior across 11 counties → live-verify.
- **[Med] skagit_recording.py:383-387 — partial-concat agency not stripped.** Only swaps when WHOLE value is filing-state; "PERRIN, RONALD, STATE OF WA, DEPT OF HEALTH" keeps agency embedded. Shared `strip_filing_agency` fixes. + unguarded grantee promotion (Low). Shared `_BARE_STATE_RE` confirmed to absorb Skagit's inverted "WASH. STATE OF" order (no regression).
- **[Med] pierce_wa_probate.py:545-547,553-556 — no orientation; agency CAN land in [R].** ARMS doesn't make filing-agency-party impossible. Wire `orient_probate_party([R]=grantor, [E]=grantee, doc_type)` on PROBATE label after doc_type set; whole-cell junk fallback routes through it; drop on (None,None). Reliability already RAISES on block (good).
- **[Med] landmarkweb.py:482-486 (King generic path) & ava_fidlar.py:330-334 (Yakima) — raw grantor→party, no orientation.** Wire defensively. **[Low] idocmarket.py:446-455 (columbia) — decedent IS grantor → no-op**, optional defensive wiring.

### Shared module gaps to ADD before migrating (so consolidation is a strict superset)
- **[High] probate.py:71-78 `_NON_PERSON_RE`** missing FUNERAL HOME/MORTUARY/CREMATORY, CORONER/MEDICAL EXAMINER, DEPT OF LICENSING/REVENUE, DSHS/SOCIAL & HEALTH, BUREAU — a funeral-home grantee would be promoted to lead.
- **[Med] probate.py:90-120 `strip_filing_agency`** strips only DEPT OF HEALTH; add VITAL RECORDS/STATISTICS + LICENSING/REVENUE tails; and DROP per-`/`-segment agency parts in stacked multi-grantor ("DOE, JOHN / STATE OF WASHINGTON"→"DOE, JOHN").
- **[Low] `_LONE_STATE_RE`** WA-only (out-of-state 2-letter residue); **[Low] `is_person_like_party`** could reject a legit surname containing BANK/STATE token (accept conservative or refine comma-form).

### Reliability — false-empty (block/captcha/timeout → [] scored as healthy 0)
- PASS (RAISE on block): king, eagleweb, pierce, whatcom, laserfiche, idocmarket.
- **[Med] clark_wa.py:104-107,323-325** — block→[] healthy 0; chunk except swallows. Assert results-UI marker + RAISE; narrow except.
- **[Med] acclaimweb.py:774-780** — grid-never-rendered→[] healthy 0. RAISE when empty & no "no results" marker.
- **[Low] skagit_recording.py:254-258** — missing "returned N" counter treated as 0. **[Low] tyler_selfservice.py:177-211** — structural-failure return [] paths → RAISE.

### MULTI-TENANT: CLEAN ✅ (independently re-confirmed, NOT just inherited)
- Every death-cert lead read/write user_id-scoped (belt); RLS+FORCE on all 10 touched tables (suspenders); **SkipTraceCache key IS per-tenant** (hashes user_id — memory's "global" note is STALE/refuted, skip_trace.py:124); enrichment-reuse fenced to same-uid; billing CAS to job.user_id; export keyed `exports/{user_id}/...`. Per-job scraper instances, fresh Playwright context, no class-level mutable state, no mutable-default args, no page.on cross-job handlers. Concurrency-safe. One Low: system_sync_session used for single-row-by-PK/job_id writes only (no tenant fan-out) — acceptable.

### Live (P3) — in progress
- Smoke: clallam OK count=1 red_flags=0 doc_type=Death. Full 21-county run pending.

## Findings — P3 live (full 21-county run COMPLETE)
**18/21 returned, red_flags=0 on EVERY county.** The current shipped death-cert scrapers emit ZERO agency/state/court/org parties in the 45d window. 3 non-returns are correct fail-loud, NOT bugs:
- pacific + spokane: EagleWeb/Cloudflare block → correctly RAISED RuntimeError (reliability hardening working).
- chelan: Acclaim timeout (known perf).
Counts: benton 3, clallam 1, columbia 3, grant 19, island 42, jefferson 43, kitsap 8, king 65, cowlitz 43, clark 165, douglas 12, skagit 72, lewis 5, okanogan 18, pierce 64, whitman 2, thurston 52, whatcom 90.
⟹ Campaign value = PREVENTIVE consolidation + latent-gap closure (funeral-home/out-of-state/partial-concat/lone-state), not active-red-flag fixing.

### Pierce decision — EVIDENCE-BASED (Codex P0 satisfied)
Pulled live Pierce [R]/[E] samples: `SAKUMOTO MILAGROS EST OF(+)`/`SAKUMOTO HOWARD K`, `ENGLER DAVID M EXEC`/`ENGLER MARIAN L(+)`, `MOUGHTON PAMELA`/`MOUGHTON TERENCE...`. [R] is ALWAYS a person (executor/estate/decedent); Pierce probate = COURT-CASE data, NOT recorder death certs → a filing agency STRUCTURALLY never lands in [R]. ⟹ **Pierce NOT wired** (orient would be a no-op at best + estate-caption/drop risk on edge rows, zero upside). Documented, not deferred-by-default.

### Scope narrowing (evidence-based)
- **idocmarket/columbia**: decedent=grantor (no-op) → leave untouched (Codex P2).
- **landmarkweb/ava_fidlar**: NOT in the active 21-county probate set (King probate = king_wa_probate.py; Yakima/ava not a live probate connector) → not wired blindly (Codex P1 multi-type-row risk). Documented follow-up.
- **Consolidation lands on the two real divergent copies: EagleWeb (11 counties) + Skagit.** ✅

## Review

### Shipped (branch chore/deathcert-multitenant-harden, 3 commits)
- **5A `d400656`** — `src/scrapers/probate.py` extended to a strict SUPERSET of the per-template copies: broadened `_AGENCY_DEPT_RE` (Vital Records/Statistics, Licensing, Revenue, Social&Health), `_NON_PERSON_RE` death-care institutions (funeral/coroner/examiner/DSHS), per-segment bare-state drop. +10 tests.
- **5B/5C `d1e9dcf`** — EagleWeb (11 counties) + Skagit migrated onto the shared helper; their own `_strip_filing_agency`/`_is_filing_state_party` deleted. Gated to per-row PROBATE type (helper now collapses ESTATE OF). ONE death-cert rule everywhere.
- **Codex-fix `59c8cda`** — round-2 review fixes: `_US_STATE` enumeration (no more "MCKINLEY STATE" false-drop), `_AGENCY_DEPT_RE` covers "WASHINGTON STATE DEPT OF HEALTH" word order, comma-form person fast-path (rescues "CORONER, JANE"/"BANK, JOHN"). 42 tests green, ruff clean.

### Cross-verification ("verify each others jobs") — DONE
- 6 parallel audit agents, each finding corroborated (multi-tenant + lifecycle independently confirmed CLEAN by 2 agents).
- Codex consulted on the design BEFORE coding (Pierce gated on live samples; phrase-based tokens) AND reviewed the diff AFTER (3 P2s caught + fixed).
- Live data = the third, empirical cross-check (different angle from static read).

### Live proof (railway run --service api, non-persisting, real portals)
- Full 21-county: 18/21 returned, red_flags=0 everywhere; pacific/spokane fail-loud (block), chelan timeout (perf).
- Migrated re-verify (13 counties): red_flags=0, counts stable.
- Final agency-affected re-verify (post Codex-fix): **8/8 returned, red_flags=0** — thurston(34, bare-state), king(65, "WASHINGTON STATE DEPT OF HEALTH" concat), cowlitz(43), skagit(72, inverted), benton(3), jefferson(47), grant(14), lewis(5). State-enum + broadened-agency + comma-form fixes did NOT regress live stripping.

### Decisions (evidence-based)
- **Pierce NOT wired** — live [R]/[E] = persons (court-probate, no agency); orient inapplicable.
- **idocmarket/landmark/ava** — untouched (no-op / not in active probate set).
- **Multi-tenant** — CLEAN, UNCHANGED by this PR (diff is pure party-string transforms; no persistence/user_id/RLS/SkipTraceCache touch).
- **Reliability false-empty (clark/acclaim/skagit-counter/tyler)** — tracked FOLLOW-UP (orthogonal to party correctness; EagleWeb RAISE pattern already proven on pacific/spokane). Fix direction: mirror eagleweb's results-marker-or-RAISE.

### Op note (raw_html_hash churn)
Changing party/heirs re-hashes already-scraped rows (`raw_html_hash` from `record.to_dict()`); harmless for a correctness fix but a re-scrape may treat corrected rows as new. Billing dedup keys on parcel/address (`delivered_records`), not raw_html_hash, so no double-charge.
