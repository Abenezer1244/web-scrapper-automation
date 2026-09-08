# Plan entitlement audit, 2026-09-08

Every line on the four in-app plan cards, traced to the code that enforces it, with a
PASS / FAIL / UNVERIFIED verdict and the evidence behind it.

Branches: `chore/entitlement-audit` (backend), `chore/entitlement-audit-fe` (frontend).
Regression suite: `tests/test_plan_entitlement_audit.py`, 111 tests, all passing.
Independent second reviewer: Codex (gpt-5.5, ~1.24M tokens), findings reconciled below.

Production facts established before any verdict:

* `ENTITLEMENT_ENFORCEMENT=true` on the `api` and `worker` Railway services
  (`railway variables -s api|worker`). The code default of `False` is not the production
  value, so county and record-type gates are live and return HTTP 402 today.
* `WORKER_QUEUES=celery,scrape-priority,scrape,enrichment` on `worker`.
* Live Stripe prices retrieved by id: Pro 19900 monthly / 191000 annual, Business 49900 /
  479000, Agency 149900 / 1439000, all licensed recurring under products named Pro,
  Business and Agency. Every id in `PLAN_CATALOG` maps to the right product at the right
  amount.
* Live Stripe metered prices exist under product "Skip Trace Lookup", attached to meter
  `mtr_61UUDj...`: 8 cents (pro), 8 cents (business), 5 cents (agency).
* Production holds 6 users and **0 Stripe subscriptions**. Nobody has been billed under
  any of these plans yet. That is what keeps the billing finding below at
  "revenue leak, no customer harmed" rather than "customer overcharged".

A correction to my own first pass, recorded because the failure mode is worth keeping:
I reported "Starter 7-day delayed data is not implemented anywhere". It is
(`src/workers/tasks_helpers/dates.py:88-91`). My two `grep` sweeps both ended in
`| head -N` and both cut before reaching it. A truncated search is not a clean one.
Codex found it. The real defect there is narrower and is listed as P2-5.

---

## 1. Authoritative plan configuration

There is no single source of truth. Twelve places define part of a plan:

| Where | What it defines | Role |
|---|---|---|
| `src/config/plans.py` `PLAN_CATALOG` | price, `records_limit`, the marketed feature bullets | The cards. `GET /billing/plans` serves it verbatim |
| `src/config/settings.py` `PLAN_LIMITS` | records per month | Read by registration, trial, downgrade |
| `src/config/settings.py` `SKIP_TRACE_BUNDLED_QUOTAS` | included lookups | Read by the meter path and `/billing/skip-trace-usage` |
| `src/config/settings.py` `AI_JOB_LIMITS` | AI-mode scrape jobs per month | Enforced, on no card |
| `src/config/constants.py` `COUNTY_LIMIT_BY_PLAN` | county cap | Enforced |
| `src/config/constants.py` `RECORD_TYPES_BY_PLAN` | record types | Enforced |
| `src/config/constants.py` `BATCH_PLANS`, `BATCH_MAX_COMBINATIONS` | batch access and size | Enforced |
| `src/config/constants.py` `BUSINESS_FEATURES_PLANS` | webhook, dialer, the enrichment toggle, API | Enforced |
| `src/config/constants.py` `SKIP_TRACE_ADDON_PLANS` | metered skip-trace toggle | Enforced |
| `src/config/constants.py` `PRIORITY_QUEUE_PLANS` | Celery queue routing | Partly enforced, see P1-3 |
| `src/api/routes/billing.py` `pricing_page()["comparison"]` | a 13-row feature matrix | Display only, contradicts the catalog it ships beside |
| FE `lib/entitlements.ts` | county cap, record-type minimums, feature helpers | Mirror, explicitly not the primary gate |
| FE `app/(marketing)/_monopo/data.ts` | `PLANS`, `COMPARISON`, `PRICING_FAQ` | A second, hand-written public pricing page |

`PLAN_CATALOG` and `PLAN_LIMITS` currently agree (50 / 1000 / 5000 / -1) and a test now
pins them together.

---

## 2. The matrix, as enforced AFTER the fixes in section 6

