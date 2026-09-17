# Brief for counsel — data retention, BridgeLeads

**Prepared:** 2026-09-17. **For:** outside counsel. **From:** engineering.

**This document asks questions. It does not answer them, and it contains no legal
advice or proposed policy wording** — drafting that is counsel's, per the owner's
standing instruction. Everything below is a statement of what the system actually
does, verified in code, with file references so anything here can be checked.

There are **three questions** (Q1, Q2, Q3). Q1 and Q2 change what the software
does and are blocking an engineering decision. Q3 is a wording question.

---

## 1. What the published policy currently says

Privacy Policy **§7** states that lead records are deleted after **365 days**.

## 2. What the system actually did until now

Nothing deleted anything on a schedule. Specifically, as of 2026-09-16:

- No scheduled job deleted from `results` (the lead table).
- Neither application database role held `DELETE` on `results`.
- The skip-trace cache (`skip_trace_cache`) had **never** had rows deleted. Its
  documented "90-day TTL" is a read-time check only: past 90 days a row simply
  stops being reused, but it is never removed. Those rows hold the full vendor
  response, including phone numbers and email addresses.
- Delivered export files (CSV/XLSX) in object storage were retained indefinitely.
  The code had no delete function at all.

So the 365-day promise in §7 was not being performed, in any respect.

## 3. What has now been built (not yet enabled)

The owner chose to **keep the lead record** and **delete the personal data inside
it**. Concretely, after the retention period the system will clear the
skip-traced **phone number, phone type, Do-Not-Call flag, email address** and the
multi-value phone/email lists, while keeping the underlying lead row — parcel
identifier, property address, recording date, and the owner name **as it appears
in the county public record**.

It also deletes the vendor cache rows, expires the provider's download links, and
deletes aged export files from object storage.

**It ships disabled and has not been switched on**, pending the answers below.

---

## Q1 — What does the 365-day clock run from?

This is the blocking question.

The system records `skip_trace_attempted_at` on a lead. It is set when contact
data is obtained — and it is **reset every time the lead is looked up again**.

Two readings, which produce materially different behaviour:

**(a) "Retain each copy of the contact data for 365 days."** The clock runs from
the most recent acquisition. A lead refreshed at least once a year keeps contact
data **indefinitely**, because each refresh restarts the period. This is what the
software currently implements, and it matches how the codebase already treats
that field elsewhere.

**(b) "Delete 365 days after the lead record was created."** The clock runs from
creation and never restarts. Contact data is deleted at 365 days regardless of
refreshes.

**What we need:** which reading §7 commits us to. If (b), the change is small and
we will make it.

**Relevant fact:** a customer can re-run a search that re-acquires the same
person's details. Under (a) that is a fresh acquisition of data we paid a vendor
for; under (b) it is not.

---

## Q2 — A related defect we found, which affects either answer

A lead's clock is also reset when a lookup **fails**.

When a re-lookup errors, the system stamps the "attempted at" field to the
current time even though **no new contact data was obtained**. The old data — in
principle already years old — then receives a fresh full retention period.

Three code paths do this (`skip_trace_dispatcher.py:569`, `:880`,
`tracerfy_ingest.py:782`).

We have **not** changed this yet, because the fix touches the billing-critical
lookup path and we want the Q1 answer first — the correct fix depends on it.

**What we need:** confirmation that retention must run from when the data was
**obtained**, not when it was last **attempted**. We expect the answer is yes, but
we are not going to assume it, because the remedy is a schema change.

---

## Q3 — §7 says "lead records"; we are deleting the data inside them

§7 promises deletion of **lead records**. The chosen design deletes the personal
contact data and **keeps the lead row**, on the reasoning that the remainder is
county public-record information about a property.

We flag it rather than assume it is fine. Two specifics counsel may want to weigh:

1. The retained row includes the **owner's name** as recorded by the county. It is
   public record, but it is still a name attached to a property and a distress
   signal (probate, pre-foreclosure, tax delinquency, code violation).
2. The retained row is retained **indefinitely** — there is no outer limit on the
   lead row itself, only on the contact data inside it.

**What we need:** whether §7 needs rewording to describe this accurately, and
whether the retained row needs its own outer limit.

---

## 4. Two adjacent facts counsel should have

Neither is a question for this brief; both bear on the same section.

- **The Do-Not-Call indicator described in §3 is never populated.** The field is
  hard-coded to "unknown" because the vendor supplies no DNC feed
  (`tracerfy_ingest.py:537`), it is not an exported column, and the live path
  treats unknown-DNC numbers as includable. Terms §5(a) obliges customers to
  honour an indicator they are never given.
- **The published policies route privacy, security and legal contact to addresses
  at a domain that is not ours.** `bridgeleads.com` has no mail exchanger, so
  those messages bounce. Any data-subject request sent to the address in the
  published Privacy Policy has never been received.

---

## 5. Where the underlying detail lives

| Topic | File |
|---|---|
| The full 20-item audit, with a corrections log | `tasks/BRIDGELEADS-COMPLIANCE-AUDIT-PHASE1.md` |
| Owner-facing steps | `tasks/PHASE2-OWNER-RUNBOOK.md` |
| The retention design, decisions D1–D4 | `tasks/todo-retention-purge.md` |
| The implementation | `src/workers/scheduler_helpers/retention.py` |
