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

## DECISION (user, 2026-06-05): DROP Enzo. Build GENERIC "push to any dialer" instead.
Enzo = newest vendor, no public API/pricing, fewest reviews = worst first integration. Generic webhook/Zapier push works with ALL dialers, zero vendor lock, unblocks the full scrape→skip-trace→push automation now. Matches PRD ("integrate via Zapier").

## Slice 5B — generic dialer push: ✅ BUILT (Codex-consulted)
- [x] `DeliverConfig`: `dialer_webhook_url` + `dialer_webhook_secret` (separate from job-summary webhook; shared https/secret validators extracted; no secret fallback). Added to `DeliverConfigDict`.
- [x] `webhook_delivery.py`: `build_dialer_push_payload` — event `leads.dialer_ready`, `schema_version`, stable `batch.id`, per-lead `external_id` (retry-safe dedup), flattened scraper fields, `lead_count`/`total_dialer_ready_count`/`truncated`, HMAC-signed. Cap `DIALER_PUSH_CAP=500` (wide rows). Reuses `_sign_payload`.
- [x] `tasks.py` completion path: if `dialer_webhook_url` set → sync `select(Result).where(job_id,user_id, *dialer_ready_conditions()).order_by(id).limit(500)` + unbounded total count → build payload → **reuse `deliver_job_webhook.delay`** (SSRF re-validate, HMAC, retry, non-fatal). Host-only logging (no URL/PII). Skips cleanly when 0 dialer-ready.
- [x] 7 payload tests (shape/external_id/batch/truncation/HMAC) + 4 eligibility tests. Builds; ruff clean (no new findings).
- [x] Codex review → **FAIL (P1 + P2), both fixed:**
  - **[P1] timing:** skip-trace is async (cache-miss phone/DNC arrive later via Tracerfy webhook), so pushing at scrape completion missed those leads with no later send. **FIX:** removed the scrape-completion trigger; added a deferred **`dialer_push_sweep`** beat task (every 5 min) that pushes only when a job's skip-trace has SETTLED (no Result still queued/submitted), claimed once via `Job.dialer_pushed_at` (migration **039**). Reuses `deliver_job_webhook`.
  - **[P2] entitlement:** `create_scraper` gated `webhook_url` but not `dialer_webhook_url` → lower plan could push PII. **FIX:** gate both for Business+.
- [x] Codex re-review → **FAIL again (P1 + P2), both fixed:**
  - **[P1] DNC-NULL:** Tracerfy populates phone but leaves `phone_dnc_flag NULL`, so strict `IS FALSE` matched NOTHING → feature pushed zero leads. **FIX:** push (and the 5A view/export filter) use `include_unknown_dnc=True` (exclude only KNOWN-DNC); the **destination dialer does the authoritative DNC scrub** (industry standard), forward-safe if a DNC feed is added. **⚠️ COMPLIANCE NOTE for user:** BridgeLeads does NOT currently scrub DNC (no feed populates the flag); dialer-ready = valid phone + not-known-DNC, dialer is the DNC compliance layer.
  - **[P2] race:** non-atomic claim could double-push. **FIX:** `SELECT ... FOR UPDATE SKIP LOCKED (of=Job)` on the candidate query.
- [x] Codex re-review #2 → **PASS (no P1); 2 P2 fixed:** (1) settled-check now based on `PendingSkipTraceRow` in-flight (queued/submitted) not `Result.skip_trace_status` — errored Tracerfy submissions leave Result stuck 'queued' but the pending row goes 'errored'=terminal, so the job now settles + pushes. (2) `deliver_job_webhook` redacts the receiver response body for `leads.dialer_ready` events (PII could be echoed back into logs/result-backend).
- [x] Codex reviews #3-#5 — converging; fixed: dup-exclusion (`is_duplicate=False` in push); stale-submitted settlement (time-bound, then `COALESCE(submitted_at, enqueued_at)` aging); **entitlement re-check at push time** (join User, current plan ∈ Business+); **honest DNC labeling** (per-lead `dnc_status` clear|unknown|dnc + payload `dnc_scrubbed:false`) — keeps the feature functional (DNC always NULL → strict pushes nothing) while NOT mislabeling; dialer is the DNC scrub layer (PRD: integrate w/ dialers).
  - **⚠️ COMPLIANCE DECISION FOR USER:** Codex oscillates strict-DNC vs functional. Root cause: BridgeLeads has NO DNC feed (phone_dnc_flag always NULL). Resolution per PRD (integrate w/ TCPA-compliant dialers that scrub): push not-known-DNC + label honestly. If you want BridgeLeads-side DNC scrubbing, that's a separate feature (needs a DNC data source).
- [ ] Final Codex review → present to user.

**Codex consult reconciled:** reuse the task as-is (payload-agnostic); inline capped leads (500) not download URL; per-lead external_id + batch id; total_dialer_ready_count + truncated; schema_version; deterministic ORDER BY id; separate secret; strict host-only/no-PII logging; no skip_trace gate (valid phone + dnc IS FALSE).

## Slice 5C — native per-dialer connectors (FUTURE, optional, demand-gated):
- Only if a paying customer needs deep integration AND supplies API docs. Candidates by API maturity/reach: CallTools, BatchDialer, PhoneBurner. NOT Enzo unless specifically demanded + docs supplied.
- API base URL + env (prod/sandbox); auth (key/OAuth/HMAC/bearer + refresh); endpoint(s) (create/update contact, add to list/campaign, bulk import); payload schema (required fields, phone format, lead IDs/metadata); rate limits + batching; idempotency/upsert (external ID, dup handling); DNC/consent source of truth (Enzo vs BridgeLeads); campaign/list model; error contract (retryable vs terminal); audit/PII-redaction/retention; status callback/webhook.
- Then: dedicated Enzo connector (reuse webhook_delivery patterns: SSRF allowlist, retries, signed/auditable payload, idempotency) + push task selecting dialer-ready (+ optionally skip-traced) leads. "Push to dialer" delivery option (UI = frontend).

## DECISION surfaced to user
- DNC default = TCPA-safe (`dnc IS FALSE`). Want a looser opt-in "candidate" mode (include unknown-DNC) exposed via the API too? (Function supports it; API currently exposes only the safe default.)
