# Billing / Stripe / Entitlement / Quota / Cost-Abuse Security Audit

**Scope:** billing, Stripe, plan entitlements, quota enforcement, paid-API cost abuse.
**Worktree:** `C:/Users/Windows/bl-wt-secaudit` (fresh tip of `origin/main`, HEAD `60f1b00`).
**Method:** read-only static review. No source file modified. `pytest` was NOT run (bare pytest reads the production `.env` and has wiped prod twice).
**Date:** 2026-09-16.

**Headline:** the Stripe surface is genuinely hardened — among the most carefully reasoned billing code in the repo. The exploitable holes are **not** in Stripe. They are (1) a free-leads loop that defeats the record quota end-to-end, (2) the still-mounted legacy Tracerfy path-secret route combined with a billing decision driven by an attacker-supplied integer, and (3) the complete absence of any skip-trace spend cap.

**Verdict: 4 × P1 (NO-GO), 1 × P2, 8 × P3. No P0.**

---

## 0. NO-GO summary

| # | Finding | Sev | Location |
|---|---|---|---|
| 1 | Cancel during `enriching` refunds quota while the leads stay fully readable — repeatable free-leads loop | **P1** | `src/api/routes/jobs.py:391-462`, `src/workers/tasks.py:2001-2008`, `src/workers/tasks_helpers/status.py:347-360` |
| 2 | Legacy Tracerfy path-secret route still mounted; secret is in access logs | **P1** | `src/api/routes/webhooks.py:169-183` |
| 3 | Webhook-body `rows_uploaded` drives a real billing decision | **P1** | `src/workers/tracerfy_ingest.py:802-829`, `src/api/billing/skip_trace_usage.py:614-634` |
| 4 | No skip-trace spend cap / circuit breaker; unbounded on Agency | **P1** | `src/workers/skip_trace_dispatcher.py:89-140`, `src/config/plans.py:78-83`, `src/config/settings.py:184` |
| 5 | Skip-trace spend during dunning is paid but unbillable | P2 | `src/api/billing/skip_trace_usage.py:382-427` |
| 6 | `livemode` never checked on any Stripe handler | P3 | `src/api/routes/billing.py:1714-1722` (absence) |
| 7 | `test_api_key_plan_guard.py` is a tautology over a constant | P3 | `tests/test_api_key_plan_guard.py:1-9` |
| 8 | `ENTITLEMENT_ENFORCEMENT` defaults `False` — county cap + record-type matrix audit-only in code | P3 | `src/config/settings.py:194`, `src/api/entitlements.py:365-392` |
| 9 | Stale `users` snapshot can skip the cap block entirely | P3 | `src/workers/tasks.py:468`, `:1583`, `src/db/session.py:112-114` |
| 10 | Empty `STRIPE_PRODUCT_*` collapses `_PRODUCT_TO_PRICE` to `{"": agency_price}` | P3 | `src/api/routes/billing.py:1019-1024`, `:1406-1410` |
| 11 | Two dispatcher `Result` writes miss the mandatory `user_id` pairing | P3 | `src/workers/skip_trace_dispatcher.py:772-779`, `:874-881` |
| 12 | `POST`/`PATCH /scrapers` carry no rate limit (they toggle paid skip-trace) | P3 | `src/api/routes/scrapers.py:351`, `:529` |
| 13 | `/billing/usage` reports raw, not effective, `records_limit` | P3 | `src/api/routes/billing.py:677` |

---

## A. STRIPE WEBHOOKS

### A1. Signature verified against the RAW request body
**CONFIRMED SECURE CONTROL — INFO**

`src/api/routes/billing.py:1712-1722`:

```python
payload = await request.body()          # raw bytes; nothing parsed beforehand
try:
    event = stripe.Webhook.construct_event(
        payload, stripe_signature, settings.STRIPE_WEBHOOK_SECRET
    )
except stripe.error.SignatureVerificationError:
    raise HTTPException(status_code=400, detail="Invalid webhook signature")
```

- `stripe_signature: str = Header(..., alias="stripe-signature")` at `:1681` is **required**, so a missing header is a 422 before the body is touched.
- Secret is length-checked (`>= 20`) at `:1703-1704` → 503 if unset/short.
- There is **no** `await request.json()` or Pydantic body model on this route, so the bytes fed to `construct_event` are byte-identical to what Stripe signed. No re-serialization anywhere on the path.
- The `webhook` rate-limit zone runs **before** the HMAC (`:1710`) so a signature-spray cannot burn CPU, and that zone **fails closed** during a Redis outage (`src/api/middleware/rate_limit.py:117` — `_FALLBACK_ZONES = {"auth","webhook","stripe"}`).

Team lead has confirmed this empirically in production (forged sig → 400, missing header → 422). Consistent with the code.

### A2. Idempotency — is the migration-095 ledger consulted BEFORE handling, or merely written after?

**ANSWER: it is consulted BEFORE handling. It is a read-then-dispatch guard, not a write-after audit log.**
**CONFIRMED SECURE CONTROL — INFO**

`src/api/routes/billing.py:1739-1765` — the ordering is the whole point:

```python
event_id = event.get("id") or ""
try:
    if event_id:
        await db.execute(text(f"SET LOCAL lock_timeout = '{_WEBHOOK_LOCK_TIMEOUT}'"))
        await db.execute(
            text("SELECT pg_advisory_xact_lock(4244, hashtext(:eid))"),
            {"eid": event_id},
        )
        await db.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
        already = await db.execute(
            text("SELECT 1 FROM stripe_webhook_events WHERE event_id = :eid"),
            {"eid": event_id},
        )
        if already.first() is not None:
            _logger.info("stripe webhook dedup: already processed %s", event_id)
            return {"received": True}          # <-- SHORT-CIRCUITS BEFORE DISPATCH

    notifications = await _dispatch_stripe_event(
        event["type"], event["data"]["object"], db
    )

    if event_id:
        await _record_stripe_event(db, event_id, event["type"])
    await db.commit()
```

Four properties worth recording:

1. **Consulted first.** The `SELECT 1` runs and returns early *before* `_dispatch_stripe_event`. The insert (`_record_stripe_event`, `:1800-1807`) happens after the handler, but that is deliberate — see (2).
2. **Transactional.** The ledger row and the handler's mutations commit or roll back **together** (`:1765`). The code comment at `:1724-1733` documents the prior defect this replaced: a Redis key written *before* the handler ran meant a handler that raised, a failed commit, or a process killed mid-request (every deploy restarts the api) left an event Stripe would retry and we would skip — and for a fully discounted checkout that event is the only thing that activates the plan.
3. **Serialised.** `pg_advisory_xact_lock(4244, hashtext(event_id))` makes a concurrent duplicate delivery of the *same* event wait for the first attempt to commit (then see the row and ack) or roll back (then process it itself). A killed process drops its connection and with it the lock.
4. **Bounded.** `lock_timeout = '15s'` (`_WEBHOOK_LOCK_TIMEOUT`, `:1811`); on `55P03` (`_LOCK_NOT_AVAILABLE`, `:1813`) the route returns **409** (`:1766-1774`) so Stripe retries later rather than dropping the event.

Table + RLS: `alembic/versions/095_stripe_webhook_events.py:40-72` — `REVOKE ALL FROM PUBLIC/anon/authenticated`, `GRANT SELECT, INSERT ON ... TO bridgeleads_app`, RLS enabled with select+insert policies. Append-only; the app role has no UPDATE/DELETE.

### A3. Replay of a valid old event
**CONFIRMED SECURE CONTROL — INFO**

Two independent layers:

- **Timestamp tolerance.** `construct_event` is called with three positional args only (`billing.py:1715-1717`) — **no `tolerance=` override**, so Stripe's default 300-second window applies. A captured-and-replayed event older than 5 minutes fails signature verification outright and never reaches the ledger.
- **Durable state gating.** Even past both the tolerance and the ledger, the one destructive action — zeroing `records_used` — is gated on durable columns, not on the event. `src/api/billing_entitlement.py:165`:

```python
fresh = user.first_paid_at is None or user.paid_entitlement_ended_at is not None
```

Both are cleared in the same call (`:178`, `:192-193`). So **a replayed `checkout.session.completed` cannot re-grant entitlement or mint a second quota bucket.** Cancel-and-resubscribe *inside* a live entitlement never sets `paid_entitlement_ended_at`, so that farming route grants nothing either. The module docstring states the invariant explicitly (`billing_entitlement.py:14-28`): plan and status changes never move the anchor or the window; the anchor moves on exactly three events (first trial→paid conversion, resubscribe after a genuine lapse, explicit admin action).

### A4. Out-of-order events cannot overwrite newer state
**CONFIRMED SECURE CONTROL — INFO**

Handlers do not trust the event body — they **re-read from Stripe**, so the last handler to run writes the current truth regardless of arrival order:

- `billing.py:2100-2107` — `stripe.Subscription.retrieve(subscription_id, expand=[...])` in `_handle_subscription_updated`.
- `billing.py:1907-1910` — `stripe.Subscription.retrieve(...)` in `_handle_checkout_completed`.
- `billing.py:2410` / `:2426` — `Invoice.retrieve` + `Subscription.retrieve` in `_handle_payment_failed`.

Critically, the user row is locked `FOR UPDATE` **before** the re-read, in all three handlers: `:2095-2097`, `:2296-2298`, `:2396-2398`. The comment at `:2089-2094` documents why: "Taken BEFORE the re-read below: locked after it, a slow read of an older state could win the lock second and overwrite the newer state another delivery had just written. Locked first, the handler that writes last is also the one that asked Stripe last."

No version column or `created` comparison is needed because of this ordering. If Stripe is unreachable the update path falls back to the event body with a warning (`:2118-2122`) rather than dropping a real plan change; the `customer.subscription.created` path instead **raises and retries** (`:2109-2117`), because a creation is only ever acted on when Stripe confirms it is entitled *now*.

