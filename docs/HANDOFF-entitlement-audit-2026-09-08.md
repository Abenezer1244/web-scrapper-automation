# HANDOFF: plan entitlement audit + fixes

**Written:** 2026-09-08 · **Status:** work committed, NOT pushed, NOT deployable yet
(two open P1s) · **Branches:** `chore/entitlement-audit` (backend),
`chore/entitlement-audit-fe` (frontend)

Read this whole file before touching anything. The short version: an audit of the four
plan cards found nine mismatches, all nine are now fixed and committed, a second
reviewer then found seven more things, five of which are still open and two of which
are P1. Nothing has been pushed and nothing should deploy until those two are resolved.

---

## 1. The goal

Verify, from the repository and live behaviour, that every entitlement the four in-app
plan cards advertise is actually what each plan receives, and fix what does not match.
The original ask was explicit that the pricing UI is a CLAIM to be tested, not a
source of truth, and that a disabled button is not enforcement: direct API calls had to
be tested too.

The four cards (served live by `GET /billing/plans`, which returns
`src/config/plans.py` `PLAN_CATALOG` verbatim):

* **Starter $0** 50 records/mo, 1 county, CSV export, manual runs
* **Pro $199** 1,000 records, 3 counties, probate + pre-foreclosure + tax-delinquent +
  auction, 250 skip traces then $0.08/lookup, CSV + Excel, daily/weekly schedule, email
  delivery, batch scraping
* **Business $499** 5,000 records, 10 counties, all record types + overlap/intersection,
  all export formats, all schedules, email + webhook + dialer, 1,000 skip traces, API
* **Agency $1,499** unlimited records and counties, all types + overlap, 2,000 skip
  traces, white-label (coming soon), priority queue + support, dedicated account manager

The owner then approved, explicitly and in full, four decisions: attach the metered
skip-trace price at checkout, implement all three missing gates with grandfathering, fix
all four clear defects, and correct all of the marketing copy.

---

## 2. Where to work

```
backend    C:/Users/Windows/bridgeleads-worktrees/entitlement-audit      chore/entitlement-audit
frontend   C:/Users/Windows/bridgeleads-web-worktrees/entitlement-audit-fe  chore/entitlement-audit-fe
```

**Continue on these branches.** Do not start a new one and do not work in the main
checkout: `main` is checked out elsewhere and other sessions are active in this repo.

The backend branch is **6 commits behind `origin/main`** as of writing (main moved twice
during the session). Merge it forward before doing anything, and read the merged regions
semantically: "Auto-merging X" is not a correctness statement. `src/api/routes/jobs.py`
and `src/workers/tasks.py` have already been through one such merge and my edits
survived it, verified by grep, not by trust.

### Running tests: use the isolated database

```bash
cd C:/Users/Windows/bridgeleads-worktrees/entitlement-audit
bash run-audit-tests.sh tests/ -m "not integration"
bash run-audit-tests.sh tests/ -m "integration"
```

`run-audit-tests.sh` points at `bridgeleads_entaudit_test`, a database created for this
work, and Redis db 1. **Do not run bare `pytest`** and do not point this at the shared
`bridgeleads_test`. That database is used by every worktree on this machine at once and
its `conftest` teardown deletes every `@test.bridgeleads.io` user, so a second session
running there deletes this one's fixture rows mid-request. It surfaces as 401s, "Could
not refresh instance", and foreign-key violations on `jobs.user_id` that look exactly
like product bugs and are not. I lost an hour to this before isolating.

The same applies to **your own** runs: an integration pass beside a non-integration one
gave 16 scattered failures in files the branch never touched. Run them one at a time.

Last known green: **2,711 non-integration + 182 integration**, `python
scripts/export_openapi.py --check` clean, frontend `tsc --noEmit` and `eslint --quiet`
clean.

---

## 3. What is already live in production

**These are done. Do not redo them, and be aware they exist.**

