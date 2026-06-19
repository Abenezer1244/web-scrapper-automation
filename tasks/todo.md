# Probate audit + multi-tenant hardening — campaign plan (2026-06-19)

Worktree `../bridgeleads-probate-harden`, branch `chore/probate-multitenant-harden` off `origin/main` (`c23523e`).
Scope: **probate only** — never touch pre_foreclosure/tax/divorce/code_violation logic.
Decisions: FULL AUTO to open PR. columbia(down)+pacific(degraded) IN SCOPE to recover; spokane Cloudflare = note-only, no evasion.

## 21 active probate connectors (by template/class)
- **EagleWeb (11):** benton clallam grant island jefferson kitsap lewis pacific spokane thurston whitman
- **Acclaim (2):** chelan douglas
- **manual:** king pierce clark whatcom
- **1-offs:** idocmarket=columbia, laserfiche=cowlitz, tyler_selfservice=okanogan, skagit_recording=skagit

## Codex consult (done) — blind spots folded in
- Non-persisting live tests miss persistence-path bugs (INSERT/UPSERT, dedup, overlap, stale cleanup, RLS) → dedicated infra audit unit.
- Template-grouped audit blind to per-county divergence (column order, doc-type aliases, detail-page-required, role inversion in 1/N) → per-county live assertions.
- Cross-verify from a DIFFERENT angle (live/golden), not same-code re-read.
- Class attrs (results=[], seen=set(), page/context), mutable default args, module globals, page.on handlers, reused BrowserContext → cross-tenant leak vectors.
- party_name pulled from court/agency/attorney/"Estate of" caption; "WILL"/"AFFIDAVIT" overmatch; guardianship inclusion rule.

## Phases
- [x] **P0 Setup** — worktree, todo, Codex approach consult
- [ ] **P1 Orchestrated code audit (Workflow)** — fan out by template group + 1 multi-tenant-infra unit + base_scraper; each finding cross-verified by a 2nd agent from a different angle; completeness critic. Output: severity-ranked findings.
- [ ] **P2 Live-test harness (parallel)** — generic registry-resolved non-persisting scrape per county (45d window); assert party orientation (no agency/court/attorney as party), doc-type sanity, count. All 21.
- [ ] **P3 Reconcile** audit findings × live data → confirmed fix list (P1/P2/P3).
- [ ] **P4 Fix** — phased ≤5 files, Codex review per fix (P1/High = NO-GO), ruff clean.
- [ ] **P5 Re-verify** — re-live-test changed counties + 2-user tenant-isolation check (test DB).
- [ ] **P6 Ship** — commit, push, PR, CI green, BUILD_JOURNAL + memory.

## Findings — P1 audit (10 agents) + cross-corroboration

### DOMINANT SYSTEMIC FINDING (independently found by 6 agents)
The probate party-orientation / filing-agency-strip that PR #69 gave **EagleWeb** + **Skagit** was NEVER ported to the other probate scrapers. Each takes `party_name = grantor` verbatim for probate (no decedent orientation, no agency/court/state/org strip, no "ESTATE OF" caption strip):
- **whatcom** (P1) whatcom_wa.py:293 — grantor verbatim for death-cert/LPA/letters/PR/affidavit-of-heirship.
- **king** (P2) king_wa_probate.py:877,1046 — grantor verbatim; + NO probate doc-type allowlist (belt only for pre_foreclosure, :861/:1019).
- **clark** (P2) clark_wa.py:374-383 — grantor verbatim; orientation gated to pre_foreclosure only.
- **acclaim** (P2) acclaimweb.py:840-844 — no person/company orient or "Estate of" strip.
- **pierce** (P2) pierce_wa_probate.py:477-481,533-537 — [R]/[E] verbatim, no [D]/decedent; whole-cell junk fallback.
- **tyler/okanogan** (P2) tyler_selfservice.py:495-496,581-597 — no Skagit-style filing-agency swap on death certs.
⟶ FIX DIRECTION: shared `orient_probate_party` / agency-strip helper, but orientation side is doc-type+county specific — design with Codex + live data (NOT a blind port).

### Doc-type quality
- **laserfiche/cowlitz** (P2) laserfiche_weblink.py:40-45 — "TRANSFER ON DEATH" + bare "WILL" pull LIVING-owner estate-planning (idocmarket explicitly excludes these).
- **clark** live: 111/165 are TOD deeds (living owners) — same TOD-as-probate question.
- **eagleweb** (P3) — probate filter has no GUARDIAN/WILL key and hard-drops every LACK OF PROBATE affidavit (:67 exclude). Product decision.
- bare "WILL"/"HEIR" substring overmatch: clark/acclaim/whatcom/laserfiche (P3) → word-boundary.

### Reliability (false-empty on captcha/timeout) — flagged across pierce/king/whatcom/eagleweb(spokane)/laserfiche
A bot-block/captcha/timeout page returns [] and scores as a healthy 0-record scrape. P2/P3. Distinguish "results header, 0 rows" from "page never loaded" → raise/FAIL.

### Other
- **skagit** (P2) skagit_recording.py:405-412 — parcel fallback `\b(\d{6,})\b` can grab a tax-acct/permit # as parcel_id (wrong-parcel).
- **eagleweb** (P3) eagleweb.py:741,754 — `_LEGAL_STOP[4:]` brittle slice (`rantee:`), works by luck, fragile.
- **base_scraper** (P3) base_scraper.py:41,63 — `_LANDMARK_PREFIX_RE` unanchored substring strip (near-nil real risk).