Additional ordering guards worth noting:
- `customer.subscription.created` applies nothing unless status is `active`/`trialing`, judged on the re-read (`:1826-1833`, `:2124-2129`) — otherwise an `incomplete` Agency subscription would rank as an upgrade and grant Agency before anything was paid.
- An unrecorded subscription may only *change* entitlement once entitled (`:2233-2243`).
- `customer.subscription.deleted` asks Stripe for a surviving entitled subscription before downgrading (`:2308-2341`), and **raises** if it cannot ask — "unknown must not read as 'none left'".

### A5. `livemode` is never checked
**CONFIRMED VULNERABILITY (defense-in-depth gap) — P3**

`grep -rn livemode` across the entire repository returns **zero matches**. No handler asserts `event["livemode"] is True`.

**Attack path:** not remotely reachable. Stripe signs test-mode events with the *test* endpoint's secret, which is not `STRIPE_WEBHOOK_SECRET` in production, so a test-mode event fails the HMAC. An attacker cannot register a webhook endpoint in our Stripe account. The real exposure is **operational**: a misconfiguration that points a test-mode endpoint at the prod URL, or a test secret leaking into the prod env, would silently grant plans from test-mode checkouts with no second gate.

**Fix:** after `construct_event`, add
```python
if not event.get("livemode") and settings.ENVIRONMENT == "production":
    _logger.error("test-mode Stripe event on the production endpoint: %s", event.get("id"))
    return {"received": True}        # 200, but do not dispatch
```

### A6. Inventory of ALL inbound webhooks

**Exactly two inbound receivers exist in the whole application. Both authenticate.**

| Endpoint | Auth mechanism | Replay possible? |
|---|---|---|
| `POST /billing/webhook` (Stripe) — `billing.py:1676` | HMAC over raw body + 300s timestamp tolerance + transactional event ledger | **No** (A2/A3) |
| `POST /webhooks/tracerfy` (header) — `webhooks.py:154` | `hmac.compare_digest` on `X-Tracerfy-Webhook-Secret`, rate-limited *before* the compare | **Yes, see A7** |
| `POST /webhooks/tracerfy/{provided_secret}` (**LEGACY, STILL MOUNTED**) — `webhooks.py:169` | same shared secret, **in the URL path** | **Yes, see A7** |

**There is no inbound receiver for Resend, PhoneBurner/dialer, or Zapier.** All three are outbound-only:
- `src/workers/webhook_delivery.py` — outbound job-summary webhook push.
- `src/workers/dialer_connectors/generic_webhook.py` + `phoneburner` — outbound dialer push (`REGISTERED_DIALER_VENDOR_IDS`, `src/config/constants.py:206-207`).
- `src/workers/delivery.py` — outbound Resend email send.

I enumerated all 38 state-changing routes across `src/api/routes/`. Every one other than the two webhook receivers carries `CurrentUser`, `Depends(require_plan(...))`, or `Depends(require_admin_mfa)`. Routers registered at `main.py:83-91` (auth, scrapers, jobs, billing, webhooks, segments, batches, notifications, analytics). **No unauthenticated state-changing endpoint exists.**

The Tracerfy header route's own design is sound in isolation — `webhooks.py:154-166`:

```python
await rate_limit(request, zone="webhook")        # 120/min, fails CLOSED on Redis outage
_verify_tracerfy_secret(request.headers.get(_SECRET_HEADER))
```

with `hmac.compare_digest` at `:40-42`, 401 on mismatch, 503 if the server has no secret, and the provided value never logged (`:64-71`). Secret is `token_urlsafe(32)` (`:186-191`). Brute force is not feasible.

### A7. P1 — Legacy Tracerfy path-secret route + body-driven billing decision
**CONFIRMED VULNERABILITY — P1 (NO-GO)**

#### A7a. The legacy route is still mounted and the migration is unfinished

`src/api/routes/webhooks.py:169-183`:

```python
@router.post("/tracerfy/{provided_secret}", status_code=status.HTTP_200_OK)
async def tracerfy_webhook_legacy(provided_secret: str, request: Request) -> dict:
    """LEGACY: secret in the URL path. Deprecated — the path secret leaks into
    access logs. Migrate Tracerfy to `POST /webhooks/tracerfy` with the
    `X-Tracerfy-Webhook-Secret` header, rotate TRACERFY_WEBHOOK_SECRET, then this
    route can be removed.

    Header-first (Codex): if the header is present it is authoritative ...
    Current Tracerfy traffic sends no header, so this branch is inert until migration.
    """
```

The docstring states outright that current Tracerfy traffic sends **no header**. The secret is therefore **travelling in URLs right now** — through Railway access logs, any intermediate proxy, `Referer` headers, and Tracerfy's own webhook-config UI. **Treat `TRACERFY_WEBHOOK_SECRET` as already disclosed.**

#### A7b. With the secret, the webhook body drives a real billing decision

`rows_uploaded` is taken straight from the request body (`webhooks.py:147`) and persisted — `src/workers/tracerfy_ingest.py:802-812`:

```python
db.execute(update(SkipTraceQueue).where(...).values(
    status="completed",
    download_url=download_url,
    completed_at=now,
    rows_uploaded=rows_uploaded,        # <-- straight from payload.get("rows_uploaded")
    credits_deducted=credits_deducted))
```

and `report_usage_from_webhook(db, queue_id)` is invoked at `:829` **inside the same transaction**, where that just-written value decides whether unmatched lookups bill — `src/api/billing/skip_trace_usage.py:614-634`:

```sql
SELECT COALESCE(q.rows_uploaded, 0) >= COUNT(p.id)
FROM skip_trace_queues q
JOIN pending_skip_trace_rows p ON p.tracerfy_queue_id = q.tracerfy_queue_id
WHERE q.tracerfy_queue_id = :qid
GROUP BY q.rows_uploaded
```
→ `billable_states = ("completed","unmatched") if accepted_all else ("completed",)`

#### A7c. Three concrete abuses against pending, already-paid-for queues

Tracerfy queue ids are small sequential integers (`162456`, `365` appear verbatim in code comments), so enumeration is trivial.

**(a) Destroy paid results.** POST `{"id": <pending queue>, "pending": false, "download_url": "https://tracerfy.<allowed host>/<anything>"}`. A non-200 raises `TracerfyError` (`src/scrapers/enrichment/skip_trace.py:793-794`) → `autoretry_for=(Exception,)` exhausts 3 retries → `on_failure` → `mark_queue_permanently_failed` sets the queue `errored` (`tracerfy_ingest.py:318-352`, `:894-896`). The genuine webhook then hits the pre-download guard `if _pre[0] in ("completed","billed","errored")` (`:512-517`) and is discarded **forever**. Money already spent with Tracerfy; results never applied to any lead.

**(b) Force-bill the whole batch as overage.** If the forged URL returns 200 with non-CSV content, `csv.DictReader` yields no usable rows → every pending row falls into `unmatched_pending` (`:760-766`) → billed per D3 below. Sending `rows_uploaded: 999999` forces `accepted_all = True`, so every unmatched row bills. Customers are charged real Stripe overage for lookups whose results were just destroyed.

**(c) Suppress a genuine delivery.** The attacker's forged ingest wins the race; the real one no-ops as `already_completed`.

#### A7d. What IS closed (so the fix is targeted)

SSRF is properly shut: `_host_is_tracerfy` pins the fetch to the configured API host or `tracerfy.*.digitaloceanspaces.com` **before** any DB or network work (`tracerfy_ingest.py:399-437`, `:484-491`), and `download_tracerfy_csv` still routes through `safe_get_following` with per-hop revalidation and URL redaction (`skip_trace.py:736-796`). **But a host pin is not identity.** The edge dedup was deliberately removed (`webhooks.py:136-142`) on the reasoning that a forged first webhook could suppress a genuine retry — correct as far as it goes, but the worker-side guard it defers to keys only on `queue_id` + status and validates nothing else about the body.

#### A7e. Fix, in priority order

1. **Delete** `tracerfy_webhook_legacy` (`webhooks.py:169-183`), complete the header migration, and **rotate `TRACERFY_WEBHOOK_SECRET`**.
2. Stop trusting the body's `rows_uploaded` / `credits_deducted` for a billing decision. Either keep the dispatcher's own submit-time `rows_uploaded` (written at `skip_trace_dispatcher.py:747`) and never overwrite it from the webhook, or re-fetch the queue via `GET /v1/api/queue/:id` with the bearer token and use the provider's authenticated value.
3. Bind `download_url` to the queue: refuse a URL whose path does not match the one recorded at submission, or drop the body URL entirely and re-fetch server-side.
4. Never let an ingest failure on an *unverified* URL make a paid queue terminal — `mark_queue_permanently_failed` should require a provider-confirmed URL and otherwise alert-and-hold.

---

## B. SERVER-AUTHORITATIVE PRICING

### B1. Checkout is a closed server-side allowlist
**CONFIRMED SECURE CONTROL — INFO**

The client sends **only** `price_id`. No amount, no plan name, no quantity:

```python
class CheckoutRequest(BaseModel):   # billing.py:1001-1003
    price_id: str

class ChangePlanRequest(BaseModel): # billing.py:1380-1382
    price_id: str
```

The allowlist is built at import from env-configured Stripe Price IDs — `billing.py:368-376`:

```python
_PRICE_TO_PLAN: dict[str, tuple[str, int, str]] = {
    pid: (p["id"], p["records_limit"], interval)
    for p in _PLANS
    for pid, interval in ((p.get("stripe_price_id"), "month"),
                          (p.get("stripe_price_id_annual"), "year"))
    if pid
}
```

and enforced at `billing.py:1028-1044` (checkout) and `:1413-1424` (change-plan), identically:

```python
if stripe_price_id not in _PRICE_TO_PLAN or stripe_price_id in _LEGACY_PRICE_TO_PLAN:
    raise HTTPException(status_code=400, detail="Invalid plan")
if not stripe_price_id.startswith("price_"):
    _logger.error("checkout: resolved id %r is not a 'price_' id ...")
    raise HTTPException(status_code=503, detail="Billing is temporarily unavailable...")
```

