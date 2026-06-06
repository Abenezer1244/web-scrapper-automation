# Native Dialer Connectors Spike — Thread 3 of 3 (2026-06-05)

**Question:** Should BridgeLeads replace/augment the shipped generic webhook dialer
push (P5) with native per-dialer connectors? What's buildable now vs demand-gated?

**Method:** Dynamic-workflow research + adversarial security review (verdict GO-WITH-FIXES).
No code built. This doc is the build-ready plan for when a customer names a dialer.

## What already ships (P5, Codex-clean, live)
Generic vendor-agnostic push: `build_dialer_push_payload` + `deliver_job_webhook`
(`src/workers/webhook_delivery.py`) + `dialer_push_sweep` Beat (`src/workers/scheduler.py`,
every 5 min, at-most-once via `Job.dialer_pushed_at`, skip-trace gate, is_duplicate
exclusion, Business+ plan gate) + `DeliverConfig.dialer_webhook_url/secret`. Works with any
webhook/Zapier dialer today.

## Dialer landscape (real-estate wholesalers)
| Dialer | API | Fit | Blocker |
|---|---|---|---|
| **PhoneBurner** | Public REST, OAuth Bearer, `POST /rest/1/contacts` | Best-documented | **No bulk endpoint** (500 leads = 500 POSTs); OAuth token refresh |
| **BatchDialer** | REST, bulk contacts import, API key | Largest wholesaler base (PropStream-owned) | **Spec paywalled** (developer.batchservice.com login) |
| **CallTools** | REST, `Token` auth, 1000 req/hr | Public | Dev-docs **TLS cert invalid** at research time |
| **Mojo** | Proprietary "Posting PIN" vendor-whitelist | Volume default | **Not a standard API** — Mojo must approve BridgeLeads as a vendor |

## Buildable NOW vs demand-gated
- **Phase A — abstraction seam (~2h, no migration):** `src/workers/dialer_connectors/` package with a
  `DialerConnector` ABC (`validate_config` / `build_requests` / `map_dnc_status`), a
  `GenericWebhookConnector` wrapping the existing payload byte-for-byte, and a `dialer_type`
  discriminator on `DeliverConfig` (default `generic_webhook`) validated against a frozen
  `REGISTERED_VENDOR_IDS`. Safety net: regression test asserting GenericWebhookConnector output ==
  current `build_dialer_push_payload`. **Caveat:** this refactors the LIVE delivery path and the
  transport signature `(job_id, webhook_url, payload)` has no `headers` param, so the seam already
  touches transport — not purely zero-risk, and has NO consumer until a vendor connector exists (YAGNI).
- **Phase B/C — vendor connectors:** DEMAND-GATED. Need a paying customer who names their dialer +
  real credentials (no-mock rule → can't smoke-test without them).

## Security requirements (MUST be satisfied before ANY vendor connector — 4 HIGH)
1. **No vendor creds as Celery task args** — `.delay()` args serialize into the Redis broker +
   result backend in plaintext. Re-read the `deliver` config from DB inside the task (under
   `system_sync_session`) and build the `Authorization` header locally, just before POST. Mirror how
   `dialer_push_sweep` already re-reads config. Replicate the `ScraperConfig.user_id == Job.user_id` +
   `Result.user_id == job.user_id` filters on any new config re-read (RLS-bypassing session).
2. **Per-connector hardcoded HTTPS host allowlist** — vendor URLs are fixed (e.g.
   `www.phoneburner.com`), NOT from the `deliver` JSON. Assert the built URL's host is in the
   connector's allowlist inside the transport, in addition to `validate_outbound_webhook` per request.
3. **Connector-driven response redaction** — the current `_redact_response` keys on
   `event=='leads.dialer_ready'`; vendor-native bodies miss it and echo PII on error. Switch to an
   explicit `redact_response`/`carries_pii=True` flag, default True for all dialer connectors.
4. **Replay path before at-most-once claim ships for vendors** — add `Job.dialer_push_error` (or a
   `dialer_push_status` enum) + an authenticated, `user_id`-scoped replay endpoint that resets
   `dialer_pushed_at=NULL`. Vendor failures (expired OAuth 401, rate-limit across 500 POSTs, outage)
   happen AFTER the claim → today = permanent silent loss of paid-for leads.

Plus (Medium): vendor connectors are **CONTACT-CREATION ONLY** (never call a dial-session endpoint —
TCPA: BridgeLeads has no DNC feed, `phone_dnc_flag` always NULL; keep `dnc_scrubbed=False` + a UI
DNC-responsibility acknowledgment); `dialer_type` via `field_validator` against `REGISTERED_VENDOR_IDS`
+ `extra='forbid'` + length/charset validators on credential fields; 401 path surfaces reference-id +
host + status only (never the vendor body/token); log host-only, test no PII/token in logs.

## Recommendation
- **DEFER all vendor connectors until a paying customer names their dialer** (research + security agree).
  The generic webhook already serves every dialer that accepts a webhook/Zapier hook.
- **The abstraction seam (Phase A) is optional future-proofing, NOT a current need** — it refactors a
  working, Codex-clean delivery path with no connector to plug in yet (YAGNI), and touches the transport.
  Build it only alongside the first real vendor connector, when the byte-equality regression test earns
  its keep.
- **First vendor to build when demand appears:** PhoneBurner (best public docs) or BatchDialer (biggest
  base, but get a developer-portal account first). Avoid Mojo (vendor-approval gate, not code).

## Sources
- PhoneBurner API: https://www.phoneburner.com/developer/route_list
- BatchDialer dev portal: https://developer.batchservice.com/docs/batchdialer/f4e6fa31af431-getting-started
- CallTools API: https://calltools.com/glossary/api-documentation/
- Mojo approved-vendor posting: https://knowledge.mojosells.com/en/article/connecting-to-approved-vendors-one-way-data-posting
