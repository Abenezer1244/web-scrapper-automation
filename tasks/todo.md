# Probate follow-ups — deferred-item sweep (2026-06-19)

Worktree `../bridgeleads-probate-followups`, branch `chore/probate-followups` off `origin/main` (`bcb0a1b`, incl. PR #72).
Workflow: orchestrate investigation agents (parallel, cross-verified) → implement 1-by-1 → Codex review EACH → ruff + test → live-verify where possible. Probate-only. Any Codex P1/High = NO-GO.

## Items
- [ ] **A. False-empty reliability** — captcha/timeout/error page parsed as valid-empty (returns [] = looks healthy). Scrapers: pierce, king, whatcom, eagleweb(spokane), laserfiche. Distinguish "genuine 0 results" (known empty-marker present) from "page never loaded / blocked" → raise/FAIL.
- [ ] **B. doc-type word-boundary** — bare "WILL"/"HEIR" substring overmatch (clark, acclaim, whatcom, laserfiche). king/eagleweb already use _doc_type_matches word-boundary.
- [ ] **C. skagit parcel fallback** — `\b(\d{6,})\b` grabs tax-acct/permit # as parcel_id; prefer no-parcel over wrong-parcel.
- [ ] **D. pierce doc_type=none** — capture per-row doc_type (live showed all 64 blank).
- [ ] **E. acclaim perf** — single-date-mode too slow (chelan/douglas timeout >420s); chunk/speed up.
- [ ] **F. cosmetic** — eagleweb `_LEGAL_STOP[4:]` brittle slice; base_scraper `_LANDMARK_PREFIX_RE` unanchored.
- [ ] **G. whatcom live-verify** — confirm the #72 party fix live (portal was too slow; smaller window).
- [ ] **H. columbia/pacific stale health** — down/degraded flags but work live; flip or leave to canary (ops).

## Review — ALL ITEMS DONE

Orchestrated 5 investigation agents → cross-verified specs → implemented 1-by-1 → Codex reviewed in 2 passes (caught 2 real issues, both fixed) → full 21-county live regression.

### Shipped
- **A. False-empty reliability** — laserfiche/eagleweb/whatcom/pierce/king now RAISE RuntimeError (→ worker `_fail_job`) when a captcha/block/error page would otherwise return a false-empty []; a GENUINE empty (portal's real no-results marker present / valid JSON envelope recordsTotal:0) still returns []. Each scraper's known marker is the discriminator; RuntimeError re-raised past every inner chunk/extract catch-all. **Live-proven:** spokane + pacific now fail loud on the Cloudflare/welcome page (were silent-0); all 17 healthy counties unaffected.
- **B. doc-type word-boundary** — clark + whatcom single-token keywords ("WILL") now match on `\b` boundary (matches WILL / LAST WILL AND TESTAMENT / WILL/TESTAMENT / (WILL); excludes GOODWILL). acclaim/laserfiche already correct (no change). *(Codex caught that the agent's `set(split())` would drop punctuation-adjacent labels → switched to `\b` regex = eagleweb idiom.)*
- **C. skagit parcel** — dropped the `\d{6,}` fallback that stored a wrong tax-acct/permit # as parcel_id (require_parcel_id drops the no-parcel row cleanly).
- **D. pierce doc_type** — stamp `record.doc_type = self.DOC_TYPE_LABEL` (was None on all rows). **Live-confirmed: doc_types=['PROBATE'].**
- **E. acclaim perf** — removed a redundant 3s Kendo wait + trimmed a post-nav settle 3s→1s (~30% faster). Single-date mode fits the prod 30-min cap; chelan/douglas only timed out the 280s TEST.
- **F1. eagleweb** — replaced the `_LEGAL_STOP[4:]` slice footgun with an explicit `_STOP_BODY` constant (behavior = the intended one).
- **F2. base_scraper** — anchored `_LANDMARK_PREFIX_RE` to a token boundary.
- **G. whatcom live-verify** — finally returned live (31 records, red_flags=0): the #72 party fix holds.

### Codex (verify-each-other)
- Batch review → P2: `set(split())` drops `WILL/TESTAMENT`-style labels → fixed to `\b` regex.
- Item-A challenge → king false-failure (captcha-error then valid-empty retry wrongly raised) → fixed (retry envelope authoritative) → re-review FIXED, PASS.

### Deferred / noted
- spokane stays Cloudflare-blocked (now fails loud, no evasion). pacific intermittently lands on its welcome page → now fails loud (watchdog re-queues) instead of silent-0 — intended.
- chelan/douglas (acclaim) still slow for large windows; the in-place re-fill refactor deferred (only matters for >45-day manual backfills; scheduled 1-day jobs are fast).
- pierce per-row ARMS sub-type (vs connector label) needs a live column probe — separate follow-up.
- columbia/pacific connector health flags still stale (canary self-corrects).
