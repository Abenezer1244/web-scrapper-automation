# Remaining compliance engineering (owner: "do all the engineering items yourself with codex", 2026-11-09)

Four items, each with a Codex consult before code and a Codex review after (any Critical/High
= NO-GO). Max 5 files per commit, ruff clean, tests on TEST_DATABASE_URL.

| Item | Where | State |
|---|---|---|
| R. Privacy-request retention purge (D5) | this file, branch `feat/privacy-retention-purge` | planned |
| 3d. Terms/Privacy re-acceptance + enforcement | this file, branch `feat/legal-reacceptance` (BE) + FE | planned |
| P9. DROP ingestion | after a research pass on the official DROP spec | research running |
| 7. Legal document drafts | `docs/legal-drafts-2026-11` worktree | drafting agent running |

## R. Retention purge (design section 11, D5: owner default pending counsel)
D5: request records 24 months; requester contact deleted 24 months after close; suppression
rules kept indefinitely.
- "Closed" = the request's `closed` or `withdrawn` event. The clock runs from that event.
- Migration 138: `privacy_retention_purge(p_limit)` SECURITY DEFINER (privacy tables' owner,
  search_path pinned), EXECUTE to bridgeleads_system only, bounded batch:
  - a request closed >= 24 months ago that NO suppression rule references: delete it
    (payload, events, tokens, outbox go with it: ON DELETE CASCADE);
  - one that a rule references (125's FK RESTRICT keeps the paperwork while the rule lives):
    delete only its payload (the contact and details). The envelope keeps only the email's
    keyed blind index, and its events carry no personal data.
  - returns counts only.
- The window is a constant in the function, not a parameter (a caller cannot shorten or
  lengthen it); changing it is a migration, with counsel.
- Beat: daily, counts-only log; the existing deadline task is a natural neighbour.
- Tests: closed 25 months ago with no rule -> gone; with a rule -> payload gone, envelope and
  rule kept; closed 23 months ago -> kept; open (never closed) -> kept; roles.
- Not in scope: requests that are never closed. The deadline alerts force a decision; an
  operator closes them.

## 3d. Re-acceptance of Terms / Privacy (audit Phase 3d)
Today signups record acceptance (migration 123, `legal_acceptances`); existing users and
version bumps have no path. Defaults below are engineering's, marked [D] for owner/counsel.
- BE:
  - `GET /auth/legal` -> `{required: bool, documents: {terms: {current, accepted}, privacy: ...}}`
    for the signed-in user. "Accepted" means a row for EXACTLY the current version (a row for
    PREVIOUS_PRIVACY_VERSION does not count, constants.py says so).
  - `POST /auth/legal/accept {accepted_terms_version, accepted_privacy_version}` records rows
    with flow 'reaccept', only for exactly-current versions (same rule as signup), idempotent.
  - `LEGAL_ACCEPTANCE_REQUIRED` (default false): signup and verify-email refuse (422, a stable
    code) a submission without the current versions.
  - `LEGAL_REACCEPT_ENFORCED` (default false): authenticated API calls from a user who has not
    accepted the current versions get 403 `legal_acceptance_required`, EXCEPT the routes a
    person must always reach: auth/session (me, refresh, logout, legal status/accept), account
    export and account deletion (privacy rights), billing portal/cancel (consumer rights).
  - Account export includes the user's legal acceptances.
  - [D] one checkbox for both documents (as signup today); no IP or user agent stored.
- FE: a blocking dialog in the signed-in app when `required`, with links to both pages and
  one checkbox; posts through the existing authenticated BFF; then refreshes the status.
- Rollout: BE first (flags off), FE next, then the owner turns the flags on.

## Codex consult (2026-11-09): NO-GO on both as written; every finding adopted
R (retention):
- Purge only when every outbox row of the request is sent or given up and none is claimed.
- terminal_at = the occurred_at of the request's `closed`/`withdrawn` event (136's lifecycle
  allows nothing after either), never received_at or the latest event.
- A deletion request waits for `privacy_erasures.completed_at` on every rule it holds.
- Legal hold: append-only `privacy_legal_holds(request_id, action hold|release, actor, at)`,
  written only by owner-only functions with the admin+MFA check; the purge skips a request
  whose latest action is `hold`. CLI `hold` / `unhold`.
- What is kept after a purge of a request a rule references: kind, channel, received and
  deadline timestamps, deadline rules, the email's keyed blind index, its events (no personal
  data), and the rule. Documented for counsel.
- EXECUTE only; no table privilege; search_path pinned; p_limit capped; counts returned.
3d (re-acceptance):
- One gate inside the shared authenticated-user dependency, exemptions declared on the route
  (dependency marker), plus a test walking every route of the app: each authenticated route
  is gated or explicitly exempt.
- API keys: blocked like sessions until the user accepts (the gate applies to both); only a
  session (`require_session`) can record acceptance.
- Admin: no bypass by `is_admin`; admin routes are exempt only where marked.
- With LEGAL_ACCEPTANCE_REQUIRED on, signup and verify-email require the exact current pair
  (no previous-version grace); the grace stays only for the non-enforced path.
- Account export gains `legal_acceptances.json` (document, version, flow, accepted_at).
- Writer uses the RLS session; RLS regression test.
- Stale versions: 409 `legal_version_changed`, nothing recorded; the client refetches.
- Wording: "an application record of acceptance of the published versions", not proof; counsel
  decides on hashes, IP/UA, separate checkboxes [D].
- Exemptions are exact: me/refresh/logout/legal status+accept, account export + deletion
  (privacy rights), billing portal (cancel); webhooks and Celery are never user calls.

## R build reviews
- Round 1: NO-GO (High: outbox claim race between selection and delete; Low: two terminal
  events). Both fixed in a9fc3d0b, with regression tests.
- Round 2: both fixes confirmed. One Medium REJECTED with evidence: a confirm/purge
  deadlock (confirm locks token then request; purge locks request then deletes tokens).
  Unreachable: a token lives at most 30 days (privacy_outbox_token), an email that cannot
  be sent gives up within about two days (10 attempts, 6 h cap), and privacy_web_confirm's
  UPDATE locks only a live, unconsumed token, while the purge only touches requests closed
  24 months ago with no unsent email. So no live token can exist on a purgeable request;
  and a deadlock, if it ever occurred, would abort one side harmlessly (Postgres detects it).
  Changing 137's confirm (already in production) for an unreachable case is not worth a
  definition swap in 138.
