# Phase 1: "Look up contacts" action + reuse correctness (2026-09-19)

Worktrees: BE `C:/Users/Windows/bl-wt-lookup` (`feat/lookup-contacts-action`, off main `3abe087`),
FE `C:/Users/Windows/bl-wt-lookup-fe` (same branch, off master `6c435d0`).

Design review that led here: Codex design consult 2026-09-19 (current design pros/cons, target
design: lookup action -> ledger -> canonical leads). This file is **Phase 1 only**.

## Why
Today contacts can only be bought as a side effect of a scrape. To trace 38 already-delivered
leads the owner had to build a second scraper with a custom range and re-scrape Pierce. Separately,
answer reuse is keyed on the address with **no owner name**, so within the 90-day window a lead can
inherit the previous owner's phone (probate makes this likely: deceased owner, then an heir).

## Verified facts this plan is built on (file:line)
- API role has NO grant on `pending_skip_trace_rows`, `skip_trace_cache`, `skip_trace_queues`,
  `delivered_records` (`scripts/provision_rls_roles.sql:151-152,183`). It CAN read `results`.
  => the confirm endpoint must dispatch a Celery task; an exact "reused" count is worker-side only.
- `_enqueue_skip_trace_rows` (`src/workers/tasks_helpers/enrich.py:2008`): per job, no result-id
  filter, commits internally, logs via Redis, gate rejects only `starter` (the API gate uses the
  allowlist `SKIP_TRACE_ADDON_PLANS`, `src/config/constants.py:211`).
- No unique constraint on `pending_skip_trace_rows.result_id` (`src/db/models.py:1235-1290`);
  active pending statuses are `queued`, `submitting`, `submitted`.
- `results.skip_trace_status` itself carries `queued` and `submitted` (written by the enqueue and by
  the dispatcher, `skip_trace_dispatcher.py:859`), so **the quote can see in-progress rows from the
  `results` table alone**, with no grant on the queue table. The queue stays worker-only.
- Reuse keys with no name: `address_cache_key` (`src/scrapers/enrichment/skip_trace.py:116`) used by
  the enqueue cache read (+ legacy locality fallback), the dispatcher `_answer_key` (known-answer
  sweep + in-flight hold, `src/workers/skip_trace_dispatcher.py:569-759`) and the ingest cache write
  (`src/workers/tracerfy_ingest.py:684`); plus the `dedup_hash` reuse passes (`enrich.py:190`),
  where strong `dedup_hash = sha256(parcel|address)` is frozen (billing key).
- The pending row already stores the exact subject sent to Tracerfy: `first_name`, `last_name`,
  `trace_type` (`normal` = name + address, `advanced` = address only, 2 credits).
- `GET /jobs/{id}/download` rebuilds the CSV live from DB rows, and the emailed link points at it,
  so contacts found later appear automatically. The R2 object and batch combined export go stale.
- `SKIP_TRACE_DAILY_ROW_CAP` is GLOBAL across tenants and pauses dispatch silently
  (`skip_trace_dispatcher.py:71`); prod value 1000.
- Billing counts pending rows `completed` (+ `unmatched` under a condition), 1 per row even for a
  2-credit advanced trace (`src/api/billing/skip_trace_usage.py:537,614-646`).
- Tenancy: there is no organization, team or account table; `users.id` IS the tenant identity
  (verified in `src/db/models.py`). Everywhere below, "account" means that `user_id`, and it stays
  the outermost part of every key.

## Decisions for the owner (recommendation first)
- **D1 Address-only (advanced) answers.** Recommend the explicit matrix below. Alternative:
  never reuse advanced answers (costs more, buys no real accuracy).

  | Trace | Reuses | Isolated by |
  |---|---|---|
  | normal (name + address) | only answers bought for the SAME subject | account + address + first/last name |
  | advanced (address only) | any advanced answer for that address in the account | account + address only; owner isolation does NOT apply, because no name was ever sent |

  A normal trace never reuses an advanced answer, and the reverse, because `trace_type` is part of
  the key. The account boundary holds in both cases.
- **D2 Advanced costs 2 Tracerfy credits but bills the customer 1 lookup.** Recommend: the quote
  shows what the customer is billed (rows), and the margin gap is logged as its own item, not
  silently folded into this work.
