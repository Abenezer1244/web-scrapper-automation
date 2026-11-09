> **DRAFT, requires counsel review. Not legal advice. Not published.**

# BridgeLeads Cookie Notice (draft)

**Draft date:** 2026-11-09.

## Drafting notes (remove before publishing)

- The website and its cookies live in the frontend repository, which is not in this worktree. The facts below
  come from the audit's live check of bridgeleads.io on 2026-11-04 (`01-EXECUTIVE-REPORT-AND-RISK-REGISTER.md`
  P1-8 and area 6, `02-DATA-PROCESSING-INVENTORY.md` row 6, `03-VENDOR-REGISTER.md`) and from
  `docs/BUILD_JOURNAL.md`. Before publishing, an engineer must re-run a live cookie inventory (desktop and
  mobile, signed out and signed in) and reconcile it with the table in section 2.
- **Do not publish this notice while it contradicts the site.** On 2026-11-04 Google Analytics set its cookies
  on the first page view, before any choice, and did not honor Global Privacy Control. The landing-page banner
  said "We use cookies to keep you signed in", which was not accurate. Either fix the site first (audit P1-8)
  or describe what the site actually does.
- `[OPEN: owner decision B1-10, opt-in consent banner or a GPC-respecting opt-out.]` Section 3 is written for
  the opt-out model and must be rewritten if the owner chooses opt-in.

---

**Last updated:** `[OPEN: publication date]`

## 1. What this notice covers

This notice explains the cookies and similar technologies used on bridgeleads.io and in the BridgeLeads app.
It is part of our Privacy Policy.

## 2. What we use

| Name | Provider | Type | Purpose | Lasts | Set when |
|---|---|---|---|---|---|
| `_ga` | Google Analytics 4 | Analytics cookie | Tells visits from the same browser apart | `[OPEN: confirm, Google default is 2 years]` | `[OPEN: today on the first page view; after the fix, only if not opted out]` |
| `_ga_CGXBSL5TJN` | Google Analytics 4 | Analytics cookie | Keeps session state for our Analytics property | `[OPEN: confirm]` | As above |
| Sign-in session cookie `[OPEN: cookie name(s)]` | BridgeLeads | Strictly necessary | Keeps you signed in to the app. The sign-in tokens are held server side, not in page scripts. | `[OPEN: lifetime]` | When you sign in |
| Vercel Web Analytics and Speed Insights | Vercel | Cookieless measurement | Counts page views and measures page speed without cookies | Vercel keeps a daily hash for 24 hours | Every page view |
| `[OPEN: any other cookie or local storage item found by the live inventory]` | | | | | |

What we send to Google Analytics: a client id, the page address with query strings removed, the referring
site (origin only), page title, device and browser type, and approximate location derived by Google. We do not
use advertising or session-replay tools. `[OPEN: confirm Google Signals and enhanced measurement are off, and
the Analytics data retention setting (audit 03, Google row).]`

Our privacy request pages are excluded from analytics.

Payments are taken on pages hosted by Stripe. Stripe's own cookie notice applies there. `[OPEN: confirm]`

## 3. Your choices

- **Global Privacy Control.** `[OPEN: once built]` If your browser sends a Global Privacy Control or Do Not
  Track signal, we do not load Google Analytics.
- **Browser settings.** You can block or delete cookies in your browser. Blocking the sign-in cookie will stop
  you from signing in to the app.
- **Google's opt-out.** Google offers a browser add-on that stops Google Analytics from collecting data.
  `[OPEN: link]`

`[COUNSEL: whether any visitor population we serve requires opt-in consent before analytics cookies (for
example if EU or UK visitors or customers are accepted, owner decision B1-9), and whether California
wiretap-style claims about analytics tags call for consent; audit P1-8.]`

## 4. Changes

We will update this notice when our use of cookies changes.

## 5. Contact

privacy@bridgeleads.io