- `plan=Agency`, `price=1`, an arbitrary `price_...` string → **400 "Invalid plan"**.
- A *legacy* price (recognised on existing subscriptions so their webhooks keep mapping) is explicitly **excluded from new sales** — `_LEGACY_PRICE_TO_PLAN` built by `_legacy_plan_prices` (`:379-417`), which also refuses duplicates, malformed entries, and any id that is currently sold.
- `metadata={"user_id": current_user.id, "price_id": price_or_product_id}` at `:1132` is stamped from the **authenticated principal**, never from the body. A tampered `metadata.user_id` is impossible from the client side, and the webhook additionally cross-checks the session customer against the stored one (`:1958-1968`) and **refuses** on mismatch.
- `records_limit` comes from the server catalog tuple (`:1113`, `:1426`), never from the request.

Supporting invariants:
- Both endpoints hold `pg_advisory_xact_lock(4243, hashtext(uid))` in the **same namespace** (`:1054`, `:1435`) so checkout and change-plan cannot interleave and produce a second subscription.
- The duplicate-subscription guard asks Stripe (`_live_subscription`, `:863-885`, `status="all"`), expires outstanding open sessions (`_expire_open_checkout_sessions`, `:888-913`), **re-asks**, creates the session, then asks a **third** time and expires the new session if a subscription appeared (`:1089-1108`, `:1144-1167`).
- It fails **closed**: `_StripeStateUnavailableError` → 503 rather than risking a duplicate subscription (`:1179-1193`). Comment at `:1180-1184`: "creating a Session here is the one outcome that can charge someone twice".
- `change_plan` cannot create a subscription at all — no live one → 409 pointing at checkout (`:1448`, `:1454`).
- `_plan_item_price_id` (`:807-820`) picks the **licensed** item, not `items[0]`, so the metered skip-trace item cannot be mistaken for the plan.
- `_plan_change_items` (`:1206-1287`) **validates** the subscription shape and raises `_UnrecognisedSubscriptionError` rather than guessing when there is not exactly one licensed item.

### B2. P3 — Empty `STRIPE_PRODUCT_*` collapses the product map
**CONFIRMED VULNERABILITY (config-conditional, economically harmless) — P3**

`billing.py:1019-1024` and the identical copy at `:1406-1410`:

```python
_PRODUCT_TO_PRICE = {
    settings.STRIPE_PRODUCT_PRO: settings.STRIPE_PRICE_PRO,
    settings.STRIPE_PRODUCT_BUSINESS: settings.STRIPE_PRICE_BUSINESS,
    settings.STRIPE_PRODUCT_AGENCY: settings.STRIPE_PRICE_AGENCY,
}
stripe_price_id = _PRODUCT_TO_PRICE.get(price_or_product_id, price_or_product_id)
```

All three default to `""` — `src/config/settings.py:146-148`:
```python
STRIPE_PRODUCT_PRO: str = ""
STRIPE_PRODUCT_BUSINESS: str = ""
STRIPE_PRODUCT_AGENCY: str = ""
```

With any of them unset, the duplicate `""` keys collapse in the dict literal and **the last one wins**: `{"": STRIPE_PRICE_AGENCY}`. A client POSTing `{"price_id": ""}` then resolves to the Agency price and passes the allowlist.

**Impact is bounded:** the caller gets an Agency Checkout session at Agency's real $1,499 price. No free upgrade, no privilege escalation, no entitlement without payment. It is an unintended mapping that lets the empty string slip past "Invalid plan", and it silently mis-routes if two product ids are ever configured equal. I could not read the Railway env from a read-only worktree, so the prod exposure is unconfirmed.

**Fix:** `_PRODUCT_TO_PRICE = {k: v for k, v in (...) if k}`.

### B3. Promotion codes — no client-supplied coupon exists
**CONFIRMED SECURE CONTROL — INFO**

`allow_promotion_codes=True` (`billing.py:1137`) hands validation entirely to Stripe, which enforces the code's customer restriction, redemption count, expiry, and — through the coupon's `applies_to` — which products it may discount. **There is no API parameter anywhere that accepts a coupon or promotion-code id.** Grep across `src/api/` for `coupon` / `promotion_code` returns only:
- `_get_founding_offer` (`:450-509`), which only **reads** `FOUNDING25` for a display banner: cached 60s in Redis, `except` narrowed to `stripe.error.StripeError`, fail-closed to inactive. (This was REDTEAM B4 — previously an unauthenticated Stripe call per request inside a bare `except: pass`.)
- `_coupon_of` / `_is_single_customer_promo` (`:937-956`), used only for detect-and-alert.

The annual-abuse vector (a `repeating` coupon applied to one 12-month invoice = a free year) is prevented **in Stripe**, not here: annual prices live on their own Products and `scripts/stripe_single_customer_promo.py` restricts the coupon's `applies_to` to the monthly product (documented `:916-931`). The webhook detects and alerts if the product split is ever undone (`:2152-2176`).

### B4. Trial cannot be re-granted
**CONFIRMED SECURE CONTROL — INFO**

`trial_ends_at` has exactly two writers:
- registration — `src/api/routes/auth_helpers/registration.py:181-190`;
- conversion — which **clears** it (`billing_entitlement.py:175`, `:270`).

`trial_consumed_at` is stamped **permanently** and is the explicit anti-farming control — `billing_entitlement.py:160-177`:

```python
if user.trial_consumed_at is None and user.trial_ends_at is not None:
    user.trial_consumed_at = as_utc(user.trial_ends_at)
user.trial_ends_at = None
if user.trial_consumed_at is None:
    user.trial_consumed_at = now
```

Reinforced in the hourly reconciliation with `COALESCE(trial_consumed_at, trial_ends_at, now)` (`src/workers/scheduler_helpers/billing.py:449-450`, `:556-558`), whose docstring at `:416-418` names the exact attack: "It is the anti-farming control for trial → paid → cancel → trial-again: `trial_ends_at` is CLEARED on conversion, so it cannot answer 'did this account ever trial'." **Trial → paid → cancel → trial-again grants nothing.**

### B5. Return flow grants nothing; no open redirect
**CONFIRMED SECURE CONTROL — INFO**

`billing.py:1130-1131`:
```python
success_url=f"{settings.FRONTEND_URL}/settings?upgrade=success",
cancel_url=f"{settings.FRONTEND_URL}/settings?upgrade=cancelled",
```

Both are **server constants** built from `settings.FRONTEND_URL`. The client supplies no return URL, so there is nothing to allowlist and **no open-redirect surface exists**. The portal return URL is likewise a constant (`:1669`).

Grep for `session_id` / `checkout_session` / `upgrade=success` across `src/api/` finds **no endpoint that finalizes a checkout from a browser redirect** — the only matches are `_expire_open_checkout_sessions` and the `success_url` literal itself. Entitlement is written **exclusively** by the webhook handlers.

Reinforcing this: `change_plan` deliberately does **not** write `users.plan` (`billing.py:1604-1607`): "customer.subscription.updated is the single writer for it, so the plan the app enforces always reflects what Stripe actually did rather than what we asked it to do."

---

## C. ENTITLEMENT + QUOTA

### C1. Full plan-gate matrix — every gated capability is enforced server-side

| Capability | Exact server-side check | JWT | API key | Always on? |
|---|---|---|---|---|
| **API access itself** | `src/api/auth.py:305` `if normalize_plan(user_match.plan) not in BUSINESS_FEATURES_PLANS: raise 403` — **at the authentication layer** | n/a | ✅ | yes |
| **API-key minting** | `src/api/routes/auth.py:450` `Depends(require_plan("business","agency"))` | ✅ | ✅ | yes |
| **Webhook delivery** | `src/api/routes/scrapers.py:215-216` `has_webhook and plan not in BUSINESS_FEATURES_PLANS` → `delivery_violation` | ✅ | ✅ | yes |
| **Dialer delivery** | `scrapers.py:217-218` same set (gated because it POSTs lead PII — Codex) | ✅ | ✅ | yes |
| **Skip tracing (enrichment toggle)** | `scrapers.py:219-220` `BUSINESS_FEATURES_PLANS`; batch `batches.py:229-232`; **worker re-check on the LIVE plan** `src/workers/tasks_helpers/enrich.py:1969-1976` | ✅ | ✅ | yes, 3 layers |
| **Skip-trace metered add-on** | `scrapers.py:225-230` `SKIP_TRACE_ADDON_PLANS` (Pro+), `src/config/constants.py:211-215` | ✅ | ✅ | yes |
| **Export formats** | `scrapers.py:232-234`, `batches.py:233-235` `disallowed_export_formats` (fails closed on unknown plan, `entitlements.py:220-223`) | ✅ | ✅ | yes |
| **Scheduling frequency** | `scrapers.py:236-239`, `batches.py:236` `schedule_frequency_allowed` | ✅ | ✅ | yes |
| **Batch scraping** | `batches.py:184-188` `BATCH_PLANS` (Pro+), `constants.py:220-223` | ✅ | ✅ | yes |
| **Overlap / intersection lists** | `src/api/routes/segments.py:80` `raise plan_limit_http(overlap_violation(...))`; `batches.py:263` | ✅ | ✅ | yes |
| **Priority queue** | `constants.py:172-177` queue routing by `PRIORITY_QUEUE_PLANS` | ✅ | ✅ | yes |
| **Extra counties (county cap)** | `src/api/entitlements.py:377-387` `projected_county_overage` → `county_cap_violation` | ✅ | ✅ | **FLAG-GATED** |
| **Premium record types** | `entitlements.py:373-375` `disallowed_record_types` → `record_type_violation` | ✅ | ✅ | **FLAG-GATED** |

**Nothing is frontend-only.** All feature gates funnel through `raise_plan_features` → `plan_limit_http` → **HTTP 402** with a structured `{code, title, message}` body (`entitlements.py:254-270`, `:656-672`). That path is explicitly **never** flag-gated — `entitlements.py:659-664` records the reasoning: "Adding a second, quieter mode for export format and scheduling would mean the same request is refused or allowed depending on which gate it trips, which is not something a customer can be told."

`disallowed_record_types` and `disallowed_export_formats` both **fail closed**: an unknown or typo'd plan is treated as Starter, never as "all allowed" (`entitlements.py:279`, `:222`).