- **D3 Daily cap.** Recommend: surface it end to end. When the dispatcher pauses it writes
  `skip_trace:daily_cap_paused` to Redis with the resume time and a TTL of two ticks (600s),
  **refreshed on every paused tick** so it survives a long pause, and **deleted explicitly on the
  first tick that resumes dispatch** rather than waiting for the TTL. Tested both ways: it stays
  while paused, and no stale "paused" state is ever shown after dispatch resumes. The API (which already has a Redis client for
  rate limiting) reads it in the quote and in the job results summary; if Redis is unreachable the
  field is simply absent and nothing else breaks. The dialog warns before confirming, and after
  confirming the results page says lookups are paused and when they resume, instead of an endless
  "looking". A per-account cap is deferred.

## Phase 1a - reuse correctness (BE, no migration)
- [ ] `lookup_subject_key(version=2, user_id, address, city, state, trace_type, first, last)`:
      NFKC, collapse Unicode whitespace, trim, case-fold; punctuation preserved; no invented
      equivalence (middle initials, hyphenation, diacritics). Versioned namespace.
      Serialization is unambiguous, not delimiter-joined: `sha256(json.dumps([...], ensure_ascii=
      False, separators=(",",":")))` over a fixed field order, where a missing name is JSON `null`
      and an empty string is a distinct value. An advanced subject is `null` first AND last plus
      `trace_type="advanced"`, so it can never collide with a normal trace whose name is missing.
      "Address" in this key means the street line, city and state as three separate normalized
      fields (never one concatenated string), which is what the current helper already passes.
- [ ] Switch all FIVE call paths together, enumerated so none can be left on the legacy key:
      (1) the enqueue cache read, (2) the dispatcher's known-answer sweep, (3) the dispatcher's
      in-flight hold, (4) the ingest cache write, (5) the `dedup_hash` reuse passes plus the
      charged-unanswered check. The test matrix names all five explicitly. The subject always comes from the pending row (dispatcher,
      ingest) or the payload actually built (enqueue), never recomputed from `party_name`.
- [ ] Stop reading legacy address-only keys (and the legacy locality fallback). Correctness comes
      from the new code reading only v2 keys; the old rows are then inert. The cutover is enforced
      with the EXISTING global kill switch rather than a hopeful deploy note: set
      `SKIP_TRACE_ENABLED=false`, deploy API and worker, confirm both are on the new build, then turn
      it back on. No lookup can run on mixed versions, so an old worker cannot serve a legacy hit or
      write a legacy key. **Deleting the legacy rows is a separate ops step AFTER that**, purely as
      PII hygiene (never as the correctness mechanism), with the deleted row count recorded. Cost:
      repeat addresses pay again for up to 90 days (cache retention is 90 days anyway, so it
      self-heals).
- [ ] `dedup_hash` reuse passes + the charged-unanswered check: compare the canonical subject
      (same helper) rather than raw SQL equality, so two NULL names do not silently stop matching
      and two different owners never match.
- [ ] Tests (isolated DB), matching the D1 matrix exactly: for NORMAL traces, a different owner at
      the same address does NOT reuse, at each of the 5 sites; the same owner does; for ADVANCED
      traces, the same address DOES reuse regardless of owner, but never across accounts; normal
      never reuses advanced and the reverse; tenant isolation holds; normalization cases (case,
      Unicode/NFKC, whitespace, punctuation, missing first or last, unit numbers, state case).

- [ ] Required at diff time (Codex round 13, condition of its PASS): a test that seeds a
      legacy-only cache hit and proves NONE of the five paths reuses it (including the locality
      fallback); a test that an advanced trace hashes null names regardless of the payload or
      `party_name`; a test that pending rows created BEFORE the cutover are processed with the v2 key
      by both the dispatcher and ingest; a test that the `dedup_hash` passes and the
      charged-unanswered check use the same canonical helper. Plus a counter for v2 key usage and a
      log line on any unexpected legacy-key access, so the cutover can be verified from production
      rather than assumed.