| Capability | Starter | Pro | Business | Agency | Enforced by |
|---|---|---|---|---|---|
| Records per month | 50 | 1,000 | 5,000 | unlimited | `quota.py` + the worker's atomic reservation |
| Distinct counties | 1 | 3 | 10 | unlimited | `entitlements.py`, create and run time |
| Record types | probate | probate, pre-foreclosure, tax-delinquent, trustee sale | all 7 | all 7 | `entitlements.py`, create and run time |
| Skip trace included | 0 | 250 | 1,000 | 2,000 | counter + meter math |
| Metered skip-trace toggle | no | yes | yes | yes | `SKIP_TRACE_ADDON_PLANS` |
| Skip-trace overage charged | n/a | yes, monthly | yes, monthly | yes, monthly | metered price attached at checkout (annual unpriced, see 6) |
| Export formats | CSV | CSV, Excel | all 4 | all 4 | `EXPORT_FORMATS_BY_PLAN`, create and edit |
| Schedule frequency | manual | manual, daily, weekly | all 4 | all 4 | `SCHEDULE_FREQUENCIES_BY_PLAN`, create and edit |
| Email delivery | yes | yes | yes | yes | ungated on purpose; the comparison row now says so |
| Webhook delivery | no | no | yes | yes | `BUSINESS_FEATURES_PLANS` |
| Dialer delivery | no | no | yes | yes | `BUSINESS_FEATURES_PLANS` |
| Batch scraping | no | yes (25 combos) | yes (100) | yes (250) | `BATCH_PLANS` |
| API access | no | no | yes | yes | `require_plan` + the api-key auth path |
| Overlap / intersection | no | no | yes | yes | `OVERLAP_PLANS`, router dependency on `/segments` |
| Priority queue | no | no | yes | yes | `PRIORITY_QUEUE_PLANS`, all four enqueue sites |
| 7-day data delay | yes, on every date mode | n/a | n/a | n/a | `dates.py`, rolling and custom |
| White-label | no | no | no | not built, labelled "coming soon" everywhere | n/a |
| Seats | none | none | none | none | no seat model exists; the claim is gone from both pricing surfaces |

Section 3 records what the audit found BEFORE these fixes.

---

## 3. Findings

### P1-1. Overlap / intersection is sold as Business and reachable from Starter

`src/api/routes/segments.py:58` declares `APIRouter(prefix="/segments")` with no
dependency, and neither `intersection_preview` (`:492`), `intersection_export` (`:540`),
`union_preview` (`:636`) nor `union_export` (`:664`) consults the plan. `main.py:85`
mounts it unguarded.

Both the Business and Agency cards say "All record types + overlap/intersection", and
`docs/pricing-strategy-2026-06.md` §4 is explicit: gate the distress-list overlap at
Business, calling it the crown jewel. A Starter account reaches it today with a bearer
token and no UI.

The frontend already knows. `lib/entitlements.ts` carries `canUseOverlap()` with a
comment saying it is deliberately unwired, because hiding a backend-allowed feature would
be the wrong half to fix. That judgement was right; the backend half is the missing one.

Batch `delivery_mode: "overlaps_only"` (`src/api/routes/batches.py:299`) is the same
capability through a second door, gated only by `BATCH_PLANS` (Pro and above).

### P1-2. "then $0.08/lookup" never reaches an invoice

`create_checkout` (`src/api/routes/billing.py:599-606`) builds the Checkout Session with
`line_items=[{"price": stripe_price_id, "quantity": 1}]`: one licensed plan item. Nothing
in the repository ever adds a second subscription item.

`STRIPE_PRICE_SKIP_TRACE_PRO`, `_BUSINESS_OVERAGE` and `_AGENCY_OVERAGE`
(`src/config/settings.py:146-148`) are set in production and referenced only by
`scripts/stripe_setup_skip_trace_billing.py`, which created them. No runtime module reads
them.

Everything before Stripe is sound: `report_lookups_for_user` locks the user row, computes
`max(0, after - quota) - max(0, before - quota)`, the outbox row commits atomically with
the queue status flip, and `report_meter_event_to_stripe` fires a `MeterEvent` with a
stable `(queue_id, user_id)` identifier so retries dedupe. But a Stripe meter event only
becomes money when the subscription carries an item priced against that meter. None does.

So a Pro customer's 251st lookup is counted, metered, and free. Business and Agency
over-quota behaviour is identical, and neither card states an overage rate at all
(`/billing/skip-trace-usage` reports 8 cents for both Pro and Business and 5 cents for
Agency; the Agency card and the public pricing page disagree with each other on that
number).

