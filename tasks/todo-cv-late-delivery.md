# Code violations: deliver a held record when it becomes complete (option 2, owner 2026-10-30)

Branch `feat/cv-late-delivery`, worktree `C:/Users/micha/bl-wt/cv-late`, base origin/main 25499289.
Owner decision: a held code violation is delivered and billed automatically once its missing owner/mailing
is found, without waiting for the next run. Never deliver an incomplete record (the gate stays strict).

## Facts (verified in code, 2026-10-30)
- F1 The background owner sweeps never touch a held row: cv_owner_recovery.py `_ELIGIBLE_ROW` needs
  `delivery_excluded_reason IS NULL` AND `actionable_sql` (which includes qualified_sql, which needs a usable
  party_name) AND a blank party_name: a stamped row lacking an owner can never be eligible, held or not.
  pierce_cv_owner_recovery.py `_ELIGIBLE_ROW` needs `delivery_excluded_reason IS NULL`. cv_mailing_recovery.py
  has no exclusion check (it can locate a held SDCI row's parcel) but never names the owner afterwards.
  => Since #542, nothing completes a held record between runs. (Only a later run's own enrichment of its
  prior_run copy + claim transfer delivers it; the hidden-duplicate skip in enrich.py is King tax only.)
- F2 Billing is once per job: finalize.py one CAS on jobs.billing_applied_at sets billed_count from
  read_partition().new; quota reserve_job_quota is a one-time CAS on jobs.reserved_at. Nothing bills a row
  after a job is done.
- F3 Held rows keep their delivered_records claim; transfer_undelivered_claims (dedup.py) can move it to a
  later run's actionable prior_run copy (holder done, delivered_a_row false); the old row becomes 'superseded'.
- F4 Frozen at done: the R2 file, email/webhook counts, job_completed notification, breakdown_* snapshot
  (breakdown_from_job rejects a snapshot unless new == record_count == billed_count -> the UI then shows no
  breakdown), dialer push (once per job, dialer_pushed_at CAS; outbox insert idempotent on (job_id,result_id)),
  skip-trace enqueue (once per run). Live: /jobs/{id}/download (built from rows), Results tab counts, segments,
  batch export.
- F5 Reusable: degradation_repair.py re-runs _enqueue_skip_trace_rows for a done job inside an RLS session.

## Plan (proposed; Codex consult before code; each phase <= 5 files, approval between phases)
### Phase A: the sweeps complete held rows (no delivery change)
- cv_owner_recovery / pierce_cv_owner_recovery: eligibility = the row is a stamped code violation that is
  held (`delivery_excluded_reason = 'not_qualified'`) or unheld-and-actionable as today; has an address; blank
  owner; parcel proven as today. Same lookup pace, lease, breaker, attempts and settle rules. Write guard
  re-applies the same predicate. Held rows rank after unheld ones (delivered leads first).
- Never unholds anything. Tests: held row gets named; held superseded row is not touched; other tenant's row.
### Phase B: late delivery (new worker module + beat entry)
- Each tick (bounded), per account, rows that are: held (`not_qualified`), not duplicate, of a done job,
  qualified NOW (qualified_sql), still the claim holder (delivered_records.first_result_id = row.id, no
  superseded), account active/not frozen.
- Per row, one transaction, lock order = result row FOR UPDATE, then claim, then users row:
  1. re-check all of the above;
  2. quota: grant 1 from the account's CURRENT window (records_used + 1 <= limit) -> if no room, leave held
     (stays in Held back; next window or a later run takes it). Unlimited plans: always.
  3. unhold: delivery_excluded_reason -> removed; write enrichment_data.late_delivery =
     {delivered_at, period_start, policy} (the per-row idempotency key: a row with late_delivery is never
     charged again; the hold record stays for audit as qualification_hold).
  4. users.records_used += 1 in the same transaction.