## Phase 1b - the action, backend
- [ ] Read-only prod pre-check for (a) duplicate active pending rows per `result_id` and (b) rows
      where `results.skip_trace_status` and the active pending rows disagree, since the quote's
      in-progress classification assumes that invariant. Report both. The second becomes a daily integrity check owned by the same ops alert path as the
      other schedulers, reporting counts only, and the repair is the documented script above. Drift
      does not block unrelated quotes, but it does block the migration.
      **What drift can and cannot do is stated plainly rather than wished away**: the API cannot
      see the queue, so a drifted row (results says `not_attempted` while an active pending row
      exists) CAN be quoted, which makes the displayed number too high. It cannot be bought twice:
      the worker does see the queue, classifies such a lead as `in_progress_elsewhere`, and the
      partial unique index refuses a second active pending row regardless. Drift therefore costs
      accuracy in the quote, never money, and the quote already says the number can only go down. If any
      exist, the resolution path is explicit and owner-approved before migration 098: a script keeps the row with the
      strongest **submission evidence** (a `tracerfy_queue_id` or `submitted_at`, then status
      `submitted` > `submitting` > `queued`), using age only as a tie-break, because a NEWER row may
      be the one that actually reached Tracerfy and age alone would discard paid work. It cancels the
      others with a reason **only when they carry no submission evidence**: a local `cancelled`
      cannot undo a Tracerfy submission or its charge, so any duplicate that was actually submitted
      is QUARANTINED (left untouched, listed with its `tracerfy_queue_id`) and the migration halts
      for owner-approved reconciliation. It first writes a JSON backup of every affected row (id,
      result id, prior status, `tracerfy_queue_id`, `submitted_at`, `enqueued_at`, `action_id`) to
      the ops scratch location, kept for 30 days, and the restore procedure is written down and
      tested on a copy before the real run. It reports the counts. The migration aborts with that
      instruction if duplicates are still present.
- [ ] Migration 098, run inside the SAME quiesced window as the 1a cutover (kill switch off, so the
      enqueue and the dispatcher are not writing to the queue at all; this closes the race where a
      live write inserts a new duplicate between the cleanup and the index):
      1. **`contact_lookup_actions`**: id, user_id, job_id, category, quote_id (UNIQUE), status,
         pricing snapshot (unit_price_cents, currency, pricing_version), quoted/claimed/reused/
         newly_queued/billable/credits counts, truncated flag, created_at, dispatched_at, started_at,
         lease_token, lease_expires_at, claimed_at,
         settled_at, plus `status_reason` and `status_changed_at` as CURRENT-state metadata.
         The history itself is an append-only `contact_lookup_action_events` (action_id, optional
         result_id, from, to, reason, lease_token/attempt, at), because a single pair of columns
         can only ever show the latest hop and a disputed charge needs the whole path. The API's
         own `created -> dispatching` hop is an event too, and a per-lead hop (a `released` or an
         `abandoned` row) carries its `result_id`, so a partially claimed action can be
         reconstructed lead by lead rather than only in aggregate. The table has its own `id` and
         `user_id` with tenant-scoped foreign keys and RLS like its siblings, and it is
         append-only in the grants (no UPDATE or DELETE for either role).
         **The invariant is that an event is appended in the SAME transaction as the change it
         records**, for: dispatch, start, claim, reuse, each exclusion, release, abandonment,
         settlement, failure, expiry and retry. An event that is not written means the change did
         not happen either, which is the only way the history can be trusted. RLS + user_id
         like every tenant table. The API role gets SELECT, INSERT and UPDATE here (it creates the
         action at confirm time and stamps `dispatched_at` / status), with owner-scoped RLS
         policies; neither table holds contact PII, only counts and verdicts. The worker writes
         through its own system session as usual.
      2. **`contact_lookup_action_results`**: action_id, result_id, user_id, disposition,
         decided_at, **UNIQUE (action_id, result_id)**. Same tenant treatment as the parent: RLS,
         `user_id` on the row, API role SELECT + INSERT (confirm writes the quoted set; only the
         worker transitions a verdict afterwards),
         and a composite foreign key carrying `user_id` so a row can never point at another
         account's action or result. `disposition` has a CHECK with the canonical vocabulary, so the
         values live in the schema rather than in application code:
         `quoted` (the initial verdict written at confirm time; every other value is a transition
         out of it, and only a `quoted` row can be claimed) |
         `newly_queued` | `reused` | `already_answered` | `in_progress_elsewhere` | `ineligible` |
         `released` (a queued row handed back by the cancellation sweep) |
         `abandoned` (the action failed or expired before this lead was claimed) |
         `excluded_no_address` | `excluded_placeholder_address` | `excluded_settled_code_violation` |
         `excluded_atip_policy` | `excluded_not_traceable`.
         This is what makes each action's outcome derivable: a reused answer and a lead that became
         ineligible elsewhere create no pending row, so pending rows alone could never tell them
         apart. The action's counts are aggregated from these rows, not guessed.
      3. `pending_skip_trace_rows.action_id` UUID NULL, indexed, written by the action's claim and
         left NULL by the scrape path, so a concurrent scrape is never counted as this action's work.
      4. The duplicate cleanup (below), verified clean, AND the status-drift repair, both verified
         clean: the migration refuses to proceed while either exists, because quoting depends on
         that invariant.
      5. Partial unique index on `(result_id)` WHERE status IN ('queued','submitting','submitted'),
         CREATE INDEX CONCURRENTLY in autocommit (the pattern from 097).
      Prerequisite for the composite foreign keys: `results` and `contact_lookup_actions` each need
      a UNIQUE `(id, user_id)` (redundant with the primary key, which is exactly what makes a
      composite FK creatable), added before the child table.
      Quiescence is a procedure, not just a flag: turn the kill switch off, **scale the worker
      service to zero and confirm no skip-trace task is running** (no `submitting` rows, beat
      stopped), and only then run the cleanup and the index creation. A flag alone would not drain
      a task that is already mid-transaction.
      These two tables are deliberately the first slice of the Phase 2 lookup ledger, so Phase 2
      extends them instead of migrating again.
