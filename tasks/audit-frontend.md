# BridgeLeads Frontend Security Audit — `bridgeleads-web`

**Auditor:** sec-frontend
**Date:** 2026-09-16
**Repo:** `C:/Users/Windows/OneDrive - Seattle Colleges/Desktop/bridgeleads-web` — exists, fully readable
**Audited ref:** `origin/master` (production branch). FETCH_HEAD dated 2026-09-16 15:00, so drift data below is current.
**Method:** read-only. No files edited in the FE repo, no builds run, no `npm audit`, lockfile untouched.

---

## ⚠️ CHECKOUT DRIFT — read before trusting any "working tree" claim

The local FE checkout sits on **`feat/schedule-day-picker`**, which is **101 commits behind and 5 ahead of `origin/master`**.

I audited `origin/master` throughout via `git show <ref>:path` and `git grep <ref>`. Every `file:line` in this document is a **production-branch** line.

**Proof the drift mattered:** the working tree's `middleware.ts` is missing `/verify-email` and `/api/session/refresh` from `PUBLIC_ROUTES`, both of which production has. Auditing the tree would have produced two false findings — one of them a spurious "unauthenticated route exposure".

This is the same trap as the backend audit's detached-HEAD-100-behind situation. Worth a standing note in the security runbook: **check `git rev-list --left-right --count origin/<prod-branch>...HEAD` before auditing anything.**

Working tree also carries uncommitted edits to `app/(marketing)/_monopo/Footer.tsx` and `app/(marketing)/page.tsx`, plus two untracked stray files inside `app/(dashboard)/scrapers/new/` (`Untitled-1.md` and a `.txt` with "Login Screen Security" in the name). `.md`/`.txt` are not route files so they do not ship, but they do not belong inside the App Router tree.

---

# THE SIX PRIORITY ANSWERS

## 1. Where is the auth token stored?

**httpOnly Auth.js encrypted session cookie. NEVER localStorage or sessionStorage.**

| Step | file:line | Code |
|---|---|---|
| Minted by FastAPI | — | `POST /auth/login` → `access_token` + `refresh_token` |
| Sealed into Auth.js JWT | `lib/auth.ts:209-211` | `token.accessToken = accessToken;` `token.refreshToken = …` |
| Persisted | `lib/auth.ts:201-203` | `session: { strategy: "jwt", maxAge: 7*24*60*60 }` → `__Secure-authjs.session-token`. **No custom `cookies` block exists in `lib/auth.ts`**, so Auth.js defaults apply: httpOnly, Secure, SameSite=Lax. |
| Access token exposed to client JS | `lib/auth.ts:233` | `session.accessToken = token.accessToken as string;` |
| Refresh token **withheld** | `lib/auth.ts:233-236` | Session callback assigns **only** `accessToken` and `error`. `refreshToken` is never copied — deliberate, and commented at `:231-232`. |
| Read client-side | `lib/api.ts:42`, `:93`, `:362`, `:1269` | `const session = await getSession();` |
| Attached to requests | `lib/api.ts:47`, `:367`, `:1274` | `headers["Authorization"] = \`Bearer ${session.accessToken}\`` |

**Evidence for the negative claim.** I checked every `(local|session)Storage.(set|get)Item` call site on `origin/master` — all 16. None holds a credential:

- `lib/api.ts:111,133,135,193,290,343` — `bl:refresh-lock` (timestamp), `bl:signed-out-at` (timestamp)
- `components/quota-upgrade-banner.tsx:48,73` — `"1"` dismissal flag
- `app/(marketing)/_monopo/CookieBanner.tsx:16,23` — `"1"`
- `components/probate-tod-notice.tsx:41,89` — dismissal signature
- `app/(auth)/register/page.tsx:166,175` — `bl.referral.code`
- `components/shell/RecoverStaleSession.tsx:35,39` — recovery attempt counter

**Consequence:** the standard "localStorage → XSS → account takeover" writeup **does not apply to this app**. The correct, lesser framing is F-03 below.

---

## 2. What does the Auth.js/NextAuth instance actually authenticate?

**Fully wired to FastAPI. NOT vestigial. It is a server-side container for the backend's bearer — the two systems are layered, not parallel.**

Auth.js authenticates nothing on its own. It has exactly one provider — `CredentialsProvider` (`lib/auth.ts:116`) — and that provider's `authorize()` performs no local credential check. It delegates every decision to the backend:

- **Password path** — `lib/auth.ts:176-190`: `POST ${BASE_URL}/auth/login`; `if (!res.ok) return null`. Auth.js never sees a password hash.
- **Identity + claims** — `lib/auth.ts:143-148`: `GET ${BASE_URL}/auth/me` with `Authorization: Bearer`. `id`, `email`, `plan`, `is_admin` all originate backend-side. Comment at `lib/auth.ts:152-155` states it: *"is_admin … originates server-side from /auth/me and is sealed into the Auth.js-signed JWT, so it can't be forged client-side."*
- **MFA** — `lib/auth.ts:186-188`: `if (data.mfa_required || !data.access_token) return null;` The one-shot password path **refuses** MFA accounts outright. The real challenge runs against the backend in `app/(auth)/login/page.tsx` (`loginStart` → `loginVerify`), and only the resulting token is handed to Auth.js via the "token-adoption" path (`lib/auth.ts:163-173`).

Given a token it did not validate, `buildUser` calls `/auth/me`; if the backend rejects it, it returns `null` and no session is created. **There is no path to an Auth.js session that does not first satisfy FastAPI, MFA included.**

### Second session / CSRF surface: real, but carries no business authority

The complete cookie-authenticated surface on the Vercel origin is **two route handlers**. I enumerated every `app/api/**/route.ts`:

1. `app/api/auth/[...nextauth]/route.ts` — three lines, re-exports Auth.js `handlers`. Protected by Auth.js's built-in double-submit CSRF (`__Host-authjs.csrf-token`). signIn/signOut POSTs are rejected without a matching token.
2. `app/api/session/refresh/route.ts` — POST, cookie-authed, **no CSRF token check**. Calls `unstable_update({})` to rotate the bearer.

Plus a server-rendered read: `app/(dashboard)/layout.tsx:31` uses the cookie session to fetch `/auth/me`.

**There are ZERO server actions** — `git grep -l "use server"` across all `*.ts,*.tsx` returns nothing. Side effect worth noting: `next.config.ts:5-13` configures `experimental.serverActions.allowedOrigins` for a feature the app does not use. Harmless dead config.

**Assessment.** Every business mutation — create scraper, run job, create batch, export, change plan, checkout, API keys, connectors, MFA toggle — goes through `apiFetch`/`bearerFetch` to FastAPI with an `Authorization` header (`lib/api.ts:47,367,1274`). Browsers do not attach bearer headers to cross-site requests. **CSRF-immune by construction, not by accident.**

`/api/session/refresh` is the one unguarded cookie POST, and SameSite=Lax means a cross-site POST carries no cookie at all. The worst outcome of a *same-site* forgery is one extra token rotation, which grants nothing. The route documents this at `route.ts:22-25` and the reasoning is sound.

On `middleware.ts:16-18` listing `/api/session/refresh` in `PUBLIC_ROUTES` — this looks alarming and is not. The handler calls `auth()` itself and 401s (`route.ts:27-32`). The comment explains why the exception must exist: if middleware redirected it, `fetch()` would follow the 302 to `/login` and report 200, which the caller would misread as "token refreshed" on a dead session. **I verified the handler rather than trusting the comment.**

### ⚠️ ONE REAL CONSEQUENCE TO ROUTE TO `sec-auth`

The Auth.js session lives **7 days** (`lib/auth.ts:202`); backend access tokens live **1 hour**. Session lifetime is therefore governed by the frontend cookie, not the backend.

Revoking an *access* token does not end the session — only refresh-token revocation does (`refreshBackendTokens`: a 4xx → `RefreshFailed` → sign-out, `lib/auth.ts:79-87`).

**The backend team must confirm that "log out all sessions" / account-compromise response revokes REFRESH tokens.** If it only invalidates access tokens, a 7-day cookie keeps minting fresh bearers after the user believes they are logged out.

---

## 3. Does the browser talk DIRECTLY to Supabase?

**NO. Certain. `connect-src https://*.supabase.co` is a dead grant. There is no browser-held Supabase credential of any kind, so this is NOT a P0 — there is nothing for a credential to read because no credential exists.**

Given the stakes, here is the full basis for certainty rather than a single grep:

1. **Source references.** `git grep -in "supabase" origin/master` across `*.ts,*.tsx,*.json,*.mjs,*.md` returns exactly **two** hits, **neither of them code**:
   - `app/(marketing)/privacy/page.tsx:106` — the string `"Supabase: database hosting"` inside the privacy policy's subprocessor list. Prose rendered to a marketing page.
   - `next.config.ts:35` — the CSP directive itself.
