# Tracerfy skip-trace: audit + hardening

Branch: `feat/skip-trace-provider-abstraction`
Worktree: `C:/Users/Windows/bridgeleads-worktrees/skiptrace-provider` (off `origin/main` @ 11c8ca7)

Scope: keep Tracerfy. Audit, harden, make failures visible. No provider swap,
no provider-abstraction rewrite — the current architecture does not need one.

---

## Findings (all verified against prod + Tracerfy's live API, not inferred)

| # | Sev | Finding | Evidence |
|---|-----|---------|----------|
| F1 | **Critical, LIVE** | 637 pending rows (15 jobs, 3 users) stuck at `status='submitting'` since 2026-09-03/09-05, up to 4 days. Their 637 Results sit at `skip_trace_status='queued'` → UI reads "Processing" forever. | prod query; all 637 have `tracerfy_queue_id IS NULL` |
| F2 | **Critical, LIVE** | Dispatcher's post-accept bookkeeping (`skip_trace_dispatcher.py:190-241`) is **outside any try**. If `db.add`/`db.execute`/`db.commit` raises after Tracerfy accepted+charged the batch, the exception escapes the task: claim stays `submitting`, no `SkipTraceQueue` row exists, and the later webhook hits the `unknown_queue` no-op → **paid results permanently discarded**. | code read; **found by Codex**, confirmed by me |
| F3 | High | 14 Tracerfy queues (Jun 7 – Jul 4; 673 rows, **743 credits**) exist on the account with no local `SkipTraceQueue` row. Their webhooks all no-op'd. This is F2's signature. Two pairs look like identical double-submissions (98183/98193, both 147 advanced rows / 144 credits, 45 min apart). | `GET /v1/api/queues/` vs local `skip_trace_queues` |
| F4 | High | Rows are submitted with NULL/empty `state` (7 stuck + 1 stranded). Tracerfy requires address+city+state. On queue 162456 we sent 4 rows, Tracerfy's `rows_uploaded=3` — it **silently dropped** the state-less row. No pre-submit validation exists. | prod query + queue 162456 |
| F5 | High | Ingest matches Tracerfy's echoed CSV address to our pending address by **exact lowercased string equality**. A non-matching row is silently `continue`d: no counter, no log, no terminal status. Row stays `submitted` forever and is never billed (`report_usage_from_webhook` counts only `'completed'`). | `tracerfy_ingest.py:306-314`; 1 live case on q162456 |
| F6 | High | **Zero test coverage** on the entire ingest path — the code that maps provider results to leads, writes contacts, and advances billing. | no test references `ingest_tracerfy_batch` / `ingest_webhook_csv` |
| F7 | Medium | `SkipTraceQueue.job_id/user_id` store only `claimed[0]`'s values, but batches are **cross-tenant**. Misleading ops/tenancy metadata. | `skip_trace_dispatcher.py:198` |
| F8 | Medium | Ingest downloads + parses the CSV **before** taking the queue lock. Concurrent duplicate webhooks both download and parse; only the DB mutation is serialized. Wasteful, not incorrect. | `tracerfy_ingest.py:231` |
| F9 | Medium | Orphan remote queues (F3) can never be ingested — the precheck rejects a missing `SkipTraceQueue`. No controlled adoption path exists. | `tracerfy_ingest.py:218` |

Alerting context (corrected after Codex challenge): `send_ops_alert` is **not**
a silent no-op — it logs a WARNING and persists a durable `audit_events` row from
a `finally`. But `OPS_ALERT_EMAIL` is empty in prod, so no human was ever paged,
and under FORCE RLS the app role cannot SELECT `audit_events` to find the trail.

Verified NOT broken (do not "fix"):
- `phone_dnc_flag` is always NULL from batch trace — **correct**. Tracerfy's batch
  CSV carries no DNC (only the Instant Trace endpoint does). `map_dnc_status`
  honestly reports "unknown" and `dialer_filters` excludes NULL from the TCPA-safe
  default. Working as designed.
- Phones/emails are encrypted at rest (`fe1:` Fernet). Confirmed, no plaintext.
- Webhook auth, SSRF pinning, replay idempotency under the queue lock: all sound.

---

## Plan

### Phase 1 — dispatcher: stop losing paid batches (F2, F4)
- [x] 1a. Wrap the post-accept bookkeeping in a guard; on failure, durably record
      the `queue_id` ↔ claim association and alert. Never leave a charged remote
      queue with no local record.
- [x] 1b. Pre-submit validation: rows missing address/city/state are removed from
      the payload **before** the POST and marked terminally on **both**
      `PendingSkipTraceRow` and `Result` (so the UI stops saying "Processing").

### Phase 2 — automated stale-claim reconciliation (F1, F9)
- [x] 2a. On a stale claim, call `GET /v1/api/queues/` and decide, using Codex's
      conservative predicate: same `trace_type`, `created_at` inside the claim
      window, `rows_uploaded <= len(claimed)` (never `==` — Tracerfy dedupes),
      `rows_uploaded > 0`, and **exactly one** candidate. No match → release to
      `queued`. One match → adopt its `queue_id`. Ambiguous → alert, never guess.
      Never blind-resubmit.

