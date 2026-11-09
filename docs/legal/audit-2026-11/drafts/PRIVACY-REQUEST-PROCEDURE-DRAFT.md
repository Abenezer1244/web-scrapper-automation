> **DRAFT, requires counsel review. Not legal advice. Not published.**

# Privacy Request Procedure (draft, internal operator runbook)

**Draft date:** 2026-11-09. **Facts checked against:** backend `main` at `7aff3c76`.
**For:** the operator who handles opt-out, deletion, access and correction requests from people named in lead
records. Customer (account holder) requests are in section 11.

## Drafting notes

- **This procedure must match the code.** It follows `scripts/privacy_admin.py` (the operator CLI), the
  database lifecycle in migrations 125, 135, 136 and 137, `src/privacy/deadlines.py`,
  `src/workers/privacy_requests.py`, `src/workers/privacy_erasure.py` and `src/api/routes/privacy.py`. If any
  of those change, this document changes in the same pull request. The rules the database enforces are listed
  in section 3 so an operator knows why a command was refused.
- Owner decisions D1 to D9 (design `06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md` section 11) were adopted
  on 2026-11-05 as company policy pending counsel. This procedure applies them. Each is marked where used.
- Gaps where the code has no tool yet are marked `[OPEN]`. Do not improvise around them without writing down
  what was done in the request's record.

## 1. Before you start

- You need an account in BridgeLeads that is an **admin with multi-factor authentication on**. Every CLI
  command checks this in the database and records you as the actor.
- You need Railway access to run commands as the migration role:
  `railway run --service worker python scripts/privacy_admin.py --operator-email <you> <command> ...`
  (the script refuses without `DATABASE_URL_MIGRATE`).
- Activation needs `PRIVACY_ENFORCEMENT_VERSION` at least 1 in the environment you run from, as well as the
  code's own enforcement version (`src/privacy/activation.py`). The journal records it set to 1 on api,
  worker and beat on 2026-11-09. `[OPEN: confirm]`
- Output from `show` contains the requester's personal details. Do not paste it into chat tools, tickets or
  logs. Every other command prints only ids, kinds, dates and counts.

## 2. Where requests come from

| Channel | How it is recorded | First events |
|---|---|---|
| Web form (`/privacy/request`) | Automatically, at submission, by `privacy_web_submit()`. The deadline is computed from `src/privacy/deadlines.py` and its rules version is stored. (conditional: `PRIVACY_INTAKE_ENABLED`, off by default) | `received`, then `verification_sent` (the person) or `in_review` (an agent) |
| Email to privacy@bridgeleads.io | By the operator, with `record --channel email` | `received` |
| Post to `[OPEN: mailing address]` | By the operator, with `record --channel postal` | `received` |
| California DROP | Not built (D8). The database accepts channel `drop` but nothing uses it. | n/a |

**The clock starts when the request is received**, not when it is verified.

## 3. Rules the database enforces (migration 136 and 137)

- A request's status is its latest event. Events are append-only; nothing is edited.
- `received` is written only at intake.
- `verified` and `decided` can each happen once.
- Only `closed` may follow `decided`, and `closed` requires `decided`.
- After `closed` or `withdrawn`, nothing more can be recorded.
- Rules can be added and activated only for **opt-out and deletion** requests that are **verified** and **not
  yet decided**. Access and correction requests cannot make rules.
- A rule's anchor must be a property (parcel, county, state), a phone number or an email address. A mailing
  address anchor is refused. A rule must also name a subject: surname and given name, or an entity name.
  Name-only rules do not exist.
- A deletion rule is never lifted. An opt-out rule can be lifted.
- A request that has a rule can never be deleted, so the paperwork cannot accidentally lift a suppression.

So the order is always: **record, verify, add rule, check, activate, decide, close.**

## 4. Deadlines

| Kind | Deadline (owner default, pending counsel) | Source |
|---|---|---|
| Opt-out | 15 business days (weekends and observed US federal holidays skipped) | `deadlines.py` |
| Delete, access, correct | 45 calendar days | `deadlines.py` |

Counted from the day after receipt, in California time, ending at 23:59:59 on the last day.

**Known inconsistency, operator must compensate.** `record` does not use `deadlines.py`. It sets the deadline
to "now plus `--deadline-days`" (default 45 calendar days, in UTC) and stores no rules version. For an
emailed or posted **opt-out** you must compute 15 business days yourself and pass the calendar-day count, for
example `--deadline-days 21` for a request received on a Monday with no holiday in the window. Check the
result against `deadlines.py` before relying on it. `[OPEN: engineering fix, make record call deadline_for()
and store RULES_VERSION.]`

