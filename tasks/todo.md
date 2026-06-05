# Phase 5 — Dialer push (Enzo)

**Branch:** `feature/phase5-dialer` off `main`. **Spec:** §Phase 5 — "Generic delivery-connector abstraction; Enzo API connector; push skip-traced + valid-phone + non-DNC rows."

## BLOCKER (spec line 23 + 188): Enzo API docs/credentials are "supplied at Phase 5" — NOT yet provided.
Cannot build the real Enzo connector (no mock code in this prod project). Built the Enzo-INDEPENDENT foundation; requesting Enzo specifics from the user.

## Verified facts
- Result skip-trace fields: phone, phone_type (Mobile|Landline|VoIP), phone_dnc_flag (Boolean nullable), email, skip_trace_status (not_attempted|queued|submitted|hit|miss|errored).
- Existing generic outbound push: `src/workers/webhook_delivery.py` (SSRF-validated `validate_outbound_webhook`, HMAC-signed, Celery autoretry) — reuse its IDEAS for the Enzo connector, but Enzo needs a dedicated connector, not a generic webhook (Codex).
- PRD stance: do NOT build a dialer engine; integrate/push to existing dialers (TCPA-regulated).

## Codex consult (done) — reconciled
- Valid phone = `phone IS NOT NULL AND trim(phone) <> ''`; phone-type-agnostic.
- **DNC (TCPA/FTC TSR): `dialer_ready` = `phone_dnc_flag IS FALSE`** — unknown/NULL DNC EXCLUDED (don't call un-verified numbers). Looser "candidate" set (unknown allowed) = explicit opt-in, named honestly.
- Do NOT gate on skip_trace_status in the reusable predicate (valid phone from any source qualifies); the Enzo task can add `='hit'` itself.
- Do NOT build Enzo tables/DTOs/fake clients/tasks (speculative without docs).

## Slice 5A — dialer-ready lead selection (Enzo-independent): ✅ BUILT
- [x] `src/api/dialer_filters.py` (pure, tested): `dialer_ready_conditions(include_unknown_dnc=False)` — valid phone + (TCPA-safe `dnc IS FALSE` default | opt-in `dnc IS NOT TRUE` candidate).
- [x] `dialer_ready=true` view/export param on `get_results` + `download_export` + `export-url` (threaded through in-app flow — 4B lesson). Empty filtered ≠ 404 empty-job; previous-job suggestion gated when filtered.
- [x] 4 predicate tests. (phone/dnc/skip-trace cols already in ResultRow.)
- [ ] Codex review → commit.

## Slice 5B — Enzo connector (BLOCKED — needs user to supply):
- API base URL + env (prod/sandbox); auth (key/OAuth/HMAC/bearer + refresh); endpoint(s) (create/update contact, add to list/campaign, bulk import); payload schema (required fields, phone format, lead IDs/metadata); rate limits + batching; idempotency/upsert (external ID, dup handling); DNC/consent source of truth (Enzo vs BridgeLeads); campaign/list model; error contract (retryable vs terminal); audit/PII-redaction/retention; status callback/webhook.
- Then: dedicated Enzo connector (reuse webhook_delivery patterns: SSRF allowlist, retries, signed/auditable payload, idempotency) + push task selecting dialer-ready (+ optionally skip-traced) leads. "Push to dialer" delivery option (UI = frontend).

## DECISION surfaced to user
- DNC default = TCPA-safe (`dnc IS FALSE`). Want a looser opt-in "candidate" mode (include unknown-DNC) exposed via the API too? (Function supports it; API currently exposes only the safe default.)