- [ ] `plan_contact_lookup(results) -> traceable_ids + excluded{no_address, placeholder,
      settled_code_violation, atip, not_traceable, already_answered, in_progress}`, one pure
      function used by BOTH the quote and the worker so they cannot drift. It reads ONLY the
      `results` table, which is what the API role can see: rows whose `skip_trace_status` is
      `queued` or `submitted` are `in_progress` and are never quoted as new lookups. The worker
      remains authoritative and can only ever LOWER the count (a cache hit or a charged-unanswered
      row it can see and the API cannot), never raise it. The quote says so in words.
- [ ] `POST /jobs/{job_id}/contact-lookups/quote` {category}: ownership 404, job must be done (409),
      plan allowlist (structured 402), kill switch/token (friendly 503). Returns quote_id
      (cryptographically random), `max_new_lookups` (named for what it is: lookups that would be
      newly bought; whether each one lands inside the monthly included allowance or becomes overage
      is a billing outcome, not a promise), the excluded breakdown, advanced count, included lookups
      left this month, `paused_by_daily_cap`, expires_at. The quote is explicitly **non-binding**:
      the allowance is re-read at confirmation, and the dialog says the final charge depends on the
      allowance left when the lookups actually settle.
      Redis, server-side payload {user_id, job_id, category, result_ids, max_new_lookups, planner_version,
      unit_price_cents, currency, pricing_version, included_remaining_at_quote}, 10 minute TTL.
      `max_new_lookups` is defined as **the number of rows that would create a new pending row**
      (billing counts one per row): it excludes in-progress and already-answered rows, and reuse
      discovered worker-side can only lower it. The id list is capped at 2000; a larger tab returns
      `truncated: true` with the covered count, and the dialog says so plainly. The covered set is
      chosen by a deterministic order (oldest `created_at`, then `id`), so re-quoting the same tab
      covers the same leads instead of a shifting window.
