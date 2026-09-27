# Security audit 2026-09-25: Phase 2 remediation plan

Source: `SECURITY-AUDIT.md` (Phase 1). Owner approved Phase 2 on 2026-09-25 ("proceed with the rest"),
except the manual items below, which stay with the owner.

Rules for every step: max 5 files; regression test that FAILS before the fix and passes after; tests run
only on the isolated rig (`bridgeleads_secaudit2_test`, Redis db 11); Codex consult before, Codex review
after; separate commit per step; NOTHING merged to main (merge = deploy) without the owner.

## Owner manual actions (remind the owner at the end)
- [ ] Rotate the Cloudflare API token (N-01) + review CF audit log back to 2026-03-17
- [ ] Rotate the admin password in `scripts/audit_out_ui/run3_thurston_whatcom.log`; move that log and `.rls-cutover-secrets` out of OneDrive
- [ ] Run `rls_catalog.py` read-only check (mig-101 FORCE RLS + policies live?)
- [ ] Cloudflare sole ingress (unblocks F-01); GitHub env protection for `DATABASE_URL_SYNC`, delete `RAILWAY_TOKEN_PRODUCTION`, narrow `BACKEND_SCHEMA_TOKEN`
- [ ] Decide: disable legacy Tracerfy path-secret route + rotate that secret; Tracerfy per-account/trial spend policy
- [ ] Carry-overs: DMARC, apex SPF, MX, backup restore test, provider billing caps

## Steps
- [x] S1 N-01 (code side): untrack `infra/terraform/terraform.tfvars`, exclude `infra/` from the Docker context, regression test that fails if a tracked file carries a credential-shaped assignment (BOM-tolerant, case-insensitive)
- [x] S2 N-02: change-plan proration by direction. Upgrade (higher records tier, or same tier) = `always_invoice` + `payment_behavior=error_if_incomplete` (paid before the subscription changes; card failure = nothing changes). Downgrade = `proration_behavior=none` (no Stripe credit; app already defers to the boundary). Card decline -> 402 with a clear message.
- [x] S3 N-03: `/jobs/{id}/results`, `/download`, `/export-url` deliver rows only for `status == "done"` (the only billed state), matching segments + batch CTEs (rule of 2026-09-08). Regression: a job in `enriching` with rows returns 0 rows / 409.
- [x] S4 E-1 + E-2 + E-3: cap webhook response body, redact URL from webhook error log, unwrap IPv4-embedding IPv6 in `_ip_is_blocked`
- [ ] ~~S5~~ NOT DONE (accepted by design, see review) F-02r: sanitize text fields in the generic dialer webhook + job webhook payloads
- [x] S6 A-2 + A-4: API-key creation and MFA setup require current password and a JWT session (not an API key)
- [x] S7 A-1 + A-5: frontend sign-out calls backend logout; logout revokes the refresh token; refresh reuse detection
- [x] S8 A-3: MFA verify failure lockout
- [ ] ~~S9~~ NOT DONE (belongs to the 1b-1b spend-cap work, see review) B-3: skip trace requires a paid subscription (`first_paid_at`), coordinated with the 1b-1b session (do not touch dispatcher files)
- [x] S10 P3 quick wins: C-1 (`include_all` admin-only), D-1 (`is_active` on download token path)

- [x] S11 F-03 (owner approved 2026-09-26, same branch; `3df7fdfd`): pin the webhook connection to the IP that passed the SSRF check. New `src/utils/pinned_http.py`: a urllib3 connection mixin overrides `_new_conn()` to resolve, reject if ANY address is blocked (`_ip_is_blocked`), then connect to a validated address only. TLS stays on the hostname (urllib3 wraps with `server_hostname=self.host` after `_new_conn`), so SNI, cert verification and Host are unchanged. Proxies refused. `webhook_delivery` uses the pinned session; a connect-time block returns `blocked` (no retry) like the pre-check. Regression: a real local server on loopback receives NOTHING through the webhook session (fails on main, where the session connects); positive path over real HTTP and TLS keeps Host/SNI/cert on the hostname. `dialer_outbox` (hardcoded vendor hosts) left as is.

Deferred (need infra/owner first): F-01/F-01b (after Cloudflare sole ingress), F-07 sandbox, S-2 owner DSN.

## Review

8 of 10 steps shipped to the branch (BE `chore/security-audit-2026-09-25`, FE `fix/reauth-sensitive-actions`),
each with a regression test proven failing on the pre-fix commit and a Codex review (4 NO-GO rounds total, all
resolved). Nothing merged: merge = deploy.

- S5 not done: the generic/job webhook sending raw values is a documented, Codex-reviewed product decision; an
  opt-in "spreadsheet-safe" delivery setting is the owner's call.
- S9 not done: "no overage lookups without an active paid subscription" belongs inside the 1b-1b per-account
  cap (branch `feat/lookup-1b1b-ledger`, another session); doing it here would collide.
- Deploy order: FE PR first (it sends `current_password` and calls logout; the current backend ignores both),
  then BE. The FE generated API types regenerate from BE `main` after the BE merge.
- Lessons: the pytest `db` fixture deletes every user, so a Playwright seed must happen after the last pytest
  run on that DB; Playwright's sync API only delivers events during Playwright calls, so `time.sleep` + a
  stale `page.url` looked like a broken sign-out that was not.
