# H3 — PII-at-Rest Encryption — Design Spec (Codex-reconciled)

**Date:** 2026-06-08
**Branch:** `security/h3-pii-encryption` (off `main`)
**Audit finding:** H3 (`docs/security/SECURITY_CHECKLIST_AUDIT_2026-06-08.md`) — owner contact PII
(phone/email) stored **plaintext** in DB columns. A read-only DB compromise exposes thousands of real
property owners' contact info in cleartext.
**Status:** design **Codex-consulted (high effort, NO-GO → revised)**. Scope reduced per the consult.

> **⚠️ TWO-BRANCH SPLIT (2026-06-09).** Implementation ships in two deploy stages so the login-critical
> `User.email` cutover never rides the same rolling deploy as the additive column add (Codex P5 finding):
> - **`security/h3-pii-encryption` (STAGE 1, this branch):** P1–P3 contact-PII encryption (Result /
>   SkipTraceCache phone/email/phones/emails + `raw_response`) **+ P4** additive `User.email` blind index
>   (`email_hmac` nullable, `@validates` dual-write). `User.email` itself stays **plaintext**; login still
>   looks it up by plaintext. Safe single deploy. Migrations 046 + 047.
> - **`security/h3-email-cutover` (STAGE 2):** the `User.email` cutover — email→`EncryptedString`,
>   `email_hmac` NOT NULL + UNIQUE (migration 048), login/register/reset switch to `email_hmac`,
>   operator-script + test updates. Deploy ONLY after Stage 1 is fully rolled out and
>   `scripts/backfill_user_email_hmac.py` reports 0 NULL / 0 collisions. Full ordered runbook (§11) and
>   the P5 Codex-gate log live on that branch's copy of this spec.
>
> Sections below describe the FULL design (both stages). Stage 1 implements everything except the §4
> `User.email` *encryption + read-switch* and migration 048.

---

## 0. Scope decision (owner, post-Codex)

The Codex consult proved that owner **names and addresses are substring-searched** in SQL
(`ILIKE '%term%'` in results search and admin cached-records) and used in placeholder comparisons and
dedup-key recomputation. Non-deterministic Fernet + an equality-only blind index **cannot** support
substring search — encrypting those columns would break search features.

**Decision: encrypt only the genuinely-private contact PII; leave public-record names/addresses
plaintext.** Skip-traced phone/email are private enrichment data and are the audit's named target.
Owner names + property/mailing addresses are derived from **public county records** and are the
search/filter/compare keys — they stay plaintext (accepted residual).

### In scope (encrypt)
| Table | Columns | Type |
|---|---|---|
| `Result` | `phone, email` | `EncryptedString` |
| `Result` | `phones, emails` | `EncryptedJSON` |
| `SkipTraceCache` | `phone, email` | `EncryptedString` |
| `SkipTraceCache` | `phones, emails, raw_response` | `EncryptedJSON` |
| `User` | `email` (+ new `email_hmac` blind index) | `EncryptedString` + HMAC |

