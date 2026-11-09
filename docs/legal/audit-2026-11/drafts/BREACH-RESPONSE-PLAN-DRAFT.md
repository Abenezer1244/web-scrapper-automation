> **DRAFT, requires counsel review. Not legal advice. Not published.**

# BridgeLeads Security Incident and Breach Response Plan (draft, internal)

**Draft date:** 2026-11-09. **Facts checked against:** backend `main` at `7aff3c76`.

## Drafting notes

- The audit found alerting in place but no runbook linking an incident to notice duties
  (`01-EXECUTIVE-REPORT-AND-RISK-REGISTER.md` area 14). This draft is that runbook.
- Every notice deadline, threshold and recipient is a counsel question. The audit cites Washington's breach
  statute (RCW 19.255.010) with 30-day consumer and Attorney General windows; that citation is listed in
  section 7 as a question to confirm, not as settled advice.
- Detection and containment steps refer to real settings and tools in this repository. If a setting is
  renamed, this plan must change with it.

## 1. Scope

Any event that may have exposed, altered, lost or made unavailable personal information we hold, or the
secrets that protect it. This covers both populations: customers, and people named in lead records.

## 2. Roles

| Role | Who | Backup |
|---|---|---|
| Incident lead (decides, coordinates) | `[OPEN]` | `[OPEN]` |
| Technical lead (contain, investigate) | `[OPEN]` | `[OPEN]` |
| Counsel | `[OPEN: firm and contact]` | |
| Communications (customers, people affected) | `[OPEN]` | |
| Vendor contacts | See section 9 | |

The company is small; one person may hold several roles. Name them anyway, with phone numbers kept outside
company systems in case those systems are down. `[OPEN]`

## 3. How we find out

| Source | What it catches | Status in code |
|---|---|---|
| security@bridgeleads.io | Reports from researchers, customers, vendors | Address is in use `[OPEN: confirm inbound routing and who reads it]` |
| Ops alerts (`OPS_ALERT_EMAIL` via Resend) | Job, watchdog, canary, billing and privacy-deadline alerts | Live when the setting is set `[OPEN: confirm production]` |
| Incident webhook (`OPS_ALERT_WEBHOOK_URL`) | The same alerts, to Grafana Cloud IRM | Live when set `[OPEN: confirm]` |
| Heartbeat (`OPS_HEARTBEAT_URL`, every 5 minutes) | A dead worker, beat or broker | Live when set `[OPEN: confirm]` |
| Uptime monitoring (Better Stack, per ops notes) | Site or API down | `[OPEN: confirm]` |
| Error tracking (Sentry, `SENTRY_DSN`) | Unhandled exceptions (type and stack frames only) | Dormant until the DSN is set |
| Stripe dispute alerts | Chargebacks (possible stolen-card use) | In code |
| Audit trail (`audit_events`) | Sign-ins, lockouts, admin actions, with IP and path | Append-only, always on |
| Vendor notices | A vendor's own breach | By contract `[OPEN: no agreements on file]` |

There is no `/.well-known/security.txt` yet (audit P3). `[OPEN]`

## 4. Severity

| Level | Meaning | Examples |
|---|---|---|
| SEV-1 | Personal information likely acquired by someone unauthorized, or a production secret exposed | Database credentials leaked; R2 bucket public; looked-up phone numbers exposed; signing key exposed |
| SEV-2 | Possible exposure, not yet confirmed | Suspicious admin sign-in; unexplained bulk download; vendor reports an incident |
| SEV-3 | Security weakness with no sign of use | A vulnerability report; a misconfiguration found and closed |

SEV-1 and SEV-2 involve counsel the same day.

## 5. Response steps

### 5.1 Triage (first hour)

1. Open an incident record `[OPEN: where; a private document or ticket]` with the time found and by whom.
2. Assign the incident lead and a severity.
3. Preserve evidence before changing anything (5.3).

### 5.2 Contain

Use only the steps that fit. Each is reversible unless marked.

