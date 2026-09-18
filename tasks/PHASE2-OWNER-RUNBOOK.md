# BridgeLeads Phase 2 — the eight remaining items, as mechanical steps

Date: 2026-09-17. Companion to `BRIDGELEADS-COMPLIANCE-AUDIT-PHASE1.md`.

Already shipped today: **BE #334** (`4d2b926`) and **FE #152** — access-log secret
scrub, owner names out of scraper logs, `<main>` + skip link, footer Sign in.
Both merged, deployed and verified live.

Everything below is blocked on access I do not have (Cloudflare, Railway, Stripe,
Tracerfy) or on a decision/legal text that is not mine to write. For each item:
what I established, the exact step, and who must do it.

---

## 1. A mailbox that works — DO THIS FIRST

**This is worse than the audit said, and it is now customer-facing.**

Verified by DNS today:

| Record | bridgeleads.io | Consequence |
|---|---|---|
| Nameservers | `reese/lola.ns.cloudflare.com` (Cloudflare) | you control DNS, so this is a dashboard fix |
| **MX** | **none** | the domain cannot receive mail at all |
| **SPF (TXT at apex)** | **none** | outbound mail has no SPF; more spam-foldering, and the domain is spoofable |
| **DMARC** (`_dmarc`) | **none** | no policy, no visibility |
| Resend DKIM (`resend._domainkey`) | **present** | sending is DKIM-signed, so this was set up once and left half-done |

The app sends from `leads@bridgeleads.io` and sets
**`Reply-To: support@bridgeleads.io`** (`src/config/settings.py:201,211`), and prints
that support address in email footers. With no MX:

- every customer who replies to **any** BridgeLeads email gets a bounce
- `privacy@`, `security@`, `legal@bridgeleads.com` in the published policies are
  undeliverable **and** on a parked domain that is not yours (`v=spf1 -all`,
  NameBright nameservers)

So one root cause breaks the DSAR channel, the vulnerability-report channel, the
legal-notice channel, and ordinary customer support.

**Step (you, Cloudflare dashboard, ~10 minutes):**
1. Enable **Cloudflare Email Routing** on `bridgeleads.io` — it is free and adds the
   MX records for you. Route `support@`, `privacy@`, `security@`, `legal@` to an
   inbox you actually read.
2. Add **SPF** and **DMARC**. Take the exact SPF value from the Resend dashboard for
   this domain rather than guessing — Resend's setup differs depending on whether you
   verified the apex or a sending subdomain. Start DMARC at `p=none` so you get
   reports without risking delivery.
3. **Care on ordering:** if Resend verified the *apex* rather than a subdomain, adding
   apex MX for Email Routing can interact with its setup. Check Resend's domain page
   after enabling routing and confirm the domain still shows verified.
4. Decide separately whether to acquire `bridgeleads.com` (parked at NameBright, so
   likely purchasable) or to move the policy contacts to `@bridgeleads.io`. **Until
   this is done, correcting the policy text is pointless** — which is why this is
   item 1 and item 4 is later.

**Then tell me** and I will update the contact addresses in the policy pages as a
mechanical find-and-replace, once they point somewhere real.

---

## 2. Retire the Tracerfy path-secret route

Mitigated but not remediated. #334 stops the secret reaching **new** access-log
lines; it is still in historical logs, and any proxy in front of the app logs the URL
independently.

**Order matters. Doing step 3 first breaks live skip-trace delivery.**

1. **You (Tracerfy dashboard):** repoint the webhook to
   `POST https://api.bridgeleads.io/webhooks/tracerfy` and set header
   `X-Tracerfy-Webhook-Secret`. Non-breaking — the header is already authoritative
   when present (`src/api/routes/webhooks.py:181-182`), so both routes work during
   the switch.
2. **You (Railway):** rotate `TRACERFY_WEBHOOK_SECRET`. Required regardless of
   anything I did: the old value is already in logs.
3. **Me:** delete the legacy route. One-line PR, ready on your word.

