# M5 — DB / Redis Network Posture (IP Allowlisting + Transport Security)

**Security-checklist item M5** (`SECURITY_CHECKLIST_AUDIT_2026-06-08.md:100`). Severity: 🟠 Medium.
**Status:** documented (this file). The restrictions themselves are **ops/infra actions** on
Supabase / Upstash / Railway — see §3-§4. Last updated 2026-06-15.

> **TL;DR.** Transport is the strong part: workers + API reach Redis over `rediss://` with cert
> verification on by default (M1 fixed). The gap is **network-level access control** — nothing in
> Supabase/Upstash currently restricts *which IPs* may open a DB/Redis connection, so anyone holding the
> role passwords (incl. whoever has the local `.rls-cutover-secrets` file) can connect from anywhere.
> A strict IP allowlist is **blocked on Railway's dynamic egress** until Static Outbound IPs (Pro plan)
> is enabled; until then, transport + credential hygiene + RLS are the controls, and the residual risk
> is documented here.

---

## 1. Current connection posture (from code)

### PostgreSQL (Supabase)
| Var | Driver / port | Role (post-RLS-cutover) | Source |
|---|---|---|---|
| `DATABASE_URL` | asyncpg → **forced to :5432** direct | `bridgeleads_app` (NOBYPASSRLS) | `session.py:14`, `settings.py:30` |
| `DATABASE_URL_SYNC` | psycopg2 → **forced to :6543** pooler | `bridgeleads_system` (NOBYPASSRLS) | `session.py:51`, `settings.py:31` |
| `DATABASE_URL_MIGRATE` | Alembic DDL only (falls back to SYNC) | owner | `alembic/env.py:22`, `settings.py:32` |

Port forcing is deliberate: asyncpg breaks on pgbouncer's prepared-statement handling (so async uses the
direct 5432 port), while 4+ Celery workers exhaust session-mode slots (so sync routes through the 6543
transaction pooler). See the comments at `session.py:13-14` and `session.py:50-51`.

**Transport:** **no `sslmode` is set anywhere in code** (grep for `sslmode`/`sslrootcert`/`connect_args`
SSL = 0 hits). TLS is implicitly whatever the Railway-set URL string carries. Supabase-issued URIs
usually include `?sslmode=require`, but the app neither enforces nor verifies it — this is a Tier-1 gap (§3).

### Redis (Upstash) — transport is solid
- `REDIS_URL` is `rediss://` (TLS), no default (`settings.py:35`).
- `REDIS_SSL_CERT_REQS` defaults to **`"required"`** (`settings.py:39`); `redis_kwargs()` attaches certifi's
  CA bundle for any `rediss://` URL (`settings.py:292-324`). The old M1 finding (`ssl_cert_reqs="none"`) is
  **fixed** (`SECURITY_CHECKLIST_AUDIT_2026-06-08.md:53,96`). This is the M4-item-5 / BACKLOG §4 "verify
  Redis CERT_REQUIRED" control — it is on by default; the only ops check is confirming the Railway env var
  is **not** overridden to `none`.

### What's missing
`docs/product/devops.md` documents the Railway→Supabase/Upstash/R2 topology but has **zero** IP-allowlist
or DB/Redis firewall steps; `docs/deployment/launch-checklist.md` likewise has no network-restriction step.
That documentation+config gap is exactly what M5 calls out.

---

## 2. Threat model — why this matters for BridgeLeads specifically

The `.rls-cutover-secrets` file at the repo root is the **only off-Railway copy** of the
`bridgeleads_app` / `bridgeleads_system` DB passwords (gitignored — `.gitignore:145`; BACKLOG §4 has its
"move to a password manager" action still open). If that machine is compromised and the passwords leak,
an attacker can open a **direct Supabase connection from any IP** — RLS limits *what* `bridgeleads_app`
can read per-tenant, but `bridgeleads_system` is broad. A Supabase Network Restriction (IP allowlist)
is the control that would deny that connection at the network layer regardless of credentials. This makes
IP restriction unusually high-value here, even though it's "only" Medium severity in the abstract.