PATCH passes the **enable-delta** rather than absolute values (`scrapers.py:193-197`, called at `:762`), so an edit can neither bypass a tier nor punish a config whose owner downgraded after creating it. Deliberate grandfathering — and still closed for Starter by the worker-side gate.

### C2. API-key callers get the same plan check as JWT — but the test proves nothing
**Guard: CONFIRMED SECURE CONTROL — INFO. Test: CONFIRMED VULNERABILITY (assurance gap) — P3**

The guard is real and sits at the **authentication** layer, so it covers every API-key request to every route — `src/api/auth.py:291-309`:

```python
key_hash = hash_api_key(token)
result = await db.execute(
    select(User).where(User.api_key_hash == key_hash, User.is_active)
)
user_match = result.scalar_one_or_none()
if user_match is None:
    raise _CREDENTIALS_EXCEPTION
# API access is a Business+ capability — an always-on feature gate ...
from src.config.constants import BUSINESS_FEATURES_PLANS, normalize_plan
if normalize_plan(user_match.plan) not in BUSINESS_FEATURES_PLANS:
    raise HTTPException(...)
```

A key minted while on Business **stops working after downgrade**. `require_plan` (`auth.py:384-402`) resolves through the same `get_current_user`, so JWT and API-key callers reach identical plan logic; both use `normalize_plan` so a hand-set `"Business"` or `"pro "` in the DB is handled the same way everywhere. API keys also carry `amr=[]`, `auth_time=None` and can **never** satisfy an admin MFA step-up (`auth.py:243-256`, `:415-421`).

**But `tests/test_api_key_plan_guard.py` is a tautology over a constant. The entire file:**

```python
# tests/test_api_key_plan_guard.py
from src.config.constants import BUSINESS_FEATURES_PLANS


def test_api_access_is_business_plus():
    assert "business" in BUSINESS_FEATURES_PLANS
    assert "agency" in BUSINESS_FEATURES_PLANS
    assert "pro" not in BUSINESS_FEATURES_PLANS
    assert "starter" not in BUSINESS_FEATURES_PLANS
```

