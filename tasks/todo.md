# Divorce record-type hardening — PR1 (classifier + party guard)

Branch: `fix/divorce-classifier-harden`

## Goal
Make the `divorce` record type legit/solid/hardened across every county scraper,
mirroring the proven `probate.py` / `preforeclosure.py` shared-module campaigns.
Multi-tenant isolation is already enforced at the worker (user_id stamp + RLS) and
export (`sanitize_for_csv`) — audit confirmed clean, no change needed there.

## Decisions (user-approved)
- **Ambiguous bare `DISSOLUTION`** -> fail closed (precision) unless the connector has a
  precise server-side divorce filter (Pierce checkbox 87, Skagit `Decree-divorce`).
- **Legal separation** -> included as a divorce-adjacent lead (`DECREE OF LEGAL SEPARATION`,
  `LEGAL SEPARATION`). Bare `SEPARATION` / `SEPARATION AGREEMENT` excluded.
- **Scope** -> split. PR1 = divorce-only (this file). PR2 = cross-cutting fail-loud
  reliability hardening (landmarkweb/ava/acclaim/tyler/skagit) — deferred.

## Codex consult (pre-code) — key points folded in
- Divorce is largely a Superior Court record, not a recorder record (King is already an
  inactive placeholder for this). Need a connector **truth table**, not blanket "make it work".
- Classifier must be 3-state (MATCH / NON_MATCH / AMBIGUOUS), not a boolean.
- Keep the party guard narrow (`is_non_person_party` only); do NOT deepen the `heirs`
  naming lie (heirs = secondary party for non-probate).
- Tests = deterministic fixtures first; live probe is final proof only.

## Tasks
- [ ] 1. `src/scrapers/divorce.py` — `classify_divorce_doc`, `is_divorce_doc`, `orient_divorce_party`
- [ ] 2. `tests/test_divorce.py` — positives / corporate negatives / ambiguous / separation / orient
- [ ] 3. Wire into scrapers, gated to `record_type=='divorce'` (Phase A templates, Phase B manual)
      - Phase A (<=5): eagleweb, tyler_selfservice, laserfiche_weblink, landmarkweb, ava_fidlar
      - Phase B (<=5): acclaimweb, skagit_recording (remove SEPARATION), whatcom_wa, pierce_wa_probate
- [ ] 4. Connector divorce truth table (confirm active set from live DB)
- [ ] 5. Verify — ruff + pytest + Codex review + code-reviewer agent; reconcile findings
- [ ] 6. Live-test active divorce connectors on BridgeLeads UI/API (before vs after counts)

## Connector divorce truth table (confirmed from live DB `county_connectors`)

Authoritative query: `railway run python scripts/diag_divorce_connectors.py`.
Only **2** connectors are ACTIVE with `divorce` in `record_types`:

| County | State | Status | Handler | precise_source | Live result |
|---|---|---|---|---|---|
| Pierce | WA | **ACTIVE — recorder-precise** | PierceWAARMSScraper, ARMS checkbox 87 = DECREE OF DISSOLUTION | True | 6 records, 0 corporate leaks, person↔person spouses, 5/6 enriched |
| Skagit | WA | **ACTIVE — recorder-precise** | SkagitRecordingScraper, server doc-type "Decree-divorce" | True | 2 records, 0 corporate leaks |
| King | WA | INACTIVE placeholder (court-only) | KingWaDivorceScraper | n/a | divorce is at Superior Court (dja.kingcounty.gov), not the recorder (mig 009) |
| Clark | WA | Not advertised (portal does not record divorce) | clark_wa.py code exists, unused | n/a | divorce not in record_types |
| Whatcom + all EagleWeb/Tyler/Acclaim/Ava/Laserfiche/iDocMarket counties | WA | Not advertised (court-only) | template/manual divorce code now correct-but-dormant | False (if ever enabled) | divorce not in record_types |

**Conclusion:** "divorce works on all counties" = it is only *available* on Pierce + Skagit
(the only WA recorders that record divorce decrees and advertise them). Everywhere else divorce
is a Superior Court record, structurally unavailable via recorder scraping — not a bug. The
shared classifier makes every dormant template path correct + fail-closed if divorce is ever enabled.

## Review

**Built:** `src/scrapers/divorce.py` (3-state classifier `classify_divorce_doc`,
`is_divorce_doc`, narrow `orient_divorce_party`) + 44 tests. Wired gated to
`record_type=='divorce'` into 9 scrapers (eagleweb, tyler_selfservice, laserfiche_weblink,
landmarkweb, ava_fidlar, acclaimweb, skagit_recording, whatcom_wa, pierce_wa_probate).
Removed Skagit's over-broad `SEPARATION` keyword. Zero behavior change to other record types
(every change is gated; non-divorce branches byte-for-byte unchanged).

**Verification:** ruff clean; 44 unit tests pass; all changed modules import-smoke clean;
`code-reviewer` agent (4 findings, all addressed/justified); Codex review ×2 (2 P2s fixed:
LEGAL SEPARATION AGREEMENT ordering + EagleWeb DISS/DISOL abbreviations). **Live-verified** the
new code against the real Pierce + Skagit portals via `scripts/diag_divorce_live.py`
(prod env): 6 + 2 records, **0 corporate-dissolution leaks**, correct person orientation.

**Decisions honored:** fail-closed on ambiguous bare DISSOLUTION for generic connectors;
legal separation included (but not bare SEPARATION / SEPARATION AGREEMENT); split scope.

**Deferred (PR2, separate blast radius):** cross-cutting fail-loud reliability hardening of the
silent-empty template paths (landmarkweb/ava/acclaim/tyler/skagit swallow extraction errors →
job DONE with 0). Affects probate/pre_foreclosure too, so it ships separately.

**Follow-up note:** Whatcom requires an APN/parcel on every row; divorce decrees often lack one,
so if Whatcom divorce is ever activated it may need the probate-style parcel exemption (eagleweb
already exempts probate+divorce). Not relevant today (Whatcom divorce inactive).

**Prod-UI caveat:** these changes are on branch `fix/divorce-classifier-harden`, NOT deployed.
The live verification ran the new code directly against the portals. Verifying via the prod
app.bridgeleads.io wizard requires merging PR1 and a Railway deploy first.
