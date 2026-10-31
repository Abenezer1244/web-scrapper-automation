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
