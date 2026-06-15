# M4 — Edge DDoS Rate-Limit Rules + Distributed-Limiter Resilience

**Security-checklist item M4** (`SECURITY_CHECKLIST_AUDIT_2026-06-08.md`). Severity: 🟠 Medium.
**Status:** documented (this file). Edge rules are an **ops/infra action** on Cloudflare — see §4.
Last updated 2026-06-15.

> **TL;DR.** BridgeLeads has a solid *application-layer* rate limiter, but it lives on the origin
> (Railway) and depends on Redis. It cannot absorb a volumetric/L7 flood, and it **fails open** for
> the `general`/`jobs` zones during a Redis outage. Cloudflare edge rules are the
> infrastructure-independent backstop that stays effective exactly when the app limiter has degraded.
> The two layers protect against different failure modes; neither alone is sufficient.

---

## 1. Current application-layer protection (origin / Railway)

Implemented in `src/api/middleware/rate_limit.py` (sliding-window over a Redis sorted set; the four
Redis ops run atomically in one pipeline — `rate_limit.py:147-153`).

| Zone | Limit | Window | Keyed by | Applied to |
|---|---|---|---|---|
| `auth` | 10 | 60s | client IP | login, register, refresh, all MFA, password-reset, break-glass |
| `jobs` | 5 | 60s | `user_id` | `POST /jobs` |
| `general` | 60 | 60s | `user_id` | batch create/download, results, everything else |
| `webhook` | 120 | 60s | source | Stripe + Tracerfy webhook POSTs |
| `stripe` | 10 | 60s | `user_id` | billing subscription/checkout/portal |

Zones defined at `rate_limit.py:24-42`.

**Client-IP extraction** (`client_ip()`, `rate_limit.py:78-109`): reads `X-Forwarded-For` only when
the direct peer is a trusted private range, then takes the entry `TRUSTED_PROXY_HOPS` from the
**right** of the XFF chain (the left is client-forgeable). `TRUSTED_PROXY_HOPS` defaults to `1`
(`settings.py:179`) for Railway's single proxy hop. `CF-Connecting-IP` is **deliberately not trusted**
today (`rate_limit.py:92-97`) because, without a Cloudflare proxy in front, an attacker on Railway can
forge it. **This assumption changes the moment Cloudflare proxies the API — see §3.**

**Companion middleware:** SSRF firewall + outbound-webhook validation (`security.py`), security
headers incl. HSTS (`security.py:495-516`), audit logging to `audit_events` (`security.py:520-609`),
and progressive brute-force lockout (`auth_hardening.py:367-372`: 5→1m, 10→5m, 20→30m, 50→24h).

---

## 2. The resilience gap (why the edge layer is required)

When Upstash Redis is unavailable, the app limiter degrades **non-uniformly**:

| Path | Behavior on Redis outage | Source |
|---|---|---|
| `general`, `jobs` zones | **fail OPEN** — all requests allowed, WARN logged | `rate_limit.py:154-179` |
| `auth`, `webhook`, `stripe` zones | per-process in-memory fallback (~10/min **per worker**) | `rate_limit.py:117-128` |
| `BruteForceProtection.check()` | **fails OPEN** — lockout not applied | `auth_hardening.py:439-444` |
| `TokenBlacklist.is_blacklisted()` | **fails CLOSED** — returns 503 (revocation is a hard boundary) | `auth_hardening.py:96-109` |

Consequence of a Redis outage window: volumetric attacks on general API paths face **zero** app-layer
throttling; brute-force on `/auth/login` faces only the coarse per-worker fallback (`N workers × 10/min`).
The fail-open choice is intentional (failing closed would 500 the whole API), but it means **the edge
is the only protection that survives a Redis outage.** That is the core M4 argument.

---

## 3. ⚠️ Integration prerequisite if Cloudflare proxies the API

Today Cloudflare is DNS-only for the API. If the API hostname is switched to **proxied (orange-cloud)**,
the XFF chain gains a Cloudflare hop and the current right-anchored `client_ip()` logic will read the
**Cloudflare edge IP**, collapsing every client into one rate-limit bucket — the limiter looks
operational but is dysfunctional. Before proxying the API, do **one** of:

- **(preferred)** Prevent origin bypass at the **network/proxy layer** — restrict the API origin so it
  only accepts traffic that actually transited Cloudflare (allowlist [Cloudflare's published IP
  ranges](https://www.cloudflare.com/ips/) at the edge in front of Railway, or use Cloudflare Tunnel /
  Authenticated Origin Pulls). *Only then* trust `CF-Connecting-IP`. ⚠️ Trusting `CF-Connecting-IP` while
  the Railway origin is still directly reachable on the public internet is **dangerous** — an attacker
  hits Railway directly, spoofs the header, and bypasses every edge rule. App-level "trust CF-IP" is **not**
  sufficient on its own; the lock must be enforced before the request reaches the app.
- **(minimum, app-level)** Set `TRUSTED_PROXY_HOPS=2` (Railway hop + Cloudflare hop) so `client_ip()`
  resolves the real client at `parts[-2]`. This fixes rate-limit bucketing but does **not** close the
  origin-bypass hole — pair it with the network-layer lock above.

Either change is a code/config follow-up tracked here — **do not enable the API orange-cloud without it.**

---

## 4. Recommended Cloudflare edge rules (ops action)

These run on Cloudflare's distributed network with **no dependency on origin Redis** — blocked requests
never reach Railway. Configure under **Security → WAF**.

### 4a. Always-on / plan-included
- **HTTP DDoS Attack Protection managed ruleset** — on by default, all plans, cannot be disabled. Absorbs
  volumetric + L7 floods. ([docs](https://developers.cloudflare.com/ddos-protection/managed-rulesets/http/))
- **WAF managed rules** — note the plan tiers: a basic **Cloudflare Free Managed Ruleset** is available on
  **all plans**; the full **Cloudflare Managed Ruleset + OWASP Core Rule Set** require **Pro and up**.
  Enable whatever the plan offers for the API hostname.
  ([docs](https://developers.cloudflare.com/waf/managed-rules/))
- **Security Level = Medium** on API paths — IP-reputation/threat-score gating with no JS challenge for
  clean IPs. ([docs](https://developers.cloudflare.com/waf/tools/security-level/))

### 4b. Rate Limiting Rules (edge equivalent of our zones, intentionally *looser*)

> Rate Limiting Rules exist on **all plans incl. Free** (Free is capped to a small number of simple rules
> with a fixed period); the richer counting characteristics + actions below are Pro+.
> ([docs](https://developers.cloudflare.com/waf/rate-limiting-rules/))

| Rule | Match expression | Limit | Action |
|---|---|---|---|
| Auth brute-force | path contains `/auth/login`, `/auth/register`, or `/auth/refresh` | 20 / 60s per IP | Managed Challenge or Block |
| Password reset | path contains `/auth/password` | 10 / 60s per IP | Block |
| Billing | path contains `/billing/` | 30 / 60s per IP | Block |
| General API | path wildcard `/api/*` (or the API host) | 200 / 60s per IP | Block |
| Webhooks | path contains `/webhooks/` | 200 / 60s per IP | Block |

Keep edge limits **looser than the app limits** (e.g. 20 vs the app's 10 on auth): the edge stops floods,
the app enforces the precise per-user/plan policy. Equal-or-tighter edge limits risk false-positive blocks.
([rate-limiting docs](https://developers.cloudflare.com/waf/rate-limiting-rules/) ·
[best practices](https://developers.cloudflare.com/waf/rate-limiting-rules/best-practices/))

### 4c. Bots & break-glass — with caveats
- **Bot challenges break JSON API clients** (JS interstitials; the Next.js frontend's fetches + any
  programmatic caller can't solve them). Tier matters:
  - **plain Bot Fight Mode (Free)** protects the **entire domain** and **cannot be scoped/skipped** by WAF
    or Page Rules. So you can't "exclude the API path." Instead: serve the API on a **separate hostname**
    so the marketing domain's BFM never touches it, **or** disable BFM and step up to the next option.
    ([docs](https://developers.cloudflare.com/bots/get-started/bot-fight-mode/))
  - **Super Bot Fight Mode (Pro/Biz) / Bot Management (Ent)** *do* support skip rules — allow known-good
    callers (Stripe, Tracerfy, the frontend) by ASN/header/bot-score and challenge the rest.
- **"I'm Under Attack" mode** — break-glass only, frontend host only (also JS-challenges everyone). Never
  leave it on API paths.

---

## 5. Acceptance / verification
- [ ] Cloudflare HTTP DDoS managed ruleset confirmed active on the API + frontend zones (default; verify).
- [ ] WAF managed ruleset + the five Rate Limiting Rules above created on the API host (Pro+ plan).
- [ ] Bot modes scoped to frontend paths only (not the API).
- [ ] If/when the API is orange-clouded: §3 prerequisite (CF-IP lock **or** `TRUSTED_PROXY_HOPS=2`) shipped
      first, then re-verify `client_ip()` resolves distinct client IPs (not the CF edge IP).
- [ ] Ops runbook references this file for the Redis-outage degradation modes (§2).

> M4 is satisfied by (a) this documented design + the edge rules in §4 being applied in Cloudflare, and
> (b) honoring the §3 prerequisite before any API proxy cutover. The distributed-limiter resilience
> concern is documented in §2; no app-code change is mandatory, but the §3 proxy-trust fix is required
> *conditional* on proxying the API.