- Same-run siblings of the row: unheld with it (they never bill; they follow the survivor as today).
- Frozen per-job numbers stay frozen (billed_count, breakdown snapshot, record_count, R2 file, email count).
  The late charge is recorded on the row only, so breakdown_from_job keeps validating. The Results "new" list
  and the live /download already include an unheld actionable row -> the row appears in its run, marked
  "Delivered <date>, after the run" (API flag from late_delivery; FE label).
- Downstream: dialer (if the config has a dialer: insert outbox row, idempotent), paid skip trace (if the
  config has it on: reuse _enqueue_skip_trace_rows for the job, as degradation_repair does), lists membership
  already upserted at job end.
- Notify: one in-app notification per account per tick: "N held records are now complete and were delivered"
  (no per-row email storm).
### Phase C: API/FE + monitoring
- ResultRow.delivered_late (+ date); Held back tab shows "Delivered on <date>" for unheld rows? (they leave
  the tab); run summary line "N delivered after the run". Monitoring: invariants accept late-delivered rows
  (late_delivery present => not a breach); a late-delivered row must be qualified at delivery (new invariant).

## Open questions for Codex / owner
- Q1 Bill against the window the row is delivered in (proposed) or the run's window?
- Q2 No quota room: leave held (proposed) or deliver as over_quota?
- Q3 Should a superseded held row (a later run took the claim) ever late-deliver? Proposed: never (the later
  run already delivered and billed it).
- Q4 Do run-level counts (record_count/billed_count) stay frozen (proposed) with the late count shown
  separately, or move?

## Codex consult r1 (GATE FAIL, 6 P1 / 5 P2): reconciliation
Codex agreed with the proposed answers: Q1 bill the window in effect at delivery; Q2 no quota room -> stay held;
Q3 never late-deliver a superseded row; Q4 run counts and frozen artifacts never change, late deliveries reported
separately. Findings, all adopted:
- P1-1/P1-2 quota: no direct `records_used += 1`. Extract from reserve_job_quota the shared "grant N under the
  users row lock" step (window rollover, pending downgrade, charge eligibility / frozen / ended -> 0) and call it
  for the late row; both paths then use one rule. The late row is claimed atomically: the late_delivery stamp is
  written in the same transaction, under the row lock, after re-checking it is absent.
- P1-3/P1-6 races: lock order job -> result row (FOR UPDATE) -> claim (FOR UPDATE) -> user, matching transfer
  (row -> claim) and reservation (job -> user). AFTER the locks, re-check in the same transaction: row still held,
  not duplicate, duplicate_reason NULL, job done and owned by the same user, claim first_result_id = row.id,
  qualified_sql true, no late_delivery stamp. Any miss -> skip, nothing written.
- P1-4 siblings: same-run siblings stay held (they are duplicates, never billed; lists/batch exports keep
  duplicates). Only the survivor is delivered.
- P1-5 sweeps: held rows get their own scoped eligibility in Phase A (stamped, held, not duplicate, not
  superseded, done job, address, proven parcel, blank owner); writes fill owner fields only; the row stays held
  and excluded from every delivery and paid path until Phase B's transaction.
- P2-7 never touch billed_count / record_count / breakdown_* (the validator needs new == record_count ==
  billed_count); late charges live on the row (late_delivery) and are summed separately.
- P2-8 copy: a late row appears in Results and the live in-app download; files and email links already sent do
  not change; UI says "Delivered <date>, after the run".
- P2-9 downstream: Phase B does NOT auto-push late rows to a dialer or paid skip trace (both have their own
  once-per-job, quota and claim machinery). Phase D (optional, owner decides): a tracked enqueue per late row.
  Until then the user can run a contact lookup on the row.
