# Billing / Stripe / Quota / Tracerfy Security Audit (round 2)

Date: 2026-09-25. Scope: Stripe checkout, plan change, portal, webhooks, entitlement and quota, Tracerfy / contact lookup.
Code audited: worktree `C:/Users/Windows/bl-wt-secaudit2`. The brief named `25a04eaf`, but the worktree HEAD was `fc38e620` (merge of PR #356, migration 101 contact-lookup ledger, schema only) when this was read. Every line number below refers to `fc38e620`.
Method: static reading only. No pytest, no Stripe/Railway/DB calls. Production env values (for example `SKIP_TRACE_DAILY_ROW_CAP`, `TRACERFY_LEGACY_PATH_ENABLED`, `ENTITLEMENT_ENFORCEMENT`) could not be read, so this report gives the code defaults and says where the production value decides the outcome.

---

## 0. Summary

| ID | Finding | Sev | Status |
|---|---|---|---|
| B-1 | Upgrade now, pay later: an immediate upgrade followed by a deferred downgrade (or an annual upgrade) gives the customer the higher tier for close to the lower tier's price | **P1** | NEW |
| B-2 | Lead rows can be read before the plan cap runs and after a cancel, and the cancel refunds the quota (prior C5, now also reachable without cancelling) | **P1** | OPEN, carried from 09-16 `audit-billing.md` C5 |
| B-3 | Trial accounts (plan `pro`, no payment) can buy paid Tracerfy lookups that can never be billed, and can use up the global daily cap | P2 | NEW |
| B-4 | The global daily cap is soft (checked once per tick, then up to 10,000 rows), counts rows rather than credits, and is shared by all tenants. No per-account cap | P2 | F-12 residual |
| B-5 | The Tracerfy webhook body is still trusted for `rows_uploaded` (a billing input) and `download_url`, and the host pin accepts any region's `tracerfy` bucket | P2 | OPEN, carried from `audit-billing.md` A7b/A7c |
| B-6 | The legacy path-secret route is still on by default, and the unhandled-exception logger writes `request.url.path` without the path redaction | P3 | F-22 residual |
| B-7 | No handling for disputes or refunds: after a chargeback the customer keeps the tier | P3 | NEW |
| B-8 | Still open from earlier (P3): `ENTITLEMENT_ENFORCEMENT` defaults False, `livemode` is never checked, an empty `STRIPE_PRODUCT_*` collapses the product map, and a custom date range has no maximum span | P3 | carried |

Status of the three prior items:
- **F-11: FIXED.** `_handle_subscription_updated` now re-raises when Stripe cannot be re-read and no longer applies the event body (`src/api/routes/billing.py:2102-2132`). A failed read writes no ledger row, and Stripe retries.
- **F-12: PARTIALLY MITIGATED.** There is now a global rolling-24h row breaker (`src/workers/skip_trace_dispatcher.py:72-113`). Its code default is `0`, which means disabled (`src/config/settings.py:365`), so the production value decides whether it runs at all. There is still no per-account ceiling: migration 101 is schema only (`alembic/versions/101_contact_lookup_action_schema.py:1-6`) and "1b-1" has not been built. See B-3 and B-4.
- **F-22: OPEN, mitigated.** Access-log redaction of the path now exists (`main.py:132-157`), and there is a kill switch (`src/api/routes/webhooks.py:208-213`), but it defaults to enabled (`src/config/settings.py:345`). One log path is still unredacted (B-6). I cannot tell from here whether the secret has been rotated.

---

## 1. Findings

### B-1. P1: Upgrade now, pay later (immediate upgrade + deferred downgrade + pending prorations)

**Evidence**
- `change_plan` modifies the subscription with `proration_behavior="create_prorations"` (`src/api/routes/billing.py:1510-1516`, call at `:1588`). The billing anchor is reset only when the interval changes (`:1518-1524`). Under Stripe's documented semantics, `create_prorations` creates pending invoice items that are charged on the NEXT invoice, not at the moment of the change.
- The webhook applies an upgrade at once and parks a downgrade until the next entitlement boundary (`src/api/billing_entitlement.py:288-303`). The code assumes the customer "already paid for" the higher cap (docstring `:219-224`), but nothing has been charged yet.
- The downgrade also produces a proration credit for the unused higher-tier time, so the pending upgrade charge is almost entirely cancelled out.

**Attack (any paying customer, repeatable every window)**
1. A Pro monthly subscriber calls `POST /billing/change-plan` with the Agency price. Stripe modifies the subscription and queues prorations. `customer.subscription.updated` then sets `plan=agency`, `records_limit=-1` immediately.
2. Once the UI shows Agency (the webhook has landed), they call `change-plan` again with the Pro price. Stripe credits the unused Agency time and charges the Pro remainder. The app records `pending_plan=pro` and keeps Agency (`billing_entitlement.py:296-303`).
3. Result: unlimited records, Agency's 2,000 bundled lookups (`src/config/settings.py:184`), Agency county and feature gates, all until the next monthly boundary. The net Stripe charge is about a few minutes of Agency. Repeat each window.

A variant on annual plans: Pro annual to Agency annual keeps the same interval, so the anchor is not reset and the whole upgrade charge waits for the annual renewal. The customer can then set cancel-at-period-end in the portal (`billing.py:1650`: the portal allows cancel). I have not verified in Stripe test mode whether pending proration items are ever invoiced on a cancel at period end. Verify that before deciding the fix.

**Prerequisites:** an active paid subscription and two API calls. No special knowledge needed.
**Impact:** full tier bypass and direct revenue loss. The operator also pays Tracerfy for the extra bundled lookups.
**Fix (pick one policy and make Stripe and the app agree):**
- Charge upgrades before granting them: use `proration_behavior="always_invoice"` with `payment_behavior="pending_if_incomplete"` on upgrades, so the higher price only takes effect once the invoice is paid.
- For downgrades, stop issuing a credit while keeping the higher tier: use `proration_behavior="none"` and schedule the price change for period end (a Subscription Schedule), so Stripe bills the higher tier for as long as the app grants it. Otherwise, apply the downgrade in the app immediately when a credit is issued.
**Regression test:** in `tests/test_change_plan*.py`, assert that an upgrade call passes `always_invoice` (or pending-if-incomplete), and that a downgrade call does not pass `create_prorations` while `apply_plan_change` returns `downgrade_pending`. Add a lifecycle test: upgrade then downgrade inside one window must not leave `records_limit` at the higher tier unless an invoice for it is paid.

### B-2. P1: Leads readable before the plan cap and after a cancel, with the quota refunded (carried C5, extended)

**Evidence**
- Results are bulk-inserted at `src/workers/tasks.py:977-1000` while the job is `enriching`. Enrichment runs at `:1555`, and the plan cap reserves quota and stamps `over_quota` only at `:1753-1880`.
- `GET /jobs/{job_id}/results` filters on ownership, category, `actionable_condition()` and the tax cap only (`src/api/routes/jobs.py:414-445`, `:492-497`). It has no gate on job status or `billing_applied_at`. `actionable_condition` hides only rows ALREADY marked `over_quota` (`src/api/lead_actionability.py:75-89`), so before the cap runs every row is visible.
- `enriching` can be cancelled (`src/config/constants.py:61-67`). After a cancel, the reservation is handed back (`src/workers/tasks.py:2384-2396`, plus the sweep at `src/workers/tasks_helpers/status.py:475-505`), and the rows stay.

**Attack:** a user with 10 records left starts a large run. While it is `enriching`, they page `GET /jobs/{id}/results?page_size=500`. Every scraped row is returned, including the ones the cap will later exclude. They then cancel, and nothing is billed. Even without the cancel, the rows beyond quota were readable in that window.
**Impact:** the product's core value metric can be bypassed, and the free leads are repeatable.
**Fix:** serve per-job results only when `job.status == 'done'` and `billing_applied_at IS NOT NULL`, which is the rule `segments.py` already follows. Also make the release of a reservation mark that job's rows undelivered in the same transaction.
**Regression test:** a job in `enriching` with inserted rows returns 0 rows (or 409) from `/results`. A job cancelled after reservation returns 0 rows, and `records_used` goes back to its pre-run value.

### B-3. P2: Trial accounts can buy paid Tracerfy lookups that can never be billed

**Evidence**
- Registration creates every account as `plan="pro"` for the trial (`src/api/routes/auth_helpers/registration.py:188`).
- The skip-trace gates check the plan only: `SKIP_TRACE_ADDON_PLANS` includes Pro (`src/config/constants.py:250-254`), the config gate is at `src/api/routes/scrapers.py:221-230`, and the enqueue gate excludes only `starter` (`src/workers/tasks_helpers/enrich.py:2283-2291`). Nothing checks trial status, whether a Stripe subscription exists, or `is_frozen`.
- Billing refuses anything other than an `active` subscription that carries the metered item (`src/api/billing/skip_trace_usage.py:308-311`, `:383`). A trial user has none, so overage goes to `non_billable` (`no_subscription_ever`) or `needs_review` (`src/workers/tracerfy_ingest.py:236-241`, `skip_trace_usage.py:426-427`).
- The 09-16 audit marked "Starter/free: spend is 0" as secure (`tasks/audit-billing.md` D4). That missed the fact that trial users are on plan `pro`, not `starter`.

**Attack:** register (verification needs only an email address), enable `skip_trace_enabled` on a config or a batch (Pro batches are allowed), and run it. Each trial account can buy up to 1,000 lookups, bounded by the trial record cap (`src/config/settings.py:450-455`). Address-only leads go as `advanced` at 2 credits each. None of it is recoverable. With many email addresses this repeats, and a few trial accounts can use up the global `SKIP_TRACE_DAILY_ROW_CAP`, which pauses lookups for every paying tenant (B-4).
**Fix:** block skip-trace enqueue unless the account has an active or trialing Stripe subscription with the metered item. At minimum, block while `trial_ends_at` is set and `first_paid_at IS NULL`. Mirror the check in `_enforce_plan_feature_gates` so the UI tells the user why.
**Regression test:** a trial user (plan pro, `first_paid_at` NULL, no customer) with `skip_trace_enabled` produces 0 `pending_skip_trace_rows` after a run.

### B-4. P2: The global daily cap is soft, global, and counts rows (F-12 residual)

**Evidence**
- The cap is read once at the top of the tick (`src/workers/skip_trace_dispatcher.py:72-113`). After that the tick submits up to `SKIP_TRACE_MAX_BATCHES_PER_TICK` (2, `src/config/settings.py:356`) batches of up to 5,000 rows each (`skip_trace_dispatcher.py:209-214`, `:274`). With a cap of 1,000 and 999 already spent, one tick can send 10,000 more rows.
- It counts rows by `submitted_at` (`:88-91`), not credits. Advanced traces cost 2 credits.
- It covers all tenants and has no per-account share, so one account (B-3, or an Agency tenant with `records_limit=-1`) can use it all and stop lookups for every paying customer for 24h.
- The code default is `0`, which disables it (`settings.py:365`). The brief says production is set to 1000. I could not verify that.
- No per-account ceiling exists: migration 101 creates the action ledger only, and nothing writes it (`alembic/versions/101_contact_lookup_action_schema.py:1-6`).

**Fix:** clamp each tick's batch size to `cap - spent_today` (and budget in credits, weighting advanced as 2). Add the per-account per-window ceiling planned for 1b-1, enforced in the dispatcher's FIFO query against `skip_trace_used_this_month` plus in-flight `submitting`/`submitted` rows.
**Regression test:** with cap=1000 and 999 rows submitted, one tick submits at most 1 row. With per-account ceiling N, a tenant with N rows in flight has 0 rows selected.

### B-5. P2: The Tracerfy webhook body still drives billing and ingest (carried A7b/A7c)

**Evidence**
- The body's `rows_uploaded` and `credits_deducted` pass straight through (`src/api/routes/webhooks.py:144-149`) and overwrite the dispatcher's own values (`src/workers/tracerfy_ingest.py:832-840`). `report_usage_from_webhook` then reads `rows_uploaded` to decide whether `unmatched` rows bill (`src/api/billing/skip_trace_usage.py:611-622`).
- The body's `download_url` is fetched. The host pin accepts any `*.digitaloceanspaces.com` host whose first label is `tracerfy`, in any region, and allows `http` (`tracerfy_ingest.py:437-452`). If DigitalOcean bucket names are unique only per region (I have not verified this), an attacker could own `tracerfy.<other-region>.digitaloceanspaces.com` and serve a CSV they control, writing phone/email values onto another tenant's leads by address key. Address keys are guessable because the leads are public county records.
- A forged URL that keeps failing lets `mark_queue_permanently_failed` make a paid queue `errored` (`tracerfy_ingest.py:337-360`). The genuine webhook is then ignored (`:529-534`).

**Prerequisites:** the shared webhook secret. That secret has been sent in URL paths (F-22). The prior sub-audit rated this P1. I rate it P2 because it needs the secret, and it becomes P1 again if the secret is exposed and not rotated.
**Fix:** ignore `rows_uploaded`/`credits_deducted` from the body (keep the dispatcher's values at `skip_trace_dispatcher.py:1163`, or re-fetch the queue with the bearer token). Pin the download URL to the configured API host plus an exact bucket host, require `https`, or drop the body URL and fetch it server-side. Never make a paid queue terminal on an unverified URL.
**Regression test:** a webhook with `rows_uploaded=999999` leaves `skip_trace_queues.rows_uploaded` at the submitted value. `https://tracerfy.fra1.digitaloceanspaces.com/x` and `http://` URLs are refused unless configured.

### B-6. P3: Legacy path secret still live; one log path unredacted (F-22 residual)

**Evidence:** `TRACERFY_LEGACY_PATH_ENABLED: bool = True` (`src/config/settings.py:345`). The route accepts the path secret (`src/api/routes/webhooks.py:215-216`). The access log is scrubbed (`main.py:132-157`), but `_unhandled_exception_handler` logs `path=%s` with `request.url.path` (`main.py:103-110`). The global redaction patterns have no `/webhooks/tracerfy/` rule (`src/utils/logger.py:31-50`). An exception during `ingest_tracerfy_batch.delay(...)` (for example, the broker is down; `webhooks.py:143-149`) therefore logs the live secret. The Railway edge also logs URLs independently.
**Fix:** point Tracerfy at the header route, set the flag to false in production, rotate the secret, and delete the route. Until then, add `_PATH_SECRET_RE` to `_SECRET_PATTERNS`.
**Regression test:** a record logged through `logging.getLogger("api.unhandled")` containing `/webhooks/tracerfy/abc123` comes out redacted.

### B-7. P3: Disputes and refunds do not revoke entitlement

**Evidence:** the dispatcher handles only checkout, subscription created/updated/deleted and `invoice.payment_failed` (`src/api/routes/billing.py:1819-1844`). `charge.dispute.created`, `charge.refunded` and `invoice.voided` are ignored. Stripe does not cancel a subscription when a dispute is opened, so a customer who pays, charges back and keeps the subscription open keeps the tier until someone intervenes by hand.
**Fix:** handle `charge.dispute.created` (freeze through `subscription_status`, or cancel, and alert ops).
**Regression test:** a dispute event for the recorded customer produces `is_frozen(user) is True`, or an ops alert.

### B-8. P3: Still open from 09-16 (short)

- `ENTITLEMENT_ENFORCEMENT: bool = False` (`src/config/settings.py:194`). If production matches the default, the county-count and record-type matrix are audit-only. The production value needs checking.
- `livemode` is never checked on a Stripe event (no occurrence in `src/`).
- `_PRODUCT_TO_PRICE` collapses to a `""` key when a `STRIPE_PRODUCT_*` is empty (`billing.py:1019-1024`, `:1406-1410`). It can only resolve to a price that is sold, so it is not a bypass. It is configuration hygiene.
- A custom date range has no maximum span (`src/api/schemas.py:411-440`; `_ordered_window` only orders it, `src/workers/tasks_helpers/dates.py:73`). Delivered records are capped by quota except on Agency, so the cost is scraper load, and Agency lookup volume per B-4.

---

## 2. Non-findings (verified)

**Stripe**
- **Checkout allowlist is server-side.** The client sends only `price_id`. It must be in `_PRICE_TO_PLAN` and must not be a legacy price (`billing.py:1026-1029`). Amount, plan and quantity are set by the server (`:1113-1117`). Success and cancel URLs come from `settings.FRONTEND_URL` (`:1130-1131`).
- **One subscription per customer.** A per-user advisory lock (4243) is shared by checkout and change-plan (`:1052-1056`, `:1434-1437`). The user row is re-read under the lock. The live-subscription check is asked of Stripe with `status="all"` and fails closed (`:863-885`). Open sessions are expired and Stripe re-checked, both before and after the new session is created (`:1097-1167`). Change-plan modifies the existing subscription and never creates one (`:1440-1454`, `:1588`). A duplicate slipping through triggers an alert (`:1991-2011`).
- **Plan change is refused** on `past_due`, `unpaid` and `incomplete` (`:1456-1486`). `users.plan` is written only by the webhook (`:1604-1607`).
- **No Stripe-side trial.** Checkout sets no `trial_period_days` (`:1119-1138`). The app trial can be granted once per account through `trial_consumed_at` (`billing_entitlement.py:173-177`), so trial, pay, cancel, trial again grants nothing. New email addresses mean new trials by design (see B-3 for the cost side).
- **Promotion codes.** Stripe validates them (`allow_promotion_codes=True`, `:1137`). A card is always collected (`payment_method_collection="always"`, `:1128`). A 100%-off first invoice activates only when the subscription status is `active`/`trialing` (`:1977-1984`). Single-customer coupons are restricted to monthly products in Stripe, and an annual subscription carrying one raises an alert (`:2162-2186`).
- **A customer id alone grants nothing.** Activation needs an entitled subscription status (`:1977`, `:2134`, `:2243-2253`). Skip-trace overage is released only by `assert_billable` (an active subscription with the metered item whose period contains the usage, `skip_trace_usage.py:289-427`). `expire_trials` asks Stripe rather than trusting the customer id (`src/workers/scheduler_helpers/billing.py:377-396`).
- **Webhook signature** is verified on the raw body (`billing.py:1712-1722`). The secret must be at least 20 characters (`:1703`). Requests are rate-limited before the HMAC check (`:1710`).
- **Idempotency ledger (mig 095):** a per-event advisory lock plus a ledger check, the handler, and the ledger insert all run in ONE transaction (`:1739-1765`). A duplicate waits and then sees the row. A timeout returns 409 so Stripe retries. Out-of-order delivery is handled by re-reading Stripe under a `FOR UPDATE` row lock taken first (`:2095-2132`, `:2306-2339`, `:2406-2436`).
- **Cross-account binding:** a session customer that does not match the stored customer is refused (`:1958-1966`). Session metadata is written by the server only.
- **Portal return URL** is fixed to `settings.FRONTEND_URL/settings` (`:1667-1670`). No open redirect.
- **Dunning:** `past_due` starts a grace period that is never extended (`billing_entitlement.py:104-127`, `:271-279`). After it expires the account is frozen, and `unpaid`, `incomplete`, `incomplete_expired` and `paused` freeze at once (`src/api/quota_window.py:57-59`, `:174-196`). A frozen account cannot start jobs (`src/api/quota.py:137-142`) and its window does not roll (`quota_window.py:217`). `subscription.deleted` drops the account to Starter unless another entitled subscription survives (`billing.py:2285-2355`). A lost deletion after cancel-at-period-end is covered by `entitlement_ends_at` (`quota.py:152-157`).

**Quota / entitlement**
- **Client cannot set plan or quota.** `ProfileUpdate` uses `extra="forbid"` (`src/api/schemas.py:276-287`). The checkout and change-plan models carry only `price_id` (`billing.py:1001-1002`, `:1380-1381`). No route writes `plan`, `records_limit` or `records_used` from input.
- **API-key callers** need Business or above (`src/api/auth.py:304-305`). After `subscription.deleted` the plan becomes `starter` (`billing_entitlement.py:330-331`), so a key loses access.
- **Record reservation** is atomic and released with an exclusive CAS (`src/workers/tasks_helpers/status.py:359-418`). The flaw is WHEN rows become readable (B-2), not the counter.
- **Upgrade and cancel farming of quota** is closed: the window moves only on a first conversion or a genuine lapse (`billing_entitlement.py:14-28`, `:165`). B-1 is about the PLAN tier, not a second quota bucket.
- `JobCreate.trigger="test"` has no billing effect (no consumer outside `src/api/routes/jobs.py:324`).

**Tracerfy / contact lookup**
- **Credentials stay on the server:** `TRACERFY_API_TOKEN` is used only by the worker's `submit_batch` (HTTPS enforced plus SSRF check, `src/scrapers/enrichment/skip_trace.py:555-580`). No API route calls Tracerfy. Users can only set `skip_trace_enabled` on their own configs (`scrapers.py:221-230`).
- **Per-lead claim idempotency (PR #349/#354):** there is one set-based claim through a join to `results` and `jobs`, pinned to the tenant, job and `not_attempted` (`src/workers/skip_trace_claim.py:460-481`). It is backed by migration 100's partial unique index, checked by identity (`:173-231`), and the claim fails closed without it (`:373-379`). The job advisory lock is asserted, not just documented (`:401-406`). Losers withdraw their own rows by primary key (`:558-573`). The charged-but-unanswered re-check runs again under the lock (`enrich.py:2351-2383`).
- **Double submit / replay:** the dispatcher commits a `submitting` claim before the POST, has no retry decorator, and keeps the claim on an unknown outcome (per 09-16 D2, unchanged). Ingest locks the queue row and no-ops once `completed`/`billed`/`errored` (`tracerfy_ingest.py:529-534`). The counter advance commits in the same transaction as the status flip (`tracerfy_ingest.py:842-873`, `skip_trace_usage.py:163-180`). The Stripe MeterEvent id is stable per (queue, user).
- **Money invariant in the dispatcher:** duplicated active claims are excluded before spend (`skip_trace_dispatcher.py:253-258`). Rows go only for delivered, non-over-quota, eligible leads (`:259-270`, `:287-300`).
- **CSV injection from provider fields:** phones are normalised to 10 digits (`src/utils/lead_formatting.py:136-152`). Email, name and phone_type pass through `sanitize_for_csv` (`src/utils/lead_export.py:584-592`). JSON and Excel use the same row builder (`src/utils/data_exporter.py:176-190`).
- **Provider errors are not shown to users.** `TracerfyError` text stays in worker logs and ops alerts. The webhook handler logs only payload keys, never the body (`webhooks.py:102-107`).
- "No party_name is still billable (2-credit advanced)" is a margin and policy question, not a security defect: the customer is charged per lookup, and it is bounded by B-3 and B-4.

---

## 3. Caveats
- The Stripe proration behaviour in B-1 is taken from Stripe's documented `create_prorations` semantics. Confirm the cancel-at-period-end variant in test mode. The core upgrade-then-downgrade path does not depend on that variant.
- Several outcomes depend on production env values I cannot read: `SKIP_TRACE_DAILY_ROW_CAP`, `TRACERFY_LEGACY_PATH_ENABLED`, `ENTITLEMENT_ENFORCEMENT`, `SKIP_TRACE_ENABLED` (read by both api and worker), and whether `TRACERFY_WEBHOOK_SECRET` has been rotated.
- Other files appeared in the worktree during the audit (`.rm_tmp.py`, `.unlazy/`), and HEAD moved from `25a04eaf` to `fc38e620`. Something else is using this worktree.
