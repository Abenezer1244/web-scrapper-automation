# InvalidToken (fe1: undecryptable under strict) — root cause + fix — 2026-06-15

Bug: `run_scrape_job` raised `InvalidToken('fe1:-prefixed value is not decryptable under strict mode')`.
Investigated with systematic-debugging + Codex + 2 Explore agents.

## Phase 1 — Root cause (DONE, evidence-backed)
- `decrypt_field` (crypto.py:140) raises when an `fe1:` token decrypts under no key in the live
  FIELD_ENCRYPTION_KEY set + strict mode.
- **Prod diagnostic (`scripts/diag_undecryptable_pii.py`, read-only):** of 5,562 fe1: tokens across 11
  encrypted columns, **61 are in `users.email`, encrypted under the HKDF-from-SECRET_KEY derived key**;
  every other column clean; **0 anomalies** (nothing unrecoverable). SECRET_KEY unchanged.
- **Fingerprints:** api + worker BOTH have the same single primary key (`8af30f234202`) + same SECRET_KEY +
  STRICT=true NOW → the bleed has STOPPED; the 61 are historical.
- **Root cause:** a past window where the API lacked FIELD_ENCRYPTION_KEY → `_build_fernet()` SILENTLY fell
  back to the derived key and wrote 61 users.email under it. Recurred because the 2026-06-13 fix
  re-encrypted at a point in time but never closed the silent-fallback hole. Those 61 users are likely
  login-broken (login/scrape decrypt users.email under strict).

## Phase 4 — Fix (two parts)
- [ ] **A. Data recovery:** re-encrypt the 61 derived-key `users.email` onto the primary key (decrypt with
      HKDF derived key → `encrypt_field` [primary] → re-verify under current set → UPDATE; never touch
      anomaly; idempotent). Self-contained (NO env mutation — env-append is what caused the incident).
      `diag_undecryptable_pii.py --apply`. Codex-gate + unit test the derived→primary roundtrip, THEN run in
      prod, THEN re-run read-only to verify 0 derived/0 anomaly.
- [ ] **B. Root-cause guard (durable):** make `crypto._build_fernet()` REFUSE the silent HKDF fallback when
      `ENVIRONMENT=production` or `PII_ENCRYPTION_STRICT=true` — raise instead of silently deriving from
      SECRET_KEY. Tests + Codex-gate. Separate PR.

## Notes
- Existing `reencrypt_derived_key_pii.py` can't be reused: it REQUIRES a 2-key env (aborts on primary-only)
  and tests the 2nd ENV key, not the HKDF key. New diagnostic computes HKDF directly.
- The 61 PKs are captured by the diagnostic output.

## Review (2026-06-15) — DONE
- **Part A (data recovery) — COMPLETE + VERIFIED IN PROD.** `diag_undecryptable_pii.py --apply` re-encrypted
  all **61** derived-key `users.email` onto the primary key (reencrypted=61, cas_skipped=0, anomaly=0).
  Re-scan: `CLEAN` — 5,562 fe1: tokens all current, 0 derived, 0 anomaly. Those 61 users' login + scrape
  decryption now work. Codex-gated (P1 CAS fix). 3 roundtrip tests.
- **Part B (root-cause guard) — COMMITTED (`6fbf85b`), ships via PR.** `_build_fernet()` refuses the silent
  HKDF fallback when FIELD_ENCRYPTION_KEY is empty + (STRICT or production); boot-time `_instance()` in API
  lifespan + worker so misconfig fails startup. conftest sets ENVIRONMENT=test (CI parity) so the guard
  doesn't trip the suite. Codex CLEAN/GO after restoring the stronger (not strict-only) guard. 33 crypto
  tests green, broad pure slice 85 green.
- **Branch `fix/crypto-reencrypt-derived-emails`** (2 commits). Recovery already applied to prod; the PR
  ships the diagnostic/recovery tooling + the guard (merge deploys the guard to prevent recurrence).