- P2-10 one in-app notification per account per tick, counted from committed rows, deduplicated by tick key;
  monitoring: a late-delivered row must carry a valid late_delivery stamp and be qualified at delivery (new
  invariant; the unheld_unqualified invariant from #550 treats late rows as normal unheld rows).
- P2-11 discovery on the system session (bounded, ids only); every mutation in a per-user RLS session
  (as degradation_repair does) with explicit user_id predicates as well.

## Phases (revised)
- A sweeps fill held rows (owner + Tacoma owner), no delivery change. ~3 files.
- B shared quota grant helper + late delivery worker + beat entry + tests. ~5 files.
- C API flag + run "delivered after the run" line (BE), FE label. BE ~4 files, FE ~3 files.
- D (optional) dialer / skip-trace for late rows.

## Phase A done (2026-10-30)
- [x] cv_owner_recovery + pierce_cv_owner_recovery name held code violations (stamped, not_qualified, address),
      delivered leads first, never unholding. Write guards re-apply eligibility.
- [x] Tests: 149 passed in both sweep suites; negative control (src reverted) fails 5 held-row tests.
- [x] Codex review GATE PASS (r1); its two P3 test gaps adopted (held other-tenant row; hold changed mid-lookup,
      King and Tacoma).

## Phase C plan (2026-10-31; A #555 and B #559 merged and live, first prod tick clean)
Goal: the user can tell a late-delivered lead from one their run delivered, and hears about it once.
No migration (notifications.type has no DB constraint; late_delivery lives in enrichment_data).

### C1a BE notification (<= 4 files: src/config/constants.py, src/api/schemas.py, src/workers/cv_late_delivery.py, tests)
- NotificationType.LATE_DELIVERY = "late_delivery"; NotificationPrefsUpdate.late_delivery toggle (default on,
  create_notification already honours prefs[type] is False and fails closed on unknown types).
- cv_late_delivery: after an account's rows are committed, ONE in-app notification per account per tick with the
  number actually delivered (committed rows only) and up to 5 job ids (+ the first job_id for the deep link).
  Best-effort: a failed notification never undoes a delivery and never stops the tick. Never per row.
### C1b BE API (<= 4 files: src/api/schemas.py, src/api/routes/jobs.py, schema/openapi.json, tests)
- ResultRow.delivered_late_at (datetime | None) from enrichment_data.late_delivery.delivered_at, on every list
  (a late row is an ordinary new lead now). ResultsPage.late_delivered_count: rows of this job with late_delivery,
  job+user scoped (own count statement, like not_qualified_count). Run counts/snapshot untouched.
### C2 FE (bridgeleads-web: types regen, NotificationsBell, NotificationsTab, Results row badge + summary line)
- Bell: "N held records were delivered" -> /results?job=<first job>; settings toggle "Held records delivered later".
- Results: muted badge "Delivered <date>, after the run" on the row; one line under the cards when
  late_delivered_count > 0: "N of these were delivered after the run, once their owner and mailing address were
  found." Copy: no em dashes. The run's own counts stay as frozen (they are what the run billed).
- Deploy order: BE first (additive fields), FE after the types regen.

### Phase C consult r1 (Codex GATE FAIL, 2 P1 / 5 P2): reconciliation
- P1 a lost notification: delivery stamps late_delivery.notified = false; a separate notify step (every tick, so it
  also picks up a crashed tick's rows) per account, system session, pg_advisory_xact_lock on the account, selects
  the pending rows FOR UPDATE, adds ONE notification row and flips them to notified = true IN THE SAME
  TRANSACTION. Exactly once: no lost notice, no duplicate. notification_emit stays the only module that writes
  notifications: it gains add_notification(db, ...) (same type/prefs rules, no commit); create_notification
  becomes a thin wrapper over it. Prefs off -> rows marked notified, no row. Rows delivered before this ships
  carry no notified key and are never announced (none exist in prod today: no held rows yet).
- P1 old bell mislabels an unknown type as "Payment failed": deploy order changes to
  C1 BE (enum + prefs key + API fields, NO emission) -> C2 FE (bell, settings, Results) -> C3 BE (emission).
- P2 grouping: the advisory lock per account serialises overlapping ticks; grouping = all pending rows of the
  account at that moment.
- P2 count: late_delivered_count joins the existing no_address/not_qualified aggregate (one statement, job_id +
  user_id, before view filters).
- P2 CSV/account export: NO new CSV column in this phase (the canonical CSV's columns are imported into
  customers' CRMs; adding one is a contract change for every account). In-app only; recorded as a follow-up
  for the owner. Account export inherits the CSV; notifications are not part of the export today (unchanged).
- P2 deletion: notifications.user_id is ON DELETE CASCADE and the purge (113) deletes them; late_delivery lives in
  results.enrichment_data and goes with the row. Nothing to add.
- P2 delivered_late_at from the row's stamp, parsed defensively (None when absent/malformed). Adopted.
- P3 settings copy says "email": updated in C2 to cover in-app.

## Phase D plan (2026-10-31): dialer + paid skip trace for late-delivered rows (owner: "complete these")
A late row is an ordinary lead now; a run that had skip tracing / a dialer on would have traced and pushed it.
Never spend outside the existing gates; never touch any OTHER row of the run.
### D1 skip trace (enrich.py, cv_late_delivery.py, tests)
- _enqueue_skip_trace_rows gains `only_result_ids: list[str] | None = None`: when set, the eligible-row selection
  (and its re-check under the advisory lock) is further restricted to those ids. Every existing gate is unchanged:
  SKIP_TRACE_ENABLED, token, config.skip_trace_enabled, plan not Starter, claim enforcement, per-account claim and
  Tracerfy capacity/billing, cache-hit copy. A run's other untraced rows (deliberately skipped, capped) are never
  queued by a late delivery.
- cv_late_delivery: delivery stamps late_delivery.contacts = "pending" ONLY when the row's config has
  skip_trace_enabled (decided under the row lock). A per-tick step, per job, in the account's RLS session (as
  degradation_repair does for a done job, no attempt token): calls the enqueue with only_result_ids = the job's
  pending late rows, then marks them contacts = "queued" (or "skipped:<gate>" for a gate outcome, so a gate never
  loops). Rows the enqueue copied from cache or queued leave skip_trace_status = not_attempted on their own.
### D2 dialer (scheduler_helpers/dialer.py, cv_late_delivery.py, tests)
- Job not pushed yet (dialer_pushed_at NULL): nothing to do, the normal sweep includes the row (actionable now).
- Job already pushed: insert the late row into dialer_deliveries via the existing idempotent outbox insert
  (_materialize_dialer_outbox, ON CONFLICT (job_id, result_id) DO NOTHING) once its skip trace has settled (the
  sweep's own rule: dialers want contacts), then the existing process_dialer_outbox drains it.
- Same dialer_ready / config / vendor checks the sweep applies.
### Questions for Codex
- Is only_result_ids enough to keep the enqueue's claim/quota/billing invariants (advisory lock, frontier,
  spend weight, account spent) correct for a done job? Does process_dialer_outbox drain rows of a job whose
  dialer_pushed_at is already set? Anything that would double-send to a dialer or double-charge a trace?

### Phase D consult r1 (Codex GATE FAIL, 3 P1 / 4 P2): reconciliation
- P1 capacity frontier: only_result_ids NARROWS the enqueue; it skips nothing. The rows still go through
  claim_skip_trace_rows (access, trial allowance, freeze/end, account limits, spend weight, frontier) and the
  dispatcher (batching, rate limit, re-check before submit). Nothing is treated as free or pre-reserved. The
  per-row outcome is read from the ROW after the call, never from the enqueue's return value:
  skip_trace_status completed/unmatched (cache copy) -> contacts "settled"; a pending_skip_trace_rows row exists
  -> "queued"; still not_attempted -> "not_queued:<gate>" (terminal; never retried in a loop).
- P2 job log: the enqueue gains quiet=True (no _publish_log lines) for this caller; a done job's log never grows.
- P2 entitlement changes: covered by the claim and the dispatcher as above; outcomes are per row.
- P1 unbounded dialer wait: a late row goes to the dialer when its contacts settle OR 24 h after late delivery,
  whichever comes first; then it is pushed only if dialer_ready_conditions hold for it (else dialer
  "not_ready", terminal). Never waits forever.
- P1 outbox drain skips the sweep's gates: the late path re-checks, for THAT result, in the account's RLS session:
  job done + owned, config dialer settings present, plan Business+ (the sweep's rule), dialer_ready_conditions,
  actionable. Only then it inserts that ONE result (job_id+result_id scoped, same user) and calls
  process_dialer_outbox.delay(job_id). Other pending rows the drain sends are the sweep's own, already checked.
  Only jobs with dialer_pushed_at set (the sweep already pushed); unpushed jobs pick the row up in the sweep.
- P2 ON CONFLICT on an existing failed/skipped delivery: never replayed by this path (dialer outcome
  "already_in_outbox"); replay stays the user's explicit action (existing endpoint).
- P2 tenant scope: the late path selects job+user scoped rows only; the insert helper is given rows whose
  user_id and job_id are checked against the job before insert.
- Split: D1 (skip trace) PR first, D2 (dialer) PR second.
### Phase D consult r2 (Codex GATE FAIL, 1 P1 / 1 P2): reconciliation
- P1 the late dialer gate = the FULL sweep predicate for that result: Business+ plan, owned config with a dialer
  destination, dialer_ready_conditions(include_unknown_dnc=True), is_duplicate = false, the 18-month tax cap,
  actionable_condition(). Built from the same helpers the sweep uses (no copy).
- P2 outcomes: D1 records only late_delivery.contacts = "requested" once the enqueue was called for the row
  (never called again for it); it claims nothing about the result. The dialer step (D2) reads the row's live
  state (skip_trace_status hit/miss/errored, open pending_skip_trace_rows) when it decides, with the 24 h bound.
- Codex r2 confirmed: only_result_ids as an optional filter on both the eligible query and the lock-time
  re-read leaves normal runs unchanged; claim + dispatcher keep every spend gate; the drain may run after
  dialer_pushed_at; replay stays the user's action.

## Phases C and D done (2026-10-31)
- [x] C1 BE #561 (79300bdf): ResultRow.delivered_late_at, ResultsPage.late_delivered_count (New list only),
  NotificationType.LATE_DELIVERY + prefs toggle.
- [x] C2 FE bridgeleads-web #298 (63d17055): Results "Delivered after the run" line, summary line, bell,
  settings toggle; Vercel production deploy succeeded.
- [x] C3 BE #566 (d692fc8a): notify_late_deliveries, one notice per account batch, exactly once.
- [x] D1 BE #568 (1568a56d): paid skip trace through the run's own enqueue (only_result_ids, quiet);
  Codex r1 GATE FAIL, r2 GATE PASS; 382 targeted tests passed locally, CI green.
- [x] D2 BE #569 (06757393): dialer push exactly once (outbox idempotent; generic webhook via_sweep /
  skipped_overlap / send_failed; 24 h trace wait); Codex r1 GATE FAIL, r2 GATE PASS; CI green.
- [x] Deployed to api/worker/beat; prod ticks every 20 min return errors 0 (candidates 0 so far).

## Review
- Shipped: Phases A-D in seven BE PRs (#555 #559 #561 #566 #568 #569, plus #564 for Accela postback tests)
  and FE #298. A held code violation is now named by the owner sweeps, delivered and billed once when
  complete, announced once per account, skip traced if its run would have been, and pushed to the run's
  dialer at most once.
- Decisions kept: no quota credit for the 929 pre-gate rows (owner decision C); a lost dialer push is
  preferred to a duplicate CRM import (generic webhook overlap window skipped); a late row never makes
  the run's other untraced leads get traced.
- Notes: the `test_audit4_held_lookup_log` source pin had to follow D1's quiet `_log` wrapper; the local
  test DB once lost its tables before a run (cause not found, rebuilt with `alembic upgrade head`).
- Nothing pending. Watch the worker tick stats (`delivered`, `contacts_requested`, `dialer_pushed`) for
  the first real late delivery.