**Verification that the migration actually completed** (run before step 3): the
legacy route is the only one that can be hit without the header, so if you want proof
rather than assumption, ask me to add a one-line warning log when the path route is
used. Then a quiet log for 24h means Tracerfy has fully moved over. I did not add
this pre-emptively because it is a code change you have not asked for.

---

## 3. California data-broker registration

**Owner + counsel only. I am not qualified and will not advise.**

What the repo already says, which is the useful part: your own
`docs/legal/PRE-LAUNCH-LEGAL-CHECKLIST.md` item 1 is a 🔴 block titled
"Data-broker registration — DO BEFORE SELLING", naming the **California Delete Act
(SB 362, CPPA, annual, deadline Jan 31)**, **Vermont 9 V.S.A. §2446**, **Oregon** and
**Texas SB 2105** — and every box is unticked, including "Engage a privacy attorney
to confirm data-broker status (it is very likely 'yes')".

The live privacy policy §5 currently asserts "Where required, we register as a data
broker." Until registration happens that sentence is a published claim your own
checklist says is not true. That is the exposure, and it is why this ranks high.

---

## 4. The nine live placeholders

**Counsel writes the text. I will not.** What I can do is remove the hunting, so
below is every placeholder with its exact location and what kind of value belongs
there. Source drafts: `docs/legal/PRIVACY-POLICY-DRAFT.md`,
`TERMS-OF-SERVICE-DRAFT.md` (both still headed "DRAFT — NOT LEGAL ADVICE").

**Privacy Policy** (`bridgeleads-web/app/(marketing)/privacy/page.tsx`):

| Placeholder | Where | Value needed |
|---|---|---|
| `[Legal entity name]` | opening paragraph | registered company name |
| `[retention period]` | §7, security logs | a duration; note `audit_events` currently has no purge, so pick one you can honour |
| `[country]` | §10 | country of establishment |
| `[legal entity name]`, `[mailing address]` | §11 contact | entity + postal address |

**Terms** (`app/(marketing)/terms/page.tsx`):

| Placeholder | Where | Value needed |
|---|---|---|
| `[LEGAL ENTITY NAME]` | **§10 Limitation of Liability** | entity — the clause caps liability for nobody until filled |
| `[legal entity name]` | **§11 Indemnification** | entity — the indemnity runs to nobody until filled |
| `[state / country]` | **§13 Governing Law** | chosen governing law |
| `[Dispute resolution / arbitration / venue clause to be set by counsel.]` | **§13** | the clause itself — this is a visible note to your lawyer, published |
| `[legal entity name]`, `[mailing address]` | §14 contact | entity + postal address |

Two corrections counsel should also make, which are factual not legal:
- §3 lists record type `eviction`, which has no connector, and **omits
  `death_certificate` and `trustee_sale`, both live** (`src/config/constants.py:175`).
