# H3 — PII-at-Rest Encryption — Design Spec

**Date:** 2026-06-08
**Branch:** `security/h3-pii-encryption` (off `main`)
**Audit finding:** H3 (`docs/security/SECURITY_CHECKLIST_AUDIT_2026-06-08.md`) — owner PII
(phone/email/names/addresses) is stored **plaintext** in DB columns. A read-only DB compromise
exposes thousands of real property owners' contact info in cleartext.
**Scope (owner decision):** Owner PII **and** `User.email` (full scope, incl. HMAC blind index).
**Mechanism (owner decision):** SQLAlchemy `TypeDecorator` + encrypt **in place**, version-prefixed
ciphertext, out-of-band backfill.

---

## 1. Threat model & goal

Protect against a **read-only database compromise** (disk theft, leaked backup, unauthorized DB
read access). The encryption key must **never** live in the database — it is app-layer only
(env var / KDF from `SECRET_KEY`). This rules out `pgcrypto` (key would transit SQL/DB).
RLS is orthogonal (it governs multi-tenant *access*, not at-rest exposure).

Out of scope: integration secrets are **environment-only** today (`TRACERFY_API_TOKEN`, S3, Stripe,
Resend, `SECRET_KEY`) — no DB-resident secrets exist, so there is nothing to column-encrypt there.

---

## 2. Crypto primitives

### 2.1 `src/utils/crypto.py` (extend existing Fernet module)

- **Version-prefixed tokens.** `encrypt_field(plaintext)` prepends a fixed sentinel `fe1:` to the
  Fernet token. `decrypt_field(value)`:
  - value starts with `fe1:` → strip prefix, Fernet-decrypt; **hard-fail** (`InvalidToken`) on
    corruption — never silently return garbage.
  - value has no prefix → **legacy plaintext** → return as-is *only while tolerant mode is on*.
  - `settings.PII_ENCRYPTION_STRICT` (default `False`): when `True`, the no-prefix branch **raises**.
    Flipped to `True` post-backfill once verification reports zero unprefixed PII rows. Closes the
    "plaintext accepted forever" footgun.
  - The prefix makes ciphertext-vs-plaintext detection **unambiguous** — we never guess by trying to
    decrypt and catching errors (a real plaintext value could coincidentally not be valid base64, etc).
- **Blind index.** `blind_index(value) -> str` = `HMAC-SHA256(k_bi, normalize(value)).hexdigest()`.
  - `k_bi` derived once via HKDF-SHA256 from `FIELD_ENCRYPTION_KEY` (fallback `SECRET_KEY`) with
    info-label `b"bridgeleads:blind-index:v1"` — **distinct** from the field-encryption derivation
    label so the two keys are independent.
  - `normalize()` for email = `strip().lower()`. Deterministic → equal inputs map to equal index.
  - Key is env-only, never stored. HMAC (keyed) not bare SHA256 → not offline-dictionary-attackable
    by a DB-only attacker who lacks the key.

### 2.2 `src/db/encrypted_types.py` (new)

- `EncryptedString(TypeDecorator)` over `Text`: `process_bind_param` → `encrypt_field`;
  `process_result_value` → `decrypt_field`. `None` passes through untouched both directions.
- `EncryptedJSON(TypeDecorator)` over `Text`: bind → `json.dumps` → `encrypt_field`;
  result → `decrypt_field` → `json.loads`. `None` passes through.
- `None` passthrough is load-bearing: the segments `ORDER BY (phone IS NOT NULL ...)` ranking and any
  `IS NULL` checks keep working because NULL stays NULL (non-null becomes ciphertext, still NOT NULL).

---

## 3. Per-column scheme