- [ ] `POST /jobs/{job_id}/contact-lookups` {quote_id}: re-check every gate; quote must match user +
      job + category. Take an atomic **claim lease** carrying a random owner token
      (`SET quote:{id}:claim <token> NX EX 120`, released or consumed only by a compare-and-delete
      that matches that token, so a timed-out request can never release a newer claimant's lease), so
      two concurrent confirms cannot both proceed. The quote itself is NOT deleted at claim time:
      if the API dies between claim and dispatch, the lease expires in 120s and the user can confirm
      again inside the quote's 10 minutes, with nothing charged and nothing queued. Only after a
      successful dispatch is the quote marked consumed; a failed dispatch releases the lease and
      returns a friendly 503 and marks the action `failed` with the reason **only by
      compare-and-set (`WHERE status='dispatching'`)**: if the worker has already reached
      `running` or `claimed`, the dispatch was in fact accepted and the API must not terminalize
      it, or newly queued rows would sit under a `failed` action. An API timeout racing the
      worker's start and its claim is a test. so a known failure
      is terminal rather than an action left dangling. Retries resolve from the DATABASE
      (`quote_id` is unique on the action row), so a Redis quote that expired or was evicted after
      a successful confirm never blocks the follow-up: the action and its quoted set are durable.
      A `dispatching` action that is never picked up within its deadline becomes `expired`. **The confirm writes the quoted set to the database before dispatching**: the action row plus
      one `contact_lookup_action_results` row per quoted lead, the eligible ones as `quoted` and
      the rest already carrying their exclusion verdict. That durable set, not the task arguments,
      is what bounds the worker: its claim joins to `contact_lookup_action_results` for this
      action WHERE disposition = 'quoted', so it can never touch an id that was not quoted even if
      a task argument said otherwise, and a lead that was in progress at quote time is not in the
      set at all and so cannot be bought later even if it is released in between.
      `max_new_lookups` is the size of that set and cannot be exceeded by construction.
      **A retry is safe in both directions**: the action row is unique on `quote_id`, so a retry
      finds it and returns the same `action_id`; if that action is still `dispatching` with no
      `dispatched_at`, the endpoint dispatches again rather than handing back a task that was
      never sent (duplicate delivery is already harmless). Tested by killing the API both before
      and after the dispatch call. Dispatch is explicitly **at least once**: duplicate delivery is safe
      because the single-transaction claim plus the partial unique index make the pending-row effect
      exactly once, and the audit row is keyed by quote id (upsert), so redelivery cannot duplicate
      or corrupt the counts. Both are tested, not assumed. 202.
- [ ] Worker `lookup_contacts(action_id, job_id, user_id, result_ids, quote_id, pricing_snapshot)`:
      the action row is created by the API at confirm time (status `dispatching`), so the worker
      always has a durable home for its dispositions even if it never runs. It carries the
      quote's `unit_price_cents`, `currency`, `pricing_version` through to the audit row. The
      snapshot is what the user was SHOWN; actual billing stays the existing metered path, so
      storing both is what makes a drift between quote and invoice detectable. **Re-authorizes every id in the database itself**: the claim SQL filters
      `results.id = ANY(:ids) AND results.job_id = :job_id AND results.user_id = :user_id`, so a task
      argument alone can never reach another account's rows, no matter what dispatched it. The same
      statement **re-checks eligibility atomically** (`skip_trace_status = 'not_attempted'` plus the
      eligibility predicate) inside the one transaction that inserts the pending row, so a row that
      was answered, settled or queued between the quote and the claim is simply not claimed.
      **That one transaction also writes exactly one `contact_lookup_action_results` row for EVERY
      quoted result id**, not only the claimed ones: the ids the claim returned become
      `newly_queued`; a cache hit copied in the same pass becomes `reused`; the rest are classified
      from the row's current state (`already_answered`, `in_progress_elsewhere`, `ineligible`) or
      from the planner's exclusion reason. Every quoted lead therefore has a durable verdict, which
      is what makes the counts derivable and the status page honest. Re-checks
      the kill switch and token before submitting; plan allowlist (fix the starter-only worker gate);
      manual mode bypasses the per-scraper
      toggle `config.skip_trace_enabled` ONLY, because the user just asked for these lookups
      explicitly; it never bypasses the global `SKIP_TRACE_ENABLED` kill switch or the missing-token
      check, which are re-validated in the worker as well as at the endpoint. **Order is: validate
      everything (kill switch, token, plan, eligibility) BEFORE the claim; the claim commit is the
      last step and is what makes the action real.** After that commit the rows belong to the
      dispatcher exactly like scrape-queued rows: nothing is stranded, because a queued row is
      always either submitted later, settled from a known answer, or released back to
      `not_attempted` by the existing `_cancel_undeliverable_queued` sweep when its job is cancelled
      or failed. If the kill switch is turned off after the commit, the rows simply wait and the UI
      shows them as waiting, with the paused reason when that is why. **Claim and insert in ONE
      transaction**: INSERT the pending row **already carrying status `queued`** (an active status,
      so the partial unique index applies at insert time) with `ON CONFLICT DO NOTHING ... RETURNING
      result_id`, then UPDATE `results` to `queued` for exactly the returned ids, and commit both
      together. Inserting in a non-active status first would sit outside the partial index and let
      two inserts collide later. A lost race claims nothing and strands nothing; there is one
      commit, so no crash window. The shared claim helper **must not commit**: the caller owns the
      transaction. Never exceeds the quoted ids.
- [ ] Refactor `_enqueue_skip_trace_rows` to share that claim path (optional `result_ids`,
      optional Redis logger) so the scrape path and the action cannot diverge.
- [ ] The action row's counts are **aggregated from `contact_lookup_action_results`** (one row per
      lead, unique per action), written in the same transaction as the claim, so a late redelivery
      re-derives the same numbers instead of overwriting them with zeros, and another process's work
      is never counted as this action's. Tested with a redelivery after partial completion and with
      a concurrent scrape touching the same job. The action row carries: user, job,
      category, quote id, quoted count, claimed count,
      reused count, newly queued count, billable rows, estimated Tracerfy credits (advanced counts
      2, so the row also carries the credits-vs-billable-rows gap for the D2 follow-up), the quoted
      `unit_price_cents` / `currency` / `pricing_version`, exclusions, dispatch outcome. No names,
      addresses or contacts.