Both reviewers reached this independently. It is a billing change, not a patch.

### P1-3. The priority queue skips the runs it exists for

`scrape-priority` is a real queue (`src/workers/__init__.py:85-90`) and the worker
consumes it (`start.sh:16`). Two of the four enqueue sites route to it:
`POST /jobs` (`src/api/routes/jobs.py:222`) and the transient-retry path
(`src/workers/tasks.py:646`). The other two do not:

* `scheduler_helpers/dispatch.py:243` calls `run_scrape_job.delay(jid)`
* `batch_tasks.py:213` calls `run_scrape_job.delay(jid)`

`.delay()` takes the task's declared route, and `app.conf.task_routes`
(`src/workers/__init__.py:95`) sends `run_scrape_job` to `scrape` for everyone. So a
Business or Agency customer gets priority on a button press and ordinary service on every
scheduled run and every batch child, which is the recurring work the product is built
around. Codex found this; verified independently by reading all four call sites and
resolving the route through `celery_app.amqp.router`.

Second half of the same finding: `start.sh:12` claims "Priority queue listed first =
processed first". Under the Redis broker that is not what queue order means. kombu 5.6.2's
`Channel.queue_order_strategy` defaults to `round_robin` and the app sets no
`broker_transport_options` override. Round robin is still a real benefit (a priority job
never waits behind an entire backlog) but it is not strict priority, and the comment
should not be read as a guarantee.

### P1-4. `/billing/pricing` contradicts the catalog in the same response

One JSON body carries both:

* `plans[pro].features` = "Probate, pre-foreclosure, tax-delinquent & auction lists"
* `comparison["Record types"]["pro"]` = **"All"**

`RECORD_TYPES_BY_PLAN["pro"]` is 4 of 7 and the gate 402s the other three.
`tests/test_billing_catalog_matches_matrix.py` pins the catalog half and never looks at
the comparison dict, which is how the two drifted.

Five more comparison cells describe gates that do not exist: Export formats, Scheduling,
Email delivery (Starter `false`), Team members, and Skip tracing (Pro reads "Per-lookup",
omitting the 250 included that the card sells).

### P1-5. The public pricing page states the opposite of what the API enforces

`https://bridgeleads.io/pricing`, from `app/(marketing)/_monopo/data.ts`, says in the hero
"Every plan reaches all supported counties" and in the FAQ "There is no per-plan county
cap." With `ENTITLEMENT_ENFORCEMENT=true`, a Pro customer's 4th county is refused with a
402. This is the same contradiction PR #235 fixed in the API's own comparison row and did
not follow through to the marketing repo.

Same page, same file:

* White-label shows a plain "Included" checkmark for Agency. The in-app card and the API
  comparison both say "coming soon", and nothing is built. This is the one surface that
  presents an unbuilt feature as shipped.
* Pro's record types read "Probate, pre-foreclosure & tax-delinquent", omitting trustee
  sale, which Pro does get. The page under-sells a real entitlement.
* "All 6 record types" appears four times. `ALL_RECORD_TYPES` has 7.
* Agency's "Additional skip-traces" shows $0.08; the API reports $0.05.
* "Team seats 1 / 1 / 3-5 / Unlimited" describes a capability with no implementation
  anywhere: `main.py` mounts no team-management router.

### P2-1. Export format carries no plan gate

`DeliverConfig.formats` (`src/api/schemas.py:480`) is validated only against
`SUPPORTED_EXPORT_FORMATS` (`:547-554`). No route reads the plan. A Starter account saves
`["json"]` and gets it; the card sells "CSV export", Pro sells "CSV + Excel export".

Separately, and a wording problem rather than a gate: the worker takes `formats[0]`
(`src/workers/tasks.py:1103`) and produces exactly one file, and the combined batch export
is hardcoded CSV (`src/workers/batch_export.py:445`). "CSV + Excel export" and "All export
formats" describe a menu, not a set the customer receives.

### P2-2. Schedule frequency carries no plan gate

`ScheduleConfig.frequency` (`src/api/schemas.py:407-416`) only checks the value is one of
manual/daily/weekly/monthly. `scheduler_helpers/dispatch.py:100-155` gates a scheduled run
on quota, county and record type, never on whether the plan includes scheduling.