- §6's sub-processor list omits **2Captcha** (`src/scrapers/enrichment/captcha.py`)
  and **PhoneBurner** (`src/workers/dialer_connectors/phoneburner.py`), and asserts a
  DPA with all ten named vendors while checklist item 4 ("Sign Data Processing
  Agreements with…") is unticked.

---

## 5. Retention: the decision, then the code

Facts, re-verified on current `origin/main`:
- `_purge_old_records_impl` deletes only `county_records` and
  `property_list_membership` (`src/workers/scheduler_helpers/county.py:75,79`).
- `results` — owner names, addresses, skip-traced phones/emails — is deleted by **no
  scheduled job**.
- Neither app role holds `DELETE ON results`
  (`scripts/_cutover_step2_grants_policies.py`), so a purge added today fails with
  `InsufficientPrivilege` until a grant changes.
- But `results.user_id` is `ondelete="CASCADE"` (`src/db/models.py:762`) and
  `models.py:514` says "user_id keeps a direct users FK for user-delete" — the data
  model is already built for deletion.
- `SKIP_TRACE_CACHE_DAYS = 90` is a reuse TTL, not a deletion; the repo says so
  itself: *"the 90-day TTL only fires on read"*
  (`scripts/purge_skip_trace_cache.py`).

**Your decision, and it determines the code:**

- **(a) Honour the policy.** I add a beat task purging `results` past 365 days, plus a
  grant migration. Irreversible data loss by design, so it wants a dry-run mode and a
  staged rollout. Note customers lose access to old leads they paid for — that is a
  product decision, not just a privacy one.
- **(b) Amend the policy** to describe what you actually do (retain lead records;
  cache reuse expires at 90 days). Cheapest, honest, and still needs counsel.
- **(c) Split it.** Keep lead rows, purge only the skip-traced contact fields
  (phone/email) past 365 days. Preserves the product's value while retiring the most
  sensitive data. My recommendation if you want to keep the promise.

**DECIDED 2026-09-17: (c).** Built, and shipped OFF. What you still have to do:

**5a. Clear the GitHub Actions billing block.** Settings -> Billing & plans. Every
CI job since ~10:24 UTC on 2026-09-17 fails in ~1s without starting, so NONE of
this is test-verified. It is lint-clean and import-verified locally only.

**5b. Turn it on in two steps, not one.** In Railway, set
`RETENTION_PURGE_ENABLED=true` and LEAVE `RETENTION_PURGE_DRY_RUN=true`. The daily
04:10 UTC task then logs exactly what it WOULD delete and writes nothing. Read
those counts. Only then set `RETENTION_PURGE_DRY_RUN=false`. The deletion is
irreversible; there is no undo.

**5c. Add the R2 lifecycle rule** (Cloudflare dashboard, minutes). You chose belt
AND suspenders. The code sweep is the suspenders; the lifecycle rule is the belt,
and it is the ONLY thing that catches the export race -- a job that read the
contact data just before the purge committed and uploads the file just after.
Check whether the bucket has object VERSIONING on: if it does, deleting the
current object leaves prior versions readable, and no code can fix that.

**5d. Counsel question (D1), still open.** The clock runs from
`skip_trace_attempted_at` -- "retain each newly obtained copy 365 days". A
re-traced row resets its own clock and can stay populated indefinitely. That is
right under that reading and WRONG if §7 means "365 days after the lead was
created". Ask counsel which. Changing it later is a one-line predicate change.

**5e. §7 wording.** §7 promises deletion of "lead records". We now delete the
personal data inside them and keep the row. That is a gap between the text and
the behaviour even after this ships. Counsel item, not a code item.

**5f. Add the four new settings to `.env.example`** (deny-ruled in my environment,
so I could not): `RETENTION_PURGE_ENABLED`, `RETENTION_PURGE_DRY_RUN`,
`SKIP_TRACE_PII_RETENTION_DAYS`, `SKIP_TRACE_CACHE_RETENTION_DAYS`,
`EXPORT_RETENTION_DAYS`, `RETENTION_PURGE_BATCH`.

**Correction to the evidence cited above.** `scripts/purge_skip_trace_cache.py` is
a ONE-TIME script from the 2026-06-10 per-tenant cutover that does an unfiltered
`DELETE FROM skip_trace_cache`, not a retention mechanism. It also could not have
run since the RLS cutover: `bridgeleads_system` held no DELETE on that table until
the grant added here. Nothing has ever deleted a cache row on a schedule, and rows
past the 90-day reuse window -- including `raw_response`, the full Tracerfy payload
-- have been accumulating since the table was created.

Plan and phase detail: `tasks/todo-retention-purge.md`.

---

### 5g. The exact commands, in the only order that works

**Reordered after a Codex review OF THIS RUNBOOK. Three steps below were missing
or in the wrong place, and each would have produced a silent failure.**

**1. Clear the Actions billing block.** github.com -> Settings -> Billing and
plans. Nothing else here can be verified until CI can run.

**2. Get counsel's answer FIRST.** Send
`docs/legal/COUNSEL-BRIEF-retention-2026-09-17.md`. It asks three questions and
drafts no policy text. Irreversible deletion should not begin before the reading
it implements is confirmed, and the answer can change the predicate.

**3. Get BE #336 green and merge it.** `gh pr checks 336` must pass on its own. Do
NOT use `gh pr merge --auto`: it gates only on REQUIRED checks and this repo has
none (see 5h). Merging `main` deploys.

**4. RE-RUN THE GRANTS. Nothing does this on deploy.** (Codex, High - this step
was missing entirely.) The sweep DELETEs `skip_trace_cache`, and that grant is new
in this change. Deploy runs `alembic upgrade head` and nothing else, so without
this the sweep fails with `InsufficientPrivilege` - which is exactly how this repo
stranded 16,761 dedup claims on `delivered_records`, silently.

    railway run --service worker bash -c "PYTHONPATH=. python scripts/_cutover_step2_grants_policies.py"

**5. Run the preflight. One command, and it gates everything above.**

    railway run --service worker python scripts/verify_retention_ready.py

Read-only, changes nothing, exits non-zero if anything is wrong. It checks the
four things a deploy does NOT do for you and that all fail silently: migration
096's index exists AND is valid (`CREATE INDEX CONCURRENTLY` can fail partway and
leave an INVALID index, and the sweep will happily run without it), the
`skip_trace_cache` DELETE grant, the `results` UPDATE grant, the beat entry being
registered in the deployed worker, and the R2 lifecycle rule. It also prints how
many rows are actually waiting, so step 7's dry-run number has something to be
checked against.