- **Rotate exposed secrets.** Candidates: database URLs (`DATABASE_URL`, `DATABASE_URL_SYNC`,
  `DATABASE_URL_MIGRATE`), `SECRET_KEY` (signs sessions and tokens; rotating signs everyone out),
  `BFF_SHARED_SECRET` (Railway and Vercel together), R2 keys (`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`),
  `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`, `RESEND_API_KEY`, `TRACERFY_API_TOKEN` and
  `TRACERFY_WEBHOOK_SECRET`, `CAPTCHA_API_KEY`, `OPS_ALERT_WEBHOOK_URL` and `OPS_HEARTBEAT_URL` (the URLs are
  credentials).
- **Encryption and keyed-hash keys** (`FIELD_ENCRYPTION_KEY`, `BLIND_INDEX_KEY`, `PRIVACY_HMAC_KEYS`) need a
  planned rotation: stored data, lookups and suppression rules depend on them. Do not drop an old
  `PRIVACY_HMAC_KEYS` version while an active rule still uses it: the rule loader then refuses every rule,
  and by design every delivery and lookup path stops (fails closed) until it is fixed
  (`src/privacy/authorize.py`). `[OPEN: rotation runbook for each key]`
- **Stop outbound data:** pause contact lookups (`SKIP_TRACE_ENABLED=false`), pause scraping, pause dialer and
  webhook delivery `[OPEN: confirm the switch for each]`.
- **Lock accounts:** revoke sessions and API keys for affected accounts.
- **Storage:** remove public access, expire signed links, rotate R2 keys.
- **Vendor:** ask the vendor to revoke credentials or files (for example the Tracerfy result-file link).

### 5.3 Preserve evidence

- Export the relevant `audit_events` rows (append-only), Railway logs and Loki logs before their retention ends
  `[OPEN: Railway log retention period]`.
- Record database state with a snapshot if the incident touches data integrity.
- Keep copies of alerts and emails. Do not delete anything a purge would delete: pause the relevant purge if
  needed (`RETENTION_PURGE_ENABLED=false`) and record that you did.

### 5.4 Assess

Answer, in writing:

1. What data, and whose? Use `02-DATA-PROCESSING-INVENTORY.md` to map stores to data. Separate customers
   from people in lead records.
2. Was it encrypted, and were the keys also exposed? (Contact data and several name and address fields are
   stored encrypted; lead rows also hold public-record fields in plain form.)
3. How many people, and in which states? Mailing addresses give state of residence for lead-record subjects.
4. Was it acquired, or only accessible?
5. Is it still happening?

### 5.5 Decide on notice

Counsel decides. Bring the answers from 5.4 and the questions in section 7.

### 5.6 Recover

Fix the cause, verify the fix, restore normal operation, and watch for recurrence.

### 5.7 Review

Within `[OPEN: N]` days: write a blameless review, record it in `docs/BUILD_JOURNAL.md`, and track fixes in
`tasks/`. Update this plan.

## 6. Communication templates

`[OPEN: drafts for (a) customers, (b) people in lead records, (c) regulators, (d) vendors. Counsel to approve.
Each must state what happened, what data, what we did, what the person can do, and how to contact us.]`

## 7. Questions for counsel

1. Which state breach-notice laws apply, given that lead-record subjects can live in any state (mailing
   addresses), and what each requires (who, when, content, regulator notice, thresholds)? Confirm the audit's
   reading of RCW 19.255.010 (Washington consumers and the Attorney General within 30 days, the Attorney
   General when more than 500 Washington residents are affected).
2. Does public-record data (names, property addresses) count as covered personal information when exposed
   without a phone number, email or account credential? Does encrypted data with an unexposed key?
3. What must we tell customers when their own accounts are affected, and what when only lead data is?
4. What must vendors tell us, and how fast? `[OPEN: no vendor agreement is on file (03-VENDOR-REGISTER.md).]`
5. If we are a registered data broker (D8), does any broker law add security or notice duties?
6. Any duty to notify our payment processor or card networks?

## 8. Testing

`[OPEN: run a tabletop exercise before launch and once a year.]`

## 9. Vendor security contacts

`[OPEN: security contact and notice terms for Supabase, Railway, Vercel, Cloudflare, Upstash, Stripe, Resend,
Tracerfy, Google, 2Captcha (if used), Grafana Labs, Better Stack, Sentry (if enabled), GitHub.]`
