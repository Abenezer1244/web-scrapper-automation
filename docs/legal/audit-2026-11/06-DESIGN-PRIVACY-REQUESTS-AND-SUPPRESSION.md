# Design: privacy requests and suppression for people in lead data (Phase 6)

**Status:** DRAFT v2, 2026-11-04. Revised after a Codex design review (section 12). Not implemented.

- **Owner/counsel decisions:** marked **[D]**.
- **Inputs:** audit P0-2 (`01-...`); a full code map of where person data enters and leaves the backend (section 4).
- **Not legal advice.**
- **No suppression rule may be activated until every enforcement layer in section 6 is deployed** (activation guard, section 8).

## 1. Problem

The Privacy Policy (section 8) promises people in our lead records (property owners who never signed up) access, correction, deletion and opt-out of "sale"/"sharing".

**Nothing can do any of it.** There is no intake, no way to find one person across all customers' data, no cross-customer erasure, and no suppression. A deleted person is re-scraped, re-enriched, re-sold and re-pushed to dialers on the next run. If BridgeLeads is a data broker under the California Delete Act, DROP deletion requests must also be processed (P0-5).

## 2. What we can and cannot guarantee (stated precisely)

Once a suppression is **active** (its activation transaction has committed):

- **Guaranteed:** no NEW scrape row, enrichment write, export file, download, results-API response, dialer push or Tracerfy submission will contain the subject.
  - Each send path re-authorizes from current data immediately before it sends.
- **Not guaranteed:**
  1. A send already in flight at the moment of activation. An external HTTP call cannot be made atomic with our database.
  2. Copies customers already downloaded or pushed into their own tools.
  3. Data Tracerfy already holds.

  (2) and (3) are outside our control. We can notify, and request deletion where a contract allows. **[D]**
- **Deletion** additionally erases our stored copies (section 7).
- **Opt-out of sale/sharing** suppresses all delivery and enrichment but does not erase. **[D: whether to erase on opt-out too]**

## 3. Request types [D]

The verification standard is a counsel decision. The design supports configurable levels; nothing is hard-coded.

| Request | Effect |
|---|---|
| Opt-out of sale/sharing | Suppress |
| Deletion | Suppress + erase |
| Access (categories, or specific pieces) | Report, sent only to the verified address |
| Correction | Correct, or suppress the field |

**Rules:**
- Pre-verification responses are always identical and never confirm that a record exists.
- Deadlines (CCPA states 45 days, extendable once) are stored per request type as configuration **[D]**. They are not assumed to be the same for every type or for DROP.

## 4. Where a person enters and leaves (code map, condensed)

**Enters**
- **One insert for all scrapers:** `run_scrape_job` (`src/workers/tasks.py:1335`). The probate TOD filter at `:1142` is the precedent for dropping records before it.
- **15+ writers that fill names or addresses on EXISTING rows:**
  - ORM enrichment in `enrich.py`;
  - raw-SQL recovery jobs (`owner_recovery`, `cv_owner_recovery`, `pierce_cv_owner_recovery`, `mailing_recovery`, `cv_mailing_recovery`, `property_recovery`);
  - the NTS matcher;
  - duplicate reuse;
  - Tracerfy ingest;
  - backfill scripts.
- **Shared cross-customer stores:**
  - `king_parcel_owners`, repopulated by `owner_recovery.store_answers`;
  - `nts_notices`, written by `nts_crawler._upsert_notice`, which COALESCEs `grantor`, so blanking it is undone by the next crawl;
  - `county_records` (orphaned).

**Leaves**
- **The central predicate:** `lead_actionability.py` gates results tabs, download, account export, segments, batch SQL, analytics, dialer selection, the skip-trace dispatcher and the contact-lookup planner.
  - Not covered: the `no_address` and `not_qualified` tabs (`jobs.py:680`, `results_category.py:51,62`).
- **Queued sends that do NOT re-read state today:**
  - the generic dialer payload is sealed into an outbox (`scheduler_helpers/dialer.py:335`, `webhook_delivery.py:224`);
  - Tracerfy pending rows are claimed and then emitted by `_build_payload_rows` with no `Result`, parcel or county context (`skip_trace_dispatcher.py:2343`).
- **Frozen artifacts:**
  - R2 job export, written twice per run (`tasks.py:1651`, `:2211`);
  - batch combined CSV;
  - account-export ZIPs;
  - the Tracerfy result CSV behind `skip_trace_queues.download_url`.
- **PII-printing scripts and admin diagnostics:** an inventory is needed (slice P4).

## 5. Identity and matching

**Subjects, not token soup.** Each lead row yields structured subject entries with provenance:

| Field | Values |
|---|---|
| `role` | `party`, `heir`, `owner_of_record`, `grantor`, `contact` |
| `kind` | `person`, or `entity` (LLC, INC, TRUST, ESTATE OF, BANK, …, by a classifier) |
| name | parsed into surname + given names (`LAST FIRST M` and `First M Last` both handled) |
| context | property and mailing |