- [ ] No record-quota change (the page already promises a lookup "never counts it as a new record").
- [ ] Tests: foreign job/quote -> 404 and no side effect; plan 402; job not done 409; kill switch;
      same quote twice and two concurrent confirms -> one set of pending rows; **action vs scrape
      race** (the action and a scrape enqueue claiming the same result concurrently -> exactly one
      pending row, nothing stranded in `queued`); duplicate task delivery -> no second pending row;
      rows that became eligible after the quote are not added; quote counts == worker planner;
      audit row written with the pricing snapshot.

## Phase 1c - the action, frontend
- [ ] "Look up contacts" button on the results header for the current tab. It is shown whenever
      the tab has leads that have never been looked up; when the quote comes back with
      `max_new_lookups = 0` the dialog explains why (every lead is already answered, in progress,
      or excluded, with the breakdown) and offers no confirm button. Hiding the button outright
      would leave a user who expects lookups with no explanation at all.
- [ ] Confirm dialog (precedent: the plan-change charge confirm in `components/settings/BillingTab.tsx`):
      up to N paid lookups, some may reuse an earlier answer at no charge, the full exclusion
      breakdown, included lookups left this month, then the per-lookup price. No em dashes
      (`node scripts/find-user-facing-dashes.mjs`).
- [ ] The confirm returns an `action_id`, and `GET /contact-lookups/{action_id}` (owner-scoped,
      reading the two new tables) is the durable status: quoted, bought, reused at no charge, still
      looking, excluded, truncated, paused reason. The page polls THAT, so the outcome survives a
      reload, a retry, several actions on one job, and a truncated scope, which invalidating the
      general results query could never show reliably. The results list is invalidated alongside it.
- [ ] Plan gate via `canSkipTracePlan`; 402 renders the existing plan notice.
- [ ] Verify in Chromium (no FE test runner): quote, confirm, progress, excluded-only case,
      truncated scope, paused-by-cap case.
