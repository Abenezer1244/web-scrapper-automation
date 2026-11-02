# Owner-name lookup recovery (Island first): ask the names a job never reached

Branch `fix/pacs-name-recovery` (stacked on `fix/enrich-recovery-transactions`, PR #583).
Owner cleared commercial owner-name searching of the Island PACS portal, sweep included (2026-11-02).

## Evidence (production, read-only, counts only, 2026-11-02)
- Island: 211 leads, all probate, no parcel ids. `pacs_name_lookup`: skipped 121, never recorded 71,
  no_match 10, found 6, failed 3. 195/211 never got an answer.
- Live re-search, same portal and pace: 15 never-asked names -> 5 a unique exact owner row, 1 an exact
  owner among several rows, 9 "None found". The 10 no_match names stay "None found" even with the
  caption phrases ("ESTATE OF", "IN RE") stripped: normalization (3a) has no measured yield, dropped.
- The inline pass reaches ~tens of names per job (900 s budget, Island 10 s pace, breaker 3); nothing
  re-asks a skipped name. Mailing, owner and property each have a recovery sweep; names do not.

## Design (Codex consult 2026-11-02 amendments folded in)
- Eligibility: done job; `is_duplicate = false`; not delivery-excluded; non-blank `party_name`; property
  address empty; `pacs_name_lookup` IN ('skipped', 'failed') (an explicit marker: rows with no marker
  are NOT swept, a separate reviewed backfill decides those); attempts < 5.
- Counties: those whose connector `assessor_url` (or KNOWN_ASSESSOR_URLS) is a PACS portal; a
  license-restricted county (PACS_SITES[c].license_restricted) only with
  COUNTY_GIS_RESTRICTED_MAILING_ENABLED; the whole sweep only with NAME_RECOVERY_ENABLED.
- One county per tick (fair rotation: the county whose eligible rows were asked longest ago), a small
  batch of distinct names; the existing `batch_lookup_pacs_by_name` lease, pace, breaker.
- Read, then end the transaction (rollback) BEFORE admission and network (Phase 1/2 lesson).
- Attempts count only real answers (found / no_match / failed). `skipped` (lease, budget, breaker)
  only rotates `last_at`, never charges.
- Writes: one guarded UPDATE per row, re-checking id, user, the queried party_name, empty property
  address, the expected `pacs_name_lookup`, eligibility and the done job; JSON merged into the CURRENT
  value; mailing filled only if empty; owner flags recomputed like property_recovery; never parcel_id,
  billing, dedup, delivery, skip trace or a Job.
- Single-flight: token-owned Redis lock, fail closed (property_recovery's `_acquire_lock`).

## Outcome (2026-11-02)
- [x] 3b-1 SHIPPED (branch `fix/pacs-name-gate`): the inline name pass of a license-restricted county
      answers to COUNTY_GIS_RESTRICTED_MAILING_ENABLED (it ran ungated; the switch is on in production,
      so nothing changes there). Codex: NO-GO (a third Island test lacked the switch; loose gate
      assertions) -> fixed -> GO.
- [x] 3b-2 BUILT, REVIEWED, PARKED (local branch `fix/pacs-name-recovery`, commit a76ef8e1, not pushed).
      Its SQL was EXPLAINed against production (plans), and then it selected ZERO rows: every one of the
      205 address-less Island rows is `is_duplicate = true`, `duplicate_reason = 'superseded'` (126
      filings, 2 jobs). That is the intended parcel-less claim release
      (dedup.release_parcelless_no_address_claims): the filing's claim is dropped so a later run can
      claim it again. The 6 delivered Island leads all have addresses. So "195 never answered" were
      never delivered leads, and the sweep has no work today. Codex review: NO-GO, wait: P1 the
      delivered-only contract needs an actionability guard in selection and UPDATE; P2 a found answer
      without a street must not write mailing. Recovering released filings needs its own
      delivery/claim/quota design. `NAME_RECOVERY_ENABLED` was removed from 3b-1 (nothing uses it).
- [ ] If Island coverage matters: design how a released parcel-less filing is re-claimed and looked
      up (owner decision), then revisit the parked sweep with Codex's P1/P2.
