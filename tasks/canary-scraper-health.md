# Canary Scraper Health — 2026-06-29

Source: Railway worker canary log (`logs.1782764539505.log`), single `canary_check` run.
Branch: `fix/canary-scraper-health` (worktree off origin/main, isolated from other sessions).

## Canary findings (triage)
| County | Verdict | Root-cause hypothesis | Confidence |
|---|---|---|---|
| clallam | ✅ healthy (1) | working | — |
| thurston | ✅ healthy (0) | 0 extracted despite 23 summary_labeled + 76 needs_detail_fetch — filter vs extraction gap | medium |
| chelan | ⚠️ degraded (0) | Kendo id mismatch: kendo-path looks for `#RecordDatePicker`, real field is `#RecordDate` → falls to press_sequentially → widget swallows → raw `el.value` force doesn't update kendo model → stale 4/21/2026 submitted | HIGH |
| lewis | ❌ failed | EagleWeb POST never reached results in ~100s; used "fallback" date path; bounced to docSearchPOST.jsp. Transient vs real flow bug | low-medium |
| spokane | ❌ failed | Cloudflare bot wall. Standing policy = "don't evade" — user OVERRODE for this session | low (feasibility) |

## Plan (phased, ≤5 files/phase, Codex on each root cause)

### Phase 1 — Chelan (HIGH confidence code bug)
- [ ] Live probe: confirm `#RecordDate` IS a kendoDatePicker and `$('#RecordDate').data('kendoDatePicker').value(d)` sticks
- [ ] Consult Codex on fix approach
- [ ] Fix `acclaimweb.py` kendo-first path to also resolve `#RecordDate` widget
- [ ] Live verify: Chelan returns records for a known-populated date
- [ ] Codex review diff

### Phase 2 — Lewis (confirm transient vs real)
- [ ] Live re-run Lewis EagleWeb scrape; capture whether POST→results completes
- [ ] If real: harden `_submit_search` / fallback date path; if transient: document + add resilience, no false fix
- [ ] Codex review

### Phase 3 — Thurston (extraction vs filter)
- [ ] Determine why 0 records extracted despite 99 parcel-source signals (probate filter correctness)
- [ ] Fix only if genuine extraction gap; else document as correct behavior

### Phase 4 — Spokane Cloudflare (research-first, lowest confidence)
- [ ] Research feasibility of Cloudflare challenge handling for the EagleWeb Spokane endpoint
- [ ] Report options honestly; do NOT ship brittle/ToS-risky evasion without explicit go

## Notes / findings (live-verified + Codex-reconciled)
### Chelan — ROOT CAUSED (evidence)
- Live probe: `#RecordDate` = classic **Telerik MVC DatePicker** (`$('#RecordDate').data('tDatePicker')` exists; `t-input`/`t-picker-wrap`/`t-select`). NO Kendo widget on page. My first hypothesis (Kendo id mismatch) DISPROVEN by probe.
- All 3 date-set methods in `_fill_dates` fail for Telerik: Kendo-API miss, press_sequentially swallowed, raw `el.value` reverts to default on blur. Date never moves off default `4/21/2026`.
- Server banner: **"Released through date: 04/21/2026 … As of 6/29/2026"** → Chelan publishes records only through ~2 months ago. Canary probes current week (`health.py:275-279`, `week_ago→today`) → legitimately 0 records → `degraded`.
- **CONCLUSION (Codex-consensus): the canary conflates DATA FRESHNESS with SCRAPER HEALTH.** "degraded (0 records)" is a false alarm for lagged counties, not a scraper bug. Latent real gap: Telerik date-entry + `.t-grid` extraction unsupported (only matters for historical windows ≤ released-through date).