- [ ] Action state machine, written down and tested, with a timestamp and a reason on every
      transition: `dispatching` (row created, task sent) -> `claimed` (claim committed, dispositions
      written) -> `settled` or `failed` (dispatch failed, nothing claimed, nothing charged) or
      `expired` (the task never ran within its window).
      The worker's FIRST act is an atomic status transition that takes the action to `running` and
      **takes a fencing lease** (`UPDATE contact_lookup_actions SET status='running',
      started_at=now(), lease_token=:token, lease_expires_at=now()+interval '10 minutes'
      WHERE id=:action_id AND status='dispatching' RETURNING id`), and the claim transaction
      itself re-checks `status='running' AND lease_token=:token AND lease_expires_at > now()`.
      A worker that stalls, gets terminalized by the reconciler and then wakes up therefore
      commits nothing, because its lease no longer matches. The reconciler for its part only
      terminalizes an action whose lease has EXPIRED, never one that is actively leased. That
      zombie-worker race is an explicit test; if it returns nothing, the
      action was already expired, running or finished and the worker stops without touching a
      single row, which closes the race where a late task starts after the reconciler expired it.
      **No quoted row is ever left dangling**: whenever an action ends as `failed` or `expired`,
      the same transaction finalizes every remaining `quoted` row to `abandoned` (in the CHECK
      vocabulary), so every quoted lead has a terminal verdict whatever happens, and the status
      page can say plainly that nothing was bought for them.
      `claimed` then means exactly what it says: the claim transaction committed. A `running`
      action whose claim never commits (crash, gate failure) is found by the reconciler and is
      either retried once or marked `failed` with a reason, so quoted rows never sit without a
      final verdict. **Every reconciler transition is itself compare-and-set and lease-fenced**
      (`WHERE status=:expected AND (lease_expires_at IS NULL OR lease_expires_at <= now())`),
      including the retry path, so terminalization can never land on an action that a worker is
      actively leasing. Tested both ways round: terminalization racing the worker's FIRST
      transition, and terminalization racing its claim. If a gate fails AFTER rows were
      claimed (token pulled, plan lapsed), the reconciler releases back to `not_attempted`, with
      disposition `released`, **only rows that are still locally `queued` with no
      `tracerfy_queue_id` and no `submitted_at`**: a row that already reached Tracerfy may have
      been charged, so it stays tracked to its real ending and is never released. The release is a
      single conditional UPDATE carrying those predicates, and **the whole release commits as ONE
      cross-table transaction**: the pending row, `results.skip_trace_status`, the action-result
      disposition, the action counters and the event row move together, because a crash between
      any two of them would leave a released row still active, or a result reading
      `not_attempted` while a pending row is live, or an audit that disagrees with the queue.
      Failure is injected at each boundary in tests. It is safe because of a guarantee
      that already exists and was verified for this plan: the dispatcher commits
      `queued -> submitting` (with `submitted_at`) BEFORE it contacts Tracerfy
      (`src/workers/skip_trace_dispatcher.py:280-289`), so a row being submitted is never still
      `queued` for the release to grab. The race is a test regardless.
      **Settlement is an explicit reconciler, not a hope**: a short beat task (alongside the existing
      schedulers) settles any `claimed` action whose `newly_queued` rows have all reached a terminal
      state, and it is the single place that handles every way a row can end: answered by Tracerfy,
      settled from a known answer, unmatched, errored, or **released back to `not_attempted` by
      `_cancel_undeliverable_queued`** (that row's disposition becomes `released`, so the status page
      says so instead of showing "still looking" forever). The same task marks a `dispatching` action
      `expired` if its task never ran. An action can therefore never sit in "still looking"
      permanently **for want of reconciliation**, and that is a test, not a claim. What the design
      deliberately does NOT promise is a deadline on the lookups themselves: if dispatch is
      paused (daily cap) or disabled (kill switch), a claimed action stays in a waiting state for
      as long as that lasts, by design. The page says which of the two it is, with the resume
      time when there is one, so waiting is always explained rather than mysterious.
- [ ] Error copy: when every quoted lead has become ineligible before the claim (someone else
      looked them up, or a scrape did), the action is a clean no-op: the audit row records zero
      claimed, and the page says there was nothing left to look up and nothing was charged.
      An expired or evicted quote returns 409 `quote_expired` and the dialog offers to
      re-quote (safe to retry, nothing was charged); an unknown category is 422; Redis unavailable
      is a friendly 503 and nothing is queued.

## Deferred (logged, not in Phase 1)
Lookup ledger (Phase 2), canonical leads (Phase 3), per-account daily cap, DNC flag is never set
and exports suppress nothing, advanced = 2 credits vs 1 billed row, re-send email, stale R2 object
and batch combined export, per-row checkboxes.

## Review
(at the end)
