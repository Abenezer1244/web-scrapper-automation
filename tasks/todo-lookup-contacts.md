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

## Decisions (ALL ANSWERED by the owner 2026-09-19; the recommendation was taken in each case)
- **D1 Address-only (advanced) answers. ANSWERED: reuse per address, documented.** The
  explicit matrix below is the rule. (Rejected: never reuse advanced answers, which costs
  more and buys no real accuracy.)

  | Trace | Reuses | Isolated by |
  |---|---|---|
  | normal (name + address) | only answers bought for the SAME subject | account + address + first/last name |
  | advanced (address only) | any advanced answer for that address in the account | account + address only; owner isolation does NOT apply, because no name was ever sent |

  A normal trace never reuses an advanced answer, and the reverse, because `trace_type` is part of
  the key. The account boundary holds in both cases.
- **D2 Advanced costs 2 Tracerfy credits but bills the customer 1 lookup. ANSWERED: show the
  customer-billed number, log the gap.** The quote shows what the customer is billed (rows);
  the action row also carries `tracerfy_credits` and the credits-vs-billable-rows gap, tracked
  as its own follow-up item rather than silently folded into this work. (Rejected: showing
  credits in the quote, which would disagree with the invoice; and repricing advanced to 2
  billed lookups, a live pricing change needing its own decision and comms.)
- **D3 Daily cap. ANSWERED: surface it end to end.** As specified below. (Rejected: leaving it
  silent. Making the cap per-account was rejected HERE and has since been
  REVERSED -- see the 2026-09-22 decision below.) When the dispatcher pauses it writes
  `skip_trace:daily_cap_paused` to Redis with the resume time and a TTL of two ticks (600s),
  **refreshed on every paused tick** so it survives a long pause, and **deleted explicitly on the
  first tick that resumes dispatch** rather than waiting for the TTL. Tested both ways: it stays
  while paused, and no stale "paused" state is ever shown after dispatch resumes. The API (which already has a Redis client for
  rate limiting) reads it in the quote and in the job results summary; if Redis is unreachable the
  field is simply absent and nothing else breaks. The dialog warns before confirming, and after
  confirming the results page says lookups are paused and when they resume, instead of an endless
  "looking".
  **(2026-09-27) The key name, TTL and read pattern here are SUPERSEDED by the v1 contract in
  "Phase 1b-1b-iii" (consult r1 E2, E5): hash `bridgeleads:skip_trace:pause:v1`, fixed-field
  `HMGET`, heartbeat, fenced writes.**

  **D3-b REVERSED 2026-09-22 (owner): the per-account daily cap is NO LONGER deferred and
  moves into Phase 1b-1.** The reasoning that deferred it assumed the cap would rarely bind.
  The production pre-check killed that assumption: 100,548 addressable leads against a GLOBAL
  `SKIP_TRACE_DAILY_ROW_CAP` of 1000/day means ONE customer's 2000-lead action consumes two
  full days of dispatch capacity for EVERY tenant. Waiting behind the cap is the normal case
  for a large action, not an edge case. It lands in 1b-1 because 1b-1 touches no money, and
  because once 1c puts the button in front of customers the first large action starves every
  other tenant -- a support incident rather than a bug that can be fixed quietly. Note 15-11
  when building it: the existing global cap is SOFT (checked outside the advisory lock, so
  concurrent ticks can both pass it), so the per-account cap must not be described to the
  user as a hard limit unless it is made one.

## Phase 1a - reuse correctness

> **Codex round 14 (2026-09-19, consult before code) returned TWO P1 blockers against the
> version of this section that rounds 1-13 passed.** Round 13 gated only on the key design; it
> never examined how a subject-keyed batch lands back through ingest, and it took the "no
> migration" constraint as given. Both P1s are recorded below as 14-A and 14-B with the code
> that proves them. The prompt and the full output are `<scratchpad>/codex_1a_consult.txt` and
> `codex_1a_out.txt`. **Do not re-simplify past them: like rounds 1-13, each one is here
> because it double-charges a customer or leaks one owner's contacts onto another lead.**

**14-A (P1) - a subject key alone double-charges and answers nobody.** Attribution back from
the vendor is ADDRESS-ONLY and always has been: `tracerfy_ingest.py:585-608` groups pending
rows by `(address, city, state)`, and `_attribution_is_safe` (`:279-315`) REFUSES the whole
group when more than one CSV row comes back for that key. Today that never fires, because
`_hold_answers_in_flight` dedups the outgoing batch on the address-only `_answer_key`, so two
owners at one address never go out together. Give them distinct v2 keys and both go out in one
batch, the vendor returns two CSV rows for one address, attribution refuses BOTH, and we are
charged twice and attribute nothing while both leads settle terminally unanswered. **The fix is
to split the two jobs the one key is doing today**: the v2 subject key governs cache and reuse,
and a SEPARATE submission-serialization key keeps one address per batch. That serialization key
is `(address, city, state, trace_type)` and is deliberately **GLOBAL, not per-tenant**, because
ingest attribution carries no tenant identifier at all: a cross-tenant pair at one address is
refused exactly the same way. (Codex notes that cross-tenant collision is therefore ALREADY
possible in production today, since the current hold key is tenant-scoped while the attribution
key is not. Pre-existing, not introduced here; logged under Deferred.) The second subject stays
queued until the first settles, which is the existing and intended behavior for a held twin.
- [x] Separate `_submission_collision_key(row)` = `(address, city, state, trace_type)`, global,
      used ONLY by `_hold_answers_in_flight` to serialize submission. The cache read, the
      known-answer sweep and the ingest write all use the v2 subject key. The two keys are
      named so they cannot be confused, and a test asserts the hold dedups two different owners
      at one address into one batch while the cache still treats them as two subjects.

**14-B (P1) - the `dedup_hash` passes cannot be made owner-safe by recomputation.** The plan
said to compare the canonical subject with the same helper instead of raw SQL equality. Codex
showed that is unsound as an authorization test, and the reason is decisive: `party_name` is
MUTATED after the fact by owner recovery and enrichment. When it is, recomputing the source
row's subject yields the CURRENT owner, which is the same as the target's subject, while the
phone and email stored on that row still belong to the PREVIOUS owner. The comparison then
passes and copies exactly the leak it was added to stop. Trace type is not persisted on
`results` at all, `None` and `''` are not recoverable, and pre-cutover PII was produced under a
legacy address-only key. **Owner decision 2026-09-19: add the durable evidence instead of
guessing at it.**
- [x] **Migration 098** (this renumbers Phase 1b's migration to **100**): add
      `results.skip_trace_subject_hash TEXT NULL`. Additive and nullable, no backfill, no
      rewrite; historical rows stay NULL and therefore FAIL CLOSED, which is the same outcome
      as not copying and is the point. Written wherever a lookup's subject becomes known: the
      enqueue (from the payload actually built), the ingest settle, and the known-answer sweep.
- [x] Both `dedup_hash` passes copy skip-trace PII **only** on an exact
      `ro.skip_trace_subject_hash = <target's v2 subject>` match. Address and enrichment fields
      keep their existing COALESCE fill-missing behavior and are not gated by this. The
      `later_sql` pass becomes newest per `(dedup_hash, subject_hash)` rather than newest per
      `dedup_hash`; it stays one bounded query keyed off the target hash set, never an N+1.
- [x] `_reuse_enrichment_for_duplicates` is NOT gated by `SKIP_TRACE_ENABLED` (verified:
      called unconditionally at `enrich.py:768`, while the enqueue gate is at `:2151`). It
      therefore keeps copying PII straight through the cutover window. Under the subject-hash
      rule that is safe, because a pre-cutover row's hash is NULL and fails closed. Do not
      "fix" this by adding a kill-switch gate: the address and enrichment copy is wanted.

**Round 14, the rest (P2, all verified in code before being accepted):**
- [x] Ingest dedups the cache write by `_seen_users` (`tracerfy_ingest.py:679-689`). Under v2
      that drops writes: two rows for ONE tenant with distinct subjects would write only the
      first. It becomes dedup by the COMPUTED CACHE KEY, and each row's key comes from its OWN
      address fields, not `matches[0]`, since v2 no longer collapses punctuation or field
      boundaries.
- [x] `_attribution_is_safe` compares names but never `trace_type`, assuming the queue is
      homogeneous. Add a fail-closed trace-type invariant to the safe-group check.
- [x] **Truncation divergence.** The pending insert truncates address/mail to 512 and city,
      state and BOTH NAMES to 128 (`enrich.py:2352-2378`), while the enqueue cache read hashes
      the UNtruncated payload. v1 already had this for city; v2 adds both names, and the code's
      own comment at `:2354-2358` documents this exact class of bug ("a 128-truncated write key
      would never match -> re-paid traces"). The key helper therefore applies the SAME
      truncation the insert will apply, so read and write hash identical bytes by construction.
      A test asserts a >128-character last name still hits its own cache entry.

- [x] `lookup_subject_key(version=2, user_id, address, city, state, trace_type, first, last)`:
      NFKC, collapse Unicode whitespace, trim, case-fold; punctuation preserved; no invented
      equivalence (middle initials, hyphenation, diacritics). Versioned namespace.
      Serialization is unambiguous, not delimiter-joined: `sha256(json.dumps([...], ensure_ascii=
      False, separators=(",",":")))` over a fixed field order, where a missing name is JSON `null`
      and an empty string is a distinct value. An advanced subject is `null` first AND last plus
      `trace_type="advanced"`, so it can never collide with a normal trace whose name is missing.
      "Address" in this key means the street line, city and state as three separate normalized
      fields (never one concatenated string), which is what the current helper already passes.
- [x] Switch all FIVE call paths together, enumerated so none can be left on the legacy key:
      (1) the enqueue cache read, (2) the dispatcher's known-answer sweep, (3) the dispatcher's
      in-flight hold, (4) the ingest cache write, (5) the `dedup_hash` reuse passes plus the
      charged-unanswered check. The test matrix names all five explicitly. The subject always comes from the pending row (dispatcher,
      ingest) or the payload actually built (enqueue), never recomputed from `party_name`.
- [x] Stop reading legacy address-only keys (and the legacy locality fallback). Correctness comes
      from the new code reading only v2 keys; the old rows are then inert. The cutover is enforced
      with the EXISTING global kill switch rather than a hopeful deploy note: set
      `SKIP_TRACE_ENABLED=false`, deploy API and worker, confirm both are on the new build, then turn
      it back on. No lookup can run on mixed versions, so an old worker cannot serve a legacy hit or
      write a legacy key. **Deleting the legacy rows is a separate ops step AFTER that**, purely as
      PII hygiene (never as the correctness mechanism), with the deleted row count recorded. Cost:
      repeat addresses pay again for up to 90 days (cache retention is 90 days anyway, so it
      self-heals).
- [x] The charged-unanswered check (`skip_trace_dispatcher.py:647-658`) uses the same v2 helper
      as the sweep, so two NULL names do not silently stop matching and two different owners
      never match. (The `dedup_hash` half of this bullet is SUPERSEDED by 14-B above: it is
      resolved by the durable `skip_trace_subject_hash`, not by comparing recomputed subjects,
      which Codex showed copies the leak it was meant to stop.)
- [x] Tests (isolated DB), matching the D1 matrix exactly: for NORMAL traces, a different owner at
      the same address does NOT reuse, at each of the 5 sites; the same owner does; for ADVANCED
      traces, the same address DOES reuse regardless of owner, but never across accounts; normal
      never reuses advanced and the reverse; tenant isolation holds; normalization cases (case,
      Unicode/NFKC, whitespace, punctuation, missing first or last, unit numbers, state case).
- [x] Tests added by round 14, each pinned to the P1/P2 it defends:
      **14-A** two different owners at one address go out in ONE batch and BOTH come back
      refused (the regression itself), then the same case with the serialization key in place:
      one submitted, one held, nothing double-charged, nothing stranded; the same across two
      tenants, since the attribution key has no tenant in it.
      **14-B** a source row whose `party_name` was mutated after its lookup does NOT donate its
      PII (recomputation would have matched and leaked); a NULL `skip_trace_subject_hash`
      never donates; an exact hash match still donates, so free reuse is preserved;
      `later_sql` picks the newest per `(dedup_hash, subject_hash)`, not per `dedup_hash`.
      **P2s** two subjects for ONE tenant in one ingest group write TWO cache rows (the
      `_seen_users` bug); a mixed-`trace_type` safe group is refused; a >128-character last
      name hits its own cache entry (truncation divergence).

- [x] Required at diff time (Codex round 13, condition of its PASS): a test that seeds a
      legacy-only cache hit and proves NONE of the five paths reuses it (including the locality
      fallback); a test that an advanced trace hashes null names regardless of the payload or
      `party_name`; a test that pending rows created BEFORE the cutover are processed with the v2 key
      by both the dispatcher and ingest; a test that the `dedup_hash` passes and the
      charged-unanswered check use the same canonical helper. Plus a counter for v2 key usage and a
      log line on any unexpected legacy-key access, so the cutover can be verified from production
      rather than assumed.

- [x] **Ops scripts (round 14, Q5): the "five call paths" was not the whole list.** Three
      scripts call `address_cache_key` directly and would read or write LEGACY keys into a v2
      world. `scripts/backfill_skip_trace_jobs.py:181` and `scripts/sprint4_enqueue_existing.py:106`
      can both COPY THE WRONG OWNER'S PII from an old address-only row (P1 if either is ever run
      after cutover); `scripts/verify_tracerfy_provenance.py:97` only reports, so it merely goes
      misleading (P3). Actions: switch the backfill to the v2 helper and the exact payload
      subject; HARD-DISABLE `sprint4_enqueue_existing.py` (it also builds an incomplete result
      stub and can substitute the mailing address for the property address, `:88-111`) until it
      is rewritten against current enqueue logic; rewrite the verifier on `build_pending_row_payload`
      + v2. If legacy verification is ever wanted again it lives as an explicitly named forensic
      tool and is never part of normal reuse.

- [x] **Cutover is a DRAIN, not a flag (round 14, Q6).** The kill switch gates the enqueue
      (`enrich.py:2151`) and the dispatcher (`skip_trace_dispatcher.py:39`). It does NOT gate
      ingest (`tracerfy_ingest.py:451`), the webhook that feeds it (`routes/webhooks.py:132`),
      or `_reuse_enrichment_for_duplicates` (`enrich.py:768`). **Ingest must keep running with
      the switch off**: a batch the vendor already accepted is paid work, and blocking it would
      strand both the contacts and the billing state. The requirement is that every ingest
      AFTER cutover runs v2 code, including for batches submitted BEFORE it. Order:
      1. switch false everywhere;
      2. restart API, Celery workers and Beat so no old process survives (a rolling deploy can
         otherwise leave an old worker submitting or writing legacy keys: P1);
      3. confirm no dispatcher is submitting and no old build remains;
      4. let the NEW ingest drain every outstanding submitted/webhook batch;
      5. reconcile `submitting` rows of unknown outcome, and do not casually cancel them;
      6. confirm no old-version queue claims or tasks remain;
      7. deploy and verify the v2 build with the switch still false;
      8. re-enable.
      Queued rows may simply stay queued. Paid or possibly-paid rows are drained or reconciled,
      never merely flagged. Deleting legacy cache rows stays a SEPARATE later step, PII hygiene
      only, never the correctness mechanism, with the deleted count recorded.

## Phase 1b - the action, backend

> **Codex round 15 (2026-09-20, consult BEFORE code) returned NO-GO with nine P1 blockers
> against the 1b section as written below.** Same lesson as round 14: the earlier rounds
> specified the action and never asked what the EXISTING writers do when the action's ledger
> is bolted onto them, nor what the API can actually SEE when it has to answer "what happened
> to my lookup". Prompt and full output: `<scratchpad>/codex_1b_consult.txt` /
> `codex_1b_out.txt`. Every finding below was verified in code before being accepted; the four
> marked VERIFIED were re-proved independently rather than taken on Codex's word.
>
> **Do not implement the bullets below without these corrections applied.**

**15-1 (P1, VERIFIED) - the migration's own ordering breaks the existing scrape enqueue.**
Step 2.5 creates the partial unique index; step 7 (share the `ON CONFLICT` claim path) is
listed later as if it were a tidy-up. `_enqueue_skip_trace_rows` (`enrich.py:2460-2495`) does
`db.add()` in a loop and flushes at ONE `db.commit()` whose handler is
`except Exception: db.rollback(); db.commit()`. Once the index exists, a single conflicting
row raises `IntegrityError` at that commit, the handler rolls back THE WHOLE JOB'S enqueue
(every pending row AND every `rec.skip_trace_status='queued'` update) and commits an empty
transaction. Silent, total, unreported loss of a job's lookups.
- [ ] **Step 7 is a PREREQUISITE of step 2.5, not a follow-up.** Refactor the scrape path onto
      the shared `INSERT ... ON CONFLICT DO NOTHING ... RETURNING` claim FIRST, update
      `results` only for returned ids, and never swallow a uniqueness error except the
      expected conflict. Only then create the index.

**15-2 (P1, self-contradiction) - dispatch-failure terminalization strands the quoted set.**
The plan says a failed publish marks the action `failed` (CAS on `status='dispatching'`) AND
that a retry finding it `dispatching` re-dispatches. Both cannot hold: after the first failure
it is `failed`, so the retry finds no path back and the durable quoted set sits under a
terminal status with nothing to re-drive it. The house pattern (`enqueue_scrape_job`,
`routes/jobs.py:266-279`) deliberately does the opposite on a broker publish failure.
- [ ] Contract: validation failure BEFORE the action is durable -> 4xx/503, no action exists.
      Action committed but publish uncertain -> leave `dispatching`, return **202 with the
      action id** ("confirmed, waiting for dispatch"). A reconciler retries dispatch keyed on
      `action_id`. ONLY a deadline expiry moves `dispatching -> expired` and abandons the
      quoted rows. Once the worker reaches `running` the API must never terminalize.

**15-3 (P1, VERIFIED) - `miss` and `errored` are not safely re-quotable, and `errored` is
ambiguous in a way the API cannot resolve.** `tracerfy_ingest.py:749,763`: a `miss` settles the
pending row `completed`, so it WAS billed - it is an answer, not a gap. Worse,
`tracerfy_ingest.py:803-811`: when Tracerfy accepted and charged for a row we could not match
(pending `unmatched`), `results.skip_trace_status` is set to **`errored`** - the SAME value a
never-submitted pre-submit rejection carries. The distinguishing fact lives on the pending row,
which `bridgeleads_app` has no grant to read. The scrape path already guards this with the
charged-unanswered check (`enrich.py:2303`), but that is worker-only. Quoting `errored` would
re-buy lookups the customer has already paid for.
- [ ] Quotable is EXACTLY `skip_trace_status = 'not_attempted'`, matching the enqueue. `miss`
      and `errored` are not quotable.
- [ ] To make retry decidable later without a second charge, add a durable result-level
      outcome (`results.last_trace_outcome`: `provider_rejected` | `provider_accepted_unmatched`
      | ...) written atomically with the existing transitions. **This widens migration 100.**
      Retry is permitted only for a PROVEN pre-submit rejection, never because `results` says
      `errored`.

**15-4 (P1) - the action-result vocabulary has no terminal values, so the status page cannot
work.** `newly_queued` cannot express hit, miss, unmatched-but-billable, errored-before-
submission, or released. The bite is structural: the API cannot read `pending_skip_trace_rows`,
so if the disposition does not itself carry the final outcome, `GET /contact-lookups/{id}` can
never show what happened. An action could reach Tracerfy and have no legal terminal verdict.
- [ ] Extend the CHECK vocabulary with terminal values (`answered_hit`, `answered_miss`,
      `unmatched_billable`, `errored_unsubmitted`, `reused`, `released`, `abandoned`), write
      down the transition matrix, and state which dispositions are BILLABLE. Settlement derives
      from those states.

**15-5 (P1, VERIFIED) - the existing writers commit independently and cannot join the promised
cross-table transaction.** `_persist_submission`, `_cancel_undeliverable_queued` (which commits
internally, `skip_trace_dispatcher.py:545`), `_release_claim`, `tracerfy_ingest` and the
stale-claim reconciliation all commit on their own today. Until that changes, an action stays
`newly_queued` forever while billing proceeds elsewhere.
- [ ] Every path that changes a pending row must update the action-result and its event in the
      SAME transaction. Remove the internal commit from `_cancel_undeliverable_queued` and give
      the caller the transaction.

**15-6 (P1) - the claim does not re-check JOB deliverability.** It re-checks result eligibility
only. A job can become failed, cancelled or undeliverable between quote and claim, and we would
buy contacts for leads that will never be delivered.
- [ ] Re-use the dispatcher's authoritative delivery predicate (`_job_delivered_sql` /
      `_partition_still_deliverable`) inside the claim transaction.

**15-7 (P1, VERIFIED) - the duplicate cleanup uses the wrong submission evidence.**
`submitted_at` is stamped at `queued -> submitting` BEFORE Tracerfy is contacted
(`skip_trace_dispatcher.py:280-289`), so it proves a local attempt, not vendor acceptance.
Cancelling on age, or treating `submitted_at` as evidence, can cause a SECOND paid submission.
- [ ] Evidence grades: `tracerfy_queue_id` = strong (vendor accepted); `status='submitting'`
      with no queue id = UNKNOWN outcome; `submitted_at` alone = local attempt only. If a
      duplicate group contains any unknown-outcome row, quarantine the WHOLE group and
      reconcile against Tracerfy. Never pick a survivor by age.

**15-8 (P1) - grants and policies are absent and will 500 on deploy day.** `RLS_ENFORCE` is
true in production and the API is `NOBYPASSRLS`. "RLS like its siblings" is not a grant.
- [ ] The migration carries the role-guarded block (the 095 pattern): `REVOKE ALL` from
      PUBLIC/anon/authenticated, exact grants to `bridgeleads_app` and `bridgeleads_system`,
      tenant-scoped `USING` + `WITH CHECK` policies for the API role, explicit system-role
      policies, role-existence guards. `scripts/provision_rls_roles.sql` (whose verify DO block
      RAISEs on stale grants) and `scripts/apply_rls_cutover_policies.sql` are updated in the
      same change.
- [ ] `contact_lookup_action_events`: `SELECT, INSERT` for BOTH roles, `UPDATE`/`DELETE` for
      neither. A blanket `FOR ALL` system policy is the wrong shape here.
- [ ] "The API may insert initial dispositions but never transition them" is NOT expressible in
      grants. Enforce allowed INITIAL dispositions with a trigger (or route creation through a
      controlled function).

