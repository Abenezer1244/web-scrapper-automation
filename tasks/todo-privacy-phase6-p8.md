# Phase 6 P8: public intake, the request page, deadline alerts (design sections 3, 9, 11)

Owner: "fix and do all yourself" (2026-11-09) after P7 shipped. Branch `feat/privacy-phase6-p8`
(off origin/main a1c84712). Frontend in bridgeleads-web. P9 (DROP) stays deferred (D8).

## What exists
- P7 (live): request/rule tables (125, 136), operator CLI `scripts/privacy_admin.py`.
- `PRIVACY_ENFORCEMENT_VERSION=1` on api, worker, beat (set 2026-11-09, skip-deploys).
- Requests can only be recorded by an operator. No public way in; the policy page gives an
  email address whose domain has no MX yet (owner: Cloudflare Email Routing).
- Patterns to reuse: signed single-purpose tokens (`src/api/unsubscribe_tokens.py`),
  enumeration-safe confirm flow (`auth_helpers/email_change.py`), transactional email
  (`src/workers/account_emails.py` + `email_layout.build_payload`), `rate_limit(zone, identifier)`.

## Design (v2, after the Codex consult; v1 is in git history)
- **A submission IS the received request** (the statutory clock starts on receipt, not on
  confirmation). `POST /privacy/requests` creates the `privacy_requests` envelope at once
  (channel 'web', `received_at` = server time, deadline computed then), the encrypted
  payload, a `received` event and a `verification_sent` event, and one outbox email. No
  "pending intake" table.
- **Confirmation only records verification.** The link carries an opaque random token; only
  its hash is stored (`privacy_request_tokens`: request, purpose, expires_at, consumed_at),
  consumed atomically (`UPDATE ... WHERE consumed_at IS NULL`). Opt-out: `verified` at level
  `email_link` (D1). Delete/access/correct: `in_review` (the operator still matches name +
  property and records `verified`). Replay, expiry and garbage all render the same page.
- **Least privilege:** two SECURITY DEFINER functions (`privacy_web_submit`,
  `privacy_web_confirm`), owned by the privacy tables' owner like 129, search_path pinned,
  bounded inputs, returning nothing personal; EXECUTE to `bridgeleads_app` only; no table
  grants; re-granted in `provision_rls_roles.sql`; role tests for every runtime and API role.
- **Enumeration-safe:** one response body for every syntactically valid submission; the same
  DB path whatever the address; no lookup of existing records or requests; never returns an
  id. Confirm returns one generic outcome.
- **Abuse:** a fail-closed `privacy` rate-limit zone: per IP, per /24, per email blind index,
  per email domain, and a global daily send budget; bounded field sizes; one live request
  per (email, kind) per 24 h (a repeat is accepted and answered identically but sends
  nothing new). CAPTCHA (Turnstile) is a follow-up needing the owner's Cloudflare setup.
- **Durable outbox:** `privacy_email_outbox` written in the same transaction; the worker sends
  with retries and backoff; the token is minted at send time (its hash stored through a
  worker-role function), never kept in plaintext.
- **One email per submission:** "we received your <kind> request on <date>; confirm it was
  you". No submitted names, addresses or match facts; no claim of completion; "received and
  pending verification".
- **Authorized agents:** agent name + email + authority statement are separate fields; an
  agent submission gets no verification link and goes to `in_review` for the operator
  (signed permission, D1).
- **Data minimised per kind:** opt-out needs email + name + one property hint; parcel,
  county and details are optional, bounded matching hints.
- **Deadlines** [D, defaults, counsel to confirm]: delete/access/correct 45 calendar days;
  opt-out 15 business days (weekends and US federal holidays, rule-computed); stored with
  a rules version. Extensions use the existing `extended` event.
- **Beat** (daily): idempotent ops alerts (ids and kinds only) for requests due within
  7 and 2 days and undecided; expired tokens purged.
- **CLI:** `show --request` (decrypted contact + details, to the operator only) and `inbox`.
- **Frontend:** `/privacy/request` form + confirm page, footer "Your privacy choices",
  policy section 8 links the form; copy marked for counsel (D9); no em dashes.

## Legal items raised by Codex (for counsel; not engineering conclusions)
- DROP: the Delete Act rules require registered data brokers to retrieve DROP requests at
  least every 45 days from 2026-08-01. Whether BridgeLeads is a data broker is D8 (counsel).
- Deadline rules beyond California, receipt-confirmation duties, verification sufficiency
  and agent authority are counsel's calls; the code keeps them as configuration.

## Codex consult (2026-11-09), reconciled
All Highs and Mediums adopted (design v2). Not adopted now: CAPTCHA (needs owner Cloudflare
setup; rate limits + send budget + per-email dedupe instead), ASN-level limits (no ASN data;
/24 instead), provisional suppression of an unverified opt-out (a rule needs an exact anchor +
subject; the operator acts within the opt-out deadline, alerted by the beat).

## Slices (<= 5 files per commit, ruff, tests on TEST_DATABASE_URL, Codex GO)
- [ ] P8a migration 137: `privacy_request_tokens`, `privacy_email_outbox`,
      `privacy_web_submit()`, `privacy_web_confirm()`, worker functions for the outbox; grants;
      provision script; tests.
- [ ] P8b deadlines module (calendar + business days + federal holidays) + settings; tests.
- [ ] P8c API: `src/api/routes/privacy.py`, `privacy` rate-limit zone (fail closed); tests.
- [ ] P8d worker: outbox sender + email copy + daily deadline beat; tests.
- [ ] P8e CLI `show`, `inbox`; tests.
- [ ] P8f frontend: form, confirm page, footer, policy link, BFF allowlist; tests.

## Review
(at the end)
