# King mailing from the Assessor bulk extract (2026-09-13)

Approved by the owner 2026-09-13 ("approved").

## Evidence
- `Real Property Account.zip` -> `EXTR_RPAcct_NoName.csv` (aqua.kingcounty.gov, free, weekly,
  Last-Modified 2026-09-05): 739,983 accounts / 737,047 PINs; 99.6% one account per PIN;
  635 empty AddrLine. Columns: AcctNbr, Major, Minor, AttnLine, AddrLine, CityState, ZipCode, ...
- Prod: 22,983 King parcels with a stored mailing; 48.8% agree with the extract (street+ZIP).
- **11,105 King rows** store mailing == their own property street (situs echo from the pre-#210
  King tax scraper, `repair_king_tax_historical.py` never applied) while the extract has a
  different taxpayer address. Two verified against King's LIVE tax-bill page
  (1282301809 -> 3011 S ESTELLE ST; 1321400230 -> GRAPEVINE TX): the extract was right both times.
- 17,172 King parcels deferred for mailing; 16,909 resolvable unambiguously from the extract.

## Codex consult (design) - adopted
- One-to-many PIN -> accounts: fill only when every account for the PIN agrees.
- Snapshot, not event-time truth: stamp source + extract date on every write.
- Never touch parcel_id / dedup / billing; no quota, no Tracerfy, no job.

## Phase A (this PR, <=5 files)
- [ ] `src/scrapers/enrichment/king_rpacct.py`: download (SSRF-safe), schema check, streaming
      PIN-filtered load, address formatting, unambiguous resolve.
- [ ] `scripts/king_rpacct_mailing.py`: dry-run default, JSONL evidence, two passes:
      1. echo repair: King, done job, mailing is the situs-echo signature, no verified
         mailing_source -> extract value, or NULL + mailing_lookup_deferred when unresolvable;
      2. deferred fill: King, done job, mailing NULL + deferred -> extract value.
      Guarded UPDATE (id, user_id, parcel_id, old mailing), owner flags recomputed.
- [ ] tests for both (real DB, real zip file built in tmp_path).
- [ ] Codex review; dry-run on prod; spot-check sample vs live tax bill; apply.

## Phase B (next)
- [ ] Live King enrichment consults the extract before the rate-limited tax-bill pages.

## Phase C
- [ ] King code_violation: point-in-polygon PIN (strict address/ZIP match) -> enrichment_data.kc_pin -> extract.