**15-9 (P1) - `results` needs `UNIQUE (id, user_id)` and the plan does not say how.** A plain
`ALTER TABLE ... ADD UNIQUE` builds under ACCESS EXCLUSIVE and blocks all reads and writes on
the hottest table in the product (171,657 rows). Codex judged the composite FK worth keeping:
RLS and worker predicates are defense in depth, but only the FK makes cross-tenant attachment
impossible at the database.
- [ ] `CREATE UNIQUE INDEX CONCURRENTLY` (autocommit, outside Alembic's transaction), then
      `ALTER TABLE ... ADD CONSTRAINT ... UNIQUE USING INDEX`, then the child FKs, `NOT VALID`
      followed by a separate validation step if deploy timing requires it.

**Round 15, the rest (P2, accepted):**
- [ ] **15-10 daily-cap resume time.** The cap is a ROLLING 24h count and nothing computes a
      resume time today; the API cannot compute one (no grant on the queue). Formula:
      the `(spent_today - cap + 1)`-th oldest `submitted_at` in the window, + 24h, ordered
      deterministically by `(submitted_at, tracerfy_queue_id, id)`, with a small margin for the
      `>=` boundary. The dispatcher must write it. TTL is **derived from the effective beat
      interval** (`max(2 * interval + grace, resume_at - now + grace)`), never hardcoded to
      600s, because beat intervals reset on every deploy here. The Redis value is ADVISORY: if
      beat stops, the UI must not present stale Redis state as authoritative.
- [ ] **15-11 (VERIFIED) the global cap is SOFT, not a spend boundary.** The cap check
      (`skip_trace_dispatcher.py:71-112`) runs in its own session BEFORE the claim's
      `pg_try_advisory_xact_lock` (`:156/:657`), and one tick may submit several batches, so
      concurrent ticks can both pass it. Either serialize the decision under the advisory lock
      and limit the tick to the remaining rows, or document it as a soft alert cap. Do not
      describe it to the user as a hard limit until it is one.
- [ ] **15-12 lock order.** `ORDER BY id` in one UPDATE is not a global lock order, and
      `INSERT ... ON CONFLICT` can take unique-index locks in a different order. Establish ONE
      order - result rows ascending `(id, user_id)`, then pending rows, then action-result rows
      - and make the scrape enqueue, the action claim, cancellation, release, the dispatcher
      transitions and ingest all follow it. Audit every bulk SQL writer; ORM iteration order is
      not a guarantee. Keep the 2000-row single transaction; do NOT chunk until lock ordering
      is disciplined and transaction time is measured (chunking would also destroy the "one
      commit, no crash window" invariant).
- [ ] **15-13 the DB unique is the concurrency authority, not the Redis lease.** If Redis is
      down or the key expires during a slow claim, two confirms both reach dispatch. Handle the
      race explicitly: attempt the action INSERT; on `IntegrityError` roll back, re-fetch by
      (`quote_id`, `user_id`, `job_id`) and return THAT action - never 500. After the action and
      action-result rows commit, Redis is no longer required; reconstruct dispatch from the
      durable rows. Only a quote evicted BEFORE action creation returns `quote_expired`. Add a
      DB dispatch CAS separate from the worker fencing lease; keep Redis as an optimization.
- [ ] **15-14 pin the planner's policy inputs.** `code_violation_skip_trace_allowed` reads
      `settings.PIERCE_CV_OWNER_SKIP_TRACE_ENABLED` at call time, and API and worker are
      separate services with separate env (this repo has a live incident where a code default
      was not the production value). Pin the flag values into the quote/action snapshot, not
      just `planner_version`. The worker may apply a STRICTER current policy but must never add
      an id outside the durable quoted set.
- [ ] **15-15 clear the worker lease at claim commit.** A 10-minute lease is right while the
      worker executes; it is wrong afterwards. A `claimed` action may wait indefinitely behind
      the daily cap or the kill switch BY DESIGN, so it must never be expired for lease elapse.
      Only `running` needs fencing.
- [ ] **15-16 the drift check needs an allowed-state matrix.** `pending='submitting'` with
      `results='queued'` is an EXPECTED window in the current dispatcher, so a generic
      "they disagree" check alerts during normal operation. Allowed pairs: `queued->queued`,
      `submitting->queued`, `submitted->submitted`, `completed->hit|miss`, `errored->errored`,
      `cancelled->not_attempted`. Only unexpected pairs count as drift.
- [ ] **15-17 one canonical price.** `0.08` is duplicated in `routes/billing.py:327` and in plan
      copy, with no constant. Create one canonical integer-cent source with a version mapping to
      the real Stripe rate. Billing NEVER derives from the action snapshot; the snapshot exists
      only to make quote-vs-invoice drift detectable.

**Cut as over-engineering (Codex, accepted):** the Redis claim lease becomes optional (the DB
action row + unique `quote_id` + a DB dispatch CAS are authoritative); no per-lead event for a
static quote-time exclusion (the action-result row already records it - keep events for
submission, billability, release, abandonment and settlement); the action's mutable counters are
a CACHE only, with `contact_lookup_action_results` the source of truth and fully recomputable.

**Codex gate: do not migrate or deploy until 15-1, 15-2, 15-3, 15-4, 15-5, 15-6, 15-7 and 15-8
are corrected.**

### Production pre-check RESULT (read-only, counts only, 2026-09-20)

Plan step 1 was run against production before any code. `railway run --service worker`,
inside `SET TRANSACTION READ ONLY`, no PII. Script: `<scratchpad>/prod_1b_precheck.py`.

```
pending_skip_trace_rows      941 total, 0 ACTIVE
results                  171,657   not_attempted 170,350 | hit 1,024 | miss 272 | errored 11
A) duplicate ACTIVE pending rows per result_id : 0 groups, 0 rows   -> index is creatable
B) pending/results cross-tab outside the 15-16 allowed matrix : 0   -> quote classification sound
   B2 results in-progress with no active pending row           : 0
sizing  not_attempted WITH a property address       : 100,548
        ... of those already-delivered (duplicate)  :  61,442
100 prereqs  results(id,user_id) index: absent | new tables: absent | action_id: absent
```

**What this changes in the plan:**
- **The duplicate cleanup / quarantine / JSON-backup / restore-rehearsal machinery in step 1 is
  NOT needed.** There is nothing to repair. Do not build it. What survives is a **migration
  guard** that ABORTS 100 with instructions if a duplicate is present at migration time (the
  check above is point-in-time and the migration runs later), plus 15-7's evidence grading
  written down for whoever has to act if the guard ever fires. Build the repair script then,
  against real rows, not now against imagined ones.
- **The status-drift repair is likewise not needed**, and the daily integrity check keeps the
  15-16 allowed-state matrix so it does not alert on the normal `submitting -> queued` window.
- **Scale note for 1c copy and for ops:** the addressable set is ~100.5k leads, 61.4k of them
  already-delivered (exactly the case that drove this feature). At the 2000-id quote cap that is
  50+ actions to cover; at `SKIP_TRACE_DAILY_ROW_CAP=1000` GLOBAL across all tenants, ONE
  confirmed 2000-lead action consumes two full days of dispatch capacity for every tenant.
  Waiting behind the cap is therefore the NORMAL case for a large action, not an edge case,
  which is why 15-15 (a `claimed` action may wait indefinitely) and 15-10 (an honest resume
  time) are load-bearing. **A per-account cap stops being safely deferrable the moment this
  ships to more than one active tenant** - flagged to the owner 2026-09-22, who moved it into
  Phase 1b-1 (D3-b above). Not silently absorbed.

### Agreed split (owner, 2026-09-20): 1b lands as THREE PRs, not one

CLAUDE.md caps a phase at 5 files, and the reconciled 1b spans the live paid path.

- **1b-0 hardening (no new feature, ships alone):** the shared
  `INSERT ... ON CONFLICT DO NOTHING ... RETURNING` claim path with `_enqueue_skip_trace_rows`
  refactored onto it (15-1), `_cancel_undeliverable_queued` de-committed (15-5), the partial
  unique index + its abort guard, lock order established (15-12). Migration **100**.
  `results.last_trace_outcome` (15-3) is deliberately NOT here: its only consumer is the quote,
  so it lands at the head of 1b-1 as migration 100, keeping 1b-0 inside the 5-file rule and on
  one subject - making the existing path safe for a unique index.
- **1b-1 read path (touches no money):** the **per-account daily cap** (D3-b, moved here by
  owner decision 2026-09-22), `results.last_trace_outcome` (15-3),
  `contact_lookup_actions` /
  `contact_lookup_action_results` / `contact_lookup_action_events` with grants + policies +
  both RLS scripts (15-8), `UNIQUE (id,user_id)` built concurrently (15-9), the disposition
  vocabulary and transition matrix (15-4), `plan_contact_lookup`, and the quote endpoint.
- **1b-2 write path:** confirm (15-2, 15-13), the worker claim (15-6, 15-14), ledger
  integration into the five existing writers (15-5), the reconciler and settlement (15-15).

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
      exist, the resolution path is explicit and owner-approved before migration 100: a script keeps the row with the
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
- [ ] Migration 100 (renumbered from 098, which Phase 1a's subject-hash migration now takes),
      run inside the SAME quiesced window as the 1a cutover (kill switch off, so the
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

## Phase 1b-1 — REVISED by Codex round 16 (2026-09-22, consult BEFORE code)

> **Round 16 returned REVISE with seven P1s against the 1b-1 scope and ORDER as written
> above.** Same lesson as rounds 14 and 15: the earlier split named the right pieces and never
> asked what the schema actually supports or what the cap does to the component that spends.
> Prompt and output: `<scratchpad>/codex_1b1_consult.txt` / `codex_1b1_consult_out.txt`.
> Every finding below was re-verified in code before being accepted, and **one was rejected**.
> **1b-0 is MERGED AND LIVE** (`0074196`, migration 100 applied and verified by the objects).

**16-1 (P1, VERIFIED) — the tenant-carrying FK cannot be built as planned: `jobs` has no
composite key.** The plan requires composite FKs so a child row can never point at another
account's parent. Only `scraper_batches` has one today —
`UniqueConstraint("id", "user_id", name="uq_scraper_batches_id_user")` (`src/db/models.py:482`).
**`jobs` (`:659`, table_args `:790`) and `results` (`:811`, table_args `:962`) have NONE.**
15-9 named `results` only, so an action FK on `(job_id, user_id)` would simply fail to create.
- [ ] Composite `(id, user_id)` uniqueness for **`jobs`**, **`results`** AND
      **`contact_lookup_actions`**, each built before the child table that references it.
      `scraper_batches` is the precedent to copy.

**16-2 (P1, VERIFIED) — `pending_skip_trace_rows.action_id` was dropped from the migration
list, and settlement cannot work without it.** The plan requires it (step 3 of migration 100's
original list) and 1b-0 removed it as dead code (`fa20d60`). It is not dead in 1b-2: a Tracerfy
batch spans tenants and actions, `SkipTraceQueue` stores only the FIRST row's tenant/job
metadata (`skip_trace_dispatcher.py:1149-1156`), and ingest reconstructs attribution from the
pending rows. Without `action_id` on each row, a hit / miss / unmatched / reuse / release
cannot be tied back to the action that bought it.
- [ ] Nullable `pending_skip_trace_rows.action_id` + index, written by the action claim and
      left NULL by the scrape path, added in the SCHEMA step — not left to 1b-2.

**16-3 (P1, VERIFIED) — `last_trace_outcome` must ship as a column only; its writers are live
paid code.** 15-3 wants it written atomically with the existing transitions. Those transitions
are **eight** writers, not the "five" the plan says: the known-answer sweep and
charged-unanswered settlement (`skip_trace_dispatcher.py:697-803`), pre-submit failures
(`:947-983`), cancellation/withdrawal (`:1077-1107`), provider-acceptance bookkeeping
(`:1113-1196`), claim release (`:1251-1298`), stale-claim reconciliation (`:1494-1663`), ingest
hit/miss/unmatched settlement (`tracerfy_ingest.py:735-845`), and the scrape's
charged-unanswered handling (`enrich.py:2345-2387`).
- [ ] The column lands nullable with a CHECK and **no writer, no default, no NOT NULL and no
      trigger**. It means UNKNOWN until 1b-2 integrates the writers.
- [ ] **NULL must never be read as `provider_rejected`.** Retry is permitted only on proven
      pre-submit rejection. Note `tracerfy_ingest.py:789-811` writes
      `results.skip_trace_status='errored'` for provider-accepted-but-unmatched work, which is
      BILLABLE and must never be retried like a rejection — the exact ambiguity 15-3 exists for.

**16-4 (P1, VERIFIED) — a per-account cap placed beside the global one reproduces the soft-cap
bug it is supposed to improve on.** The global cap counts in its OWN session
(`skip_trace_dispatcher.py:72-92`, `with system_sync_session() as _db`) BEFORE the claim's
`pg_try_advisory_xact_lock` (`:219`), and one tick may submit several batches. A per-account
query in the same place inherits all of it: two ticks pass the same account's check, one batch
exceeds the account's remaining budget, and the two caps can disagree.
- [ ] **Reserve capacity inside the SAME transaction that moves rows `queued -> submitting`**,
      not in a pre-check. The effective allowance is `min(global_remaining, account_remaining)`;
      the transaction selects no more than that, commits the reservation, and only THEN calls
      Tracerfy. **No database lock is held across the provider call.**
- [ ] Count `submitting` rows of unknown provider outcome conservatively (as spent) until
      reconciled — the 15-7 evidence grading, applied to the cap.

**16-5 (P1, VERIFIED) — FIFO + an account filter starves tenants.** Selection is
`ORDER BY PendingSkipTraceRow.enqueued_at LIMIT 5000` (`skip_trace_dispatcher.py:273-279`).
If the oldest tenant sits at its cap, re-selecting that tenant's rows ahead of everyone else
can keep later tenants out of every batch indefinitely. This repo already has this exact shape
recorded as a landmine elsewhere.
- [ ] Fair selection: deterministic round-robin over eligible `user_id`s, or a windowed query
      allocating per account before the global limit. **Test:** one tenant with a large backlog
      against several tenants with later rows; every tenant must make progress.

**16-6 (P1) — grants and RLS cannot express the disposition state machine.** The API is
`NOBYPASSRLS` (`scripts/provision_rls_roles.sql:55-59`) and has no grant on the worker tables
(`:150-152`). RLS bounds the TENANT; it says nothing about which transition is legal. If the API
can insert arbitrary dispositions it can write terminal verdicts or fabricated exclusions.
- [ ] A `BEFORE INSERT/UPDATE` **trigger** enforces: API may create only the approved INITIAL
      states; API may never perform a worker verdict transition; system transitions follow the
      matrix; action status and event agree. A `SECURITY DEFINER` function is the alternative
      and is only safe if it validates `app.current_user_id`, pins `search_path` and applies
      the tenant predicate — otherwise it IS an RLS bypass.
- [ ] Explicit per-operation policies, never a blanket `FOR ALL`: actions — app SELECT, INSERT
      plus only the narrow update path; action_results — app SELECT, INSERT, system SELECT,
      INSERT, UPDATE; events — both roles SELECT, INSERT and **neither** UPDATE or DELETE.
      Shape follows `scripts/apply_rls_cutover_policies.sql:90-97,183-203`.

**16-7 (P2) — append-only is not the same as trustworthy.** SELECT+INSERT stops mutation but
still lets the API fabricate history. Restrict API event insertion to the initial action event
through the same trigger/function.

**16-8 (P2) — "the number can only go down" holds only if the quoted id set is IMMUTABLE.**
The planner reads `results` only and can overstate under drift. It cannot overcharge, because
the worker re-authorizes against the queue and the active-claim unique index refuses a second
row. It becomes FALSE the moment confirmation re-runs the planner and adds newly eligible ids.
- [ ] Confirmation may EXCLUDE ids, never ADD them; the durable quoted set bounds the worker.

**16-9 (P2) — child-side FK indexes, absent from the plan:** `contact_lookup_actions(job_id,
user_id)`, `contact_lookup_action_results(action_id, user_id)` and `(result_id, user_id)`,
`contact_lookup_action_events(action_id, user_id)`, `pending_skip_trace_rows(action_id, user_id)`.
Without them RLS reads, cascades and reconciliation degrade as actions accumulate.

**16-10 (P1) — the writer inventory is incomplete, and a writer hides in `scripts/` again.**
`scripts/repair_probate_party_and_bad_parcel.py` sets `skip_trace_status='queued'` (`:253`) and
`'not_attempted'` (`:271`) directly. This is the THIRD phase in which an ops script turned out
to be part of the concurrency design. Audit or disable every queue/status writer before 1b-2,
not only `backfill_skip_trace_jobs.py`.

**16-11 (P1, VERIFIED, found while extracting the patterns — NOT in 15-8) — there is a THIRD
RLS script, and a table missing from it is never FORCEd.** 15-8 names
`provision_rls_roles.sql` and `apply_rls_cutover_policies.sql`. It omits
**`scripts/apply_rls_force.sql`**, whose `tbls text[]` array (`:31-49`) is the list the
convergence loop iterates to apply `FORCE ROW LEVEL SECURITY`. Without FORCE, RLS does not
apply to the table OWNER, so a policy can look correct and still not constrain every path.
- [ ] Append all three new tables to that array **and to the commented rollback array
      (`:96-106`)**, or FORCE silently never reaches them.
- [ ] Note also `apply_rls_force.sql:56-70` HARD-FAILS if a `SECURITY DEFINER` function's owner
      lacks BYPASSRLS. That is a second reason to prefer the plain trigger below over a
      `SECURITY DEFINER` function for 16-6.

**16-12 — WITHDRAWN 2026-09-22. THE PREMISE WAS FALSE, and it is worth knowing why.**
The claim below is wrong: **nothing in this repository calls `create_all`.** Both the local rig
(`C:/Users/Windows/bl-testenv/run-full-pytest.sh`) and CI (`.github/workflows`, line 116) build
the test database with **`alembic upgrade head`**, and `tests/conftest.py` mentions neither. So
migration 101's trigger IS present in every test database and a mirror in `models.py` would be
dead code plus a second copy to keep byte-identical for nothing. It was written, then removed
after checking.

The reason it was believable is the interesting part: `models.py`, `alembic/env.py` and
migrations 049 and 089 all carry comments asserting that create_all is how test databases get
their functions. **Four stale comments agreeing with each other read as documentation.** They
misled the pattern extraction, the reviewer and me in turn. The `_RESULT_PARSE_FILING_DATE_FN`
mirror at `models.py:39-71` appears to be dead for the same reason; left alone as pre-existing
and out of scope, but it is not the precedent it looks like.

~~ORIGINAL CLAIM (kept so the correction is legible): tests build the schema with
`create_all`, not migrations.~~ A trigger created only inside migration 101 simply would not
exist in the test database, so every test asserting "the API cannot write a terminal
disposition" would pass **vacuously** — the exact failure this project has already hit twice
(a stubbed fixture, and a test that copied its implementation).
- [ ] Mirror the trigger as a `before_create` DDL on the metadata, the way
      `_RESULT_PARSE_FILING_DATE_FN` is registered at `src/db/models.py:39-71`, and keep it
      **byte-identical** to the migration's copy (that file's own comment demands exactly this
      of migration 049's function).
- [ ] The trigger follows the ONE trigger precedent in the repo
      (`023_add_county_records.py:71-86`): plain `LANGUAGE plpgsql`, NOT `SECURITY DEFINER`,
      reading `current_setting('app.current_user_id', true)`. That GUC is the discriminator the
      whole design needs: the API always sets it (`src/api/deps.py:17-42`, transaction-scoped,
      re-applied by the `after_begin` listener at `src/db/session.py:231-284`), while the worker
      uses `system_sync_session()` and sets **no GUC at all**. So "is this the API or the
      worker?" is `uid <> ''` — and, per 023's own rationale, a trigger fires regardless of role
      privileges, including for BYPASSRLS and superuser, which is why it is the actual guarantee
      where a grant is not.

### REJECTED from round 16, with reasoning

**Codex asked that 101 consult `pg_stat_progress_create_index` before dropping an INVALID index,
claiming 098 and 100 drop one blindly. They do not, and the check would be redundant.**
Migration 100 already reasons about exactly this (`100_...py:122-138`): inside a migration an
invalid index is dead and never mid-build, **because migrations are serialized by the advisory
lock in `scripts/migrate.py`** — and `start.sh:41` really does boot through
`python scripts/migrate.py`, not bare `alembic upgrade`. 100 also checks the index by IDENTITY
(unique, key count, plain column, `result_id`, exact predicate), which is stronger than validity
alone. 101 copies that pattern and **states the migrate.py dependency in a comment**, because
the argument collapses if anything ever migrates with bare alembic. Outside a migration the
opposite rule stands: an invalid index may simply be BUILDING.

### Revised split and ORDER (supersedes the 1b-1 ordering in "Agreed split")

1. **1b-1a SCHEMA** — migration 101: `results.last_trace_outcome` (column only),
   `pending_skip_trace_rows.action_id`, composite `(id,user_id)` uniqueness for `jobs`,
   `results` and `contact_lookup_actions` (CONCURRENTLY, then `ADD CONSTRAINT ... USING INDEX`
   under its own `lock_timeout`), the three tables, child-side FK indexes, grants, RLS policies
   and the disposition trigger, plus both RLS scripts. No endpoint, no writer semantics.
2. **1b-1b HARD DISPATCH CAP** — atomic reservation, `min(global, account)`, fair selection,
   per-account resume time as ONE window query published through a Redis pipeline.
   **This MOVES AHEAD of the quote** (see below).
3. **1b-1c PLANNER + QUOTE** — `plan_contact_lookup`, the immutable quoted set, upper-bound
   wording, Redis-as-advisory, the quote endpoint.
4. **1b-2 WRITERS** — confirm, the worker claim, `last_trace_outcome` writes, events,
   all existing-writer integrations, reconciliation and settlement.

**Why the cap moves ahead of the quote:** the quote is specified to show `paused_by_daily_cap`
and a resume time. Shipping the quote first would have it promising a pause/resume state that
the cap does not yet compute per account, so the first thing a customer sees would be the one
number the system cannot yet honour. **The cap is therefore NOT a read-path feature** — it
changes the component that spends — which is the part of D3-b that "1b-1 touches no money" got
wrong.

### 1b-1a diff review round 19 (2026-09-24, on `7bc1893a`) — NO-GO, 1 P1 + 2 P2

Rounds 17 and 18 are recorded in the commit messages of `92bc48ab` and `7bc1893a`. Round 19
found no fifth grant path (every GRANT/REVOKE/POLICY/FORCE site in the repo was listed and
checked; no `ALTER DEFAULT PRIVILEGES` anywhere) and confirmed the round-18 ordering is right.
All three findings below were re-verified in code before being accepted.

**19-1 (P1, VERIFIED, and WIDER than reported) — `contact_lookup_actions` has no guard
trigger.** Only results and events are guarded. Codex named the UPDATE path (the column grant
lets the API do `failed -> dispatching`, `dispatching -> settled`). The INSERT is worse: it is
table-wide, so a user-scoped session can CREATE an action already `claimed`/`running`, with a
lease token, `started_at`, and invented counts. Per 15-2 the API owns exactly one hop: it
creates the action in `dispatching` and may stamp `dispatched_at` on a retry. It never changes
`status`.
- [ ] Third trigger `contact_lookup_actions_guard` (same GUC discriminator, plain plpgsql, not
      SECURITY DEFINER). User-scoped INSERT: `status='dispatching'`; `lease_token`,
      `lease_expires_at`, `started_at`, `claimed_at`, `settled_at` NULL; `claimed_count`,
      `reused_count`, `newly_queued_count`, `billable_rows`, `tracerfy_credits` = 0
      (`quoted_count` and `truncated` are the API's to write). User-scoped UPDATE: only when
      `OLD.status='dispatching'` AND `NEW.status = OLD.status`. DELETE: refused. A BEFORE
      UPDATE trigger sees the row version it locked, so an API update racing the worker's
      `-> running` is re-checked against `running` and refused (tested, two connections).
- [ ] Tests: each forbidden INSERT shape, each forbidden transition, the permitted insert and
      permitted `dispatched_at` stamp, the worker path unaffected. Mutation-test the guard.

**19-2 (P2, VERIFIED) — the event guard's state check races.** The parent lookup takes no
lock; at READ COMMITTED the API can see `dispatching`, the worker commits `running`, and the
API's `dispatching` event commits after it.
- [ ] Parent lookup takes `FOR SHARE` (FOR KEY SHARE is not enough: a status change is a non-key
      update and does not conflict with it). The API role can do this: it has column UPDATE
      privilege on the table and a tenant `_app_update` policy. Test with two connections.

**19-3 (P2, VERIFIED) — both verify blocks are blind to column grants.**
`provision_rls_roles.sql:205-236` and `_cutover_step2_grants_policies.py:99-118` read
`information_schema.role_table_grants`, which does not list column privileges, so they pass
while the API has ZERO UPDATE on actions — exactly the round-18 bug.
- [ ] Both verifiers assert, via `information_schema.column_privileges`, that `bridgeleads_app`
      holds UPDATE on exactly `{status, status_reason, status_changed_at, dispatched_at}` of
      `contact_lookup_actions` and has no table-level UPDATE there.

Files: migration 101, `provision_rls_roles.sql`, `_cutover_step2_grants_policies.py`,
`tests/test_contact_lookup_schema.py` (4, inside the 5-file rule).

**Pre-code consult on the plan above (2026-09-24): PLAN: REVISE. All accepted.** Prompt and
output: `<scratchpad 803e30a3>/codex_r19_fixplan{,_out}.txt`. Confirmed sound: the
BEFORE UPDATE race claim (EvalPlanQual re-reads the committed row before the trigger fires),
and FOR SHARE (needs UPDATE on at least one column plus an UPDATE RLS policy, both present; no
deadlock with the FK's FOR KEY SHARE). The revisions below SUPERSEDE the matching bullets
above:
- **(P1) Timestamps are server-owned, not API-supplied.** A `server_default` is skipped whenever
  the caller passes a value, so an API-written future `created_at` could keep a `dispatching`
  action from ever expiring and strand its quoted rows. For a user-scoped session the triggers
  OVERWRITE `created_at` and `status_changed_at` (actions), `at` (events) and `decided_at`
  (action_results) with `now()`.
- **`dispatched_at` is NULL on insert and set once**, `NULL -> now()`, only while the action is
  still `dispatching`. It is not restamped on retry: a restamp would let a retry push back the
  apparent dispatch clock. This matches 15-2 ("still `dispatching` with no `dispatched_at` ->
  dispatch again").
- **(P2) Narrow the column grant to `(dispatched_at)`** at all three grant sites. `status` and
  `status_changed_at` are dead under the trigger rule, and a grant that matches the real writes
  is what makes the verifier mean something. Order stays: table-wide REVOKE, then the column
  grant. The REVOKE also wipes the old four-column grant, so an existing database converges.
- **(P2) The API writes no free-text reasons.** `status_reason` on the action and `reason` on its
  event must be NULL from a user-scoped session: the API's hop needs no explanation, and
  history is evidence.
- **`quoted_count >= 0` CHECK** (API-set at confirm, immutable after: no grant and the trigger
  refuses it). `truncated` stays API-set.
- **(P2) The verifiers use `has_table_privilege` / `has_column_privilege`**, which take the target
  role explicitly and report effective privilege. `information_schema.column_privileges` is
  filtered by the current user's role membership, so it is not reliable here. Assert: no
  table-level UPDATE; UPDATE on `dispatched_at`; no UPDATE on any other column.
- **Carried into 1b-2, no code now:** the FK proves a quoted result belongs to the tenant, NOT
  that it was actually in the quote. The worker stays authoritative for eligibility and
  re-authorizes every id (already in the 1b worker bullet).

- [x] Round-19 fixes implemented, `0cd956b9`. 58 tests; mutation-tested (trigger dropped: 28
      fail; FOR SHARE removed: the two-connection race test fails; verifier checked in four
      real grant states).

### 1b-1a diff review round 20 (2026-09-25, on `0cd956b9`) — NO-GO, 2 P1 + 1 P2

All five round-19 fixes confirmed correct, no new defect from them. Prompt and output:
`<scratchpad 0f367d2a>/codex_r20_review{,_out}.txt`.

- [x] **20-1 (P2, VERIFIED by building it)** the events guard read its parent unqualified, so a
      session's own `pg_temp.contact_lookup_actions` saying `dispatching` could vouch for a real
      action already `running`. Now `public.`-qualified, and all three guards pin
      `SET search_path = pg_catalog, public, pg_temp`. `b7ad8949`; the test fails with DID NOT
      RAISE against the old guard.
- [x] **20-2 (P1) an empty GUC meant "the worker" whatever the ROLE.** Checked production
      read-only (owner-run, 2026-09-25): worker `DATABASE_URL`=`bridgeleads_app`,
      `DATABASE_URL_SYNC`=`bridgeleads_system`; api BOTH DSNs = `bridgeleads_app`; neither
      superuser nor BYPASSRLS. So an API code path opening `system_sync_session()` would have
      passed every guard as the worker. Owner chose to harden: an empty GUC is accepted only
      for `bridgeleads_system` or a superuser/BYPASSRLS role (owner, migrations, ops); every
      other role is refused. Tests run AS the real roles (`SET LOCAL ROLE`); reverting the
      branch fails the three API-role tests and leaves the worker test green.
      **Not closed, and pre-existing:** the GUC is still the tenant identity for every RLS
      table in the product, so a session that can run arbitrary SQL as `bridgeleads_app` can
      claim any tenant. It is set server-side from the verified JWT with a bound parameter
      (`src/api/deps.py:36-40`), so reaching it needs SQL injection. Project-wide, out of this PR.
- [ ] **20-3 (P1, future, 1b-2 gate)** the composite FK proves a quoted result is the tenant's,
      not that it was in the quote. Same as the carried bullet above: the 1b-2 worker must
      re-authorize every id against the action's job and its immutable quoted set before it
      creates a pending row. No spend path exists in 1b-1a.

### Round 21 (2026-09-25, on `422f1a42`) — **GO**

Both round-20 fixes confirmed correct, no new defect; both new tests exercise the real path.
Prompt and output: `<scratchpad 0f367d2a>/codex_r21_review{,_out}.txt`. One P3 (the worker-role
pass-through test covered one table) fixed by parametrizing it over all three; 65 passed.
**Two things to carry, neither blocking:**
- **Hard deletes by a tenant now fail closed.** Every FK into these tables cascades, and every
  guard refuses a user-scoped DELETE, so deleting a job, result or user from a request path
  would fail once that user has an action. Nothing does that today (the API only soft-cancels
  jobs, `routes/jobs.py:349-380`; retention runs as the system role). Whoever builds account
  deletion / DSAR erasure must run it through `system_sync_session()`.
- **UNVERIFIED:** whether `bridgeleads_app` holds membership in `bridgeleads_system` with SET,
  which would let it `SET ROLE` past the guard. A read-only `pg_auth_members` check on
  production would settle it; if it does hold one, that is a privilege-boundary defect in its
  own right.

## Phase 1b-1b — HARD per-account dispatch cap (PLAN, 2026-09-25, pre-Codex-consult)

Branch `feat/lookup-1b1b-cap` off `fc38e620`. Required shape: findings 16-4, 16-5, 15-10,
15-11. No migration.

### Facts this plan stands on (read in code, `src/workers/skip_trace_dispatcher.py`)
- The global cap (`:72-113`) counts `submitted_at >= now-24h` in its OWN session BEFORE the
  claim's `pg_try_advisory_xact_lock` (`:218`). Two ticks can both pass it, and one tick may
  claim several batches (`SKIP_TRACE_MAX_BATCHES_PER_TICK`, default 2) and up to 5000 rows per
  batch after one check. It is SOFT (15-11).
- Every claim pass takes the advisory lock, selects `queued` rows `ORDER BY enqueued_at LIMIT
  5000 FOR UPDATE SKIP LOCKED` (`:225-283`), filters in Python (withdrawn / unsubmittable /
  held in flight), then marks the survivors `submitting` with `submitted_at = claim_time` and
  COMMITS (`:349-357`), which releases the lock. Only then does it POST to Tracerfy.
- `submitted_at` is the right "spent" measure: set at claim, kept through `submitted` and
  ingest (charged rows keep it), and cleared to NULL by `_release_claim` (`:1282`) on every
  uncharged release (rate limit, 5xx, connection error, out-of-credits remainder, definite
  rejection). A `submitting` row of UNKNOWN outcome keeps it, so it already counts as spent
  (16-4). Beat interval is 300s (`scheduler.py:195`).

### Design
1. **One authoritative capacity read, INSIDE the claim transaction, after the advisory lock.**
   A single query returns `global_spent` and `spent` per `user_id` over the rolling 24h
   window. `global_remaining = cap - global_spent` (unbounded when the cap is 0 = disabled);
   `account_remaining(u) = account_cap - spent(u)` (unbounded when 0). Because every claim
   decision holds the same lock and every earlier claim is committed with its `submitted_at`,
   a second tick, and the second batch of the same tick, see the first one's spend. The cap
   becomes HARD. The pre-lock check at `:72-113` stays only as the cheap early exit and the
   ops alert, and reads through the same helper so the two cannot disagree.
2. **Fair selection in SQL, not Python.** Candidate ids come from a subquery: the eligible
   `queued` rows (every predicate the current query has, unchanged) numbered
   `ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY enqueued_at, id) AS rn`, kept only where
   `rn <= account_remaining(user_id)`, ordered by `(rn, enqueued_at, id)` and limited to
   `min(5000, global_remaining)`. Ordering by `rn` first is the round-robin: every eligible
   account's oldest row, then every account's second, and so on, so one big backlog cannot
   keep later tenants out of a batch. The outer query locks exactly those ids with
   `FOR UPDATE OF pending_skip_trace_rows SKIP LOCKED` (Postgres forbids FOR UPDATE beside a
   window function at the same level).
3. **The Python filters after selection can only LOWER the count.** Withdrawn, unsubmittable
   and in-flight-held rows drop out; nothing is added after the cap is applied. No change to
   the claim, POST, release, or bookkeeping code.
4. **Per-account resume time, ONE window query** (15-10) over the same 24h window: for each
   account with `spent >= account_cap` and at least one eligible `queued` row, resume_at is
   the `(spent - cap + 1)`-th oldest `submitted_at` + 24h, ordered deterministically by
   `(submitted_at, id)`. The same for the global cap. Published as ONE Redis hash
   (`skip_trace:cap_pause`: field `user_id` or `__global__` -> ISO resume_at) replaced whole
   in a MULTI pipeline (`DEL` + `HSET` + `EXPIRE`) each tick, so an account that stops being
   capped disappears on the next tick. TTL = `max(2*beat_interval + grace, resume_at - now +
   grace)`. Redis is ADVISORY: a Redis failure is logged and never changes a dispatch decision.
5. **Setting:** `SKIP_TRACE_ACCOUNT_DAILY_ROW_CAP` (int, 0 = disabled), in `settings.py` and
   `.env.example`. The unit is ROWS, the same unit as the global cap and as billing (D2: an
   advanced row costs 2 credits but bills 1 row).

### Owner decisions needed BEFORE deploy (not before code)
- The production value of `SKIP_TRACE_ACCOUNT_DAILY_ROW_CAP`, and whether it should vary by plan.
  The code default is 0 (deploying changes nothing), and the code default is not the prod value
  ([[code_default_is_not_the_production_value]]), so it must be SET on the worker.
- Rows vs credits as the unit (above).

### Files (5)
`src/workers/skip_trace_dispatcher.py`, NEW `src/workers/skip_trace_capacity.py` (the capacity
read, the fair-selection subquery, the resume-time query, the Redis publish),
`src/config/settings.py`, `.env.example`, NEW `tests/test_skip_trace_account_cap.py`.

### Tests (isolated DB; the concurrency ones with two real connections)
- [ ] Account at its cap: none of its rows claimed; other accounts' rows are.
- [ ] Account below its cap by k: exactly k of its rows claimed in one pass, even when 5000
      are eligible.
- [ ] Global cap hard: two ticks racing on two connections never claim more than
      `global_remaining` together; same for two batches in one tick.
- [ ] Unknown-outcome `submitting` rows count as spent; released rows (NULL `submitted_at`) do not.
- [ ] Fairness: one tenant with a 6000-row backlog enqueued first, four tenants with later
      rows; every tenant has rows in the first batch.
- [ ] Rolling window: a row submitted 24h+1s ago no longer counts.
- [ ] Resume time: exact value for a known spend history; deterministic on `submitted_at` ties.
- [ ] Redis: the hash is written, replaced, and cleared when an account drops below its cap;
      Redis down leaves dispatch unchanged.
- [ ] Both caps 0: nothing is limited (up to 5000 eligible rows claimed, as today); only the
      ORDER changes, to round-robin. Fairness applies whether or not a cap is set.
- [ ] Mutation: remove the in-transaction read (keep only the pre-lock check) and the race
      test must fail; remove `rn` from the ORDER BY and the fairness test must fail.

### Pre-code consult on the 1b-1b plan (2026-09-25) — PLAN: REVISE, 7 P1 + 7 P2

Prompt and output: `<scratchpad 0f367d2a>/codex_1b1b_consult{,_out}.txt`. #1-#3 re-verified in
code before being accepted; they are PRE-EXISTING defects in live code, not in the plan.

- **C1 (P1, VERIFIED, live)** `_persist_submission` writes `pending_skip_trace_rows.submitted_at
  = now` at `submitted` (`skip_trace_dispatcher.py:1181`). The adoption path calls it with
  `adopted=True` (`:1663`), possibly days after the claim, which drags an old paid batch into
  today's window. The GLOBAL cap is live at 1000 in production, so this already over-counts
  today. Fix: the pending row keeps `submitted_at == claim_time` through `submitted`, retry,
  adoption and ingest; only `SkipTraceQueue.submitted_at` takes the bookkeeping time.
- **C2 (P1, VERIFIED, live)** that UPDATE is pinned on `status='submitting'` only, not on the
  claim generation. A delayed bookkeeping retry could stamp an old queue id onto a newer claim.
  Fix: `WHERE status='submitting' AND submitted_at=:claim_time`, check the row count, route a
  mismatch to the orphaned-queue alert. Never attach an old queue id to a newer claim.
- **C3 (P1, VERIFIED, live, ops script)** `scripts/repair_stuck_skip_trace_claims.py:88-99`
  selects, then updates by `id` only. Fix: one pinned conditional UPDATE ... RETURNING; touch
  `results` only for rows actually released.
- **C4 (P2)** `scripts/repair_probate_party_and_bad_parcel.py:214-245` may requeue an `errored`
  row carrying submission evidence. Fix: require `tracerfy_queue_id IS NULL AND submitted_at IS
  NULL`, else quarantine.
- **C5 (P1)** post-selection filters (held in flight, unsubmittable) can drop an account's rn=1
  row every tick, so its rn=2+ rows never go. **C6 (P1)** the outer SKIP LOCKED drops allocated
  rows without replacement. Fix for both: a bounded REFILL loop inside the same locked
  transaction: re-run the fair selection excluding ids already dropped, until the allowance is
  used or nothing is left. Tests: a permanently held head, a locked head.
- **C7 (P2)** perf: EXPLAIN (ANALYZE, BUFFERS) gate at production scale; an index on
  `(user_id, submitted_at)` may need a migration (102).
- **C8-C10 (P2)** Redis contract for 1b-1c: a `published_at` heartbeat so a stale or missing
  publish reads as UNKNOWN, never "not paused"; publish EVERY account at or over its cap, with
  or without queued rows (the quote comes before any row exists); the API reads only
  `HGET <own user_id>`, never the whole hash; the key is namespaced; the TTL covers the latest
  resume time and each value is compared with now. (2026-09-27: "HGET" became a fixed-field
  `HMGET` of metadata + `global` + the own field: see "Phase 1b-1b-iii" consult r1 E5.)
- **C11 (P1) unit.** A row cap lets 100 advanced rows spend ~200 credits. OWNER DECISION below.
- **C12 (P1)** freeze the unknown-outcome state machine: test initial unknown, partial-resubmit
  unknown, 429/5xx, definite rejection, full and partial 402, reconciler release, adoption,
  ambiguous. Unknown `submitting` counts until reconciled.
- **C13 (P2)** scope: beat interval to a setting that `scheduler.py` consumes; update
  `tests/test_skip_trace_daily_cap.py`; the two scripts; maybe migration 102.
- **C14 (P2)** `skip_trace_capacity.py` imports nothing from the task; ONE `_CLAIM_LOCK_KEY`.

### OWNER DECISIONS (2026-09-25)
- **Unit = CREDITS, weighted** (normal=1, advanced=2), per account AND globally; resume time uses
  cumulative cost. The live global cap (`SKIP_TRACE_DAILY_ROW_CAP=1000`) changes meaning from
  rows to credits, so its production value is re-read before 1b-1b-ii deploys (rename the
  setting to say credits; keep the old name readable for one release).
- **Three PRs, ledger first**, as below.

### 1b-1b-i plan (branch `feat/lookup-1b1b-ledger`), pre-code
Every reader of `pending_skip_trace_rows.submitted_at` was listed (`grep` over `src/`). Readers
that only see `submitting` rows (reconciler, release, stale alert) are unaffected by C1. Three
see `submitted`/`unmatched` rows:
- `scheduler_helpers/dialer.py:84-89` ages a `submitted` row out after 12h by `submitted_at`.
  Today adoption restamps it to now, which gives an adopted batch 12h for its phones to land.
  **With C1, an adopted row from days ago ages out at once**, and the dialer can push the job
  before the adopted batch's phones arrive (its own comment: it never pushes again). So C1
  REQUIRES the dialer to age a `submitted` row by its batch's bookkeeping time
  (`skip_trace_queues.submitted_at`, joined on `tracerfy_queue_id`), falling back to the row's.
- `skip_trace_dispatcher.py:747` and `enrich.py:2369`: the 90-day charged-unanswered window.
  The claim time is the truer purchase time; the difference is seconds (days only on adoption).
  No change.

Changes:
- [x] C1 `_persist_submission`: pending rows keep `submitted_at`; only the queue row takes now.
- [x] C2 same UPDATE pinned on `submitted_at = claim_time`; rowcount checked; a shortfall is
      logged and alerted with the queue id (the adoption backstop still re-derives it).
- [x] Dialer ages `submitted` rows by the batch's queue `submitted_at` (above).
- [x] C3 `repair_stuck_skip_trace_claims.py`: one pinned conditional UPDATE ... RETURNING.
- [x] C4 `repair_probate_party_and_bad_parcel.py`: requeue only rows with no queue id and no
      `submitted_at`.
- [x] C12 tests freezing the state machine, plus C1/C2/dialer regression tests, mutation-tested.
Files: dispatcher, dialer, 2 scripts, 1 new test file = 5.

**Pre-code consult on 1b-1b-i (2026-09-25): PLAN: REVISE, all accepted.** Output:
`<scratchpad 0f367d2a>/codex_1b1bi_consult_out.txt`. Supersedes the matching bullets above:
- Dialer: age by the queue's `submitted_at`; fall back to the row's only when
  `tracerfy_queue_id` IS NULL. A non-null queue id with NO queue row keeps the job unsettled
  and alerts; never a one-shot push of a possibly paid orphan.
- Reader inventory also includes the live global cap (`dispatcher:90`), which C1 makes correct.
- Adoption (`:1650-1666`) passes NO `claim_time` today: pass it, and pin the update on
  `submitted_at = claim_time AND trace_type AND tracerfy_queue_id IS NULL`.
- A pinned-update SHORTFALL never rolls back the queue insert (Tracerfy has charged; ingest
  would discard the batch as `unknown_queue`). `UPDATE ... RETURNING`, commit the queue and the
  matched rows, and send a DISTINCT partial-bookkeeping alert with the queue id and the
  unmatched row ids. Rows that missed the pin already belong to a newer claim or were released,
  so this is double-pay DETECTION, not prevention; flag at diff review.
- `_release_claim` gets `RETURNING id, result_id, user_id` and updates `results` only for the
  returned `(result_id, user_id)` pairs (today it updates the original set: a latent bug in the
  live path too). The C3 script reuses it.
- **New invariant:** `trace_type` never changes once `submitted_at` or `tracerfy_queue_id` is
  set, or a weighted cap can be rewritten after the fact. Application-enforced here (C2/C4
  predicates + tests); a DB trigger or a per-row credit snapshot is decided in 1b-1b-ii.
- Invariant after C1, as an acceptance criterion: for every row attached to an accepted queue,
  `pending.submitted_at == claim_time`; ingest preserves it.
- C12 tests go in a NEW file with real DB sessions; only `submit_batch` and the queue-list
  seam are monkeypatched. Existing coverage (not duplicated): `test_skip_trace_dispatcher_claim`,
  `test_skip_trace_reconciliation`, `test_tracerfy_ingest`, `test_skip_trace_over_quota`,
  `test_skip_trace_already_delivered`, `test_skip_trace_daily_cap`.

**1b-1b-i BUILT (2026-09-25), before Codex diff review.** 17 tests in
`tests/test_skip_trace_spend_ledger.py`; five mutations each caught by exactly their test (C1
restamp, C2 pin, release-set, dialer by row, dialer missing-queue). 349 passed across every
skip-trace/ingest suite; dialer, subject-key and script suites green. Found while building:
- `repair_stuck_skip_trace_claims.py` also looked at claims of ANY age (a claim seconds old has
  no Tracerfy queue yet because its POST is in flight) and skipped `_release_is_safe`; and it set
  the Result to `not_attempted` while the pending row went back to `queued` (queue/results
  drift). It now does exactly what the live reconciler does.
- Two tests in `test_repair_probate_party_and_bad_parcel.py` PINNED the unsafe re-point (clear
  the queue id; treat a queue id as a reason to re-point). Pre-2026-09-07 ingest wrote charged,
  unmatched rows as `errored` WITH a queue id, so that re-point re-bought paid lookups. Tests
  updated with the reasoning; this is the 6th file (over the 5-file rule, disclosed).
- The dialer predicate moved into `skip_trace_unsettled(now)` so it is testable; unchanged logic.
- Deviation from the consult: a 'submitted' row naming a missing queue keeps the job unsettled
  but does not alert (the state is unreachable through code). Flag at diff review.

**Diff review round 1 on `1d2154aa` (2026-09-25): NO-GO, no P1, 3 P2 + P3s.** C1, C2, the
release fix and the dialer correlation confirmed; retention never deletes queue rows. Fixed:
- P2 dialer: a job held by a row naming a missing queue now alerts (`_alert_rows_naming_missing_queues`,
  ops-alert cooldown + durable row). My deviation above is withdrawn.
- P2 the repair script lacked the live reconciler's CONTESTED-queue refusal. Extracted
  `contested_queue_ids()` and both use it; a contested match prints REFUSE.
- P2 `_CANCEL_PENDING` in the probate script lacked the evidence guard; it then reset the lead
  to `not_attempted`. Guarded like the re-point.
- P3 alert wording (only attached rows are ingested; the listed rows need reconciling).
- P3 tests: the outcome test asserts the POST happened; new tests for the bookkeeping retry,
  the reconciler's release, contested refusal, and the missing-queue alert.
Eight mutations, each caught by its own test. 434 passed across every touched suite.
**Deferred, recorded (not in this PR):** P3 the probate script's raw `results` updates are
keyed by UUID and not tenant-paired (pre-existing, script-wide); P3 a DB-backed test of the
probate script's caller behaviour (its SQL guards are tested; the callers are rowcount-gated).

### Revised split (ACCEPTED by owner, 5-file rule)
- **1b-1b-i SPENT-LEDGER HARDENING** (no feature; fixes live defects C1-C4 + C12 tests):
  dispatcher, the two scripts, tests. Ships alone, like 1b-0. The cap cannot be correct on top
  of a ledger that moves charged rows in time.
- **1b-1b-ii THE CAP**: capacity module, fair selection + refill, in-lock read, settings,
  `.env.example`, tests (+ migration 102 only if the EXPLAIN gate demands it).
- **1b-1b-iii PAUSE STATE**: resume-time query, Redis publish with heartbeat, beat interval
  setting, `scheduler.py`, tests.

## Phase 1b-1b-ii — the CREDIT-WEIGHTED hard cap (PLAN, 2026-09-25, pre-Codex-consult)

Branch `feat/lookup-1b1b-cap` off `2275c2de` (1b-1b-i live). Owner decisions: unit = CREDITS
(normal=1, advanced=2), per account AND global; three PRs. Codex findings it must satisfy:
C5, C6, C7, C11, C12, C13, C14, plus 16-4, 16-5, 15-11.

### Facts (post-1b-1b-i)
- `pending.submitted_at` is the claim time for every row attached to an accepted queue, and
  is cleared only by a proven-uncharged release (1b-1b-i). Unknown-outcome `submitting`
  rows keep it, so they count as spent.
- A claim pass handles ONE trace_type (`for trace_type in ("normal", "advanced")`), so inside
  a pass every row costs the same `c` (1 or 2). Credit allowances become ROW limits by integer
  division: `rows = floor(credits_remaining / c)`. No mixed-cost knapsack inside a pass.
- `pending_skip_trace_rows` has no index on `submitted_at` (only `(status, trace_type,
  enqueued_at)` and `(action_id, user_id)`). Prod held 941 rows on 2026-09-20; 1c actions add
  up to 2000 each.

### Design
1. **Weight** `credits(trace_type)` = 2 for `advanced`, else 1 — the SAME map as
   `_CREDITS_PER_ROW` (reused, not duplicated). Spent = `sum(weight)` over rows with
   `submitted_at >= now() - 24h`, globally and per `user_id`, in ONE query.
2. **In-lock read** (16-4, 15-11): inside each claim pass, after `pg_try_advisory_xact_lock`,
   before selecting. `global_rows = min(5000, floor((G - global_spent) / c))` (unbounded side
   when G = 0); per account `floor((A - spent_u) / c)` (unbounded when A = 0). Every earlier
   claim is committed with its `submitted_at`, so the next pass, the next batch and the next
   tick all see it. The pre-lock check at the top of the tick stays only as the cheap early
   exit + ops alert, computed by the SAME helper.
3. **Fair selection** (16-5): the existing eligibility select (every predicate unchanged)
   becomes a subquery with `row_number() OVER (PARTITION BY user_id ORDER BY enqueued_at, id)
   AS rn`; keep `rn <= account_rows(user_id)`; order `(rn, enqueued_at, id)`; limit
   `global_rows`. Outer: `SELECT ... WHERE id = ANY(:ids) FOR UPDATE OF pending SKIP LOCKED`.
4. **Refill loop** (C5, C6): after the lock and the Python filters (withdrawn / unsubmittable /
   held in flight), if fewer rows survived than the allowance and some were dropped or skipped,
   re-run the fair select EXCLUDING every id already considered, with each account's and the
   global allowance reduced by what already survived, until the allowance is filled or no new
   candidate exists (R3), with the in-flight hold run over ALL survivors so far (R2). All in the same
   locked transaction; nothing added after the cap is applied can exceed it, because each round
   is computed against the remaining allowance.
5. **Settings**: `SKIP_TRACE_DAILY_CREDIT_CAP` (global) and `SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP`,
   `int | None = None` (R9): `None` = unset, explicit `0` = disabled. The old
   `SKIP_TRACE_DAILY_ROW_CAP` stays readable for ONE release: if the new global is `None` and the
   old is set, the old value is used as credits and a WARNING is logged ONCE per process naming
   the change (1000 rows becomes 1000 credits = 500 advanced rows).
   **Owner sets the production values before deploy.**
6. **Migration 102** (its own PR, first): a partial index `(submitted_at) INCLUDE (user_id,
   trace_type) WHERE submitted_at IS NOT NULL`, built CONCURRENTLY, restart-safe like 100/101;
   plus a trigger forbidding a change of `trace_type` once `submitted_at` or
   `tracerfy_queue_id` is set (the weight of spent money must not be rewritable; 1b-1b-i made
   this an application invariant only).
7. **Not touched**: the claim commit, the POST, releases, bookkeeping, reconciliation (1b-1b-i
   froze them, C12). The unknown-outcome state machine is unchanged.

### Split (5-file rule)
- **1b-1b-ii-a**: migration 102 + models + `alembic/env.py` + tests (4 files). EXPLAIN (ANALYZE, BUFFERS) of the
  spent query and the fair select on a seeded 100k-row test DB, before and after, as the gate.
- **1b-1b-ii-b**: NEW `src/workers/skip_trace_capacity.py` (weights, the spent read, the fair
  select, the refill), `skip_trace_dispatcher.py`, `settings.py`, `.env.example`, NEW
  `tests/test_skip_trace_credit_cap.py`, with the legacy `tests/test_skip_trace_daily_cap.py`
  tests folded in (R11): 5 files. `alembic/env.py` belongs to ii-a (R6), not here.

### Tests (ii-b)
- [ ] Account at its cap: none of its rows claimed; others are. Below by k credits: exactly
      `floor(k/c)` rows.
- [ ] Weighting: an account 1 credit below its cap gets 1 normal row and 0 advanced rows.
- [ ] Global hard: two ticks on two connections never exceed `G` together; two batches in one
      tick neither.
- [ ] Unknown-outcome `submitting` counts; released (NULL) does not; 24h+1s no longer counts.
- [ ] Fairness: one tenant with a 6000-row backlog enqueued first + four later tenants: all in
      the first batch.
- [ ] Refill: a tenant whose rn=1 row is permanently held still gets rows 2..k; a locked head
      row is replaced.
- [ ] Both caps 0: up to 5000 rows claimed as today; only the ORDER changes.
- [ ] Deprecated-setting fallback + its warning.
- [ ] Mutations: read moved back outside the lock (race test fails); `rn` removed from ORDER BY
      (fairness fails); refill removed (held-head fails); weight 2 -> 1 (weighting fails).

### Pre-code consult on 1b-1b-ii (2026-09-25): PLAN: REVISE, 3 P1 + 7 P2 + 1 P3, all accepted
Output: `<scratchpad 0f367d2a>/codex_1b1bii_consult_out.txt`. Confirmed sound: the in-lock read
makes the cap hard for DISPATCHER spend (overlapping ticks, batches, partial 402, adoption);
per-pass cost is constant; the partial index shape fits. SUPERSEDES the matching bullets above:
- **R1 (P1, VERIFIED, wider than reported) five scripts spend OUTSIDE the dispatcher.**
  `scripts/sprint4_all_counties.py`, `sprint4_phase3_advanced.py`, `sprint4_phase3_king_pf.py`,
  `sprint4_phase3_preforeclosure.py`, `sprint4_phase3_verify.py` call `submit_batch()` directly:
  no lock, no pending row, no cap. Sprint 4 experiments. **Hard-disable all five** (the 1a
  precedent: `sprint4_enqueue_existing.py`): body deleted, running it prints the reason and
  exits 1. Own tiny PR, first: it is the only thing that makes "hard cap" a true statement.
  **DONE in ii-0**; Codex confirmed the dispatcher is then the only code path that spends.
- **R2 (P1) the in-flight hold must be CUMULATIVE across refill rounds.** `_hold_answers_in_flight`
  only sees the current list; a refill row can share an address with an earlier round's survivor
  and go out in the same batch (paid twice, answered by neither). Each round runs the hold
  against (earlier survivors + new rows). Regression test: a duplicate that only appears in a
  refill round.
- **R3 (P1) refill runs until the allowance is filled or NO new candidate exists**, not 3 rounds:
  15,001 blocked rows would otherwise starve the row behind them forever. Each round excludes
  every id already considered, so it terminates. Test: more blocked rows than one round's limit
  (the round size is a parameter so the test can make it small).
- **R4 (P2)** unknown `trace_type` must not count as 1 credit: migration 102 adds
  `CHECK (trace_type IN ('normal','advanced'))` (guarded: aborts if a row violates it), and the
  weight lookup raises on an unknown type instead of defaulting.
- **R5 (P2)** fairness is documented as "fair whenever a batch can hold one row per eligible
  account"; with less global headroom than that the earliest `rn=1` rows win. Low-headroom test
  pins the documented behaviour.
- **R6 (P2)** the index lives in the migration AND in `alembic/env.py` `CONCURRENT_INDEXES`, so
  autogenerate never proposes a blocking plain index.
- **R7 (P2) DECISION: fixed weight map + trigger** (smallest correct). The trigger refuses a
  `trace_type` change when the OLD or the NEW row carries `submitted_at` or `tracerfy_queue_id`
  (covers a single UPDATE that sets both). A per-row credit snapshot is recorded as the upgrade
  if the price map ever changes.
- **R8 (P2)** 102 follows 100/101: identity-checked CONCURRENT index with invalid-corpse
  cleanup under the migrate.py advisory lock, drop/create-idempotent trigger, CHECK added
  `NOT VALID` then validated. Lands (and is verified by the objects) before the cap code.
- **R9 (P2)** `SKIP_TRACE_DAILY_CREDIT_CAP: int | None = None`: `None` = fall back to the legacy
  row setting, explicit `0` = disabled. Warn once per process, not every tick.
- **R10 (P2, carried into iii)** resume time under weights = expiry of the oldest row at which
  the CUMULATIVE weight frees enough credits, not the `(spent-cap+1)`-th row; mixed-weight tests.
  The capacity helper exposes a pure `credits_for(trace_type)`; the API cannot reuse the worker's
  query (no grant on the queue), so the quote stays advisory and re-read at confirm.
- **R11 (P3)** the legacy cap tests fold into the new test file (ii-b stays at 5 files).

### Revised order for 1b-1b-ii
1. **ii-0** hard-disable the five spending scripts (5 files, no code path change).
2. **ii-a** migration 102 (index + CHECK + trigger) + `alembic/env.py` + tests; EXPLAIN gate.
3. **ii-b** the cap: `skip_trace_capacity.py`, dispatcher, settings, `.env.example`, tests.

**ii-0 BUILT** (PR #359): the five scripts retired; Codex REVISE -> GO.

**ii-a BUILT (2026-09-25), before Codex diff review.** `alembic/versions/102_...`, `models.py`,
`alembic/env.py`, `tests/test_pending_skip_trace_weight.py` (4 files).
- 8 tests: unknown type refused; a spent row cannot be retyped (claimed, accepted, and the legacy
  errored-with-queue-id shape); retyping while stamping the spend in ONE update refused; an unsent
  row may still change type (the probate name refresh); the claim and bookkeeping updates are not
  blocked; the index has the shape the cap reads.
- Mutations, each caught (run by hand, not checked in): trigger dropped (4 fail), CHECK dropped,
  trigger checking only the OLD row.
- Replay from four half-applied states (run by hand, not checked in: index only; index + NOT
  VALID check; a same-named index of the wrong shape; one on the wrong key): every one converged
  to exactly the right objects. Unknown-type abort: exits with the instruction and leaves NO index
  behind; clean after removal.
- **EXPLAIN gate at 100,000 spent rows (1,666 in the window, 50 accounts):** with the index a
  Bitmap Index Scan, **4.2 ms**; without it a Seq Scan, **38.8 ms**. The cost now follows the
  window, not the table. Seeded and measured inside one transaction, rolled back.

**Codex ii-a diff review (2026-09-25): NO-GO.** One P1 and four P2s, all addressed 2026-09-26:
- P1 UNVERIFIED precondition: the 941-row count never showed the trace_type distribution, and
  102 aborts on ANY unknown historical value. Fix: read-only prod `GROUP BY trace_type` preflight
  BEFORE merge. **DONE 2026-09-26 (owner-run, worker role, read-only):** advanced 440 (435 with
  submission evidence), normal 501 (499); UNKNOWN = 0; all 941 rows visible to the role. Server
  PostgreSQL 17.6 (>= 14 for CREATE OR REPLACE TRIGGER). No 102 object present yet.
  `alembic_version` is not readable by that role (returned no row), so it was not checked there.
- P2 trigger bypass: `BEFORE UPDATE OF trace_type` misses a later BEFORE trigger rewriting
  NEW.trace_type, and any statement whose SET list omits the column. Fix: `AFTER UPDATE ... FOR
  EACH ROW WHEN (OLD.trace_type IS DISTINCT FROM NEW.trace_type)`. It sees the final row, and the
  WHEN keeps the dispatcher's status updates free. `CREATE OR REPLACE TRIGGER` (PG14+; local 16.14).
- P2 index identity missed access method, order, collation and opclass. Fix: compare the whole
  `pg_get_indexdef()` to `_INDEX_DEF`, plus indisvalid.
- P2 CHECK idempotence was name-only. Fix: `contype = 'c'` and `pg_get_constraintdef()` must equal
  `_CHECK_DEF`, else ABORT (never drop it: it may be something else's).
- P2 lock held to commit. Fix: after the read-only guard everything runs in autocommit, one
  statement per transaction. Downgrade drops the index only if it is on this table.
- Checked-in tests added (13 total): upsert retype refused; same-value write allowed; retype by
  another BEFORE trigger refused; same-named index of another shape rebuilt; same-named CHECK
  that says something else aborts. Mutation: the pre-fix migration fails 3 of the 5 new tests
  (the other 2 pin behaviour it already had); a name-only CHECK fails the CHECK test.
- Replay: downgrade 101, upgrade, upgrade again (no-op) leaves the exact index def, a validated
  CHECK and exactly one AFTER UPDATE trigger.

**Codex ii-a re-review of d273c25b (2026-09-26): NO-GO.** It verified all four P2 fixes as correct
(AFTER trigger aborts UPDATE, UPDATE FROM and ON CONFLICT; OR REPLACE swaps BEFORE for AFTER; the
autocommit restructure is restart-safe and the version stamp lands after upgrade(); identity checks
complete; downgrade scoped). Open:
- P1 (unchanged): the prod trace_type preflight, owner-run. The preflight also prints
  `server_version`: 102 needs PG14+ in production (CI and compose pin 16).
- P2 test crash-safety: the DDL tests restored state only on exceptions. FIXED: an autouse fixture
  first puts the schema back exactly as 102 leaves it. Proven from a planted crashed state (index
  gone, impostor CHECK, leftover test trigger): 13 passed and nothing was left behind.
- P2 owner-level bypass: DELETE+INSERT with the same id, TRUNCATE, or DISABLE TRIGGER still
  rewrite effective weight. The runtime roles hold no DELETE/TRUNCATE/DDL on this table
  (provision_rls_roles.sql), so only the table owner can. **OWNER DECISION (2026-09-26): OUT OF
  SCOPE.** 102 guards against application and script bugs, not against the table owner, who can
  drop any trigger anyway. No delete guard, so account-deletion cascades and the retention purge
  are untouched.

**ii-a MERGED + LIVE 2026-09-26: PR #361 `4b963d1c`.** Merged after an all-zero quiet check;
verified in prod by the objects (index, CHECK, trigger all present). PG 17.6.

### ii-b implementation spec (2026-09-26, post-102, BEFORE Codex consult)
Branch `feat/lookup-1b1b-ii-b-cap` off `4b963d1c`. Supersedes Design 1-5 above where they differ;
R1-R11 still bind. Files (5): NEW `src/workers/skip_trace_capacity.py`,
`src/workers/skip_trace_dispatcher.py`, `src/config/settings.py`, `.env.example`, NEW
`tests/test_skip_trace_credit_cap.py` (absorbs `tests/test_skip_trace_daily_cap.py`, which is
deleted: a 6th path, but a deletion, R11).

What 102 now guarantees, and what ii-b may therefore rely on: every row's `trace_type` is
'normal' or 'advanced' (CHECK); a spent row's type never changes (trigger); the spent query has
its index. ii-b still RAISES on an unknown type in `credits_for` (R4): the CHECK is the database's
promise, the raise is the code's, and neither should silently count 1.

**`skip_trace_capacity.py` (pure where it can be, one query where it cannot):**
- `CREDITS_PER_ROW = {"normal": 1, "advanced": 2}`, moved here from the dispatcher (which
  imports it back for `affordable_row_count`, unchanged). `credits_for(trace_type)` raises
  `ValueError` on anything else.
- `resolve_caps() -> (global_cap, account_cap)`, `0` = disabled. Global:
  `SKIP_TRACE_DAILY_CREDIT_CAP` if not None, else the legacy `SKIP_TRACE_DAILY_ROW_CAP` read as
  credits with a WARNING once per process (R9). Account: `SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP`
  or 0. Both settings `int | None`, `ge=0`.
- `spent_credits(db, since) -> (global_spent, {user_id: spent})`: ONE query,
  `SELECT user_id, sum(CASE trace_type WHEN 'normal' THEN 1 WHEN 'advanced' THEN 2 END)
  FROM pending_skip_trace_rows WHERE submitted_at >= :since GROUP BY user_id`, the CASE BUILT
  from `CREDITS_PER_ROW` (one source of truth). Counts claimed, submitted, unknown-outcome and
  terminal rows alike: `submitted_at` is set at claim and cleared only by a proven-uncharged
  release (1b-1b-i). Rides `ix_pending_skip_trace_spent`.
- `row_allowance(cap, spent, cost) -> int | None`: `None` when cap is 0 (unbounded), else
  `max(0, (cap - spent) // cost)`. Pure.
- `fair_candidates(db, eligibility_stmt, cost, *, global_rows, account_room, exclude_ids)`:
  the dispatcher's eligibility select (every predicate unchanged) as a subquery with
  `row_number() OVER (PARTITION BY user_id ORDER BY enqueued_at, id) AS rn`; keep
  `rn <= account_rows(user_id)` (accounts with 0 room excluded in SQL, via a VALUES list of
  (user_id, rows) for capped accounts); `id NOT IN exclude_ids`; order `(rn, enqueued_at, id)`;
  limit `global_rows`. Outer `SELECT ... WHERE id = ANY(:ids) FOR UPDATE OF pending SKIP LOCKED`,
  re-ordered in Python to the subquery's order (FOR UPDATE does not preserve it).

**Dispatcher, inside each pass after `pg_try_advisory_xact_lock` (16-4, 15-11):**
1. `spent_credits` -> global and per-account row allowances for this pass's cost `c`. Global
   allowance 0 -> end the pass (rollback, `continue` to the next trace_type: advanced may be
   out of room while normal is not).
2. **Refill loop (R2, R3):** `survivors = []`, `considered = set()`, per-account remaining rows,
   global remaining rows (≤ 5000). Each round: `fair_candidates` excluding `considered`, sized
   to the global remaining; empty -> stop. Add all to `considered`. Run
   `_partition_still_deliverable` (withdraw + cancel), `_partition_submittable` (fail),
   then `_hold_answers_in_flight(db, survivors + new)` -- CUMULATIVE (R2); the result becomes
   `survivors` (earlier survivors come first, so they keep their place). Recompute remaining
   allowances from `survivors`. Stop when global remaining is 0 or every candidate account is
   full. Terminates: each round excludes every id already seen.
3. Claim `survivors` exactly as today (the claim UPDATE, the commit, the POST, releases,
   bookkeeping, reconciliation all UNCHANGED, C12).
4. The pre-lock check at the top of the tick stays as the cheap early exit + ops alert for the
   GLOBAL cap only, computed by `spent_credits` + `resolve_caps` (same helpers); its message
   names credits.

**Why the cap is hard:** every earlier claim committed its `submitted_at` before the lock was
released, so each pass, batch and tick reads spend that includes it. The partial-402 path only
ever submits FEWER rows than claimed. Adoption does not create spend (it attaches rows already
claimed). ii-0 removed the only other spenders.

**Documented limits:** fairness holds whenever a batch can hold one row per eligible account
(R5); below that, the earliest `rn = 1` rows win. An account over its cap simply waits (no
user-facing state until 1b-1b-iii).

**Owner, before merge:** production values for `SKIP_TRACE_DAILY_CREDIT_CAP` and
`SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP`. Prod has `SKIP_TRACE_DAILY_ROW_CAP=1000` (09-16); left as
is it becomes a 1000-CREDIT global cap (= 500 advanced lookups) with a warning.

### Codex pre-code consult on the ii-b spec (2026-09-26): PLAN: REVISE, 2 P1 + 3 P2 + 1 P3, all accepted
Output: `<scratchpad 0ade294c>/codex_iib_consult_out.txt`. Confirmed sound: in-lock allowance +
committed `submitted_at` make overlapping claims unable to exceed the cap; 402 partial, unknown
outcome, releases and adoption add no spend; the pre-lock global-only check can under-dispatch,
never over-dispatch; the cumulative hold keeps earlier survivors (list order) and FOR SHARE locks
last to the claim commit. SUPERSEDES the matching spec bullets above:
- **S1 (P1) a locked ranked head was not really replaced.** `rn <= room` was applied before the
  outer `FOR UPDATE SKIP LOCKED`, so an account with room 1 and a locked `rn=1` row had no `rn=2`
  to fall back on, and excluding ids AFTER ranking left gaps that ended refill early. Fix:
  `exclude_ids` is applied BEFORE `row_number()`; the helper returns every ALLOCATED id (pre-lock),
  including those SKIP LOCKED skipped; all of them join `considered`; rooms are charged only by
  SURVIVORS. The next round therefore ranks the next rows. Regressions: a locked `rn=1` with room
  1; a held/withdrawn head spanning several rounds.
- **S2 (P1) no allowance for zero-spend accounts or global-only mode.** Fix: no VALUES list of
  capped accounts. The fair select LEFT JOINs a `spent` CTE (the same weighted sum) and a VALUES
  list of rows already TAKEN this pass (survivors, per user), and computes per account
  `room = CASE WHEN :account_cap = 0 THEN NULL ELSE greatest(0, (:account_cap -
  coalesce(spent,0)) / :cost) - coalesce(taken,0) END`; keep `room IS NULL OR rn <= room`.
  Tests: global cap only with several zero-spend accounts; account cap with no prior spend.
- **S3 (P2) cost of re-ranking every round.** Gate: EXPLAIN (ANALYZE, BUFFERS) on a seeded DB with
  100k+ eligible rows AND a worst-case multi-round refill (15k+ blocked heads), budget ≤ 250 ms
  per round and ≤ 2 s per pass. If it fails, switch to a keyset frontier instead of re-ranking.
- **S4 (P2) `affordable_row_count` kept `.get(trace_type, 1)`.** Fix: it calls `credits_for()`;
  regression: an unknown type in the 402 path raises.
- **S5 (P2) legacy "unset" vs "0" indistinguishable.** Fix: `SKIP_TRACE_DAILY_ROW_CAP: int | None
  = None`. A new `SKIP_TRACE_DAILY_CREDIT_CAP=0` disables without touching the legacy value or
  warning. `.env.example` documents the new names commented out (no empty integer assignment).
- **S6 (P3) operator text says "rows".** Fix: the alert and log name credits, the effective cap
  and its source setting. The 09-18 security review and the 1b-0 handoff are dated records and
  stay as written; the ii-b PR description carries the change for operators.

### Codex consult round 2 (2026-09-26): PLAN: REVISE, 2 P2 + 1 P3, all accepted
Output: `<scratchpad 0ade294c>/codex_iib_consult2_out.txt`. S1, S2, S4, S5 verified closed; S1 proven:
survivors can never exceed the account or global allowance in one pass. SUPERSEDES where they differ:
- **T1 (P2) refill unbounded under live enqueues; worst case one round per id.** Fix, three parts:
  (a) **watermark**: the pass reads `clock_timestamp()` once, right after taking the lock, and every
  refill round also requires `enqueued_at <= :watermark`, so rows arriving mid-pass wait for the
  next pass. (b) **growing look-ahead** instead of one-row-per-room rounds: round `r` (0-based)
  allocates, per account, up to `room_remaining * 2**r` ranked rows (and globally up to
  `min(5000, global_remaining * 2**r)`); after the filters only `room_remaining` survivors per
  account (and `global_remaining` overall) are TAKEN, in rank order, and the extra survivors are
  simply not claimed (they stay 'queued'; their row locks end with the pass). So a head of k
  blocked rows is passed in about log2(k) rounds, which is what keeps R3 (no starvation) true.
  (c) **hard bound**: at most 12 rounds and a 2 s deadline per pass; when either is hit, the pass
  claims the survivors it has and logs `refill_truncated` with the counts. Residual limit,
  documented: an account starves only behind more than `room * 2**12` blocked rows at once.
  The keyset frontier is NOT built now; it is the escape hatch only if the S3 EXPLAIN gate fails.
- **T2 (P2) arithmetic and the 5000 bound.** Room is computed in SQL as
  `GREATEST(0, FLOOR((:account_cap - COALESCE(spent, 0))::numeric / :cost)::bigint - COALESCE(taken, 0))`,
  NULL when the account cap is 0. The global allowance is normalized in Python:
  `None -> 5000`, else `min(5000, allowance)`; `LIMIT` is never NULL. Boundary tests: caps and
  spend of 0, 1, 2, 3 against both costs (1 and 2), for the account and the global allowance.
- **T3 (P3) config and task-result text.** `settings.py` comments say credits; the early-exit
  result becomes `{"skipped": "daily_cap", "spent_credits": .., "cap_credits": .., "cap_source":
  "<setting name>"}`. The old keys (`spent_today`, `cap`) have no reader outside the absorbed
  test, so they are not kept.

### Codex consult round 3 (2026-09-26): PLAN: REVISE, 2 P2. T1(b), T2, T3 verified closed.
Output: `<scratchpad 0ade294c>/codex_iib_consult3_out.txt`. Implementation conditions it set for
T1(b), adopted: all allocated candidates go to the cumulative hold in exact rank order; account and
global caps are applied AFTER the hold; pre-lock allocated ids are tracked as `considered`.
- **U1 (P2) the watermark does not exclude a row inserted before it and committed after it**
  (`enqueued_at` is the server `now()` at insert, `models.py:1356`). Codex's fix (a REPEATABLE READ
  pass with the advisory lock as its first query) is **REJECTED, with reason**: in REPEATABLE READ
  the snapshot is taken when that first statement STARTS, before the lock is acquired. A
  concurrent tick can commit its claim and release the lock in between; this pass then takes the
  lock but reads spend WITHOUT that claim, and overspends the cap. That trades a FIFO nuance for a
  money bug. **Instead:** stay READ COMMITTED (the in-lock spend read sees every committed claim);
  keep the watermark as a best-effort bound; the HARD bound on the pass is T1(c). The fairness
  claim is reworded: FIFO and fairness hold among rows committed before the pass started; a row
  committing mid-pass may be taken in its rank position. It is eligible either way, and it can
  never exceed a cap. Regression: a row committed mid-pass is counted against the cap like any
  other (never over it).
- **U2 (P2) the 12-round cutoff is bounded starvation.** Accepted. R3's promise becomes BOUNDED
  fairness: an account is passed over only while more than `room * (2**12 - 1)` of its ranked
  rows are blocked at once (4,095 at room 1). `refill_truncated` telemetry kept (logged with
  counts). Regression at the documented cutoff: truncation happens, survivors found so far are
  claimed, nothing exceeds a cap. A durable keyset continuation frontier is the upgrade if the
  telemetry ever fires in production.

### Codex consult round 4 (2026-09-26): U1 rejection CONFIRMED; wording/telemetry adopted
Output: `<scratchpad 0ade294c>/codex_iib_consult4_out.txt`. Codex confirmed that REPEATABLE READ with
the xact lock in the first statement takes its snapshot BEFORE the lock, so the spend read could be
stale; the only safe RR design (session lock taken before BEGIN on a pinned connection) is "not
worthwhile": READ COMMITTED + the xact lock preserves the money invariant. Its remaining items were
wording and test specifications, adopted verbatim, so this closes the consult:
- **V1** Fairness applies to eligible rows VISIBLE BEFORE THE FIRST CANDIDATE READ, subject to the
  one-row-per-account / global-headroom rule (R5). Rows committing later are outside the FIFO
  guarantee and may be taken by rank. The in-lock spend read happens AFTER the lock is acquired, in
  the same transaction (READ COMMITTED).
- **V2** The starvation bound is CONDITIONAL: with no earlier deadline truncation, the round limit
  bounds inspection at `room * 4095`; the 2 s deadline may bound it lower.
- **V3** `refill_truncated` logs: reason (`round_limit` | `deadline`), rounds completed, elapsed ms,
  initial and remaining global room, considered / blocked / survivor counts, and the number of
  accounts still with room.
- **V4** Added regressions: a row committed mid-pass never takes spend past a cap; room 1 behind
  4,095 blocked rows plus a later survivor (the documented cutoff); an early deadline claims the
  survivors found so far without exceeding either cap.

### ii-b TO BUILD (checklist; the spec = "ii-b implementation spec" as amended by S1-S6, T1-T3, U1-U2, V1-V4)
- [x] `src/workers/skip_trace_capacity.py`
- [x] dispatcher: pre-lock early exit via the helpers; in-lock allowance; refill loop; `affordable_row_count` via `credits_for`
- [x] `settings.py` (+ `.env.example`)
- [x] `tests/test_skip_trace_credit_cap.py` (absorbs `test_skip_trace_daily_cap.py`): 90 pass. Existing
      skip-trace / Tracerfy / lookup suites: 475 pass (4 batches, local lookup1b DB).
- [ ] **EXPLAIN gate (S3): FAILS at 100k+, passes at 35k.** One real dispatcher pass, account cap 1
      credit, account 0 behind N held heads (each with an in-flight twin), 50 accounts:
      | queued | held heads | worst `allocate` round | refill |
      | 10,000 | 1,500 | 49 ms | all passed in 11 rounds, no truncation |
      | 35,000 | 5,000 | 166 ms | deadline after 6 rounds (2.1 s) |
      | 117,000 | 15,000 | ~440 ms (FAIL) | deadline after 2 rounds |
      The round-0 query alone: 12 ms / 39 ms / 167 ms (a window sort over every eligible row, which
      spills to disk at 117k). Much of each round's remaining time is the duplicate hold re-reading
      the account's in-flight rows every round. CAP HARDNESS IS UNAFFECTED: truncation only ever
      claims fewer rows, and says so (`refill_truncated`). What degrades is latency for an account
      behind thousands of blocked rows. Prod today: 941 rows in total.
      Fix per S3 = keyset frontier (per-account LATERAL scan from the last row considered), which
      needs an index `(trace_type, user_id, enqueued_at, id) WHERE status = 'queued'` = migration
      103, plus reading the in-flight set once per pass. **OWNER DECISION (2026-09-26): SHIP ii-b NOW
      with the documented envelope (per-round budget holds to ~35k queued; deeper blocked runs are
      truncated and logged, never over-claimed). The keyset frontier + migration 103 + one in-flight
      read per pass become ii-c, which MUST land before Phase 1c can create large queues.**
- [ ] **ii-c** (scheduled, before 1c): migration 103 index; per-account LATERAL keyset `allocate`;
      in-flight set read once per pass; re-run this gate at 117k / 15k and pass it.
- [x] mutations, each caught: read outside the lock (1 fails); `rn` out of ORDER BY (1); refill
      removed (4); weight 2 -> 1 (5); hold not cumulative (1). Also found and fixed by the tests: an
      account cap with no prior spend crashed `allocate` (a Python int where SQL was needed).
- [x] Codex diff review to GO (3 rounds; P1 retype-between-allocate-and-lock fixed; P2 room VALUES
      -> unnest); owner delegated the cap values: SKIP_TRACE_DAILY_CREDIT_CAP=2000,
      SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP=500 set on api + worker.

**ii-b MERGED + LIVE 2026-09-27 04:11Z: PR #364 `19230e72`.** Merged after an all-zero quiet check;
first dispatcher tick on the new code 04:17Z succeeded (1.05 s, no errors, no warnings).

## Phase 1b-1b-ii-c — the keyset frontier (PLAN, 2026-09-27, BEFORE Codex consult)

Why: ii-b's S3 gate fails at 117k queued / 15k held heads (~440 ms per `allocate` round, the pass
truncated by the deadline after 2 rounds): every round re-ranks EVERY eligible row with a window
sort that spills to disk, and every round's duplicate hold re-reads the account's in-flight rows.
Owner: must land before Phase 1c can create large queues. Nothing about cap hardness changes.

**Split (5-file rule), same shape as ii-a / ii-b:**
- **ii-c-1** migration 103 + `models.py` + `alembic/env.py` + `tests/test_pending_skip_trace_frontier_index.py`.
- **ii-c-2** `skip_trace_capacity.py` + `skip_trace_dispatcher.py` + `tests/test_skip_trace_credit_cap.py`.

**ii-c-1: migration 103.** `ix_pending_skip_trace_queued_frontier ON pending_skip_trace_rows
(trace_type, user_id, enqueued_at, id) WHERE status = 'queued'`, CREATE INDEX CONCURRENTLY in
autocommit, exactly 102's discipline: identity by the whole `pg_get_indexdef()` plus
`indisvalid`; an invalid or wrong-shaped same-named index on this table is dropped and rebuilt
(safe only under migrate.py's advisory lock); a same-named index on ANOTHER table aborts. Listed in
`CONCURRENT_INDEXES`; declared on the model. No CHECK, no trigger, no data change. Downgrade drops
it only if it sits on this table. Tests: shape, a wrong-shape same-named index rebuilt, replay
(downgrade 102 -> upgrade -> no-op upgrade), the autouse repair fixture pattern from 102's tests.

**ii-c-2: the keyset `allocate`.** Replaces "re-rank everything minus `considered`" with a
per-account FRONTIER:
- Round 0 finds the accounts with queued rows of this type (a loose index scan on the new index:
  recursive CTE, one probe per account), each with frontier = (-infinity).
- Each round: `unnest(:users, :after_at, :after_id, :per_account_limit) AS a` CROSS JOIN LATERAL
  (the dispatcher's eligibility select, unchanged predicates + watermark, `WHERE p.user_id =
  a.user_id AND (p.enqueued_at, p.id) > (a.after_at, a.after_id) ORDER BY p.enqueued_at, p.id
  LIMIT a.lim`), `row_number()` over each account's lateral output = rank; order `(rank,
  enqueued_at, id)`; LIMIT = the round's global limit. Per-account limit = `room * 2**r` (NULL
  room -> the global limit).
- Because rank 1 of every account precedes rank 2 of any, the rows returned for an account are a
  PREFIX of its candidates, so its frontier advances to its last RETURNED row (enqueued_at, id).
  Rows cut by the global LIMIT are not returned and not passed. An account whose lateral returned
  fewer than its limit is exhausted and leaves the account list.
- `considered` (the id-exclusion array) and `_REFILL_MAX_CONSIDERED` are removed: the frontier IS
  the exclusion and the statement no longer grows with the pass. The count stays in telemetry.
- Everything else in the pass is ii-b unchanged: lock only if still queued and of this type, the
  deliverability / submittable filters, the CUMULATIVE hold, `take_within_caps`, 12 rounds / 2 s,
  the in-lock spend read, READ COMMITTED.
- **In-flight read once per account per pass:** `_hold_answers_in_flight` takes an optional
  per-pass cache of each account's in-flight answer keys and queries only accounts not yet in
  it. Stale in the SAFE direction only: under the claim lock no other tick adds in-flight rows;
  an ingest finishing mid-pass can only remove one, so a cached key can only hold a row that
  could have gone (it goes next tick), never let a duplicate through. `_fresh_answers` is still
  read every round (keyed, cheap).
- **Gate:** the ii-b S3 script re-run at 117k queued / 15k held heads: every `allocate` round
  ≤ 250 ms and the refill ≤ 2 s; and at 10k and 35k no worse than ii-b. All ii-b tests pass
  unchanged except those that named `considered_limit`.

**Risks to check in the consult:** keyset ties (enqueued_at equal: id breaks them; the tuple
compare must match the ORDER BY exactly); a row whose `enqueued_at` changes mid-pass (VERIFIED
none: it is set only by the server default at insert; every other use in src/ and scripts/ reads it); rows inserted mid-pass BEHIND a frontier (the watermark again, V1); LIMIT
with an expression; accounts discovered only after round 0 (none: the watermark fixes the set).

### Codex pre-code consult on ii-c (2026-09-27): PLAN: REVISE, 2 P1 + 3 P2 + 2 P3, all accepted
Output: `<scratchpad 0ade294c>/codex_iic_consult_out.txt`. Confirmed: the prefix argument is correct
under ORDER BY (rank, enqueued_at, id) + a global LIMIT; the frontier reproduces ii-b's
`considered` exclusion; the in-flight cache is safe under current writers (new claims insert
'queued', skip_trace_claim.py:451-463; only the dispatcher moves rows to submitting/submitted).
SUPERSEDES the matching bullets above:
- **F1 (P1)** `allocate()` returns `(id, user_id, enqueued_at)` in order, and each account's
  frontier advances over EVERY returned row (SKIP LOCKED, withdrawn, unsubmittable, held alike),
  never only over survivors. Sentinel is typed and never NULL: `'-infinity'::timestamptz` and the
  all-zero uuid. The tuple compare is exactly `(p.enqueued_at, p.id) > (a.after_at, a.after_id)`,
  matching `ORDER BY p.enqueued_at, p.id` ASC.
- **F2 (P1)** Index column order is `(trace_type, user_id, enqueued_at, id) WHERE status =
  'queued'`: discovery fixes trace_type first, then skips across user_id; the lateral fixes both
  and walks (enqueued_at, id). The gate must SHOW the loose scan in EXPLAIN on the 117k workload.
- **F3 (P2)** A short lateral result does NOT retire an account (READ COMMITTED: a job can turn
  deliverable mid-pass). An account leaves the active set only when a later probe returns ZERO
  rows. Short results are telemetry only.
- **F4 (P2)** The `considered_limit` test is replaced by frontier tests: equal timestamps (id
  tie-break), a global-LIMIT cut (the cut rows come back next round), held heads, locked heads,
  post-allocation drops.
- **F5 (P3)** The in-flight cache keeps an explicit "accounts already read" set, so a cached EMPTY
  result is distinguishable from an unread account. Invariant, written into the code comment: any
  future writer (the 1b-2 action path) inserts only 'queued' rows through the claim protocol.
- **F6 (P2)** 103 keeps ALL of 102's discipline (CONCURRENTLY in autocommit, whole-indexdef
  identity + indisvalid, wrong-shape repair, cross-table collision abort, CONCURRENT_INDEXES,
  scoped downgrade). Deployment gate: ii-c-1 is merged, applied, and VERIFIED BY THE OBJECT in
  production before ii-c-2 deploys. Build time: prod holds ~941 pending rows (09-26), so the
  concurrent build is sub-second; lock_timeout bounds only the waits, as in 102.
- **F7 (P3)** `ix_pending_skip_trace_dispatch` stays (residual queued scans, e.g. the reconciler
  path at dispatcher.py:860-865). Dropping it would be its own measured migration.

### Codex consult round 2 on ii-c (2026-09-27): PLAN: REVISE, 1 P1 + 1 P2 + 1 P3
Output: `<scratchpad 0ade294c>/codex_iic_consult2_out.txt`. F1-F7 verified closed. Codex supplied the
discovery query it expects to be index-driven (a recursive CTE: `ORDER BY user_id LIMIT 1`, then
`user_id > u.user_id ... LIMIT 1` per step, with `status='queued' AND trace_type=:t AND
enqueued_at <= :watermark`), adopted verbatim; the gate EXPLAINs it AND the lateral.
- **H1 (P1) a row that turns eligible mid-pass can sit BEHIND the frontier.** Accepted as a
  wording fix; Codex's remedy (materialize the joined eligible set once per pass) REJECTED, with
  reason: the row is skipped for THIS PASS ONLY. Every pass restarts every frontier at
  -infinity, so it goes out next tick, which is exactly the outcome Codex's own remedy specifies
  ("rows becoming eligible after materialization wait for the next tick"). Materializing would
  also re-introduce a full scan of the eligible set on every pass, the cost ii-c exists to
  remove. F3 is reworded: a row that becomes eligible mid-pass is taken this pass if it is still
  ahead of its account's frontier, otherwise next tick; nothing is skipped beyond one pass, and
  nothing can exceed a cap.
- **H2 (P2) per-account look-ahead x active accounts is unbounded before the global LIMIT.**
  Accepted. Per-account lateral limit per round:
  `L = min(room_left * 2**r  (or unbounded), share * 2**r, global_left)` where
  `share = ceil(global_left / active_accounts)` and active_accounts counts only accounts with
  room. So one round produces at most about `global_left * 2**r` candidates in total, whatever
  the tenant count, and the lateral also stops early when an account runs out. Benchmark added to
  the gate: 15,000 accounts with queued rows, bounded candidate production per round, and no
  account starved (each with room gets its first row in round 0 when the batch can hold one per
  account; R5 otherwise).
- **H3 (P3) the in-flight cache relies on a comment about future writers.** Accepted as a 1b-2
  REQUIREMENT, recorded here and carried into 1b-2's plan: the action worker inserts only through
  `claim_skip_trace_rows` ('queued') under the shared claim protocol, and 1b-2 ships an
  integration test that no writer can create an in-flight row while a dispatcher pass holds the
  claim lock. ii-c's cache comment points at that requirement.

### Codex consult round 3 on ii-c (2026-09-27): H1 CLOSED; H2/H3 fixes adopted verbatim -> consult closed
Output: `<scratchpad 0ade294c>/codex_iic_consult3_out.txt`. Codex confirmed H1 (the one-pass skip is no
money loss, no permanent starvation, no cap breach; materializing adds a scan and no benefit).
Frontiers are PASS-LOCAL and never persisted (stated in the code).
- **H2 corrected.** The last term was `global_left`, which at global_left = 1 inspects only 12 rows
  per pass (a lead behind 13+ blocked rows could starve forever). Now, per round r:
  `round_limit = min(BATCH_ROW_LIMIT, global_left * 2**r)` (also the outer LIMIT, as in ii-b) and
  `L = min(room_left * 2**r (or unbounded), ceil(global_left / active_with_room) * 2**r, round_limit)`.
  The room * 4095 cutoff (room 1, no earlier deadline) is preserved. Bounds, documented separately:
  returned rows per round <= round_limit; lateral WORK is O((active_accounts + global_left) * 2**r).
  Fairness (rank 1 of every account with room before rank 2 of any) holds subject to the
  global-headroom rule (R5): when active accounts outnumber global_left the rank-1 layer is cut.
- **H3 carried to 1b-2, exactly:** the action worker calls `lock_job_for_claim()` then
  `claim_skip_trace_rows()`; no direct queue inserts, never a 'submitting'/'submitted' row; a
  two-session integration test proves the action path cannot create an in-flight row while
  `_CLAIM_LOCK_KEY` is held.

### ii-c-2 round 0: the watermark misled the planner (2026-09-27)
Round 0 took 1,172 ms because the walk did NOT use 103. The real plan: an Index Scan of
`ix_pending_skip_trace_dispatch (status, trace_type, enqueued_at)` with `user_id` as a FILTER,
64,680 rows removed per account x 50. Cause: the frontier compare gives the planner a derived
`enqueued_at >= acct.after_at` (unknown value); paired with the watermark `enqueued_at <= const`
it is a range with one unknown end, priced at a flat 0.5% (~585 rows for the whole queue).
Rounds >= 1 only looked fast because the old seed stored each account's rows contiguously in
time. (Adding `user_id` to the lateral's ORDER BY changed nothing: inside the lateral the
planner already drops it as fixed. Tried and reverted.)
- **Fix:** the watermark moves OUT of the lateral into `allocate()` (before `row_number()`).
  Post-watermark rows sort after every earlier row, so dropping them trims a TAIL of each
  account's walk and the prefix / frontier / retirement argument is unchanged (Codex: PLAN GO).
  `enable_bitmapscan=off` stays. Same dumped statement, same data: watermark inside 676 ms
  (bitmaps off) / 1,020 ms (on); outside 286 ms (on) / **1.2 ms (off, Index Scan on 103)**.
- **Also (profiling the 2 s refill):** `_in_flight_keys` read a blocked account's 15,000
  in-flight rows as whole ORM rows: 1,331 ms of the refill. Now it selects only the 7 key
  columns: 266-408 ms (Codex: safe, same keys).
- **Codex consult r2 (REVISE, all adopted):** B's budget is 500 ms per round (tenant scale,
  O(accounts) by design; A/C keep 250 ms); the loop checks for a full batch BEFORE the round
  and deadline limits (a filled batch was logged `refill_truncated reason=deadline`); the
  `allocate()` comment says psycopg2 still writes the arrays out element by element.
- **Gate (every EXPLAINed round ASSERTED: 103, no dispatch index, no bitmap, watermark not an
  index cond; each case contiguous AND interleaved):** worst allocate round / refill.
  A 117k/50/15k held: 113 / 1.31 s and 131 / 1.33 s; with 500k filler results over 20k done
  jobs: 118 ms / 1.45 s. C 35k/50/5k: 38 / 1.16 s and 36 / 0.99 s. B 15k accounts x 2: 382 and
  463 ms (296-372 without EXPLAIN), fairness PASS (5,000 distinct accounts, exactly the
  earliest first rows, R5). All refills within the deadline; A/C end at the documented 4,095
  cutoff (round_limit), by design.
- **Mutations (each caught):** frontier over survivors only; retire on a short result (new
  test); cache without the `read` set (new test); round_limits last term = global_left;
  watermark dropped (new test); full-batch check after the limits (new test).
- **Regression:** credit cap 106 + 103's 19, then the two skip-trace batches and
  plan_entitlement_audit: 818 passed, 0 failed.
- **Codex diff review r1: VERDICT GO, no P1.** Fixed anyway: [P2] the SET LOCAL / walk /
  RESET now run in a SAVEPOINT (a failing walk in an aborted transaction made the RESET fail
  and mask the real error; new test asserts the walk's own DataError and `enable_bitmapscan`
  back on); [P2] a test pins the per-account `round_limit` term; [P3] module headers name
  ii-c. Both new mutations caught (8 in all). Not added, with reason: a test for the
  watermark's place relative to the LIMIT (post-watermark rows are a tail, so any placement
  before the outer LIMIT gives the same prefix) and for the key columns (dropping one raises
  AttributeError in every in-flight test). After the fix: 592 skip-trace tests pass, gate A
  unchanged (113-138 ms, refill 1.1-1.35 s).

### ii-c TO BUILD
- [x] ii-c-1: migration 103 + models + env.py + tests; replay; merge; VERIFY THE INDEX IN PROD
- [x] ii-c-2: keyset allocate + frontier + in-flight cache + tests; gate at 117k/15k and 15k accounts
- [x] Codex diff review each to GO; quiet check before each merge (ii-c-2: #366 merged
      `2e839076` 2026-09-27 10:00Z after diff r1 GO + r2 GO + a rebase check GO; worker runs it,
      first tick OK on an empty queue)

## Phase 1b-1b-iii — the PAUSE STATE (PLAN, 2026-09-27, BEFORE Codex consult)

Branch `feat/lookup-1b1b-iii-pause-state` off `f80f79ce` (everything through #373 live).
Binding inputs: D3, 15-10, C8-C10, C13, and the one-line scope in "Revised split".

### Facts (read in code, 2026-09-27)
- The early global check (`dispatch_pending_skip_trace`, top) returns
  `{"skipped": "daily_cap"}` + ops alert. The in-lock pass `continue`s on `global_rows == 0`
  and only logs. An account at its cap is simply given no room by `round_limits()`; nothing
  records that it is paused. Nothing computes a resume time or writes Redis.
- `spent_credits()` reads `submitted_at >= now - 24h` over 102's index
  `(submitted_at) INCLUDE (user_id, trace_type)`. A row stops counting once
  `now - 24h > submitted_at`. Rows in the window are bounded by the global cap (prod 2000
  credits), so a per-tick read of them is cheap.
- A pass may need 1 credit (normal) or 2 (advanced): an account 1 credit under its cap is
  paused for advanced lookups but not for normal ones.
- The dispatcher runs on the shared `celery` queue beside the other beat tasks, so a tick can
  start late; the beat interval restarts in full on every deploy (39-50 s container gap).
  `scheduler.py` imports no settings today; `tests/test_beat_schedule.py` fails any plain
  interval >= 600 s.
- Redis in workers: `redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())`, as
  `ops_alerts.py` does. Tests have a local Redis (`conftest.redis_client`, local-only FLUSHDB).

### Design
1. **Resume time (15-10), in `skip_trace_capacity.py`:** `resume_times(db, now, caps)`, ONE
   read of the rows in the window ordered `(submitted_at, tracerfy_queue_id NULLS FIRST, id)`
   with a running credit sum (`_weight_sql`), globally and per account. For a scope with spend
   `S` and cap `C`, a lookup of cost `c` fits once `S - C + c` credits have left the window, so
   `resume_at(c)` = `submitted_at` of the first row whose running sum reaches `S - C + c`,
   + 24h + 1 s margin (the `>=` boundary). `None` when it fits now. Computed for `c = 1` and
   `c = 2`. Reported only for scopes where the advanced lookup does not fit
   (`S + 2 > C`, cap on).
2. **Redis contract (C8-C10, D3), NEW `src/workers/skip_trace_pause_state.py`:**
   - Key `bridgeleads:skip_trace:pause:v1`, one HASH. Fields: `published_at`, `fresh_until`
     (ISO UTC), `global` and one per paused `<user_id>`, each a JSON
     `{"normal_resume_at": iso|null, "advanced_resume_at": iso}`. No spend or cap numbers
     (customers see only when they resume).
   - Every account whose advanced lookup does not fit is published, with or without queued
     rows (the quote comes before any row).
   - Written EVERY tick (not only paused ones) as ONE `MULTI`: `DEL`, `HSET` all fields,
     `EXPIRE ttl`. So the heartbeat is refreshed each tick, and a field is gone on the first
     tick that no longer pauses it: no stale "paused" after resume. The empty-hash case still
     carries `published_at`/`fresh_until`, so "not paused" is distinguishable from "unknown".
   - `fresh_until = now + 2 * interval + GRACE` (GRACE 120 s: the deploy gap plus a slow tick
     with two 30 s POSTs). TTL = `max(2 * interval + GRACE, latest resume_at - now + GRACE)`,
     from the SETTING, never 600 hardcoded.
   - A reader `read_pause_state(r, user_id, now)` defines the API side of the contract
     (1c calls it): `HMGET key published_at fresh_until global <user_id>`, never `HGETALL`.
     Missing key, missing or past `fresh_until`, malformed JSON, or Redis error → `UNKNOWN`.
     Otherwise each resume time is compared with now (a past one reads as not paused).
     Returns `PAUSED(normal_resume_at, advanced_resume_at)` combining account and global
     (the later of the two per cost), `NOT_PAUSED`, or `UNKNOWN`.
   - Redis unreachable on publish: WARNING logged, the tick's result is unchanged, nothing
     raises. ADVISORY only: nothing reads it to decide spend (the in-lock DB read does).
3. **Where it runs (dispatcher):** the tick body becomes `_dispatch_tick()`;
   `dispatch_pending_skip_trace()` calls it, then `publish_pause_state()` in `finally`, in its
   OWN system session (not under the claim lock, which is not needed for an advisory read).
   So the early global-cap exit, `claim_locked`, every deferral and a raising tick all
   publish. Not when `SKIP_TRACE_ENABLED` is off or the token is missing: then the heartbeat
   goes stale and the API reads UNKNOWN, which is true.
4. **Beat interval (C13):** `SKIP_TRACE_DISPATCH_INTERVAL_SECONDS: int = 300`, validator
   `60 <= v < 600` (600+ must be a crontab: `landmine_beat_intervals_reset_on_every_deploy`).
   `scheduler.py` reads it for the `dispatch-pending-skip-trace` entry; the publisher reads
   the SAME setting for `fresh_until`/TTL. Default = today's 300, so deploying changes nothing.

### Split (5-file rule; the plan file counts)
- **iii-a (interval setting)**: `settings.py`, `.env.example` (reads of it are denied to
  Claude: the owner adds the line, or an append-only write if allowed), `scheduler.py`,
  `tests/test_beat_schedule.py`, this plan = 5.
- **iii-b (pause state)**: `skip_trace_capacity.py`, NEW `skip_trace_pause_state.py`,
  `skip_trace_dispatcher.py`, NEW `tests/test_skip_trace_pause_state.py`, this plan = 5.

### Tests (real PG + local Redis; Tracerfy via the `http://` rejection trick only)
- [ ] iii-a: the entry's interval equals the setting; the validator refuses 59 and 600;
      default 300 (deploy changes nothing).
- [ ] Resume math: normal-only; an advanced row straddling `S - C + c`; ties on
      `submitted_at` ordered by queue id then id; released rows (NULL) and rows past 24h ignored;
      INVARIANT against `spent_credits()` itself: at `resume_at(c)` the scope has room for `c`,
      1 s before it (margin removed) it does not.
- [ ] Per-account vs global: account paused with global free; global paused with the account
      free; both (reader returns the later time per cost); cap 0 → never published.
- [ ] Account at its cap with NO queued rows is published.
- [ ] Both directions: tick 1 paused → field present with resume times; age the rows out →
      tick 2: field gone, `published_at` newer.
- [ ] Heartbeat: missing key, `fresh_until` past, malformed field → UNKNOWN; fresh + no field
      → NOT_PAUSED; resume time already past → not paused.
- [ ] TTL follows the setting (interval 120 → `2*120+120`), and covers a resume 20 h out.
- [ ] Early global-cap exit and `claim_locked` both publish.
- [ ] Redis down (REDIS_URL to a closed local port): tick result identical, no raise; reader
      UNKNOWN.
- [ ] Reader never calls HGETALL (the call recorded on a real client subclass).
- [ ] Mutations: margin dropped; tie-break dropped; `DEL` dropped (stale field survives);
      TTL hardcoded 600; publish moved out of `finally` (early exit test fails); advanced
      threshold `+2` → `+1`.
- [ ] Regression batches: the ii-c list (credit cap + 103 tests, both skip-trace batches,
      plan_entitlement_audit) + `test_beat_schedule`.

### Questions for the consult
- Q1 publish every tick vs only when a scope is paused (chosen: every tick, for the heartbeat).
- Q2 reader in the worker module now (only tests use it until 1c) vs in 1c.
- Q3 GRACE 120 s on a shared `celery` queue: is a late tick better read as UNKNOWN (chosen)?
- Q4 the resume time does not include "next tick after": the UI says "after X".

### Codex pre-code consult r1 (2026-09-27): PLAN: REVISE, 2 P1 + 7 P2 + 2 P3
Output: `<scratchpad 4fe51d38>/codex_iii_consult_out.txt`. Q1, Q3, Q4 confirmed as chosen.
SUPERSEDES the matching bullets above:
- **E1 (P1) a cap of 1 can never buy an advanced lookup**, so `advanced_resume_at` has no
  value. ACCEPTED: the validator on `SKIP_TRACE_DAILY_CREDIT_CAP`,
  `SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP` and the legacy `SKIP_TRACE_DAILY_ROW_CAP` (read as
  credits) refuses 1: `0` (off) or `>= 2`. Prod values are 2000 / 500 (safe). In iii-a.
- **E2 (P1) publish race.** A `claim_locked` tick reads spend before the claiming tick commits,
  then publishes AFTER it and restores a stale view. ACCEPTED: a monotonic fence. The snapshot
  time is `clock_timestamp()` read in the publisher's session BEFORE the window query (READ
  COMMITTED: the query's snapshot is later, so it sees every commit before that time). The
  write is ONE Lua script (atomic): it replaces the hash only if the stored `snapshot_us` is
  absent or older, else it is a no-op, logged at DEBUG. Test: publish a newer snapshot, then an
  older one; the newer survives.
- **E3 (P2) `finally`.** ACCEPTED: the publisher's DB session is CLOSED before any Redis I/O,
  the whole publisher is best-effort (WARNING, never raises), and tests assert a tick's return
  value AND a tick's exception both pass through unchanged.
- **E4 (P2) integer TTL.** ACCEPTED: `ceil(...)`, minimum 1 s; `fresh_until` from the same
  `now` the snapshot used.
- **E5 (P2) the binding text conflicts.** ACCEPTED: this contract SUPERSEDES D3's key name
  (`skip_trace:daily_cap_paused`) and C8-C10's "HGET <own user_id>": the API reads a FIXED
  field list with `HMGET` (metadata, `global`, its own `<user_id>`), never the whole hash and
  never another tenant's field. Noted at D3 and C8-C10.
- **E6 (P2) disabled / no token leaves a fresh "paused".** ACCEPTED: those paths `DEL` the key
  (best-effort) so the reader returns UNKNOWN at once. Test: paused, then disabled.
- **E7 (P2) cost.** ACCEPTED, bounded by construction: no read at all when both caps are 0;
  the running sums run only for the GLOBAL scope (rows bounded by the global cap) and for the
  accounts `spent_credits()` already found at their threshold (each bounded by its cap), over
  102's `submitted_at` index with `user_id = ANY(:paused)`. One `now` for every window
  calculation. Gate: EXPLAIN (ANALYZE, BUFFERS) on a seeded window of 100k rows / 500 accounts
  with the account cap alone; add an index only if it fails (a migration = its own PR).
- **E8 (P2)** already the plan (iii-a).
- **E9 (P2) missing tests.** ACCEPTED, added below.
- **E10 (P3) import weight.** `src/workers/__init__.py` builds the Celery app, and the API
  (1c) will import the reader. ACCEPTED: the contract module moves to
  `src/utils/skip_trace_pause_state.py` and imports only the stdlib and settings: the
  writer takes the resume times and a Redis client as arguments (the dispatcher computes them
  with `skip_trace_capacity.resume_times()` and opens the client); the reader takes a client.
- **E11 (P3) the conftest Redis fixture lacks `redis_kwargs()` and never closes.** NARROWED:
  the new test file builds its own client with `settings.redis_kwargs()` and closes it;
  changing `tests/conftest.py` would be a 6th file. Logged as a follow-up.

**Revised iii-b files:** `skip_trace_capacity.py`, NEW `src/utils/skip_trace_pause_state.py`,
`skip_trace_dispatcher.py`, NEW `tests/test_skip_trace_pause_state.py`, this plan = 5.
**Tests added (E9):** overlapping-publish fence (older snapshot loses); cap 1 refused; paused
then disabled → UNKNOWN; TTL of a fractional duration rounds UP; malformed
`fresh_until` / `snapshot_us` / field JSON → UNKNOWN; the publisher's DB read failing → tick
result unchanged, WARNING logged; the client is built with `redis_kwargs()`.
**Mutations added:** fence compare flipped; `ceil` → `int`; disabled-path DEL dropped;
session closed after (not before) the Redis write is not mutation-testable, asserted by a
test that the session is closed when the client is called.

### Codex pre-code consult r2 (2026-09-27): PLAN: REVISE, 2 P1 + 3 P2 + 1 P3
Output: `<scratchpad 4fe51d38>/codex_iii_consult_r2_out.txt`. E3, E4, E8, E10, E11 confirmed.
SUPERSEDES E1, E2, E5, E6, E7:
- **G1 (P1) a clock is not a monotonic fence** (clock correction can move it back; equal
  microseconds can occur). ADOPTED: the fence is `pg_current_xact_id()` (xid8: unique per
  transaction, never reused, no migration), taken in the publisher's READ COMMITTED
  transaction BEFORE the window query. Why that closes the race: the claiming tick's publisher
  takes its xid after its claim committed; any publisher whose xid is larger took it after that
  commit too, so its later snapshot includes the claim; any smaller one loses the compare. Stored
  as a 20-digit zero-padded string, so the Lua compare is an exact string compare (no double
  rounding). Tests: a newer fence then an older one (older is a no-op); an equal fence is a
  no-op; two real publisher sessions interleaved around a committed claim.
- **G2 (P1) a bare DEL lets an older in-flight publisher recreate "paused".** ADOPTED: the
  disabled / no-token paths write a FENCED TOMBSTONE (the same Lua script, a hash with only
  `fence` and `state=disabled`, no `fresh_until`, TTL `2 * interval + GRACE`), so the reader
  returns UNKNOWN at once and an older publisher cannot overwrite it. Taking the fence costs
  those paths one short system session. (A key that EXPIRES can still be recreated by a
  publisher older than its TTL, i.e. one stalled for 2 intervals + grace between its DB read and
  its Redis write: accepted, and the next tick's publish replaces it.)
- **G3 (P2) E1 is WITHDRAWN**, not narrowed. Refusing a cap of 1 is a boot-breaking config
  change, and ten live tests in `test_skip_trace_credit_cap.py` exercise `account_cap=1` at
  runtime. Taken instead: r1's other option. When a scope's cap is below the cost of an
  advanced lookup, `advanced_resume_at` is the literal `"never"`; the reader returns it as
  `NEVER`; the TTL ignores it. Test: a cap of 1 publishes `"never"`. iii-a no longer touches the
  cap validators.
- **G4 (P2) cost claim narrowed.** `spent_credits()` groups the whole 24h window, and 102's
  index leads on `submitted_at` only. With the global cap on (prod: 2000), the window holds at
  most ~2000 rows. With ONLY an account cap, the publisher's read costs what the in-lock
  `spent_credits()` read already costs every pass today (live since #364). The EXPLAIN gate
  (100k-row window, 500 accounts, account cap only) is MANDATORY before the diff review; an
  account-leading index, if it fails, is its own migration PR.
- **G5 (P2) the field list is explicit:** `HMGET key published_at fresh_until fence global
  <user_id>`. `fence` must parse as 20 digits and `fresh_until` as an ISO UTC time, or the
  reader returns UNKNOWN.
- **G6 (P3)** every test deletes the key and closes its client in a fixture finalizer.

**iii-a files now:** `settings.py` (the interval setting only), `.env.example`, `scheduler.py`,
`tests/test_beat_schedule.py`, this plan = 5. iii-b unchanged (5).

### Codex pre-code consult r3 (2026-09-27): PLAN: REVISE, 1 P1 + 3 P2 + 2 P3, all adopted
Output: `<scratchpad 4fe51d38>/codex_iii_consult_r3_out.txt`. The xid fence argument and the
exact 20-digit string compare CONFIRMED; G2's residual accepted. Adopted: H1 the fence
transaction is verified (below); H2 a tombstone-expiry test; H3 `NEVER` fully specified; H4 the
gate runs the REAL resume query with thresholds; H5 Lua treats a malformed stored fence as
absent; H6 one normative contract (below) replaces the scattered bullets.

### FINAL contract and build list (normative; supersedes Design 1-3, E*, G* where they differ)
**Resume time** — `skip_trace_capacity.resume_times(db, now, caps)`:
- [ ] Scopes: `global` when the global cap is on; each account `spent_credits()` found with
      `S + 2 > C` when the account cap is on. No read at all when both caps are 0.
- [ ] Per scope and cost `c` in (1, 2): `None` when `S + c <= C` (fits now); the literal
      `NEVER` when `1 <= C < c` (a cap of 1 never admits an advanced lookup); else the
      `submitted_at` of the first row, ordered `(submitted_at, tracerfy_queue_id NULLS FIRST,
      id)`, whose running credit sum reaches `S - C + c`, + 24h + 1 s.
- [ ] One `now` for every window calculation in the tick's publish.

**Fence transaction** (H1) — the publisher's own system session:
- [ ] First statement `SET TRANSACTION ISOLATION LEVEL READ COMMITTED`; then ONE select of
      `pg_current_xact_id()`, `pg_is_in_recovery()`, `current_setting('transaction_read_only')`.
      In recovery or read-only → no publish, WARNING (best-effort; nothing else changes).
- [ ] Then the window read; then the transaction is ended and the session CLOSED; only then
      any Redis I/O.

**Redis hash** `bridgeleads:skip_trace:pause:v1`, written by ONE Lua script:
- [ ] Fields: `fence` (20-digit zero-padded xid), `published_at`, `fresh_until` (ISO UTC),
      `global` and one per published `<user_id>`: JSON
      `{"normal_resume_at": iso|null, "advanced_resume_at": iso|"never"|null}`. No spend or cap
      numbers.
- [ ] Script: if the stored `fence` is 20 ASCII digits and `>=` the incoming one → no-op
      (DEBUG log); otherwise (absent, malformed, or older) → `DEL`, `HSET` all fields,
      `EXPIRE ttl`. Atomic.
- [ ] Every tick publishes (heartbeat), even with nothing paused.
- [ ] `fresh_until = now + 2 * interval + GRACE` (GRACE = 120 s). TTL =
      `ceil(max(2 * interval + GRACE, latest finite resume_at - now + GRACE))` seconds,
      minimum 1; `NEVER` is excluded.
- [ ] Disabled / no-token paths publish a TOMBSTONE through the same script: `fence` +
      `state=disabled`, no `fresh_until`, TTL `2 * interval + GRACE`.

**Reader** — `read_pause_state(r, user_id, now)` in `src/utils/skip_trace_pause_state.py`
(stdlib + settings only; the API calls it in 1c):
- [ ] `HMGET key published_at fresh_until fence global <user_id>`; never `HGETALL`, never
      another tenant's field.
- [ ] UNKNOWN when: Redis errors; the key or `fence` or `fresh_until` is missing; `fence` is
      not 20 digits; `fresh_until` is not ISO UTC or is `<= now`; a field is malformed JSON.
- [ ] Otherwise, per cost, combine account and global: `NEVER` dominates, else the later
      finite time; a time `<= now` counts as `None`. Both costs `None` → `NOT_PAUSED`; else
      `PAUSED(normal_resume_at, advanced_resume_at)`. UI meaning (1c): `NEVER` = "advanced
      lookups are unavailable under the current limit", normal ones may still run.

**Dispatcher:**
- [ ] The tick body becomes `_dispatch_tick()`; `dispatch_pending_skip_trace()` returns its
      result and, in `finally`, calls the best-effort publisher. A tick's return value and a
      tick's exception pass through unchanged; a publisher failure is only a WARNING.

**Beat interval (iii-a):**
- [x] `SKIP_TRACE_DISPATCH_INTERVAL_SECONDS: int = 300`, validator `60 <= v < 600`;
      `scheduler.py` uses it; the publisher uses the SAME setting.

**Gate (H4, before the diff review):** EXPLAIN (ANALYZE, BUFFERS) of the real
`resume_times()` query on a seeded window of 100k rows over 500 accounts, for: account cap
only; global + account caps; all 500 accounts paused (worst case). Pass = under 100 ms
execution, warm, each. A fail = an account-leading index in its own migration PR first.

**Tests** (`tests/test_skip_trace_pause_state.py`, real PG + local Redis, a client built
with `settings.redis_kwargs()`, key deleted and client closed in a finalizer):
- [ ] Resume math: normal-only; an advanced row straddling `S - C + c`; ties by queue id
      then id; NULL `submitted_at` and rows past 24h ignored; INVARIANT against
      `spent_credits()` itself at `resume_at(c)` (room) and 1 s before it (no room).
- [ ] Per-account vs global: each alone, both (the later time per cost), a cap of 1 on each
      (`NEVER`, and `NEVER` + a finite time → `NEVER`), both caps 0 (no read, nothing paused).
- [ ] An account at its cap with NO queued rows is published.
- [ ] Both directions: paused tick → field present; rows aged out → next tick: field gone,
      `published_at` newer.
- [ ] Fence: newer then older (older no-ops); equal (no-op); malformed stored fence
      (replaced); two real sessions interleaved around a committed claim (the post-claim view
      survives); tombstone then an older publish (tombstone survives); tombstone EXPIRED then
      the stale publish recreates, then the next fenced publish wins (H2).
- [ ] Fence transaction: a read-only transaction → no publish, WARNING.
- [ ] Heartbeat / reader: missing key, missing or malformed `fence` / `fresh_until`,
      `fresh_until` past, malformed JSON → UNKNOWN; fresh + no own field → NOT_PAUSED; a past
      resume time → not paused; HMGET only (a real client subclass records the calls).
- [ ] TTL follows the setting (120 → 360 s), rounds a fractional duration UP, covers a resume
      20 h out, ignores `NEVER`.
- [ ] Dispatcher: the early global-cap exit, `claim_locked` and a normal tick all publish;
      disabled and no-token write the tombstone; a tick's return and a raised exception pass
      through; the publisher's DB read failing → tick result unchanged + WARNING; the DB
      session is closed when the Redis client is first called; Redis on a closed local port →
      tick unchanged, reader UNKNOWN.
- [x] iii-a (`tests/test_beat_schedule.py`): the entry equals the setting; 59 and 600 refused;
      default 300.
- [ ] Mutations, each caught: margin dropped; tie-break dropped; `+ 2` threshold → `+ 1`;
      `NEVER` branch dropped; fence compare flipped; malformed-fence-as-absent dropped;
      tombstone dropped (plain DEL); `ceil` → `int`; TTL hardcoded 600; publish moved out of
      `finally`; the read-only check dropped.
- [ ] Regression: credit cap + 103 tests, both skip-trace batches, plan_entitlement_audit,
      test_beat_schedule.

### Codex pre-code consult r4 (2026-09-27): PLAN: REVISE, 1 P1 + 1 P2, both adopted
Output: `<scratchpad 4fe51d38>/codex_iii_consult_r4_out.txt`. Amends the FINAL contract:
- **I1 (P1) an account cap of 1 was invisible for an account with no spend** (it is not in
  `spent_credits()`, so it had no field and read NOT_PAUSED, yet its advanced lookups can never
  run). Fix: a new ALWAYS-published field `account_default`, the state of any account without
  its own field: `{"normal_resume_at": null, "advanced_resume_at": "never"}` when
  `1 <= account cap < 2`, else both `null`. The reader uses its own field if present, else
  `account_default`. Test: a zero-spend account with no queued rows under an account cap of 1
  reads `PAUSED(None, NEVER)`.
- **I2 (P2) strict reader.** `global` and `account_default` are ALWAYS published (both `null`s
  when that cap is off or not binding). HMGET list becomes `published_at fresh_until fence
  global account_default <user_id>`. Each JSON value must be an object with EXACTLY the two
  keys; `normal_resume_at` is `null` or an ISO UTC time; `advanced_resume_at` is `null`,
  `"never"` or an ISO UTC time. Any violation, or a missing `global` / `account_default` →
  UNKNOWN. Tests: missing `global`; missing `account_default`; extra key; wrong type; a
  non-UTC or unparseable time; `"never"` in `normal_resume_at`. Mutation: shape validation
  dropped.

### Codex pre-code consult r5 (2026-09-27): PLAN: REVISE, 1 P2, adopted. I1, I2 CLOSED.
- **J1 (P2)** `published_at` is required and validated like `fresh_until`: missing or not an
  ISO UTC time → UNKNOWN. Tests: missing; malformed.

### Codex pre-code consult r6 (2026-09-27): **PLAN: GO**. J1 closed, no new findings.
Output: `<scratchpad 4fe51d38>/codex_iii_consult_r6_out.txt`. Next: the OWNER checks this plan
before any code (iii-a first, then iii-b). **Owner approved the plan, the `.env.example`
append and the 100 ms gate (2026-09-27).**

### iii-a BUILT (2026-09-27), before the Codex diff review
- `SKIP_TRACE_DISPATCH_INTERVAL_SECONDS` (default 300, validator 60..599) in `settings.py`; the
  beat entry reads it; `.env.example` line appended (commented). 4 new tests in
  `test_beat_schedule.py` (the entry rebuilt at 120 reads 120; default 300; 59/60/599/600).
- Mutations, each caught by its own test: the entry hardcoded back to 300; the bound widened to
  `<= 600`.
- Regression: `test_beat_schedule` + all 27 test files importing the scheduler +
  `test_skip_trace_credit_cap`: 904 passed, 0 failed. ruff clean.
- Rebased on `ae351c4e` (#375: no file overlap). **Codex diff review r1 (three-dot): VERDICT
  GO, no findings** (`<scratchpad 4fe51d38>/codex_iiia_review_out.txt`). The PR also carries
  the previous session's handoff doc (docs only): 6 files, disclosed.
- **MERGED #376 `bca09eff`, LIVE 2026-09-28:** beat/worker/api on the commit; beat booted
  06:34:29Z and sent `dispatch-pending-skip-trace` at 06:39:29Z (= +300 s); the tick succeeded.

### iii-b first build (2026-09-28, branch `feat/lookup-1b1b-iii-b-pause-publish`, local WIP)
Built to the FINAL contract: `src/utils/skip_trace_pause_state.py` (hash, Lua fence, tombstone,
strict reader), `resume_times()` (window functions), the dispatcher wrapper
(`_dispatch_tick` + `_publish_pause_state` in `finally`, `_pause_fence`), 52 tests, 15
mutations each caught by its own test (tie-break not run: an EQUIVALENT mutant, since only the
threshold row's `submitted_at` reaches the answer and tied rows share it). Found while
building: `test_skip_trace_credit_cap`'s datetime-shadow guard inspects the task, whose body
moved to `_dispatch_tick`; the new test file re-points it (no 6th file).

**H4 GATE: FAIL** (`<scratchpad 4fe51d38>/gate_iiib.py`, 100,043 rows / 500 accounts): account
cap only 193 ms; global + account 315 ms; worst 404 ms. Every statement sorts the whole window
and spills at work_mem 4MB. Query-only variants (`gate_iiib_variants.py`, identical answers)
at best 84 ms global + 112 ms account (64MB): still ~200 ms. **Codex (`codex_iiib_gate_out.txt`):
RECOMMEND A, the plan's own route**:
- The hard caps make steady state `S <= C`, so a paused scope's threshold is within its first
  2 credits, but a cap enabled or LOWERED over existing spend gives `S > C`: the query must
  walk to the dynamic threshold then, not assume 2 rows.
- 102 cannot seek to an account (`user_id` is INCLUDE only): per-account walks need an
  account-leading index. Hence **migration 105 first, its own PR**, then iii-b rebuilt on
  EARLY-STOP queries: totals in one aggregate; per paused scope, walk rows in `submitted_at`
  order only until the running sum reaches `S - C + c` (global over 102, accounts over 105).
- Ordering by `submitted_at` alone is output-identical (ties), so 105 need not carry
  `tracerfy_queue_id`/`id`.
- READ COMMITTED staleness between the two statements is advisory-only (the dispatcher
  re-checks spend under the claim lock); prefer ONE statement for totals + prefixes.

**Prototype of the rebuilt query (`gate_iiib_v2.py`, candidate index created on the TEST DB and
dropped; seed VACUUM ANALYZEd like production's autovacuum, 500 accounts x 200 rows = 100k, every
account exactly at its cap; answers IDENTICAL to the window-function reference in every case):**

| case (warm, best of 3) | without 105 | with 105 |
|---|---|---|
| reachable: all 500 accounts at cap + global at cap | 154 ms | 112 ms |
| reachable: account cap only, all 500 at cap | 189 ms | 86 ms |
| UNREACHABLE: caps lowered to 10 / 5000 over that spend | 5,790 ms | 322 ms |
| (baseline) the LIVE in-lock `spent_credits()` at the same scale | 60 ms | |

EXPLAIN with 105: the 500 per-account walks cost ~12 ms together (Index Only Scan on the
candidate, 3 rows each), the global walk 0.3 ms (102). The rest is the per-account TOTALS, one
pass over the window: the same work the live `spent_credits()` already does inside the claim lock
on EVERY pass. (Without VACUUM the baseline read 347 ms: a fresh table has no visibility map.)
So 105 is required (reachable 154-189 -> 86-112 ms; lowered caps 5.8 s -> 0.32 s), and the
H4 bar of 100 ms is NOT reachable at a 100k window by any query that must total the window.
A 100k window is itself only possible with the global cap off or >= 100k credits: while the
global cap is on, the window holds at most ~G rows (prod G = 2000).

## Phase 1b-1b-iii-c — migration 105 (PLAN, 2026-09-28, BEFORE Codex consult; lands BEFORE iii-b)

**Why:** 102 cannot seek to an account (`user_id` is INCLUDE only); the rebuilt resume query
walks each paused account's oldest rows and stops at its threshold. Measured above.

**The index:** `ix_pending_skip_trace_account_spent` on `public.pending_skip_trace_rows
(user_id, submitted_at) INCLUDE (trace_type) WHERE submitted_at IS NOT NULL`. Keys only
(user_id, submitted_at): ties on submitted_at cannot change the answer (proved above), so
`tracerfy_queue_id`/`id` are not carried. INCLUDE trace_type = the weight, index-only.

**Migration 105**, 103's discipline exactly: CREATE INDEX CONCURRENTLY in an autocommit block,
`lock_timeout 5s`, STRUCTURAL identity from the catalogs (valid/live/ready, non-unique,
non-exclusion, btree, no expressions, key cols `[user_id, submitted_at]`, INCLUDE cols
`[trace_type]` (indnatts 3, indnkeyatts 2), ascending, default opclasses, column collations,
normalized predicate `submitted_atISNOTNULL`); an invalid or wrong-shaped index of the name on
THIS table is dropped and rebuilt (serialized by `scripts/migrate.py`'s advisory lock); one on
another table, or one backing a constraint, ABORTS and is never dropped. Downgrade drops only
ours. No data change. Backward safe: nothing requires it; iii-b deploys only after it is verified
in production by the object.

**Files (5):** `alembic/versions/105_pending_skip_trace_account_spent.py`, `src/db/models.py`
(the Index on the model), `alembic/env.py` (`CONCURRENT_INDEXES`), NEW
`tests/test_pending_skip_trace_account_spent_index.py` (mirroring 103's: built, right shape,
idempotent re-run, invalid rebuilt, wrong shape rebuilt, other-table collision aborts,
constraint-backed aborts, downgrade drops only ours, predicate normalization), this plan.
**Then:** merge (quiet check first: merge = deploy, migrates on boot), verify the index in prod
by the object (`pgdef.py`-style read: indisvalid + shape).

**iii-b rebuilt on 105 (after it is live):** `resume_times()` becomes ONE statement (one
snapshot): per-account totals (CTE) -> paused accounts (`spent + 2 > A`) -> per account a LATERAL
walk over 105 in `submitted_at` order with a running sum, `LIMIT 2` once `run >= spent - A + 1`;
the global scope likewise over 102. Python applies `scope_resume()` (at1 = first row, at2 = first
row with `run >= need + 1`). Covers `S > C` (a lowered cap) by walking to the dynamic threshold.
Everything else in iii-b (contract module, fence, reader, dispatcher wiring, tests) is unchanged.

**PROPOSED gate (replaces H4's 100 ms, needs the OWNER):** on the 100k / 500-account seed,
VACUUM ANALYZEd: (1) every REACHABLE case (each scope at or under its cap) <= the live
`spent_credits()` at the same scale + 60 ms (today: 60 + 60 = 120 ms; measured 86-112 ms);
(2) the UNREACHABLE lowered-cap case <= 1 s (measured 322 ms), since it is transient and
advisory, outside the lock, once per 300 s; (3) the EXPLAIN shows 105 for the account walks
and 102 for the global walk. The publisher is never on the claim path, so its cost bounds only
DB load, not dispatch.

### Codex pre-code consult r1 on 105 + the rebuilt query (2026-09-28): PLAN: REVISE, 3 P1 + 5 P2, all adopted
Output: `<scratchpad 4fe51d38>/codex_105_consult_out.txt`. Amends the 105 plan and "iii-b rebuilt":
- **K1 (P1) INCLUDE breaks 103's shape check** (`indnatts == indnkeyatts == len(keys)` and all
  of `indkey` compared with the keys). 105's check splits `indkey` by ordinal: keys
  (`ord <= indnkeyatts`) must be `[user_id, submitted_at]`, INCLUDE (`ord > indnkeyatts`) must be
  `[trace_type]`, `indnatts = 3`, `indnkeyatts = 2`; ascending / default-opclass / collation
  checks apply to the KEY columns only (`indoption`, `indclass`, `indcollation` cover keys only).
- **K2 (P1) the global row vanished when the walk returns nothing** (spent 0 under a cap of 1:
  `CROSS JOIN LATERAL` emits nothing, so the scope reads unbound instead of `(None, NEVER)`).
  The global total row is UNCONDITIONAL, joined `LEFT JOIN LATERAL`; the walk runs only when a
  threshold exists (`cap >= 1`, `spent + 2 > cap`), and Python's `scope_resume()` handles
  `need <= 0` and `cap < cost` without rows. Accounts need no change: an account with no spend
  is not in the totals and reads `account_default`.
- **K3 (P1) returned order is semantic**: `ORDER BY submitted_at` directly before `LIMIT 2` in
  each lateral, and `run` returned. Tests: threshold exactly on a row; crossed by an advanced
  row; `need <= 0`; `S > C` (lowered cap) with the threshold beyond the first two credits, for an
  account AND for the global scope; cap 1 -> NEVER; tied timestamps; an empty scope.
- **K4 (P2) one snapshot = one statement.** `resume_times()` executes exactly ONE statement
  (totals, both scopes' walks); the fence precedes it; the publisher never calls
  `spent_credits()`. Test: both caps on -> exactly one SELECT touching `pending_skip_trace_rows`.
- **K5 (P2) a stronger gate** (replaces "PROPOSED gate" above, still needs the OWNER): 20 timed
  runs per case, report p50/p95/max; the FIRST run after seeding reported separately as the
  cold-ish figure (the OS cache cannot be dropped locally: said plainly); production `work_mem`
  read from prod first (read-only `SHOW work_mem`) and SET LOCAL to it in the gate; cases: global
  cap on and off, uniform and skewed account sizes, and a concurrent writer inserting claimed
  rows during the runs. PASS: reachable p95 <= live `spent_credits()` p95 + 60 ms at the same
  scale; lowered-cap max <= 1 s; the plan shows `Index Only Scan` on 105 for the account walks
  and on 102 for the global walk, no temp spill, and each walk's actual rows <= 3.
  PLUS an absolute ceiling in code: the publisher's transaction runs `SET LOCAL
  statement_timeout = '5s'`, so it can never become a runaway load (a timeout is a WARNING,
  like any publisher failure). Test: the timeout is set.
- **K6 (P2)** 105 merges and is verified in production by catalog identity (valid, ready, live,
  table, btree, key list, INCLUDE list, predicate) BEFORE iii-b merges.
- **K7 (P2) 105 near-miss matrix**, each rebuilt (or aborted where 103 aborts): exact shape
  accepted as-is (no rebuild on replay); missing INCLUDE; wrong INCLUDE column; an extra INCLUDE
  column; keys reversed; a descending key; a non-default key opclass; a non-default key
  collation; an expression index; a different predicate; INVALID; unique; other-table collision
  (abort); constraint-backed (abort); downgrade drops only ours.
- **K8 (P2)** the gate asserts 105 and 102 in the actual plans (K5), and the iii-b tests include
  the lowered-cap walks (K3).

### Codex consult r2 on 105 (2026-09-28): PLAN: REVISE, 2 P2, both adopted. K1-K4, K7, K8 closed.
Output: `<scratchpad 4fe51d38>/codex_105_consult_r2_out.txt`.
- **L1 (P2) the timeout's place:** `SET LOCAL statement_timeout = '5s'` runs immediately after
  a SUCCESSFUL fence, in the same transaction, before the single resume statement (not before
  the fence, not in a new transaction). Test: in the publisher's transaction, `SHOW
  statement_timeout` reads `5s` when the resume statement runs, and the setting does not
  outlive the transaction.
- **L2 (P2) production verification = the WHOLE K1 predicate**, not a subset: the migration's
  own `_is_right_shape()` (valid, ready, live, non-unique, non-exclusion, btree, no expressions,
  keys `[user_id, submitted_at]`, INCLUDE `[trace_type]`, `indnatts = 3`, `indnkeyatts = 2`,
  ascending keys, default key opclasses, key column collations, normalized predicate) run
  read-only against production after the deploy, imported from the migration file so the check
  and the build can never drift.

### Codex consult r3 on 105 (2026-09-28): **PLAN: GO**, no findings (L1, L2 closed).
Output: `<scratchpad 4fe51d38>/codex_105_consult_r3_out.txt`. Next: the OWNER decides the gate
(K5 replaces H4's flat 100 ms), then 105 is built on its own branch from main.
**OWNER DECISION (2026-09-28): the RELATIVE gate (K5) replaces H4's flat 100 ms.** 105 is built
first on `feat/lookup-1b1b-iii-c-account-spent-index` (from `9ee0fac9`); iii-b then rebased
onto it and rebuilt on the early-stop query.

### 105 TO BUILD
- [x] Migration 105 (K1 shape check split into keys / INCLUDE; 103's drop/abort rules; L2's
      `_is_right_shape()` importable for the prod check)
- [x] The Index on the model; `CONCURRENT_INDEXES` in `alembic/env.py`
- [x] Tests: the K7 near-miss matrix, replay, downgrade
- [ ] Codex diff review to GO; quiet check; merge; verify in prod by the whole K1 predicate (L2)

**105 BUILT (2026-09-28), before the Codex diff review.** 28 tests in
`tests/test_pending_skip_trace_account_spent_index.py`: shape + rendering; model and
`CONCURRENT_INDEXES`; a replay keeps the same index (same oid, no rebuild); 11 near misses each
rebuilt (no INCLUDE, wrong INCLUDE, extra INCLUDE, trace_type as a KEY, keys reversed, a
descending key, an expression key, the opposite predicate, no predicate, unique, hash);
predicate normalization both ways; EXCLUDE constraint under the name aborts and is left; other
table's index refused, not dropped; upgrade/downgrade/replay converge; downgrade leaves another
table's same-named index alone; INVALID corpses rebuilt. Test DB migrated 104 -> 105 through
`scripts/migrate.py`; one head.
- Found while building: 103's INVALID test uses a corpse that is ALSO the wrong shape, so it
  never isolated `indisvalid` (a mutation dropping it survived here too). New test: the build is
  cancelled in its LAST wait (a REPEATABLE READ snapshot held by another session), which leaves
  an index that is READY, of exactly our shape, and INVALID. (Two dead ends first: an idle READ
  COMMITTED reader holds no snapshot, so the build never waited; a ROW EXCLUSIVE holder stops the
  build before `indisready`, so `indisready` alone rejected it and the mutation still survived.)
  103's test has the same blind spot: logged as a follow-up, not fixed here (6th file).
- Regression: 105 + 103 + 102 index suites, db_safety, credit cap, dispatcher claim, spend
  ledger, claim, reconciliation: 317 passed, 0 failed. ruff clean.
- Mutations (9): INCLUDE list unchecked, key/INCLUDE split dropped, descending accepted, unique
  accepted, predicate unchecked, invalid accepted, constraint-backed dropped, other-table
  collision not refused: each caught by its own test. `indnkeyatts` unchecked SURVIVES as an
  equivalent mutant: the key list is read by `ord <= indnkeyatts`, so the key-list and
  INCLUDE-list equalities already pin it; kept as a belt.
- Not testable with these types, said plainly: the non-default operator class and collation
  checks (uuid and timestamptz have no alternative btree opclass in core PostgreSQL and are not
  collatable). They stay as defence.

**Codex diff review r1 on 105 (2026-09-28): NO-GO, 2 P2, both fixed.**
(`<scratchpad 4fe51d38>/codex_105_review_out.txt`)
- P2 `lock_timeout` does not end a CONCURRENTLY build's wait for older transactions: one
  stalled transaction on the table would hang the migration and the boot. Fixed:
  `statement_timeout = 60s` around the build and the downgrade's drop (`_bounded()` /
  `_unbounded()`); a timeout leaves an INVALID index that the next run rebuilds. New test: a held
  REPEATABLE READ snapshot, the timeout shortened to 1 s; `upgrade()` ENDS with a statement
  timeout (not a hang, bounded join), and the next run converges. Mutation (timeout not set):
  caught. 102 and 103 have the same gap: logged as a follow-up (they are built and live).
- P2 the INVALID-of-the-right-shape test depended on a 500 ms timer. Now deterministic: the
  build runs on its own connection in a thread; the test polls `pg_stat_progress_create_index`
  until that backend is `waiting for old snapshots` with the index ready and invalid, then
  `pg_cancel_backend`s it; every wait bounded; cleanup unconditional. Mutation (invalid
  accepted): caught by it. Suite 29 passed, 3 more repeat runs 29/29.

**Codex diff review r2 on 105 (three-dot, rebased on `9ee0fac9`): VERDICT GO, no findings.**
(`<scratchpad 4fe51d38>/codex_105_review_r2_out.txt`; 60 s is below migrate.py's 900 s lock
budget; the phase name is right on PG 16 and 17.)
**MERGED #379 `29afc82e`, LIVE 2026-09-28.** api/worker/beat on the commit; migrate.py applied it
(one replica waited on the lock); verified in production by the migration's OWN
`_is_right_shape()` (`bl-checks/p105.py`): valid/ready/live, keys `[user_id, submitted_at]`,
INCLUDE `[trace_type]`, 3/2 attributes, definition byte-identical to `_INDEX_DEF`, covering 1,002
spent rows. (`alembic_version` reads EMPTY to the worker role: RLS on, no policy. Not a fault.)

### iii-b REBUILT on 105 (2026-09-28, branch `feat/lookup-1b1b-iii-b-pause-publish` rebased on `29afc82e`)
- `resume_times()` is ONE statement (K4): a `totals` CTE; `paused` accounts (`spent + 2 > A`),
  each with a LATERAL walk over 105 in `submitted_at` order, `ORDER BY` then `LIMIT 2` once
  `run >= spent - A + 1` (K3); the global total `g`, ALWAYS one row, `LEFT JOIN LATERAL` walk
  over 102 (K2). Python sorts each walk by its running sum (strictly increasing; SQL does not
  promise UNION ALL order) and applies `scope_resume()`. Built in Core on `_weight_sql()`: one
  copy of the weights, no string SQL.
- `_pause_fence()` sets `SET LOCAL statement_timeout = '5s'` after a successful fence (L1).
- Tests: 56 (4 new: a lowered cap walks past the first two credits, account and global; an
  empty global scope still reports, incl. cap 1 -> NEVER; one statement; the 5 s ceiling is in
  force during the statement and gone after the transaction).
- Mutations (20): 19 caught, each by its own test (new: LIMIT 2 -> 1; global walk inner-joined;
  the walk newest-first; the 5 s ceiling unset; the +2 threshold in the `paused` CTE). The
  Python sort SURVIVES as expected: PostgreSQL returns each lateral's rows in order here, but
  SQL does not promise it, so the sort stays.
- **K5 GATE: PASS** (`gate_iiib_k5.py`, production planner settings read 2026-09-28 and SET
  LOCAL in every timed session: work_mem 2184kB, random_page_cost 1.1, effective_cache_size
  384MB, jit off; prod is PG 17.6, local PG 16.14):

  | seed | baseline `spent_credits()` p95 | budget | reachable p95 (global on / off / + writer) | lowered caps max |
  |---|---|---|---|---|
  | uniform: 500 x 266 credits, every account AT its cap | 80.6 ms | 140.6 ms | 94.2 / 79.4 / 99.7 ms | 367.7 ms |
  | skewed: 133..400 credits | 34.8 ms | 94.8 ms | 44.4 / 35.7 / 47.2 ms | 332.4 ms |

  Plan (both seeds): account walks = Index Only Scan on 105, 3 rows per loop, one loop per
  paused account (500 / 3); global walk = Index Only Scan on 102, 3 rows; no temp spill. The
  totals are one ordered pass over 105 (GroupAggregate, no hash). First runs (cold-ish) 38-144
  ms. The writer case committed 200-328 claims during its runs.
  A gate bug fixed on the way: the EXPLAIN reused caps read before the writer case added spend,
  which put the global scope OVER its cap (239-893 rows walked: the lowered-cap state, not the
  reachable one); caps are now re-read at EXPLAIN time. The walk-row check also counted the
  totals' full pass over 105 as a walk; walks are now the looped scans.
- **Regression** (foreground, 3 batches after a background run was reaped for low memory):
  every `test_skip_trace_*` suite, `test_tracerfy_ingest`, the 102/103/105 index suites,
  `test_beat_schedule`, `test_plan_entitlement_audit`: 285 + 148 + 391 = **824 passed, 0
  failed**. ruff clean.

**Codex diff review r1 on iii-b (three-dot, rebased on `405ba52c` #380: no overlap; its
conftest change only ADDS a `connectors` fixture): NO-GO, 2 P2, both fixed.**
(`<scratchpad 4fe51d38>/codex_iiib_review_out.txt`)
- P2 the fence and its checks were two SELECTs; H1 says one. I had split them believing
  `pg_current_xact_id()` raises in a read-only transaction: TESTED, it does not (it returned an
  id with `transaction_read_only = on`). Now one SELECT; the docstring no longer claims a
  read-only transaction "cannot hold a usable id" (the real reason: the ordering argument holds
  only on the primary, in the dispatcher's read-write path). New test: the fence's statements
  are exactly SET TRANSACTION, ONE SELECT with all three, SET LOCAL statement_timeout.
- P2 `\d{20}` accepts other scripts' digits. Now `[0-9]{20}` (Lua's `%d` is byte-wise ASCII
  already). New reader case: 20 Arabic-Indic digits -> UNKNOWN.
- Mutations: each fix reverted -> caught by its new test. Suite 58 passed.

**Codex diff review r2 on iii-b: NO-GO, 1 P2 + 1 P3, both fixed; r1's two fixes VERIFIED.**
(`<scratchpad 4fe51d38>/codex_iiib_review_r2_out.txt`)
- P2 a client built without `decode_responses` returns bytes, and bytes that are not UTF-8
  raised `UnicodeDecodeError` OUT of the reader instead of UNKNOWN. `_text()` now turns
  undecodable bytes (and any non-bytes, non-text value) into `_MalformedError`, and the
  decoding runs inside the reader's `try`. New test on a real bytes-mode client: invalid UTF-8
  -> UNKNOWN; valid bytes -> NOT_PAUSED. Mutation (decode back outside the try): caught.
- P3 naive and non-UTC `published_at` added to the malformed cases. Suite 61 passed.

**Codex diff review r3 on iii-b (three-dot, rebased on `5689139d` #381, docs only): VERDICT GO,
no findings.** (`<scratchpad 4fe51d38>/codex_iiib_review_r3_out.txt`)

## Phase 1b-1c — PLANNER + QUOTE (PLAN, 2026-09-28, BEFORE Codex consult)

Branch `feat/lookup-1b1c-planner-quote` off `c0b09b7a` (all of 1b-1b live). Binding inputs:
the 2026-09-20 planner/quote bullets (Phase 1b, "`plan_contact_lookup`" and "`POST
/jobs/{job_id}/contact-lookups/quote`"), 15-3, 15-14, 15-17, 16-3, 16-8, D2, D3 + the FINAL
pause contract, and "Revised split" item 3. **Touches no money and writes no database row:**
the quote reads `results`/`jobs`/`users` and writes ONE Redis key.

### Facts (read in code, 2026-09-28)
- **The tab** = `GET /jobs/{id}/results` (`routes/jobs.py:498-539`): `job_id`, `user_id`,
  `category_condition(category)` (`new` = `is_duplicate IS FALSE`; `already_delivered` =
  prior-run duplicates), `tax_cap_condition(today)`, `actionable_condition()`, and nothing
  unless `_run_delivered(job)` (`status == 'done'`, `:83`). View filters (search, tax,
  owner-location, dialer) narrow the view; the quote ignores them and covers the TAB.
  Both categories are inside `skip_trace_eligible_condition()` (`results_category.py:53`).
- **The scrape enqueue's gates, in order** (`enrich.py:2271-2431`): kill switch, token,
  per-config `skip_trace_enabled` (the SPEC, not this code, has the manual action bypass
  ONLY this one: N1), plan != starter;
  SQL: `property_address IS NOT NULL`, actionable, `skip_trace_status='not_attempted'`,
  skip-trace-eligible; charged-unanswered (reads `pending_skip_trace_rows`: WORKER ONLY);
  `street_is_placeholder`; settled complaint (only when `record_type='code_violation'`,
  `king_cv_sources.is_settled`, source-keyed); then `build_pending_row_payload()`
  (`skip_trace.py:1054`), the authority: None for no/placeholder-literal address, foreign
  address, code-violation owner not proven, the ATIP policy
  (`code_violation_skip_trace_allowed`, reads `settings.PIERCE_CV_OWNER_SKIP_TRACE_ENABLED`),
  a non-personal party name, no city/state. Its `trace_type` is `normal` (1 credit) or
  `advanced` (2). All of these import cleanly from the API (no `src.workers`).
- `results.skip_trace_status` values: `not_attempted`, `queued`, `submitted`, `hit`, `miss`,
  `errored`. Quotable is EXACTLY `not_attempted` (15-3). `errored` is ambiguous (pre-submit
  rejection OR provider-accepted-unmatched, BILLED) and `last_trace_outcome` is NULL on every
  row until 1b-2 (16-3), so `errored` is never quotable.
- **API Redis clients are ASYNC** (`redis.asyncio`, `rate_limit.py:51-58`,
  `auth_hardening.py:17-24`). `read_pause_state()` calls `r.hmget()` SYNCHRONOUSLY: handed an
  async client, `vals` is a coroutine and the generator unpack raises `TypeError`, which is
  OUTSIDE its `_MalformedError` handler = a 500. The quote must give it a sync client.
- **The action row needs a price snapshot** (`unit_price_cents`, `currency`,
  `pricing_version`, all NOT NULL, `models.py:1845-1847`) and **no canonical price exists**:
  `routes/billing.py:323-329` hardcodes 0.05 (agency) / 0.08 (pro, business) (15-17).
- **Included lookups left**: quota `settings.SKIP_TRACE_BUNDLED_QUOTAS[plan]` (pro 250,
  business 1000, agency 2000); used `users.skip_trace_used_this_month`, which counts as 0
  once the entitlement window has rolled (`skip_trace_usage.py:140-152`: `period_start` NULL
  or `< effective_window().start`). `/billing/skip-trace-usage` does NOT apply that roll rule
  (reports the stale counter until the daily rollover job runs).
- **No API code reads `SKIP_TRACE_ENABLED` or `TRACERFY_API_TOKEN` today.** Whether the
  Railway `api` service even carries them is unknown (API and worker env are separate: 15-14).
- API-initial dispositions (the 101 trigger): `quoted` + the five `excluded_*`. `already_
  answered` / `in_progress_elsewhere` are worker verdicts: the API can never write them.
- `Result.created_at` exists (server default), `id` is a UUID: order `(created_at, id)`.
- OpenAPI: `schema/openapi.json` is CI-checked (`export_openapi.py --check`); regen only in
  `.venv-schema` (memory `reference_openapi_regen_env_matters`).

### Design
1. **Planner, NEW `src/api/contact_lookup_planner.py`** (imports no `src.workers`; the 1b-2
   worker imports it):
   - `PlannerPolicy(pierce_cv_owner_skip_trace_enabled: bool)` + `PLANNER_VERSION = 1`,
     pinned into the quote (15-14). `policy_from_settings()` builds it once per request.
   - `classify(row, record_type, policy) -> Verdict`, PURE (no DB, no I/O; the only settings
     read is inside `build_pending_row_payload`, see caveat). First match wins, in the
     enqueue's order:
     1. status `queued`/`submitted` -> `in_progress`; `hit`/`miss` -> `already_answered`;
        `errored` -> `previously_attempted`; any other value != `not_attempted` ->
        `previously_attempted` (fail closed).
     2. `property_address` NULL/blank -> `no_address` (`excluded_no_address`).
     3. `street_is_placeholder` or the `(enrichment unavailable)` literal -> `placeholder`
        (`excluded_placeholder_address`).
     4. `record_type == 'code_violation'` and `is_settled(source, status)` ->
        `settled_code_violation`.
     5. Tacoma ATIP-sourced owner and `not policy.pierce_cv_owner_skip_trace_enabled` ->
        `atip` (`excluded_atip_policy`), computed from the PINNED flag.
     6. `build_pending_row_payload(row) is None` -> `not_traceable`.
     7. Else `quotable`, with the payload's `trace_type`.
     Caveat (stated in code): step 6 reads the PROCESS flag; if it is stricter than the pinned
     one the row lands in `not_traceable`, never the reverse, so the planner can only exclude
     more, never quote more.
   - `plan_contact_lookup(rows, record_type, policy, cap=2000) -> Plan`: over rows already in
     `(created_at, id)` order: `quoted_ids` (the first `cap` quotable), `advanced_count` (of
     those), per-bucket counts over the COVERED window, `covered_count`, and `truncated`
     (a quotable row exists beyond the cap). The covered window ends at the row that filled
     the cap, so a re-quote covers the same leads, and leads bought by an earlier action fall
     to `in_progress`/`already_answered` and the scan moves past them (no stuck window).
   - `tab_rows(db, job, user_id, category, after, limit)`: the tab predicate above +
     `skip_trace_eligible_condition()` (belt), keyset on `(created_at, id)`, loading only the
     columns `classify` needs. The quote walks it in chunks of 500 and stops when the cap
     fills, or at a hard **scan ceiling of 10,000 rows** (then `truncated` with the reason
     `scan_limit`), so one request's work is bounded whatever the tab size.
   - The 1b-2 worker reuses `classify` on the quoted ids (by id, not by tab) and adds only
     what it alone can see (charged-unanswered, cache hits, the queue): it can only LOWER
     the count (16-8).
2. **Price + allowance, NEW `src/config/lookup_pricing.py`** (15-17): integer cents per plan
   (`pro`/`business` 8, `agency` 5, others none), `CURRENCY = "usd"`,
   `PRICING_VERSION = "2026-06"`; `included_lookups_remaining(user, now)` applying the SAME roll
   rule as `report_lookups_for_user`. `routes/billing.py` `/skip-trace-usage` reads the price
   from here (same numbers, one source). Its stale-`used` behaviour is logged, not changed.
3. **Quote endpoint `POST /jobs/{job_id}/contact-lookups/quote`** {category}, in
   `routes/jobs.py` (the route family it belongs to; keeps the PR inside 5 files). Order:
   1. `rate_limit(zone="export")`: the quote decrypts nothing but walks up to 10k rows and
      writes Redis; fail-closed zone, 20/min per user.
   2. Job by `(id, user_id)` else 404. `status != 'done'` -> 409 `run_not_finished`.
   3. `normalize_plan(user.plan) not in SKIP_TRACE_ADDON_PLANS` -> the structured 402
      (`skip_trace_violation(plan)`, the batches shape).
   4. `not SKIP_TRACE_ENABLED or not TRACERFY_API_TOKEN` -> 503
      `{code: "contact_lookups_unavailable"}` (friendly copy, no detail).
   5. Plan the tab (Design 1).
   6. Pause state: `read_pause_state()` UNCHANGED, called through `run_in_threadpool` with a
      module-level SYNC client, `redis.from_url(REDIS_URL, **redis_kwargs(),
      socket_timeout=0.5, socket_connect_timeout=0.5)`. Any failure is already UNKNOWN.
   7. Store the quote: key `bridgeleads:contact_lookup:quote:v1:<quote_id>`, `SET NX EX 600`,
      JSON `{v:1, user_id, job_id, category, quoted_ids, advanced_count, max_new_lookups,
      counts, covered_count, truncated, planner_version, policy, unit_price_cents, currency,
      pricing_version, included_remaining_at_quote, created_at}`. `quote_id =
      secrets.token_urlsafe(32)`. Only the quoted ids are stored (the immutable set, 16-8);
      exclusions are counts. Redis failure -> 503, no quote returned (a quote nobody can
      confirm must not be shown).
   8. 200 `ContactLookupQuote`: `quote_id`, `expires_at`, `category`, `max_new_lookups`,
      `advanced_count`, `covered_count`, `truncated` + `truncated_reason` (`cap` |
      `scan_limit` | null), `excluded` {no_address, placeholder, settled_code_violation,
      atip, not_traceable}, `already_answered`, `in_progress`, `previously_attempted`,
      `included_lookups_remaining`, `unit_price_cents`, `currency`, `pause` {`status`
      (`paused`|`not_paused`|`unknown`), `normal_resume_at`, `advanced_resume_at` (ISO |
      `"never"` | null)}. No lead ids, names or addresses in the response.
   The quote is **non-binding** and an UPPER bound: `max_new_lookups` can only go down at
   confirm and in the worker (reuse, answers found meanwhile); the schema field docs say so.

### Split (5-file rule; the plan file counts)
- **1b-1c-i planner + pricing (no endpoint):** NEW `src/api/contact_lookup_planner.py`, NEW
  `src/config/lookup_pricing.py`, `src/api/routes/billing.py`, NEW
  `tests/test_contact_lookup_planner.py`, this plan = 5.
- **1b-1c-ii quote endpoint:** `src/api/routes/jobs.py`, `src/api/schemas.py`,
  `schema/openapi.json`, NEW `tests/test_contact_lookup_quote.py`, this plan = 5.

### Tests (real PG + local Redis; no mocks)
- [ ] i: `classify` table test, one row per branch, incl. each `build_pending_row_payload`
      None reason (foreign, CV owner unproven, non-personal name, no locality) -> `not_traceable`.
- [ ] i: **PARITY with the real scrape enqueue**: seed one job with every branch, run
      `_enqueue_skip_trace_rows` (token set, `http://` Tracerfy never reached: it only
      enqueues), assert the set of result ids it queued == the planner's `quoted_ids`, and
      each pending row's `trace_type` == the planner's. Then flip the ATIP flag both ways.
- [ ] i: order + cap: 2,001 quotable rows -> 2,000 quoted, `truncated`, the SAME ids on a
      re-plan; rows already answered at the front do not stop the window moving.
- [ ] i: `included_lookups_remaining`: under quota, over quota (0), rolled window (full
      quota), NULL `period_start`, starter (0).
- [ ] i: billing `/skip-trace-usage` returns the same rates as before (0.05 / 0.08 / None).
- [ ] ii: foreign job 404 (and no Redis key written); not done 409; starter 402 (shape);
      kill switch off 503; token missing 503.
- [ ] ii: happy path: counts, advanced count, stored payload shape + TTL (<= 600), key
      bound to user/job/category, no ids in the response body.
- [ ] ii: pause via the REAL `publish()`: paused (account and global), `"never"`,
      not paused, UNKNOWN (no key; tombstone; Redis on a closed port -> still 200).
- [ ] ii: quote store failing (closed port for the store client) -> 503, nothing returned.
- [ ] ii: the tab query runs AS `bridgeleads_app` (`_become`-style, provisioned in-
      transaction, memory `landmine_ci_has_no_provisioned_roles`): no queue-table read.
- [ ] ii: `in_progress`/`hit`/`miss`/`errored` rows are never in `quoted_ids`.
- [ ] Mutations, each caught: the `errored` branch dropped; the placeholder branch dropped;
      the settled branch dropped; ATIP computed from the process flag instead of the pinned
      one; the cap off by one; the order tie-break dropped; `SET NX` -> plain SET; the
      threadpool/sync-client swapped for the async client (500); the kill-switch check dropped.
- [ ] Regression: both skip-trace batches, `test_contact_lookup_schema`,
      `test_skip_trace_pause_state`, billing tests, `plan_entitlement_audit`.

### Questions for the consult
- Q1 store only `quoted_ids` (exclusions as counts) vs also the excluded ids so confirm can
  write `excluded_*` verdicts (the 2026-09-20 text). Chosen: quoted only; the static
  exclusions are not part of the purchase and storing them makes the payload unbounded.
- Q2 cap semantics: 2,000 QUOTED ids with a covered window (chosen) vs 2,000 covered rows
  (the literal 2026-09-20 text; it can freeze on a front of answered leads).
- Q3 the sync client via `run_in_threadpool` (chosen, keeps `read_pause_state` the only
  reader) vs an async twin of the reader.
- Q4 the kill-switch/token gate needs the API service env: see owner item O1.
- Q5 scan ceiling 10,000 and zone `export`: right bounds?

### Owner items (asked before code)
- **O1** Does the Railway `api` service carry `SKIP_TRACE_ENABLED` and `TRACERFY_API_TOKEN`?
  If not, the quote 503s forever. Options: add them to `api` (presence check only), or the
  quote reads the kill switch and trusts the pause state for the rest.
- **O2** Confirm the live Stripe metered rates are 8¢ (pro, business) and 5¢ (agency), the
  numbers `PRICING_VERSION = "2026-06"` names.
- **O3** A read-only prod count (no PII): the largest tab (rows per job and category) and its
  not-attempted-with-address count, to confirm the 10,000 scan ceiling.

### Codex pre-code consult r1 (2026-09-28): PLAN: REVISE, 6 P1 + 10 P2 + 1 P3
Prompt/output: `<scratchpad dea35045>/codex_1b1c_consult_r1{,_out}.txt`. Each finding was checked
in code. SUPERSEDES the matching Design/Test bullets above:
- **N1 (P1) REJECTED, wording fixed.** "Manual mode bypasses only the per-config toggle" is
  NOT in the enqueue; it is the 2026-09-20 spec's DECISION (worker bullet, "manual mode
  bypasses the per-scraper toggle `config.skip_trace_enabled` ONLY, because the user just
  asked for these lookups explicitly"). The doc addresses it, so the doc wins. The Fact is
  relabelled as spec, not code.
- **N2 (P1) -> owner O4.** The quote covers the unfiltered tab while the page may show a
  filtered view. Either the quote takes the view filters, or 1c's copy says "every lead on
  this tab (filters do not apply)".
- **N3 (P1) ADOPTED.** The enqueue's worker-only reads (charged-unanswered, the cache) can
  drop a row the planner quotes: `quoted_ids` is an UPPER bound, stated in the schema docs and
  the plan. Parity test: EQUALITY on a seed with an empty cache and no `unmatched` rows, then
  SUBSET (queued ⊂ quoted) with a valid cache hit and a charged-unanswered row seeded.
- **N4 (P2) ADOPTED.** The enqueue re-reads its rows under the job lock
  (`enrich.py:2541-2570`); the quote is a snapshot. 1b-2 test: confirm/worker only ever
  REMOVE ids (added to "Carried into 1b-2").
- **N5 (P2) ADOPTED, simplified.** In the quote, the pinned policy IS the API process's flag
  read once per request, so step 5 and step 6 agree there; a mismatch exists only in the
  1b-2 worker, where 15-14 allows the stricter current policy. Test: `classify` with a policy
  that disagrees with the process flag only ever excludes MORE, never quotes more.
- **N6 (P1) ADOPTED (Q1 reversed; the 2026-09-20 spec agrees).** The payload stores the
  excluded ids per static bucket (the five `excluded_*` reasons), bounded by the scan ceiling,
  so the 1b-2 confirm can write their verdicts. See N10 for the size bound.
- **N7 (P3)** Q2 confirmed (2,000 quoted ids, covered window).
- **N8 (P2) ADOPTED.** Pause read: `asyncio.wait_for(run_in_threadpool(read_pause_state, ...),
  1.0)`; a timeout, pool error or client-construction error -> UNKNOWN.
- **N9 (P1) -> owner O1.** The spec requires the token gate at the quote (friendly 503);
  Codex: provision both vars on `api`, fail closed if absent, never substitute the pause state.
- **N10 (P2) ADOPTED.** New zone `lookup_quote` = 10/min per user (the `export` fallback is
  per-process, not fail-closed as I wrote). The scan runs under `SET LOCAL statement_timeout =
  '5s'`. Size bound: one LIVE quote per `(user, job, category)`: key
  `bridgeleads:contact_lookup:quote:v1:<user_id>:<job_id>:<category>` holding the payload
  incl. `quote_id`; a new quote REPLACES it (the old `quote_id` then reads `quote_expired`,
  and the dialog re-quotes). Worst case per user = rate x TTL x ceiling x 37 B.
- **N11 (P2) ADOPTED.** ONE module-level SYNC client (`socket_timeout` and
  `socket_connect_timeout` 0.5 s) for both the pause read and the quote store, each call via
  `run_in_threadpool` under `wait_for`. Store failure or timeout -> 503, no quote returned.
- **N12 (P1) REJECTED.** `previously_attempted`, `in_progress` and `already_answered` are
  REPORTED COUNTS, never written: 16-8 says a lead in progress at quote time "is not in the set
  at all", and the API may only create the six API-initial dispositions. The durable set is
  `quoted` + the five `excluded_*`. No migration.
- **N13 (P2) ADOPTED.** The payload adds `expires_at`, `truncated_reason` and the excluded ids.
  `record_type` is NOT needed: the settled check becomes source-keyed and unconditional
  (`is_settled` returns False for any non-code-violation source, so this equals the enqueue on
  a code-violation job and can only exclude MORE elsewhere), which also removes the planner's
  dependency on a `ScraperConfig` that may have been deleted.
- **N14 (P2) CARRIED to 1b-2:** confirm matches `user_id`, `job_id`, `category`, `v`, expiry and
  `quote_id` from the stored payload, re-fetches the job by `(id, user_id)`, and never accepts a
  client-supplied id.
- **N15 (P2) ADOPTED.** Pricing and `/skip-trace-usage` both use `normalize_plan`; currency is
  stored as ISO 4217 `"USD"`.
- **N16 (P2) ADOPTED.** Tests added: mailing-only (property NULL) row; charged-unanswered and
  cache-hit rows (N3); policy mismatch (N5); a superseded quote; expiry; pause and store
  timeouts on a blackholed port; scan ceiling exactly 10,000 / 10,001 rows; one mutation per
  enqueue predicate the planner mirrors.
- **N17 (P2)** The split holds (N12: no migration).

### OWNER DECISIONS (2026-09-28)
- **O1:** the owner adds `SKIP_TRACE_ENABLED` and `TRACERFY_API_TOKEN` to the Railway `api`
  service. The quote fails closed (503) when either is absent. Before the ii merge, verify
  PRESENCE on `api` (booleans only, never values).
- **O4:** the quote covers the WHOLE tab; view filters never apply. 1c copy says so.
- **O2, O3 and the handoff's `railway ssh` HMGET check:** approved, read-only (results below).
- **Journal:** the 1b-1b-iii `docs/BUILD_JOURNAL.md` entry lands in the 1b-1c-i PR.
  **Revised i files:** NEW `src/api/contact_lookup_planner.py`, NEW
  `src/config/lookup_pricing.py`, NEW `tests/test_contact_lookup_planner.py`,
  `docs/BUILD_JOURNAL.md`, this plan = 5. `routes/billing.py` is NOT touched: switching
  `/skip-trace-usage` to the canonical price (and its stale-`used` roll rule) is a logged
  follow-up; until then a test asserts `lookup_pricing`'s rates equal what
  `/skip-trace-usage` returns for every plan, so the two sources cannot drift silently.

### Prod checks (read-only, 2026-09-28 ~11:16Z; scripts in `C:/Users/Windows/bl-checks/`)
- **Pause hash LIVE** (`railway ssh --service worker`, one HMGET): `published_at`
  11:11:38Z, `fresh_until` +720 s (= 2 x 300 + 120), fence `00000000000000161183`, `global`
  and `account_default` both `{null, null}`, 5 fields (no paused account), no `state`, TTL 444 s.
  Handoff open item 1 CLOSED.
- **O1:** `api` AND `worker` both carry `SKIP_TRACE_ENABLED` (truthy) and `TRACERFY_API_TOKEN`
  (set). No owner action; re-verify presence before the ii merge.
- **O2 (`stripe_lookup_rates.py`, `railway run --service api`, live key, GET only):** every
  `STRIPE_PRICE_SKIP_TRACE_*` is active, `usd`, `per_unit`, metered: Pro 8, Business overage 8,
  Agency overage 5 (cents), monthly and annual alike. `PRICING_VERSION = "2026-06"` names
  exactly these.
- **O3 (`tab_sizing.py`, tax cap not applied = upper bounds):** `already_delivered`: 83 tabs,
  max 16,965 rows (median 47), 3 over 10k, max not-attempted-with-address 9,571; `new`: 86
  tabs, max 16,549 (median 13), 1 over 10k, max candidates 10,657.

### Redesign from O3: the 10,000-row scan would STALL a large tab (P1, mine)
A 10,000 ceiling over ALL tab rows breaks the covered-window promise on the largest tabs: after
four 2,000-lead purchases, ~8,000 bought rows (now `in_progress`/`already_answered`) sit at the
front of `(created_at, id)`, and each later quote spends its ceiling re-reading them until it
finds nothing. Fix (supersedes Design 1's `tab_rows` walk, N6 and N10's size bound):
- **Tab-wide counts in ONE SQL aggregate** over the tab: `in_progress`, `already_answered`,
  `previously_attempted`, `no_address` (property NULL/blank/placeholder literal), and
  `candidates` (= `not_attempted` with a property address). These buckets need no Python.
- **The window walks CANDIDATES only** (SQL prefilter `skip_trace_status = 'not_attempted'`
  AND property address present), keyset `(created_at, id)`, chunks of 500; Python classifies
  placeholder / settled / atip / not_traceable / quotable; stops at 2,000 quotable or the
  ceiling. Bought leads leave the candidate set, so the window always moves forward.
- **Ceiling 20,000 candidates** (above the largest prod tab, 16,965 rows): a safety bound,
  `truncated_reason = 'scan_limit'`, not a normal path. `SET LOCAL statement_timeout = '5s'`.
- **Payload stays ~2,000 ids**: `quoted_ids` + `window_end` (the last `(created_at, id)`
  classified) + counts, NOT the excluded ids. The 1b-2 confirm writes `excluded_*` verdicts by
  RE-CLASSIFYING the window `(start, window_end]` at confirm time: exclusions are
  informational, and a row excluded at quote but quotable at confirm simply gets no row
  (confirm may exclude, never add: 16-8). `quoted_ids` alone is immutable. Worst-case payload
  ~80 KB; with one live quote per `(user, job, category)` and 10/min, per-user Redis is
  bounded by the number of distinct tabs quoted in 10 minutes.
- `excluded_no_address` is reported as a tab-wide COUNT and written by nobody in 1b-1c.

### Codex pre-code consult r2 (2026-09-28): PLAN: REVISE, 1 P1 + 4 P2 + 3 P3, all adopted
Output: `<scratchpad dea35045>/codex_1b1c_consult_r2_out.txt`. N1 and N12 rejections
ACCEPTED; keyset `(created_at, id)` confirmed a total order, served by the existing
`(job_id, user_id, is_duplicate, created_at)` index; N13 confirmed. Adopted:
- **P1 (#2) SQL address buckets are not `classify()`.** `btrim` is not Python `.strip()`
  (tabs, newlines, Unicode whitespace), the padded `(enrichment unavailable)` literal is a
  `placeholder` not `no_address`, and independent aggregates double-count past status
  precedence. FIX (simpler than a proven-equivalent SQL trim): SQL splits on the EXACT
  `skip_trace_status` string ONLY; every address decision stays in `classify()`. The window
  walks ALL `not_attempted` rows, and the largest prod tab (16,965) fits the 20,000 ceiling,
  so permanent no-address rows cannot stall a window today.
- **#3** confirm-time invariant, stated and carried to 1b-2 (below). The audit contract for
  exclusions is CONFIRM-TIME state; the quoted set is exactly what the customer saw.
- **#6** one storage model: one live quote per `(user, job, category)`. Rate zone: the
  existing `export` (20/min per user; on a Redis error a per-process limiter applies, it is
  NOT globally fail-closed). A new `lookup_quote` zone would be a 6th file in ii; logged.
- **#7** currency is `"USD"`. The quote uses `normalize_plan` exactly like the spend path's
  gate (`enrich.py:2285`); `/skip-trace-usage`'s raw `.lower()` is pre-existing, display-only,
  and joins the billing.py follow-up.
- **#8** the tests are rewritten in the FINAL list below.

### FINAL 1b-1c contract and build list (normative; supersedes Design, N*, the redesign and r2 where they differ)
**Planner** — NEW `src/api/contact_lookup_planner.py` (no `src.workers` import; 1b-2 imports it):
- [x] `PLANNER_VERSION = 1`; `PlannerPolicy(pierce_cv_owner_skip_trace_enabled)`;
      `policy_from_settings()` read ONCE per request.
- [x] `classify(row, policy) -> Verdict`, pure, first match wins:
      1. status `queued`/`submitted` -> `in_progress`; `hit`/`miss` -> `already_answered`;
         any other status except `not_attempted` -> `previously_attempted` (incl. `errored`).
      2. `(property_address or "").strip()` empty -> `no_address`.
      3. that stripped value == `(enrichment unavailable)` or `street_is_placeholder()` ->
         `placeholder`.
      4. `king_cv_sources.is_settled(ed.source, ed.status)` (unconditional, source-keyed) ->
         `settled_code_violation`.
      5. Tacoma ATIP-sourced owner and not `policy.pierce_cv_owner_skip_trace_enabled` ->
         `atip`.
      6. `build_pending_row_payload(row) is None` -> `not_traceable`.
      7. else `quotable(trace_type)`.
- [x] `plan_window(rows, policy, cap=2000) -> Window`: rows in `(created_at, id)` order;
      `quoted_ids` (first `cap` quotable), `advanced_count`, counts for `no_address`,
      `placeholder`, `settled_code_violation`, `atip`, `not_traceable`, `examined`,
      `window_end` (last key examined), `stopped` (`cap` | `scan_limit` | None).
- [x] Async DB helpers (API only): `tab_status_counts(db, job_id, user_id, category)` = ONE
      `GROUP BY skip_trace_status` over the tab predicate (category + actionable + tax cap +
      skip-trace-eligible, exactly as `GET /results`); `iter_not_attempted(...)` = the same
      predicate + `skip_trace_status = 'not_attempted'`, keyset `(created_at, id)`, chunks of
      500, the columns `classify` needs only. Both under `SET LOCAL statement_timeout = '5s'`.
- [x] Ceiling 20,000 examined rows (`stopped = 'scan_limit'`, a safety bound).

**Pricing** — NEW `src/config/lookup_pricing.py`:
- [x] `UNIT_PRICE_CENTS = {"pro": 8, "business": 8, "agency": 5}` (live Stripe, O2),
      `CURRENCY = "USD"`, `PRICING_VERSION = "2026-06"`; `unit_price_cents(plan)` via
      `normalize_plan`, None when not offered.
- [x] `included_lookups_remaining(user, now)`: `max(0, quota - used)`, with `used = 0` when
      `skip_trace_period_start` is NULL or before `effective_window(user, now).start` (the
      `report_lookups_for_user` rule, `skip_trace_usage.py:140-152`).

**Quote endpoint** — `POST /jobs/{job_id}/contact-lookups/quote` {category} in `routes/jobs.py`:
- [x] `rate_limit(zone="export", identifier=user.id)`; job by `(id, user_id)` else 404;
      `status != 'done'` -> 409 `run_not_finished`; plan not in `SKIP_TRACE_ADDON_PLANS`
      (normalized) -> the structured 402; kill switch off or token empty -> 503
      `contact_lookups_unavailable`.
- [x] Status counts, then the window, as above.
- [x] ONE module-level SYNC Redis client (`redis_kwargs()`, `socket_timeout` and
      `socket_connect_timeout` 0.5 s); every call through `run_in_threadpool` under
      `asyncio.wait_for(..., 1.0)`.
- [x] Pause: `read_pause_state()` unchanged; timeout / pool / client error -> UNKNOWN.
- [x] Store: key `bridgeleads:contact_lookup:quote:v1:<user_id>:<job_id>:<category>`, plain
      `SET ... EX 600` (REPLACES the tab's previous quote), JSON `{v: 1, quote_id, user_id,
      job_id, category, quoted_ids, advanced_count, counts, window_end, stopped,
      planner_version, policy, unit_price_cents, currency, pricing_version,
      included_remaining_at_quote, created_at, expires_at}`. `quote_id =
      secrets.token_urlsafe(32)`. Failure or timeout -> 503, no quote returned.
- [x] 200 `ContactLookupQuote` (schemas.py): `quote_id`, `expires_at`, `category`,
      `max_new_lookups`, `advanced_count`, `examined`, `truncated` + `truncated_reason`,
      `excluded` {no_address, placeholder, settled_code_violation, atip, not_traceable},
      `already_answered`, `in_progress`, `previously_attempted` (tab-wide), `remaining`
      (not-attempted rows past `window_end`), `included_lookups_remaining`,
      `unit_price_cents`, `currency`, `pause` {status, normal_resume_at, advanced_resume_at}.
      Field docs: an UPPER bound, non-binding, the whole tab (filters never apply, O4). No
      lead ids, names or addresses.

**Carried into 1b-2 (from this consult):** confirm receives `quote_id` + category, loads the
tab's key, compares `quote_id` (a superseded one is `quote_expired`), consumes it atomically,
checks `user_id`/`job_id`/`v`/expiry, re-fetches the job by `(id, user_id)`, and never takes a
client id. `quoted` rows come ONLY from `quoted_ids`; re-classifying `(start, window_end]`
writes only `excluded_*` rows; a row inside the window that is newly inserted or newly
quotable is never quoted (mutation-tested). Worker/confirm only ever REMOVE ids (N4).

**Tests** — i: `tests/test_contact_lookup_planner.py`; ii: `tests/test_contact_lookup_quote.py`
(real PG + local Redis, a client with `redis_kwargs()`, keys deleted in a finalizer):
- [x] i `classify`: one row per branch and per `build_pending_row_payload` None reason; address
      edge cases NULL, `''`, spaces, tab/newline, Unicode whitespace (NBSP), padded placeholder
      literal; status precedence over address; settled on a code-violation AND a non-CV source.
- [x] i PARITY with the real `_enqueue_skip_trace_rows`: EQUALITY of queued ids and each
      `trace_type` vs `quoted_ids` on a clean seed; SUBSET with a valid cache hit and a
      charged-unanswered row seeded; ATIP flag both ways; a policy disagreeing with the process
      flag only ever excludes MORE.
- [x] i window: 2,001 quotable -> 2,000 + `stopped='cap'`, the same ids on re-plan; shared
      `created_at` across the batch pages correctly by `id`; bought rows at the front do not
      stop it; ceiling at exactly 20,000 / 20,001 examined.
- [x] i pricing: rates per plan incl. a dirty `" Pro "`; the drift guard (equal to
      `/skip-trace-usage` for clean plans); `included_lookups_remaining` under / over quota,
      rolled window, NULL period start, starter.
- [x] ii gates: foreign job 404 (no key written); not done 409; starter 402 shape; kill switch
      503; empty token 503.
- [x] ii happy path: counts, advanced, `remaining`, the stored payload (shape, TTL <= 600,
      scoped key), no ids in the body; a second quote REPLACES the first (new `quote_id`).
- [x] ii pause through the real `publish()`: paused (account, global), `"never"`, not paused,
      UNKNOWN (no key, tombstone, a closed port, a blackholed port within the 1 s bound).
- [x] ii store failing (closed and blackholed port) -> 503, no quote in the body.
- [x] ii the tab queries run AS `bridgeleads_app` (provisioned in-transaction): no queue read.
- [x] Mutations, each caught: every `classify` branch dropped in turn; `.strip()` -> none;
      ATIP from the process flag; cap off by one; the `id` tie-break dropped; status filter
      dropped from the window query; the sync client swapped for the async one; `wait_for`
      removed (blackholed test hangs past the bound); the kill-switch check dropped; the key
      unscoped (another tab's quote overwritten).
- [x] Regression: both skip-trace batches, `test_contact_lookup_schema`,
      `test_skip_trace_pause_state`, billing tests, `plan_entitlement_audit`, `test_beat_schedule`.

**Amendments from consult r3 (2026-09-28: PLAN: REVISE, 1 P1 + 3 P2; r2 all closed).** Output
`<scratchpad dea35045>/codex_1b1c_consult_r3_out.txt`. These amend the FINAL list above:
- [x] **R1 (P1) ADOPTED: entitlement gate.** After the plan gate, `run_eligibility(user, now)`
      (`quota.py:187`, the one rule every billable start reads); code `frozen` or `ended` ->
      `run_refusal_http(code, message, resumes_at)` (the batches shape, `batches.py:283-286`).
      `over_limit` does NOT refuse: it is the RECORD allowance, and a lookup never counts as a
      record (Phase 1b "No record-quota change"). Tests: frozen, past_due past grace, ended ->
      refused; over the record limit -> quote still 200. Carried to 1b-2: re-checked at confirm
      and under the claim.
- **R2 (P2) REJECTED, doc wins (15-14).** `build_pending_row_payload` re-reads the process ATIP
  flag. In the API the pinned policy IS that flag, read once in the same request, so they
  cannot disagree; in the 1b-2 worker 15-14 explicitly allows "a STRICTER current policy"
  but never an addition, and a process flag stricter than the pinned one only relabels an
  ATIP row `not_traceable` (never quotes it). Changing `skip_trace.py` (the live paid path)
  for a label is not worth the risk; the existing N5 test pins "only ever excludes MORE".
- [x] **R3 (P2) ADOPTED: `remaining` is not a subtraction.** It is its own COUNT of
      not-attempted tab rows with `(created_at, id) > window_end` (0 when the window was not
      cut), so it can never go negative. `today` (tax cap) and `now` are captured ONCE per
      request and shared by every query. The status counts and the window are READ COMMITTED
      statements: an ADVISORY snapshot, which the response already says (upper bound).
- [x] **R4 (P2) ADOPTED without a new zone: Redis is checked BEFORE the scan.** Order: gates ->
      `PING` on the sync client (threadpool, 1 s bound) -> 503 when it fails -> only then the
      DB scan. During a Redis outage the endpoint therefore does no DB work at all, whatever
      the `export` zone's per-process fallback admits. Test: a closed/blackholed Redis -> 503
      with zero `results` queries executed (a SQLAlchemy `before_cursor_execute` listener
      counts them). Mutation: the PING moved after the scan -> the count test fails.
- [x] **R5 (P2, consult r4) ADOPTED.** `rate_limit()`'s async client has no socket timeout
      (`rate_limit.py:54-58`), so a blackholed Redis hangs the request BEFORE the PING. The
      quote wraps it: `asyncio.wait_for(rate_limit(...), 1.0)`; a timeout -> the same 503,
      before any DB work. Test: blackholed Redis -> 503 within ~1-2 s, zero `results`
      queries. Mutation: the `wait_for` removed -> the test exceeds its bound. (Pre-existing
      for every route that calls `rate_limit`: logged follow-up, "async Redis clients carry
      no socket timeouts".)
- **R6 (P2, consult r5) ACCEPTED AS A FACT, NOT FIXED HERE.** `rate_limit()` applies its
  MULTI before awaiting (`rate_limit.py:202-207`), so a `wait_for` timeout can refuse a request
  whose hit Redis already counted (atomic: never half-applied). That over-counts, which is
  the FAIL-CLOSED direction consult r3 #5 asked this limiter to take: a stalled Redis can
  only make a caller wait sooner, never let one through. It needs Redis to stall past 1 s
  AFTER applying the write, and costs at most one of 20 hits per minute. A compensating
  removal is a shared-middleware change (a 6th file) and joins the follow-up above.

**Consult r4 (R1, R3 closed; R2 rejection accepted), r5 (R5 -> R6), r6 (2026-09-28): R6
disposition accepted, no findings: `PLAN: GO`.** Outputs `<scratchpad dea35045>/
codex_1b1c_consult_r{4,5,6}_out.txt`. **OWNER APPROVED the plan (2026-09-28).** Build order:
1b-1c-i, then 1b-1c-ii.

### 1b-1c-i BUILT (2026-09-28), before the Codex diff review
- NEW `src/api/contact_lookup_planner.py`, NEW `src/config/lookup_pricing.py`, NEW
  `tests/test_contact_lookup_planner.py` (63 tests), `docs/BUILD_JOURNAL.md` (the 1b-1b-iii
  entry). No endpoint, no write path, no migration.
- **One bug found by the first run:** the keyset's `tuple_(created_at, id) > (…, …)` bound the
  id as VARCHAR (`uuid > character varying` does not exist); each value is now bound with its
  column's type. Found by `test_the_keyset_pages_a_shared_created_at_by_id`.
- **Parity holds against the REAL enqueue** on a code-violation job carrying every gate (12
  leads), with the ATIP flag both ways: equal ids and equal `trace_type`s. With a cache hit and
  a charged-unanswered row seeded, the enqueue queues a strict SUBSET of the quote.
- **Added beyond the plan:** a recording-proxy test that every attribute `classify` (and
  `build_pending_row_payload` inside it) reads is a SELECTED column, because `getattr(row, x,
  None)` on a Row lacking `x` returns None silently and would diverge from the enqueue.
- **Mutations: 16/16 caught** (`<scratchpad dea35045>/mutate_1b1c_i.py`): errored and
  in-progress branches, `.strip()`, placeholder, settled, ATIP from the process flag, cap and
  ceiling off by one, the `id` tie-break, the window's status filter, the tab's category, the
  remaining count's keyset, a dropped select column, the roll rule, `normalize_plan` in the
  price, an agency price drift.
- **Regression (foreground, 5 chunks, all 26 skip-trace / tracerfy / lookup / billing /
  entitlement / beat files): 1,040 passed, 0 failed.** No type checker is configured in this
  repo (no mypy/pyright in `pyproject.toml` or CI); ruff clean.
- **Codex diff review (three-dot, on `c0b09b7a`):** r1 no code findings, one P3 (the handoff
  still said nothing was built: a dated status note added); **r2 `VERDICT: GO`, no findings.**
  Security pass: no endpoint or write path; every query carries `user_id` + `job_id`; no
  contact/encrypted column is selected; no secret; no new dependency.
- **MERGED + LIVE: PR #384, merge `9833a7b0` (2026-09-28 14:50Z).** Rebased once over #383
  (journal conflict, both entries kept); Codex r3 GO on the rebased three-dot diff; CI green on
  the exact head; quiet all zeros; api/worker/beat on `9833a7b0`, clean boot.

### 1b-1c-ii BUILT (2026-09-28), before the Codex diff review
Branch `feat/lookup-1b1c-ii-quote` off `9833a7b0`. `routes/jobs.py` (the endpoint),
`schemas.py` (`ContactLookupQuoteRequest`, `ContactLookupQuote`, `ContactLookupExcluded`,
`ContactLookupPause`, `ContactLookupUnavailableResponse`), `schema/openapi.json`, NEW
`tests/test_contact_lookup_quote.py` (24 tests), this plan = 5.
- **Where the build differs from the plan text, and why:**
  - "Pause UNKNOWN on a closed port -> still 200" cannot happen as written: R4 PINGs the same
    client before the scan, so a dead Redis is a 503 first. UNKNOWN is proven through the real
    publisher (no key, a stale heartbeat, a tombstone); the reader's own Redis-failure paths
    are `test_skip_trace_pause_state`'s.
  - "Store failing (closed and blackholed port)" would also stop at the PING. The store path
    is proven against a PRIVATE real `redis-server` run with `maxmemory 1` + `noeviction`: it
    answers PING and reads and refuses every write (OOM). Local Redis is 5.0 (no ACLs), and a
    server-wide `CLIENT PAUSE` would have stalled another session's suite.
  - The request body is `{"extra": "forbid"}` (house style), so a stray filter param is a 422
    rather than silently ignored (O4: filters never apply).
- **OpenAPI:** `.venv-schema` is BROKEN (its base was the removed Anaconda Python). A fresh
  CI-equivalent venv at `C:/Users/Windows/bl-schema-venv` (uv CPython 3.12.12 = CI's 3.12,
  `pip install -r requirements.txt`: fastapi 0.141.1, pydantic 2.13.4) regenerated it: +326,
  **0 deletions** vs `origin/main`, every addition the quote's; `--check` OK.
- **Mutations: 14/14 caught** (`<scratchpad dea35045>/mutate_1b1c_ii.py`): the limiter
  unbounded (the request hangs: caught by the 150 s runner timeout), the PING dropped (the
  closed-port test then sees `results` queries), kill switch, frozen/ended not refused,
  over_limit refusing, plan gate, the job owner filter, run-not-done, the key unscoped by
  category, `nx=True` (no replace), a swallowed store failure, the async client handed to the
  sync calls, truncation without anything left, the category dropped from the planner call.
  **The last one SURVIVED the first run** (no test quoted the `already_delivered` tab with
  leads in it): `test_each_tab_quotes_only_its_own_leads` added, now caught.
- **Regression (5 chunks, every test file that calls `/jobs` + the lookup/billing set): 857
  passed, 0 failed.** ruff clean.

### main moved under ii: #374 + #378 (security audits 4/4b) change two binding inputs (2026-09-28)
Rebased cleanly onto `4f56a5e3`, but NOT reviewed or pushed: two facts the plan stood on changed.
- **F1 WHO MAY BUY A LOOKUP is now decided by the claim** (`paid_lookup_access()`,
  `src/workers/skip_trace_claim.py`, audit S3-03/S4-01): `starter` / `frozen` / `ended` are
  blocked; `full` for admin, a paid term ending later, `active`, `past_due` in grace, or an
  operator-granted plan; everything else (app trial, `trialing`, `canceled`, ...) is `trial`,
  which may queue at most `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE` (25) credits over its WHOLE
  LIFETIME. The rest is HELD (`not_attempted`). The room is `allowance -
  lifetime_credits_queued()`, read from `pending_skip_trace_rows`: WORKER-ONLY, so the API
  cannot compute a trial's remaining room. My quote gates on the plan NAME: a trial `pro`
  account would be quoted up to 2,000 leads while the claim buys at most 25 credits.
  Money-safe (the claim is the authority), customer-wrong. Frozen/ended: my R1 gate uses the
  same predicates (`is_frozen`, `entitlement_ends_at <= now`), so it agrees.
- **F2 the `export` zone is now the shared bucket of EVERY full-CSV route** (job, batch and
  segment downloads, S4-03). A quote in that zone spends the customer's download budget.
- The API already lazy-imports `src.workers.*` inside routes (`batches.py:411,765`,
  `registration.py:105`), so the quote may call `paid_lookup_access()` itself (one rule, no
  copy); `skip_trace_claim.py` imports only sqlalchemy and the logger at module level.

**Proposed amendments (need Codex + OWNER):**
- **A1 access by the claim's own rule.** Local import of `paid_lookup_access`; `starter` ->
  the structured plan 402 (as now); `frozen`/`ended` -> the run-refusal 402 (R1, unchanged
  predicates, `run_eligibility` still supplies the message); `full` -> as planned; `trial` ->
  OWNER DECISION T:
  - **T-a (recommended)** quote it, capped: walk the window as now but stop once the quoted
    CREDITS (normal 1, advanced 2) would exceed the lifetime allowance, in the same window
    order the claim keeps; the response adds `access` (`full` | `trial`) and
    `trial_credit_allowance`, and says lookups already used on the trial lower it further
    (the claim holds the rest). Stored `quoted_ids` capped the same way.
  - **T-b** refuse trials at the quote with a structured 402 ("Paid plans include contact
    lookups"), leaving their 25 credits to scrape-time lookups only.
- **A2 a dedicated `lookup_quote` zone** (10/min per user, in `_FALLBACK_ZONES`), so quoting
  never spends download budget. It is `rate_limit.py`, a 6th file in ii: OWNER DECISION Z:
  - **Z-a** allow ii at 6 files (the zone line + its comment);
  - **Z-b (recommended)** a tiny precursor PR (zone + a test + the plan), then ii;
  - **Z-c** share the existing `writes` zone (30/min: cancel + scraper edits) instead.

**Codex consult on A1/A2 (2026-09-28): PLAN: REVISE, 3 P1 + 1 P2 + 2 P3.** Output
`<scratchpad dea35045>/codex_1b1c_ii_amend_r1_out.txt`. F1, F2 confirmed; nothing else in
#374/#378 touches the planner or its parity. A1 sound for an advisory quote (pass the route's
`now`, no row lock on the quote path). Codex preferred T-a with an EXACT API-visible lifetime
counter (a migration); recommended Z-b; asked for payload/key **v2** and an explicit
trial-cap disposition/reason at the 1b-2 confirm.

**OWNER DECISIONS (2026-09-28):**
- **T: cap at the FULL allowance** (no new schema). A trial's quote stops once its quoted
  CREDITS would exceed `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE`, in window order (the order the claim
  keeps). This is still a true UPPER bound: the claim's room is `allowance - used <=
  allowance`, and the claim only lowers it. The response carries `access` (`full` | `trial`)
  and `trial_credit_allowance`, and its docs say credits already used on the trial lower it
  further. Rejected: the exact counter (a money-path migration phase ahead of ii for a number
  the claim already enforces) and refusing trials (their allowance was meant to be usable).
- **Z: precursor PR first.** `lookup_quote` zone, 10/min per user, in `_FALLBACK_ZONES`;
  `rate_limit.py` + its test only (this plan records it in ii). Then ii rebases on it.
- Adopted from Codex: payload `v: 2` with `access`, `trial_credit_allowance` and
  `quoted_credits`, key namespace `...:quote:v2:...`; carried to 1b-2: confirm re-checks access
  and the trial room under the user-row lock, refuses a payload whose `v` it does not know, and
  records the trial-held leads with an explicit reason.

### Amendments BUILT (2026-09-28)
- **Precursor PR #386** (`feat/lookup-quote-rate-zone`, 4 files): the `lookup_quote` zone
  (10/min, fail-closed; 6 tests, 3/3 mutations) AND the planner's `credit_cap` (moved here so ii
  stays at 5 files): `Window.credit_cap` / `quoted_credits` / `over_credit_cap`,
  `stopped="credit_cap"`, applied exactly as `claim_skip_trace_rows` applies its room (in order,
  skip a lead that does not fit, keep a cheaper later one), `CREDITS` pinned equal to the
  worker's `CREDITS_PER_ROW`, `PLANNER_VERSION` 2. **Parity with the REAL claim in trial mode**
  (what it keeps == what the planner quotes); 6/6 mutations. Codex r1 GO, r2 NO-GO (P2 the
  window should carry its cap, P3 docstring) fixed, r3 GO, r4 GO after rebase onto `fa658ccf`.
  **A flaky test caught before push:** rows sharing one `created_at` were ordered by random id,
  so the trial test's credit total varied (it had passed once by luck). Each row now has its own.
- **ii** (on top of #386): `paid_lookup_access()` decides access (lazy import); a trial is
  capped via `credit_cap = SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE`; response adds `access`,
  `trial_credit_allowance`, `over_trial_allowance`, `truncated_reason` may be `credit_cap`;
  zone `lookup_quote`; payload and key `v2`. 26 quote tests; **18/18 mutations** (4 new: trial
  cap not applied, zone back to `export`, access by plan name only, payload v1); openapi +352,
  0 deletions, `--check` OK. Regression on the full stack: **923+ passed, 0 failed**.
- 🛑 **CI BLOCKED (2026-09-28): GitHub Actions billing.** #386's re-run on `acdcadcf` failed in 3 s
  ("recent account payments have failed or your spending limit needs to be increased"). Its
  previous head (`3a990e0e`) had passed CI, but `main` moved (#387, docs), so the gate needs a
  new green run. Nothing merges until the owner fixes billing.
- **Codex diff review of ii** (three-dot against the precursor branch): **`VERDICT: GO`, no
  findings** (`<scratchpad dea35045>/codex_ii_review_r1_out.txt`). ii stays LOCAL (unpushed)
  until #386 merges; then: rebase onto main, Codex re-check, push, PR, CI, merge gate.
- **#386 MERGED + LIVE (2026-09-30): merge `29172543`.** Billing fixed by the owner. The first
  green-able run then failed the required Dependency Audit on PyJWT 2.13.0 (10 new CVEs, not
  this PR); the S4-02 session shipped the bump as #391 (`0539de2b`). main moved three times under
  #386 (#391, #390 the 2c-bis attempt fence, #389 S4-02); each rebase was clean with a
  byte-identical patch, and Codex r5-r8 each said GO. #390 gave `_enqueue_skip_trace_rows` an
  optional `attempt_token` fence: it only ever queues LESS, so the quote stays an upper bound and
  the parity test (called without a token) still pins selection, gates and trace types. CI green
  on `4fc42a5d`, quiet all zeros; api/worker/beat SUCCESS on `29172543`, clean boot.
- **ii's first CI run FAILED (PR #393, 2026-09-30): 1 of 5,422.**
  `test_a_quote_redis_cannot_store_is_never_shown` spawned a HARDCODED local Windows
  `redis-server.exe`; CI's Redis is a `redis:7-alpine` service container with no binary on the
  runner. A portability bug in the test, not in the endpoint. Fix: on Redis >= 6 (CI), a
  throwaway ACL user `+@all -@write` on the test Redis (a real NOPERM on SET); on Redis 5 (the
  local rig, no ACLs), the private `maxmemory 1` server via `BL_TEST_REDIS_SERVER` or PATH;
  neither -> the test FAILS, never skips. The write probe now requires a server `ResponseError`
  (a connection error is also a `RedisError` and proved nothing). The swallowed-store mutation
  is still caught.

**Files:** i = planner, `lookup_pricing.py`, planner tests, `docs/BUILD_JOURNAL.md`, this plan;
ii = `routes/jobs.py`, `schemas.py`, `schema/openapi.json`, quote tests, this plan.
**Logged follow-ups:** `/skip-trace-usage` onto `lookup_pricing` + `normalize_plan` + the roll
rule; a dedicated `lookup_quote` rate zone; `(…, created_at, id)` index if windows grow.

## Phase 1b-2 — the WRITE path: confirm, worker claim, reconcile, settle (PLAN, 2026-09-30, BEFORE Codex consult)

1b-1c is LIVE (#384, #386, #393; main `3372cb82`). This is the first phase that SPENDS: a
customer confirms a quote and leads enter the paid Tracerfy queue. It adds no new spend path of
its own. Rows enter the queue ONLY through `lock_job_for_claim()` + `claim_skip_trace_rows()`
(H3), and from there the existing dispatcher, caps, ingest and metered billing do all the rest.

### Facts (read in code, 2026-09-30, main `3372cb82`; three research passes, spot-checked)
- **Schema (101, untouched by 102-106).**
  - Action statuses: `dispatching, running, claimed, settled, failed, expired`.
  - Dispositions:
    - the API-initial set: `quoted` + 5× `excluded_*`;
    - worker verdicts: `newly_queued, reused, already_answered, in_progress_elsewhere,
      ineligible, released, abandoned`;
    - terminal: `answered_hit, answered_miss, unmatched_billable, errored_unsubmitted`.
  - Events have no CHECK on from/to.
  - **The guard triggers restrict ONLY user-scoped sessions.** An empty GUC as
    `bridgeleads_system` (or super/BYPASSRLS) passes with no transition matrix. So the worker's
    state machine is enforced by CODE, and must be pinned by tests.
  - The API may:
    - insert an action `dispatching` with zero counts (the server owns `created_at`);
    - insert `quoted`/`excluded_*` results;
    - insert ONE `-> dispatching` event (checked `FOR SHARE` against a `dispatching` parent);
    - stamp `dispatched_at` once. Nothing else.
  - Grants: app SELECT+INSERT on all three, `UPDATE(dispatched_at)` on actions. System
    SELECT/INSERT/UPDATE on actions+results, SELECT/INSERT on events. DELETE for nobody.
- **`pending_skip_trace_rows.action_id`** is nullable and indexed `(action_id, user_id)`, with
  **NO FK** (audit2 T-5, P3 "latent"). **No code writes it**, or `results.last_trace_outcome`.
- **The claim** (`skip_trace_claim.py`):
  - The caller owns the transaction, nothing commits.
  - It asserts one user, one job, `lock_job_for_claim` held, the unique index valid.
  - It locks the user row `FOR NO KEY UPDATE` and applies `paid_lookup_access`.
  - Trial room is `allowance - lifetime_credits_queued`, walked in the CALLER's order, skipping
    a lead that doesn't fit.
  - It inserts `queued` with `ON CONFLICT DO NOTHING RETURNING`, joined to results
    (`user_id` + `job_id` + `not_attempted`), then advances results.
  - The insert column list is fixed `_COLUMNS` and has **no `action_id`** (confirmed by grep).
- **The scrape enqueue** (`enrich.py:2229-2840`):
  - gates;
  - charged-unanswered settle (`errored`);
  - placeholder / settled-CV filters;
  - lock + attempt fence;
  - re-read under the lock;
  - per-row ATIP skip, `build_pending_row_payload`, cache-hit ORM copy (encrypted columns: NO
    raw SQL);
  - claim, commit.
- **Writers after the claim, and whether each commits itself** (the 16-3 inventory is now
  exact):
  - (G) `_cancel_undeliverable_queued`: caller commits.
  - (H) `_settle_queued_from_known_answers`: SELF-commits.
  - (I)/(J) cancel / pre-submit fail: caller commits.
  - (K) queued→submitting: self.
  - (L) `_persist_submission`: self.
  - (M) `_release_claim`: self.
  - (N) stale-claim reconcile: via L/M.
  - (O)/(P) ingest completed / unmatched: self, once, under the queue-row lock.
  - (Q) retention purge.
  - (R)/(S) two scripts.
- **Billing is by pending row at ingest** (`report_lookups_for_user` + the meter outbox), keyed
  `(tracerfy_queue_id, user_id)`. **Nothing bills per action, and 1b-2 must not either.**
  Settlement here is BOOKKEEPING: derive verdicts and counts, never call the meter.
- **API→Celery house pattern** (`POST /jobs`, `POST /batches`):
  - commit the durable row;
  - `apply_async` in try/except;
  - a publish failure warns and returns the committed row (no 500);
  - a beat sweep re-drives stale `pending` rows after N minutes.

  New task modules MUST be added to `src/workers/__init__.py` `include`. Beat entries of 10 min
  or more must be crontab (`test_beat_schedule`).
- The quote's Redis payload v2 (`jobs.py:1203-1230`) carries `quoted_ids` (≤2000, in window
  order), `policy`, pricing, `access`, `trial_credit_allowance`, `window_end`, `expires_at`. It
  holds counts only for exclusions, not their ids.

### Design decisions to settle in the consult (my recommendation first)
- **S1 Settlement is DERIVED, not written by the eight live writers (supersedes 15-5 as
  written).** 15-5 wants every writer to update the action verdict in its own transaction. That
  means refactoring H, K, L, M and O/P (live paid code, most self-committing) for a status page.
  - Instead: pending rows carry `action_id` (written by the claim, atomically), and they ARE
    the per-lead truth.
  - A reconciler maps each claimed lead to its terminal verdict from `pending_skip_trace_rows`
    (by `action_id`) + `results`:
    - `completed` + hit → `answered_hit`;
    - `completed` + miss → `answered_miss`;
    - `unmatched` → `unmatched_billable`;
    - pre-submit `errored` → `errored_unsubmitted`;
    - `cancelled` → `released`;
    - `reused` → `reused`.
  - It recomputes the counts cache, and settles when no active row remains.
  - Lag = one reconciler tick. The "Cut as over-engineering" note already made the counts a
    recomputable cache. It never bills.
- **S2 `last_trace_outcome` writers are OUT of 1b-2 (own phase, 1b-3).** Its only consumer is a
  future retry-errored feature. Writing it touches every one of the eight writers. NULL stays
  UNKNOWN (16-3).
- **S3 Order = spend path last to become reachable.**
  - **2a** schema + claim `action_id`;
  - **2b** worker task (idle: nothing dispatches to it);
  - **2c** reconciler (idle: no actions);
  - **2d** the confirm endpoint (the switch that makes it live);
  - **2e** `GET` action status.

  Each is ≤5 files and each is a deploy.

### 1b-2a — `action_id` FK + the claim writes it (migration 107)
- [ ] Migration 107: composite FK `(action_id, user_id)` → `contact_lookup_actions(id, user_id)`.
  - `NOT VALID` then `VALIDATE` (every existing row is NULL: trivially valid). Each step under
    its own `lock_timeout`; object-verified; replay-safe (the 101 pattern).
  - **ON DELETE: `NO ACTION`**, never CASCADE. A pending row is billing evidence, and deleting
    an action must never delete it. Actions are only deleted by a user CASCADE, which also
    reaches the pending rows through their own user FK. (Consult: confirm the cascade order
    cannot trip NO ACTION.)
- [ ] `models.py`: the FK on `PendingSkipTraceRow`.
- [ ] `claim_skip_trace_rows(..., action_id=None)`:
  - written on every inserted row;
  - `None` = the scrape path, byte-for-byte as today.
  - A non-None `action_id` must belong to the same user (the FK proves it at insert).
- [ ] Tests: the FK refuses a foreign or other-tenant action; the claim writes `action_id`; the
  scrape path still writes NULL; parity tests unchanged.
- Files: migration, `models.py`, `skip_trace_claim.py`, `tests/test_skip_trace_claim_action.py`,
  this plan = 5.

### 1b-2b — the worker `lookup_contacts(action_id)`
NEW `src/workers/contact_lookup_action.py`, registered in `include`, `system_sync_session()`.
One task, idempotent under at-least-once delivery. Steps:
1. **Start CAS:** `dispatching -> running` with `lease_token` / `lease_expires_at`
   (now + 10 min) / `started_at` + event. Nothing updated means another delivery holds it, or
   it's terminal: no-op.
2. **Job gate:**
   - job by `(id, user_id)`, `status='done'` and delivered (`_job_delivered_sql`, 15-6);
   - else `failed` + every `quoted` → `abandoned` + events.
3. **Access gate:**
   - plan in `SKIP_TRACE_ADDON_PLANS` (fixes the enqueue's starter-only gate for this path);
   - `paid_lookup_access` not blocked;
   - else `failed` / `abandoned`.
4. **Kill switch or token off: the action WAITS** (back to `dispatching`, lease cleared). The
   reconciler re-drives it until the deadline, then `expired`. (Consult: vs failing fast.)
5. `lock_job_for_claim(db, job_id)` (H3).
6. **Read the quoted set (20-3, 16-8):**
   - `results JOIN contact_lookup_action_results ON action_id AND disposition='quoted'`;
   - `results.user_id = action.user_id AND results.job_id = action.job_id`;
   - order `(created_at, id)`, the quote's window order.

   The set can only SHRINK.
7. Per lead, classified in this order:
   - `classify(row, current policy)`: a stricter current policy only excludes (15-14). Not
     quotable → its verdict (`already_answered` / `in_progress_elsewhere` / `excluded_*` /
     `ineligible`).
   - Charged-unanswered (the enqueue's `_settle_charged_unanswered` rule) → `already_answered`.
   - Valid cache hit → ORM copy exactly as the enqueue does → `reused`.
   - Else a claim payload.
8. `claim_skip_trace_rows(db, payloads, action_id=..., report=...)`:
   - returned → `newly_queued`;
   - `report["held"]` (trial room) → `ineligible` + a per-lead event with reason
     `trial_allowance`;
   - lost race → verdict from the row's current status.
9. **Same transaction:**
   - every quoted lead gets exactly one verdict;
   - counts are aggregated from the verdicts;
   - status `claimed`, `claimed_at`, **lease cleared (15-15)**, event;
   - ONE commit.
- **Shared code, not a copy:** the cache-hit copy and the charged-unanswered rule are extracted
  from `enrich.py` into helpers both paths call. (Consult: extraction touches the live enqueue.
  The alternative is a copy plus a parity test, which the repo's history says drifts.)
- Tests (real PG + Redis):
  - redelivery / double delivery → one set of pending rows;
  - the action races a scrape enqueue on the same lead → exactly one row, nothing stranded;
  - a lead answered or queued after the quote → not bought;
  - a non-quoted id / other-tenant id can never be bought;
  - the trial cap holds and the held leads are recorded;
  - kill switch → waits;
  - job not done → abandoned;
  - every verdict written;
  - the lease is cleared at claim;
  - parity: what the action claims == what the enqueue would claim for the same leads.
  - Mutations.
- Files: `contact_lookup_action.py`, `src/workers/__init__.py`, `enrich.py` (extraction), tests,
  this plan = 5.

### 1b-2c — the reconciler (beat, 120 s interval; under 10 min, so plain seconds is allowed)
- [ ] `dispatching`:
  - with `dispatched_at IS NULL` and older than 1 min, or `dispatched_at` older than 5 min →
    re-publish;
  - past the deadline (`created_at + 30 min`) → `expired`, quoted → `abandoned` + events.
- [ ] `running` with `lease_expires_at < now` → back to `dispatching` (the claim is atomic, so
  nothing is half-done) + event.
- [ ] `claimed`: derive terminal verdicts from pending rows by `action_id` (S1) + `results`.
  Recompute counts. No active pending row left → `settled`, `settled_at`, event.
- [ ] All in `system_sync_session()`, bounded per tick (LIMIT + oldest-first, with the
  starvation landmine in mind: a per-action cursor, not `ORDER BY oldest LIMIT` alone).
- Files: `src/workers/scheduler_helpers/contact_lookups.py` (impl), `scheduler.py` (entry), tests, this
  plan = 4.

### 1b-2d — `POST /jobs/{job_id}/contact-lookups` {quote_id, category} → 202
Gates:
1. `wait_for(rate_limit(zone="lookup_quote"))`: shares the quote's bucket; a confirm follows a
   quote.
2. job `(id, user_id)`, else 404; not `done` → 409.
3. plan → 402; frozen/ended → 402; kill switch / token → 503.
4. `paid_lookup_access` not blocked. The TRIAL ROOM is not checkable by the API (worker-only
   table): the claim enforces it and the action records the held leads.

Idempotency (15-13):
- Load the tab's Redis key.
- Key missing → look up the action by `(quote_id, user_id)` in the DB → return it (200/202);
  none → 410 `quote_expired`.
- `quote_id` mismatch (superseded) → 410 `quote_expired`.
- `v != 2` → 409 `quote_unsupported`.
- user / job / category / expiry must match.

In ONE API transaction (GUC set, so the guard triggers apply):
- INSERT the action (`dispatching`, `quoted_count`, `truncated`, pricing snapshot);
- one `quoted` result row per `quoted_ids`;
- the `-> dispatching` event;
- COMMIT. A unique `quote_id` `IntegrityError` → rollback, re-fetch, return that action.

Then:
- delete the Redis quote by compare-and-delete on `quote_id` (Lua);
- `lookup_contacts.apply_async(args=[action_id])`;
- success → `UPDATE dispatched_at` (once);
- failure → warn and return 202 anyway; the reconciler re-drives.

Response `ContactLookupAction {action_id, status, quoted_count}`.
- **No exclusion rows at confirm** (the quote stored counts, not ids). The exclusion breakdown
  stays the quote's. (Consult: vs re-classifying the window at confirm.)
- Tests:
  - every gate;
  - replay → same action;
  - two concurrent confirms → one action;
  - superseded quote → 410;
  - expired → 410 unless the action exists;
  - publish failure → 202 + `dispatched_at` NULL;
  - the API cannot write any other state (the triggers, as the real role);
  - no lead ids in the body.
- Files: `routes/jobs.py`, `schemas.py`, `schema/openapi.json`, tests, this plan = 5.

### 1b-2e — `GET /jobs/{job_id}/contact-lookups/{action_id}`
- Status, counts, and the pause state (the quote's reader), for the 1c page.
- Files: route, schema, openapi, tests, plan.

### Questions for the consult
1. S1 derived settlement vs 15-5's in-writer updates: what breaks?
2. The FK's ON DELETE and the cascade order.
3. The worker's kill-switch behaviour: wait vs fail.
4. Extracting the enqueue helpers vs copy + parity.
5. The confirm writes no exclusion rows.
6. The shared rate bucket.
7. Anything in the claim/dispatcher that assumes `action_id IS NULL`.
8. The 20-3 re-authorization: is a JOIN on the quoted set enough?
9. Is there any path where an action buys a lead outside `quoted_ids`, or twice?

### Codex pre-code consult r1 (2026-09-30): PLAN: REVISE, 3 P1 + 4 P2 + 1 P3, all adopted
Output: `<scratchpad 49da3c50>/codex_1b2_consult_r1_out.txt`. Both design-changing P1s were
re-verified in code (`skip_trace_usage.py:599-646`; `repair_probate_party_and_bad_parcel.py:111,
216,258,266,281`). Codex agreed with S1 (derived settlement), the FK's `NO ACTION`, "wait" on
the kill switch, helper extraction over a copy, no exclusion rows at confirm, and the 2a→2e
order. These AMEND the sections above:
- **V1 (P1) Confirm proves the quoted set.**
  - Result rows go in with `INSERT ... SELECT` from `results`, constrained to
    `id = ANY(:quoted_ids) AND user_id = :uid AND job_id = :job_id`.
  - The inserted count must EQUAL the number of unique `quoted_ids`, else roll back and return
    409 `quote_stale`.
  - The worker fails the action closed (`failed`, all `abandoned`) if the durable `quoted` set
    size ≠ `action.quoted_count`.
  - The worker's read also re-checks the job-deliverability predicate under the lock (Q8).
- **V2 (P1) Unmatched follows the BILLING rule.** Billing bills `unmatched` only when the
  queue's `rows_uploaded >= COUNT(rows sent)` (`accepted_all`); otherwise only `completed`.
  - Migration 107 adds the disposition **`unmatched_unbilled`** to the CHECK (+ the models
    constant).
  - The reconciler maps `unmatched` to `unmatched_billable` / `unmatched_unbilled` by the SAME
    predicate: `queue_accepted_all(db, queue_id)`, extracted in `skip_trace_usage.py` and
    called by both billing and the reconciler. One rule, two callers.
- **V3 (P1) The repair script is gated before any spend path.** New PR **2-0**, first:
  `scripts/repair_probate_party_and_bad_parcel.py`
  - takes `lock_job_for_claim()` per job it touches;
  - REFUSES (report and skip) any pending row with `action_id IS NOT NULL`, and any result
    that has a non-terminal action verdict.

  16-10 is closed as a gate, not a note. Files: the script, its test, this plan.
- **V4 (P2) Settlement is atomic and never silent.**
  - The reconciler writes every terminal verdict, the recomputed counts and `settled` in ONE
    transaction.
  - A lead stuck `submitted` with an unknown provider outcome (`skip_trace_dispatcher.py:
    1621-1627`) keeps the action `claimed` with `status_reason='provider_reconciliation_required'`,
    an ops alert once per action, and the reason visible in 2e.
- **V5 (P2) FK delete behaviour.** Keep `NO ACTION` (checked at END of statement, after the
  job/user cascades have removed both rows). Tests: delete a job, and a user, that own an action
  with action-linked pending rows; both succeed. A direct action delete is still refused by the
  guard / grants.
- **V6 (P2) The quote snapshot is persisted.** Migration 107 adds `contact_lookup_actions.
  quote_snapshot JSONB NOT NULL DEFAULT '{}'`. It holds the quote's counts, the exclusion
  breakdown, `policy` (the 15-14 pin: it closes the "policy only in Redis" gap), `access`,
  `trial_credit_allowance`, `planner_version`, `window_end`, `stopped`, `remaining`.
  - The API writes it at INSERT. The guard already lets the API insert any non-listed column,
    and its UPDATE rule freezes it (the `to_jsonb` diff).
  - A test proves the API cannot change it after insert.
- **V7 (P2) Confirm is limited in the `writes` zone** (30/min, already fail-closed), not the
  quote's 10/min bucket. A retry after a broker failure is not starved by the quote scans, and
  no 6th file (a new zone) is needed. The idempotent re-fetch still sits AFTER the limiter
  (a limiter hit is a 429 the client retries).
- **V8 (P3) One transition matrix in code.** `ACTION_TRANSITIONS` / `VERDICT_TRANSITIONS` in the
  worker module. Every CAS goes through `_move(action, from, to, reason)`, which asserts the
  matrix and writes the event in the same statement batch. Mutation tests: `settled→running`,
  `failed→claimed`, a terminal verdict rewritten.

**Revised order and files:**
- **2-0** writers contract: the repair-script gate + the dispatcher's two stale comments
  (the script, its test, `skip_trace_dispatcher.py` comments only, this plan = 4).
- **2a** migration 107: action FK + `unmatched_unbilled` + `quote_snapshot`; `models.py`; claim
  `action_id`; tests; plan (5).
- **2b** worker (5).
- **2c** reconciler + `queue_accepted_all` extraction (`src/workers/scheduler_helpers/contact_lookups.py`,
  `scheduler.py`, `skip_trace_usage.py`, tests, plan = 5).
- **2d** confirm (5).
- **2e** status (5).

### Codex pre-code consult r2 (2026-09-30): PLAN: REVISE, 3 P1 + 4 P2; V5, V6 confirmed
Output: `<scratchpad 49da3c50>/codex_1b2_consult_r2_out.txt`. Codex confirmed that a
non-deferred `NO ACTION` is checked at END of statement, after cascades. It also confirmed that
cascaded deletes fire the 101 BEFORE DELETE guards as the deleting role, so the V5 tests delete
as the system/owner role. And it confirmed that `quote_snapshot` is insertable by the API and
frozen on UPDATE. These AMEND V1-V8:
- **W1 (P1) The lease is committed, and the claim is fenced by it.**
  - T1: the start CAS `dispatching→running` (token, expiry, `started_at`, event) COMMITS alone,
    so a crashed worker leaves a visible expiring lease for the reconciler.
  - T2 (fresh transaction):
    1. `SELECT ... FOR UPDATE` the action;
    2. re-check `status='running' AND lease_token=:mine AND lease_expires_at > now()`;
    3. `lock_job_for_claim`, then the claim and the verdicts, then `claimed` + the lease
       cleared;
    4. ONE commit.
  - A lost fence means a no-op. Every fail / wait / abandon transition is its own committed
    transaction. Test: kill the worker between T1 and T2; the reconciler recovers it.
- **W2 (P1) The 2-0 repair-script gate, exactly.** Per job, in the transaction that writes:
  `lock_job_for_claim(job)` → RE-READ the rows → refuse (skip + report) any pending row with
  `action_id IS NOT NULL` and any result with a non-terminal action verdict → write → commit.
  Tests on real rows: an action-linked row is never touched; an unlinked one is repaired.
- **W3 (P1) Unmatched mirrors what billing DID, and billing's own rule has a latent gap.**
  - The reconciler calls the SAME extracted `queue_accepted_all(db, queue_id)` as
    `report_usage_from_webhook`, so the action page always agrees with what Stripe received.
  - Codex found the gap, and I verified it: the rows actually SENT (`len(claimed)`,
    `skip_trace_dispatcher.py:1510-1528`) are never persisted. `_persist_submission` allows
    `moved < claimed` (alert only, `:1576-1578`). `accepted_all` compares `rows_uploaded` with
    the rows STAMPED with the queue id. So in a partial-bookkeeping batch, unmatched rows can be
    billed although a row was dropped.
  - That is a PRE-EXISTING live billing edge case (rare: it needs the partial-bookkeeping alert
    to have fired). It is NOT introduced here. → **Owner item O-C.**
- **W4 (P2) V7 wording corrected.** The `writes` zone is a shared per-user 30/min limit with a
  bounded per-process fallback when Redis fails (not a hard fail-closed). Confirm passes
  `identifier=current_user.id`.
- **W5 (P2) The claim reports WHICH leads it held.** `report["held_ids"]` (the result ids the
  trial room held, in order) alongside `report["held"]` (the count, unchanged), so the worker
  writes a per-lead `ineligible` + `trial_allowance` event. In 2a.
- **W6 (P2) The claim proves action ↔ job.** With `action_id`, the insert JOINs
  `contact_lookup_actions a ON a.id = :action_id AND a.user_id = v.user_id AND a.job_id =
  v.job_id`: a pending row can never be attributed to another job's action. In 2a. Test: an
  action of job A handed leads of job B claims nothing.
- **W7 (P2) One state machine, both writers.** `ACTION_TRANSITIONS`, `VERDICT_TRANSITIONS` and
  `_move()` live in the worker module (`contact_lookup_action.py`), and the 2c reconciler
  IMPORTS them, so there is no second copy and no 6th file.

### Codex pre-code consult r3 (2026-09-30): PLAN: REVISE, 2 P2 + 1 P3; W1-W7 closed, no deadlock
Output: `<scratchpad 49da3c50>/codex_1b2_consult_r3_out.txt`. Lock order verified against the
dispatcher: W1's action -> job advisory -> user row does not conflict with the dispatcher's global
advisory lock + `SKIP LOCKED` row locks. Adopted:
- **X1 (P2) S1 stands; the stale dispatcher contract is corrected.** `_cancel_undeliverable_queued`'s
  docstring (`skip_trace_dispatcher.py:868-873`) promises that 1b-2 moves the action verdict to
  `released` in the same transaction. Under S1 cancellation stays pending-row-only and the
  reconciler derives `cancelled -> released`. The docstring is rewritten in **2-0** (the
  writers-contract PR), BOTH places: the comment at `:284-290` and the docstring at `:868-873`,
  so no reviewer re-implements the old promise.
- **X2 (P2) O-C is a HARD GATE before 2d**, with regression tests for `rows_sent != rows_uploaded`
  and a legacy `rows_sent IS NULL` (bill `completed` only).
- **X3 (P3)** the 2c path is `src/workers/scheduler_helpers/contact_lookups.py`.
- 2-0 replaces the script's global scan-then-commit with a per-job lock -> re-read -> refuse -> write
  -> commit (`repair_probate_party_and_bad_parcel.py:404,571`).

**Consult r4 (2026-09-30): REVISE** (O-C wording, the second stale dispatcher comment at `:284-290`,
the 2-0 manifest; all fixed). **r5: `PLAN: GO`, no findings.** Outputs `<scratchpad 49da3c50>/
codex_1b2_consult_r{4,5}_out.txt`.

**OWNER DECISIONS (2026-09-30): plan APPROVED, build from 2-0 in order.** O-A: confirm goes
live with 2d (no flag). O-B: kill switch off -> the action WAITS until its deadline, then expires.
O-C: its own billing PR, and it BLOCKS 2d.

### Owner items (before code)
- **O-C** (billing, pre-existing, found by the consult) Persist `skip_trace_queues.rows_sent`
  at submission and compare `rows_uploaded` against it in `accepted_all`. With a mismatch, or
  with no `rows_sent` (old rows), bill `completed` only. It's a live billing change: a
  migration, the dispatcher and billing, its own PR. **2d is BLOCKED until O-C has shipped**
  (migration, dispatcher, billing, and the mismatch + `rows_sent IS NULL` regression tests):
  confirm is what adds volume. It does not block 2-0/2a/2b/2c.
- **O-A** Should the confirm endpoint (2d) go live before the frontend (1c) ships? It's
  reachable only by an authenticated paying account and gated like the quote. The alternative
  is a feature flag: `settings.py` + `.env.example`, over the 5-file rule, a split.
- **O-B** Kill switch off after a confirm: should the action wait (recommended), or fail?

### 2-0 BUILT (2026-09-30), before the Codex diff review
- `scripts/repair_probate_party_and_bad_parcel.py`:
  - Every write is now `_guarded_write`: one transaction per row, `lock_job_for_claim(job)`,
    then the action check UNDER the lock, then the existing guarded UPDATEs (they are the
    re-read), then commit.
  - A lead is SKIPPED (journal `skipped_action_linked`) when any of its pending rows has an
    `action_id`, or it has an open verdict (`quoted`, `newly_queued`).
  - A dry run takes no lock and writes nothing, but reports `would_skip_action_linked`.
  - The run-wide single commit is gone, so the claim lock is never held across the script's
    live county lookups.
  - The candidate queries now select `job_id`.
- `skip_trace_dispatcher.py`: comments only (`:284-290`, `_cancel_undeliverable_queued`'s
  docstring). Cancellation writes queue and result rows only; `released` is derived (S1, X1).
- Tests: 8 new REAL-DB tests in `tests/test_repair_probate_party_and_bad_parcel.py`:
  - an unowned lead repaired;
  - an action-owned trace untouched;
  - a `quoted` lead untouched;
  - a settled verdict doesn't block;
  - the write WAITS on another session's claim lock (two connections);
  - a dry run reports and writes nothing;
  - end to end through `repair_party` (the stale placeholder-name trace re-derived on the free
    lead, untouched on the owned one).
  - 29 passed. **Mutations 6/6 caught** (lock dropped, action check dropped, open verdicts
    narrowed, pending `action_id` ignored, dry-run check dropped, party write unguarded).
- **Found while building it (P1, MINE, fixed separately as #398):** since #393, any process
  whose FIRST import is `src.scrapers` (every ops script) dies with a circular ImportError:
  `routes/jobs.py` imported the planner at module top (the planner → `pierce_atip_owner` →
  `base_scraper` → `src.api` → the routers). The api and the worker survived by import order.
  #398 moves the import into the handler, and adds a fresh-interpreter import test over 10
  entry points + every Celery `include` module.
- **Codex diff review r1: NO-GO, 3 P2, all adopted.**
  1. Lock order: the script wrote results before pending rows, against the queue's order
     (pending, then results), which could deadlock a dispatcher tick. `_guarded_write` now
     locks the lead's pending rows (`FOR UPDATE`, by id), then the result, before any write.
     New test: while another session holds the pending row, the blocked repair holds NOTHING
     on the result (a `NOWAIT` lock succeeds).
  2. The lock test now proves the wait happens INSIDE `lock_job_for_claim` (a pass-through
     spy around the real function).
  3. `_PARCEL_RECOVER` is also guarded on `party_name`, because the recovered parcel is chosen
     from it.
  Now 30 passed; **mutations 8/8** (+ row locks dropped, + recovery party guard dropped).
- Local test DB upgraded 105 → 106 (#394's `jobs.breakdown_*`) via `scripts/migrate.py`.

### 2-0 MERGED + LIVE (2026-09-30): #400, merge `2b907bc1`
- Codex r2 GO; CI green; quiet all zeros; api/worker/beat SUCCESS on `2b907bc1`, clean boot.
- The #393 import-cycle hotfix shipped first as **#398** (`d68563ce`).

### 2a SPLIT (2026-09-30): 2a-i schema, 2a-ii the claim
- Migration 101's drift test pins its vocabulary tuple to `models.py`, so adding
  `unmatched_unbilled` also changes `tests/test_contact_lookup_schema.py`. That makes a 6th
  file.
- The S4-02 session's #399 is also changing `skip_trace_claim.py` right now.
- So 2a splits:
  - **2a-i** = migration 107 + `models.py` + the schema tests + this plan (4);
  - **2a-ii** (after #399 lands) = the claim's `action_id` / `held_ids` / action↔job join +
    its test + this plan (3).

### 2a-i BUILT (2026-09-30), before the Codex diff review
- **Migration 107** (`107_contact_lookup_action_link.py`):
  - `fk_pending_skip_trace_action_tenant` `(action_id, user_id)` →
    `contact_lookup_actions(id, user_id)`, ON DELETE NO ACTION, NOT VALID then VALIDATE;
  - the disposition CHECK re-added with `unmatched_unbilled` (NOT VALID, VALIDATE);
  - `contact_lookup_actions.quote_snapshot JSONB NOT NULL DEFAULT '{}'`.
  - Lock timeout 5 s, replay-safe. The downgrade refuses while `unmatched_unbilled` rows exist.
  - Applied locally and verified BY THE OBJECTS (FK `confdeltype='a'` validated; CHECK
    validated with the new value; column jsonb NOT NULL default `'{}'`).
- `models.py`: the FK in `__table_args__`, `quote_snapshot`, and `unmatched_unbilled` in
  `CONTACT_LOOKUP_DISPOSITIONS`.
- Tests (`test_contact_lookup_schema.py`, 75 passed):
  - the drift test pins 101 = models minus the new value, and 107 = models;
  - a pending row may name its own action, and NULL is unchecked;
  - another tenant's action and a missing action are REFUSED;
  - deleting a job, and a user, with action-linked pending rows succeeds (NO ACTION at
    statement end, V5);
  - an action that owns pending rows cannot be deleted alone;
  - `unmatched_unbilled` is refused to the API and allowed to the worker;
  - the API writes `quote_snapshot` once and can never change it (V6);
  - the snapshot defaults to `{}`.
- **Migration mutations 4/4 caught.** Each cycle ran a downgrade to 106, the mutated 107, the
  tests, then a restore (downgrade + replay exercised):
  - CASCADE instead of NO ACTION;
  - a non-composite FK;
  - the new value left out of the CHECK;
  - the snapshot nullable with no default.
- **Codex diff review r1: NO-GO (2 P2 + 2 P3, no P1), all adopted.**
  1. Replay object-verification: the FK is checked BY DEFINITION on
     `public.pending_skip_trace_rows` (an impostor is rebuilt), and an existing
     `quote_snapshot` must be jsonb NOT NULL DEFAULT '{}' or the migration aborts.
  2. The downgrade refuses while ANY action row exists (dropping the snapshot would destroy
     quote evidence).
  3. The lock docs now name the real levels:
     - ACCESS EXCLUSIVE for ADD COLUMN and the CHECK swap;
     - SHARE ROW EXCLUSIVE on `pending_skip_trace_rows` for the FK, held to commit;
     - `lock_timeout` bounds acquisition only.
  4. A test asserts both constraints are validated and defined as intended.
  - Verified locally against the real DB:
    - REPLAY on an applied schema (`stamp 106` + migrate) succeeds;
    - an IMPOSTOR FK (single-column, CASCADE) is rebuilt to the composite NO ACTION FK;
    - a WRONG column (nullable) aborts at 106, and once fixed it migrates to 107.
- **Codex r2: GO**, plus 2 P3s, both done:
  - the constraint test is scoped by `conrelid`;
  - real-DB replay tests run 107's REAL `upgrade()` through alembic `Operations` inside one
    rolled-back transaction: a replay on the applied schema; an impostor FK rebuilt; a
    malformed `quote_snapshot` refused.
  Mutation: with the impostor check disabled, the replay test FAILS. 15 tests for 107.
- **Codex r3: GO.** A P3 is accepted as a fact: the replay tests hold DDL locks inside their
  rolled-back transaction, which a PARALLEL run sharing the DB could block on. The suite runs
  serially (local rig and CI).
- **Regression: 726 passed, 0 failed** (every test file that writes pending rows or the
  ledger, 2 chunks). ruff clean. No type checker is configured.

### 2a-i MERGED + LIVE (2026-09-30): #402, merge `e9397f0f`
- Migration 107 was verified in PRODUCTION BY THE OBJECTS (read-only):
  - the FK is validated, `confdeltype='a'`, with the exact composite definition;
  - the CHECK is validated and includes `unmatched_unbilled`;
  - `quote_snapshot` is jsonb NOT NULL default `'{}'`;
  - 0 pending rows carry an `action_id`.
- api and beat logged "migrations applied" and booted clean.
- The worker's DSN reads an EMPTY `alembic_version`, so objects, not the version table, are
  the proof.
- Rebased once over #401 (AI-mode removal, no overlap); Codex r4 GO.

### 2a-ii BUILT (2026-09-30), before the Codex diff review
Branch `feat/lookup-1b2a-ii-claim-action` off `e9397f0f` (after #399's
`held_lookup_message`).
- `claim_skip_trace_rows(..., action_id=None)`:
  - With an action, every inserted row carries it. The INSERT JOINs
    `contact_lookup_actions a ON a.id = :action_id AND a.user_id = v.user_id AND a.job_id =
    v.job_id` (W6): an action of another job, another tenant, or none claims NOTHING.
  - With `None` the three SQL fragments are empty: the scrape path's statement is unchanged
    (a test asserts it never mentions an action).
  - `report["held_ids"]`: the held leads' result ids in the caller's order (W5), beside the
    unchanged `held` count. For a blocked account, all of them.
- Tests: NEW `tests/test_skip_trace_claim_action.py` (9, real PG). The 3 exact-dict
  `report` assertions in `test_audit4_paid_skip_trace_gate.py` now include `held_ids` (the
  contract changed on purpose). Files: claim, 2 tests, this plan = 4.
- **Mutations 5/6 caught.** Caught: the join dropped; its job check dropped; the action not
  written; trial held ids; blocked held ids. **The survivor is EQUIVALENT:** the join's
  tenant check is implied by its job check (`a.job_id = v.job_id` with `j.user_id =
  v.user_id`, and 101's FK `(job_id, user_id) -> jobs` makes an action's tenant its job's
  tenant). It is kept as the explicit belt.
- **Codex r1: GO**, plus a P3, done:
  - the scrape path's INSERT is now pinned to a GOLDEN statement + param-key set, captured
    from origin/main's UNMODIFIED claim and proven BYTE-IDENTICAL to this branch's with
    `action_id=None` (a diff of the two driver-level captures);
  - new: an action claim across insert chunks marks every row;
  - new: a lead settled mid-claim is withdrawn by OUR pending id, across chunks, with an
    action.
  11 tests; mutations unchanged (5/6, the survivor equivalent).
- **Regression: 825 passed, 0 failed** (all 27 files that touch the claim, pending rows or
  the ledger, 2 chunks). The first run caught the 3 exact-dict asserts, fixed as above.

### 2a-ii MERGED + LIVE (2026-09-30): #406, merge `8c9afb98`

### 2b BUILD SPEC (2026-09-30, branch `feat/lookup-1b2b-worker` off `11069d6a`, BEFORE the Codex consult)
The step "1b-2b" above, as amended by V1, V8/W7 and W1. This section only fills what the step
leaves open; where it differs from the step, the amendments decide. Nothing dispatches to
this task until 2d, so the merge changes nothing live EXCEPT the enrich extraction.

**Module `src/workers/contact_lookup_action.py`** (Celery `include`; imports the planner and
`enrich` INSIDE the task, the #393 lesson).
- `ACTION_TRANSITIONS`:
  - `dispatching → {running, expired}`;
  - `running → {claimed, failed, dispatching}` (`dispatching` = wait, or the reconciler's
    lease expiry);
  - `claimed → {settled}`;
  - `settled`, `failed`, `expired` → nothing.
- `VERDICT_TRANSITIONS`:
  - `quoted → {newly_queued, reused, already_answered, in_progress_elsewhere, ineligible,
    abandoned, excluded_*}` (an `excluded_*` only from a stricter current policy);
  - `newly_queued → {answered_hit, answered_miss, unmatched_billable, unmatched_unbilled,
    errored_unsubmitted, released}` (2c's);
  - every other disposition → nothing.
- `_move(db, action_id, frm, to, reason, *, lease_token=None, sets=...)`:
  - asserts the matrix (`IllegalTransition`, a programming error);
  - CAS `UPDATE … WHERE id AND status = frm [AND lease_token = :mine] RETURNING`, stamping
    `status_reason` and `status_changed_at`;
  - inserts the event in the same transaction;
  - returns False when the CAS matched nothing.
- `_set_verdicts(db, action, frm, {result_id: to}, *, reasons=None)`:
  - asserts every pair;
  - one bulk `UPDATE … FROM (VALUES …) WHERE disposition = frm`, and asserts it updated EXACTLY
    the given number of rows;
  - per-lead events only where the plan wants them (held, abandoned).
- **T1:** `_move(dispatching → running)` with a new token, `lease_expires_at = now() + 10
  min`, and `started_at = COALESCE(started_at, now())`. COMMIT. Nothing matched → no-op.
- **T2:**
  1. `SELECT … FOR UPDATE` the action;
  2. fence: `running` + my token + `lease_expires_at > now()`, else roll back, no-op;
  3. kill switch / token off → `running → dispatching`, lease cleared, reason `kill_switch`,
     COMMIT (O-B);
  4. `lock_job_for_claim(job)`;
  5. job gate: `status = 'done' AND _job_delivered_sql` (`:since` bound), by `(id,
     user_id)`. Else `failed` (`job_not_delivered`) + all quoted → `abandoned`;
  6. access gate: `normalize_plan(plan) ∈ SKIP_TRACE_ADDON_PLANS` AND
     `paid_lookup_access(read_access_rows(lock=""))` not blocked. Else `failed`
     (`plan_not_eligible` / `access_<value>`) + abandoned. The claim re-reads the user LOCKED;
     a block found there is per-lead `ineligible`;
  7. V1: `count(quoted rows of this action JOIN results ON id, user_id = action.user_id,
     job_id = action.job_id)` must == `quoted_count`. Else `failed` (`quoted_set_mismatch`)
     + abandoned;
  8. read the quoted leads as `(Result, enqueue_eligible)` tuples (Z3: `populate_existing`,
     same join, `ORDER BY created_at, id`);
  9. classify each tuple in order with `_verdict_for(result, enqueue_eligible, policy)` (D1
     below): the boolean is an ARGUMENT, and step 2 of D1 reads it;
  10. claim the payloads with `action_id` + `report`;
  11. write the verdicts and counts; `running → claimed`, `claimed_at`, the lease cleared;
  12. ONE commit.
- **Failure inside T2:** roll back, then in a NEW transaction `_move(running → dispatching)`,
  fenced by the token, lease cleared:
  - `ClaimUnenforcedError` → reason `claim_unenforced` + an ops alert;
  - a `lock_timeout` → `claim_lock_busy`;
  - anything else (incl. `SoftTimeLimitExceeded`) → `worker_error`, logged, then RE-RAISED.

  The reconciler (2c) re-drives until the 30-min deadline, then `expired`.
- **Task:** `lookup_contacts(action_id)`, a non-UUID → log, no-op. `soft_time_limit=240`,
  `time_limit=300`: below the 10-min lease, so the task dies before its lease could be taken
  over.

**D1 classification, first match wins** (pinned policy = snapshot `policy` AND current settings;
a missing key reads as False, i.e. the stricter one):

| Check | Verdict |
|---|---|
| `classify` IN_PROGRESS | `in_progress_elsewhere` |
| ALREADY_ANSWERED | `already_answered` |
| PREVIOUSLY_ATTEMPTED (errored, unknown) | `ineligible` |
| the enqueue's SQL predicates fail now (`property_address` NOT NULL, `actionable_condition`, `skip_trace_eligible_condition`): over quota, superseded, … (Y3/Z1: the `enqueue_eligible` column) | `ineligible` |
| an excluded bucket | its `excluded_*` |
| charged-unanswered (the SHARED helper; it settles the result `errored` exactly as the enqueue does) | `already_answered` |
| valid cache hit (the SHARED helper, an ORM copy) | `reused` |
| else a claim payload | — |
| the claim WON it | `newly_queued` |
| `held_ids` | `ineligible` + an event (`trial_allowance`, or `access_<value>`) |
| refused (unwritable) or lost a race | a verdict from its current status: `queued`/`submitted` → `in_progress_elsewhere`, `hit`/`miss` → `already_answered`, else `ineligible` |

Counts:
- `newly_queued_count` / `reused_count` are the verdict counts;
- `claimed_count` = newly_queued + reused (leads this action answered or bought);
- `tracerfy_credits` = the credits of the newly queued rows' trace types;
- `billable_rows` stays 0, for 2c.

**The enrich extraction** (behaviour-preserving):
- `settle_charged_unanswered(db, user_id, rows) -> (kept, settled_n)`: the enqueue's inner
  closure, lifted verbatim to module level;
- `copy_cached_answer(db, user_id, rec, payload) -> bool`: the cache read, the TTL check and
  the ORM copy.

The enqueue calls both in the same places with the same arguments. The planner parity tests,
the enqueue tests and the 2a-ii golden INSERT pin it.

**Tests** (`tests/test_contact_lookup_action.py`, real PG + Redis; actions seeded through SQL
exactly as 2d will write them):
- every row of D1;
- the matrix;
- T1/T2 fencing (a lost lease → no-op, nothing bought);
- a kill between T1 and T2 leaves `running` + an expiring lease;
- redelivery / double delivery → one set of rows;
- the action racing the real scrape enqueue on one lead (two threads) → one row;
- non-quoted, other-tenant and other-job ids never bought;
- the trial cap with `held_ids` recorded;
- kill switch → waits;
- job not done → abandoned;
- V1 mismatch → failed;
- the lease cleared at claim;
- parity with `_enqueue_skip_trace_rows` on the same seeds;
- the failure paths;
- counts.

Mutation runner per the 2a pattern.

**Pre-existing, found while reading (to verify in PRODUCTION before raising, read-only):**
the claim's withdrawal path `DELETE`s its own uncommitted pending rows
(`skip_trace_claim.py:775`), but no script or migration grants `DELETE ON
pending_skip_trace_rows` to `bridgeleads_system` (`provision_rls_roles.sql:306` grants only
`SELECT, INSERT, UPDATE` on all tables). If production matches, that race path raises
`permission denied` and rolls back the WHOLE claim:
- on the enqueue it is caught and re-raised as a failed enqueue; the leads stay
  `not_attempted`;
- on the action it is the `worker_error` wait path.

So it costs availability, not money. The check (`has_table_privilege`) is ready in this
session's scratchpad (`priv_check.py`) and is blocked on `railway login`.

### Codex pre-code consult r1 on 2b (2026-09-30): PLAN: REVISE, 1 P1 + 3 P2, all adopted
Output: `<scratchpad 4ebfb689>/codex_2b_consult_r1_out.txt`. Codex found these SOUND:
- the tenant/job fencing;
- the V1 check;
- the snapshot-AND-current policy;
- the extraction (as long as the helpers stay non-committing and the action uses
  `populate_existing`);
- the counts;
- the matrices.

It said to ACCEPT trace-type drift between quote and claim (billing is per row, and the cache
key carries the current subject + trace type), and to test both directions. These AMEND the
build spec:
- **Y1 (P1, pre-existing, NOT 2b's code) the missing `DELETE` grant is CONFIRMED in the
  repo.** `provision_rls_roles.sql:305-326` grants `bridgeleads_system` only `SELECT, INSERT,
  UPDATE`, and `scripts/verify_worker_delete_grants.py:36-47` does not list the table. Nothing
  double-buys, but the claim's lost-race withdrawal raises and rolls back the whole claim, for
  the live enqueue and the action alike.
  - It becomes **owner item O-D, a HARD GATE before 2d** (beside O-C). First verify production
    read-only (`priv_check.py`); then either grant `DELETE ON pending_skip_trace_rows` to the
    system role (provision script + verifier + an applied grant), or redesign the withdrawal.
  - 2b ships idle, so it is not blocked. 2b's `worker_error` wait path is what the action does
    meanwhile.
- **Y2 (P2) hard-kill recovery is the LEASE, not redelivery.** `task_reject_on_worker_lost` is
  not set (`src/workers/__init__.py:115-119`), and setting it would not help anyway: after T1
  the action is `running`, so a redelivery no-ops by design. The recovery guarantee is 2c's
  lease expiry (`running → dispatching`) + re-publish. This is written in the module docstring
  and pinned by the kill-between-T1-and-T2 test. The global Celery config is NOT changed (it
  would touch every live task).
- **Y3 (P2) D1 has ONE precedence, and the enqueue's SQL predicates gate the payload:**
  1. `classify`'s status buckets (IN_PROGRESS / ALREADY_ANSWERED / PREVIOUSLY_ATTEMPTED);
  2. the enqueue's SQL predicates (`property_address` NOT NULL, `actionable_condition`,
     `skip_trace_eligible_condition`), evaluated IN the quoted-set query → `ineligible`;
  3. `classify`'s address/policy buckets → `excluded_*`;
  4. charged-unanswered (it only ever sees `not_attempted` rows) → `already_answered`;
  5. the cache → `reused`;
  6. the claim.

  No lead failing 1-3 reaches `claim_skip_trace_rows`. Each step is tested.
- **Y4 (P2) the pinned policy is parsed strictly:** `snapshot["policy"][key] is True`, AND the
  current settings. Missing, non-bool or `"true"` → False. Tests:
  - a missing key;
  - a string value;
  - trace-type drift both ways;
  - quote-set shrinkage (a quoted result deleted → the V1 mismatch);
  - every `newly_queued` terminal disposition legal in the matrix.

### Codex consult r2 on 2b (2026-09-30): PLAN: REVISE, 1 P1 + 1 P2, both adopted
Y1, Y2 and Y4 are closed; gating 2d on O-D is accepted.
- **Z1 (P1) a predicate is a COLUMN, never a filter.** The quoted-set query selects every
  quoted lead (the V1 join, nothing else in its WHERE) plus `enqueue_eligible` = the three
  enqueue predicates as ONE boolean expression in the select list. A lead failing it gets the
  verdict `ineligible`. Filtering would leave it `quoted` with no verdict, against "every quoted
  lead gets exactly one verdict".
  - The worker asserts that its verdict map covers EXACTLY the quoted set before writing.
  - `_set_verdicts` asserts the row count.
- **Z2 (P2) O-D names every grant source.** The grant goes into ALL three, kept consistent:
  - `scripts/provision_rls_roles.sql`;
  - `scripts/verify_worker_delete_grants.py`;
  - `scripts/_cutover_step2_grants_policies.py` (`_SYSTEM_DELETE_TABLES`, `:75-95,163-171`).

**r3 (2026-09-30): REVISE, 1 P1 + 1 P2, both fixed IN PLACE:**
- the D1 table now lists the predicate row BEFORE the `excluded_*` row (Y3's order);
- **Z3** the query shape is
  `select(Result, and_(Result.property_address.isnot(None), actionable_condition(),
  skip_trace_eligible_condition()).label("enqueue_eligible"))`:
  - `.join(ContactLookupActionResult …quoted…)`;
  - `.where(Result.user_id == action.user_id, Result.job_id == action.job_id)`;
  - `.order_by(Result.created_at, Result.id)`, `populate_existing`;
  - rows are `(Result, enqueue_eligible)` tuples. A NULL from the expression counts as
    not eligible (`is True`).

  Tests: one lead per predicate failing (over quota, superseded duplicate, no property AND no
  mailing, NULL `property_address`), each → `ineligible`, never claimed.

**r4: REVISE, 1 P2** (T2 did not say the boolean is passed into classification): fixed in
place (T2 steps 8-9, `_verdict_for(result, enqueue_eligible, policy)`). **r4's P2 is CLOSED.**

**r5: REVISE, 1 P1: REJECTED, with reasoning.**
- **The claim.** Codex: the predicates read at T2.8 are not re-checked atomically by the claim
  INSERT (the 1b contract at `:616` says "the eligibility predicate"). A lead can go over
  quota or superseded between T2.8 and the INSERT (writers exist: `dedup.py:1051`,
  `trustee_sale_finalize.py:189`).
- **Why it's rejected:**
  - **The money boundary is SUBMISSION, and it already re-checks atomically.**
    `_partition_still_deliverable` (`skip_trace_dispatcher.py:1349-1418`) reads the Results
    `FOR SHARE SKIP LOCKED`, re-checks `skip_trace_eligible_condition` and over-quota, and
    holds that lock until the `queued → submitting` claim commits. The pre-submit sweep
    `_cancel_undeliverable_queued` (`:900-925`) cancels the same predicates.
  - So a lead that turns ineligible after the claim is cancelled, never charged, and 2c
    derives `released`.
  - The live enqueue has the IDENTICAL claim-time window (`enrich.py:2560-2719`: re-read, then
    claim) and depends on the same backstop. Parity is the contract.
  - Adding predicates to the shared claim INSERT would change the scrape path's golden
    statement (2a-ii) and make `skip_trace_claim.py` a 6th file, for a window that cannot
    spend money.
- **Test added to the 2b list:** an action-claimed lead made over quota after the claim is
  cancelled by the real `_cancel_undeliverable_queued` before any submission.

**r6 (2026-09-30): `PLAN: GO`, no new findings. The r5 rejection HOLDS** (Codex verified it in
code):
- eligibility, over-quota and job delivery are re-checked under lock at the submit boundary;
- the address half is only ever FILLED after delivery, never emptied;
- pre-submit validation rejects missing provider fields.

O-D stays the pre-2d gate. Outputs: `<scratchpad 4ebfb689>/codex_2b_consult_r{1..6}_out.txt`.

### 2b BUILT (2026-09-30), before the Codex diff review
- **NEW `src/workers/contact_lookup_action.py`:**
  - `ACTION_TRANSITIONS` / `VERDICT_TRANSITIONS`;
  - `_move()`: matrix-checked CAS + event; the lease is set only on entering `running` and
    cleared on every other move;
  - `_set_verdicts()`: matrix-checked; exact row count, or it raises;
  - `_claim()` (T2) and `run_action()` (T1, T2, the failure → wait path);
  - the `lookup_contacts` task (soft 240 s / hard 300 s).
  - **One change from the spec, found by mutation:** V1 is `total == in_job == quoted_count`
    over ALL of the action's quoted rows. A job-scoped count alone passes when a quoted row
    outside the job happens to keep the count equal, and that row would stay `quoted` forever
    with no verdict. Tested.
- `src/workers/__init__.py`: the module is in Celery `include`. `test_import_cycles` (27)
  covers it in a fresh interpreter.
- `src/workers/tasks_helpers/enrich.py`: `settle_charged_unanswered()` and
  `copy_cached_answer()` lifted to module level, verbatim. The enqueue calls them where it
  used to inline them. The now-unused local imports are dropped.
- **Tests** (`tests/test_contact_lookup_action.py`, 51, real PG + Redis):
  - every D1 row in one action, incl. cache entries on in-flight / errored leads (never
    overwritten);
  - PARITY with the real `_enqueue_skip_trace_rows`, ATIP both ways;
  - the pinned policy: 6 cases, incl. a string `"true"` and missing keys;
  - trace-type drift both ways;
  - the kill switch, then the re-drive; an empty token;
  - job not delivered (3 statuses); plan / frozen / ended;
  - the trial cap with held events;
  - V1: shrinkage, another job, and an outside row with a matching count;
  - kill between T1 and T2 → recoverable; lost fence ×3;
  - redelivery; two concurrent deliveries;
  - the race with the real scrape enqueue (one row per lead);
  - a lost race via a pass-through spy (×3 statuses);
  - a busy claim lock → wait;
  - an injected error → wait + re-raise, nothing written;
  - over quota after the claim → cancelled by the real dispatcher sweep;
  - the matrices.
- **Mutations 25/26 caught.** The survivor is EQUIVALENT: dropping the job scope from the
  quoted-set READ. V1 has just proven every quoted row is inside the job, in the same
  transaction, under the action row lock. Kept as the belt.
  - The first run caught 21/25. Of its 4 survivors:
    - one exposed the V1 gap above;
    - two showed the cache copy could overwrite an in-flight lead if `classify` were bypassed
      (tests now seed cache entries on those leads);
    - one was a matrix test passing for the wrong reason (the lease rule raised first; it now
      matches the matrix message).
- **Regression: 974 passed, 0 failed** (all 30 files touching pending rows / claim / ledger /
  the enqueue, 4 chunks), plus 67 in the 4 files that read Celery `include`, and 27 import
  cycles. The first 15-file chunk overran 590 s (killed; no stray pytest left), so the chunks
  are now 7-8 files. ruff clean. No type checker is configured.

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

## Safety PR: Alembic can never reach production from a test or a stray CLI run (PLAN, 2026-09-27)

The Deferred bullet below, taken now. Same class as the two production wipes.

**How a migration reached a database BEFORE #370 (the problem this section fixed):**
- Boot: `start.sh` -> `scripts/migrate.py`, which reads `DATABASE_URL_MIGRATE or DATABASE_URL_SYNC`
  from the process env (it never reads `.env`) and hands `env.py` its own connection, so env.py's
  URL choice is unused there.
- CI: bare `alembic upgrade head` twice (Test job on the test DB; the production "Run
  Migrations" job with the `DATABASE_URL_SYNC` secret), always with explicit env vars, no `.env`.
- Anything else (a developer's bare `alembic ...`, any test or script calling
  `alembic.command.*` without a connection): `env.py` calls `load_dotenv()`, which searches
  upward from `alembic/` (NOT the cwd), so in the OneDrive checkout it loads the PRODUCTION
  `.env`, and then prefers `DATABASE_URL_MIGRATE` (the prod owner role). The test guard pins
  only `DATABASE_URL`/`_SYNC`, so a test that ran Alembic would migrate or downgrade production.
  No test does this today (103's replay deliberately avoids env.py); nothing stops the next one.

**Codex pre-code consult (PLAN: REVISE, all adopted):** pin (not clear) and removing
`load_dotenv()` with NO opt-in both confirmed [P1]; run the env.py tests in a SUBPROCESS [P2];
a belt in env.py under `ENVIRONMENT=test` [P2]; migrations must never get a connection from
`Settings` (053 reads settings for keys only; 027/028 mention DATABASE_URL in docstrings only)
[P2]; stale comments [P3]. More than 5 files, so two phases.

**Codex consult round 2 (REVISE, adopted):** the belt must CLASSIFY the target, not just match
one env var (else `ENVIRONMENT=test` with both URLs on production passes) [P1]; compare database
IDENTITY (host, effective port, database), rejecting routing overrides in the query [P1]; an AST
check of the migrations, not a text scan [P2]; the missing-URL error before env.py imports the
models [P2].

**Phase A: code (5 files)**
- [x] `src/db_safety.py` (NEW, dependency-free, `src/__init__.py` is empty): the classifier
      moved out of `tests/_db_safety.py` unchanged (name suffix `_test`/`_testing`; explicit
      host, local or `TEST_DB_HOST_ALLOWLIST`; routing query keys refused) plus
      `db_identity(url) -> (host, port, database)` (port None when absent; `classify()`
      separately requires an explicit port).
- [x] `tests/_db_safety.py`: imports the classifier from there (behaviour unchanged);
      `enforce_test_database()` also PINS `DATABASE_URL_MIGRATE` to the validated
      `TEST_DATABASE_URL_SYNC`. Pin, never delete: an absent key is exactly what `load_dotenv()`
      and pydantic's `env_file=".env"` refill from the file; a present one they leave alone.
- [x] `alembic/env.py`: no `.env` load at all. A connection handed in (migrate.py) is used as
      today. Otherwise the URL comes only from the process env (`DATABASE_URL_MIGRATE or
      DATABASE_URL_SYNC`); neither set -> a clear error, raised before `src.db.models` is
      imported. BELT, when `ENVIRONMENT=test`: the target (the URL, or the handed-in
      connection's `engine.url`) must classify as a test database AND have the identity of
      `TEST_DATABASE_URL_SYNC`, which must itself classify; else refuse: before connecting on
      the bare-URL path, before `run_migrations()` on the handed-in connection.
      CI's Test job passes (`ENVIRONMENT=test`, local `bridgeleads_test`); production never
      sets ENVIRONMENT=test.
- [x] `tests/test_db_safety.py` (new; there are no guard tests today):
      (a) the guard pins `DATABASE_URL_MIGRATE` over a prod-looking value, and still refuses
          what it refused before (pure, monkeypatched env);
      (b) SUBPROCESS, cwd=tmp_path, PYTHONPATH=worktree, sanitized env: the REAL `alembic/`
          copied to `tmp_path/alembic` with a trap `.env` in it naming
          `DATABASE_URL_MIGRATE=...@dotenv-prod.invalid/...`, the variable absent from the
          child's env, `alembic.command.current()` on a Config with no ini file: it reads the
          TEST database. Today's env.py fails it (tries `dotenv-prod.invalid`);
      (c) SUBPROCESS: no URL and no connection -> the clear error;
      (d) SUBPROCESS: `ENVIRONMENT=test` and a target that is not a test DB (both
          `DATABASE_URL_MIGRATE` and `TEST_DATABASE_URL_SYNC` on `prod.invalid/postgres`) ->
          refused before connecting;
      (e) AST over `alembic/versions/*.py`: no import or call of an engine factory
          (`create_engine`, `create_async_engine`, `engine_from_config`, aliases included), no
          `src.db.session` import, no executable read of `DATABASE_URL*` (docstrings and
          comments ignored).
- [x] `alembic.ini`: the stale comment (names only DATABASE_URL_SYNC).
- Mutations: guard deletes instead of pinning; `load_dotenv()` restored; belt removed;
  identity check removed. Each must fail a test.

**Codex consult round 3 (REVISE):**
- [P1] adopted: the classifier also refuses the `service` query key (pg_service.conf can
  redirect) and REQUIRES an explicit port (else `PGPORT` can redirect; PGHOST/PGDATABASE are
  already moot: host and database are required explicitly). CI and local test URLs name :5432.
- [P2] adopted, tests: (f) both URLs classify as test DBs but differ in host / port / database
  -> refused (so the identity check is proven, not just the classifier); (g) a handed-in
  connection whose `engine.url` masks the password -> identity compares host/port/database
  only; (h) an allowlisted remote host passes, `TEST_DB_HOST_ALLOWLIST` kept in the child env.
- [P1] NOT adopted, reason sent back to Codex: "migrate.py connects before env.py inspects the
  handed-in connection". Under `ENVIRONMENT=test` a wrong target is then only CONNECTED to and
  advisory-LOCKED (session lock, released on close; no data read or written); env.py refuses
  before `run_migrations()`, so no DDL can run. Guarding migrate.py too buys no data safety
  and adds a sixth file. **Codex round 4: reason ACCEPTED.**

**Codex consult round 4 (REVISE, adopted):** [P1] libpq still routes elsewhere with host, port
and dbname all explicit when `PGHOSTADDR` is set, or a service file (`PGSERVICE`,
`PGSERVICEFILE`, `PGSYSCONFDIR`) supplies `hostaddr`. `src/db_safety.py` gets
`ambient_redirects()` (those four, when non-empty); BOTH the test guard (the suite's own engine
has the same exposure) and env.py's belt refuse when it is non-empty. [P2] tests: (i) each of
the four set -> env.py refuses before `run_migrations()`, on the bare-URL path AND on the
handed-in-connection path; the guard refuses them too.

**Codex consult round 5 (REVISE; rounds 1-4 confirmed closed):**
- [P1] `docker-compose.yml`'s `migrate` service reads `env_file: .env`, so a production `.env`
  reaches Alembic directly, whatever env.py does. WIDER than Codex framed it: `api`, `worker`
  and `beat` read the same `env_file`, so `docker compose up` from a checkout whose `.env` is
  production runs the WHOLE local stack (scrape, spend, migrate) against production. That is a
  dev-stack design decision (what config the local stack reads), so it goes to the OWNER as its
  own PR, not folded into this one.
- [P2] `run-audit-tests.sh` should unset the four ambient variables before pytest (a developer
  with legitimate libpq settings would otherwise be refused), and CI's test job can set them
  empty. Phase B.

**Phase A: MERGED AND LIVE, #370 `2b397c82` (2026-09-27 12:32Z).** All five items above done as
written. Diff review r1 NO-GO, fixed: [P1] `set_main_option()` is ConfigParser interpolation, so
a percent-encoded password crashed env.py before the belt (belt now first, `%` escaped); [P2]
the allowlist test did real DNS (now 127.0.0.2:1 with connect_timeout); [P2] the libpq variables
now tested on BOTH env.py paths; [P2] the AST scan hardened (aliases, getattr/import_module, any
`src.db` import, concatenation, f-strings, real docstrings only) with probe tests; [P3] abort
message. r2: two "P1s" in jobs.py / rate limits were `origin/main` having moved (#368) under a
two-dot diff; rebased. r2 also: the teardown check re-tests the libpq variables, the scan refuses
aliased dynamic imports. r2's migrate.py P2 not adopted (same reason Codex accepted in consult
round 4; accepted again in r3). **r3: GO, no findings.** Rebased twice more (#369, #371; branch
protection requires an up-to-date branch), CI green each time. 42 tests, 10 mutations caught.
After deploy: worker and api boot migrations ran through the new env.py; CI's production "Run
Migrations" (bare alembic, ENVIRONMENT unset) succeeded.

**Phase B: docs (this PR):** this plan + review, `.claude/rules/testing.md`, the stale comments
in `tests/conftest.py` and `tests/test_pending_skip_trace_frontier_index.py`,
`docs/BUILD_JOURNAL.md` (ii-c-2 + #370).

## Local-env PR: the local stack can never run on production config (PLAN, 2026-09-27)

Owner-approved as its own PR (Codex found it in the safety-PR consult, round 5).

**How production config reaches the local stack today** (`docker-compose.yml`, "LOCAL
DEVELOPMENT ONLY"): `api`, `worker`, `beat` and `migrate` all read `env_file: .env`, and `api`/
`worker`/`beat` also mount `.:/app`, so pydantic's `env_file=".env"` reads the host `.env` inside
the container even without `env_file:`. The OneDrive checkout's `.env` IS production, so
`docker compose up` there would scrape, spend and migrate against production. (The image itself
is clean: `.dockerignore` excludes `.env`.) Facts: Docker is not installed on this machine, and
the stack cannot have worked against its own Postgres anyway: `src/db/session.py` rewrites
`:5432/` -> `:6543/` on the sync URL unconditionally, so a `postgres:5432` DSN dials 6543.

**Change (5 files):**
- [x] `docker-compose.yml`:
      - every app service reads `env_file: .env.local` (created from `.env.example`; `.env.*` is
        gitignored), never `.env`;
      - `environment:` PINS the four infrastructure endpoints to the compose containers
        (`environment:` beats `env_file:`): `DATABASE_URL`, `DATABASE_URL_SYNC`,
        `DATABASE_URL_MIGRATE` (port-less host `postgres`, so the 6543 rewrite cannot fire) and
        `REDIS_URL` (Celery broker + backend); `ENVIRONMENT=development`;
      - `api`/`worker`/`beat` mount `./src` and `./main.py` only (hot reload), not `.:/app`, so
        no `.env` exists in the container for pydantic to find;
      - postgres/redis passwords are literal throwaway local values (NO interpolation, per
        consult round 1 below; this line first proposed `${LOCAL_*}`), ports bound to 127.0.0.1.
- [x] `tests/test_local_compose_env.py` (new; Docker is not here to render the config, so the
      YAML is the thing tested): no service reads `.env` (env_file or a mount of `.`/`.env`);
      every service that runs app code pins all four endpoints to the compose hosts; the sync
      URLs carry no `:5432/`; no interpolated variable a production `.env` defines
      (`DATABASE_URL*`, `REDIS_URL`, secrets) is referenced; ports loopback-only.
- [x] `run-audit-tests.sh`: `unset PGHOSTADDR PGSERVICE PGSERVICEFILE PGSYSCONFDIR` before pytest
      (the guard now refuses them).
- [x] `CLAUDE.md` Setup: `cp .env.example .env.local`; compose never reads `.env`.
- [x] this plan + review.
- Mutations: `env_file: .env` restored on one service; a `.:/app` mount restored; one pinned
  URL removed; a `:5432/` sync URL. Each must fail the test.

**Codex pre-code consult (PLAN: REVISE), reconciled:**
- [P1] adopted: NO `${...}` interpolation anywhere in `docker-compose.yml` (compose reads the
  project `.env` to interpolate): the local-only container passwords are literal throwaway
  values (containers bound to 127.0.0.1); the test rejects any `${`.
- [P1] already planned, now exact: all three DB URLs host `postgres`, NO port.
- [P2] adopted: the test also asserts no null `environment:` entries (a null passes the host
  value through), exact `env_file: .env.local` on app services, only the `./src` and
  `./main.py` mounts, loopback-only ports, no build args/secrets, and `.dockerignore` excluding
  `.env` / `.env.*` except `.env.example`.
- [P2] adopted: `migrate` runs `python scripts/migrate.py` (as production boot does) and
  `api`/`worker`/`beat` wait for it (`service_completed_successfully`).
- [P1] "blank every third-party credential (R2, Stripe, Resend, Anthropic...) and disable paid
  integrations" PARTLY adopted: compose pins the three paid switches OFF
  (`SKIP_TRACE_ENABLED`, `CAPTCHA_ENABLED`, `REGRID_ENABLED`, all default off in settings), blanks
  `TRACERFY_API_TOKEN`, and points `FRONTEND_URL` / `API_BASE_URL` at localhost; a
  `.env.local` cannot override `environment:`. NOT blanking Stripe / R2 / Resend / Anthropic:
  `.env.local` is a file the developer creates on purpose from `.env.example`, and local
  billing / export / email work needs their test-mode keys; the ACCIDENT this PR closes is the
  existing production `.env` being picked up by name. Reason sent back to Codex.
- **Codex round 2: partial adoption ACCEPTED.** Two more switches, adopted: [P1]
  `AI_ENRICHMENT_ENABLED` (DEFAULTS TO TRUE; with any `ANTHROPIC_API_KEY` parcel enrichment calls
  Claude, live-billed) pinned `false`; [P1] `RETENTION_PURGE_ENABLED=false` +
  `RETENTION_PURGE_DRY_RUN=true` pinned (the purge irreversibly deletes DB and R2 PII). The test
  asserts every pinned switch on every app service.

**Codex diff review r1 (NO-GO), fixed:** [P1] a fresh stack would not boot: `.env.example`'s
placeholder `SECRET_KEY` is refused by settings and migration 053 imports settings. Now pinned:
a throwaway local `SECRET_KEY` (which also stops a production key in `.env.local` from minting
JWTs production would accept), blank `FIELD_ENCRYPTION_KEY` / `BLIND_INDEX_KEY` /
`TRACERFY_WEBHOOK_SECRET` (blank falls back outside production), `PII_ENCRYPTION_STRICT=false`;
[P2] `ALLOWED_ORIGINS` pinned to localhost (a local frontend was refused); [P2] the claim narrowed
to what is true: the checkout's `.env` is never consumed, and `.env.local` cannot reach a
production database/Redis, use a production key, or enable anything paid/destructive (its
third-party keys remain the developer's); [P2] the test now checks exact URLs, long-syntax
mounts and ports, every health dependency, and rejects `network_mode`; [P2] docs: this plan's
stale interpolation line, CLAUDE.md's Alembic wording and `docker compose`.

**Codex diff review r2 (NO-GO), reconciled:**
- [P1] adopted: NO committed signing key (`.claude/rules/security.md`: no secrets in code), so
  r1's pinned throwaway `SECRET_KEY` is removed; CLAUDE.md's setup has the developer generate a
  local one into `.env.local` (the example's placeholder is refused at boot, loudly); the test
  asserts `SECRET_KEY` is NOT in `environment:`. The blank key pins stay (blank is not a secret).
- [P1] wording adopted: the header now claims only what is pinned (production database/Redis
  unreachable, the listed paid/destructive switches off), and says `.env.local`'s SECRET_KEY and
  third-party keys are used as given.
- [P1] "pin Stripe / R2 / Resend empty" NOT adopted: the same decision Codex ACCEPTED in consult
  round 2 (a deliberately created `.env.local` with test-mode keys is how billing, exports and
  email are developed locally; the accident this PR closes is the checkout's `.env`).
- [P2] migration 053 refuses a blank blind-index key on a database holding pre-053 users: a new
  local database has none; noted in the compose comment. [P3] blank Tracerfy webhook secret =
  503 on the webhook routes, as skip trace is off: noted.
- Logged, not here: `main.py` always also allows the production CORS origins (app code, not the
  local stack); `GIS_ENRICHMENT_ENABLED` / `OWNER_RECOVERY_ENABLED` / `PROPERTY_RECOVERY_ENABLED`
  for deterministic local runs (not a safety issue).

**Logged, not here:** CI test job setting the four libpq variables empty (runner defense; GitHub
runners set none); `scripts/bootstrap.sh`'s "Copy .env.example to .env" wording (a production
setup script that reads no `.env`); `docker-compose.prod.yml` (dormant, already audit #3 S3-53
"delete dormant compose"); `.env.example`'s own comments (Claude's reads are denied: owner).
- Out of scope, logged: pydantic `env_file=".env"` still loads other PRODUCTION secrets into a
  test run started from the OneDrive checkout (landmine `bare_pytest_uses_prod_env`); four
  manual ops scripts call `load_dotenv()` on purpose.

## Deferred (logged, not in Phase 1)
- ✅ **DONE: #370 `2b397c82` (2026-09-27), see "Safety PR" above.** Original note:
  **SAFETY, own small PR, soon (found in the ii-c-1 review, 2026-09-27):** `alembic/env.py`
  calls `load_dotenv()` and prefers `DATABASE_URL_MIGRATE` over `DATABASE_URL_SYNC`. The test-DB
  guard (`tests/_db_safety.py`) pins only `DATABASE_URL`/`_SYNC`, so ANY test or script that
  invokes Alembic through env.py on a machine whose `.env` names a production
  `DATABASE_URL_MIGRATE` would migrate (or downgrade) PRODUCTION. Same class as the two prod wipes.
  Fix: the guard also pins (or clears) `DATABASE_URL_MIGRATE`; env.py stops loading `.env`
  implicitly for non-boot invocations. 103's tests avoid env.py for this reason.

Lookup ledger (Phase 2), canonical leads (Phase 3), DNC flag is never set
and exports suppress nothing, advanced = 2 credits vs 1 billed row (the D2 follow-up; the gap is
recorded per action so it can be priced later), re-send email, stale R2 object and batch combined
export, per-row checkboxes.

**Pre-existing, found by Codex round 14, NOT introduced by this work:** the in-flight hold key is
tenant-scoped (`skip_trace_dispatcher.py:737-745`) while vendor attribution is global
(`tracerfy_ingest.py:585-608`), so two TENANTS with different owners at one address can already
today be submitted together, refused by `_attribution_is_safe`, and both charged without an
answer. Phase 1a's global `_submission_collision_key` closes this as a side effect, which is why
that key is deliberately not tenant-scoped. Worth a read-only prod count of how often it has
fired (pending rows that ended `unmatched` sharing an address key within one queue id).

## Review

### Phase 1a: MERGED AND LIVE (done 2026-09-20, merged + cut over in production)

Six commits on `feat/lookup-contacts-action`, off `origin/main`. Nothing pushed, no PR, no
Tracerfy credits spent, production untouched.

| | |
|---|---|
| `5ddf1cc` | `lookup_subject_key` + `submission_collision_key` + the two wrappers, 24 tests |
| `01b5ba0` | all five call paths switched, migration 098, legacy read deleted |
| `b3d5d00` | 14 integration tests, ops scripts, cutover runbook |
| `c181059` | Codex diff-review fixes (attribution multiplicity P1, restart-safe migration) |
| `5ba5381` | Security Master Review, dead legacy surface removed |
| `3bd966e` | plan checkboxes + the pre-existing local Stripe failures |

**What changed, in one line:** reuse is keyed on WHO an answer was bought for, not just where,
so an heir's lead can no longer be served the deceased owner's phone.

**What it costs.** Legacy cache rows go inert, so a repeat address may be paid for once more
inside the remaining 90 days of its entry; that self-heals, since the TTL is 90 days anyway.
Leads settled before 098 stop donating contacts to duplicate re-scrapes (NULL hash fails
closed) and fall through to the v2 cache read, which is free whenever the same subject really
was traced before.

**The plan was wrong in two places**, both found by consulting Codex BEFORE writing code, and
both recorded above as 14-A and 14-B. 14-A would have double-charged the customer and answered
nobody. 14-B could not be fixed without a migration, which the plan had ruled out. Rounds 1-13
missed both because they only ever examined the key itself, never the round trip.

**Three things caught in review, two of them my own errors:**
1. I applied the submission key across batches as well as within one. Attribution is scoped to
   a single `tracerfy_queue_id`, so that bought nothing and put one tenant behind another
   tenant's lookup. `test_another_accounts_lookup_never_holds_or_answers_mine` caught it.
2. Codex returned NO-GO on a P1 my own fix had made worse: `_attribution_is_safe` short-circuited
   before checking answer multiplicity, and the submission key turned that early return from a
   corner into the normal path. An existing test had the bug pinned as correct behaviour.
3. One of my tests passed both clean AND mutated, so it was exercising the wrong pass entirely;
   rewritten. Another was vacuous and was deleted, not dressed up.

**Verification.** 540 passed across the skip-trace/ingest/enrichment/reconciliation area; 191
passed on a second isolated database after the security pass; ruff clean; migration applied and
verified BY THE OBJECTS (column + index), never by `alembic_version`. Security Master Review:
0 Critical, 0 High, GO, two consecutive clean passes. The mutation tests are the evidence that
the important assertions are real. **Not yet done:** a clean full-suite pass on the final
commit (the run was killed at ~76% for host memory; the only failures seen were the nine
pre-existing Stripe ones, proven identical at `origin/main`).

**Before a PR:** rebase onto `origin/main`, which moved twice during the session (now
`8ba7bf8`).

### Phase 1b-0: MERGED AND LIVE (done 2026-09-20; merged 2026-09-22 as `0074196`, migration 100 applied in production)

Hardening only. No API route, no schema, no new dependency, no frontend.

| | |
|---|---|
| `d4bd021` | shared `ON CONFLICT` claim + migration 100 + the 15-1 regression test |
| `5babf77` | Codex review 1: the claim stops trusting its payloads, or the index existing |
| `fa20d60` | Codex review 2: enforcement per caller; `action_id` removed as dead code |
| `4f05c08` | Codex review 3: the claim withdraws its own losers, so the race has no residue |
| `4808441` | Codex review 4: the DB is the arbiter; job advisory lock stops the double enqueue |
| `28afa5a` | Codex review 5: the backfill was the other writer; the lock was released early |
| `5e70ecb` | Codex review 6: the backfill locks before it reads, and commits per job |
| `d1a6011` | Security pass 1: fail closed on the money invariant; pin the lead to its job |

**In one line:** one lead can hold only one active skip-trace claim, and a conflict now costs
only itself instead of silently discarding a whole job's lookups.

**What it cost to get right.** The plan-level consult (round 15) found nine P1s before a line
was written. The diff review then returned NO-GO **six times** before PASS. The Security Master
Review then ran **twelve passes**, finding a Critical, seven Highs and a long tail of Mediums.
Every one was a way to charge a customer twice, strand paid work, or lose leads silently.

**The three findings that only a CHANGE OF ANGLE could produce.** After pass 6 came back
completely clean, the temptation was to stop. Passes 7, 8 and 10 each found something the
previous ones structurally could not:
1. **Pass 7 (hostile input / DoS):** Postgres caps a statement at 65,535 bind parameters and
   the claim spent 15 per row, so it broke at **4,368 leads and enqueued nothing**. Production
   holds 100,548 claimable leads. Six passes of correctness review had walked straight past it.
2. **Pass 8:** the fix for that introduced the next bug -- the chunked insert rebuilt its bind
   parameters per chunk while the withdrawal still indexed them by whole-batch position, so it
   would delete the WRONG pending row.
3. **Pass 10 (deploy day):** the dispatcher, which is the thing that actually SPENDS, never
   checked the invariant. 100 aborts precisely when duplicates exist, `start.sh` boots the
   worker anyway, and both rows of a duplicate pair would have been submitted and charged.
   Reviewing the code could not find this, because the gap was not in the code under review.

**The lesson worth carrying:** a clean pass is evidence about the questions asked, not about
the code. Change the question.

**Three findings worth remembering beyond this phase:**
1. **The fix's own ordering was the bug.** Creating the unique index BEFORE moving the enqueue
   onto `ON CONFLICT` would have made one conflicting row roll back an entire job's enqueue and
   commit an empty transaction, silently. The index and the refactor are one change, not two.
2. **A second writer hides in `scripts/`.** 1a had already audited the ops scripts for the
   subject key and fixed `backfill_skip_trace_jobs.py`. It was wrong again here for a different
   invariant, because it built `PendingSkipTraceRow` itself. **A script that writes to a queue
   is part of that queue's concurrency design.**
3. **A reviewer's fix can be worse than the bug.** Codex's remedy for the unguarded cache-hit
   write was a raw-SQL UPDATE. `phone`/`email`/`phones`/`emails` are `EncryptedString` /
   `EncryptedJSON`, so that would have written PLAINTEXT PII. The intent was implemented with
   the shared lock and ORM writes instead. **Check a suggested fix against the schema.**

**Carried into 1b-2, do not lose:**
- The action worker MUST call `lock_job_for_claim()` before its own cache-and-claim pass. The
  cache-hit write is an ORM write (phone/email are `EncryptedString`, so it cannot be raw SQL)
  and it cannot see another writer's uncommitted pending row. `claim_skip_trace_rows` ASSERTS
  the lock is held, so forgetting raises rather than racing.
- It inherits fail-closed enforcement and the one-job-per-claim rule automatically.
- Codex's standing suggestion, not built: a shared claim-context API that takes the lock, does
  the cache and claim work, and requires the action/audit writes before commit, so "caller owns
  the transaction" stops being convention. Worth doing when 1b-2 needs it.

**Deploy notes for whoever ships this:**
- Forward safe. **Not backward safe:** rolling back past this release with 100 applied is
  unsafe while traffic flows, because the previous release's enqueue is not conflict-aware and
  discards whole job batches silently. Downgrade 100 with it, or stop the worker first.
- 100 aborts only if duplicate active rows exist. Production had **zero** when checked, so it
  should not abort. If it ever does, the dispatcher holds those specific leads back and alerts
  rather than paying for them twice, so an unquiesced deploy is SAFE -- but a quiesced one (the
  1a pattern: kill switch off, workers to zero, migrate, restart) still avoids the mixed-version
  window entirely and is the more conservative choice. Owner's call.

**Verification.** 313 tests green on an isolated database across the skip-trace, enqueue,
dispatcher, reconciliation and Phase 1a subject-key suites; ruff clean. Migration 100 applied
and verified BY THE OBJECTS (unique, valid, correct predicate), never by `alembic_version`, and
its rebuild path proved live by planting the exact wrong index and re-running it. The 15-1
regression test is mutation-tested: with `ON CONFLICT` removed it fails with the IntegrityError
the pre-100 code would have produced.

**Not done:** a full-suite run (the local suite is killed for host memory; CI is the signal).

### Still open

- **(2026-09-28) 1b-1a and all of 1b-1b are LIVE** (#356 ... #382, main `c0b09b7a`).
  **Next: 1b-1c PLANNER + QUOTE** (section "Phase 1b-1c" above: two PRs, i planner +
  pricing, ii the quote endpoint), then 1b-2 WRITERS, then 1c frontend.
- Carried into 1b-2 (do not lose): H3 (`lock_job_for_claim()` + `claim_skip_trace_rows()`
  only), 20-3 (re-authorize every id against the immutable quoted set), the round-21 notes
  (tenant hard deletes fail closed; the unverified `pg_auth_members` check).
- 🛑 **In this repo a merge IS a deploy**: push to `main` redeploys Railway api + worker and
  `start.sh` migrates on boot via `scripts/migrate.py`. Migration 100 was live ~2 seconds after
  #354 merged, which silently overrode the owner's choice of a QUIESCED deploy. It was harmless
  (0 duplicate rows, 0 active pending rows, 0 non-terminal jobs), but **quiescing has to happen
  BEFORE the merge, because there is no step in between**. Applies to migration 101.
- The cutover is a DRAIN and a restart, not a flag flip: the kill switch does not gate ingest,
  the webhook, or `_reuse_enrichment_for_duplicates`. See the runbook.
- Deferred, logged above: the pre-existing cross-tenant collision (worth a read-only prod count
  of how often it has fired), and the D2 credits-vs-billed-rows gap.