| Table | Encrypted columns | Type | Plaintext (unchanged) |
|---|---|---|---|
| `Result` | `party_name, property_address, mailing_address, phone, email` | `EncryptedString` | `phone_type`, `phone_dnc_flag` (non-PII metadata); `dedup_hash`, `property_key` (SHA256, non-PII) |
| `Result` | `phones, emails` | `EncryptedJSON` | |
| `SkipTraceCache` | `phone, email` (Str); `phones, emails` (JSON) | Encrypted* | `address_hash` (PK, SHA256 — non-PII); `phone_type`, `phone_dnc_flag` |
| `PendingSkipTraceRow` | `property_address, first_name, last_name, mail_address, mail_city` | `EncryptedString` | `city, state, zip, mail_state, mail_zip` (used in cache-key hashing pre-storage / low-entropy) |
| `CountyRecord` | `party_name, property_address, mailing_address` | `EncryptedString` | |
| `DeliveredRecord` | `property_address` | `EncryptedString` | `parcel_id`, `dedup_hash` (non-PII) |
| `User` | `email` | `EncryptedString` **+ new `email_hmac`** | `api_key_hash` (already HMAC), `mfa_secret_encrypted` (already Fernet) |

**Why owner PII needs no blind index:** confirmed via full code map — owner `phone`/`email`/addresses
are **display-only**. Dedup uses a separate `dedup_hash` / `property_key` = SHA256(parcel|address)
computed from plaintext *before* storage; skip-trace uses `address_hash` = SHA256(address|city|state),
also pre-storage. Neither reads the stored PII column as a key. Segments JOIN on `property_key`, never
on phone/email. So non-deterministic Fernet is safe for all owner PII.

**Why `User.email` is the exception:** it is the one PII column used in `WHERE email == x`
(login, registration dup-check, password reset, admin lookup) with a unique constraint. See §5.

---

## 4. Migration & rollout (additive schema, no in-migration data churn)

Respects the alembic-on-boot landmine (`incident_migration_branch_mismatch`): migrations do **schema
only**; all data movement is out-of-band scripts run via `railway run`.

1. **Migration 046** — widen every encrypted column to `TEXT` (Fernet tokens exceed `String(32)`/`(255)`);
   add `users.email_hmac VARCHAR(64) NULL` + a **non-unique** index. Schema only.
2. **Backfill** `scripts/backfill_pii_encryption.py` (railway run): batched, **idempotent** (skips any
   value already `fe1:`-prefixed), encrypts existing rows table by table, and sets `email_hmac =
   blind_index(decrypted_email)` for users. Pattern mirrors existing `property_key` / multi-contact
   backfills in `scripts/`.
3. **Migration 047** — promote `users.email_hmac` to **UNIQUE** (after backfill confirms zero
   collisions). Auth uniqueness now rides this index (§5).
4. **Strict cutover** — verification script counts unprefixed PII values; when zero, set
   `PII_ENCRYPTION_STRICT=true` in prod env.

Tolerant read (§2.1) means the app keeps working through the whole window: rows mid-backfill are a
mix of `fe1:`-ciphertext and legacy plaintext, and both read correctly.

---

## 5. `User.email` searchable path (high-risk auth phase)

Fernet is non-deterministic ⇒ a DB unique constraint on the (now ciphertext) `email` column no longer
prevents duplicate signups, and `WHERE email == x` can't match. Uniqueness + lookups move to the
deterministic `email_hmac` blind index:

- Every lookup `WHERE User.email == x` → `WHERE User.email_hmac == blind_index(x)`:
  login (`auth.py`), registration duplicate-check, password-reset request, admin user lookup.
- `email_hmac` is maintained wherever `email` is set (a single choke point — ORM `@validates('email')`
  or a `before_insert`/`before_update` event — so it can never drift from `email`).
- Registration uniqueness enforced by the UNIQUE index on `email_hmac` (migration 047), not on `email`.
- **Brute-force keying audit:** `BruteForceProtection` keys Redis by `(ip, email)`. At runtime we still
  hold the decrypted email in memory, so functionality is unaffected; but to avoid writing plaintext
  email into Redis keys, switch the email component of the key to `blind_index(email)` (or a short
  hash). Audit + adjust in this phase.
- Keep the existing `email` column populated (ciphertext) for display/contact; only its *role as a
  key* moves to `email_hmac`.

---

## 6. Raw-SQL traps (TypeDecorator does NOT cover raw `text()`)

The ORM `TypeDecorator` only encrypts/decrypts for ORM-mapped reads/writes. Raw `text()` SQL sees the
**stored bytes** (ciphertext). Explicit handling per path:

- **`src/api/routes/segments.py`** — raw `text()` SELECTs that return `phone`/`email` for display/export:
  decrypt in Python via `decrypt_field` after fetch. `ORDER BY ... phone IS NOT NULL` ranking is
  unaffected (ciphertext is non-null; NULL stays NULL).
- **`_reuse_enrichment_for_duplicates` (`src/workers/tasks.py`)** — raw `UPDATE ... SET phone =
  ro.phone, email = ro.email, phones = ro.phones, emails = ro.emails`: this is a **ciphertext →
  ciphertext copy**, correct with no change. Add a regression test pinning the invariant.
- **Skip-trace cache write / tracerfy ingest (`src/workers/tasks.py`, `tracerfy_ingest.py`)** — verify
  rows are written via **ORM instances** (TypeDecorator encrypts automatically). If any path uses raw
  `INSERT`/`UPDATE` with PII, call `encrypt_field` explicitly. Confirm `address_cache_key` is computed
  from the **plaintext** address available in the payload (it is — from `payload["property_address"]`,
  not from a stored encrypted column).
- **CSV export (`src/api/routes/jobs.py`)** — reads via `getattr(r, "phone", ...)` on ORM objects →
  TypeDecorator decrypts; `sanitize_for_csv` still applied **after** decrypt. No change beyond verify.

---

## 7. Phasing (≤5 files/phase; Codex-gate each — any Crit/High = NO-GO)

- **P1 — Primitives.** `crypto.py` (prefix + strict flag + `blind_index`), `encrypted_types.py`,
  `settings.py` (+`.env.example`) flag, pure unit tests. No schema, no wiring → fully local-testable.
- **P2 — Schema + display-only ORM types.** Migration 046, `models.py` (apply types to
  Result/SkipTraceCache/CountyRecord/DeliveredRecord/PendingSkipTraceRow). Tolerant read keeps prod up.
- **P3 — Backfill + raw-SQL fixes.** `scripts/backfill_pii_encryption.py`, `segments.py`
  decrypt-after-fetch, skip-trace/ingest write-path audit, reuse-for-dupes invariant test.
- **P4 — `User.email` blind index (high-risk auth).** `models.py` (User.email + email_hmac + choke
  point), `auth.py` lookups, brute-force key audit, user-email leg of the backfill.
- **P5 — Cutover.** Migration 047 (email_hmac UNIQUE), verification script, flip `PII_ENCRYPTION_STRICT`.
  Ops: provision a dedicated `FIELD_ENCRYPTION_KEY` in Railway prod (today it falls back to a
  SECRET_KEY-derived key — works, but couples PII ciphertext to the JWT secret's lifecycle).

---

## 8. Testing

- **Pure/local:** prefix round-trip; strict-mode raise on unprefixed; plaintext passthrough in tolerant
  mode; `blind_index` determinism + email normalization (case/whitespace); `EncryptedString` /
  `EncryptedJSON` None-passthrough; JSON round-trip; corrupt-`fe1:`-token hard-fails.
- **Integration (CI-only — prod `DATABASE_URL` + table-wiping `db` fixture, never run locally):**
  backfill idempotency (run twice = no double-encrypt); segments decrypt path; reuse ciphertext-copy
  invariant; login resolves via `email_hmac`; registration duplicate detection via blind index;
  `email_hmac` UNIQUE rejects dup signup.

---

## 9. Risks & open items

- **`FIELD_ENCRYPTION_KEY` provisioning (ops, non-blocking):** provision a real key in prod before
  strict cutover; until then the HKDF-from-`SECRET_KEY` fallback applies (rotating `SECRET_KEY` would
  then orphan ciphertext — same caveat already documented for the MFA secret).
- **Strict-mode flip is a deliberate post-backfill ops step**, not part of the code deploy.
- **Backfill on a large `results` table** must be batched + resumable to avoid long transactions.
- **Key rotation** path already exists (MultiFernet: prepend new key, re-encrypt, drop old) — unchanged.

---

## 10. Codex consult

Per `.claude/rules/codex-collaboration.md`, this design is pressure-tested with Codex **before** any
code. Findings reconciled here (doctrine: docs silent → Codex wins; any Crit/High = NO-GO).

_(Codex consult notes appended below after the run.)_