2. **Dependencies.** No `@supabase/supabase-js`, and no package with `supabase` in the name, in `dependencies` **or** `devDependencies`. Without a client library there is no `createClient`, no `.from(...).select(...)`, no PostgREST call.
3. **No raw HTTP path either** — a client could in principle hit PostgREST with bare `fetch`. It does not: `git grep -E 'fetch\(\s*["\`]https?://'` over all `*.ts,*.tsx` returns **ZERO hits**. There is no hardcoded absolute-URL fetch anywhere in the client. Every outbound request goes through `apiFetch`/`bearerFetch` to `NEXT_PUBLIC_API_URL`.
4. **No credential to use even if a call existed.** No `SUPABASE_ANON_KEY`, no `NEXT_PUBLIC_SUPABASE_*` var (the complete `NEXT_PUBLIC_*` inventory is one entry — see §4), no service-role key, no Postgres DSN in any client artifact.

**Verdict:** stale configuration, not a signal. The CSP entry predates or outlived a direct-Supabase approach that is not present in the shipped code.

**FIX (F-07, P3 hygiene):** delete `https://*.supabase.co` from `connect-src` at `next.config.ts:35`. An unused wildcard-subdomain connect grant is pure exfiltration surface — it would let any injected script POST stolen data to an attacker-registered `*.supabase.co` project. Same file: verify `frame-src https://my.spline.design` is still used by the marketing hero; if the 3D hero was replaced, drop that too.

---

## 4. Any genuine secret in `NEXT_PUBLIC_*` or hardcoded in source?

**No.**

### Complete `NEXT_PUBLIC_*` inventory — exactly one variable

| Var | Referenced at | Legitimately public? |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `lib/api.ts:38` · `lib/auth.ts:6` · `app/(dashboard)/layout.tsx:8` · `app/(auth)/register/page.tsx:21` · `app/(marketing)/_monopo/pricingApi.ts:22` · `app/(marketing)/coverage/page.tsx:27` | **Yes.** An API base URL is a public fact — the browser must know it to issue requests. Textbook correct use of the prefix. **Not a finding.** |

Server-only and correctly **un**-prefixed: `API_URL` (`coverage/page.tsx:27`, server component only), `NEXTAUTH_SECRET`, `NEXTAUTH_URL`.

### Hardcoded literals

`git grep -E "(sk_live|sk_test|whsec_|pk_live|pk_test|SUPABASE_SERVICE|service_role|AKIA|BEGIN (RSA|PRIVATE)|postgres(ql)?://|redis://|re_[A-Za-z0-9]{16})"` across `*.ts,*.tsx,*.js,*.mjs,*.json,*.md` → **zero hits**.

No `.env*` file is tracked on `origin/master` (`git ls-tree -r origin/master | grep -i env` → empty). `.gitignore:34` is `.env*`; `git check-ignore -v` confirms coverage of both local env files.

**The FE ships no third-party client credential at all** — not even a Stripe publishable key. Stripe is reached purely by redirecting to backend-minted URLs, so there is no `pk_` present to mistakenly report as a finding.

### F-08 [P3] — `.env.check` holds a Vercel OIDC token

`bridgeleads-web/.env.check:2` contains `VERCEL_OIDC_TOKEN="eyJ..."`. Decoded payload: `"environment":"development"`, `"project":"bridgeleads-web"`, `"owner_id":"team_JflfEPrkanV9PMjBevRcXBHJ"`, **`"exp":1774434828` ≈ 2026-03-25 — six months expired**. Gitignored and never committed.