`raw_response` holds the **full** Tracerfy provider payload (all phones/emails) — a bigger leak than
the picked columns, so it is encrypted (Codex P1 #7).

### Out of scope (stay plaintext — public-record tier and/or searched)
`party_name`, `property_address`, `mailing_address` (Result/CountyRecord), `DeliveredRecord.property_address`,
`PropertyListMembership.property_address`, all `PendingSkipTraceRow` fields (names/addresses only — no
phone/email), `phone_type`, `phone_dnc_flag`. Integration secrets are **env-only** (`TRACERFY_API_TOKEN`
etc.) — no DB-resident secrets.

**Accepted residual (P2, deferred):** `SkipTraceQueue.download_url` is a *signed, expiring* Tracerfy
CSV URL on a transient queue row — sensitive but short-lived; tracked as a follow-up, not in H3.

---

## 1. Threat model & goal

Protect against a **read-only database compromise** (disk theft, leaked backup, unauthorized DB read).
The key never lives in the DB — app-layer only. Rules out `pgcrypto`. RLS is orthogonal (access, not
at-rest). Goal: a DB dump exposes no skip-traced phone/email/provider-payload in cleartext.

---

## 2. Crypto primitives

### 2.1 `src/utils/crypto.py` (extend existing Fernet module)

- **Version-prefixed tokens.** `encrypt_field()` prepends sentinel `fe1:` to the Fernet token.
  `decrypt_field(value)`:
  - `fe1:`-prefixed **and** Fernet-decrypts → plaintext (strip prefix, decrypt).
  - `fe1:`-prefixed **but does not decrypt** → treat as **legacy plaintext** (return as-is) in tolerant
    mode; in strict mode this is corruption → raise. (Resolves Codex P1 #1: a real plaintext value that
    happens to start with `fe1:` is never silently lost — detection is decrypt-validated, not
    prefix-only.)
  - no prefix → legacy plaintext → return as-is in tolerant mode; **raise** in strict mode.
  - `settings.PII_ENCRYPTION_STRICT` (default `False`) flips both legacy branches to raise once backfill
    is verified complete. Closes the "plaintext accepted forever" footgun.
- **Blind index.** `blind_index(value) -> str` = `HMAC-SHA256(K_BI, normalize(value)).hexdigest()`.
  - **`K_BI` is a dedicated stable key** `settings.BLIND_INDEX_KEY` (env), **independent of**
    `FIELD_ENCRYPTION_KEY`. (Resolves Codex P1 #10: deriving from the field key would let MultiFernet
    rotation silently change the blind-index key and brick all logins. `K_BI` is rotated only via an
    explicit dual-index re-backfill, never casually.) Fallback for dev: HKDF from `SECRET_KEY` with
    info-label `b"bridgeleads:blind-index:v1"`.
  - `normalize(email)` = `strip().casefold()` (Unicode-aware case fold, not just `.lower()` — Codex P1
    #8 normalization gap). Deterministic ⇒ equal inputs map to equal index.
  - HMAC (keyed), not bare hash → not offline-dictionary-attackable by a DB-only attacker.

### 2.2 `src/db/encrypted_types.py` (new)

- `EncryptedString(TypeDecorator)` over `Text`: bind → `encrypt_field`; result → `decrypt_field`.
  `None` passes through untouched both ways. **Blank normalization:** empty/whitespace-only binds to
  `None` (Codex P2: prevents empty-string ciphertext from defeating `IS NOT NULL` / `trim != ''`
  contactability predicates).
- `EncryptedJSON(TypeDecorator)` over `Text`: bind → `json.dumps` → `encrypt_field`; result →
  `decrypt_field` → `json.loads`. `None` passes through. During transition a legacy plaintext JSON-text
  value decrypts as passthrough then `json.loads` cleanly.

---

## 3. Migration & rollout (additive schema, no in-migration data churn)

Respects the alembic-on-boot landmine (`incident_migration_branch_mismatch`): migrations are
**schema-only**; all data movement is out-of-band `railway run` scripts.

1. **Migration 046** — widen encrypted `String` columns to `TEXT` (`Result.phone/email`,
   `SkipTraceCache.phone/email`); convert encrypted JSON columns to `TEXT`
   (`Result.phones/emails`, `SkipTraceCache.phones/emails/raw_response`) via
   `ALTER ... TYPE TEXT USING col::text` (JSON→text is lossless for round-trip). Schema only.
2. **Backfill** `scripts/backfill_pii_encryption.py` (railway run): batched, **idempotent**, table by
   table. Per value: `fe1:`-prefixed-and-decryptable → skip; else (`fe1:`-prefixed-but-invalid, or
   no-prefix) → encrypt. Blank/whitespace → `NULL`. (Resolves Codex P1 #1 idempotency hole + P2 blank
   normalization.) Batched by PK range, short transactions (Codex P1: no long lock on `results`).
3. **Migration 047** (User phase) — `users.email_hmac` → `NOT NULL` + `UNIQUE`, only after the user
   backfill confirms every row populated and zero collisions (Codex P1 #8/#9).
4. **Strict cutover** — verification script counts unprefixed in-scope values; when zero, set
   `PII_ENCRYPTION_STRICT=true` in prod env.

Tolerant read keeps the app fully working through the mixed-state window.

---

## 4. `User.email` searchable path (high-risk auth phase)

Fernet is non-deterministic ⇒ the existing unique constraint on (now-ciphertext) `email` no longer
enforces uniqueness, and `WHERE email == x` can't match. Move uniqueness + lookups to the deterministic
`email_hmac` blind index:

- Lookups `WHERE User.email == x` → `WHERE User.email_hmac == blind_index(x)`:
  - `auth.py:282` (register dup-check), `:373` (login), `:1313` (forgot-password). (Codex confirmed
    these are the only three; no separate admin lookup exists.)
- `email_hmac` maintained at a **single choke point** wherever `email` is set (`@validates('email')` or
  `before_insert/before_update`) so it cannot drift.
- **Ordering (Codex P1 #8/#9):** backfill `email_hmac` for **all** users *before* switching any lookup
  to it; do **not** deploy encrypted `email` writes before the DB-enforced unique blind index (047) is
  live; registration must `catch IntegrityError` on the `email_hmac` unique violation (the pre-insert
  check is not race-safe on its own).
- **Brute-force keys (Codex P2):** `auth_hardening.py` embeds plaintext email in **four** key uses —
  `check` (`:419`), `record_failure` (`:470`), notification dedupe `notified:*` (`:489`), `clear`
  (`:532`). All switch to the **same** `blind_index(email)` (or a short stable hash) so no plaintext
  email lands in Redis and the four stay consistent.

---

## 5. Raw-SQL & predicate handling (TypeDecorator does NOT cover raw `text()`)

Under the reduced scope, only **phone/email** are encrypted, so most of Codex's address/name SQL breaks
**do not apply** (those columns stay plaintext). Remaining touch-points:

- **`segments.py`** raw `text()` SELECTs return `phone`/`email` among other columns
  (`:69-108`, `:137-177`): decrypt `phone`/`email` in Python after fetch via `decrypt_field`.
  `party_name`/`property_address`/`mailing_address` in the same select stay plaintext — no change.
  `ORDER BY ... property_address` (`:112`) is plaintext — unaffected. Contactability ranking
  `phone IS NOT NULL OR email IS NOT NULL` works (blanks normalized to `NULL`).
- **`dialer_filters.py:34`** `func.trim(Result.phone) != ""`: with blank→`NULL` normalization there is
  no empty-string ciphertext, so the predicate stays correct (NULL→excluded, ciphertext→included).
  Rewrite to `Result.phone.isnot(None)` for clarity.
- **`_reuse_enrichment_for_duplicates` (`tasks.py`)** raw `UPDATE ... SET phone=ro.phone, email=ro.email,
  phones=ro.phones, emails=ro.emails`: **ciphertext→ciphertext copy**, correct, no change. The
  `property_key` recompute path (`:1044`) reads plaintext `property_address` (out of scope) — unaffected
  (resolves Codex P1 #5 under reduced scope). Add a regression test pinning the ciphertext-copy invariant.
- **CSV export (`jobs.py`)** reads phone/email via `getattr` on ORM objects → TypeDecorator decrypts;
  `sanitize_for_csv` applied **after** decrypt. Verify only.
- **Skip-trace write paths** (`tracerfy_ingest.py` Core `update(Result)`, first-result Core
  `insert(Result)`, `SkipTraceCache` writes): SQLAlchemy Core against mapped columns invokes
  TypeDecorator bind-encryption. Verify each writes via mapped columns (not literal raw SQL); the
  `DeliveredRecord.property_address` raw copy is plaintext, unaffected.

---

## 6. Phasing (≤5 files/phase; Codex-gate each — any Crit/High = NO-GO)

- **P1 — Primitives.** `crypto.py` (prefix + decrypt-validated tolerant/strict + `blind_index` w/
  dedicated `BLIND_INDEX_KEY`), `encrypted_types.py` (`EncryptedString`/`EncryptedJSON` + blank-norm),
  `settings.py` (+`.env.example`: `PII_ENCRYPTION_STRICT`, `BLIND_INDEX_KEY`), pure unit tests. No
  schema, no wiring → fully local-testable.
- **P2 — Schema + display ORM types.** Migration 046 (widen/convert the 9 contact columns), `models.py`
  (apply types to `Result` + `SkipTraceCache` phone/email/phones/emails/raw_response). Tolerant read
  keeps prod up.
- **P3 — Backfill + raw-SQL/predicate fixes.** `scripts/backfill_pii_encryption.py`, `segments.py`
  (decrypt phone/email after fetch), `dialer_filters.py` (predicate clarify), reuse ciphertext-copy
  invariant test.
- **P4 — `User.email` blind index (high-risk auth).** Migration (add `email_hmac` nullable+index),
  `models.py` (User.email + email_hmac choke point), `auth.py` (3 lookups → blind index + IntegrityError
  catch), `auth_hardening.py` (4 brute-force keys → blind key), user leg of backfill.
- **P5 — Cutover.** Migration 047 (`email_hmac` NOT NULL + UNIQUE), verification script, flip
  `PII_ENCRYPTION_STRICT`. Ops: provision dedicated `FIELD_ENCRYPTION_KEY` **and** `BLIND_INDEX_KEY` in
  Railway prod before strict cutover.

---

## 7. Testing

- **Pure/local:** prefix round-trip; `fe1:`-prefixed-but-invalid → tolerant passthrough / strict raise;
  no-prefix plaintext passthrough; `blind_index` determinism + `casefold` normalization (case/whitespace/
  Unicode); `EncryptedString`/`EncryptedJSON` `None`- and blank-passthrough; JSON round-trip; corrupt
  real `fe1:` token strict-raises.
- **Integration (CI-only — prod `DATABASE_URL` + table-wiping `db` fixture; never run locally):** backfill
  idempotency (twice = no double-encrypt, blanks→NULL); segments phone/email decrypt; reuse
  ciphertext-copy invariant; login resolves via `email_hmac`; registration dup detection via blind index
  + `email_hmac` UNIQUE rejects dup signup; brute-force key consistency across check/record/clear.

---

## 8. Risks & open items

- **Key provisioning (ops, pre-strict-cutover):** provision real `FIELD_ENCRYPTION_KEY` **and**
  `BLIND_INDEX_KEY` in prod. Until then dev HKDF-from-`SECRET_KEY` fallback applies (rotating
  `SECRET_KEY` would orphan ciphertext + change blind index — same caveat as the MFA secret).
- **Strict-mode flip** is a deliberate post-backfill ops step, not part of a code deploy.
- **`email_hmac` key rotation** requires a dual-index re-backfill (documented, out of band).
- **Deferred residual:** `SkipTraceQueue.download_url` (signed, expiring) — follow-up.

---

## 9. Codex consult — reconciliation log

Consult run 2026-06-08 (high effort, 2.53M tok, read-only). **Verdict: NO-GO on the original spec →
revised.** Reconciliation (doctrine: docs silent → Codex wins; any Crit/High = NO-GO until resolved):

| Codex | Finding | Resolution |
|---|---|---|
| P1 #1 | `fe1:` prefix-only detection ambiguous; backfill skip bricks colliding plaintext | Decrypt-validated detection; backfill encrypts prefixed-but-invalid (§2.1, §3.2) |
| P1 #2 | segments returns more than phone/email | Reduced scope: names/addresses plaintext; decrypt only phone/email (§5) |
| P1 #3 | `scrapers.py` cached-records ILIKE + returns ciphertext | Reduced scope: `party_name`/`property_address` stay plaintext (§0) |
| P1 #4 | `jobs.py` ILIKE search + placeholder compares on address | Reduced scope: plaintext (§0) |
| P1 #5 | reuse recomputes `property_key` from ciphertext address | Address out of scope → plaintext; recompute unaffected (§5) |
| P1 #6 | `PropertyListMembership.property_address` leak | Address tier = public record, accepted residual (§0) |
| P1 #7 | `SkipTraceCache.raw_response` full payload unencrypted | **Added to scope** as `EncryptedJSON` (§0) |
| P1 #8 | email cutover login-outage + casefold gap | Backfill-before-switch ordering; `casefold`; 047 NOT NULL (§3, §4) |
| P1 #9 | registration race; unique must be DB-enforced | Don't write encrypted email before 047 UNIQUE; catch IntegrityError (§4) |
| P1 #10 | blind-index key derived from field key bricks login on rotation | Dedicated stable `BLIND_INDEX_KEY` (§2.1) |
| P2 | brute-force Redis keys plaintext email ×4 | All 4 → `blind_index(email)` (§4) |
| P2 | empty-string ciphertext defeats contactability predicates | Blank→`NULL` normalization at write + backfill (§2.2, §3) |
| P2 | scheduler/jobs placeholder address compares | Address plaintext → unaffected (§0) |
| P2 | `SkipTraceQueue.download_url` signed PII URL | Deferred residual (§0, §8) |

Sound points Codex affirmed: ciphertext→ciphertext copy in reuse is fine for phone/email/phones/emails;
`EncryptedJSON` over `Text` round-trips with `USING col::text` + `None`→`NULL`; contactability ranking
survives encryption given blank→`NULL`.
