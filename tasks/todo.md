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

## Review
_(filled in at end)_