---

## 3. Recommendations — Tier 1 (achievable now, no plan upgrade)

1. **Enforce SSL on Supabase** (Dashboard → Settings → Database → SSL Configuration → *Enforce SSL on
   incoming connections*). Applies to both 5432 and 6543. Rejects any non-TLS client.
   ([docs](https://supabase.com/docs/guides/platform/ssl-enforcement))
2. **Make TLS explicit in the app** (belt-and-suspenders): add `?sslmode=require` to all three
   `DATABASE_URL*` Railway env values, **or** pass `connect_args={"sslmode": "require"}` (sync) /
   the asyncpg `ssl` arg in `session.py`. ⚠️ Code follow-up — guard so **local dev** (localhost Postgres
   without TLS) still works (e.g. only require when host ≠ localhost). Track as a small Codex-gated PR.
3. **Confirm Redis cert verification on Railway:** `REDIS_SSL_CERT_REQS` is unset or `required` on the
   api + worker services (NOT `none`). This is the BACKLOG §4 "verify Redis CERT_REQUIRED" item — verify via
   `railway variables` on both services; the default already fails safe.

## 4. Recommendations — Tier 2 (requires Railway Pro plan)

Railway uses **dynamic outbound IPs** by default — there is nothing stable to allowlist until you enable
**Static Outbound IPs** (Pro plan, per-service) on api + worker + beat.
([Railway docs](https://docs.railway.com/networking/static-outbound-ips))

4. Enable Static Outbound IPs on the three services; record the assigned IPv4s.
5. **Supabase Network Restrictions** — allowlist only those Railway IPs (+ your admin/migration IP).
   Covers direct **and** pooler ports; does not cover Supabase's HTTPS APIs (not used here).
   ([docs](https://supabase.com/docs/guides/platform/network-restrictions))
6. **Upstash IP Allowlist** — same Railway IPs. Available on **paid plans only**, **IPv4 only**.
   ([docs](https://upstash.com/docs/redis/howto/ipallowlist))

## 5. Residual risk to accept/document
- Railway static IPs are **shared-tenant** — another Railway customer could originate from the same IP.
  Partial control, not hard isolation. A dedicated egress proxy (QuotaGuard/Fixie) is the only way to a
  truly dedicated IP, and is likely overkill at this stage.
- Static outbound IPs can **change when a service's region changes** (and are not guaranteed permanent
  across infra changes) — re-verify the allowlist after any region move.
- Upstash allowlist is IPv4-only (no IPv6 coverage).
- **Until Tier 2 is enabled, the DB/Redis access boundary is credentials + TLS + RLS only** — accept this
  explicitly and prioritize moving `.rls-cutover-secrets` off the local disk (BACKLOG §4).

---

## 6. Acceptance / verification
- [ ] Tier 1.1 — Supabase "Enforce SSL" enabled (verify a non-SSL connection is rejected).
- [ ] Tier 1.3 — `REDIS_SSL_CERT_REQS` confirmed not `none` on api + worker (`railway variables`).
- [ ] Tier 1.2 — `sslmode=require` shipped (env or `connect_args`, localhost-guarded) — Codex-gated PR.
- [ ] Tier 2 — *if* Railway Pro: Static Outbound IPs on api/worker/beat → Supabase Network Restrictions +
      Upstash IP allowlist set to those IPs. If not on Pro: §5 residual risk accepted in writing.
- [ ] `docs/deployment/launch-checklist.md` updated with the Supabase/Upstash network steps (close the
      documentation gap that M5 flagged).

> M5 is satisfied by this documented posture + decisions: Tier 1 is the immediate bar (SSL enforcement +
> explicit `sslmode` + Redis cert verification), Tier 2 is gated on the Railway Pro static-IP capability,
> and the residual risk is recorded in §5 for the period before Tier 2 lands.
