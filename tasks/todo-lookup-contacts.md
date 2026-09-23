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

- **Phase 1b-1 has NOT started.** 1a and 1b-0 are both merged and live, so the old
  "must not start until 1a merges" gate is cleared. The next code is 1b-1a (SCHEMA), per the
  revised order in the round-16 section above — NOT the quote endpoint.
- 🛑 **In this repo a merge IS a deploy**: push to `main` redeploys Railway api + worker and
  `start.sh` migrates on boot via `scripts/migrate.py`. Migration 100 was live ~2 seconds after
  #354 merged, which silently overrode the owner's choice of a QUIESCED deploy. It was harmless
  (0 duplicate rows, 0 active pending rows, 0 non-terminal jobs), but **quiescing has to happen
  BEFORE the merge, because there is no step in between**. Applies to migration 101.
- The cutover is a DRAIN and a restart, not a flag flip: the kill switch does not gate ingest,
  the webhook, or `_reuse_enrichment_for_duplicates`. See the runbook.
- Deferred, logged above: the pre-existing cross-tenant collision (worth a read-only prod count
  of how often it has fired), and the D2 credits-vs-billed-rows gap.