**The risk is the pattern, not this dead token.** Vercel CLI auto-writes this file; it sits in a **OneDrive-synced folder** (replicating to Microsoft's cloud and every linked device); and the next `vercel env pull` / `vercel dev` rewrites it with a **valid** token.

**FIX:** delete `.env.check` and treat that path as credential material going forward.

---

## 5. Real, reachable XSS sinks on county-scraped data or user-entered names?

**NONE FOUND.**

### What I grepped

All `*.ts,*.tsx,*.js,*.jsx` on `origin/master` for:
`dangerouslySetInnerHTML | innerHTML | outerHTML | document.write | eval( | new Function | srcdoc | insertAdjacentHTML | javascript:`

→ **exactly ONE hit.**

### The single sink, and why it is unreachable

`components/ui/chart.tsx:95` — stock shadcn `ChartStyle`, injecting CSS custom properties:

```js
__html: Object.entries(THEMES).map(([theme, prefix]) => `
${prefix} [data-chart=${id}] { ... --color-${key}: ${color}; ... }`)
```

**Traced to all three call sites** — `app/(dashboard)/dashboard/_components/LeadsTrendChart.tsx`, `RecordTypeMix.tsx`, `TopCountiesBars.tsx`. Every one passes a hardcoded, developer-authored `chartConfig`: static keys, static `var(--…)` color values. **No scraped record, no API field, and no user-entered string reaches `config` or `id`.** Not reachable.

### The threat-model fields specifically

County-scraped Party Name / Property Address / Mailing Address, and user-entered list/integration names, all render as JSX **text children** and are React-escaped:

- `app/(dashboard)/results/[id]/_components/LeadValues.tsx:31,58` — `<PartyName name={row.party_name} />`, `{row.property_address}`
- `app/(dashboard)/results/[id]/_components/MailingValue.tsx:23` — `{row.mailing_address}`
- `app/(dashboard)/batches/[id]/_components/BatchLeadsTable.tsx:98-108`
- `app/(dashboard)/scrapers/[id]/records/page.tsx:371-396`

### Also confirmed absent

- **No markdown or HTML renderer anywhere in the app.** No component accepts an `html` prop.
- **No `DOMPurify`** — and none is needed, since there is nothing to sanitize into.
- The four `document.createElement("a")` calls (`lib/api.ts:873,912,1072,1101`) all set `a.href` to `URL.createObjectURL(blob)` — **never an API-supplied string**. No `javascript:` href path exists.
- No `style={{...}}` interpolates API data. The only two `...props.style` spreads are `components/ui/dropdown-menu.tsx:58,66`, from internal callers.
- **No `<Script>` tags and no third-party SDK** — `git grep "next/script"` → empty.

I am deliberately not padding this section with theoretical sinks. There are none to list.

---

## 6. Open redirect — allowlisted or unvalidated?

**No open redirect exists — but by omission, not by allowlist. One Stripe URL is validated and one is not.**

### Login / post-auth: safe because the param is ignored

`middleware.ts:36` sets `loginUrl.searchParams.set("callbackUrl", req.url)`, but `app/(auth)/login/page.tsx` **never reads it**. It reads only `searchParams?.get("reset")` (line 63) and hardcodes `router.push("/dashboard")` on success (line 133). An attacker-supplied `?callbackUrl=https://evil.com` is inert.

All ~20 `router.push` / `router.replace` destinations are string literals or templates over API-issued UUIDs (`/live/${job.id}`, `/batches/${res.batch_id}`, `/results/${j.id}`). **None** is constructed from a query parameter.

### Logout: safe, all call sites pass literals

`signOutSafely(callbackUrl = "/login")` → `window.location.href = callbackUrl` (`lib/api.ts:283,315`). I checked **all six call sites**: `app/(auth)/reset-password/ResetPasswordForm.tsx:58`, `components/settings/security-tab.tsx:26,110`, `components/shell/CommandPalette.tsx:200`, `components/shell/CompleteProfile.tsx:116`, `components/shell/UserMenu.tsx:114`, `lib/api.ts:432`. Every one passes nothing or the literal `"/login?reset=success"`.

### Stripe return URLs: SPLIT — F-06 [P3]

**Validated** — `components/settings/BillingTab.tsx:86-91`:
```js
if (data.checkout_url?.startsWith("https://checkout.stripe.com/")) {
  window.location.href = data.checkout_url;
} else { toastError(null, "We couldn't open checkout. Please try again."); }
```

**NOT validated** — bare assignment, no check:
- `components/shell/UserMenu.tsx:54` — `window.location.href = res.portal_url;`
- `components/shell/CommandPalette.tsx:169` — `window.location.href = res.portal_url;`

Not exploitable today, since `portal_url` originates from our backend via Stripe. It becomes a redirect gadget only if that response is ever attacker-influenced. What makes it worth filing is the **inconsistency**: the same author identified this exact risk for checkout and guarded it, so the two unguarded sites read as oversight rather than a decision.

**FIX:** apply the same prefix check (`https://billing.stripe.com/`) at both sites — better, hoist it into one `redirectToStripe(url)` helper in `lib/api.ts` so a third call site cannot be added unguarded.

### ⚠️ Forward-looking flag

Because `callbackUrl` is **set but never read**, a logged-out user deep-linking to `/scrapers/new` always lands on `/dashboard`, losing their destination. That is a live UX papercut someone will eventually "fix".

**If anyone wires `callbackUrl` up, the allowlist must land in the same commit:** validate to a same-origin **path** (`new URL(cb, origin).origin === origin`, and reject `//`-prefixed values), never a bare string. This is precisely how open redirects get introduced later.

---

# REMAINING FINDINGS

## F-01 [P1 *if* backend does not enforce; else P3] — `/admin/connectors` gated on plan, not `is_admin`, and it submits an arbitrary `base_url`

**CLASSIFICATION:** UNVERIFIED (frontend side confirmed; server-side enforcement is the open question)

This is my top escalation and the one item I cannot close from this repo.

**EVIDENCE** — `app/(dashboard)/admin/connectors/page.tsx:48-49`:
```js
const userPlan = (session?.user as { plan?: string })?.plan ?? "starter";
const isAgency = userPlan === "agency";
```
Used at lines **129, 146, 255** to show/hide the "add connector" form. The form collects `county`, `state`, **`baseUrl`**, `selectedTypes`, and submits via `createConnector` → `lib/api.ts:966-973`:
```js
return apiFetch<ConnectorResponse>("/scrapers/connectors", { method: "POST", body: JSON.stringify(body) });
```

The sibling admin page does it **correctly** — `app/(dashboard)/admin/funnel/page.tsx:52`: `const isAdmin = session?.user?.is_admin === true;`. So `is_admin` **is** threaded through the session (`lib/auth.ts:152-156, 216, 238`) and this route simply does not use it.

**ATTACK PATH:** Client-side hiding is not authorization. Any authenticated user calls `POST /scrapers/connectors` directly with their own bearer — no devtools required, the request shape is in the generated OpenAPI types the FE itself consumes. If the backend does not require admin, **any user can register a county connector pointing at a `base_url` of their choosing**, which the scraper fleet will then fetch. That is a user-supplied URL entering the scraping engine — exactly the case `.claude/rules/security.md` non-negotiable #2 exists to cover.

**FIX (frontend):** gate line 49 on `session?.user?.is_admin === true`, matching `admin/funnel`.
**FIX (backend — this decides the severity):** confirm `POST /scrapers/connectors` requires admin **and** that `base_url` passes `validate_scraping_target()` before any navigation.

→ **ROUTE TO `sec-ssrf` / `sec-tenant`.**

---

## F-02 [P3 — defense-in-depth, NOT a vulnerability] — CSP `'unsafe-inline'` / `'unsafe-eval'`; missing `base-uri`, `object-src`, `form-action`

Assessed honestly per instruction: **I found zero reachable XSS sinks (§5), so this CSP weakness gates nothing currently exploitable. It is not a P2 — calling it one would be padding.**

**EVIDENCE** — `next.config.ts:41-55`, matching the observed production header byte-for-byte:
```
script-src 'self' 'unsafe-inline' 'unsafe-eval'
```
with no `base-uri`, `object-src`, or `form-action` directives.

**ATTACK PATH:** None today — it requires an XSS that does not exist. The value is purely as a second line of defense if one is ever introduced. Of the gaps, **missing `base-uri` is the sharpest**: an injected `<base href="https://evil.com">` repoints every relative script URL, and `script-src 'self'` does **not** stop it, because origin resolution happens *after* `<base>` is applied. Missing `form-action` lets an injected form POST credentials off-origin.

**FIX (cheap half — do now):** add `base-uri 'self'; object-src 'none'; form-action 'self'`. Three directives, zero behavioral risk, ~5 minutes.
**FIX (rest — schedule, do not block launch):** dropping `'unsafe-eval'` is probably free; verify on a preview deploy. Removing `'unsafe-inline'` requires nonce-based CSP threaded through middleware — a real project, and not justified as a blocker given zero sinks.

---

## F-03 [P3] — Access token readable by page JS via `GET /api/auth/session`

**EVIDENCE:** `lib/auth.ts:233` — `session.accessToken = token.accessToken as string;` — places the bearer in the JSON returned by `/api/auth/session`, which `lib/api.ts:42,93,362,1269` then read client-side.

**ATTACK PATH:** Script execution in the app origin → `fetch('/api/auth/session')` → bearer → full API access as the victim for ≤1 hour. **Assessed against the actual XSS exposure in §5: there is no reachable sink to start this chain.** The blast radius is also deliberately bounded — the **refresh token is withheld** from the session object (`lib/auth.ts:233-236`, explicitly commented), so an attacker gets one hour and cannot mint more. That is a designed mitigation, not luck.

**FIX:** No urgent action. The real controls are keeping the sink count at zero and tightening CSP (F-02). Eliminating it properly means proxying all API calls through Next route handlers so the bearer never crosses into the browser — a large refactor I would **not** prioritize over F-01 or F-02.

---

## F-04 [P3] — `Access-Control-Allow-Origin: *` on HTML responses

**EVIDENCE:** This header is **not set anywhere in this repo.** `next.config.ts` `headers()` does not emit it; there is no `vercel.json`, no `_headers` file; `git grep -i "Access-Control-Allow-Origin"` over source returns nothing. **It is injected by the Vercel edge** — so remediation is a hosting-config change, outside this repo's control surface.

**ATTACK PATH — why it does not work:** The CORS spec forbids `Access-Control-Allow-Origin: *` from being honored on a credentialed request. A `fetch(..., {credentials:'include'})` from `evil.com` against a wildcard ACAO is **rejected by the browser before the response reaches script**, and there is no `Access-Control-Allow-Credentials: true`. So:

- Attacker reads *unauthenticated* marketing HTML → already public, obtainable server-side anyway. No gain.
- Attacker targets a *dashboard* page → the request goes out uncredentialed → the server sees no session → `middleware.ts:34-38` redirects to `/login`. They read a login page, not the victim's leads.

Consistent with the backend audit's C-01 (API-side CORS correctly rejects attacker origins). This wildcard is on the static/HTML tier only.

**FIX:** Remove at the Vercel project level for hygiene, so a future JSON route cannot inherit it. Not urgent, not a launch blocker.

---

## F-05 [SECURE] — Security headers and image `remotePatterns`

**EVIDENCE** — `next.config.ts:43-55` sets, on `/(.*)`: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`, `Strict-Transport-Security: max-age=31536000; includeSubDomains`, plus CSP with `frame-ancestors 'none'`.

**`images.remotePatterns` (`next.config.ts:15-26`) is tightly scoped** to `https://bridgeleads.io` and `https://app.bridgeleads.io` — no wildcard, no `**`. The Next image optimizer **cannot be abused as an open fetch proxy**. This is the single most commonly botched Next.js security setting and it is correct here.

The `isDev` guard on `connect-src` (`next.config.ts:31-38`) is also correctly written — `localhost:8000` is appended only when `NODE_ENV !== "production"`, which the observed production CSP confirms.

**CROSS-REFERENCE:** the FE host **does** send HSTS with `includeSubDomains` — that is what partially compensates for its absence on `api.bridgeleads.io` (backend finding F-04), for browsers that visit the apex first.

---

## F-06 [P3] — Stripe `portal_url` unvalidated

See §6 above. `checkout_url` is validated (`BillingTab.tsx:86-91`); `portal_url` is not (`UserMenu.tsx:54`, `CommandPalette.tsx:169`).

---

## F-07 [P3] — Dead `https://*.supabase.co` grant in `connect-src`

See §3 above. `next.config.ts:35`. Delete it.

---

## F-08 [P3] — `.env.check` Vercel OIDC token

See §4 above.

---

## F-09 [P3] — Dependencies (KNOWLEDGE-BASED, **NOT** A SCAN)

I did **not** run `npm audit`, per the read-only constraint. **This is recall against a May 2026 knowledge cutoff — anything published after that is invisible to me. Treat as a starting point, not a clearance.**

| Package | Version | Runtime/Dev | Assessment |
|---|---|---|---|
| **`next`** | **16.1.7** (`package-lock.json:8552`) | runtime | **Not affected by the middleware-auth-bypass class.** CVE-2025-29927 (`x-middleware-subrequest` header bypass) was fixed in 14.2.25 / 15.2.3; 16.1.7 is far past it. **This matters specifically here** — `middleware.ts` *is* this app's auth gate, so that CVE class would have been directly exploitable. Also past CVE-2025-57822 (middleware SSRF) and the image-optimizer advisories. No known issue at this version as of cutoff. |
| `next-auth` | 5.0.0-beta.30 | runtime | No known CVE. But **beta software carrying production authentication** is a standing risk, and the app uses `unstable_update` (`app/api/session/refresh/route.ts:35`) — explicitly unstable API. Beta→beta bumps can change session semantics with no major-version signal. Lockfile-pinned (good); read the changelog before any bump. |
| `react` / `react-dom` | 19.2.3 | runtime | Current, no known issue. |
| `zod` | ^4.3.6 | runtime | Current major, no known issue. |
| `remotion`, `@remotion/*` | ^4.0.441 | **listed as runtime** | Video-authoring tooling in `dependencies`, not `devDependencies` — inflating the production tree and attack surface if only used to build marketing assets offline. |
| `shadcn` | ^4.0.8 | **listed as runtime** | A CLI scaffolding tool. Almost certainly belongs in `devDependencies`. |

`package.json` uses caret ranges throughout, but the lockfile is committed — fine **provided CI installs with `npm ci`, not `npm install`**. Worth confirming in the deploy audit → `sec-deploy`.

**FIX:** run `npm audit --omit=dev` for an authoritative answer; move `remotion`/`@remotion/*`/`shadcn` to devDependencies.

---

## F-10 [SECURE] — No direct-to-DB or direct-to-third-party browser calls

`git grep -E 'fetch\(\s*["\`]https?://'` over all `*.ts,*.tsx` → **zero hits**. Every outbound call routes through `apiFetch`/`bearerFetch` to `NEXT_PUBLIC_API_URL`. No Supabase client, no Stripe.js, no direct Tracerfy/Resend/R2 access from the browser.

**And there is no analytics or error-reporting SDK at all** — `git grep "next/script"` → empty; no PostHog, Sentry, GTM, Segment, Mixpanel, Hotjar, Clarity, Plausible, or `@vercel/analytics`. **The token therefore cannot leak to a third party, because there is no third party.**

The only `console.*` calls in the app are three `console.error(error)` in the error boundaries (`app/(auth)/error.tsx:20`, `app/(dashboard)/error.tsx:22`, `app/(marketing)/error.tsx:21`) — logging the React error object, not the session. The token never enters a URL either: `lib/api.ts:1085` comments and implements *"no token in query string"*, with downloads going through `bearerFetch` + a `blob:` URL.

---

## F-11 [N/A] — Source maps not emitted to production

`productionBrowserSourceMaps` is absent from `next.config.ts`; Next defaults it to `false`. Even if enabled, impact would be low: no secrets exist in client code, and the API surface is already published as the OpenAPI schema the FE generates its types from. Nothing to fix.

---

# CLIENT-GATED FEATURES NEEDING SERVER-SIDE CONFIRMATION

Gated in the browser only. **Frontend hiding is not authorization** — this list exists so the backend audit can confirm enforcement, not to call the hiding itself a vulnerability.

`lib/entitlements.ts` documents which gates it *believes* are server-authoritative. **Verify, do not trust** — its own comments record that it was once wrong about exactly this (*"This block previously said the flag was off … That was true when written and is no longer."*).

| # | Capability | Client gate (file:line) | FE claims server enforcement? | Backend must confirm | Route to |
|---|---|---|---|---|---|
| 1 | **Create connector w/ arbitrary `base_url`** | `plan === "agency"` — `admin/connectors/page.tsx:49` — **not `is_admin`** | ❌ no claim | **Requires admin AND `base_url` → `validate_scraping_target()`** | `sec-ssrf` **P1** |
| 2 | Activation-funnel admin dashboard | `is_admin === true` — `admin/funnel/page.tsx:52,69` | ✅ "backend independently enforces admin" | `GET /billing/activation-funnel` requires admin | `sec-auth` |
| 3 | Record types by plan | `canUseRecordType()` — `entitlements.ts:71-79` (probate=starter; pre_foreclosure/tax_delinquent/trustee_sale=pro; code_violation/divorce/death_certificate=business) | ✅ "enforced when `ENTITLEMENT_ENFORCEMENT` on; flag IS ON in prod (verified 2026-07-30)" | **Re-verify the flag is still on** in api *and* worker Railway services | `sec-billing` |
| 4 | Distinct-county cap | `countyCap()` — `entitlements.ts:82-84` (1/3/10/∞) | ✅ same flag | Cap enforced on config create **and** update | `sec-billing` |
| 5 | Batch scrape | `canBatch()` ≥ pro — `entitlements.ts:87-89` | ✅ "ALWAYS, independent of flag" | `POST /batches` 402s below pro | `sec-billing` |
| 6 | Per-config webhook delivery | `canUseWebhook()` ≥ business — `entitlements.ts:92-94` | ✅ "ALWAYS" | Webhook field rejected below business | `sec-billing` |
| 7 | Skip-trace add-on | `canSkipTracePlan()` ≥ pro | ✅ "ALWAYS" | Skip-trace endpoints 402 below pro | `sec-billing` |
| 8 | Lead overlap / segments | `canUseOverlap()` ≥ business — `entitlements.ts:102-104` | ✅ "router-level dependency returning structured 402" | `/segments/*` incl. `/intersection/export`, `/union/export` | `sec-export` |
| 9 | Export formats | `canUseExportFormat()` — `entitlements.ts:108-119` (starter csv; pro +excel/xlsx; business +json) | ✅ mirrors `EXPORT_FORMATS_BY_PLAN` | Format rejected server-side. **The `xlsx`/`excel` alias must move together** or the same file is offered under one name and refused under the other | `sec-export` |
| 10 | Schedule frequencies | `canUseFrequency()` — `entitlements.ts:128-138` | ✅ mirrors constants | Frequency rejected server-side | `sec-billing` |
| 11 | Programmatic API keys | `canUseApiKeyPlan()` ≥ business | ❌ no claim | API-key issuance 402s below business | `sec-auth` |
| 12 | **Quota / frozen-account blocking** | `isBlocked = isFrozen \|\| isOverLimit` hides search + table — `scrapers/[id]/records/page.tsx:82,205,236` | ❌ no claim | **The rows must not be RETURNED** when over-quota/frozen | `sec-tenant` **flagged** |

**Item 12 deserves a real request, not a code read.** The FE hides the table but the query still ran. If the backend serves rows to a frozen or over-limit account and the FE merely declines to paint them, the quota gate is cosmetic and the leads are sitting in the network tab. Given the prior `1001/50` quota incident, check the network response, not the UI.

**GOOD PROPERTY TO PRESERVE:** `lib/entitlements.ts` **fails closed**. `rank()` (`:63-65`) maps any unknown plan string to `starter`, and `canUseRecordType()` (`:71-79`) returns `false` outright for any record type absent from the matrix — including `eviction`, which has no live connector. A corrupted, forged, or newly-added plan value **over-grants nothing**. Do not let a refactor lose this.

---

# SUMMARY

| Severity | Count | Items |
|---|---|---|
| **P1 (conditional)** | 1 | **F-01** `/admin/connectors` gated on plan not `is_admin`, submits arbitrary `base_url` — P1 **only if** the backend does not enforce admin. Backend must settle. |
| **P3** | 7 | F-02 CSP (defense-in-depth) · F-03 access token via session endpoint · F-04 ACAO `*` (Vercel-level, unexploitable) · F-06 `portal_url` unvalidated · F-07 dead Supabase CSP grant · F-08 expired OIDC token on OneDrive · F-09 dependencies (unscanned) |
| **Secure** | 6 | No reachable XSS sink · No open redirect · Headers + tight `remotePatterns` · No third-party/DB browser calls · httpOnly cookie token storage · Session-refresh design |
| **N/A** | 1 | F-11 source maps |

**The frontend is the strongest layer audited.** No secrets in client artifacts, no XSS sink, no open redirect, no direct DB access, no analytics exfil path, correct token storage, tightly-scoped image optimizer, fail-closed entitlements.

**Frontend is NOT a launch blocker on its own.** The single blocking question is F-01, and it is a *backend* question.

Two things worth doing this week regardless, both trivial:
1. Add `base-uri 'self'; object-src 'none'; form-action 'self'` to the CSP (F-02) — ~5 minutes, zero risk.
2. Delete the dead `https://*.supabase.co` grant (F-07).

---

# CAVEATS — stated plainly

- **I did not run `npm audit` or any scanner.** F-09 is recall against a May 2026 cutoff. Newer advisories are invisible to me.
- **I did not execute the app or inspect a built bundle.** "No secrets in client artifacts" is a **source-level** conclusion — strong, but not build-output verification.
- **F-01's severity genuinely depends on backend behavior I could not observe from this repo.** I classified it UNVERIFIED rather than guessing at the outcome.
- All findings are against `origin/master` as of 2026-09-16 15:00. The local checkout is 101 commits behind that and was **not** the audit subject.
