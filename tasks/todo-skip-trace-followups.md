# Skip-trace follow-ups after #342/#156 (2026-09-18)

Worktrees: BE `C:/Users/Windows/bl-wt-prov` (`feat/skip-trace-provenance`, off main b4b648f),
FE `C:/Users/Windows/bl-wt-prov-fe` (same branch, off master d5d1b3a).

The five items left from the "already delivered is not already traced" work:

1. Owner's 38 leads are still untraced (the fix is forward-only) -> owner re-runs the range with
   skip trace on. Spends real Tracerfy credits: ASK the owner before triggering anything in prod.
2. "Reused, no new charge" count on the Already delivered tab. DONE on #344/#157 via
   `results.skip_trace_source` (the API role cannot read the queue tables, so provenance lives on results).
3. Verify the fix in production (done by item 1's run: header, tab, CSV, usage delta).
4. Security: Pre-Launch prompt (§15) before the next prod deploy + Master Review (§14) until two
   clean passes.
5. Audit: can the frontend change enrichment status or trigger lookups on another account's leads?

## Item 5 audit (done before design; file:line on b4b648f)
- No route in src/api writes results at all (no update(Result)/UPDATE results/db.add(Result)).
- Skip-trace state is written only by workers: enrich.py (reuse, enqueue), dispatcher, ingest.
- Paths from an HTTP request to a lookup or a contact write:
  - POST /webhooks/tracerfy[/{secret}]: constant-time secret check, rate limited; ingest applies
    answers only to pending rows already recorded under that queue id. CSV download is SSRF
    validated (every hop, HTTPS only) but NOT pinned to Tracerfy's CDN host: the secret is the gate.
  - POST /jobs: ScraperConfig must belong to current_user (404 otherwise); skip trace comes from
    the stored config, not the request.
  - POST/PATCH /scrapers + batches: skip_trace_enabled is plan-gated server-side (tested in
    test_plan_entitlement_audit).
- Missing tests to add: another account cannot start a run of my scraper; no request body schema
  in the app accepts a skip-trace state field (structural guard against a future route).

## Item 2 design
- Migration 097: `results.skip_trace_source VARCHAR(16) NULL`. Nullable, no default: metadata-only
  in Postgres, no rewrite, no long lock. `bridgeleads_app` has table-level SELECT on results
  (provision_rls_roles.sql:102), so the API can read it.
- Values: 'lookup' = Tracerfy answered for this row (ingest). 'reused' = the answer was copied from
  this account's earlier answer (reuse SQL x2, enqueue cache hit, dispatcher known-answer sweep).
  NULL = never settled, or settled before this column existed (unknown, NOT counted as reused).
- Set only alongside a hit/miss write, in the same statement. errored/purged leave it untouched.
- AlreadyDeliveredContacts gains `reused` (subset of found + none_found with source 'reused').
- FE: "... 2 came from an earlier lookup, at no new charge." only when reused > 0.

## Order
- [x] P1 BE: migration 097, model, 5 writers, API field, tests (writers + summary), openapi.
- [x] P2 BE: item-5 tests (cross-tenant run start, structural no-state-in-body guard).
- [x] P3 FE: summary copy, regenerated types, tsc/eslint, Playwright.
- [x] P4 Security: §14 Master Review x2 (until two clean), §15 Pre-Launch on the full change set.
- [x] P5a Codex diff review until GATE: PASS (r1 FAIL, r2 PASS 2026-09-19); PRs #344 / #157 open.
- [x] P5b Owner approved; #344 merged `5bd9c59`, #157 merged `6c435d0`, both deployed.
- [ ] P6 Owner's custom-range run (Aug 18 to Sep 17, skip trace on), then compare to the frozen snapshot.

## Decisions / disagreements (Codex plan consult, 13 findings, checked against code)
CORRECTION to my own audit above: the CSV host IS pinned. `tracerfy_ingest._host_is_tracerfy`
refuses any non-Tracerfy host before DB work or fetch (REDTEAM B1/T3), tested by
`test_untrusted_download_host_is_refused`; responses are capped at 16 MB (safe_http) and 60 s.
I had read only download_tracerfy_csv. Codex's SSRF P1 is therefore already met.
ADOPTED
- Cross-tenant negative tests on every ID-bearing path that can lead to a lookup or move contact
  data: POST /jobs (foreign config), PATCH /scrapers/{id}, DELETE /jobs/{id}, POST
  /scrapers/{cfg}/jobs/{job}/dialer-replay. Assert 404 AND no job / pending row / change.
- Behavioral (not structural) test: skip_trace_status / skip_trace_source / phones injected into
  scraper create+update bodies are ignored.
- skip_trace_source set only inside the same predicate that copies the answer (reuse stmt 1 gets
  its own CASE on the full copy predicate); every hit/miss writer tested.
- CHECK constraint (NULL | 'lookup' | 'reused'); migration sets lock_timeout so a busy table
  fails the boot migration fast instead of queueing every request behind ALTER TABLE.
- UI copy says exactly what `reused` proves: "no new lookup was bought" (no pending row went to
  Tracerfy for that row, and billing only counts completed/unmatched pending rows).
- Item 1/3 production run: frozen before/after of the exact result ids (read-only pre-check:
  eligibility, expected lookups, ATIP), owner-approved, then after: statuses, source, usage delta.
- CSV: provenance deliberately NOT exported (dialer import layouts are a contract).
REJECTED (evidence)
- "Cache hit may cross tenants": address_cache_key hashes user_id (skip_trace.py:132); tested by
  test_another_accounts_trace_is_never_copied.
- "Stale provenance on retry/purge": a results row settles once per run; the summary reads source
  only for hit/miss rows, and purge moves status to 'purged', out of the count.
- Webhook signatures / removing the legacy path route: Tracerfy does not sign webhooks; retiring
  the path route needs Tracerfy reconfigured first (existing owner step, kill switch
  TRACERFY_LEGACY_PATH_ENABLED). Host pin already bounds a leaked secret.
- Provider charge-ID ledger, P3 metrics: out of scope; logged as follow-ups.

## Review
- Codex r1: GATE FAIL (review status wording; unbounded VALIDATE). Both fixed.
- Codex r2 (2026-09-19): GATE PASS, no P1/P2. Two P3 doc findings adopted: this checklist was
  stale; the security review's §15 table now labels each inherited item as release-blocking or an
  accepted carryover not introduced by this change.
- Verified: full BE suite 4178 passed on an isolated DB (9 Stripe tests fail locally and on untouched
  main: rig env); ruff, openapi --check, FE tsc + eslint clean; Chromium E2E shows the reused line.
