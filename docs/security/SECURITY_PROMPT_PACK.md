# Abro Security Prompt Pack

> **Purpose:** Operational reference. Copy-paste prompts you run against Claude Code (during build) and Codex (during daily review) to catch the security gaps AI tools typically miss.
>
> **Source:** Adapted from Mike Girma's *Zero to Dangerous: Security Prompt Pack* (Teknical), with examples, table names, and stack references rewritten for Abro's specific architecture.
>
> **When to consult this file:** Before shipping a new feature (run the relevant section's prompts). At end of every build day (run the Master Security Review). Before launch on day 17 (run the Pre-Launch Prompt).
>
> **What this file is NOT:** This is not the source of truth on security. CLAUDE.md §2 (non-negotiables), CLAUDE.md §5 (authorization matrix), CLAUDE.md §6 (logging), and PRD §18 are authoritative. This file is the operational layer on top of them — prompts you run, not rules you obey.

---

## How this maps to Abro

The original prompt pack is stack-agnostic. Abro is opinionated: Next.js 15 App Router, Supabase with RLS, Apify ingestion via Railway, Tailwind/shadcn UI, server actions for all mutations. Some of the original prompts assume different patterns (Express, raw SQL, JWT in localStorage) — those have been rewritten for our actual stack. The threats are the same. The fixes are different.

When a prompt references "your tables" or "your endpoints," it means the schema in CLAUDE.md §4 and the authorization matrix in CLAUDE.md §5.

---

## Sections

1. [Frontend-only validation](#1-frontend-only-validation)
2. [Hardcoded secrets and API keys](#2-hardcoded-secrets-and-api-keys)
3. [Authentication and session security](#3-authentication-and-session-security)
4. [Missing permission checks](#4-missing-permission-checks)
5. [Sensitive error messages and data leaks](#5-sensitive-error-messages-and-data-leaks)
6. [Injection attacks (SQL, XSS, CSRF)](#6-injection-attacks-sql-xss-csrf)
7. [File upload security](#7-file-upload-security)
8. [Rate limiting and brute force](#8-rate-limiting-and-brute-force)
9. [HTTPS and transport security](#9-https-and-transport-security)
10. [Data privacy and PII handling](#10-data-privacy-and-pii-handling)
11. [Insecure configuration and defaults](#11-insecure-configuration-and-defaults)
12. [Outdated and vulnerable dependencies](#12-outdated-and-vulnerable-dependencies)
13. [Logging, monitoring, and audit trails](#13-logging-monitoring-and-audit-trails)
14. [The Master Security Review](#14-the-master-security-review)
15. [The Pre-Launch Prompt](#15-the-pre-launch-prompt)
16. [Server-action boundary + client Supabase default-deny + action auth helpers](#16-server-action-boundary--client-supabase-default-deny--action-auth-helpers)
17. [Business-rule invariants](#17-business-rule-invariants)
18. [Runtime output projection](#18-runtime-output-projection)
19. [Rate-limit implementation verification and hostile-client tests](#19-rate-limit-implementation-verification-and-hostile-client-tests)
20. [Browser bundle secret exposure and service-role scope verification](#20-browser-bundle-secret-exposure-and-service-role-scope-verification)
21. [Environment separation and migration drift](#21-environment-separation-and-migration-drift)
22. [Webhook and callback verification](#22-webhook-and-callback-verification)
23. [Open redirect audit](#23-open-redirect-audit)
24. [Email security and abuse](#24-email-security-and-abuse)
25. [Session privilege freshness](#25-session-privilege-freshness)
26. [Data retention and dead-letter quarantine](#26-data-retention-and-dead-letter-quarantine)
27. [AI-generated content safety and link-out safety](#27-ai-generated-content-safety-and-link-out-safety)
28. [Operational incident drills and production seeding guardrails](#28-operational-incident-drills-and-production-seeding-guardrails)
29. [Authorization coverage map, supply-chain audit, and security ownership](#29-authorization-coverage-map-supply-chain-audit-and-security-ownership)

---

## 1. Frontend-only validation

**Abro context:** Every form on Abro uses React Hook Form + Zod schemas. The Zod schema is supposed to live in `/lib/schemas/` and be shared between client validation and server action re-validation. The failure mode: schema exists, client uses it, server action skips it and trusts whatever the client sent.

**Audit prompt:**
```
Review every server action under /app/actions/. For each one:
1. Does it import a Zod schema from /lib/schemas/?
2. Does it call schema.parse() or schema.safeParse() on the input BEFORE any database operation?
3. If validation fails, does it return { data: null, error: <message> } without reaching the database?

Flag every action where the schema is missing OR is imported but not actually used to validate input. Server actions that trust client-shaped data are the entry point for the rest of the OWASP top 10.
```

**Reinforce backend validation prompt:**
```
For the [feature name] server action: confirm the Zod schema validates all of these explicitly:
- Required fields are present
- String fields have maxLength to prevent abuse (no field accepts 100KB of text)
- Numeric fields have min/max bounds appropriate to the domain
- Enum fields use z.enum() and reject anything outside the allowed values
- Array fields have a maximum length
- UUID fields use z.string().uuid()
- Email fields use z.string().email()

If any of these are missing, add them. Reject failed validation with a 400-equivalent error response, not silent acceptance.
```

**Test prompt:**
```
For [server action name], write a test that bypasses the client by calling the server action directly with: (1) missing required fields, (2) string fields with 50,000 characters, (3) numeric fields outside valid ranges, (4) malformed UUIDs, (5) enum values that aren't in the allowed set. Confirm the server rejects each with a clear error and the database is never touched.
```

---

## 2. Hardcoded secrets and API keys

**Abro context:** Abro's secrets are: Supabase URL and anon key (public-ish, can be in client), Supabase service-role keys (3 separate scoped keys — never in client), Apify API token, Resend API key, PostHog project key (public), OpenAI API key (for Abro Summary generation, server-side only). The failure mode: the wrong key in the wrong place.

**Audit prompt:**
```
Scan the entire repo for hardcoded secrets. Check:

1. Every file under /app/, /components/, /lib/ — anything that ships to the client. Flag any string that looks like an API key: "sk-...", "service_role:", UUIDs paired with words like "secret" or "token", base64 blobs longer than 40 characters.

2. Confirm that ONLY these env vars are accessed in client-side code (files without 'use server' at the top, files under /app/ that aren't server actions or route handlers): NEXT_PUBLIC_SUPABASE_URL, NEXT_PUBLIC_SUPABASE_ANON_KEY, NEXT_PUBLIC_POSTHOG_KEY. Any other process.env reference in client code is a leak.

3. Confirm /supabase/migrations/*.sql files have no embedded secrets, no test database URLs, no admin credentials.

4. Confirm /services/ingestion/ files reference SUPABASE_SERVICE_ROLE_KEY_INGESTION specifically, not the general service-role key.

5. Confirm .env is in .gitignore. Run: git log --all --full-history -- .env. If anything ever committed, that secret has been rotated.

List every finding with file and line.
```

**Scoped service-role enforcement prompt:**
```
Review every Supabase client instantiation in the project. There should be exactly three service-role clients, each in its own file with its own scoped key:

- /lib/supabase/server-action.ts: uses anon key + user JWT for RLS-enforced queries (THE DEFAULT for user-facing server actions)
- /services/ingestion/supabase-client.ts: uses SUPABASE_SERVICE_ROLE_KEY_INGESTION, scoped to listings, listing_score_components, partnership_listings, external_api_log, ingestion_log, dead_letter_listings
- /lib/supabase/admin.ts: uses SUPABASE_SERVICE_ROLE_KEY_ADMIN, accessible only from /app/admin/* server actions that have verified the requester is Abenezer

If any user-facing server action uses the admin client or ingestion client, that's a critical finding. The whole RLS model collapses if normal user paths bypass RLS via service role.
```

---

## 3. Authentication and session security

**Abro context:** Supabase Auth handles signup, login, password reset, OAuth (Google). Sessions are stored in cookies, managed by `@supabase/ssr`. Default Supabase config is mostly good but needs verification for v1.

**Auth audit prompt:**
```
Review the auth configuration for Abro:

1. Are passwords being handled by Supabase Auth (correct) or has any custom password storage been introduced (would be a critical finding)?

2. In /lib/supabase/server.ts or wherever the SSR client is configured, confirm cookies are set with: httpOnly: true, secure: true (in production), sameSite: 'lax'.

3. Confirm the session refresh logic works — the @supabase/ssr middleware should refresh expiring sessions on every request.

4. Confirm logout invalidates the session server-side: the signOut server action calls supabase.auth.signOut() AND clears the cookie. Test by capturing the session token before logout and trying to reuse it after — it should be rejected.

5. Password reset flow: confirm the reset email is sent via Resend, the reset link uses Supabase's built-in token (which expires after 1 hour by default), and the password-change page validates the token before accepting a new password.

6. Email verification: confirm new signups require email verification before they can take social actions (per CLAUDE.md §11 — connection requests, partnership joins, messaging require verified email).

7. Rate limiting on auth endpoints: confirm the rate-limit middleware (per CLAUDE.md §11) caps login attempts at 5 failures per 10 minutes per IP, with 30-min lockout.

List every issue.
```

**Cookie config verification prompt:**
```
Open the browser DevTools on a logged-in Abro session. Inspect Application > Cookies for the deployed domain. Confirm:

- The Supabase auth cookies (sb-access-token, sb-refresh-token, or however @supabase/ssr names them) have HttpOnly: true
- They have Secure: true
- They have SameSite: Lax (not None)
- The expiration is reasonable (Supabase default is 1 hour for access, 30 days for refresh)

Any cookie without HttpOnly is accessible to client-side JavaScript and can be stolen via XSS. Verify with: document.cookie in the browser console — auth cookies should NOT appear in the output.
```

---

## 4. Missing permission checks

**Abro context:** This is the most important section. CLAUDE.md §5 (authorization matrix) is the source of truth. The failure mode here is the one Mike Girma calls out specifically: Claude Code writes a server action that fetches data, RLS catches some of it, application-layer check is missing entirely.

**Per-action audit prompt:**
```
For server action [name] in [file path]:

1. Which row of the CLAUDE.md §5 authorization matrix does this action touch?
2. What's the unauthorized response per the matrix (401 / 403 / 404 for admin / field-omitted)?
3. Walk through the action's code line by line. Confirm in order:
   a. const session = await supabase.auth.getSession() OR equivalent — and the action returns 401 if no session
   b. If the action operates on a resource owned by a user (a profile, a partnership, a connection), confirm the action checks that the requesting user is authorized to operate on this specific resource_id BEFORE querying or mutating
   c. If the action is admin-only, confirm the requester's user_id matches the admin user_id AND the action returns 404 (not 403) to prevent enumeration
   d. If the response contains other users' data, confirm sensitive fields (capital_source, raw quiz_responses, email, phone, partnership message contents) are omitted from the payload entirely

For any check that's missing, write the exact lines of code to add, in the correct position within the action.
```

**Full-codebase matrix audit prompt:**
```
For each row of the CLAUDE.md §5 authorization matrix, list every server action and route handler that operates on that resource. For each:

1. Identify the file and line where authorization is checked.
2. If no check exists, that's a Critical finding.
3. If a check exists, identify whether it would correctly deny: (a) an unauthenticated request, (b) an authenticated request from a different user, (c) a URL-parameter-manipulated request.

Output a table:
| Matrix row | File:line | Auth check present? | Denies unauthenticated? | Denies wrong user? | Denies URL tampering? |

Anything with a "no" in any column is a Critical or High finding.
```

**Test for URL-parameter tampering prompt:**
```
Generate test cases for /tests/authorization/ that simulate the IDOR attack pattern for Abro:

1. Test: user A logged in, calls getUserProfile(user_B_uuid) — must return 403 OR return user_B's data with capital_source, raw quiz_responses, email, phone OMITTED.

2. Test: user A logged in, calls updateUserProfile(user_B_uuid, ...) — must return 403, database unchanged.

3. Test: user A logged in (not a member of partnership P), calls getPartnershipMessages(partnership_P_uuid) — must return 403, no messages returned.

4. Test: user A logged in (not creator of partnership P), calls approveJoinRequest(request_in_P_uuid) — must return 403, request status unchanged.

5. Test: user A logged in (not admin), calls listDeadLetterListings() — must return 404 (not 403).

6. Test: user A logged in (not admin), navigates to /admin/logs — must redirect or 404, never render admin UI.

Run all six tests. Any pass that should fail is a critical finding.
```

---

## 5. Sensitive error messages and data leaks

**Abro context:** Next.js server actions return `{ data, error }`. The `error` should be a user-facing string ("Something went wrong, reference XYZ"), never a stack trace, never a raw Postgres error, never a Supabase error object. The error_id ties it to a server-side log entry per CLAUDE.md §6.

**Error message audit prompt:**
```
Review every server action and route handler in /app/. For each error path (try/catch, .catch(), error returns):

1. What gets returned to the client when an error occurs?
2. Does it ever include: a stack trace, a file path, a database column name, a raw Postgres error code, a Supabase error object, the SQL query that failed, the table name involved?

If yes to any of those, that's a finding. The pattern should be:

try {
  // operation
} catch (err) {
  const errorId = await logError(err, { userId, requestId, context }) // returns UUID
  return { data: null, error: `Something went wrong, please try again. Reference: ${errorId}` }
}

The errorId lets the user contact support and lets us look up the full stack trace in error_log. The user sees nothing about Postgres, our schema, or our internals.
```

**API response field-stripping prompt:**
```
Review every server action that returns data about a user (their own or another's). For each response, list every field returned. Compare against CLAUDE.md §5 — which fields are this requester authorized to see?

For other users' profiles, the response must omit: capital_source, raw quiz_responses (the JSONB blob), email, phone, blocked_users list, any admin flags.

For the requester's own profile, all fields may be included, but raw quiz_responses should still be a separate explicit call (getMyQuizResponses) rather than bundled into getProfile.

For listings, never include any field for broker contact, broker name, broker phone, broker email, narrative description, photo URLs — those fields don't exist in our schema, so this is mostly about confirming Apify ingestion code doesn't try to map them in.

For partnership member views: non-members get the partnership's public metadata only (name, target_geo, target_industry, pitch); members get the member list, messages, join requests. Confirm the response shape changes based on membership.

Flag every endpoint where the response includes a field a requester shouldn't see.
```

---

## 6. Injection attacks (SQL, XSS, CSRF)

**Abro context:** SQL injection is largely prevented by the Supabase typed client — all queries are parameterized automatically. The risk is `dangerouslySetInnerHTML` (banned per CLAUDE.md §2 non-negotiable #10), the Abro Summary AI output if not properly handled, and raw SQL in migrations.

**SQL injection check prompt:**
```
Search the codebase for any raw SQL with string interpolation:

1. grep for: supabase.rpc with template literals containing variables
2. grep for: Any postgres client outside the Supabase typed client
3. grep for: query() or execute() with template literals
4. Review all /supabase/migrations/*.sql — these are static and fine, but confirm none read from a variable input

The Supabase typed client parameterizes by default. The risk is custom raw SQL via .rpc() or via a separate postgres library. If found, rewrite using .from(table).select() etc., or use Supabase's parameterized .rpc('function_name', { params }) pattern.
```

**XSS check prompt:**
```
Search the codebase for XSS risks:

1. grep for: dangerouslySetInnerHTML — should return ZERO results. If any exist, that's a Critical finding (violates CLAUDE.md §2 non-negotiable #10).

2. grep for: eval, Function constructor with string args, setTimeout/setInterval with string args — all should return zero.

3. Review every place user-generated content is rendered:
   - Partnership pitch text (rendered on partnership cards and detail page)
   - Partnership message contents (rendered in the workspace)
   - Quiz free-text responses (only shown to the user themselves but still)
   - Abro Summary AI-generated text (rendered on listing pages)
   - User name and community affiliation (rendered on user cards and profiles)

   For each, confirm: rendered as React text children (auto-escaped) — not via innerHTML, not via Markdown rendering with raw HTML enabled.

4. Special case: the Abro Summary is AI-generated. Even though it goes through our prompt validation per PRD §8.5.5, treat the output as untrusted text. Confirm it's never rendered with raw HTML.
```

**CSRF check prompt:**
```
For Abro's server actions (Next.js 15 App Router):

1. Confirm Next.js's built-in CSRF protection is in place — server actions are not callable from cross-origin requests by default (Next.js validates the Origin header). Verify by reviewing next.config.js for any setting that disables this.

2. For any state-changing operation that's NOT a server action (e.g., a route handler), confirm it validates the Origin header against an allowlist that includes only the production domain and the Vercel preview URL.

3. For auth cookies: confirm SameSite=Lax is set (per section 3 above). This is the primary CSRF defense for cookie-auth flows.

4. Confirm there are no state-changing GET requests anywhere. All mutations should be POST/PUT/DELETE/PATCH (or server actions, which are POST under the hood).
```

---

## 7. File upload security

**Abro context:** v1 does NOT have user file uploads. Category illustrations are server-generated and uploaded to Supabase Storage by Abenezer via admin, not by users. Avatar images are not supported in v1. This whole section is a verification that we haven't accidentally introduced upload paths.

**Verification prompt:**
```
Confirm Abro v1 has zero user-facing file upload endpoints. Specifically:

1. grep for: useFormState with file input, type="file", multipart/form-data, FormData, supabase.storage.from().upload() called from a user-facing path. None of these should appear in user-facing code.

2. The only allowed storage write is from the admin client (/lib/supabase/admin.ts) uploading category illustrations to Supabase Storage. This happens via an admin UI at /admin/illustrations or by direct Supabase dashboard upload.

3. If any user-facing upload path is found, it's a scope violation per PRD §4.2 (file uploads are not in v1).

4. Confirm Supabase Storage bucket policies are configured to deny public uploads — only the admin service-role key can write.
```

---

## 8. Rate limiting and brute force

**Abro context:** CLAUDE.md §11 has the full rate limit table. The risk is that AI-generated code adds new endpoints without rate limits.

**Rate limit audit prompt:**
```
Review every server action and route handler in /app/. For each, identify what rate limit applies per CLAUDE.md §11:

- Account creation (signup): 3 per IP per hour
- Auth (login, password reset): 5 per 10 minutes per IP
- Connection requests: 20 outbound per user per day
- Partnership join requests: 10 per user per day
- Messages: 60 per user per hour per partnership
- Listings API (browse, search): 60 per user per minute
- All other authenticated endpoints: 100 per user per minute (default DoS protection)

The rate limit should be applied via shared middleware or a wrapper. Confirm:

1. There's a shared rate-limit utility in /lib/rate-limit.ts that uses Upstash, Redis, or a Supabase-based counter table.
2. Every server action that does ANY of the above categories invokes the rate limit check at the top.
3. If a new server action is added that doesn't fit a category, the default DoS protection applies.

Flag any server action that has no rate limit applied. Also flag any rate limit set lower than CLAUDE.md §11 specifies (more lenient than expected = bug).
```

**Brute force test prompt:**
```
Write a test that simulates a brute force attempt on the login endpoint:

1. From a single IP, attempt 10 logins in 60 seconds with wrong password for a real account.
2. Confirm the 6th attempt onward returns a rate-limit error (HTTP 429 or equivalent).
3. Confirm the legitimate user from that IP can't login for the next 30 minutes either (per CLAUDE.md §11).
4. Wait 30 minutes (or fast-forward via test fixtures). Confirm login works again with correct credentials.

Run the same test for password reset and signup. The thresholds differ but the lockout behavior should be the same.
```

---

## 9. HTTPS and transport security

**Abro context:** Vercel handles HTTPS automatically with managed certificates. The risks are: mixed content from images or external scripts, missing security headers in next.config.js, http:// links in seed data.

**HTTPS audit prompt:**
```
Confirm HTTPS enforcement for Abro:

1. Vercel deployment: HTTPS is automatic for the production domain. Confirm the custom domain has an active Let's Encrypt certificate via the Vercel dashboard.

2. next.config.js: confirm the headers() function returns these security headers on every response:
   - Strict-Transport-Security: max-age=31536000; includeSubDomains; preload
   - X-Frame-Options: DENY
   - X-Content-Type-Options: nosniff
   - Referrer-Policy: strict-origin-when-cross-origin
   - Permissions-Policy: camera=(), microphone=(), geolocation=()
   - Content-Security-Policy: see section below for the full CSP

3. Confirm productionBrowserSourceMaps: false is set (no source maps shipped to production).

4. grep for: any http:// URLs in the codebase. Listing source URLs from Apify are typically https:// already, but verify. Any http:// in seed data, in components, in tests, is a finding.

5. The Apify ingestion service running on Railway: confirm it's called over https://api.apify.com only.
```

**CSP configuration prompt:**
```
Write the Content-Security-Policy header for Abro. Allowlist:

- default-src 'self'
- script-src 'self' (Next.js will need 'unsafe-inline' for hydration scripts; use nonces if possible)
- style-src 'self' 'unsafe-inline' (Tailwind injects inline styles)
- img-src 'self' data: https://*.supabase.co (for category illustrations from Storage) https://abro-app.vercel.app
- font-src 'self' https://fonts.gstatic.com
- connect-src 'self' https://*.supabase.co https://api.posthog.com
- frame-ancestors 'none' (defense in depth with X-Frame-Options: DENY)
- form-action 'self'
- base-uri 'self'

If any external domain needs to be added later, document it in /docs/DEPLOYMENT.md so we know why and can audit.
```

---

## 10. Data privacy and PII handling

**Abro context:** CLAUDE.md §2 non-negotiables protect the highest-sensitivity fields. PRD §18.4 covers profile privacy controls. The risks are: logging fields we shouldn't, exposing emails via the URL/error, or accidentally creating an export that leaks others' data.

**PII inventory prompt:**
```
Map every piece of personal data Abro collects and stores. For each, identify:

| Field | Stored in | Encryption at rest | Logged? | Exposed via API? | User can delete? |

Expected fields:
- name (users.first_name, users.last_name)
- email (Supabase Auth, not in users table)
- phone (users.phone — optional, v1 marked as not yet collected)
- ZIP code (users.zip_code)
- capital_source (users.capital_source — never exposed)
- raw quiz responses (quiz_responses.responses JSONB — never exposed to anyone except owner)
- partnership message contents (messages.content)
- community affiliation (users.community_affiliation — opt-in only)

For each row, confirm:
1. Supabase encryption-at-rest is enabled (default true for Supabase Pro).
2. The field is NEVER logged via the shared logger (sanitization layer per CLAUDE.md §6 should strip these).
3. Exposure via API follows the authorization matrix.
4. User deletion (account deletion flow) cascades correctly.

Flag any gap.
```

**Right-to-delete prompt:**
```
Implement the account deletion server action (deferred from v1 but planned). The action should:

1. Verify the requesting user (cannot delete another user's account).
2. Cascade-delete or anonymize per the relationships:
   - users row: delete
   - quiz_responses: delete
   - partnership_members rows where user is the member: delete (this is what makes the partnership lose them)
   - partnerships where user is creator: hand off to another member if one exists, else mark as closed
   - messages from this user: anonymize the sender_id (set to NULL or to a sentinel "deleted_user" UUID) — don't delete, because other members rely on conversation context
   - connections: delete (both directions)
   - Supabase Auth: delete the auth user
   - audit_log entries about this user: KEEP, but redact the user_id to a hash (forensic record per CLAUDE.md §6)
3. Send a confirmation email via Resend before final deletion.
4. Log the deletion to audit_log (append-only) with timestamp.

This isn't in v1 scope but should be ready for phase 2 per PRD §13. Write the migration plan now while the schema is fresh in mind.
```

---

## 11. Insecure configuration and defaults

**Abro context:** The stack is opinionated. Most defaults are good. The risks are: forgetting to set production env vars, leaving Supabase RLS off on a new table, leaving debug routes accessible.

**Production-readiness prompt:**
```
Walk through Abro's production configuration:

1. Vercel project: NODE_ENV is set to "production"; preview deploys use staging Supabase, production deploys use production Supabase; build command runs `next build` (no dev scripts); deploy branch is `main` only.

2. next.config.js: productionBrowserSourceMaps: false; poweredByHeader: false; reactStrictMode: true.

3. All Supabase tables have RLS enabled — every migration that creates a table must include `ALTER TABLE ... ENABLE ROW LEVEL SECURITY;` and at minimum SELECT, INSERT, UPDATE, DELETE policies. Run `select tablename from pg_tables where schemaname='public' and rowsecurity = false` against production — expected output: empty.

4. /admin routes return 404 (not 403, not the admin UI) when called without admin authentication. Test from an incognito browser.

5. /api/debug/, /api/test/, anything that looks like a dev-only route: confirm doesn't exist or returns 404 in production.

6. No console.log in production code (lint-enforced per CLAUDE.md §2 non-negotiable #9). Run `grep -r "console\." app/ lib/ components/ services/ --include="*.ts" --include="*.tsx"` — should return empty.

7. CORS: server actions are origin-restricted by Next.js; route handlers (if any) explicitly check Origin against an allowlist (production domain + Vercel preview URLs only).

8. Spend caps configured per PRD §18.10 on all paid services: Vercel ($50), Supabase ($50), Railway ($25), Resend ($20), Apify ($250). Verify in each dashboard.

Flag every misconfiguration.
```

---

## 12. Outdated and vulnerable dependencies

**Abro context:** Pinned versions, weekly `npm audit`, GitHub Dependabot enabled.

**Dependency audit prompt:**
```
Run `npm audit --production` and parse the output. List:

1. Critical vulnerabilities — must fix before next deploy
2. High vulnerabilities — fix within 48 hours
3. Moderate vulnerabilities — fix within 2 weeks
4. Low — track but don't block

For each Critical or High, identify whether the fix requires a major version bump. If yes, separately note the breaking changes per the package's changelog. If no, apply the fix.

Also flag any direct dependency that:
- Hasn't been updated in 18+ months (check `npm outdated`)
- Has < 1000 weekly downloads on npm (per PRD §18.13 item 03 SBOM rules)
- Was added without a documented reason in /docs/decisions/dependencies.md

For new dependencies being considered, run this check BEFORE adding. Adding a dependency is a decision that gets reviewed by Architect agent (per /agents/03-system-architect.md).
```

---

## 13. Logging, monitoring, and audit trails

**Abro context:** CLAUDE.md §6 specifies the five log tables (request_log, error_log, slow_query_log, external_api_log, audit_log). The risk is that AI-generated code uses `console.log` instead of the shared logger, or skips logging an error path entirely.

**Logging coverage prompt:**
```
Review every server action and route handler. For each:

1. Does it log the request to request_log via the shared logger at /lib/logger.ts? (Should be middleware-applied, not per-action.)

2. Does every error path log to error_log with: timestamp, error_class, stack trace, request_id, user_id (if available), and the request context (path, method, sanitized inputs)?

3. Does it generate an error_id (UUID) and return it to the user in the error message ("Reference: XYZ")?

4. For external API calls (Apify, Resend, PostHog): does it log to external_api_log with status, duration, retries, error if any?

5. For mutations that touch the audit_log categories (account creation/deletion, partnership status change, listing manual delete, source kill switch fired): does it log to audit_log?

Flag any action that fails any of the five.

Then verify the sanitization layer in the shared logger strips: passwords, tokens, email addresses, phone numbers, capital_source values, raw quiz responses, partnership message contents. Run a test: trigger a deliberate error in a server action that receives an email + password, then inspect error_log — neither value should appear anywhere in the log entry.
```

**Alert wiring prompt:**
```
Confirm the alert rules from CLAUDE.md §6 are wired up and firing to Abenezer's phone (via email-to-SMS or Slack webhook):

1. Error rate > 5% of requests in any 5-minute window
2. Any endpoint with 95th-percentile latency > 10 seconds in a 5-minute window
3. Any Apify ingestion run with > 20% rejection rate
4. Any external API (Apify, Resend, PostHog) returning 5xx for 3+ consecutive calls
5. Any RLS policy violation (should be impossible; one means investigation)
6. Spike in 5xx responses above 10 in any 5-minute window
7. Spend approaching cap on any paid service (80% threshold per /agents/07-devops.md)

Trigger each alert deliberately as a test. Confirm Abenezer receives the alert within 5 minutes. Document the test results in /docs/codex-daily-review-log.md.

If alerts are noisy after the first week, tune thresholds — but never disable an alert category entirely.
```

---

## 14. The Master Security Review

> **This is the prompt that runs after every meaningful feature.** Run it twice. Run it until it comes back clean.

**Master prompt — to run after every feature:**

```
I just finished building [feature name and description] in Abro.

The relevant files changed are: [list files, or "everything in the day-N branch"].

Review ONLY the new code for security issues. Walk through this checklist explicitly:

1. **Authorization (CLAUDE.md §5):** Every server action checks (a) session, (b) ownership or membership of the resource, BEFORE the database query. Application-layer check exists IN ADDITION TO RLS. Admin routes return 404 (not 403) to non-admins. Sensitive fields (capital_source, raw quiz_responses, email, phone, partnership message contents) are omitted from response payloads when the requester isn't authorized to see them. Test cases for unauthorized paths exist.

2. **Secrets and keys:** No hardcoded secrets. No process.env reference in client-side code except NEXT_PUBLIC_SUPABASE_URL, NEXT_PUBLIC_SUPABASE_ANON_KEY, NEXT_PUBLIC_POSTHOG_KEY. The right service-role key for the right service (ingestion key for ingestion service, admin key for /admin routes only, anon key + user JWT for user-facing).

3. **Input validation:** Zod schemas from /lib/schemas/ used and called via .parse() or .safeParse() at the top of every server action. String fields have maxLength. Numeric fields have bounds. Enum fields use z.enum(). UUIDs validated.

4. **Error handling:** No stack traces, file paths, database column names, or Postgres errors returned to clients. Errors return { data: null, error: "Something went wrong, reference XYZ" } with the reference UUID logged in error_log.

5. **XSS / dangerouslySetInnerHTML:** Zero instances of dangerouslySetInnerHTML. All user-generated content (pitch text, message content, free-text quiz responses, AI-generated Abro Summary) rendered as React text children, auto-escaped.

6. **SQL injection:** All queries via the typed Supabase client (parameterized). No raw SQL with string interpolation. .rpc() calls use parameter objects, not concatenated strings.

7. **File uploads:** Confirmed zero new user-facing upload paths (v1 doesn't have them).

8. **Rate limiting:** Every new server action has a rate limit applied per CLAUDE.md §11 category, or the default DoS protection at 100/min.

9. **CSRF:** Server actions are origin-restricted by Next.js (verified). New route handlers (if any) check Origin against the allowlist. SameSite=Lax on auth cookies.

10. **Encryption / PII:** No new fields collected without a CLAUDE.md / PRD justification. New fields that contain PII (if any) are in the sanitization allowlist for the shared logger.

11. **Configuration:** New tables have RLS enabled in the same migration. New admin features return 404 to non-admins. No new debug routes.

12. **Dependencies:** Any new dependency added passes the SBOM check (≥1000 weekly downloads, ≥24 months active, pinned exact version for security-sensitive packages).

13. **Logging:** Every new error path logs to error_log. Every new external API call logs to external_api_log. Every new mutation that fits an audit category logs to audit_log. The sanitization layer strips sensitive fields.

14. **Non-negotiables (CLAUDE.md §2):** None of the 11 non-negotiables violated. Specifically: no payment processing, no commission logic, no money-only partnership members, no broker narrative/contact/photo scraping, no dangerouslySetInnerHTML, no console.log, no raw SQL with user input, no removed kill switch, no secrets in tracked code, no service-role key in user-facing paths.

For each issue found, output:
- **Category:** [number from 1-14 above]
- **Severity:** Critical / High / Medium / Low
- **File:line:** path/to/file.ts:N
- **Issue:** one sentence
- **Why it matters:** one sentence referencing CLAUDE.md or PRD
- **Exact fix:** code snippet showing the change

At the end:
- **Critical count:** N (must fix before commit)
- **High count:** N (fix within 24-48h)
- **Approval status:** GO / NO-GO for commit
- **What to verify on second pass:** items that need re-review after the first pass fixes

If everything is clean, say so. Don't invent findings to seem thorough.

Then I'll run this prompt a SECOND TIME. The second pass catches cascading issues — fixes that introduced new patterns with their own vulnerabilities. Run until two consecutive passes come back clean.
```

---

## 15. The Pre-Launch Prompt

> **Run this on day 17, before final production deploy. Once.**

```
Abro is about to deploy to production for the May 30 launch. Run a comprehensive security audit of the entire codebase. For each item below, output PASS or FAIL with file references and exact fixes:

1. All secrets are in environment variables (Vercel, Railway, Supabase dashboards), not in code. Run grep for hardcoded keys — should return empty. Check .env is in .gitignore. Check git log --all --full-history -- .env returns no commits.

2. Every Supabase table has RLS enabled. Run: select tablename from pg_tables where schemaname='public' and rowsecurity = false; — expected empty.

3. Every server action validates input via a Zod schema from /lib/schemas/ before any database operation.

4. Every server action that touches user data checks authorization at the application layer (not just RLS), in addition to the database-level RLS policy. Walk every row of CLAUDE.md §5 — every row has a passing test in /tests/authorization/.

5. Error messages returned to clients contain no stack traces, file paths, column names, or raw Postgres errors. All errors return error_id format.

6. No dangerouslySetInnerHTML anywhere. All user-generated content rendered as React text children.

7. All queries use the typed Supabase client (parameterized). No raw SQL with user input.

8. CORS / Origin restrictions in place. Server actions origin-restricted by Next.js default. Route handlers (if any) validate Origin header.

9. Auth cookies: HttpOnly, Secure, SameSite=Lax. Verify in browser DevTools on the deployed domain.

10. Security headers present on every response: HSTS, X-Frame-Options: DENY, X-Content-Type-Options: nosniff, Referrer-Policy, Permissions-Policy, CSP. Verify with: curl -I https://abro-app.vercel.app

11. Rate limiting applied to every server action per CLAUDE.md §11 categories or default DoS protection.

12. HTTPS enforced on all paths. No http:// URLs in code, components, or seed data. Vercel custom domain has active Let's Encrypt cert.

13. File uploads: zero user-facing upload paths in v1 (confirm absence).

14. Logging: every error path logs to error_log via the shared logger. Sanitization layer strips passwords, tokens, emails, phone numbers, capital_source, raw quiz responses, partnership message contents. Verify by triggering a test error and inspecting the log entry.

15. Dependencies: npm audit returns no Critical vulnerabilities, all High vulnerabilities have fixes pending or applied. All security-sensitive dependencies pinned to exact versions.

16. Admin routes return 404 (not 403, not admin UI) when accessed by non-admin or unauthenticated users. Test from an incognito browser at /admin/logs, /admin/listings, /admin/illustrations.

17. No console.log, console.error, console.debug in production code. Lint rule enforces this — verify the rule is active in eslint config.

18. No test credentials, dummy users, or seed data in the production database. The production database has only: 15-25 real pilot users per CLAUDE.md §7, 3-5 real partnerships, 500 real listings from Apify.

19. Spend caps configured in every paid service's dashboard: Vercel $50, Supabase $50, Railway $25, Resend $20, Apify $250. Verify each.

20. Privacy policy and terms of service pages exist at /privacy and /terms, linked from the footer, and accurately describe Abro's data handling.

21. SPF, DKIM, DMARC DNS records configured for the email-sending domain. Verify with: dig TXT abro.app, dig TXT _dmarc.abro.app.

22. Backup verification: Supabase backups are enabled (default for Pro tier). Trigger a test restore in a staging environment — if you can't restore, you don't have a backup.

23. The five launch-readiness questions from CLAUDE.md §6 can each be answered with a SQL query in under 5 seconds against the live error_log:
    - "How many users were affected?"
    - "What was the error?"
    - "When did it start?"
    - "Is it still happening?"
    - "Which code path triggered it?"

24. Codex daily review log (/docs/codex-daily-review-log.md) has zero outstanding Critical or High findings from the last 7 days.

25. Build journal (/docs/BUILD_JOURNAL.md) day-17 entry indicates final state with no open items flagged "broken" or "unfinished."

Output: a table of all 25 items with PASS/FAIL and notes. Anything FAIL is deploy-blocking until resolved. Deploy GO/NO-GO at the end.
```

---

## The 6 questions to ask yourself with every feature

(From Mike Girma's original — kept verbatim because they're the right framing.)

1. Who is allowed to use this — and does my code actually check?
2. What happens if someone types something weird into this field?
3. What sensitive data am I touching — and is it stored, sent, and shown safely?
4. Am I using secure defaults, or did I just keep whatever the AI gave me?
5. If someone tried to abuse this feature, what would they do first?
6. Would I know if something went wrong — is there logging and monitoring?

---

## When to consult which section

| Building... | Run sections |
|---|---|
| A new server action that reads user data | 1, 4, 5, 13, 14, 16, 18, 19 |
| A new server action that writes user data | 1, 4, 5, 8, 13, 14, 16, 17, 18, 19 |
| A new admin feature | 4, 11, 13, 14, 16, 25 |
| Auth or signup changes | 3, 8, 10, 13, 14, 23, 24, 25 |
| Apify ingestion changes | 1, 2, 5, 12, 13, 14, 17, 26 |
| New external API integration | 2, 9, 12, 13, 14, 22 |
| New table or schema migration | 4, 10, 11, 13, 14, 21 |
| New React component rendering user content | 6, 14, 27 |
| New client component touching Supabase | **16**, 14 |
| New webhook receiver | **22**, 14 |
| New OAuth / post-login redirect | **23**, 3, 14 |
| New AI-generated content surface | **27**, 6, 13, 14 |
| Before any production deploy | **15**, 20, 21, 28 |

The Master Security Review (section 14) runs after every feature, regardless. The Pre-Launch Prompt (section 15) runs once, on day 17. Sections 16-29 cover topics that were under-specified in v1 — fold them into 14's checklist when the feature touches the relevant area.

---

## 16. Server-action boundary + client Supabase default-deny + action auth helpers

**Abro context:** All mutations and sensitive reads flow through server actions or route handlers — the browser's Supabase client should never write, never call `.rpc()`, and never read a private table. CLAUDE.md §5 lists what's authorized for browser-side anonymous-key reads; everything else requires a server action that re-validates auth + rate-limit + input. The failure mode this section catches: a "convenient" `useEffect(() => supabase.from('users').insert(...))` in a client component that bypasses every server-side check.

**Server-action boundary audit prompt:**

```
Search the codebase for client-component Supabase usage:
  rg -n "from \"@/lib/supabase/client\"" app/ components/

For every result, classify the call shape:
  - .from(...).select(...)     → READ (allowed only for tables listed
                                  as "any authenticated reader" in
                                  CLAUDE.md §5: listings, users public
                                  columns)
  - .from(...).insert(...)     → WRITE → NOT ALLOWED in client code
  - .from(...).update(...)     → WRITE → NOT ALLOWED
  - .from(...).delete(...)     → WRITE → NOT ALLOWED
  - .rpc(...)                  → NOT ALLOWED unless the RPC is
                                  documented SECURITY DEFINER + the
                                  caller permission is encoded in the
                                  RPC body
  - .auth.signInWithOAuth      → allowed (Supabase auth client)
  - .auth.signOut              → allowed
  - .auth.getUser              → allowed in client components

Any other client-side .from / .rpc usage is a finding. Replace with a
server action that wraps the operation behind getUser() + rate-limit +
input Zod parse.

Output: file:line for every flagged call + the proposed server-action
replacement.
```

**Action auth helper standard:**

```
Search for redirect-based auth helpers inside server actions:
  rg -n "requireUser\(\)" app/actions/

requireUser() throws Next.js redirect sentinels — that's fine for page
server components but WRONG for server actions. Server actions must
return a typed error envelope, not redirect mid-mutation (Codex
pass-2 #3).

The action-safe pattern:
  const user = await getUser();
  if (!user) throw new UnauthenticatedError();

The wrapper in lib/server-action.ts converts UnauthenticatedError to
{ ok: false, error: { code: "unauthenticated", message: "..." } }.

Flag every requireUser() call inside an `app/actions/` file. Replace
with getUser() + throw UnauthenticatedError, unless the redirect is
explicitly intentional (rare — and if it is, document why inline).
```

**Default-deny client Supabase enforcement:**

```
Verify lib/supabase/client.ts only exports the anon-key client. Verify
that the anon key in the browser is NEVER used for:
  - INSERT / UPDATE / DELETE on any table (RLS denies these by default
    for the anon role; this check is a tripwire for accidental
    permissive policies)
  - SELECT on quiz_responses, audit_log, error_log, security_log,
    request_log, slow_query_log, external_api_log, dead_letter_listings,
    blocked_users, partnership_join_requests (sensitive tables)
  - .rpc() to any function that mutates

Run as a Supabase psql query:
  SELECT polname, polcmd, tablename FROM pg_policies
   WHERE schemaname='public'
     AND (polroles::text LIKE '%authenticated%' OR polroles::text LIKE '%anon%')
     AND polcmd IN ('INSERT', 'UPDATE', 'DELETE');

The result set should be small and match CLAUDE.md §5 explicitly. Any
unexpected write policy is a finding.
```

---

## 17. Business-rule invariants

**Abro context:** Some rules are not about auth or RLS — they're about what the business model permits. Money-only members violate the legal posture (CLAUDE.md §2 NN #3). Capturing broker narrative violates NN #4. These rules must be enforced at the DB + the action + the schema, in that order of authority.

**Invariant enforcement audit prompt:**

```
For each invariant below, identify the DB-level, action-level, and
schema-level enforcement point. Any missing layer is a finding.

1. T + K >= 4 for every partnership_members row
   - DB: trigger enforce_user_tk_floor_on_partnership_members
         + enforce_user_tk_floor_for_existing_members
   - Action: app/actions/partnerships.ts createPartnership /
             approveJoinRequest pre-check
   - Schema: lib/schemas/quiz.ts — scoring functions return clamped
             1-10 so an upstream parse can't bypass

2. Partnership readiness threshold (CLAUDE.md §8)
   - Derived server-side from partnership_members rows; never written
     by client. Verify no client code sets partnerships.status='ready'.

3. No passive-investor role
   - DB: no archetype enum value for 'investor' (verify via
         SELECT enum_range(NULL::archetype))
   - Schema: lib/schemas/user.ts archetypeEnum does not include
             "investor"

4. No commission / finder-fee concepts
   - grep for: "commission", "finder_fee", "fee_basis", "transaction_fee"
     across the codebase — should return zero non-doc matches.

5. Listing ingestion do-not-capture (CLAUDE.md §2 NN #4)
   - DB: listings table has NO columns for narrative description,
         broker name, broker contact, photo URLs. Verify via:
         SELECT column_name FROM information_schema.columns
          WHERE table_schema='public' AND table_name='listings';
   - Action: services/ingestion/src/sanitize.ts allowlist
   - Schema: lib/schemas/listing.ts PublicListing does not type these
             fields (drift check forces a compile error if they're
             added)

6. Quiz score bounds (1-10)
   - DB: users.time_score/money_score/knowledge_score CHECK (val
         BETWEEN 1 AND 10)
   - Code: lib/scoring.ts clampScore() — pure function returns 1-10
   - Schema: nothing — derived field

7. Derived fields cannot be client-set
   - users.time_score / money_score / knowledge_score / archetype are
     derived from quiz responses. Verify no server action accepts
     these as input (search updateProfile schema; they should be
     absent).
   - listings.intrinsic_deal_score is derived at ingestion. Verify
     no action permits client writes to it.

For any invariant that lacks DB-layer enforcement, the action layer is
the only line of defense — flag as HIGH.
```

---

## 18. Runtime output projection

**Abro context:** TypeScript types are a compile-time hint, not a runtime boundary. A `select('*')` that returns extra columns will satisfy `Promise<PublicUser>` at compile time even though the runtime payload includes private fields. The §5 contract requires runtime projection.

**Output projection audit prompt:**

```
For every server action that returns user / listing / partnership /
quiz data:

1. Identify the explicit column allowlist used in .select() (e.g.,
   PUBLIC_USER_COLUMNS, PUBLIC_LISTING_COLUMNS, GATE_COLUMNS).
2. Identify the runtime projection step that strips any non-allowlisted
   key before return (e.g., projectPublicUser, attachFitScore).
3. Confirm the two layers match — if the SELECT widens, the
   projection should still strip.

The pattern (from lib/schemas/user.ts):
  const PUBLIC_USER_KEY_ALLOWLIST = [...] as const;
  export function projectPublicUser(row) {
    const out = {};
    for (const k of PUBLIC_USER_KEY_ALLOWLIST) {
      if (k in row) out[k] = row[k];
    }
    if (out.community_affiliation_public !== true) delete out.community_affiliation;
    return out;
  }

Any action that returns DB rows DIRECTLY (no projection) is a finding.
Even if the SELECT looks tight today, a future widening — e.g., a join
that adds users.capital_source for a counter query — will leak.

A TypeScript type alone (`return data as PublicUser`) is NOT
sufficient. Casts at the wire boundary don't strip keys.
```

---

## 19. Rate-limit implementation verification and hostile-client tests

**Abro context:** §8 above lists the limit table. This section verifies the limits actually fire BEFORE the DB and that hostile-client tests (calling actions directly, not via the UI) catch bypass attempts.

**Rate-limit implementation verification prompt:**

```
For every action that includes a `rateLimiters.X.check()` call:

1. The check runs BEFORE any DB query in the same action. Verify by
   reading the step ordering — auth → rate-limit → validate → query.
2. The key passed to .check() is `${user.id}:<bucket-name>` or
   `${ip}:<bucket-name>`, NEVER user-controlled.
3. On allowed=false the action throws RateLimitedError and the wrapper
   converts to { ok: false, error: { code: "rate_limited", ... } }.
4. The DB is NEVER touched when rate-limited. Verify via the
   corresponding test case: `expect(mockSupabaseClient.from).not.
   toHaveBeenCalled()`.
5. The exhaustion event is logged via logSecurity({
     event: "rate_limit_triggered", userId, details: { action,
     reset_at_ms }
   }).

For every action that does NOT have a rate-limit preflight, identify
which CLAUDE.md §11 category applies and add the preflight. If no
category applies, default to listingsApi (60/min).
```

**Hostile-client test prompt:**

```
For each existing server action, add tests that bypass the UI and call
the action directly with hostile inputs:

1. EXTRA FIELDS: pass a payload with `evil_field: "..."` smuggled in.
   The .strict() schema must reject. Test:
     // @ts-expect-error
     const res = await myAction({ ...validInput, evil_field: "x" });
     expect(res.ok).toBe(false);
     expect(mockDb.from).not.toHaveBeenCalled();

2. WRONG USER IDS: from user A's session, target user B's resource
   (where applicable). Test that 403 or null is returned, never
   data.

3. REPLAYED REQUESTS: call the same action 100 times in a tight loop.
   The rate-limit bucket must trip and DB calls must stop.

4. MASSIVE STRINGS: pass a 100,000-char string into a text field.
   The schema's .max() must reject; DB never touched.

5. INVALID ENUMS: pass an enum value not in z.enum([...]). Schema
   must reject.

6. RAPID REPEATED REQUESTS: simulate 10 calls in 10ms. Rate-limit
   should fire by call 6 (or whatever the bucket allows). DB calls
   stop once limited.

These tests are the "would a curl-wielding attacker break it?" check.
Every action that touches the auth matrix should have at least 3 of
the 6 hostile-client tests.
```

---

## 20. Browser bundle secret exposure and service-role scope verification

**Abro context:** §2 covers the source-tree grep. This section covers the BUILT bundle grep (which catches process.env mistakes the source grep misses) plus the runtime test that proves each service-role key can only touch its scoped tables.

**Browser bundle secret exposure prompt (pre-launch):**

```
After running `next build`, open the production bundle and search for
secret-shaped strings:

  cd .next/static
  for term in SERVICE_ROLE OPENAI_API_KEY RESEND APIFY SUPABASE_SERVICE \
              sk- secret_key TOKEN PASSWORD; do
    echo "=== $term ==="
    rg -i "$term" .
  done

Expected output: empty for all terms except possibly the public
NEXT_PUBLIC_SUPABASE_ANON_KEY (which is design-intent in the client).
Anything else found is a Critical leak — find the import path that
pulled it into the client bundle and refactor to server-only.

Also run from a deployed preview URL:
  curl -s https://<preview>.vercel.app/_next/static/chunks/main-app-*.js \
    | grep -oE '(eyJ[A-Za-z0-9_-]{30,})|sk-[A-Za-z0-9_-]{20,}'

Any match = blocked deploy.
```

**Service-role scope verification prompt:**

```
Prove each service-role key can only touch the tables its job requires.
Run as the key in question against the production DB:

For SUPABASE_SERVICE_ROLE_KEY_INGESTION (Railway):
  SELECT 'listings' AS t, count(*) FROM listings;           -- allowed
  SELECT 'listing_score_components', count(*) FROM listing_score_components; -- allowed
  SELECT 'dead_letter_listings', count(*) FROM dead_letter_listings; -- allowed
  SELECT 'ingestion_log', count(*) FROM ingestion_log;       -- allowed
  SELECT 'external_api_log', count(*) FROM external_api_log; -- allowed
  -- These should FAIL with permission denied:
  SELECT count(*) FROM users;
  SELECT count(*) FROM quiz_responses;
  SELECT count(*) FROM partnerships;
  INSERT INTO users (id, name) VALUES ('00000000-...', 'attack');

For SUPABASE_SERVICE_ROLE_KEY_ADMIN (Vercel /admin only):
  -- All admin reads succeed; writes to listings should fail.

For the default service-role key used by user-facing actions:
  -- Per CLAUDE.md §12 this should NOT exist for user-facing paths.
  -- User-facing paths use the anon key + user JWT; RLS does the work.
  -- If you find a default-service-role import in app/ outside /admin,
  -- that's a Critical finding.

Document the result of each test in /docs/security-verification.md.
Run before launch.
```

---

## 21. Environment separation and migration drift

**Abro context:** Preview deploys must hit a staging DB; production deploys must hit production. Migrations must run forward-only on staging first, with the generated `database.types.ts` matching the live schema.

**Environment separation prompt:**

```
1. Vercel project: confirm the env scope for SUPABASE_URL,
   SUPABASE_SERVICE_ROLE_KEY, NEXT_PUBLIC_SUPABASE_URL,
   NEXT_PUBLIC_SUPABASE_ANON_KEY is set to:
     - "Production" → production Supabase project values
     - "Preview" → staging Supabase project values
     - "Development" → local .env.local override

2. Railway ingestion: confirm a separate Supabase project (or at
   minimum a separate service-role key) for staging. The cron job
   should ALWAYS hit production by design; staging cron, if any, hits
   staging.

3. Local scripts: every script under /scripts/*.{ts,mjs,sh} that can
   write to a Supabase project must check NODE_ENV or an explicit
   --target flag. A bare `node scripts/seed.mjs` should not be able
   to write to production. Add a `--target=production` gate that
   prints "ARE YOU SURE? type yes" and refuses input from
   non-interactive contexts.

4. Migration policy: never run `supabase db push` against production
   without running it on staging first. The standard workflow:
     - run on staging
     - regenerate types: npm run db:types
     - run app + service tests against staging
     - then promote to production
```

**Migration drift / rollback prompt:**

```
1. Forward-only: NEVER edit a migration file after it's been applied
   to staging or production. New behavior = new migration. The dated
   numeric prefix makes this auditable.

2. Destructive operations (DROP TABLE, DROP COLUMN, RENAME COLUMN,
   ALTER TYPE ... DROP VALUE) require:
     - explicit approval comment at the top of the migration file
       referencing the BUILD_JOURNAL entry that decided it
     - a backup verification step in the same PR
     - a tested rollback path

3. lib/supabase/database.types.ts must match live schema. Verify by:
     supabase gen types typescript --project-id <id> --schema public \
       | diff - lib/supabase/database.types.ts
   Empty diff = clean. Any diff is a drift finding — regenerate.

4. ALTER TYPE ENUM expansions are pure-add and idempotent
   (ADD VALUE IF NOT EXISTS). Removing an enum value is destructive
   and requires the procedure above.
```

---

## 22. Webhook and callback verification

**Abro context:** v1 has no inbound webhooks (Resend transactional only — outbound; Supabase Auth handles its own callbacks; PostHog is push-only). This section documents the standard for ANY future webhook receiver so the first one ships with the right shape.

**Webhook receiver audit prompt:**

```
For any new route handler that receives webhook callbacks (Resend
delivery status, Supabase auth events, PostHog event sinks, payment
providers in phase 2):

1. SIGNATURE VERIFICATION: the handler must verify the provider's
   signature header (e.g., `svix-signature` for Resend) against the
   shared secret. Verify in a constant-time comparison (use
   crypto.timingSafeEqual, NOT === on strings — leak via timing).

2. REPLAY PROTECTION: extract the timestamp from the signature
   payload (most providers include it). Reject if timestamp drift >
   5 minutes (configurable, ±300s). Store a recent-id ring buffer
   in memory (or a hash in Redis) to drop duplicates.

3. NO UNAUTHENTICATED MUTATION: if the handler mutates Abro state
   (writes to a table, sends an email, kicks off a job), it MUST
   verify the signature first. A failure to verify returns 401
   immediately with NO body content (don't leak that the endpoint
   exists).

4. TIMESTAMP TOLERANCE: ±300 seconds is the standard. Anything
   stricter breaks normal latency; anything looser opens replay
   windows.

5. LOG TO external_api_log: every webhook call lands a row with
   provider, status, signature_ok boolean, timestamp_drift_ms.

6. RATE LIMIT: even verified webhooks should be capped (e.g.,
   100/min/provider) to prevent a compromised provider key from
   DoSing.

Output: for each webhook route, the file:line of each of the 6 checks.
Any missing check is a Critical finding.
```

---

## 23. Open redirect audit

**Abro context:** Open redirects are the classic phishing-amplification vector. The OAuth callback (`/auth/callback`) and any future "?next=..." or "?redirectTo=..." parameter is the attack surface.

**Open redirect audit prompt:**

```
1. Search for redirect parameters:
     rg -n "searchParams.*next|searchParams.*redirectTo|searchParams.*returnUrl|searchParams.*continue" app/

2. For each result, verify the redirect destination is validated
   against an allowlist BEFORE redirect() is called:
     - allowed: relative paths starting with "/" and NOT "//"
     - allowed: absolute URLs whose origin matches the deployed
       domain or vercel.app preview pattern
     - rejected: everything else → fall back to "/"

   The standard helper (add if not present):

   function safeRedirect(target: string | null): string {
     if (!target) return "/";
     if (target.startsWith("//")) return "/"; // protocol-relative
     if (target.startsWith("/")) return target; // relative path ok
     try {
       const u = new URL(target);
       const allowedHosts = [
         "abro.app",
         "www.abro.app",
       ];
       if (allowedHosts.includes(u.hostname)) return target;
       if (u.hostname.endsWith(".vercel.app")) return target;
     } catch {
       /* fallthrough */
     }
     return "/";
   }

3. OAuth callback at /auth/callback: verify the redirect handed to
   supabase.auth.signInWithOAuth's `redirectTo` is a fully-qualified
   URL constructed from window.location.origin (not user-controlled
   query string).

4. Post-login default: if no `next` is provided, redirect to
   /profile, NOT to the referer header (which an attacker can spoof).

Any open redirect path found is a HIGH finding — these get phished
fast in real-world attacks.
```

---

## 24. Email security and abuse

**Abro context:** Abro sends transactional emails via Resend (welcome, password reset, partnership invites). No marketing email at launch. The risks: enumeration via "user not found" timing/copy differences, magic-link abuse, signup spam, and Resend domain reputation.

**Email security audit prompt:**

```
1. ENUMERATION:
   - Password reset endpoint: ALWAYS return the same response
     ("Check your email") whether the address exists or not. NEVER
     return "user not found" — that's an enumeration oracle.
   - Sign-up endpoint: same. "We sent a verification email" regardless
     of whether the address was already registered.
   - Magic link login: same.
   - Verify the timing is uniform within ±100ms — if "exists" is
     faster than "doesn't exist" due to skipping DB writes, an
     attacker can enumerate via timing.

2. RESET / MAGIC LINK ABUSE:
   - Reset token expiry: ≤ 1 hour (Supabase default). Verify
     SUPABASE_PROJECT/Auth Settings.
   - Token single-use: confirm reuse after success fails.
   - Rate-limit on reset requests: 3 per email per hour.

3. INVITE / JOIN-REQUEST SPAM:
   - partnership_join_requests: 10/user/day (already in place per §11).
   - Verify the action sends at most 1 email per request (no
     accidental loop).

4. RESEND DOMAIN VERIFICATION:
   - SPF, DKIM, DMARC DNS records configured for the sending domain.
   - Verify with: dig TXT abro.app +short; dig TXT _dmarc.abro.app +short
   - dkim selector verification.

5. UNSUBSCRIBE:
   - For non-transactional email (future weekly digest): unsubscribe
     link in every message, one-click, no-confirmation required.
   - Transactional email exempt — no unsubscribe needed for password
     reset / verification.

6. SPAM SCORE: send a test from the Resend dashboard to a Gmail
   inbox; spam-folder = needs SPF/DKIM/DMARC + From header sender
   domain to match.
```

---

## 25. Session privilege freshness

**Abro context:** Page loads check admin status, but a long-lived session could outlast a privilege revocation. Every admin action must re-check at execution time, not trust the page-load gate.

**Session privilege freshness prompt:**

```
For every admin action (anything that writes to admin_only tables,
flips listing.is_active, accesses /admin/* server actions):

1. The action must call `isAdmin(user.id)` AT THE TOP of the action
   body, AFTER `getUser()`. Not just on the parent page-load. The
   admin check should be a single line:
     if (!isAdmin(user.id)) {
       void logSecurity({ event: "admin_route_probed", userId: user.id,
                          details: { action: ACTION_NAME } });
       throw new Error("Not found"); // 404-shaped per §5 anti-enum
     }

2. Verify isAdmin reads from env.ADMIN_USER_ID (a UUID), not from a
   JWT claim. The env value is set at deploy time; revoking is one
   env var change + redeploy.

3. Every admin-route-probed log entry is reviewed daily. A signed-in
   non-admin who probes /admin → security_log row → manual review for
   account compromise vs honest mistake.

4. Phase-2: switch ADMIN_USER_ID to a roles table with revocation
   timestamps so the freshness check can read DB state, not env. v1's
   single-admin model is fine.
```

---

## 26. Data retention and dead-letter quarantine

**Abro context:** CLAUDE.md §6 lists log tables. None of them have explicit retention windows yet — they'll grow without bound. This section sets the policy. Dead-letter quarantine (NN #4 reinforcement) covers the rejected-payload edge case.

**Data retention policy prompt:**

```
Set retention windows per table. Run as scheduled SQL job (Supabase
edge function or pg_cron in phase 2).

| Table                  | Retention | Action after window         |
|------------------------|-----------|-----------------------------|
| request_log            | 90 days   | DELETE                      |
| error_log              | 180 days  | DELETE                      |
| slow_query_log         | 30 days   | DELETE                      |
| external_api_log       | 90 days   | DELETE                      |
| security_log           | 1 year    | DELETE (forensic floor)     |
| audit_log              | 7 years   | KEEP (compliance / forensic)|
| ingestion_log          | 90 days   | DELETE                      |
| dead_letter_listings   | 30 days   | DELETE (raw payload purged) |
| messages (soft-delete) | indef     | KEEP — message_history      |
| quiz_responses         | indef     | KEEP — re-scoring depends   |
| deleted users          | 30 days   | hard-delete (right-to-erase)|

For deleted users: a `users.deleted_at` soft-flag flips immediately;
the row + cascading personal data hard-deletes after 30 days unless a
legal hold is present. Confirm the deletion job covers: users,
quiz_responses, partnership_members (this user only), connections,
messages.sender_id anonymized, audit_log redacts user_id to a hash.

Document the schedule in /docs/retention-runbook.md once written.
```

**Dead-letter quarantine prompt:**

```
The dead_letter_listings table stores rejected scraper payloads for
investigation. Per CLAUDE.md §2 NN #4 the raw payload must NEVER
contain broker narrative / contact / photos — even in rejected rows.

Audit:
1. Read services/ingestion/src/sanitize.ts and confirm the sanitization
   layer runs BEFORE the dead-letter insert (not after).
2. Run against production:
     SELECT id, rejection_reason, raw_payload
       FROM dead_letter_listings
       LIMIT 50;
   For each row, grep raw_payload for the do-not-capture allowlist
   violations:
     - keys: "description", "narrative", "broker", "agent",
             "phone", "email", "photo", "image", "contact"
   Any row containing any of these keys (even with null values) is a
   sanitization failure → HIGH finding + immediate fix to the
   sanitizer.

3. Add a test: feed services/ingestion/src/__tests__ a payload
   containing broker narrative. Assert that the dead-letter insert
   either fails OR the inserted row's raw_payload has those keys
   absent.

4. Retention: 30 days per the table above. After 30 days, even a
   sanitized dead-letter row is purged.
```

---

## 27. AI-generated content safety and link-out safety

**Abro context:** The Abro Summary (PRD §8.5.5) is server-side AI text generated at ingestion. It must not hallucinate, must not give legal/financial advice, must not reconstruct broker contact info, and must be rendered as escaped React text. The external source-link CTA (CLAUDE.md §10) must use an allowlist + safe relationship attrs.

**AI content safety prompt:**

```
For the Abro Summary generation pipeline (services/ingestion/src/
abro-summary.ts or equivalent):

1. HALLUCINATION VALIDATION:
   - The prompt feeds ONLY structured fields (asking_price, sde, naics,
     city, state, etc.) to the model. Never the raw scraped HTML or
     broker narrative.
   - Output is validated against the source fields BEFORE save:
       * "established 50 years ago" rejected if years_established=12
       * "$500K SDE" rejected if SDE column is null or differs
   - Test fixture: feed a known-bad model response (with a
     hallucinated metric) and assert the validator rejects.

2. NO ADVISORY LANGUAGE:
   - grep the output template + sampled saved summaries for:
       "should buy", "great investment", "guaranteed", "you will earn",
       "tax-deductible", "legally", "consult a lawyer", "as your
       advisor"
   - Any match → sanitization failure. The Abro Summary is descriptive,
     not advisory.

3. NO BROKER RECONSTRUCTION:
   - The output may NEVER mention a broker, broker firm, contact name,
     or phone number. grep saved abro_summary values for:
       "broker", "agent", "contact", "call", "email", "phone",
       "@gmail", "@yahoo", phone-number-shaped digit groups
   - Any match → both a hallucination AND a NN #4 violation.

4. RENDERING:
   - Confirmed in U.2b/U.2c that abro_summary is rendered as React
     text children. No dangerouslySetInnerHTML, no MDX. Re-verify on
     every UI commit.

5. PROMPT + OUTPUT LOG SANITIZATION:
   - If you keep prompt/output logs in external_api_log: confirm
     they DO NOT contain user PII (user_id is fine, but no quiz
     responses, no email, no phone). The summary is generated for a
     listing, not a user — should be PII-clean by construction.
   - The logger's sanitization allowlist already strips email/phone/
     etc., but verify by sampling: SELECT details FROM external_api_log
     WHERE provider='openai' LIMIT 20.

Filed for v1: the model + prompt aren't shipped yet. When they do, run
all 5 checks before the first production summary lands.
```

**Link-out safety prompt:**

```
For every external <a> rendered with a URL from the DB (currently the
source_url on listings; future: any broker URLs, partnership invite
URLs):

1. The href value passes through isAllowedSourceUrl(rawUrl, source)
   (or a similar per-source allowlist).
2. The anchor has target="_blank" rel="noopener noreferrer".
3. No open redirect wrapper — e.g., a "click-through tracker" route
   that takes the destination as a query param. If one is added in
   phase 2 (for click analytics), it must allowlist destinations the
   same way.
4. The displayed link text matches the destination host. Avoid
   "Click here" — show "View on BizBuySell ↗" so users can spot a
   mismatch.

Grep for risk:
   rg -n "target=\"_blank\"" app/ components/
For each match, verify rel="noopener noreferrer" on the same element.
```

---

## 28. Operational incident drills and production seeding guardrails

**Abro context:** Documented runbooks aren't proven until you run them. Pre-launch drills shake out gaps in monitoring + alerting + on-call response. Seeding guardrails ensure the May-30 demo doesn't have fake users muddying the live data.

**Pre-launch operational drills prompt (day 16):**

```
Run each drill once on production. Time-box each to 30 minutes.

DRILL 1 — Fake error spike:
  Trigger 50+ uncaught exceptions in a 5-minute window from a
  staging instance. Verify:
  - error_log fills up
  - the §6 alert rule "error rate > 5%" fires within 5 minutes
  - the alert reaches Abenezer's phone/email
  - the on-call response playbook (docs/runbook-incident.md if it
    exists; create if not) is actionable

DRILL 2 — Fake rate-limit attack:
  From a single IP, hit a server action 200 times in 60 seconds.
  Verify:
  - rate_limit_triggered events log
  - security_log rows confirm the user_id key
  - no DB writes leaked through
  - if a sustained-attack alert exists, it fires

DRILL 3 — Fake ingestion failure:
  Set INGESTION_MAX_LISTINGS_PER_RUN=0 (or kill the Apify token) for
  one BBS run. Verify:
  - the run shows status='failed' in ingestion_log
  - the alert rule "Apify ingestion > 20% rejection" fires (in this
    case 100% rejection)
  - shutoff doesn't trigger (we want partial failures to alert, not
    auto-pause)

DRILL 4 — Fake leaked-secret rotation:
  Rotate SUPABASE_SERVICE_ROLE_KEY_INGESTION to a new value in Railway.
  Verify:
  - the rotation completes inside 5 minutes
  - the next cron run uses the new key (check ingestion_log timestamp)
  - the old key, if used, fails with "JWT expired" in external_api_log

DRILL 5 — Backup restore:
  Trigger a test restore in a Supabase staging project. Verify the
  restore succeeds and the restored DB matches a known prior state.
  "If you can't restore, you don't have a backup."

Document each drill's pass/fail in docs/launch-drills.md.
```

**Production seeding guardrails prompt:**

```
At launch the database should contain ONLY real data: pilot users from
the cohort + scraped listings + partnerships seeded with consenting
participants. No "John Doe" fake users; no "Coffee Shop Demo" fake
listings.

Pre-deploy script (run before promoting to production):

  SELECT 'fake_user_candidates' AS check, count(*) FROM users
    WHERE LOWER(name) IN ('test', 'demo', 'fake', 'admin', 'john doe', 'jane doe')
       OR email ILIKE '%test%' OR email ILIKE '%demo%' OR email ILIKE '%example.com';

  SELECT 'fake_listing_candidates', count(*) FROM listings
    WHERE LOWER(industry_label) LIKE '%test%' OR LOWER(industry_label) LIKE '%demo%'
       OR source_url ILIKE '%example.com%' OR source_url ILIKE '%localhost%';

  SELECT 'fake_partnership_candidates', count(*) FROM partnerships
    WHERE LOWER(name) IN ('test partnership', 'demo', 'sample');

Any non-zero count is a launch blocker until investigated. Real users
may sometimes use the word "test" — manual review required, not
auto-delete.

If a seed/demo dataset is needed for the May-30 demo, it lives in
STAGING only. The demo presents the live production state, not a
canned dataset.
```

---

## 29. Authorization coverage map, supply-chain audit, and security ownership

**Abro context:** The §5 matrix is the source of truth. A living coverage map proves every row has matching code + tests. Supply chain extends the dependency audit to scripts that run at install/build time. Ownership clarifies who reviews each category before commit/deploy.

**Authorization coverage map (living table):**

```
Maintain docs/authz-coverage.md. Every row of the CLAUDE.md §5 matrix
gets a row in this table with file:line references. Updated on every
feature that touches authz.

| Matrix row                       | Server action / handler        | RLS policy                       | Test                                            | Last reviewed |
|----------------------------------|--------------------------------|----------------------------------|-------------------------------------------------|---------------|
| Own profile read                 | app/actions/profile.ts:135     | users_select_self                | app/actions/profile.test.ts (auth path)         | 2026-05-19    |
| Other user public                | app/actions/profile.ts:176     | users_select_authenticated       | app/actions/profile.test.ts (public payload)    | 2026-05-19    |
| Listings list                    | app/actions/listings.ts:117    | listings_select_authenticated    | app/actions/listings.test.ts                    | 2026-05-19    |
| ...                              | ...                            | ...                              | ...                                             | ...           |
| /admin/* not-admin               | lib/auth.ts:requireAdmin       | (404 via notFound)               | (manual smoke-test; pre-launch)                 | 2026-05-19    |

Audit prompt to keep it current:

  For every authorization matrix row, find:
    1. The server action / route handler that owns the operation.
    2. The RLS policy (or "none — service role bypass" if applicable).
    3. The test file that proves unauthorized callers fail.
    4. The date of last review.
  Any row missing a test is a HIGH finding. Any row whose last review
  date is > 30 days old gets re-reviewed.

Run weekly; flag drift.
```

**Supply-chain script audit prompt:**

```
Extends §12 dependency audit to scripts that run at install / build /
deploy time. These scripts can read env vars and write to the file
system — same blast radius as a malicious dependency.

1. package.json scripts:
   - `postinstall`: should be absent (none in Abro v1; verify). Any
     postinstall script is a finding — review what it does + who
     contributed it.
   - `prepare`: only `scripts/install-hooks.sh` (read the source;
     should be a husky-style git hook install, nothing more).
   - `prepublish`, `prepack`: absent (Abro is not a published package).

2. Direct + transitive scripts:
     npx --yes can-i-ignore-scripts
   Output enumerates every package whose install runs a script. For
   each one not in a known-trustworthy allowlist, evaluate.

3. GitHub Actions: every workflow under .github/workflows/ — verify:
   - `secrets` used only by trusted actions (pin SHA, not @v3 tag).
   - No `pull_request_target` triggers with checkout of untrusted
     branch + secret access.
   - `permissions:` is read-only by default; write only where needed.

4. Vercel build command: in vercel.json (or the Vercel dashboard),
   confirm the build command is `next build` (or equivalent). NOT a
   shell that pipes from `curl ...`.

5. Railway start command + build command: same posture as Vercel —
   no `eval`, no `curl | bash`.

6. .env files in scripts: every script under /scripts/ that reads
   process.env must declare the var explicitly in a comment header.
   `process.env.SUPABASE_SERVICE_ROLE_KEY` reads in a casual ad-hoc
   script are a leak risk if the script gets shared / Slack-pasted.

7. Lockfile audit:
     npm ci --dry-run
   No deviation from package-lock.json. Any deviation triggers an
   audit of what changed.
```

**Security ownership checklist:**

```
Before each commit (Backend / Frontend / QA / DevOps lanes):
  - Backend:    §1, 4, 5, 13, 16, 17, 18, 19, 25
  - Frontend:   §6, 7, 14, 27
  - QA:         §4 (matrix tests), §19 (hostile-client), §28 (drills)
  - DevOps:     §2, 9, 11, 20, 21, 22, 23, 24
  - Security Analyst (review-only, blocks deploy on Critical/High):
                §14 Master Review covering all above; plus §17, 18,
                26, 29 specifically
  - Codex (independent cross-check at end of day): same as
                Security Analyst — independent pass

For each commit, the author runs the relevant lane's sections via the
Master Security Review (§14) and logs the SELF entry in
docs/codex-daily-review-log.md.

For each deploy:
  - DevOps: §15 Pre-Launch Prompt + §20 + §21 + §28
  - Security Analyst: independent §14 + §15 on the cut diff
  - Both must approve before promoting the deploy
  - Codex cross-check log reviewed for any outstanding Critical/High

For each incident:
  - On-call (currently Abenezer): §28 drill playbook
  - Post-incident: write up in docs/codex-daily-review-log.md as an
    `INCIDENT` entry with severity, scope, root cause, mitigation,
    resolution, postmortem. Cross-reference any §14 categories that
    should have caught it earlier.
```