* **Three yearly metered Stripe Prices were CREATED in the live account** by
  `scripts/stripe_setup_skip_trace_annual.py`, on meter
  `mtr_61UUDj0Img9gfXgl241HE9wT1C7yZGHg`, product `prod_UJShnBWr5KDV6q`:
  * `price_1UDNZQHE9wT1C7yZUP3xLjHP` pro, 8c
  * `price_1UDNZRHE9wT1C7yZ5SMHjj11` business, 8c
  * `price_1UDNZRHE9wT1C7yZSuH1gO90` agency, 5c

  A Stripe Price can be deactivated but never deleted. They are unreferenced until this
  branch deploys.
* **Railway env set on BOTH `api` and `worker`** with `--skip-deploys` (so nothing
  redeployed against unmerged code): `STRIPE_PRICE_SKIP_TRACE_PRO_ANNUAL`,
  `STRIPE_PRICE_SKIP_TRACE_BUSINESS_ANNUAL`, `STRIPE_PRICE_SKIP_TRACE_AGENCY_ANNUAL`.

Nothing else outward-facing was touched. No branch pushed, no PR opened, no deploy.

### Production facts established during the audit (do not re-derive)

* `ENTITLEMENT_ENFORCEMENT=true` on api AND worker. The code default of `False` is not
  the production value, so county and record-type gates return 402 today.
* `WORKER_QUEUES=celery,scrape-priority,scrape,enrichment` on worker.
* `OPS_ALERT_EMAIL=memiki70@gmail.com` on worker, so ops alerts are reachable.
* The live customer-portal config `bpc_1TGRdUHE9wT1C7yZvb7Hj8RD` has
  `subscription_update` **disabled**, which is why Stripe's usage-based restriction on
  portal plan switching does not bite here.
* **0 Stripe subscriptions, 6 users** (1 agency, 4 pro, 1 starter), none with a
  `stripe_subscription_id`. Plans are set **by hand in the database**. That is why the
  plan-normalization and missing-`stripe_customer_id` findings are real rather than
  theoretical.
* Every `PLAN_CATALOG` price id maps to the right Stripe product at the right amount:
  Pro 19900/191000, Business 49900/479000, Agency 149900/1439000.

---

## 4. What shipped (5 commits on `chore/entitlement-audit`)

```
a81c1c2 test(entitlements): four test bodies in my own suite never ran
835b986 fix(billing): a lookup after a window ends is not billed against the old one
e12d54f docs(entitlements): the three items are closed, the Codex gate is not
479ae58 feat(billing): close the three items the audit left open
e1c9a7b Merge remote-tracking branch 'origin/main'
23c467f fix(freshness): clamp only when a delay is in force, and record the audit
7ce4412 feat(plans): gate export format, scheduling and overlap, and bill the overage
b1b25e6 fix(plans): the four defects the audit found that were not product decisions
1eeb495 test(entitlements): audit every plan card against the code that enforces it
```

Frontend, 2 commits on `chore/entitlement-audit-fe`: `5fe8708`, `83c7b77`.

### The nine original mismatches, all fixed

1. **Overlap/intersection had no plan gate.** `/segments` (four endpoints) shipped with
   auth and tenant scoping and no plan dependency, so Starter reached what Business and
   Agency are sold. Now a router-level dependency, so a fifth endpoint cannot be added
   without it.
2. **"then $0.08/lookup" could not bill.** Checkout built the subscription with one
   licensed line item; the three metered prices were set in production and read by
   nothing. The plan's metered price is now a second subscription item (no `quantity`,
   Stripe rejects one on a metered price), and every reader that took `items[0]` now
   finds the LICENSED item by price id, because with two items index 0 is whichever
   Stripe returns first.
3. **Priority queue reached manual runs only.** The scheduler and batch fan-out published
   with `.delay()`, which takes the task's declared route (`scrape`) for every plan. All
   four enqueue sites now go through `scrape_queue_for_plan()`.