It never constructs a request, never presents a `bl_` token, never imports or touches `auth.py`. **It would pass unchanged if `auth.py:305` were deleted.** The guard is real; the test is aspirational. (Same class as the repo's own `a_test_that_copies_its_impl_asserts_nothing` landmine.)

**Fix:** an integration test that presents a `bl_` API key belonging to a Pro-plan user against a real endpoint and asserts 403 — plus a downgrade case (key minted on Business, plan set to Pro, key now rejected).

### C3. P3 — `ENTITLEMENT_ENFORCEMENT` defaults `False`
**PARTIAL-WEAK CONTROL — P3 (verify the prod env)**

`src/config/settings.py:194`:
```python
ENTITLEMENT_ENFORCEMENT: bool = False
```

With the flag off:
- `enforce_entitlements` logs `"entitlement audit (NOT enforced) ... would_block: ..."` and returns (`entitlements.py:386-392`) — no 402.
- `apply_reconciliation_async` / `_sync` return `(0, 0)` without pausing anything (`:568-575`, `:626-633`).
- `should_block_run` returns `False` for worker/scheduler call sites (`:503`).
- The per-user `pg_advisory_xact_lock(4242, ...)` that closes the distinct-county TOCTOU is **skipped entirely** (`:365-369`), so even the read-then-decide count is unprotected in audit mode.

Meanwhile `/billing/pricing` advertises the caps as real and derives the cells from the enforced matrix (`billing.py:572-585`), with an in-code comment noting that under enforcement "the page promised counties the API answers 402 for."

**I cannot read the Railway env from a read-only worktree.** If the flag is off in production, county limits and the record-type matrix are unenforced, and a Pro account can scrape all seven record types across unlimited counties. **Confirm the deployed value on BOTH the `api` and `worker` services** — they read it independently and a split would be worse than either state.

### C4. Quota consumption IS atomic — no race window
**CONFIRMED SECURE CONTROL — INFO**

`src/workers/tasks.py:1605-1687`. This is not SELECT-then-UPDATE:

```python
_want = db.execute(sa_text(
    "SELECT count(*) FROM results "
    "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
    f"  AND is_duplicate = false AND {address_actionable_sql('results')}"), ...).scalar() or 0
...
_claimed = db.execute(sa_text(
    "UPDATE jobs SET reserved_at = CAST(:at AS timestamptz) "
    "WHERE id = :jid AND reserved_at IS NULL"), ...).rowcount
if _claimed:
    _res_row = db.execute(sa_text(
        "WITH cur AS ("
        "  SELECT u.id, u.records_used, u.records_limit, ..."
        "  FROM users u WHERE u.id = CAST(:uid AS uuid) FOR UPDATE"
        "), w AS ("
        "  SELECT cur.*, " + window_cte_sql() + " FROM cur"
        "), g AS ("
        "  SELECT w.*, LEAST(:want, GREATEST(0, eff_limit - base)) AS granted FROM w"
        ") UPDATE users u SET"
        "    records_used = g.base + g.granted," + window_set_sql("g")
        + "  FROM g WHERE u.id = g.id  RETURNING g.granted, g.new_start"), ...).one()
```

- `SELECT … FOR UPDATE` inside the `cur` CTE takes the **row lock**. Under READ COMMITTED the losing transaction blocks and then EvalPlanQual re-reads the **post-commit** row version, so `base` is the already-decremented value and `LEAST(:want, GREATEST(0, eff_limit - base))` grants only what genuinely remains.
- **N concurrent jobs cannot each consume the full remainder.** There is no read-then-write window to name — the read and the write are one statement under one lock.
- The job-level CAS (`reserved_at IS NULL`, `:1622`) makes a watchdog re-run idempotent: the loser unblocks after `reserved_count` is durable and correctly reuses the existing grant (`:1688-1695`).
- Lazy rollover, pending-downgrade application and counter-zeroing all happen **inside this same statement** (`src/api/quota_window.py:279-330`), so a boundary crossed mid-race can never be observed half-applied.
- `:want` is a server-side `count(*)`. **No client-supplied value reaches quota arithmetic anywhere** — verified across `POST /jobs`, `POST /batches`, `POST /scrapers`: `page_size` is bounded `ge=1, le=500` (`jobs.py:366`), tax filters `le=100_000_000`/`le=1200` (`:372-378`), `sort` is an allowlisted enum (`:387`), and client `date_from`/`date_to` change how much is *scraped*, never how much is *granted*.

**Settlement** (`tasks.py:2120-2154`) nets `billable - reserved` under the same `FOR UPDATE OF u`, CAS-guarded on `billing_applied_at IS NULL` (`:2054-2058`), and commits in the same transaction as the done-CAS (`:2211-2236`). `reservation_is_current_sql` (`quota_window.py:333-374`) correctly refuses to net a grant whose window has since rolled — the old form compared calendar months and silently gave records away under a 20th-of-month anchor.

**The cap is applied to the exported file itself**, not merely accounted afterwards: the enriched re-export filters `is_actionable(res)` (`tasks.py:1873-1886`), so capped rows are physically absent from the delivered artifact.

### C5. P1 — Cancel during `enriching` refunds quota while the leads stay readable
**CONFIRMED VULNERABILITY — P1 (NO-GO). This is the finding that defeats the value metric.**

Five facts compose into a repeatable free-leads loop:

**1. Results are persisted long before quota is touched** — `src/workers/tasks.py:837-845`:
```python
if not _set_status(db, job, "enriching", record_count=len(records)):
    ... return                      # a cancel BEFORE this point saves nothing (correct)
_publish_log(r, job_id, "info", "Saving records to database...", db=db)
# Bulk insert results ...
```

**2. `enriching` is user-cancellable for the entire enrichment + cap + export phase** — `src/config/constants.py:61-67`:
```python
CANCELLABLE_STATUSES = frozenset({PENDING, QUEUED, PROBING, SCRAPING, ENRICHING})
```
and `src/api/routes/jobs.py:336-345` flips the row to `cancelled` with **no coordination with the worker**.

**3. The worker notices only AFTER the cap has already run** — `tasks.py:2001-2008`:
```python
db.refresh(job)
if job.status in _TERMINAL_STATUSES:
    _logger.info("Job %s externally terminalized (%s) after export — skipping billing/delivery", ...)
    _release_claims_of_cancelled_job(db, job_id, _boot_user_id)
    return                          # billing never runs => records_used never settled
```

**4. The reservation is then refunded** — explicitly at `tasks.py:2227-2229`, or within ~5 minutes by the beat sweep, `src/workers/tasks_helpers/status.py:347-360`:
```python
"SELECT id FROM jobs WHERE status IN ('done', 'failed', 'cancelled') "
"  AND reserved_at IS NOT NULL AND billing_applied_at IS NULL AND reserved_count > 0 ..."
```

**5. But the delivered rows are STILL SERVED.** `GET /jobs/{job_id}/results` has **no job-status gate**:
- `jobs.py:391-396` fetches the job for **ownership only**.
- `jobs.py:427-462` filters on `is_duplicate`, the tax cap and `actionable_condition()` — and nothing else.
- `actionable_condition()` only hides rows the cap marked `over_quota` (`src/api/lead_actionability.py:86-89`), i.e. exactly the rows **beyond** the grant.
- The in-quota rows come back in full, including `property_address`, `mailing_address`, `phone`, `email` (`src/api/schemas.py:1249-1289`), at `page_size` up to 500.

**Attack path:**
1. Authenticated Starter user (limit 50, `records_used` 0) calls `POST /jobs`.
2. Polls `GET /jobs/{id}` until `status == "enriching"` — on a real county run this is a minutes-long window, so no race precision is needed.
3. `DELETE /jobs/{id}` → 204.
4. The worker keeps going: the cap reserves 50, marks rows 51..N `over_quota`, re-exports, then hits `tasks.py:2002` and returns **without billing**.
5. The sweep refunds 50 → `records_used` back to 0.
6. The user pages `GET /jobs/{id}/results` and harvests the 50 in-quota leads.
7. Repeat indefinitely. **Charged: 0.**

**This endpoint is the ONLY reader in the codebase without the gate.** Every sibling has it:
- `src/api/routes/segments.py:207, 286, 373, 441` — all join `jobs j … AND j.status = 'done'`.
- `src/workers/batch_export.py:89-90` — same.
- `GET /jobs/{id}/download` requires `job.export_key`, written only by the done-CAS (`jobs.py:1203-1204`, `tasks.py:2211-2216`).
- The skip-trace dispatcher drops `over_quota` and unbilled-terminal rows (`src/workers/skip_trace_dispatcher.py:612-653`).

The same root also fires on every `_fail_job` path — each releases the reservation (`status.py:494`) while the rows remain readable, e.g. the enriched re-export failure at `tasks.py:1896-1939`. That variant is not attacker-triggered but leaks the same way.

**Fix — prefer both:**
- Gate `GET /jobs/{job_id}/results` on **delivery state**, not just ownership: serve rows only when `job.billing_applied_at IS NOT NULL` (or `job.status == 'done'`), matching the rule `segments.py` and `batch_export.py` already enforce. A cancelled/failed job should return its counters and an empty page, never the leads.
- Make "released" mean "undelivered": when `release_quota_reservation` or the sweep refunds a grant, mark that job's rows `over_quota` (or a new `released` reason) **in the same transaction**, so `actionable_condition()` hides them everywhere at once.

### C6. Release guards are individually correct; the missing guard is "not billed ≠ not delivered"
**PARTIAL-WEAK CONTROL — P1 (same root as C5)**

`src/workers/tasks_helpers/status.py:257-308`:
```python
"UPDATE jobs SET reserved_at = NULL "
"WHERE id = :jid AND reserved_at IS NOT NULL AND billing_applied_at IS NULL "
"  AND reserved_count > 0 "
"  AND EXISTS (SELECT 1 FROM users u WHERE u.id = jobs.user_id AND " + _RESERVATION_STILL_HELD + ") "
"RETURNING user_id, reserved_count"
...
"UPDATE users SET records_used = GREATEST(0, records_used - :n) WHERE id = CAST(:uid AS uuid)"
```

Verified present and correct: exclusive CAS on `reserved_at` so a double refund is impossible (the amount comes from this statement's own `RETURNING`, not a prior unlocked SELECT); `billing_applied_at IS NULL` so a job that billed can never be refunded; window-equality via `reservation_is_current_sql(..., rolling="false")` (`status.py:37-43`) so a refund cannot eat a *new* window's usage; `GREATEST(0, …)` floor; a rolled grant retired without refund (`:278-295`).

**What is missing:** the guard proves the job never **billed**. It does not prove the job never **delivered** — and per C5 the rows survive cancellation and are readable. Note `sweep_stranded_quota_reservations` even sweeps `status = 'done'` rows (`:352-355`), harmless today only because the done-CAS and billing commit together so a `done` job can never have `billing_applied_at IS NULL`.

### C7. The "1,001/50" incident — over-restriction, not a bypass
**CONFIRMED SECURE CONTROL (enforcement side) — INFO**

`alembic/versions/088_quota_entitlement_periods.py:212-218`:
```python
BACKFILL_WINDOWS = """
    UPDATE users
    SET quota_anchor_at    = records_period_start,
        quota_period_start = records_period_start,
        quota_period_end   = ((records_period_start AT TIME ZONE 'UTC') + interval '1 month') ...
"""
```

The backfill put **every** user — mid-trial accounts included — on a calendar window running to the 1st, rather than closing the window at `trial_ends_at`. When the trial lapsed and the plan dropped to Starter, the Pro-trial usage remained inside the live window, producing `1,001 / 50`. Confirmed by remediation commit `ea1ead9` ("fix(quota): close the trial windows migration 088 stretched past the trial end (#320)"), which shipped `scripts/repair_trial_window_backfill.py` and a change to `src/workers/scheduler_helpers/billing.py`. The migration itself was correctly **not** rewritten — it has already run in prod.

`BACKFILL_FIRST_PAID` (`088:229-256`) *does* honour `trial_ends_at` on its legacy-payer arm (`AND trial_ends_at IS NULL`), so that half was always sound.

**The direction of the defect is over-restriction:** affected users were **refused** every scrape. There is no bypass, because display and enforcement read the **same** window:
- `/billing/usage` uses `effective_records_used` / `effective_window` (`billing.py:675-701`);
- the gates use `quota_block_reason` (`src/api/quota.py:130-170`);
- the worker's charging statements use the `public.quota_*` SQL twins of the identical rule (`088:81-188` ↔ `src/api/quota_window.py`), and `tests/test_quota_window.py` proves the two agree over a generated date matrix.

**Over-quota consumption is blocked server-side today** — by the reservation (C4), which is independent of any display path.

**Residual:** the repair is a one-off script, so any future backfill touching windows should re-run it, or assert that no window spans a `trial_ends_at`.

### C8. P3 — Enqueue gate is binary, not remaining-aware; no quota check at worker start
**PARTIAL-WEAK CONTROL — P3**

Four gates, all the same shape:
- `src/api/routes/jobs.py:222-229` (manual "Run now")
- `src/api/routes/batches.py:273-280` (batch create)
- `src/workers/scheduler_helpers/dispatch.py:124-134`, `:357-363` (scheduled)
- `src/workers/batch_tasks.py:133-144` (batch fan-out)

all: `_blocked = quota_block_reason(user); if _blocked: raise 402`. And `quota_block_reason` → `is_over_record_limit` → `effective_records_used(user) >= limit` (`src/api/quota.py:116-127`).

There is **no quota check at worker start** — `tasks.py` bootstraps at `:437-475` with no quota read. The only execution-time enforcement is the reservation at `:1583-1687`.

A user at 0/50 can therefore enqueue N jobs and every one passes the gate. They do **not** over-consume: each reservation is serialized (C4), so the first gets 50 and the rest get `granted = 0`, mark every row `over_quota`, export nothing and bill nothing. The cost is **wasted scraper capacity**, not records — and the failure is quiet (those jobs complete `done` with `record_count = 0`).

**Fix (optional):** refuse when `effective_records_limit - effective_records_used <= 0`, and short-circuit at the worker bootstrap so a zero-grant job never scrapes. A per-user in-flight job cap bounds the capacity abuse.

### C9. P3 — Stale `users` snapshot can skip the cap block entirely
**CONFIRMED VULNERABILITY (narrow, mostly accidental) — P3**

`tasks.py:468` loads the ORM user **once**:
```python
user = db.execute(select(User).where(User.id == job.user_id)).scalar_one()
```
and `src/db/session.py:112-114` sets `expire_on_commit=False` on `SyncSessionLocal`, so the intervening `db.commit()` calls do **not** re-read it. The object consulted at `tasks.py:1583` is a snapshot up to ~65 minutes old:

```python
from src.api.quota import effective_records_limit as _eff_limit
if _eff_limit(user) != -1:          # tasks.py:1581-1583 — stale snapshot
```

**Path:** an Agency subscriber (`records_limit == -1`) starts a long job; mid-run their entitlement lapses and `_reconcile_quota_periods_impl` writes `records_limit = 50` (`src/workers/scheduler_helpers/billing.py:240-260`) or the Stripe `deleted` handler does (`src/api/billing_entitlement.py:331`). The stale snapshot still reads `-1`, the cap block is **skipped**, the job exports uncapped, and settlement charges the full delivered count against a 50 limit. The same staleness lets an old `quota_period_end` / `subscription_status` drive `should_roll` inside `effective_records_limit`.

The reserved **amount** is never wrong — that SQL reads fresh values under lock. Only the **decision to run the cap at all** uses stale data, and only the `-1 → finite` direction is harmful.

**Fix:** re-read the limit immediately before the cap decision (`db.refresh(user)` or a targeted scalar SELECT of `records_limit, pending_records_limit, quota_period_end, subscription_status, entitlement_ends_at, entitlement_grace_ends_at`), or fold the unlimited test into the reservation statement so there is one read under one lock.

### C10. The `-1` unlimited sentinel is handled consistently
**CONFIRMED SECURE CONTROL — INFO (one P3 display nit)**

- `src/api/quota.py:124-127` — `if limit == -1: return False`; never over.
- `tasks.py:1583` — `if _eff_limit(user) != -1:` so the `eff_limit - base` arithmetic (which for `-1` would yield `GREATEST(0, -1 - base) = 0`, i.e. "always zero grant") is never reached with `-1`.
- `src/api/billing_entitlement.py:57-64` — `_rank` maps `-1 → float("inf")`, so a change **to** unlimited always ranks as an upgrade and applies immediately (`:288-298`). `pending_records_limit` can therefore never hold `-1`, which is what would make `COALESCE(pending_records_limit, records_limit)` in `quota_window.py:298-299` accidentally mean "unlimited at the boundary".
- `tasks.py:2240`, `billing.py:685-686` — both guard `!= -1`.
- `src/api/quota.py:91-113` — deliberately returns the **pending** limit across a boundary so an Agency→Pro downgrade does not export uncapped.

**P3 nit:** `/billing/usage` reports the **raw** `records_limit` (`billing.py:677`) while reporting `effective_records_used`. A user with a pending downgrade whose window has expired but not yet rolled sees the old, higher cap while enforcement will apply the lower pending one. Display-only. **Fix:** `limit = effective_records_limit(current_user)` at `billing.py:677`.

---

## D. TRACERFY COST ABUSE (real money per lookup)

### D1. The complete chain between a user and a paid provider call
**Mostly CONFIRMED SECURE — one P3 gap**

Money is spent in exactly **one** place: the Celery Beat task `dispatch_pending_skip_trace` → `submit_batch()`. **No API route calls Tracerfy.** Users can only *fill the queue* that task drains.

| # | User-reachable trigger | Authorization | Plan/entitlement | Rate limit |
|---|---|---|---|---|
| 1 | `POST /jobs` — `jobs.py:278` | `ScraperConfig.user_id == current_user.id AND active` (`:289-296`) | `quota_block_reason` → 402 (`:224-229`) | `zone="jobs"` = **5/min/user** (`:285`, `rate_limit.py:26`) |
| 2 | `POST /batches` — `batches.py:173` (fans out N configs → N jobs) | `user_id` stamped from `current_user` (`:361`) | Pro+ (`:184-188`); combo cap `BATCH_MAX_COMBINATIONS` (`:190-197`); skip-trace tier gate (`:228-232`); `quota_block_reason` (`:275-280`) | `zone="general"` = **60/min/user** (`:180`) |
| 3 | `POST /scrapers` (`scrapers.py:351`), `PATCH /scrapers/{id}` (`:529`) — sets `skip_trace_enabled` | ownership via `get_rls_db` + explicit `user_id` filter | `_enforce_plan_feature_gates` (`:225-230`); PATCH gates the enable-delta (`:762-770`) | **NONE — P3, see D5** |
| 4 | Beat `dispatch_scheduled_jobs` (user-owned schedule) | system session | `quota_block_reason` (`dispatch.py:124-134`) + execution-time entitlement re-check | beat interval |
| 5 | Beat `dispatch_pending_skip_trace` — **the actual spend** (`skip_trace_dispatcher.py:32-343`) | system session, cross-tenant by design (`:60-64`) | **none at this layer — see D4** | `SKIP_TRACE_MAX_BATCHES_PER_TICK=2` per 5 min (`settings.py:349`) — a *burst* limit, not a *cost* limit; each batch is up to 5000 rows (`:131`) |

The real gate is at enqueue — `src/workers/tasks_helpers/enrich.py:1955-1976`:
```python
if not settings.SKIP_TRACE_ENABLED: return
if not settings.TRACERFY_API_TOKEN: ... return
if not getattr(config, "skip_trace_enabled", False): return
if normalize_plan(job.user.plan) == "starter": ... return
```
Called from **exactly one** site: `tasks.py:1831`, inside `run_scrape_job`, **after** the plan cap. Every other worker that touches leads explicitly documents that it never enqueues (`owner_recovery.py:20`, `cv_owner_recovery.py:21`, `property_recovery.py:20`, `pierce_cv_owner_recovery.py:15`, `mailing_recovery.py:23`).

### D2. Authorization, dedup, concurrency, retries — all closed
**CONFIRMED SECURE CONTROL — INFO**

**Cross-tenant injection is not possible.** No API surface accepts lead or result ids for skip tracing; the only user input is `scraper_config_id`. Rows are selected server-side, tenant-pinned — `enrich.py:1986-1998`:
```python
eligible = db.execute(sa_select(Result).where(
    Result.job_id == job_id,
    Result.user_id == job.user_id,          # tenant pin
    Result.property_address.isnot(None),
    actionable_condition(),
    Result.skip_trace_status == "not_attempted",
    Result.is_duplicate.is_(False),
)).scalars().all()
```
`user_id` on the pending row is copied from `result.user_id` (`skip_trace.py:1038`), never from a request. Billing re-derives attribution from `pending_skip_trace_rows.user_id` (`skip_trace_usage.py:636-646`). The dispatcher's FIFO join is tenant-paired on both sides (`skip_trace_dispatcher.py:100-113`). The result cache is per-tenant by construction — `user_id` is inside the hash (`skip_trace.py:116-139`).

**Dedup — four layers against buying the same lead twice:**
1. Per-tenant 90-day `SkipTraceCache`, read before enqueue, with a **dual read** against the pre-2026-09-03 legacy locality key so a key-format change cannot re-buy an address already paid for (`enrich.py:2064-2092`).
2. `Result.skip_trace_status == "not_attempted"` in the eligibility query (`enrich.py:1995`).
3. `Result.is_duplicate.is_(False)` (`:1996`) plus the pre-submit withdrawal sweep (`skip_trace_dispatcher.py:396-467`, `:573-658`).
4. Placeholder-address exclusion (`enrich.py:2008-2017`), foreign-address exclusion (`skip_trace.py:926-928`), non-personal party names (`:950-951`).

**Webhook double-billing** is anchored on a row lock, not an edge dedup — `tracerfy_ingest.py:552-574`:
```python
queue_row = db.execute(select(SkipTraceQueue)
    .where(SkipTraceQueue.tracerfy_queue_id == queue_id).with_for_update()).scalars().first()
if queue_row is None: return {"skipped": "unknown_queue"}
if queue_row.status in ("completed", "billed", "errored"):
    return {"skipped": f"already_{queue_row.status}"}
```
The lock is held through ingest + counter advance + status flip to a **single** `db.commit()` (`:848`), so a replay blocks, sees `completed`, and no-ops **before** reaching billing. `report_lookups_for_user` deliberately does not commit (`skip_trace_usage.py:47-64`, `:163-180`), which is what couples the counter advance to the once-only status flip. Three further Stripe-side layers: `UniqueConstraint("tracerfy_queue_id","user_id")` on the outbox (`models.py:1323-1329`) + `on_conflict_do_nothing` (`skip_trace_usage.py:753-755`); a stable MeterEvent identifier `f"skip_trace_q{queue_id}_u{user_id}"` (`:503`) so Stripe dedupes server-side; and `SELECT … FOR UPDATE` on the outbox row plus `pg_try_advisory_xact_lock(4243, hashtext(user_id))` before reporting (`tracerfy_ingest.py:118-136`), with a claim on `disposition == 'pending'` (`:154-155`) so a retry cannot overturn a human's write-off.

**Counter atomicity.** `skip_trace_usage.py:91-102` takes the row lock first:
```sql
SELECT plan, stripe_customer_id, skip_trace_used_this_month, ...
FROM users WHERE id = :uid FOR UPDATE
```
and the read-modify-write at `:173-180` executes under that lock, held until the ingest worker's single commit. A second ingest for the same user blocks. Lock ordering is pinned by `ORDER BY user_id` in the per-user rollup (`:643`) specifically to make cross-batch deadlock impossible (documented `:593-598`).

**Dispatcher concurrency.** The FIFO head is taken `FOR UPDATE SKIP LOCKED` of the queue table only (`skip_trace_dispatcher.py:136`), then a durable `status='submitting'` claim with a CAS on `status == "queued"` is **committed before the POST** (`:198-206`). A concurrent tick's `status == "queued"` predicate excludes claimed rows.

**Retries never double-pay.** The two retrying tasks (`ingest_tracerfy_batch`, `max_retries=3`, `tracerfy_ingest.py:440-450`; `report_skip_trace_meter_event`, `max_retries=5`, `:35-43`) **never call `submit_batch`**. The only task that submits — the Beat dispatcher — has **no retry decorator at all** (`skip_trace_dispatcher.py:32`). The guard is the durable claim plus a failure classifier that decides release-vs-keep per outcome (`:477-499`):
```python
if m.startswith("network error") or "non-json" in m or "missing queue_id" in m:
    return "unknown_outcome"   # KEEP the claim — Tracerfy may have charged
```
`unknown_outcome` and the bare `except Exception` around the POST (`:279-282`) both return with the claim intact and are **never auto-resubmitted** (`:216-223`). Releases are pinned to one specific claim via `submitted_at` (`:835-867`); the reconciler refuses to release while an unaccounted queue sits inside a 30-minute quiet window (`:907-927`), refuses to adopt a queue matching more than one claim (`:1173-1191`), and defers rather than releases when a matching queue is still pending (`:1022-1031`). `_persist_submission` is idempotent with a fresh-session retry so an accepted `queue_id` is never lost (`:302-325`, `:697-780`). **This is the strongest-engineered area of the subsystem.**

**P3 gap (latent, not exploitable today):** two `Result` writes match on `id` alone, breaking the project's mandatory `user_id`-pairing rule that every sibling honours — `skip_trace_dispatcher.py:772-779` (`_persist_submission`) and `:874-881` (`_release_claim`, errored branch). Compare `_fail_unsubmittable` (`:561-570`) and `_cancel_undeliverable` (`:682-691`), which both use `tuple_(Result.id, Result.user_id).in_(...)`, and `tracerfy_ingest.py:774-783`, whose comment says the pairing is "MANDATORY here, not decorative." Not exploitable now because `result_id` is a UUID read from a claimed row, but a regression risk if `claimed` is ever populated from anything less trustworthy. **Fix:** `tuple_(Result.id, Result.user_id).in_([(c.result_id, c.user_id) for c in claimed])`.

### D3. Unmatched lookups ARE charged — intentional, documented policy
**CONFIRMED (policy, not a defect) — INFO**

`src/api/billing/skip_trace_usage.py:545-560` states it plainly:
- `completed` — a hit **and** a miss both bill: the provider searched, and "no contact exists" is a real answer.
- `unmatched` — Tracerfy accepted the row and charged a credit, but our address reconciliation could not map the answer back to the lead; it counts against the customer's quota.
- `errored` — rejected by the dispatcher's pre-submit validation, so Tracerfy never saw the row. Billing these would charge customers for lookups that were never sent.

Commit: `0e0af40 feat(billing): charge the customer for provider-completed unmatched lookups (#238)`. The `unmatched` vs `errored` split exists purely to make that distinction billable (`tracerfy_ingest.py:737-759`). **Your suspicion was correct.**

There is a conservative safety valve — `unmatched` bills only when Tracerfy demonstrably accepted every row sent (`skip_trace_usage.py:614-634`), erring toward the customer. **But that valve is exactly what A7b subverts**, since the integer it reads comes from the webhook body.

### D4. P1 — No per-account spend cap, no per-job cap, no circuit breaker; unbounded on Agency
**CONFIRMED VULNERABILITY — P1 (NO-GO)**

`SKIP_TRACE_BUNDLED_QUOTAS` is referenced in exactly **four** places — `skip_trace_usage.py:110` (billing math), `billing.py:318` and `:550` (UI display), `settings.py:180` (the definition). **It is never consulted before spending.**

`dispatch_pending_skip_trace` drains the FIFO head with no reference to any user's counter, plan quota, or spend — `skip_trace_dispatcher.py:89-140`. The only ceilings anywhere:
- `settings.py:349` — `SKIP_TRACE_MAX_BATCHES_PER_TICK = 2`. A **burst** limit, not a cost limit; each batch carries up to 5000 rows (`skip_trace_dispatcher.py:131`).
- `skip_trace_dispatcher.py:231-268` — the Tracerfy **402 out-of-credits** handler. This is the *de facto* global circuit breaker: the **prepaid Tracerfy balance**. It is a provider-side accident, not a control, and its handler deliberately submits the *affordable prefix* (`:244-250`) to keep spending right up to the balance.

Spend is otherwise bounded only **indirectly**, by delivered leads: the enqueue runs after the plan cap (`tasks.py:1811-1831`) and over-quota rows are excluded by `actionable_condition()`. **That indirect bound evaporates on Agency:**

```python
# src/config/plans.py:78-83
{"id": "agency", ..., "records_limit": -1,       # unlimited
# src/config/settings.py:184
"agency": 2000,    # 2000 free/month, then $0.05/trace
```

**Attack path:** an Agency subscriber — or anyone holding a stolen Agency JWT or API key, which `auth.py:305` accepts — creates batches of up to 100 county × record-type combinations (`constants.py:228-230`) at 60 batches/min (`batches.py:180`). `records_limit = -1` means `is_over_record_limit` never fires, so **every** scraped lead is delivered, and every delivered lead with an address is enqueued for a paid lookup. Past 2,000 lookups in the window each is billed to the customer at $0.05 — but **BridgeLeads pays Tracerfy first**, from a prepaid balance, with no ceiling and no alert until the 402. A compromised account, or an Agency customer who disputes or charges back, converts directly into unrecoverable provider spend.

Pro and Business are bounded by their record caps (1,000 and 5,000 records → ≤$60 / ≤$320 per window). **This is specifically the unlimited tier.**

**Starter/free:** spend is 0 — blocked at `enrich.py:1969` and `SKIP_TRACE_BUNDLED_QUOTAS["starter"] = 0`. **CONFIRMED SECURE.**

**Fix:**
1. Hard per-window lookup ceiling per account (e.g. `SKIP_TRACE_HARD_CAP = {pro: 2000, business: 5000, agency: 20000}`), enforced in the dispatcher's FIFO query as a join against `users.skip_trace_used_this_month` **plus in-flight `submitting`/`submitted` rows** — the counter only advances at *ingest*, so a naive check lags by a full batch. Leave over-cap rows `queued` and fire an ops alert.
2. Global daily credit-spend circuit breaker (sum `credits_deducted` over 24h) that halts the dispatcher and pages ops, instead of relying on the Tracerfy 402.
3. Give Agency a skip-trace lookup ceiling even though records are unlimited — **"unlimited records" was never sold as "unlimited paid lookups."**

### D5. P3 — The routes that toggle paid skip-trace have no rate limit
**PARTIAL-WEAK CONTROL — P3**

`POST /scrapers` (`scrapers.py:351`) and `PATCH /scrapers/{id}` (`:529`) carry **no `rate_limit()` call**, and there is no global limiter middleware (`main.py:63`). These are precisely the routes that flip `skip_trace_enabled`.

**Fix:** add `await rate_limit(request, zone="general", identifier=current_user.id)` to both. Separately, consider moving batch creation to the `jobs` zone — one `POST /batches` is worth up to 100 scrapes but currently costs the same budget as a `GET`.

### D6. P2 — Spend during dunning is paid but unbillable
**PARTIAL-WEAK CONTROL — P2**

A `past_due` subscriber inside the grace window is **not** frozen (`src/api/quota_window.py:190-193`), so jobs run, leads deliver, and Tracerfy credits are spent. But `assert_billable` accepts only `status == "active"` (`skip_trace_usage.py:382-384`) — a `past_due` subscription is skipped, the loop falls through to `raise _NotBillableError("coverage_unproven")` (`:427`), and the row is settled `needs_review` (`tracerfy_ingest.py:237-241`).

**Net: BridgeLeads pays the provider, the customer's card is already failing, and the overage is parked for a human who may never adjudicate it.**

**Fix:** either freeze skip-trace enqueue (not the whole account) once `subscription_status == 'past_due'`, or make grace-period overage explicitly billable on cure. This is a policy decision rather than a bug — but it is currently unstated and silently costs money.

---

## E. Rate limiting notes relevant to this scope

`src/api/middleware/rate_limit.py:24-40` zone config:
- `"auth"`: 10/min per IP
- `"jobs"`: 5/min per user
- `"webhook"`: 120/min
- `"stripe"`: 10/min **per user** (`identifier=current_user.id` at `billing.py:709`, `:1015`, `:1404`, `:1661`) — tighter than `general` because each call spends the operator's Stripe quota and can spam Customer/Checkout objects. Team lead has confirmed this zone is user-keyed and therefore unaffected by the IP-limiter defect found elsewhere.
- `_FALLBACK_ZONES = {"auth", "webhook", "stripe"}` (`:117`) — these three **fail closed** during a Redis outage; every other zone fails open (availability over abuse-resistance for non-security paths).

The admin funnel endpoint additionally rate-limits **before** `require_admin` (`billing.py:102-120`) so denied probes are throttled without paying an auth decode — a prior Codex P2.

---

## F. Confirmed-secure controls (do not re-litigate)

- Raw-body HMAC with a required `stripe-signature` header and no tolerance override (300s replay window).
- Transactional `stripe_webhook_events` ledger **read before dispatch** under advisory lock 4244, with 409-on-timeout so Stripe retries.
- Re-read-from-Stripe defence against out-of-order delivery, with the user row locked **before** the re-read.
- Conversion quota reset gated on durable `first_paid_at` / `paid_entitlement_ended_at`, not on the event.
- Plan and status changes never move the entitlement anchor or window — upgrade-farming and cancel/resubscribe-farming are worthless by construction.
- Server-side price allowlist; no amount, plan name or quantity crosses the wire; `metadata.user_id` from the authenticated principal; session-customer cross-check with refusal on mismatch.
- Duplicate-subscription guard that asks Stripe three times, expires open sessions, and fails **closed** on a Stripe outage.
- No client-supplied coupon anywhere; annual-coupon abuse prevented in Stripe with detect-and-alert here.
- No entitlement from the return URL; return URLs are server constants, so no open-redirect surface.
- Atomic quota reservation and settlement under `SELECT … FOR UPDATE` with a single-statement grant.
- The record cap is applied to the exported file itself, not merely accounted afterwards.
- Trial anti-farming via permanently-stamped `trial_consumed_at`.
- API access gated at the authentication layer, so API keys inherit every downstream plan check; API keys can never satisfy an admin MFA step-up.
- Starter fully blocked from paid skip-trace at three independent layers.
- Skip-trace claim/retry machinery: durable pre-POST claim, no retry on the submitting task, ambiguous outcomes keep the claim.
- `-1` unlimited sentinel handled consistently; `pending_records_limit` can never hold `-1`.
- No client-supplied value reaches quota arithmetic without a server-side clamp.

---

## G. Method and caveats

- Files read in full: `src/api/routes/billing.py` (all 2467 lines, in chunks), `src/api/routes/webhooks.py`, `src/api/quota.py`, `src/api/quota_window.py`, `src/api/billing_entitlement.py`, `src/api/entitlements.py`, `src/config/plans.py`, `src/config/stripe_client.py`, `tests/test_api_key_plan_guard.py`, plus targeted reads across `src/api/auth.py`, `src/api/routes/scrapers.py`, `src/config/constants.py`, `src/config/settings.py`, `src/api/middleware/rate_limit.py`.
- Two parallel sub-investigations covered the skip-trace cost chain and the quota reservation/settlement/release machinery in depth; their file:line evidence is incorporated above.
- **`pytest` was not run** — bare pytest reads the production `.env` and has wiped production twice.
- **No source file was modified.** This report is the only file created, and it is untracked.
- **Unverifiable from a read-only worktree:** the deployed values of `STRIPE_PRODUCT_*` and `TRACERFY_WEBHOOK_SECRET` on the Railway `api` and `worker` services. Finding 10 is conditional on those. (`ENTITLEMENT_ENFORCEMENT` **is** settled by in-repo evidence — see §H4, which supersedes finding 8 / §C3.)

---

## H. Codex cross-check — confirm or reject

Four independent Codex findings in my scope, adjudicated against the code.

### H1. `customer.subscription.updated` falls back to the event body on a Stripe read failure
**Codex: real. My verdict: CONFIRMED — the fallback exists exactly as described. Severity P3, not P2.**

The code is `src/api/routes/billing.py:2100-2122`:

```python
subscription_id = data.get("id")
if subscription_id:
    try:
        data = dict(
            stripe.Subscription.retrieve(
                subscription_id, expand=["items.data.price", "discounts"]
            )
        )
    except Exception as exc:  # noqa: BLE001 — never 500 a webhook
        if require_entitled:
            _logger.error(
                "customer.subscription.created: could not re-read %s from "
                "Stripe (%s); retrying later", subscription_id, str(exc)[:200],
            )
            raise
        _logger.warning(
            "customer.subscription.updated: could not re-read %s from Stripe "
            "(%s) — applying the event payload, which may be out of order",
            subscription_id, str(exc)[:200],
        )
```

Codex read it correctly. During a Stripe API outage, a delayed or out-of-order `updated` event **is** applied from the event body, and the out-of-order protection described in §A4 is exactly what is lost. Preconditions: Stripe unreachable **and** a stale delivery arriving in that window. Stripe retries for three days and does not guarantee ordering, so the two coinciding is plausible, not theoretical.

**Why P3 rather than P2 — what the blast radius actually is:**

- **A grant of a plan never paid for is impossible.** The `require_entitled` branch (`customer.subscription.created`) **raises** rather than falling back (`:2109-2117`), so the one path that can mint entitlement from nothing is already closed. The comment at `:2110-2112` states the reasoning: "The event body can be stale (created active, cancelled since), so failing to ask is a retry, not a grant."
- **A stale SMALLER plan does not cut the customer off.** `apply_plan_change` parks downgrades in `pending_plan` / `pending_records_limit` (`src/api/billing_entitlement.py:296-303`); it does not apply them until the next entitlement boundary. A later correct event overwrites the parked value first.
- **A stale LARGER plan applies immediately** (`billing_entitlement.py:288-295`) — that is over-entitlement, i.e. revenue loss, not a customer-facing breach.
- **Self-healing.** Any subsequent webhook for that subscription re-reads successfully and rewrites current truth, and the hourly `_reconcile_quota_periods_impl` treats Stripe as the source of truth and repairs drift (`src/workers/scheduler_helpers/billing.py:383-388`) — and critically **skips rather than downgrades on a Stripe error**, so the reconciliation cannot compound the same outage.

**The one branch genuinely worth tightening** — and neither Codex nor my first pass called it out specifically: a fallback-applied stale `status == "active"` runs `_clear_dunning(user)` (`billing_entitlement.py:267-270` → `:100-102`), which **un-freezes a delinquent account** by clearing `entitlement_grace_ends_at`. That is the only fallback outcome that hands service to someone who is not paying, and it persists until the next real event or the hourly reconciliation.

**Fix:** when the retrieve failed, apply only the non-lifecycle fields, or skip `_clear_dunning` and the immediate-upgrade branch and let Stripe be re-asked. Cheapest correct version: record that `data` is unverified and pass a `verified: bool` into `apply_plan_change`, refusing to clear dunning or apply an upgrade when it is False.

### H2. Worker reservation is atomic; the API preflight is advisory
**Codex: correct. My verdict: CONFIRM — both halves.**

Codex's line range (`src/workers/tasks.py:1620-1710`) brackets the same code I analysed at `:1605-1687`; we are describing one mechanism.

- **Conditional claim:** `UPDATE jobs SET reserved_at = … WHERE id = :jid AND reserved_at IS NULL` (`:1622`) — a CAS that makes a watchdog re-run idempotent.
- **User-row lock:** `SELECT u.id, u.records_used, u.records_limit, … FROM users u WHERE u.id = CAST(:uid AS uuid) FOR UPDATE` inside the `cur` CTE.
- **Atomic update in the same statement:** `LEAST(:want, GREATEST(0, eff_limit - base))` computes the grant and `UPDATE users u SET records_used = g.base + g.granted` applies it, with `RETURNING g.granted, g.new_start`.

Under READ COMMITTED the loser blocks and EvalPlanQual re-reads the post-commit row, so `base` is already decremented. **There is no read-then-write window to name.** `:want` is a server-side `count(*)` — no client value reaches it.

**The API preflight is indeed advisory.** `quota_block_reason` at `src/api/routes/jobs.py:222-229`, `src/api/routes/batches.py:273-280`, `src/workers/scheduler_helpers/dispatch.py:124-134` and `src/workers/batch_tasks.py:133-144` is a binary "already at cap" test (`used >= limit`, `src/api/quota.py:116-127`), not a remaining-aware reservation. Enqueuing N jobs while under the cap passes N times — but jobs 2..N receive `granted = 0`, mark every row `over_quota` and bill nothing. The cost is wasted scraper capacity, not records. Detail in §C8.

Codex's framing is right and matches my §C4 independently. **No disagreement.**

### H3. Only worker dispatch reaches Tracerfy; queue rows idempotent and tenant-scoped; no spend ceiling
**Codex: correct on all three. My verdict: CONFIRM, with one addition Codex did not cover.**

- **Only worker dispatch reaches Tracerfy** — CONFIRMED. No API route calls the provider. Money is spent in exactly one place: the Beat task `dispatch_pending_skip_trace` → `submit_batch` (`src/workers/skip_trace_dispatcher.py:32-343`). The enqueue gate at `src/workers/tasks_helpers/enrich.py:1955-1976` is called from exactly one site, `src/workers/tasks.py:1831`, after the plan cap.
- **Queue rows idempotent and tenant-scoped** — CONFIRMED, and Codex's cited range (`tasks_helpers/enrich.py:1938-2085`) is the right one. Tenant pin at `:1986-1998` (`Result.user_id == job.user_id`); idempotency from `skip_trace_status == "not_attempted"` (`:1995`), `is_duplicate.is_(False)` (`:1996`), and the per-tenant 90-day cache with a dual read against the legacy key format (`:2064-2092`). Billing re-derives attribution from `pending_skip_trace_rows.user_id` (`src/api/billing/skip_trace_usage.py:636-646`), never from a request. Full detail in §D2.
- **No per-account spend ceiling or circuit breaker** — CONFIRMED, and this is my P1 (§D4). `SKIP_TRACE_BUNDLED_QUOTAS` is never consulted before spending; the dispatcher drains the FIFO head with no reference to any counter (`skip_trace_dispatcher.py:89-140`). The only ceilings are `SKIP_TRACE_MAX_BATCHES_PER_TICK = 2` (a burst limit; each batch up to 5000 rows) and the Tracerfy **402 out-of-credits** handler (`:231-268`) — i.e. the prepaid provider balance, which is a provider-side accident rather than a control. Unbounded specifically on Agency, where `records_limit = -1` removes the indirect bound that caps Pro and Business.

**What Codex did not cover, and it is my second P1:** the inbound Tracerfy webhook surface. The legacy path-secret route is still mounted (`src/api/routes/webhooks.py:169-183`), its own docstring says current traffic sends no header so the secret is travelling in URLs and access logs, and the webhook **body's** `rows_uploaded` is persisted (`src/workers/tracerfy_ingest.py:802-812`) and then decides whether unmatched lookups bill (`skip_trace_usage.py:614-634`) in the same transaction. Full write-up in §A7. Codex's "queue rows are idempotent" is true of the *enqueue* side and of replayed *identical* webhooks; it is not a defence against a forged webhook carrying attacker-chosen field values.

### H4. `ENTITLEMENT_ENFORCEMENT` — code default vs production state
**Codex is right about the CODE DEFAULT and wrong about the PRODUCTION CONSEQUENCE. The team lead is right. This supersedes my own §C3 and finding 8.**

Both statements are true and they are not in conflict:

- **Codex's fact:** `src/config/settings.py:194` → `ENTITLEMENT_ENFORCEMENT: bool = False`. Correct.
- **Codex's inference** — "so Starter can create configs with disallowed counties/record types" — is **wrong in production**, because the deployed env overrides the default.

**The evidence in the repo that settles it**, in descending order of strength:

1. **`docs/ENTITLEMENT-AUDIT-2026-09-08.md:12-14`** — a prior audit's "Production facts established before any verdict" section, citing an actual command:
   > `ENTITLEMENT_ENFORCEMENT=true` on the `api` and `worker` Railway services (`railway variables -s api|worker`). The code default of `False` is not the production value, so county and record-type gates are live and return HTTP 402 today.

   This is a recorded read of the deployed env on **both** services, which is the thing that matters — a split between api and worker would be worse than either state.

2. **`docs/HANDOFF-entitlement-audit-2026-09-08.md:102-103`** — the same fact in the handoff's "do not re-derive" list.

3. **`docs/BUILD_JOURNAL.md:1477-1478`** — stated as a key learning:
   > `ENTITLEMENT_ENFORCEMENT` is ON in prod (per the #235 entry below), so the plan notice fires for real customers today. **The code default of False is not the production value.**

4. **`docs/BUILD_JOURNAL.md:1586-1589`** — PR #235's entry, the decision that depended on it: `/billing/pricing` advertised county counts the API answers 402 for, "and `ENTITLEMENT_ENFORCEMENT` is ON in prod. Operator chose **Option A** (page drops to match enforcement) over raising the caps." A shipped product decision was made *because* the flag is on — that is behavioural evidence, not just a note.

5. **`src/api/routes/billing.py:569-571`**, the comment the team lead cited, which is consistent with all of the above:
   > "With ENTITLEMENT_ENFORCEMENT on in production that is not a cosmetic typo: the page promised counties the API answers 402 for."

**One apparent contradiction, resolved:** `docs/BUILD_JOURNAL.md:1516` says "`ENTITLEMENT_ENFORCEMENT` still defaults off" and `:4279`/`:4302` describe it as "default False = audit/log-only" with a pending flip. Those are all statements about the **code default** and about the state *before* the flip; `:4302`'s "Pending: flip ENTITLEMENT_ENFORCEMENT" is dated 2026-06-21, while the `railway variables` read is 2026-09-08. The flip happened in between. No contradiction.

**Corrected verdict for §C3 / finding 8:** county caps and the record-type matrix **are enforced in production today** and return 402. Codex's stated exposure does not exist. I am **downgrading finding 8 from P3 to INFO**, and restating it as documentation risk rather than an enforcement gap:

- The residual risk is that the *code* reads as unenforced, so any reviewer — human or model — who checks `settings.py:194` and stops there reaches Codex's conclusion. That has now happened at least twice.
- The genuine operational risk is a **split** between the `api` and `worker` services, or a future service (a new worker, a one-off container) deployed without the variable, which would silently revert to audit-only for whatever runs there. Worth a startup assertion.

**Fix (INFO, optional):** log the effective value at api and worker boot, and add a comment at `settings.py:194` pointing at `docs/ENTITLEMENT-AUDIT-2026-09-08.md:12` so the next reader does not have to rediscover that the default is not the deployed value. A `/readiness` field carrying the effective flag would make a service split visible without a `railway variables` round-trip.

### H5. Net effect on the report

| Codex item | Verdict | Severity |
|---|---|---|
| 1 — event-body fallback on Stripe read failure | **CONFIRMED** (new finding, added as #14) | P3 |
| 2 — reservation atomic, preflight advisory | **CONFIRMED** — agrees with §C4/§C8 | INFO |
| 3 — worker-only Tracerfy path, no spend ceiling | **CONFIRMED** — agrees with §D1/§D2/§D4; Codex missed the webhook surface in §A7 | P1 (the ceiling) |
| 4 — enforcement off, Starter unrestricted | **REJECTED in production** — code default correct, consequence wrong | INFO (was P3) |

Revised totals: **4 × P1, 1 × P2, 8 × P3** (finding 8 downgraded to INFO, new finding 14 added at P3).

**Finding 14 for the §0 table:** *`customer.subscription.updated` applies the event body when the Stripe re-read fails, losing out-of-order protection; a stale `active` additionally clears dunning and un-freezes a delinquent account* — P3 — `src/api/routes/billing.py:2100-2122`, `src/api/billing_entitlement.py:267-270`.
