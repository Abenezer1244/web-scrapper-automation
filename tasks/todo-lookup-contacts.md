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
  resume time and each value is compared with now.
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

### ii-c TO BUILD
- [ ] ii-c-1: migration 103 + models + env.py + tests; replay; merge; VERIFY THE INDEX IN PROD
- [ ] ii-c-2: keyset allocate + frontier + in-flight cache + tests; gate at 117k/15k and 15k accounts
- [ ] Codex diff review each to GO; quiet check before each merge

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
- 🛑 **SAFETY, own small PR, soon (found in the ii-c-1 review, 2026-09-27):** `alembic/env.py`
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