An `heirs` string is split into separate entries. Each is matched on its own and never implies the others.

**Keys:**
- Versioned keyed HMACs: `HMAC(K_privacy[v], "privacy|<kind>|" + normalised value)`.
- `K_privacy` is a dedicated key derived from a rotatable key hierarchy, not an unversioned reuse of `blind_index`.
- Every rule stores `key_version` and `normalization_version`. During rotation, rules carry HMACs under both versions.

**A rule** = an anchor (`property` = `compute_property_key`, parcel-primary; or `mailing`; or `contact`) plus a REQUIRED subject (surname + at least one given name for a person; full normalised name for an entity).
- Name-only rules are rejected.
- Empty-subject rules are rejected.
- `property_all_persons` exists only as an explicit operator scope with documented authority **[D]**.

**Automatic vs review:**
- A match is automatic only on an exact anchor HMAC and an exact subject HMAC.
- Address-normalised, missing-parcel, same-surname-co-owner or entity-ambiguous candidates go to operator review. They never apply automatically.

**Co-owners [D]:** suppress the whole row for delivery (privacy-safe default), or blank only the matched subject entry and keep the row for the others.

## 6. Enforcement layers (all fail closed)

- **Single authorization service.** `privacy.authorize(subjects) -> allowed | suppressed(rule_ids)`. It loads the active rules once per operation, indexed in memory by anchor HMAC, with no per-row query. A rule snapshot that cannot be loaded fails the operation (retry). It never proceeds unchecked.
- **Storage flag.** A dedicated `results.privacy_suppressed_at` (and rule id), NOT the scalar `delivery_excluded_reason`, so privacy never overwrites quota or qualification state.
- **Read paths:**
  - the central predicate excludes privacy-suppressed rows;
  - every tab, including `no_address`/`not_qualified`, honours it;
  - exports filter rows before rendering, so counts stay consistent.
- **Durable send authorization:**
  - **Dialer outbox:** store row ids, not sealed person payloads. Rebuild and authorize immediately before each send.
  - **Tracerfy dispatcher:** join each pending row back to its `Result` (parcel, county, subjects) and authorize before the POST.
  - **Tracerfy ingest:** quarantines answers for suppressed subjects (write nothing, drop the cache entry).
- **Write paths:**
  - the pre-insert filter in `run_scrape_job`;
  - `store_answers` and `_upsert_notice` become suppression-aware before commit;
  - the recovery writers' guarded UPDATEs add a "not privacy-suppressed" predicate;
  - an enforcer sweep (beat) re-applies rules so stored state converges.
- **Lock order:** compatible with the account-deletion fence (users → rows). Activation cancels or withdraws queued work for the subject (unsent pending rows, dialer outbox entries).

## 7. Erasure (deletion requests)