The Starter card says "Manual runs" and the strategy doc says "no schedule" for Starter.
Today a Starter account saves a daily schedule and the beat fires it. Pro gets monthly,
which is sold as a Business line.

### P2-3. The `enrichment.skip_tracing` toggle does nothing

`enrichment.skip_tracing` is gated to Business and Agency
(`src/api/routes/scrapers.py:190`) and persisted into `ScraperConfig.enrichment`
(`:274`). No worker reads it: `grep -c skip_tracing src/workers/tasks_helpers/enrich.py`
is 0. The skip-trace entry point gates on `getattr(config, "skip_trace_enabled", False)`
instead, a separate column set by a separate field.

A Business account that flips only this toggle gets no lookups and no error. The comment
in `src/config/constants.py:113-115` calls it "the always-on enrichment, included with the
plan", which is not what it does. Business and Agency still get skip tracing through
`skip_trace_enabled`, so no card promise is broken; a control that silently does nothing
is the defect. Codex found this.

### P2-4. Six plan comparisons skip normalization, and none of them strip

Raw comparisons: `src/api/auth.py:391` (`require_plan`),
`src/api/routes/scrapers.py:185` and `:190`, `src/api/routes/jobs.py:222`,
`src/api/routes/jobs.py:140` (`AI_JOB_LIMITS.get(plan, 5)`),
`src/workers/tasks.py:648`, `src/workers/scheduler_helpers/dialer.py:129`.
Lowercasing but not stripping: `src/api/entitlements.py:48`,
`src/api/routes/batches.py:165`, `src/api/billing/skip_trace_usage.py:104`.

Not theoretical here. With 0 Stripe subscriptions and 6 users spread across
starter/pro/agency, plans in this deployment are set by hand in the database, which is
exactly how a `"Business"` or `"pro "` gets in. Consequences differ by site: a mis-cased
Business is refused webhook delivery (safe) but is also routed to the ordinary queue with
no error (a paid customer silently losing a paid feature), and `"pro "` falls through to
the Starter county cap and a skip-trace quota of zero.

### P2-5. A custom date range bypasses the Starter freshness delay

`_resolve_date_range` (`src/workers/tasks_helpers/dates.py:86-99`) computes
`end_date = today - 7 days` for a Starter, then the `range_mode == "custom"` branch
returns the caller's own `date_from`/`date_to` without clamping either to `end_date`. A
Starter saving a custom window ending today gets today's records, which is the paid
freshness moat. The rolling and `since_last_run` branches honour the delay correctly.
Codex found this.

### P2-6. A billable meter event with no customer id is stamped as reported

`report_meter_event_to_stripe` raises `_StripeNotConfiguredError("no_customer_id")` and
the outbox task treats that as terminal, stamping `reported_at` so the sweep stops
retrying (`src/workers/tracerfy_ingest.py:88-89`). The reasoning is sound for "Stripe is
off", but for a real customer missing a `stripe_customer_id` it permanently writes off a
billable overage. Moot while P1-2 stands, and live the moment it is fixed.

### P3-1. Business gets the priority queue without being sold it

`PRIORITY_QUEUE_PLANS` is `{business, agency}`; only the Agency card mentions
"Priority queue + support". Over-delivery, not a shortfall. Worth deciding whether the
copy or the constant is wrong.

### P3-2. Skip-trace allowance resets on a different clock than records

Records reset on the user's own entitlement anniversary (migration 088). The skip-trace
counter resets on the calendar 1st (`src/workers/scheduler_helpers/billing.py:60-69`).
Both are deliberate and documented. "250 included" does not say which month it means, and
a customer who subscribes on the 20th gets a partial first skip-trace month.

---

## 4. Agency-only lines

| Line | Verdict |
|---|---|
| Priority queue | **PARTIAL.** The queue is real and manual runs reach it; scheduled and batch runs do not, and the transport gives round robin rather than strict priority (P1-3) |
| Support | MANUAL SERVICE, NOT APP-GATED |
| Dedicated account manager | MANUAL SERVICE, NOT APP-GATED. No fields, routing or admin assignment exist, and none should be invented |
| White-label (coming soon) | **PASS in the app.** Both the card and the API comparison carry the qualifier and nothing presents it as live. **FAIL on the public pricing page**, which shows it as included (P1-5) |

---

## 5. What was fixed