Do not proceed while it says `NOT READY`.

**6. Apply the R2 lifecycle rule BEFORE enabling the purge.** (Codex, High - it
used to be last.) The rule is the only thing that catches an export uploaded
moments after a purge commits, so enabling deletion first leaves that race open
for the whole interval between the two steps.

    railway run --service worker python scripts/set_r2_lifecycle.py
    railway run --service worker python scripts/set_r2_lifecycle.py --apply --yes-bucket bridgeleads-exports

It MERGES with any existing rules rather than replacing them, prints before and
after, and refuses unless `--yes-bucket` matches the configured bucket. A success
means the rule is STORED, not that anything is deleted: R2 applies lifecycle
asynchronously and existing objects can take over 24h.

**7. Dry run. Writes nothing.**

    railway link                      # this worktree is not linked
    railway variables --service worker --set RETENTION_PURGE_ENABLED=true
    railway variables --service worker --set RETENTION_PURGE_DRY_RUN=true

The task runs daily at 04:10 UTC; to see it now rather than waiting:

    railway run --service worker python -c "from src.workers.scheduler import purge_skip_trace_pii; purge_skip_trace_pii()"

Read the line beginning `retention purge DRY RUN (nothing written)`. It reports
every leg: results rows, cache rows, provider links, and export objects.

**8. Read the counts, then enforce.** Sanity-check them against what you expect the
business to hold. Only then:

    railway variables --service worker --set RETENTION_PURGE_DRY_RUN=false

**9. Know the kill switch before you need it.** `RETENTION_PURGE_ENABLED=false`
stops the sweep at the next tick. It does NOT bring anything back - the database
purge, the link nulling and the R2 expiry are all irreversible. Watch the first
enforced run for `retention purge INCOMPLETE`, the `past retention but sat in
queued/submitted` line, and the `completed with NO completed_at` anomaly line.

**Codex's standing objection, recorded rather than argued away:** it rates
unsupervised owner execution of this - no CI, no test ever run - as Critical, and
recommends a second reviewer and a verified database backup before step 8. That
judgement is the owner's to make, but it should be made knowingly.

### 5h. `Test` CANNOT be made a required check

Runbook item 2 and the handoff both say to make `Test` required. **It is not
possible on this account.** `web-scrapper-automation` is a PRIVATE repo on a free
personal plan, and both the branch-protection and rulesets APIs return:

> `403 Upgrade to GitHub Pro or make this repository public to enable this feature.`

So the guardrail whose absence let a red build onto `main` cannot be switched on.
Either upgrade to GitHub Pro, or accept that the only defence is procedural:
always `gh pr checks <n>` and wait, never `--auto`.

---

### 5i. `.env.example` additions (blocked for the agent, 30 seconds for you)

`.env.example` is covered by a tooling deny rule in the agent environment, so
this block was NOT written by the agent and must be pasted by hand. Append:

```
# ─── Skip-trace PII retention (Privacy Policy §7) ───────────────────────────
# Ships OFF. Deletion is irreversible; roll out as ENABLED=true + DRY_RUN=true,
# read the logged counts, then DRY_RUN=false. See tasks/todo-retention-purge.md.
RETENTION_PURGE_ENABLED=false
RETENTION_PURGE_DRY_RUN=true
# Days before skip-traced contact PII is cleared off a lead row (the lead itself
# is kept). Clock runs from skip_trace_attempted_at -- see decision D1/D4.
SKIP_TRACE_PII_RETENTION_DAYS=365
# Cache rows are unusable past the reuse window and hold the full vendor payload,
# so they are deleted at the reuse window rather than at 365 days.
SKIP_TRACE_CACHE_RETENTION_DAYS=90
# Tracerfy CDN links need no auth. A completed queue drops its link after this
# many days; pending/errored keep theirs until the PII window, since a paid batch
# that was never applied is recovered by hand from it.
SKIP_TRACE_LINK_RETENTION_DAYS=30
# Max age of delivered export objects in R2. Must match the lifecycle rule set by
# scripts/set_r2_lifecycle.py.
EXPORT_RETENTION_DAYS=365
# Rows per batch in the retention sweep.
RETENTION_PURGE_BATCH=1000
```

---

## 6. Annual billing disclosure

Stripe charges annual plans **upfront for 12 months**: Pro **$1,910**, Business
**$4,790**, Agency **$14,390** (`scripts/stripe_pricing_migration_2026_06.py:37-41`,
recorded in `docs/stripe-prices-2026-06.md`). The pricing page's annual toggle shows
only "$159/mo" with no total and no statement of billing frequency.

The 20% discount maths is honest. The gap is that a buyer cannot see the amount that
will hit their card, which is exactly what California's automatic-renewal rules are
strict about.

**You:** confirm those three totals are current in Stripe, and decide the copy
(showing "$1,910 billed annually" alongside the per-month figure is the normal
pattern). It is user-facing commercial copy, so I am not writing it unprompted — but
once you give me the wording it is a small, contained change.

---

## 7. Confirm FOUNDING25 is redeemable

`allow_promotion_codes=True` (`src/api/routes/billing.py:1137`) accepts only a Stripe
**PromotionCode** object, not a raw coupon id. The script that creates one,
`scripts/stripe_founding_code_and_webhook_events.py`, **is now merged on main** — but
a merged script is not a run script.

**You, in Stripe:** confirm an **active PromotionCode with code `FOUNDING25`** exists
on the `FOUNDING25` coupon, with `customer = null`. If it does not, nobody can redeem
the code your homepage advertises. Separately: the coupon has
`max_redemptions = 25`, and the "LIMITED SPOTS" banner is hardcoded
(`_monopo/Pricing.tsx`) while the backend already computes `spots_remaining`
(`billing.py:299-358`) — so the banner will keep advertising a dead code. Wiring the
banner to the real number is a small change I can make on request.

---

## 8. Confirm ENABLE_DAILY_SCRAPE

**You, Railway worker service:** check the `ENABLE_DAILY_SCRAPE` env var.

`settings.py:309` defaults it to `False` and `scheduler_helpers/county.py:25-26`
returns immediately without it — but **a code default is not the production value**,
and I withdrew this as evidence in the audit after this project's own landmine
(reading staged flag defaults as prod state produced a false P0 twice). This is a
one-line confirmation, not a finding.

Command: `railway variables -s worker | grep ENABLE_DAILY_SCRAPE`

If it is unset or false, then the coverage page's "Every county below is scraped
daily" is straightforwardly untrue and needs copy work. If it is true, the claim is
still **overstated** for two independent reasons that stand on their own: per-customer
scrapes run on each config's own frequency, and `health_status` is a weak proxy
(trustee_sale rows are seeded `healthy` with no probe, migration `081:41-44`).

---

---

## Codex cross-check (agreed on all 8, added two things)

Codex reviewed this triage independently and **agreed with every blocked/not-blocked
verdict**. Three substantive additions:

