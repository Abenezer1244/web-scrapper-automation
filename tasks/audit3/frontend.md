# Audit 3, leaf 1.6: Frontend (bridgeleads-web)

- Date: 2026-09-27
- Frontend audited: `origin/master` `6030491` (Merge PR #164 perf/session-token-cache), fresh worktree `C:/Users/Windows/bl-web-audit3-fe` (detached; `node_modules/` and `.next/` are gitignored build output). The shared OneDrive checkout was not touched.
- Backend cross-reference: `C:/Users/Windows/bl-wt-secaudit3` at `786efcf0`. Backend authorization is covered by leaf 1.3, `tasks/audit3/authz-matrix.md`, which this report cites instead of redoing.
- Prior round: `C:/Users/Windows/bl-wt-secaudit2/tasks/audit2-frontend.md` (W-1 to W-6, FE ref `8332673`). Changes since then: `git:8332673..6030491` touches 22 files (sign-out now revokes the backend session in `lib/auth.ts:113-145`, password re-auth before API key mint and MFA setup in `lib/api.ts`, a 30 s client session cache, ResultsTable contact columns). Every prior item is re-measured below.
- Stack as built: Next.js 16.3.5 (Turbopack, `proxy.ts`), next-auth 5.0.0-beta.32, React 19.2.3.

## Check 14: production build and bundle secret scan

Method:
1. `cmd:npm ci` (exit 0, 849 packages), then `cmd:AUTH_SECRET=<placeholder> NEXT_PUBLIC_API_URL=http://127.0.0.1:8013 npx next build` (exit 0, TypeScript clean, 21 routes). Only placeholder values were used; no `.env` file exists in the worktree and none was copied.
2. Regex scan (script `C:/Users/Windows/bl-web-audit3-fe-scratch/scan.py`) for: Stripe `sk_`/`rk_`/`pk_`/`whsec_`, JWT `eyJ..eyJ..`, `postgres://`, `redis://`, Resend `re_`, AWS `AKIA/ASIA`, PEM private-key headers, GitHub/Slack/Google tokens, Sentry DSN, `*.supabase.co`, `service_role`, `*.up.railway.app` / `railway.internal`, `r2.cloudflarestorage.com`, secret env-var names (`AUTH_SECRET`, `NEXTAUTH_SECRET`, `SECRET_KEY`, `STRIPE_SECRET_KEY`, `RESEND_API_KEY`, `DATABASE_URL`, `TRACERFY`), the placeholder secret value itself, generic `key/secret/token/password = "<16+ chars>"`, `NEXT_PUBLIC_*`, and bare 40-64 hex strings.
3. Live: `curl https://bridgeleads.io/login`, extracted the 21 `/_next/static/immutable/chunks/*.js` it references, fetched each once at 1.1 s spacing (all 200, 1.40 MB), plus 2 `.js.map` probes. Total production requests for this leaf: 24, all unauthenticated GET.

| Scope | Files / bytes | Hits | Classification |
|---|---|---|---|
| Local `.next/static/**` (what the browser receives) | 63 files, 2.87 MB | 0 on every pattern | No credential, DSN, JWT, internal host, env-var name or `NEXT_PUBLIC_*` literal. The inlined API base is the only config value. |
| Local `.next/server/**` (server only, not web-served) | 622 files, 24.5 MB | PEM header x3; `AUTH_SECRET`/`NEXTAUTH_SECRET` names x44; `NEXT_PUBLIC_API_URL` name x7; 40-hex x38 | All benign: the PEM header string is inside the `jose` library's `importPKCS8` validation code (in `.js.map` files); env-var NAMES are Auth.js reading `process.env.AUTH_SECRET` (no value); the 40-hex strings are git commit hashes inside GitHub URLs in library comments (every one of the 38 contexts contains `github.com`). |
| Placeholder `AUTH_SECRET` value | whole `.next` | 1 file | Only `.next/cache/turbopack/*.sst` (local build cache, never deployed; Vercel builds its own). 0 in `static` and 0 in `server`. |
| Next per-build secrets | `.next/prerender-manifest.json` (`previewModeId`, `previewModeSigningKey`, `previewModeEncryptionKey`), `.next/server/server-reference-manifest.json` (`encryptionKey`) | present server-side | Real server secrets by nature, generated randomly per build, server-only. `grep -rl` over `.next/static` = 0 files. Not exposed. |
| Live 21 chunks from `https://bridgeleads.io/login` | 21 files + HTML, 1.40 MB | 0 on every pattern | Only absolute hosts: `api.bridgeleads.io` (intended-public API base), `checkout.stripe.com` and `billing.stripe.com` (redirect allowlist strings, not keys), `errors.authjs.dev`, `vercel.live`, `nextjs.org`, `react.dev`, schema/w3 URLs. |
| Live source maps | 2 probes | 404, 404 | No public source maps; 0 `sourceMappingURL` comments in the 21 chunks, 0 `.map` under local `.next/static`. |

Key-shaped strings meant to be public: exactly one configured value, `NEXT_PUBLIC_API_URL` (7 references: `lib/api.ts:39`, `lib/auth.ts:6`, `app/(dashboard)/layout.tsx:8`, `app/(auth)/register/page.tsx:21`, `app/(marketing)/_monopo/pricingApi.ts:22`, `app/(marketing)/coverage/page.tsx:27`), which is the public API origin. There is no Stripe publishable key and no analytics id in the bundle (Stripe is reached only by top-level redirect to backend-minted URLs). Real server secrets found in the client bundle: none, local or live.

Dependency advisories: `cmd:npm audit --omit=dev` = 0 total, `cmd:npm audit` (incl. dev) = 0 total.

## XSS

Sink inventory (`cmd:grep -rnE "dangerouslySetInnerHTML|innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function|srcdoc|createContextualFragment|DOMParser" app components lib hooks proxy.ts remotion`): one hit, `components/ui/chart.tsx:95` (shadcn `ChartStyle` writes a `<style>` from the chart `config`). All four callers pass hardcoded configs (`LeadsTrendChart.tsx:73`, `TopCountiesBars.tsx:48`, `RecordTypeMix.tsx:54` `{}`); the `config=` props in `ScrapersTable.tsx:477,514` are a ScraperConfig passed to a card component, not a chart. No API or scraped value reaches it. No markdown or HTML renderer is installed (`package.json` deps), no `next/script`, no `<script>` or JSON-LD in `app/` or `components/`.

| Surface (data origin) | Where rendered | Mechanism | React escaping covers it? |
|---|---|---|---|
| Party names (scraped) | `components/ui/party-name.tsx` (span text and popover `<p>`), ResultsTable, LeadCards, BatchLeadsTable, SegmentCards, records page | JSX text child | Yes. Measured in browser (below). |
| Property / mailing addresses, heirs, legal description, doc type, parcel id, enrichment fields (scraped) | `results/[id]/_components/*`, `LeadValues.tsx`, `scrapers/[id]/records/page.tsx` | JSX text, `title=` attribute (`ResultsTable.tsx:454`, `LeadCards.tsx:200`) | Yes. Attributes are set as properties, not parsed as HTML. |
| Emails and phones (skip trace) | `EmailCell.tsx:49` `href={mailto:${em}}`, `PhoneCell.tsx:59` and `SegmentCards.tsx:63` `tel:${dialablePhone(...)}`, `SegmentCards.tsx:80` mailto | fixed scheme prefix in a template literal | Yes. The scheme cannot become `javascript:`; `dialablePhone` strips to `[\d+]` (`results/[id]/_lib.ts:104-105`). A hostile email can at most add `?subject=` style mailto params. |
| Scraper names, list/batch names, county/source values | ScrapersTable (`title={config.name}` at `:254`), scrapers page, BatchScraperCard, results index, dashboard charts (county names as recharts tick text) | JSX text / SVG text | Yes. |
| Webhook / dialer URLs (user-entered) | `/deliver` and edit form | JSX text / input `value` | Yes. Never rendered as a link. |
| Live Run log lines (SSE) | `components/log-stream.tsx:187` `{log.message}`; level colour from a fixed switch `:31-42` | JSX text; `hooks/use-log-stream.ts:95-98` type-checks each event | Yes. |
| API error messages | `lib/errors.ts` `toastError` -> sonner text; `admin/connectors` `setError(err.message)` as JSX text | text only; `lib/errors.ts:27-41` also drops messages matching an HTML-markup pattern | Yes (and the filter replaced the hostile detail with generic copy, measured). |
| Notification text | `components/shell/NotificationsBell.tsx` (type -> fixed labels; routing via `ROUTE_BY_TYPE` map at `:98`) | JSX text | Yes. |
| Onboarding CTA route (backend) | `components/onboarding-banner.tsx:133` `href={next_action.route}` | next/link href from API | Backend builds it only from `frontend_routes` constants and a job UUID (`src/api/routes/auth_helpers/session.py:66-100`). Even when forced to `javascript:...` by the stub, React 19 replaced it with `javascript:throw new Error('React has blocked a javascript: URL ...')` (measured). |
| Stripe redirects | `lib/api.ts:1430-1443` `redirectToStripe`; `BillingTab.tsx:86,170` inline prefix checks | allowlisted `https://checkout.stripe.com/` / `https://billing.stripe.com/` | Not an XSS or open-redirect path. |
| Internal links built from ids | `results/page.tsx:95,116`, `scrapers/page.tsx:455`, `BatchScraperCard.tsx:268`, `results/[id]/page.tsx:707`, `DeliveryProvenance.tsx:86` (encoded) | fixed `/results/`, `/batches/`, `/scrapers/` prefix | Yes; a relative path prefix cannot yield a script URL. |
| CSV / file downloads | `lib/api.ts:960-983`, `:995-1020`, `:1166-1183`, `:1196-1229` | `res.blob()` -> `URL.createObjectURL` -> `<a download=...>` click, URL revoked | The file is saved, not rendered. The stub served the CSV with `Content-Type: text/html` to test this; the code path forces a download via the `download` attribute (read in code, not clicked, see Not verified). No CSV preview component exists. |
| Open redirect | `proxy.ts:55` sets `callbackUrl`; `app/(auth)/login/page.tsx` never reads it (only `reset`, `:61`); `signOutSafely` callers pass literals (`lib/api.ts:366`) | n/a | No open redirect found. |

Conclusion from code plus the browser run: no reachable XSS sink. Negative control: every free-text string field of every API response the dashboard consumes was replaced with a multi-context payload (see Browser verification) and nothing executed.

## Client-only gates

Every frontend gate below is UI-only; enforcement must be server-side. The backend side for each was measured by leaf 1.3 (`tasks/audit3/authz-matrix.md`, section "Check 15: client-side checks and their server enforcement", plus "Plan entitlement" and "Quota"). Summary with the route that must enforce it:

| FE gate (file:line) | What it gates | Backend route that must enforce | Leaf 1.3 result |
|---|---|---|---|
| `lib/entitlements.ts:73` canUseRecordType; `scrapers/new/_steps/CountyStep.tsx:369,679`; `scrapers/new/page.tsx:254,263` | record types per plan | `POST/PATCH /scrapers`, `POST /batches`, `POST /jobs`, worker | enforced (flag `ENTITLEMENT_ENFORCEMENT`, documented true in prod) |
| `lib/entitlements.ts:84` countyCap | county count | `POST /scrapers`, `POST /batches` | enforced under advisory lock |
| `lib/entitlements.ts:89` canBatch; `CountyStep.tsx:207` | batch scrape | `POST /batches` | enforced |
| `lib/entitlements.ts:94` canUseWebhook; `DeliveryStep.tsx:530-619` | webhook and dialer | `POST/PATCH /scrapers`; dialer replay route | enforced; replay gap AZ-4 (P3) |
| `lib/entitlements.ts:104` canUseOverlap; `segments/page.tsx:94`; `lib/nav.ts:25` minPlan | Lists / segments | `/segments/*` | enforced |
| `lib/entitlements.ts:118` canUseExportFormat; `DeliveryStep.tsx:302` | export formats | `POST/PATCH /scrapers`, `POST /batches` | enforced |
| `lib/entitlements.ts:139` canUseFrequency; `ScheduleStep.tsx:187` | schedule frequency | `POST/PATCH /scrapers`, `POST /batches` | enforced at save |
| `lib/entitlements.ts:151` canUseApiKeyPlan; `components/settings/ApiKeysTab.tsx:58` | API keys | `POST /auth/api-key` and every API-key request | enforced |
| `lib/entitlements.ts:156` canSkipTracePlan; `scrapers/new/page.tsx:110,296`; `scrapers/[id]/edit/page.tsx:46` | skip trace | `POST/PATCH /scrapers`, `POST /batches`, worker enrich | enforced for Starter; trial Pro not capped, AZ-2 (P1, backend) |
| `admin/funnel/page.tsx:52` `is_admin` | funnel page | `GET /billing/activation-funnel` (`require_admin`) | enforced |
| `admin/connectors/page.tsx:49` and `lib/nav.ts:28,35` `plan === "agency"` | Counties admin page and create form | `POST /scrapers/connectors` (`require_admin_mfa`), `GET ?include_all=true` (`require_admin`) | enforced server-side; FE gate mismatch is AZ-7 / FE-5 (INFO) |
| `scrapers/[id]/records/page.tsx:76-82` isFrozen / isOverLimit banner | shows a blocked banner, query still runs | `GET /scrapers/{id}/records` | GAP, AZ-1 (P3, backend) |
| `dashboard/page.tsx:167`, `UsageBadge.tsx`, `quota-upgrade-banner.tsx` | quota display | `POST /jobs`, `POST /batches`, worker reservation | enforced |
| `proxy.ts:31-59` session gate; `app/(dashboard)/layout.tsx:36` second gate | page routing | every API route authenticates itself | FE gate not relied on |

Frontend-side checks performed here (not in leaf 1.3): `lib/entitlements.ts` still fails closed (unknown plan -> starter); the session `plan` and `is_admin` come only from `/auth/me` via the server-side `authorize` (`lib/auth.ts:167-185`) and are sealed in the Auth.js JWE, so they cannot be edited in the browser; even if they were, the stub run showed the UI simply calls the same backend routes. `proxy.ts` gate re-probed on the local production build without a session: `/dashboard`, `/admin/funnel`, `/results/abc`, `/%64ashboard` -> 307 to `/login`; `x-middleware-subrequest: proxy:proxy:proxy:proxy:proxy` -> 307; `POST /api/session/refresh` -> 401.

## Browser verification

Setup (all local, nothing sent to production):
- Production build from Check 14 served by `next start -p 3013 -H 127.0.0.1` with the placeholder `AUTH_SECRET`.
- A hostile API stub at `127.0.0.1:8013` (`C:/Users/Windows/bl-web-audit3-fe-scratch/stub.py`) that answers every GET the dashboard makes by generating the 200-response shape from the backend's own `schema/openapi.json` (64 paths), hand-shaping the untyped ones (`/auth/me`, `/auth/onboarding`, `/billing/usage`, `/billing/plans`, `/scrapers`, `/jobs`), and filling every free-text string with `<img src=x onerror="window.__xss=...">"><svg onload="window.__xss=..."></svg></style></script><script>window.__xss=...</script>`; URL-named fields (`*_url`, `route`, `link`) got `javascript:window.__xss=...`. SSE `/jobs/{id}/logs` streamed 3 log events with that payload as `message`. POSTs other than login returned 400 with the payload as `detail` (toast path). Downloads were served as `text/html`.
- A full local backend on `bl_audit3_fe_test` / Redis db 13 was not used: the stub gives stronger coverage because it puts the payload into every field at once, including fields the backend would normalise. This is stated again under Not verified.
- Headless Chromium via Playwright (`C:/Users/Windows/bl-rescat-venv/Scripts/python.exe`, script `C:/Users/Windows/bl-web-audit3-fe-scratch/drive.py`), real login through the `/login` form (browser `POST /auth/login` to the stub, then the Auth.js token-adoption path, which server-side calls the stub's `/auth/me`). `bypass_csp=True` was set only so the page could reach the 127.0.0.1 stub (prod `connect-src` allows only `*.bridgeleads.io`). This does not weaken the test: the production CSP already allows `'unsafe-inline'` scripts, so it would not have blocked an inline-handler XSS either.
- Detection: an init script replaced `window.alert`, recorded any `window.__xss` push, and ran a MutationObserver flagging any inserted `<img src="x">`, `<svg onload>`, or `<script>` containing `__xss` (this catches raw HTML insertion even if a handler did not fire). A dialog listener was also attached. Rendered-as-text occurrences of `<img src=x onerror` in `document.body.innerText` were counted as the positive control that the payload actually reached the screen.

Pages and interactions exercised (20 routes plus interactions): `/dashboard`, `/scrapers`, `/results`, `/results/{id}` and `?view=delivered` (every row "details" toggle expanded, "Show full name" popover opened), `/live/{running id}` and `/live/{done id}` (SSE log lines), `/batches/{id}`, `/scrapers/{id}/records` (party-name popover), `/scrapers/{id}/edit`, `/scrapers/new`, `/deliver`, `/segments` (built an intersection and a union list from hostile rows), `/settings` and its billing, api, referrals, notifications tabs, `/admin/connectors`, `/admin/funnel`, the notifications bell on every page, and a Run click on `/scrapers` returning a hostile 400.

Results (`cmd:python drive.py`):
- `window.__xss` stayed `[]` on every page and after every interaction; 0 dialogs; the MutationObserver recorded 0 raw-HTML insertions.
- Positive control: the payload rendered as visible text on the pages that show those fields, for example 16 text occurrences on `/results/{id}`, 25 after expanding rows and opening the name popover, 11 on `/scrapers`, 11 on `/scrapers/{id}/records`, 12 on `/segments` after Build list, 6 on `/live/{running id}` (log lines), 6 on `/admin/connectors`, 2 on `/deliver`.
- Onboarding CTA with `route = javascript:...`: the rendered `href` was React 19's `javascript:throw new Error('React has blocked a javascript: URL as a security precaution.')`; nothing ran.
- Toast: the hostile 400 `detail` on Run was replaced by "Couldn't start the run. Please try again." (`lib/errors.ts` markup filter); rendered as text regardless.
- Prior W-2 (route-param path traversal) re-measured: navigating to `/live/..%2f..%2fauth%2fme` and `/results/..%2fscrapers%2f<uuid>` made the browser request `GET /jobs/..%2F..%2Fauth%2Fme`, `GET /jobs/..%2F..%2Fauth%2Fme/logs`, `GET /jobs/..%2Fscrapers%2F<uuid>` and `.../results` (stub request log). The `%2F` stayed percent-encoded (Next 16.3.5 hands the page the raw segment), so no dot-segment normalisation happened and the request stayed under `/jobs/`. On the backend the decoded path `/jobs/../../auth/me` cannot match `/jobs/{job_id}` (Starlette `str` convertor excludes `/`). Not exploitable on this build; see FE-6.
- Session exposure re-measured: `fetch('/api/auth/session')` from page JS returned `accessToken` (value starts `eyJhbG`, redacted) plus `user.plan` and `user.is_admin`; no `refreshToken` anywhere in the JSON. Cookies `authjs.session-token`, `authjs.csrf-token`, `authjs.callback-url` are HttpOnly, SameSite=Lax (local http, so no `__Secure-` prefix here; prod prefixes were measured in audit 2).

## Prior items re-verified (audit 2, FE `8332673` -> `6030491`)

| Prior | Status now | Evidence |
|---|---|---|
| W-1 admin nav and connectors page gated on plan, not `is_admin` | OPEN (INFO; backend enforces) | `app/(dashboard)/admin/connectors/page.tsx:49`, `lib/nav.ts:28,35`; leaf 1.3 AZ-7 reproduced Agency non-admin gets 404 on admin calls. FE-5. |
| W-2 unencoded route params in API paths | CHANGED: not exploitable as described | 13 unencoded interpolations remain (`lib/api.ts:929,953,961,989,999,1040,1055,1104,1108,1143,1207,1245,1403`), but the browser run shows `%2F` is not decoded into a slash. FE-6. |
| W-3 CSP `unsafe-inline`/`unsafe-eval`, unused `frame-src`, broad `connect-src` | OPEN | `next.config.ts:41,78,83`; live header on `https://bridgeleads.io/login` identical. FE-1. |
| W-4 access token readable by page JS | OPEN (reproduced) | `lib/auth.ts:265`; browser fetch of `/api/auth/session` returned `accessToken`. FE-2. |
| W-5 `Access-Control-Allow-Origin: *` on HTML | OPEN | live `curl -D` of `https://bridgeleads.io/login`: `Access-Control-Allow-Origin: *`, no `Allow-Credentials`. FE-3. |
| W-6 `serverActions.allowedOrigins` includes localhost | OPEN | `next.config.ts:4-14`; `grep -rn "use server"` = 0, so it guards nothing today. FE-4. |
| F-06/F-26 Stripe portal URL unvalidated | FIXED (still) | `lib/api.ts:1430-1443` `redirectToStripe`; `BillingTab.tsx:86,170` prefix checks. |

## Findings

| ID | Severity | Category | Component | Evidence | Prereqs | Impact | Remediation | Regression test | Status |
|---|---|---|---|---|---|---|---|---|---|
| FE-1 | P3 | CSP hardening | next.config.ts CSP | next.config.ts:78 script-src 'self' 'unsafe-inline' 'unsafe-eval'; :83 frame-src my.spline.design and js.stripe.com (no reference in app, components or lib); :41 connect-src https://*.bridgeleads.io https://*.stripe.com. live:curl -D of https://bridgeleads.io/login shows the same policy | An XSS sink, none found (Browser verification) | Defence in depth only: an injected inline script or handler would not be blocked; the wildcard subdomain grant would allow exfiltration to any dangling bridgeleads.io subdomain | Set frame-src 'none', drop https://*.stripe.com, narrow connect-src to https://api.bridgeleads.io; test dropping 'unsafe-eval' on a preview; later a nonce CSP via proxy.ts to drop 'unsafe-inline' | CI step fetching the preview deploy CSP and asserting no unsafe-eval, no stripe, no spline | CONFIRMED |
| FE-2 | P3 | Token exposure | Auth.js session callback | lib/auth.ts:265 session.accessToken = token.accessToken; cmd:python sess.py (headless) fetch('/api/auth/session') returned accessToken (redacted eyJhbG...) and no refreshToken | Script execution on the app origin (none found) | An XSS would yield a backend bearer valid up to 1 hour; no refresh capability; the new sign-out revokes the family (lib/auth.ts:121-138) | Longer term, a same-origin API proxy so the bearer never reaches page JS; accepted design today | Assert /api/auth/session JSON never contains refreshToken; assert accessToken absence once a proxy exists | REPRODUCED |
| FE-3 | P3 | Header hygiene | Vercel edge response headers | live:curl -D https://bridgeleads.io/login returned Access-Control-Allow-Origin: * with no Access-Control-Allow-Credentials; not set anywhere in the repo | None | None measurable: credentialed cross-origin reads are refused by browsers, uncredentialed reads get public HTML or the login redirect | Remove the wildcard at the Vercel project level | Header check in the deploy smoke test | CONFIRMED |
| FE-4 | P3 | Config hygiene | next.config.ts serverActions | next.config.ts:4-14 allowedOrigins includes localhost:3000 and localhost:3005 in the production config; cmd:grep -rn "use server" app components lib = 0 | A future server action | None today; a future server action would accept cross-origin POSTs whose Origin is localhost | Delete the block or make the localhost entries dev-only | Grep gate: no localhost origins in the production config | CONFIRMED |
| FE-5 | INFO | Client-only gate mismatch | Admin nav and Counties page | app/(dashboard)/admin/connectors/page.tsx:49 and lib/nav.ts:28,35 gate on plan agency, not is_admin; server gates on is_admin (see authz-matrix.md AZ-7, reproduced 404 for Agency non-admin) | Agency customer | UI only: Agency customers see an Admin group whose calls 404; a non-Agency admin cannot reach the pages from the nav. No data exposure | Gate both on session.user.is_admin like admin/funnel/page.tsx:52 | Nav test with plan agency and is_admin false shows no Admin group | CONFIRMED |
| FE-6 | INFO | Client path traversal (prior W-2, re-measured) | lib/api.ts route-param interpolation | lib/api.ts:1104,1108,1143,1403 and 9 more interpolate route ids unencoded; cmd:python drive.py navigating /live/..%2f..%2fauth%2fme produced GET /jobs/..%2F..%2Fauth%2Fme (stub request log), %2F not decoded, request stayed under /jobs/ | Victim clicks a crafted app link | None on this build: the id cannot contain a real slash and a bare .. segment is normalised away by the browser before routing; the backend route does not match the decoded path | Keep as hardening: encodeURIComponent every path segment or reject non-UUID route params before calling the API | Unit test that cancelJob with a slash-bearing id issues an encoded path | REPRODUCED |

Counts: P0 0, P1 0, P2 0, P3 4, INFO 2.

## Not verified

- Full local backend on `bl_audit3_fe_test` with Redis db 13 was not stood up. The render test used a schema-driven hostile stub instead, which covers every string field of every GET response the dashboard consumes. Not covered by this choice: any rendering that depends on real backend normalisation of values (none would make escaping weaker) and end-to-end behaviour of real backend errors.
- The CSV download was not clicked in the browser (the Download buttons were not reachable by the generic selector within the timeout). The conclusion that a hostile `text/html` export is saved, not rendered, rests on code reading of `lib/api.ts:960-983`, `:995-1020`, `:1166-1183`, `:1196-1229` (`download` attribute on a blob URL, revoked immediately).
- Stateful flows that need real data were not driven: the scraper creation wizard submit, batch creation, checkout and portal redirects, MFA and API-key minting. Their rendered strings are fixed copy or pass through the same `toastError` path that was measured.
- Production pages behind login were not visited (no production credentials, by rule). Live verification is limited to the login page chunks and headers; dashboard chunks were scanned only from the local build of the same commit, which may differ from Vercel's build output in chunk layout but not in source.
- Production `__Secure-` / `__Host-` cookie prefixes were not re-measured (local run is plain http); audit 2 measured them in production.
- Vercel project environment variables were not inspected (no access). A secret added to a `NEXT_PUBLIC_*` variable on Vercel would appear only in the live bundle; the 21 live login chunks were clean, but chunks for authenticated routes were not fetched from production.