Extensions: `event --type extended --extend-days N` sets the new deadline to now plus N days (not the old
deadline plus N). `[COUNSEL: when an extension is allowed and what notice the requester must get.]`

Reminders: a daily beat sends ops alerts listing open requests due within 7 days, and separately within
2 days (ids, kinds and dates only). The `inbox` command shows the same list on demand, soonest first, with
extensions applied.

## 5. Step by step

### Step 1. Record (email and post only)

Web requests are already recorded. For email or post:

```
record --kind opt_out|delete|access|correct --channel email|postal \
       --deadline-days <N> --contact "<how to reach them>" \
       --requester-email <their email, if any> --details "<what they asked, matching hints>"
```

Contact and details are encrypted at rest. Record only what the person gave you and what you need to find
their records: name, property address or parcel, county.

### Step 2. Verify (D1)

| Kind | Standard (D1) | How |
|---|---|---|
| Opt-out | Email-link confirmation | Web: automatic when the person clicks the link (`verified`, level `email_link`). Email channel: `[OPEN: no link can be sent for an operator-recorded request. Either reply asking them to use the web form, or reply and treat their reply from the same address as confirmation. Counsel to decide.]` Then `event --type verified --verification-level email_link`. |
| Delete, access | Email link plus a match of name and property address against our records | Web: the click moves the request to `in_review`; it is not verified yet. The operator checks the name and property match (Step 3), then `event --type verified --verification-level <code>`. |
| Agent (any kind) | Signed permission from the person | Web: goes straight to `in_review`, no link. The operator obtains and checks the signed permission, then records `verified`. |

Verification level codes are free text in the database (lower case, digits and underscores). Only `email_link`
is used by code. `[OPEN: adopt a fixed list, for example email_link, email_link_name_property,
agent_signed_permission.]`

Never tell an unverified requester whether we hold records about them.

If verification fails or the person does not respond, decide with a denial code (Step 7). A decision without
verification is allowed for exactly this.

### Step 3. Find the person

- `show --request <id>` prints the requester's contact and details.
- To build a rule you need an anchor and a subject:
  - **Anchor:** the parcel number with county and state (preferred), or a phone number (10 US digits), or an
    email address that appears in our data.
  - **Subject:** surname and given name, or for an estate or trust the entity name (D4).
- `[OPEN: there is no CLI search of lead records by name or address. Find the parcel from the property
  address in the county's own assessor records, or ask engineering for a one-off lookup that prints ids only.]`
- **Entities (D4):** people named as an estate, trustee or heir are covered. A pure company (LLC, INC) is
  declined with a fixed code.
- **Co-owners (D2):** a rule withholds the **whole** lead row from delivery, including co-owners on that row.

### Step 4. Add the rule (opt-out and deletion only)

```
add-rule --request <id> --parcel <parcel> --county <county> --state WA --surname <surname> --given <given>
add-rule --request <id> --phone <number> --surname ... --given ...
add-rule --request <id> --parcel ... --county ... --state ... --entity "<estate or trust name>"
```

The rule is created `pending`, keyed under every live key version. Nothing is enforced yet. A request may hold
more than one rule (for example two parcels).

### Step 5. Check before activating

```
candidates --rule <rule id>
```

A dry run: how many stored leads the rule would withhold (exact match) and how many it would send to review
(possible match: address-normalised, missing parcel, same-surname co-owner, ambiguous entity). Zero is a valid
answer: the rule still prevents the person being added in future.

### Step 6. Activate

```
activate --rule <rule id>
```

What happens, in one transaction plus an immediate sweep:

- The rule becomes active. If it would make the rule set unloadable, nothing commits.
- Matching leads are flagged at once across all customers (the beat sweep is the backstop).
- Queued contact lookups for those leads are cancelled, and unsent dialer deliveries are skipped.
- From then on, every read, export, download, dialer push and contact lookup re-checks the rules before
  sending, and new scrapes and enrichment writes skip the person.
- **Deletion only:** an erasure is queued (Step 6a).

Then review what it holds:

```
held --rule <rule id>        # each held lead with WITHHELD (exact) or REVIEW (possible)
release --rule <rule id> --result <lead id>   # a REVIEW lead that is not this person
```

Only REVIEW leads can be released. An exact match cannot be released; if the rule itself is wrong, it must be
lifted (opt-out only).

### Step 6a. Deletion: erasure (D3)

Runs by itself (beat `drive-privacy-erasures`, every 10 minutes). For exact (WITHHELD) matches only, never
REVIEW:

- Lead rows: the person columns are blanked and enrichment data is cleared; a nameless property record is kept
  for billing (D3).