**1. A latent trap around `audit_log` — verified, and it changes how §2's telemetry
must be built.** `audit_log()` (`src/api/middleware/security.py:567`) writes
`request.url.path` both to the log line (`:585`) and into the **`audit_events`
database table** (`:596` → `:551`) — a table that is append-only to the app role and
has **no purge job**. My #334 scrub covers only the uvicorn access log, so it would
not help here.

I verified the exposure: `audit_log` has **27 callers, none in
`src/api/routes/webhooks.py`**, so the Tracerfy path secret does **not** reach
`audit_events` today. It is a trap, not a live leak. But the moment anyone adds an
`audit_log` call to that route — an obvious thing to want for auth failures — the
secret is silently persisted somewhere the app cannot even read it back to delete.
So the §2 hit-telemetry must emit a **fixed, secret-free event name** and must never
pass the path.

**2. §2 has a safe intermediate I had not proposed.** Add
`TRACERFY_LEGACY_PATH_ENABLED`, defaulting to enabled for compatibility. When
disabled, the legacy route returns **410 before secret validation or ingestion**.
Emit `tracerfy_legacy_route_hit` (no path). Sequence becomes: you repoint Tracerfy →
disable the flag → rotate the secret → observe zero legacy hits → I merge the route
deletion. That gives you a reversible kill switch before the irreversible delete.

**3. §5's correct shape, if you pick (a) or (c).** Codex was specific and it matches
the grant posture:
- a recurring **Celery Beat task under `bridgeleads_system`**, not an ops script (a
  script is right only for a backfill or a purge rehearsal, never for enforcement)
- a **narrowly scoped `DELETE` grant on `results` for the system role only** — keep
  `bridgeleads_app` with no delete
- add `results` to the worker-delete verification allowlist
  (`scripts/_cutover_step2_grants_policies.py:107-121`) so the grant cannot drift
  silently, which is the failure this repo has already had once
- delete in **bounded per-user batches** with `user_id` + `created_at` predicates
- **check the index**: `alembic/versions/068_results_user_created_index.py:22-25` is
  **partial to non-duplicates**, so a full `(user_id, created_at)` index may be needed
- **scope must include** the skip-trace cache, queue metadata, and **R2 export
  objects** — and note `src/utils/data_exporter.py:218-254` has upload and download
  but **no delete method at all**, so R2 deletion has to be built from scratch

**Order:** Codex endorsed mailbox-first but moved the two one-minute production checks
(§8, §7) ahead of the expensive decisions. Adopted below — verify cheap facts before
committing to work that depends on them.

**One number to correct in my favour and against it:** Codex counted 19 bracketed
occurrences in the Privacy *draft* and 10 in the Terms draft. My "nine" counts the
**published pages**, which I fetched live. Both are right about different artefacts;
the worksheet in §4 is built from the published copies, which is what Codex also
recommended.

---

## Recommended order

Revised after the Codex cross-check — cheap production checks first, because they
cost a minute each and change what the later work has to say.

1. **Mailbox** (§1) — nothing else in privacy means anything while the channel is
   dead, and it is breaking ordinary support replies today. ~10 min, Cloudflare.
2. **Tracerfy migration + rotation** (§2) — a live credential is in historical logs.
3. **`ENABLE_DAILY_SCRAPE` check** (§8) — one command.
4. **FOUNDING25 check** (§7) — one dashboard look; report-only.
5. **Retention decision** (§5) — unblocks the largest remaining engineering item;
   (c) is my recommendation.
6. **Data-broker registration** (§3) — needs counsel and your authority; hard Jan 31
   deadline, and the policy already claims it is done.
7. **Counsel fills the placeholders** (§4) — after §1, so the contacts are real.
8. **Annual disclosure** (§6) — lowest legal risk, once the Stripe facts are confirmed.

## What I can start the moment you answer

- §5 with a decision → the purge task + grant migration, or the policy amendment text
  for counsel
- §2 step 3 → the route deletion PR
- §2 verification → the one-line "path route was used" warning log
- §7 → wire the LIMITED SPOTS banner to `spots_remaining`
- §1 follow-up → find-and-replace the policy contact addresses once they resolve