> **Status 2026-11-08 (P6, PR #610, dormant until P7):** built as migration 135 + `src/privacy/erase.py` + `src/workers/privacy_erasure.py`. Deviations: King owners become tombstones (not deleted) so the P5 writer gates hold; only exact (WITHHELD) matches are erased, never REVIEW (section 5); every download already renders from the database, so R2 copies are deleted rather than invalidated. Review log: `tasks/todo-privacy-phase6-p6.md`.

One idempotent procedure with an **explicit column inventory**, run at activation and again after in-flight jobs drain.

| Store | Action |
|---|---|
| `results` | Blank the person columns. Set **`enrichment_data = NULL`** (as the account purge does; a key-picking approach misses future keys). Set the privacy flag. Keep the skeleton for billing **[D]** |
| `skip_trace_cache` | Delete through recorded lineage (the subject hashes of every row ever traced for the subject), including advanced-trace entries |
| `pending_skip_trace_rows` | Delete unsent; scrub sent; ingest quarantine covers late answers |
| `king_parcel_owners`, `county_records` | Delete matched rows; writer gates block refills |
| `nts_notices` | Blank every person column (inventory), with the upsert gate |
| R2 | Delete or invalidate job exports (both writes), batch combined CSVs and account-export ZIPs containing the subject; check object versioning and CDN caching |
| Tracerfy | Clear `download_url`. Vendor deletion is NOT reported as done unless the contract or API guarantees it **[D]** |
| `delivered_records`, `property_list_membership` | Property ledgers without names, kept **[D]** |
| Customers who received the subject | Notification duty and its legal basis **[D]**. Counsel to confirm; the earlier citation was not verified |

## 8. Data model and activation guard (migration 125 when built)

**`privacy_requests`**
- Kind, status lifecycle, received/deadline/extension, encrypted requester contact and details, verification level reached, fixed decision codes.
- Retention **[D]**.

**`privacy_request_events`**
- Append-only audit trail.

**`privacy_suppressions`**
- Rule rows: anchor kind + HMAC, subject HMACs, scope, `key_version`, `normalization_version`, request id, `state` (pending → active → lifted), created/activated timestamps.
- Append-only, with state changes as new rows.
- Retention **[D]**.

**Activation guard:** a rule can be activated only when `settings.PRIVACY_ENFORCEMENT_VERSION` is at least the version that implements every layer in section 6. Until then rules can be recorded but stay `pending`.

## 9. Interfaces

**Public:**
- `POST /privacy/requests`: enumeration-safe, rate-limited, email-link confirmation.
- A frontend page linked from the policy and a footer "Your privacy choices" link (copy **[D]**).

**Admin** (admin + MFA step-up):
- review candidates, choose scope, activate, deny with a fixed code, generate an access report.

**Beat:**
- deadline alerts, enforcer sweep, the erasure second pass.

**DROP ingestion:** after registration is decided **[D]**.

## 10. Order of work (Codex: decisions first; slices split further than 5 files would allow)

| Step | Work |
|---|---|
| D | Owner/counsel: verification levels, co-owner policy, erasure scope, opt-out erasure, entity handling, retention, Tracerfy terms, customer notification |
| P1 | Versioned subject parser + key library + fixtures from real record formats (owner-independent parts: parsing, entity detection, HMAC versions) |
| P2 | Schema + roles + audit + activation guard (rules can be created only as `pending`) |
| P3 | Authorization service + read-path enforcement + the dedicated flag + tab gaps |
| P4 | PII-emission inventory (scripts and admin tools) + the durable send changes (dialer outbox by id, Tracerfy dispatcher join, ingest quarantine) |
| P5 | Write-path gates (insert filter, `store_answers`, `_upsert_notice`, recovery predicates) + enforcer sweep |
| P6 | Erasure procedure + artifact invalidation + second pass |
| P7 | Admin review and activation; THEN raise `PRIVACY_ENFORCEMENT_VERSION` |
| P8 | Public intake + frontend page + deadline beat; P9 DROP |

Each step is split into commits of at most 5 files, each separately verified and Codex-reviewed.

**Interim, until P7 ships:** handle requests manually.
- Acknowledge within the legal window.
- Record the request (the mailbox must exist first, P0-1).
- An operator blanks the subject's rows by hand under a written procedure.
- **Before the automation exists, re-import CANNOT be prevented.** Counsel should know this when advising on responses and on data-broker registration timing.

## 11. Decisions [D]: ADOPTED by the owner 2026-11-05, pending counsel review

The owner adopted these privacy-protective defaults as company policy so P3-P9 can be built. Counsel has not reviewed them.
Each is a policy constant or configuration value, so it can change after counsel review.

| # | Decision | Adopted default |
|---|---|---|
| 1 | Verification | **Opt-out:** email-link confirmation only. **Delete/access:** email link plus a name and property-address match. **Authorized agents:** need signed permission. |
| 2 | Co-owners | Suppress the WHOLE row from delivery. |
| 3 | Erasure | **Delete:** blank the person data and keep a nameless property skeleton for billing. **Opt-out:** suppress, no erase. |
| 4 | Entities | People named as estate, trustee or heir are covered. Pure companies (LLC/INC) are declined with a fixed decision code. |
| 5 | Retention | **Request records:** 24 months (CCPA regulations). **Requester contact:** deleted 24 months after close. **Suppression rules:** kept indefinitely. |
| 6 | Tracerfy | Ask for deletion where possible. Never report vendor deletion as done unless confirmed. |
| 7 | Customer notification | None until counsel confirms a basis. |
| 8 | Data-broker registration / DROP | Not built until counsel decides. Intake is built so DROP can plug in. |
| 9 | Privacy-choices page copy | Drafted by engineering, marked for counsel review. |

## 12. Codex design review (2026-11-04): what changed from v1

| Codex finding | Change |
|---|---|
| Critical: serializers cannot guarantee "never sent"; queued dialer and Tracerfy payloads bypass them | Durable send authorization (section 6); exact guarantee semantics (section 2) |
| High: name-token subsets are not identity; no key rotation; empty-token rules | Structured subjects, versioned dedicated keys, rejected empty and name-only rules, ambiguous matches to review (section 5) |
| High: "one predicate" overstated; scalar exclusion field; tab gaps; scripts | Dedicated flag, all tabs, filter before render, PII-emission inventory (sections 6, 10) |
| High: erasure incomplete (enrichment_data keys, cache lineage, writer refills, NTS COALESCE, account ZIPs, Tracerfy) | Explicit inventory, `enrichment_data = NULL`, lineage, writer gates, artifacts (section 7) |
| High: races with recovery jobs and the deletion fence | Guarded writes, compatible lock order, cancellation of queued work at activation (section 6) |
| High: S1-S5 not owner-independent; operator script unsafe; 5-file budget unrealistic | Decisions first; activation guard; steps split into ≤5-file commits (sections 8, 10) |
| Legal flags (opt-out verification, data-point counts, deadlines, customer-notification citation, retention basis, opt-out vs erasure contradiction) | All moved to [D], the contradiction resolved explicitly (sections 2, 3, 7) |
| Medium: performance | Rules loaded once per operation, in-memory index, no per-row queries (section 6) |