- Contact-lookup cache entries for the person are deleted; unsent lookups deleted; sent lookups keep only the
  property address; the vendor result-file link is cleared.
- King County owner records become tombstones; trustee-sale notices lose the grantor name; orphaned county
  records are deleted.
- Stored export files in R2 that contained the person are deleted; downloads are rebuilt from the database.
- A second pass runs at least 24 hours later, after in-flight jobs finish, and repeats if it still finds the
  person. The erasure completes only when nothing for the person is still out at the contact-lookup vendor.

`[OPEN: no CLI command shows erasure progress. Engineering to add one, or document the read-only query on
privacy_erasures (completed_at, second_pass_at).]` Do not decide a deletion request as completed until the
erasure is complete.

Vendor copies (D6): ask Tracerfy to delete where possible. **Never tell the requester that vendor data was
deleted unless Tracerfy confirms it.** `[OPEN: Tracerfy contact and contract terms.]`

### Step 7. Decide

```
event --request <id> --type decided --decision-code <code>
```

Decision codes are free text in the database. Only `suppressed` appears in tests. `[OPEN: adopt a fixed list,
for example: suppressed (opt-out done), erased (deletion done), access_provided, corrected, denied_unverified,
denied_entity (D4), denied_no_authority (agent), withdrawn_by_requester. Counsel to approve, including whether
"no records found" may be said after verification.]`

### Step 8. Reply to the requester

`[OPEN: no reply templates exist and nothing in code sends the final reply. Send from privacy@bridgeleads.io
to the verified address only. Counsel to approve templates for each decision code.]` The reply should say what
was done and what was not (customer copies, vendor copies; section 9), without listing the leads.

### Step 9. Close

```
event --request <id> --type closed
```

## 6. Access requests

No tool builds an access report. `[OPEN: engineering tool, or a written manual method. COUNSEL: categories of
information vs specific pieces; what "specific pieces" means for public-record data; whether to include which
customers received the data.]` Send the report only to the verified address. Record `decided` with an
access code, then `closed`.

## 7. Correction requests

No correction tool exists, and the database will not attach a rule to a correction request. The design's
option to suppress a single field is not built. `[OPEN: owner and counsel decide what correction means for
data copied from public records (for example: correct our copy, note the dispute, or offer an opt-out).]`

## 8. Other lifecycle events

- **Withdrawn by the requester:** `event --type withdrawn`. Ends the request. Active rules stay active.
- **Lifting an opt-out** (for example, the person asks to be included again): `lift --rule <rule id>`. Its
  leads are re-decided (another rule may still hold them). Deletion rules cannot be lifted.
  `[COUNSEL: what proof to require before lifting.]`
- **Status at any time:** `status --request <id>` (events and rules), `inbox` (all open requests).

## 9. What we can and cannot promise (design section 2)

Once a rule is active, no new scrape row, enrichment write, export file, download, results response, dialer
push or contact-lookup submission will contain the person. We cannot guarantee:

1. a send already in flight at the moment of activation;
2. copies customers already downloaded or pushed to their own tools;
3. data the contact-lookup vendor already holds.

Customer notification (D7): none until counsel confirms a basis.

## 10. Records

| What | Where | Kept (D5) |
|---|---|---|
| Request envelope, events | `privacy_requests`, `privacy_request_events` | 24 months after close `[OPEN: no job]` |
| Requester contact and details | `privacy_request_payloads` (encrypted) | Deleted 24 months after close `[OPEN: no job]` |
| Rules | `privacy_suppressions` | Indefinitely |
| Confirmation tokens | `privacy_request_tokens` (hash only) | 30 days after expiry |

Once the contact record is gone, `show` answers "no such request, or its payload was deleted (retention)".

## 11. Requests from customers (account holders)

These use the product, not this CLI:

- Access and portability: Settings, account export (ZIP, 7-day link). (conditional: `ACCOUNT_EXPORT_ENABLED`)
  The export does not include audit or IP history, sessions or billing (audit P2-7). `[COUNSEL: whether that
  is enough.]`
- Deletion: Settings, account deletion (removed after 30 days; can be cancelled meanwhile). (conditional:
  `ACCOUNT_DELETION_ENABLED`)
- Promotional email: one-click unsubscribe.
- If a customer emails privacy@ instead: `[OPEN: written steps for the operator, or point them to Settings.]`

A customer who is also named in lead records (for example as a property owner) is handled under both
procedures.

## 12. Escalate to counsel

- Any request from a lawyer, regulator or court.
- A request you cannot verify but that alleges harm or danger (for example a stalking or abuse victim).
- A request that would require deleting data under a legal hold.
- Any DROP-related request (D8).