4. **Export format and schedule frequency had no gate anywhere.** Now
   `EXPORT_FORMATS_BY_PLAN` and `SCHEDULE_FREQUENCIES_BY_PLAN`, enforced at create and on
   the edit **enable-delta**, so a downgraded account can still rename a config it
   already has. `"manual"` is in every plan: it is the absence of a schedule.
5. **`enrichment.skip_tracing` was gated, persisted, and read by nothing.** Now mirrored
   onto `skip_trace_enabled`, the column the worker actually reads, on create and on the
   edit enable-delta only.
6. **The Starter 7-day freshness delay applied to the rolling window only.** A custom
   window's end is now clamped, but ONLY when a delay is in force (`end_date < today`).
7. **Six plan comparisons skipped `.lower()`, three lowered without `.strip()`.** One
   `normalize_plan()` now backs every gate, the dialer's SQL filter included, and
   `plan_label` follows so the tier enforced is the tier named.
8. **`/billing/pricing` contradicted the catalog in the same response** (Pro read "All"
   record types). Every comparison cell that describes a gate is now derived from it.
9. **The public pricing page said the opposite of what production enforces** ("There is
   no per-plan county cap" while the API 402s), and showed white-label as a plain
   checkmark. Corrected, along with Pro's missing auction lists, "All 6 record types"
   (there are 7), Agency's overage rate, and the removal of team-seat claims that have
   no implementation anywhere.

### The three items closed in the follow-up (`479ae58`)

* **Annual overage**: the three yearly prices above, plus
  `_metered_skip_trace_price(plan, interval)` in `src/api/routes/billing.py`.
* **The batch's second door to the overlap product**: `delivery_mode="overlaps_only"` is
  a Business line but is ALSO the field's default and "Batch scraping" is a Pro card
  line. `body.model_fields_set` separates them: asked for explicitly below Business it
  402s; omitted, it coerces to `"everything"`. Codex confirmed `model_fields_set` is
  reliable here and found no create-time bypass.
* **The skip-trace clock**: `users.skip_trace_period_start` now holds the start of the
  ENTITLEMENT WINDOW rather than a calendar month. Same column, new meaning, no
  migration. It had to move because a Stripe metered item bills over the SUBSCRIPTION
  period, so a customer anchored on the 20th got their 250 free lookups back halfway
  through the period Stripe was invoicing.

---

## 5. Active files

Backend, changed by this branch's own commits:

```
src/config/constants.py        normalize_plan, scrape_queue_for_plan,
                               EXPORT_FORMATS_BY_PLAN, SCHEDULE_FREQUENCIES_BY_PLAN,
                               OVERLAP_PLANS, export_format_label
src/config/plans.py            plan_label now strips, to match the gates
src/config/settings.py         the three *_ANNUAL metered price slots
src/api/entitlements.py        new Violation builders, plan_limit_http made public,
                               raise_plan_features
src/api/routes/scrapers.py     _enforce_plan_feature_gates rewritten: collects
                               violations, adds export_formats + schedule_frequency,
                               enable-delta on edit
src/api/routes/batches.py      the same gates + the delivery_mode coercion
src/api/routes/segments.py     _require_overlap_plan router dependency
src/api/routes/billing.py      metered line item, _plan_item_price_id, derived
                               comparison cells, skip-trace usage window
src/api/routes/jobs.py         scrape_queue_for_plan, normalize_plan on AI_JOB_LIMITS
src/api/auth.py                require_plan and the api-key path normalize
src/api/billing/skip_trace_usage.py   _MissingCustomerError, effective_window rollover
src/workers/scheduler_helpers/dispatch.py   per-plan queue on scheduled runs
src/workers/batch_tasks.py     per-plan queue on the batch fan-out
src/workers/scheduler_helpers/meter.py      the sweep's users JOIN + held-rows alert
src/workers/scheduler_helpers/billing.py    the daily roll keys off the window
src/workers/scheduler_helpers/dialer.py     lower(trim()) in the SQL filter
src/workers/tasks_helpers/dates.py          the guarded freshness clamp
src/workers/tasks_helpers/enrich.py         normalize_plan
src/workers/tracerfy_ingest.py              re-resolve the customer id, HOLD not write off
scripts/stripe_setup_skip_trace_annual.py   NEW, idempotent
run-audit-tests.sh                          NEW, isolated database
tests/test_plan_entitlement_audit.py        NEW, 168 tests
tests/test_billing_catalog_matches_matrix.py  +6 comparison-drift guards
docs/ENTITLEMENT-AUDIT-2026-09-08.md        the written audit
```

Frontend: `lib/entitlements.ts`, `lib/nav.ts`, `app/(dashboard)/segments/page.tsx`,
`app/(dashboard)/scrapers/new/page.tsx` and its `_steps/{CountyStep,DeliveryStep,ScheduleStep}.tsx`,
`app/(dashboard)/scrapers/[id]/edit/page.tsx`, `app/(marketing)/_monopo/{data.ts,Pricing.tsx}`,
`components/plan-limit-notice.tsx` (exported `BILLING_HREF`).

---

## 6. OPEN: what the review gate found and nobody has fixed

Codex reviewed three times. Round 1 found four defects plus a regression in my own fix;
round 2 ran out of quota mid-review; round 3 completed. It verified the allowance fix as
correct ("The SELECT supplies every required attribute... no backward movement or
beat-induced lost allowance found", across 1,296 window combinations) and found **no new
defect** in the export/schedule enable-delta, the `/segments` dependency coverage, the
four queue-publishing sites, the freshness guard, or the derived pricing cells.

These remain. **The two P1s are a NO-GO for deploy** under
`.claude/rules/codex-collaboration.md`.

| Sev | Finding |
|---|---|
| **P1** | **Held meter events release on the wrong signal.** `meter.py:46` releases a held row as soon as the user has a `stripe_customer_id`, but `billing.py:770` saves that id when the CHECKOUT SESSION IS CREATED, before payment or subscription completion. Someone who starts checkout and abandons gets their pre-subscription usage fired anyway. Codex's recommendation: require human review for the pre-subscription backlog. Charging it is defensible only if the customer agreed to pay for those units, and starting checkout does not establish that. |
| **P1** | **"Switching" plans through checkout creates a SECOND subscription.** `billing.py:783` always creates a subscription-mode session and nothing cancels the existing one, despite `billing.py:807` describing this as the plan-change path. A monthly subscriber buying annual keeps both obligations. **Predates this work**, but attaching metered items makes overlapping subscriptions much more consequential. |
| P2 | **Pending downgrades still grant the previous plan's allowance.** `skip_trace_usage.py:109` reads the stored `plan` although the effective window can already be past the boundary that applies `pending_plan`. Codex reproduced Business to Pro with 300 new-window lookups: **0 billable instead of 50**, and later reconciliation does not recover them. Fix: select `pending_plan` and use it when the rollover is effective. |
| P2 | **Existing recurring overlap batches escape the new gate.** Recurrence creates a run without re-checking entitlement, and finalization rereads the parent's saved mode at `batch_export.py:426`. An old Pro batch, or a Business batch after downgrade, keeps producing filtered overlap exports. Either check entitlement when generating a run, or make the grandfathering an explicit written policy. |
| P2 | **`tasks.py:2064` still does `.lower()` without `.strip()`.** Creation accepts `"business "` but the worker then skips the saved webhook. I missed this one in the normalization sweep. Use `normalize_plan()`. |
| P2 | **`/billing/skip-trace-usage` shows a stale counter under a fresh window.** `billing.py:238` reads raw usage while line 254 computes effective dates, so right after a rollover a customer sees an exhausted allowance and estimated charges against a new period. Project the counter using its period stamp. This endpoint also misses plan normalization. |
| P3 | **The annual-price idempotency check does not validate economics.** `stripe_setup_skip_trace_annual.py:63` omits amount, currency, `interval_count` and billing shape. Codex got a fixture with matching tags but 800 cents, EUR and a two-year interval accepted. |
| P3 | **Test assertions that overstate coverage.** Source-text assertions at lines 1100-1101, 1236-1237, 1268-1269, 1438-1441 and 1501-1509 can pass with unreachable code or wrong runtime behaviour: they do not establish worker execution, queue routing, delivery, durable holding, or beat correctness. The test at line 1473 claims a calendar rollover but advances no clock and its SQL creates an invalid window. |

Codex's ruling on one question I had deliberately left open: **keep the batch provenance
columns.** The combined CSV emits `overlap`, `lists_count` and `lists` in every delivery
mode, so a Pro user still gets the overlap SIGNAL and can sort by it. Codex judged that
explaining which purchased lists contributed to a deduplicated property is useful
independently of `/segments`, and that suppressing it would remove list membership and
the customer's ability to reconcile merged rows to their sources without changing what
they are charged. I agree. No action.

---

## 7. Failed attempts and landmines

* **My grep lied twice, the same way.** I reported the Starter 7-day delay as
  unimplemented after two sweeps that both ended in `| head -N` and both cut before
  `src/workers/tasks_helpers/dates.py:88`. It was implemented all along. **A truncated
  search is not a clean one.**
* **Codex caught a regression in my own fix.** The freshness clamp applied to every plan,
  and `trustee_sale` derives a forward auction horizon from the window's LENGTH
  (`_window_span_days`), so a paid customer asking for the next 90 days of auctions would
  have had it truncated or erased. The guard is `end_date < today`, not the plan name.
* **Four of my own test definitions were declared twice.** An index-based patch inserted
  copies instead of replacing; Python binds the later one, so pytest collected half of
  each pair and four bodies were dead from the moment they were written. Fixed in
  `a81c1c2`. Verify with `ast.parse` and a `Counter`, not by reading.
* **Gating the batch `overlaps_only` default broke Pro batch creation outright** on the
  first attempt. Caught by my own test. That is why the coercion exists.
* **`codex review --base <branch>` rejects a prompt argument** in CLI 0.153.4. Use
  `codex exec` and let it run `git diff origin/main` itself.
* **Never pipe codex stderr to /dev/null.** The `--base` failure and both usage-limit
  cut-offs were only visible there.
* Two existing tests were asserting the OLD behaviour and had to be re-pinned, not
  deleted: `test_entitlement_notice_copy.py` (untrimmed plan) and
  `test_scraper_edit.py::test_patch_switch_to_since_last_run_persists` (a Starter fixture
  with a daily schedule, whose actual subject is `date_range_mode`).

---

## 8. Next steps, in order

1. **Merge `origin/main` forward** (6 behind) and re-read the merged regions semantically.
2. **Decide the two P1s.** Neither is a code detail:
   * Should a pre-subscription skip-trace backlog ever be charged automatically? Codex
     says no, require human review. If you agree, the sweep needs a stronger release
     signal than "a customer id exists" (a live subscription, or an explicit flag), and
     the held backlog needs a way for a human to release or write it off.
   * The duplicate-subscription checkout path predates this work. It needs either a
     Subscription modify path or a guard that refuses checkout when one is active.
3. **The three cheap P2s**: `normalize_plan()` at `tasks.py:2064`; project the counter in
   `/billing/skip-trace-usage`; read `pending_plan` in `report_lookups_for_user`.
4. **Decide the recurring-batch P2**: re-check entitlement per run, or write the
   grandfathering down as policy.
5. **P3s**: validate amount/currency/interval in the annual-price idempotency check;
   replace the source-text test assertions with behavioural ones.
6. **Then** push both branches, open PRs, and re-run the Codex gate on the final diff.
   Do not deploy before the P1s are closed.

Full findings with file:line are in
`C:/Users/Windows/AppData/Local/Temp/claude/.../scratchpad/gate2_final.md` for this
session only; everything material from it is reproduced above, because that path will
not survive.