The owner chose the most complete option on all four decisions. Every finding in
section 3 is closed except the two noted at the end.

**Not product decisions, just defects:**

* Priority queue: the scheduled dispatcher and the batch fan-out now pass the queue,
  resolved from the owner's plan. `scrape_queue_for_plan()` is the one place that
  decision is made, and all four enqueue sites use it.
* Plan normalization: `normalize_plan()` strips and lowercases, and every gate goes
  through it, the dialer's SQL filter included. `plan_label` follows, so the tier a
  customer is enforced on is the tier the notice names.
* Starter freshness: a custom window's end is clamped to the plan's freshness edge. A
  window entirely inside the embargo collapses to the edge instead of inverting.
* The dead `enrichment.skip_tracing` toggle now sets the column the worker reads, on
  create and on the edit enable-delta only. Mirroring the effective value would have let
  an unrelated PATCH start paid lookups on a legacy config with the dead toggle stored
  true, which is the opposite of a fix.

**The three missing gates:**

* Export format: `EXPORT_FORMATS_BY_PLAN`, enforced on create and on the edit
  enable-delta, mirrored in the wizard as a locked chip that says why.
* Schedule frequency: `SCHEDULE_FREQUENCIES_BY_PLAN`, same shape. "manual" is in every
  plan: it is the absence of a schedule, not a schedule, and locking it would leave a
  Starter unable to save a scraper at all.
* Overlap and intersection: a router-level dependency on `/segments`, so a fifth endpoint
  cannot be added without it. `canUseOverlap()` is now wired in the frontend, the nav item
  is hidden below Business, and the page explains itself rather than rendering a screen
  whose every button ends in a refusal.

All of these raise the structured `{code, title, message}` 402 the plan notice renders.
The webhook, dialer and skip-trace refusals were converted to the same shape: they were
bare sentences, and an unstructured 402 gets the neutral "Manage billing" action, because
the only other thing that arrives unstructured is a failed payment.

**Billing:**

* The plan's metered skip-trace price is attached as a second subscription item at
  checkout, so an over-quota lookup produces a real invoice line. A metered price carries
  no quantity; Stripe rejects the item if it does.
* Every reader that took `items[0]` now finds the licensed item by price id. With two
  items on the subscription, index 0 is whichever Stripe returns first, and a plan lookup
  on the metered price would have alerted "price not in plan map" and refused to activate
  a plan the customer had just paid for. The same applies to `GET /billing/subscription`,
  where the metered item's unit_amount of 8 would have shown the customer a $0 plan.

**Copy, on both surfaces:**

* Every comparison cell that describes a gate is derived from that gate. Pro no longer
  reads "All" record types; export, scheduling and skip tracing are derived; overlap,
  batch and priority queue are new rows.
* The public pricing page states the county caps instead of denying them, adds auction
  notices to Pro, shows Agency overage at 5 cents, and labels white-label "Coming soon"
  instead of a plain checkmark.
* "Team members" and "Team seats" are gone from both. There is no seat model: no invite
  flow, no member table, no route.

### Two things deliberately left

1. **A batch's `delivery_mode="overlaps_only"` is NOT gated.** It is the DEFAULT for a
   batch and "Batch scraping" is a Pro card line, so gating it would leave Pro able to
   create a batch and unable to receive the only export it makes by default. A batch
   dedupes across the counties and record types of the one run its owner paid for;
   `/segments` overlaps the whole result history across lists, which is what the strategy
   doc gates. If a Pro batch should deliver "everything" instead, that is a change to the
   Pro batch and the card has to move with it. Pinned by a test either way.
2. **Annual subscriptions are still unmetered.** Stripe requires every item in one
   subscription to share a recurring interval, and the three metered skip-trace prices are
   monthly. An annual checkout therefore attaches nothing and logs a warning rather than
   failing the sale. Three yearly metered prices need creating in Stripe and setting as
   `STRIPE_PRICE_SKIP_TRACE_*_ANNUAL` on api and worker. The settings slots exist and are
   read; only the Stripe objects are missing, and creating live Stripe prices is not
   something to do unasked.

Reported and not fixed: the meter outbox stamps a billable event as reported when the
customer has no `stripe_customer_id` (P2-6), and the skip-trace allowance resets on the
calendar month while records reset on the subscriber's anniversary (P3-2).
