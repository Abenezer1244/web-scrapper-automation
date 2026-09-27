# BridgeLeads Frontend Security Audit, round 2 (`bridgeleads-web`)

- Date: 2026-09-25
- Audited ref: `origin/master` @ `8332673` (feat(results): say why a pre-foreclosure run has no auction dates (#160))
- Clean worktree (left in place): `C:/Users/Windows/bl-web-secaudit2` (detached, `git status` clean; `.next/` and `node_modules/` are gitignored build output)
- Stack as built: Next.js **16.3.5** (Turbopack, `proxy.ts` replaces `middleware.ts`), next-auth **5.0.0-beta.32**, React **19.2.3**
- Backend cross-checks read from `C:/Users/Windows/bl-wt-secaudit2` @ `fc38e620`
- Prior round: `tasks/audit-frontend.md` (2026-09-16, F-01..F-11). Status of each is carried forward below.

## Method

1. `npm ci` then `npm run build` with placeholder env only (`AUTH_SECRET=placeholder-build-only-000000000000`, `NEXT_PUBLIC_API_URL=https://api.bridgeleads.io`). No real `.env` was copied. Build: exit 0, TypeScript clean, 21 pages.
2. Regex scan of `.next/static/**` (92 files, 60 JS, 3.8 MB). Separately fetched 20 production chunks from `https://bridgeleads.io` (login + landing) and scanned those too, plus a `.map` probe on each.
3. `npm audit --omit=dev` and `npm audit` (live registry, `auditReportVersion: 2`).
4. `next start` locally on the built bundle and probed the `proxy.ts` gate with 26 crafted requests (bypass classes listed in section 4).
5. Read-only `curl` of production response headers and Auth.js cookie flags.
6. Source review of XSS sinks, navigations, gating, Auth.js config and route handlers.

---

## Findings

Severity is not inflated. No P0, P1 or P2 in this round. Everything below is hardening or UI-consistency.

### W-1 [P3] `/admin/connectors` and the Admin nav group are gated on `plan === "agency"`, not `is_admin` (prior F-01, downgraded)

- Evidence: `app/(dashboard)/admin/connectors/page.tsx:48-49` (`const isAgency = userPlan === "agency"`), `lib/nav.ts:28` (`agencyOnly: true`) and `lib/nav.ts:35` (`plan === "agency"`). The sibling `app/(dashboard)/admin/funnel/page.tsx:52` correctly uses `session?.user?.is_admin === true`.
- Backend verdict (closes the prior P1 question): `POST /scrapers/connectors` is `dependencies=[Depends(require_admin_mfa)]` (`src/api/routes/scrapers.py:932-936`): non-admins get 404, admins need enrolled MFA and a fresh MFA-backed JWT, and `base_url` passes `validate_scraping_target(..., require_allowlisted=False)` before persisting. `GET /billing/activation-funnel` is `Depends(require_admin)` (`src/api/routes/billing.py:118-120`).
- Prerequisites: an authenticated agency-plan user who is not an admin.
- Impact: none on data. An agency non-admin sees an Admin menu and a connector form that always 404s; a real admin on a non-agency plan cannot reach either admin page from the nav. Confusing, and it advertises the admin surface to paying customers.
- Fix: gate both on `session.user.is_admin === true` (page line 49, and give `NavGroup` an `adminOnly` flag used in `lib/nav.ts:35`).
- Regression test: with a session `{plan:"agency", is_admin:false}` the nav has no Admin group and `/admin/connectors` shows the access-denied state; with `{plan:"starter", is_admin:true}` both appear.

### W-2 [P3] Route-param IDs are interpolated into backend paths unencoded (client-side path traversal, defence-in-depth)

- Evidence: `lib/api.ts:826` (`DELETE /scrapers/${id}`), `:850`, `:858` (`/batches/${id}`, `/batches/${id}/download`), `:886`, `:937`, `:952` (`batchId`, `runId`), `:1001` and `:1005` (`GET` / `DELETE /jobs/${id}`), `:1040` (`/jobs/${jobId}/results`). Only four call sites use `encodeURIComponent` (for example `:757`, `:793`, `:803`). The IDs come from dynamic route segments, and Next decodes params (`node_modules/next/dist/shared/lib/router/utils/route-matcher.js:19` `decodeURIComponent(param)`), so `/live/..%2f..%2fauth%2fme` yields `id = "../../auth/me"`.
- Prerequisites: victim logged in, clicks an attacker link on `app.bridgeleads.io`.
- Impact: the browser sends the victim's bearer to an attacker-chosen backend path. GETs only render into the victim's own page (React-escaped, nothing exfiltrated). The one mutating path, `cancelJob(id)` on `app/(dashboard)/live/[id]/page.tsx:253`, is only offered when the preceding `GET` with the same `id` returns an object whose `status` is cancellable (`:216`), and the backend exposes only two DELETE routes (`/jobs/{id}`, `/scrapers/{id}`), so no practical cross-resource DELETE exists today. Not end-to-end exploited (probing needs a real session against prod, which was not done). Filed because it is one new mutating route away from being real.
- Fix: `encodeURIComponent` every path segment in `lib/api.ts` (or a `path\`...\`` tagged-template helper that encodes interpolations), and optionally reject route params that are not UUIDs before querying.
- Regression test: unit test that `cancelJob("../scrapers/x")` issues `DELETE .../jobs/..%2Fscrapers%2Fx`; grep-gate in CI for `` `/[a-z]+/\$\{ `` without `encodeURIComponent`.

### W-3 [P3] CSP keeps `'unsafe-inline'` and `'unsafe-eval'` in `script-src`, plus three unused or broad grants (prior F-02/F-07, partially fixed)

- Evidence: `next.config.ts:78` `script-src 'self' 'unsafe-inline' 'unsafe-eval'`; `:83` `frame-src https://my.spline.design https://js.stripe.com`; `:41` `connect-src ... https://*.bridgeleads.io https://*.stripe.com`. Production header confirmed identical (curl of `https://bridgeleads.io/login` and `https://app.bridgeleads.io/login`).
- Confirmed fixed since round 1: `base-uri 'self'`, `object-src 'none'`, `form-action 'self'` are present; `https://*.supabase.co` is gone.
- Dead grants: nothing in `app/`, `components/`, `lib/` references `spline` or `js.stripe.com` (grep empty); Stripe is reached only by top-level navigation to backend-minted URLs, so neither `frame-src` entry nor `connect-src https://*.stripe.com` is used. `*.bridgeleads.io` wildcard means a dangling subdomain would become an exfil target.
- Prerequisites: an XSS sink, of which none is reachable (section 3).
- Impact: defence-in-depth only.
- Fix: now, drop `frame-src` to `'none'`, drop `https://*.stripe.com` from `connect-src`, and narrow `https://*.bridgeleads.io` to `https://api.bridgeleads.io`. Test on a preview deploy whether `'unsafe-eval'` can be dropped (Turbopack prod output normally needs no eval). Later, nonce-based CSP through `proxy.ts` to remove `'unsafe-inline'`.
- Regression test: a CI check that fetches the preview deploy's CSP and asserts no `unsafe-eval`, no `*.stripe.com`, no `spline`.

### W-4 [P3] Backend access token is readable by page JS via `/api/auth/session` (prior F-03, unchanged, accepted design)

- Evidence: `lib/auth.ts:233` `session.accessToken = token.accessToken`; consumed at `lib/api.ts:42-49`. Refresh token is correctly withheld (`lib/auth.ts:232-236`).
- Prerequisites: script execution on the app origin (none reachable today).
- Impact: an XSS would yield a bearer valid for at most 1 hour; no refresh capability.
- Fix: none urgent. The long-term fix is a same-origin API proxy so the bearer never reaches the browser; not justified over W-3.
- Regression test: assert the `/api/auth/session` JSON never contains `refreshToken`.

### W-5 [P3] `Access-Control-Allow-Origin: *` on HTML responses (prior F-04, unchanged)

- Evidence: present on production `https://bridgeleads.io/login` and `https://app.bridgeleads.io/login`; not set anywhere in the repo (Vercel edge injects it). No `Access-Control-Allow-Credentials`.
- Impact: none. Credentialed cross-origin reads are refused by the browser; uncredentialed reads get the login redirect or public marketing HTML.
- Fix: remove at the Vercel project level for hygiene.

### W-6 [P3] Dead `experimental.serverActions.allowedOrigins` includes `localhost:3000` and `localhost:3005` in production config

- Evidence: `next.config.ts:4-14`. Zero `"use server"` in the codebase, so it guards nothing today.
- Impact: none today. If a server action is ever added, the production build would accept its cross-origin POSTs from a localhost origin.
- Fix: delete the block, or make the localhost entries `isDev`-only.

### Carried forward and now closed

- Prior F-06 / F-26 (Stripe `portal_url` unvalidated): FIXED. `redirectToStripe(url, kind)` at `lib/api.ts:1316-1329` enforces `https://checkout.stripe.com/` or `https://billing.stripe.com/`; used by `components/shell/UserMenu.tsx:54` and `components/shell/CommandPalette.tsx:169`. `components/settings/BillingTab.tsx:86` and `:170` keep equivalent inline prefix checks (consider routing them through the helper too, P3 cosmetic).
- Prior F-09 (deps unscanned): now scanned, zero advisories (section 2). `remotion` still sits in `devDependencies` now, fine.
- Prior F-08 (`.env.check` OIDC token): not re-checked; it lives only in the other checkout and is out of scope for a clean worktree.

---

## 1. Bundle scan

Local client bundle `.next/static/**` (92 files) and 20 production chunks from `bridgeleads.io`, regex:
`sk_live|sk_test|rk_live|rk_test|whsec_|pk_live_|pk_test_|postgres(ql)?://|rediss?://|service_role|eyJ<jwt>|re_[A-Za-z0-9]{16,}|AUTH_SECRET|NEXTAUTH_SECRET|<the placeholder secret>|railway.internal|up.railway.app|tracerfy|AKIA...|BEGIN PRIVATE|supabase.co|r2.cloudflarestorage|api_key|secret|localhost:8000`

| Pattern | Hits | Classification |
|---|---|---|
| Stripe `sk_`/`rk_`/`whsec_`/`pk_` | 0 | none shipped (Stripe is redirect-only, so not even a publishable key) |
| `postgres://`, `redis://`, `service_role`, JWT `eyJ...` | 0 | none |
| Resend `re_...`, Tracerfy, AWS `AKIA`, private keys | 0 | none |
| `AUTH_SECRET` / placeholder secret value | 0 in `.next/static`, 0 in `.next/server` | the placeholder appears only in `.next/cache/turbopack/*.sst` (local build cache, never deployed) |
| `railway.internal`, `*.up.railway.app`, `localhost:8000` | 0 | none; dev-only `connect-src` localhost correctly absent from prod CSP |
| `secret` (2), `api_key` (1) | 3 | benign identifiers: MFA setup-key field `e.secret` rendered to its owner, and `generateApiKey` response field. No values |
| Hostnames | `api.bridgeleads.io` (the inlined `NEXT_PUBLIC_API_URL`), `checkout.stripe.com`, `billing.stripe.com`, `recorder.kingcounty.gov`, `hooks.zapier.com` / `hooks.example.com` (placeholder text), library doc URLs, `http://localhost:3000/api/auth` (next-auth client default base, overridden at runtime) | all intended-public |

`NEXT_PUBLIC_*` inventory: exactly one, `NEXT_PUBLIC_API_URL` (7 references: `lib/api.ts:39`, `lib/auth.ts:6`, `app/(dashboard)/layout.tsx:8`, `app/(auth)/register/page.tsx:21`, `app/(marketing)/_monopo/pricingApi.ts:22`, `app/(marketing)/coverage/page.tsx:27`). Intended-public. Server-only `API_URL` stays unprefixed.

Source maps: 0 `.map` files under `.next/static`, no `sourceMappingURL` comments, `productionBrowserSourceMaps` not set. Production: all 20 probed `/_next/static/immutable/chunks/*.js.map` return **404**. Server-side maps exist only under `.next/server` / `.next/build` (not web-served).

## 2. npm audit

| Scope | critical | high | moderate | low | info | total |
|---|---|---|---|---|---|---|
| `--omit=dev` (128 prod deps) | 0 | 0 | 0 | 0 | 0 | 0 |
| full (incl. 691 dev deps) | 0 | 0 | 0 | 0 | 0 | 0 |

Version notes: Next 16.3.5 is past CVE-2025-29927 (`x-middleware-subrequest`, fixed 15.2.3) and the Dec-2025 RSC/Flight advisories; React 19.2.3 carries the RSC follow-up fixes; next-auth beta.32 is past GHSA-8fpg-xm3f-6cx3 (<= beta.31). Standing risk: production auth still rides a next-auth beta and uses `unstable_update`.

## 3. XSS

- `dangerouslySetInnerHTML|innerHTML|outerHTML|document.write|eval(|new Function|srcdoc|insertAdjacentHTML|javascript:` plus markdown libs: **one** hit, `components/ui/chart.tsx:95` (shadcn `ChartStyle`, fed only by hardcoded chart configs). Unreachable. No markdown/HTML renderer, no `next/script`, no third-party SDK.
- Scraped fields (party names, property/mailing addresses, doc types), list names, scraper names and webhook URLs (`app/(dashboard)/deliver/page.tsx:178,187`) render as JSX text children, React-escaped.
- Lead-derived hrefs: `EmailCell.tsx:49` and `SegmentCards.tsx:80` use `mailto:${em}`, `PhoneCell.tsx:59` and `SegmentCards.tsx:63` use `tel:${dialablePhone(...)}`. The scheme is fixed by the template, so a `javascript:` value cannot change it. No county source URL is rendered as a link.
- API-derived href: `components/onboarding-banner.tsx:133` `href={next_action.route}` comes from our backend (`GET /auth/onboarding`), not user or county data, and React 19 blocks `javascript:` URLs in `href`. Non-finding.
- API error text: `lib/errors.ts` filters backend messages through `LEAK_SIGNATURES` (including an HTML-markup pattern) and a length cap before `sonner` renders them as text.

## 4. Client-only gating and the `proxy.ts` gate

| Gate | Where | Backend endpoint that must enforce | Status |
|---|---|---|---|
| Admin connectors (plan not admin) | `admin/connectors/page.tsx:49`, `lib/nav.ts:28,35` | `POST /scrapers/connectors` | **verified** `require_admin_mfa` + SSRF validation (W-1) |
| Admin funnel | `admin/funnel/page.tsx:52` | `GET /billing/activation-funnel` | **verified** `require_admin` |
| Record type by plan | `lib/entitlements.ts:73-80`, `CountyStep.tsx:369,679` | `POST/PATCH /scrapers` | not re-verified here (backend audit scope) |
| County cap | `entitlements.ts:84` | `POST/PATCH /scrapers` | not re-verified |
| Batch | `entitlements.ts:89`, `scrapers/new/page.tsx:113` | `POST /batches` | not re-verified |
| Webhook | `entitlements.ts:94`, `scrapers/[id]/edit/page.tsx:45` | scraper create/update `webhook_url` | not re-verified |
| Overlap / segments | `entitlements.ts:104`, `segments/page.tsx:94` | `/segments/*` | not re-verified |
| Export formats | `entitlements.ts:118` | scraper create/update, export routes | not re-verified |
| Frequencies | `entitlements.ts:139` | scraper create/update | not re-verified |
| API keys | `entitlements.ts:151`, `ApiKeysTab.tsx:55` | `POST /auth/api-keys` | not re-verified |
| Skip trace | `entitlements.ts:156`, `scrapers/[id]/edit/page.tsx:46` | skip-trace endpoints | not re-verified |
| Frozen / over-quota hides rows | `scrapers/[id]/records/page.tsx:82,236` | records/results GETs must not return rows | not re-verified (verify via network response, not UI) |

`lib/entitlements.ts` still fails closed (unknown plan -> starter, unknown record type -> denied).

`proxy.ts` gate probe (local `next start` of the production build, no session):

| Request | Result |
|---|---|
| `/dashboard`, `/Dashboard`, `/DASHBOARD`, `/admin/*`, `/results/abc`, `/settings` | 307 to `/login` |
| `/dashboard/` | 308 to `/dashboard` (then gated) |
| `RSC: 1`, `RSC` + `Next-Router-Prefetch`, `?_rsc=` | 307 to `/login` |
| `x-middleware-subrequest: proxy:... / middleware:... / src/proxy:...` (x5) | 307 to `/login` (not bypassable) |
| `/_next/data/x/dashboard.json` | 307 |
| `/%64ashboard`, `/dashboard%2f` | 307 |
| `/login/..%2fdashboard` | 404; `/api/auth/..%2f..%2fdashboard` 400 (Auth.js rejects) |
| `/api/auth/../../dashboard`, `/favicon.ico/../dashboard`, `/_next/static/../../dashboard`, `/public/../dashboard` (`--path-as-is`) | 307 |
| `/_next/image?url=/dashboard` | 400 |
| `POST /api/session/refresh` | 401 (self-guarding handler) |

The matcher excludes only `_next/static`, `_next/image`, `favicon.ico`, `public/`; no route lives under those prefixes. The segment-boundary public match (`proxy.ts` `pathname === r || startsWith(r + "/")`) and the fail-closed `!req.auth?.user` check hold. Independent second gate: `app/(dashboard)/layout.tsx:36` redirects when `session?.accessToken` is missing, so even a proxy bypass would render no dashboard.

## 5. Auth.js

| Item | Observed |
|---|---|
| Session strategy | `jwt`, 7-day `maxAge` (`lib/auth.ts:200-203`); Auth.js JWE-encrypted with `AUTH_SECRET` |
| Session cookie | Auth.js defaults (no `cookies` override): `__Secure-authjs.session-token`, HttpOnly, Secure, SameSite=Lax |
| CSRF / callback cookies (prod, curl) | `__Host-authjs.csrf-token; Path=/; HttpOnly; Secure; SameSite=Lax`, `__Secure-authjs.callback-url; HttpOnly; Secure; SameSite=Lax` |
| Token exposed to JS | access token yes (W-4); refresh token no; `is_admin`/`plan` sourced from `/auth/me` and sealed server-side |
| `callbackUrl` / open redirect | `proxy.ts` sets `callbackUrl`, the login page never reads it; all `signIn` calls use `redirect:false` then `router.push("/dashboard")`. Auth.js default `redirect` callback enforces same-origin: `GET /api/auth/signin?callbackUrl=https://evil.example` set the callback cookie to the app origin, not evil. `signOutSafely` callers all pass literals. No open redirect |
| Stripe return handling | allowlisted via `redirectToStripe` (F-26 fixed, see above) |
| `trustHost: true` | acceptable on Vercel (edge sets the forwarded host) |
| Token-adoption Credentials path | requires `/auth/me` to accept the token; Auth.js double-submit CSRF protects the sign-in POST, so login-CSRF is blocked |

## 6. Security headers (production, `https://bridgeleads.io/login` and `https://app.bridgeleads.io/login`)

| Header | Value | Verdict |
|---|---|---|
| Content-Security-Policy | see below | OK with W-3 caveats |
| Strict-Transport-Security | `max-age=31536000; includeSubDomains` | OK (no `preload`, optional) |
| X-Frame-Options | `DENY` | OK |
| X-Content-Type-Options | `nosniff` | OK |
| Referrer-Policy | `strict-origin-when-cross-origin` | OK |
| Permissions-Policy | `camera=(), microphone=(), geolocation=()` | OK (could add `payment=()`, `usb=()`) |
| Access-Control-Allow-Origin | `*` (Vercel edge) | W-5 |
| X-Powered-By | absent in prod (present in local `next start`) | OK |
| Cross-Origin-Opener-Policy | absent | optional hardening (`same-origin`) |

CSP directives: `default-src 'self'` | `base-uri 'self'` (confirmed added) | `object-src 'none'` | `form-action 'self'` | `script-src 'self' 'unsafe-inline' 'unsafe-eval'` | `style-src 'self' 'unsafe-inline'` | `img-src 'self' data: blob: https://bridgeleads.io` | `font-src 'self' https://fonts.gstatic.com` | `connect-src 'self' https://*.bridgeleads.io https://*.stripe.com` (Supabase confirmed removed) | `frame-src https://my.spline.design https://js.stripe.com` | `frame-ancestors 'none'`. No `report-to`.

## 7. Route handlers (`app/api/**/route.ts`)

Exactly two:
- `app/api/auth/[...nextauth]/route.ts`: re-exports Auth.js handlers. No proxying.
- `app/api/session/refresh/route.ts`: POST, calls `auth()` and returns 401 without a session, then `unstable_update({})`. Fetches only the fixed `${BASE_URL}/auth/refresh` (`lib/auth.ts:68`) with the sealed refresh token. Returns no tokens. Not CSRF-token protected, but SameSite=Lax blocks cross-site cookies and the worst same-site outcome is an extra rotation.

No open proxy, no SSRF, no arbitrary path or URL forwarding, no cookie forwarding to third parties. Zero server actions. `images.remotePatterns` still limited to `bridgeleads.io` / `app.bridgeleads.io` (the `/_next/image` probe returned 400).

## Explicit non-findings

- No secret, key, DSN, JWT or internal hostname in the client bundle, local or production.
- No publicly served source maps.
- 0 npm advisories (prod and dev).
- No reachable XSS sink; no `javascript:` href path from lead or user data.
- No open redirect (login, signout, Auth.js callback, Stripe).
- `proxy.ts` gate not bypassable by the tested classes, including CVE-2025-29927-style headers; dashboard layout is an independent second gate.
- Admin mutations are enforced server-side (`require_admin_mfa`, `require_admin`).
- No API proxy / SSRF surface in Next route handlers.
- Cookies use `__Host-` / `__Secure-` prefixes, HttpOnly, Secure, SameSite=Lax in production.

## Cross-reference for the backend audit (not a frontend finding)

`GET /scrapers/connectors` is unauthenticated (`src/api/routes/scrapers.py:367-400`) and `?include_all=true` also returns `down`/`unknown` connectors with `base_url`, `gis_endpoint`, `assessor_url` and `health_status`. The URLs are public county portals, so impact is low (P3 info disclosure of fleet health), but `include_all` is documented as "admin tooling" and is not admin-gated.