### Phase 3 — ingest: no silent drops (F5)
- [x] 3a. Count + log unmatched CSV rows and unmatched pending rows, give them a
      terminal status, and alert. Deliberately **not** adding a fuzzy/normalized
      fallback matcher: there is no evidence Tracerfy standardizes addresses
      (126/126 completed rows matched exactly), and Codex confirmed a normalized
      street match risks cross-lead contamination (units, duplexes, directionals).
      Measure first — Phase 3a is the measurement.

### Phase 4 — tests (F6)
- [x] 4a. Ingest: successful match, no-match vs failure, unmatched row, webhook
      replay billing idempotency, cross-tenant batch isolation, phone/email dedupe.
- [x] 4b. Dispatcher: post-accept bookkeeping failure, stale reconciliation
      (no-match / single-match / ambiguous), dedupe count mismatch, invalid-address
      terminal status.

### Phase 5 — repair (separate, reviewed, run after Phases 1-4 deploy)
- [x] 5a. Read-only reconciliation report for the 637 + the 14 orphan queues.
- [ ] 5b. Release the 637 — **no longer needs the script**. Once Phase 2 deploys,
      `_reconcile_stale_claims` releases them automatically on the first tick.
      Dry run confirms all 637 release, 0 adopt, 0 refuse. Do NOT run the script
      against the currently-deployed (old) dispatcher: it would re-strand them.

---

## Open decisions for the owner

1. **Billing an accepted-but-unmatched row.** Tracerfy charges per accepted row.
   Today an unmatched row is silently not billed to the user. Codex argues for
   billing it ("provider attempted the lookup"). I disagree on defaulting to that:
   an unmatched row is *our* reconciliation bug, and charging a user for a lead
   they never received is user-hostile. Phase 3a makes it visible and alertable
   without changing who pays. **Billing policy change is yours to make.**
2. `OPS_ALERT_EMAIL` is unset in production. Every one of the 15 alert call sites
   is currently mute. This is the reason F1 ran for 4 days unnoticed.

---

## Review

### Shipped (6 commits on `feat/skip-trace-provider-abstraction`)

| Commit | What |
|---|---|
| `c1099e9` | Guard the post-accept bookkeeping (F2); reject untraceable rows (F4) |
| `25cc8fb` | Automated stale-claim reconciliation against Tracerfy's queue list (F1, F9) |
| `e7bcecb` | Settle ingest rows the CSV never named (F5); first ingest tests (F6) |
| `b979575` | One-off repair script + orphan-queue report (Phase 5) |
| `bc92658` | Codex round 1: double-charge + cross-batch adoption + redrive durability |
| `1874658` | Codex round 2: release window, attribution guard, missing `on_failure` |

### Codex rounds

**Round 1 — 6 findings, 4 accepted.** Two were mine and severe: the post-accept
bookkeeping ran unguarded (the live cause of the 14 orphaned paid queues), and
the reconciler released a claim whose queue was merely *pending*, which would
have resubmitted and double-charged. One finding (ingest match key) was rejected
**with measurement** rather than argument: production has exactly one in-batch
key collision ever and it is benign.

**Round 2 — 3 findings, all 3 accepted, all in my own round-1 fixes.**
Release and adoption were sharing one window; the attribution guard had two
escape routes (one answer + differently-named rows, and all-NULL advanced
names); and `on_failure` never existed despite the docstring claiming it, which
my new redrive sweep would have turned into an infinite re-enqueue.

**Round 3** cut off mid-run (no final message). Its open question — whether
assigning `.on_failure` on a Celery task instance actually fires — was answered
empirically instead, with two tests that invoke the hook the way Celery does and
assert on the database. Both pass.

### Not fixed, deliberately

- **F7** `SkipTraceQueue.job_id/user_id` still store `claimed[0]`'s values on a
  cross-tenant batch. Documented in place as non-authoritative; nothing reads
  them for tenancy (ingest re-derives per-user attribution from the pending
  rows). Changing the columns is a migration for cosmetics.
- **F8** ingest downloads and parses the CSV before taking the queue lock.
  Wasteful under a duplicate webhook, never incorrect — the lock still
  serialises every mutation.
- Two genuinely different people with the same normalised name at one address
  would be treated as one owner. Indistinguishable from the same person with the
  data available.
- The 8 prod rows whose address carries a ZIP but no city/state
  (`'325 HARVARD AVE E #401 98102'`) are declined rather than recovered. The
  scraper config knows its own `state`, so passing it into
  `build_pending_row_payload` would rescue most of them — a real follow-up, but
  it means threading a new argument through a hot path for 0.16% of rows.
- One prod address has legal-notice text bleeding into `property_address`
  (`'3046 36th Avenue W, Seattle, WA 98199 I. NOTICE IS HEREBY ...'`). That is
  an NTS parser defect, not a skip-trace one.