### Codex fix-order (reconciled, agreed):
1. Make canary **lag-aware**: classify `source_lagged` vs `scraper_broken` vs `no_records` (don't treat 0 as degraded blindly). Highest value, lowest regression risk.
2. Widget-family date-setting (detect Kendo vs Telerik `tDatePicker`), set + **assert value stuck** (fail loud if reverts). `new Date(y, m-1, d)` zero-based.
3. `.t-grid` extraction support (else fixing dates just exposes the next failure).
4. County/version fixtures so Chelan fix doesn't regress Douglas/Pend Oreille.

### Canary verdict semantics (health.py): healthy=≥1 rec; 0=degraded (sticky-healthy guard); exception=down. This is the thing to make lag-aware.

## LIVE RE-RUN of all 4 (window 06/22→06/29) — reproduced
| County | db_health | Re-run result | Classification |
|---|---|---|---|
| chelan | (degraded) | n/a (Telerik+lag, see above) | Canary false alarm (data lag to 04/21) + latent Telerik gap |
| **lewis** | down | **EXCEPTION (2x)** — final_url=`docSearchPOST.jsp`, body=search form, data current ("through June 26, 2026") | **REAL scraper bug** — EagleWeb POST never reaches results; NOT lag, NOT Cloudflare. Reproduced. |
| thurston | healthy | **0 records**, reached `docSearchResults.jsp` cleanly | Working — pipeline OK, 0 = no probate filings that week (filter). Low/no concern. |
| spokane | down | EXCEPTION — `Performing security verification… not a bot` | Cloudflare bot wall (active challenge). Matches "don't evade". Feasibility low. |

**Verdict:** The ONE clear real code bug is **Lewis EagleWeb POST→results**. Spokane=Cloudflare (feasibility-limited). Chelan=canary semantics. Thurston=fine.

### Lewis — DEEP root cause (live-verified, overturned the simple fix)
- Form dump: Lewis date ids are `RecordingDateIDStart`/`RecordingDateIDEnd` (CAP R/D). Code `_configure_search` (eagleweb.py:394-398) checks `recordingDateIDStart` (lowercase) — CSS `#id`/getElementById are CASE-SENSITIVE → miss → fragile fallback path.
- BUT decisive test: filled CORRECT-case ids via `fill("")`+`press_sequentially` → values **reverted to defaults `07/04/1848`/`06/26/2026`**. So Lewis date field is a **datepicker widget that swallows programmatic input** (same class as Chelan Telerik). The intended window is NEVER applied; Lewis submits the default full 1848→2026 range → POST bounces to `docSearchPOST.jsp` (reproduced 3x). Data IS current (released through 06/26).
- So Lewis is NOT a 1-line selector-case fix. Needs: (1) proper datepicker set + value-stuck assertion, (2) understand why the (default-range) POST bounces while clallam/thurston reach results. Real but multi-layer; needs Codex + iterative live testing.

## BUILD candidates (ranked)
1. **Lag-aware canary** (health.py) — Codex-endorsed; fixes the false-alarm CLASS (Chelan + any lagged county). Distinguish source_lagged / scraper_broken / no_records. Clear win, contained.
2. **Lewis EagleWeb date-entry + POST bounce** — real bug; deeper than expected (widget swallow + bounce). Medium-high effort, uncertain. Shares a fix pattern with Chelan (assert-value-stuck datepicker handling).
3. **Spokane Cloudflare** — active bot challenge; realistically needs CAPTCHA-solver/residential proxy = brittle/ToS-risky. Research-only recommended despite override.
4. **Thurston** — working; no fix. (0 = low probate volume that week; pipeline reaches results.)

## BUILD (user approved ALL 4) — phased, Codex on each
- [x] **Phase 1 — Lag-aware canary** (health.py). DONE. 2-stage probe; Stage2 historical re-probe for non-healthy+empty connectors only, cheap-first windows `[(90,83),(270,240)]`, upgrade-only. Commits `58b30ae` + `a266a8a`. Codex review GATE=PASS (1×P2 addressed: cheap-first to bound busy-county cost). Live-verified: clallam recovers via 270-240d net. 19 related tests pass. **REVIEW:** clean root-cause fix; Chelan/Lewis stay non-healthy until their fixes land then auto-recover — couples correctly. Known tradeoff (Codex-acknowledged): Stage-2 historical success can't detect recent-only ingestion breakage (rare; logged not caught).
- [~] **Phase 2 — Chelan Telerik — DEFERRED (user decision).** Live probes proved Chelan's "Record Date" search is hostile: date field reverts ALL sets (press_seq, raw el.value, Telerik `tDatePicker.value(Date)` w/ & w/o change) to released-through 04/21/2026 within ~1 tick; form.submit() stays on `SearchTypeRecordDate?Length=6` (AJAX 12-row .t-grid, no results page). 2mo lag. Phase 1 lag-aware canary already handles it gracefully (hidden, no false alarm). Revisit only if Chelan becomes priority. Not worth multi-hour reverse-eng now.
- [x] **Phase 3 — Lewis EagleWeb — DONE.** Commits `d3efcc6` + `ef576f7` (refactor per Codex P2). Root cause (empirically corrected): press_sequentially DOES set live .value, but clicking Search blurs the date field → widget resets .value to min default → POST runs full 1848→today range (slow/bounces) or empty window. Fix: when date-field ids known, _submit_search sets raw values + `HTMLFormElement.submit()` in ONE tick (no blur). Added case-correct `RecordingDateIDStart` pair. **Live-verified: Lewis 0→12, thurston 0→38 (was falsely 'healthy 0'), clallam 1 (no regression)**, all reach docSearchResults. Codex review GATE=PASS clean (after P2 fix). 171 scraper tests pass; 16 test_scraper_edit 503s are pre-existing/environmental (imports none of my files).
- [~] **Phase 4 — Spokane Cloudflare — PARTIAL (honest finding).** Built `_wait_through_cloudflare()` in eagleweb.py (Codex-reconciled: state-based clear detection, interactive-CAPTCHA bail, fast no-op for non-CF counties). **Landing-page CF: SOLVED** — verified we now pass the initial managed JS challenge (headless+headed, ~20s), log in (Public Login), reach docSearch, enter dates. **BUT the hard blocker: CF RE-CHALLENGES the search POST (docSearchPOST.jsp, "Ray ID …"), and a POST doesn't survive the challenge round-trip** (retries as GET, search lost) — did NOT resolve in 45s. This is the brittle/paid territory (CF-solver service or a GET-based search path). NOT shippable end-to-end. CF-wait code is correct groundwork but uncommitted pending user decision (it adds ~20s to a still-failing Spokane). Other EagleWeb counties: CF-wait is a fast no-op.