### MULTI-TENANT: essentially CLEAN ✅ (this is the reassuring result)
- audit-infra: ALL core hot-path queries tenant-scoped (belt+suspenders). Result INSERT stamps user_id; dedup/billing/enrichment-reuse all user_id-filtered; SkipTraceCache key DOES include user_id (memory's "global" note is STALE/fixed); RLS+FORCE on every probate-touched table; scheduled/daily/batch converge on same run_scrape_job w/ correct user_id. NO P1/P2 cross-tenant defects. Only P3 parity nits (2 delivered_records reads belt-only, not exploitable — first_job_id is job-unique).
- audit-base: instance lifecycle PROVEN per-job; fresh Playwright context per job; no class-level mutable state; no shared browser. Statically clean.
- Every per-scraper agent independently confirmed: per-instance state, fresh context, no page.on leaks, no mutable-default args.

### Live empirical (COMPLETE, 17/21 returned)
- CLEAN red_flags=0 (14): benton clallam clark columbia(RECOVERED) grant island jefferson kitsap lewis pacific(RECOVERED) pierce skagit thurston whitman.
- WRONG PARTY (live-confirmed): **cowlitz 12/42 (filing_agency=9, bare_state=3)**, **king 1/65 (filing_agency)**, **okanogan 1/23 (estate_caption)**. Decedent already in heirs field → safe swap.
- No live data: chelan/douglas (Acclaim single-date-mode too slow >420s = perf finding), spokane (Cloudflare, note-only), whatcom (slow >420s).
- Bad samples: cowlitz "STATE OF WASHINGTON [DEPARTMENT OF HEALTH]" → heirs=decedent; king "WASHINGTON STATE DEPARTMENT OF HEALTH"→heirs=CONKLIN; okanogan "ESTATE OF GLENNA K JONES / ...".

### Codex design verdict (helper)
SAFE with 3 guards: (1) promote grantee ONLY if person-like (not agency/org), (2) both-agency→(None,None), (3) no-op on TRANSFER ON DEATH deeds (grantor=living owner). Regexes safe vs WASHINGTON/STATE FARM/WA STATE UNIVERSITY/person-with-ESTATE. okanogan: collapse "ESTATE OF X / X / Y"→"X".

## Fix plan (phased, Codex review per phase)
- [ ] **A. shared `src/scrapers/probate.py`** + unit tests (real samples) — orient_probate_party w/ 3 guards.
- [ ] **B. wire confirmed:** laserfiche(cowlitz P1), king(P2), tyler(okanogan P3) → live re-verify.
- [ ] **C. wire defensive:** whatcom(P1 code), acclaim, clark, pierce (gated probate+death-cert, no-op if grantor already decedent).
- [ ] **D. secondary:** doc-type word-boundary (WILL/HEIR), laserfiche TOD+WILL list, false-empty reliability guards (pierce/king/whatcom/eagleweb/laserfiche), skagit parcel fallback, eagleweb _LEGAL_STOP slice.
- [ ] **TOD product Q:** keep TOD-deeds as probate leads? (clark 67% TOD). DEFAULT: keep (property-transfer-at-death signal) + flag to user.
- [ ] **E. ship.**

## Review

### Shipped
- **NEW `src/scrapers/probate.py`** — shared `orient_probate_party(grantor, grantee, doc_type)` + `strip_filing_agency` / `strip_estate_caption` / `is_person_like_party`. Promotes the DECEDENT over a filing agency (WA Dept of Health), bare filing-state, or "ESTATE OF" caption; 3 Codex guards (person-like grantee only; both-agency→None; TOD no-op).
- **Wired into 6 scrapers** (probate-gated, no-op when grantor is already the decedent): laserfiche_weblink, king_wa_probate (JSON+DOM paths), tyler_selfservice, whatcom_wa, clark_wa, acclaimweb. pierce DEFERRED ([R]/[E] structure differs; live red_flags=0).
- **`tests/test_probate_party.py`** — 28 tests on the REAL live samples + guard/regression cases.
- **Live-confirmed fix:** cowlitz 12→0, king 1→0, okanogan 1→0 red_flags; clark 0→0 (no regression).
- **Codex diff review:** found 2 P2 edge cases (WA-abbrev agency residue; estate-caption dropping stacked co-parties) — BOTH FIXED + regression-tested.

### Multi-tenant verdict: CLEAN (no code change needed)
Audited belt+suspenders on every hot-path query; SkipTraceCache per-tenant; RLS+FORCE on all probate tables; per-job scraper instances. The memory's "SkipTraceCache global cross-tenant" note is STALE/already-fixed.

### Deferred / documented (NOT in this PR)
- **TOD product Q:** TRANSFER ON DEATH deeds (grantor=living owner) dominate some counties (clark 67%). Kept as probate-adjacent signal; flag for user decision.
- columbia(down)/pacific(degraded) health flags are STALE — both return clean data live; canary self-corrects.
- Reliability: captcha/timeout page parsed as valid-empty (pierce/king/whatcom/eagleweb-spokane/laserfiche) — separate hardening.
- doc-type word-boundary overmatch ("WILL"/"HEIR" substring); laserfiche TOD+bare-WILL list; skagit parcel `\d{6,}` fallback; pierce doc_type=none; acclaim single-date-mode too slow (chelan/douglas timeouts); eagleweb `_LEGAL_STOP[4:]` brittle slice; base_scraper `_LANDMARK_PREFIX_RE` unanchored.
- whatcom live-unverified (portal too slow >350s); fix applied defensively from code audit.
