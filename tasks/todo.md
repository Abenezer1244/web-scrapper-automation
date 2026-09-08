# King County mailing enrichment: why it stopped, and how it recovers

Branch: `fix/king-source-health-recovery` · Worktree: `C:/Users/Windows/bridgeleads-worktrees/king-source-recovery`

Incident job: `7afdda0c-0362-46ff-9849-0c60b62a3ce8` (config `dsergtyujkmn`, king/WA pre_foreclosure, 2026-09-07 13:25 UTC)

---

## 1. What the evidence says

All of this was read out of production, read-only.

### 1.1 The persisted block

`external_source_health` holds exactly one row:

```
source_key                 = king_erealproperty
status                     = throttled
first_seen_at              = 2026-09-04 11:15:39Z
updated_at                 = 2026-09-07 03:02:14Z
cooldown_until             = 2026-09-08 03:02:14Z
consecutive_probe_failures = 0
last_probe_at              = NULL
last_success_at            = NULL
reason = "King phase-1 circuit breaker tripped: 50/50 recent eRealProperty fetches
          failed (last status=302) after 49 of 200 parcels. Aborting so a block is
          never recorded as 'this parcel has no data'."
```

### 1.2 Timeline

| When (UTC) | Job | What happened |
|---|---|---|
| 09-04 07:40 - 10:33 | `60a0e80c` | King tax_delinquent, **17,157 parcels**. 173 looked up, 99 mailing found, 16,984 deferred. Budget exhausted, no error. |
| 09-04 09:03 - 09:32 | `68d83263` | King tax_delinquent, **17,157 parcels**, **overlapping the job above**. 173 looked up, 16,983 deferred. |
| 09-04 10:48 - 12:13 | `035501e3` | King tax_delinquent, **17,157 parcels**, third overlapping run. Breaker tripped **30/50** at 11:15:39. Source marked throttled, cooldown to 09-05 11:15. |
| 09-05, 09-06 | (none) | No King jobs. Cooldown expired 09-05 11:15 and nothing probed the source. |
| 09-07 01:37 - 03:10 | `230a1d0f` | King tax_delinquent, 17,157 parcels. Phase 1 tripped **50/50, last status=302** at 03:02:14. Re-armed to 09-08 03:02. 17,107 deferred. |
| 09-07 13:25 - 13:32 | `7afdda0c` | **The reported job.** Never made a single eRealProperty request: the gate raised `SourceUnavailableError` immediately. 153 parcels deferred. |

### 1.3 Live probes I ran

* From my workstation and from the **production worker container**, `blue.kingcounty.com/Assessor/eRealProperty/Dashboard.aspx?ParcelNbr=` returns **200** today, no redirect, no cookies. Parsers all still match: Site Address, the `Parcel Number` echo cell, the owner `Name` cell, and the `payment.kingcounty.gov` tax link all extract correctly on real parcels from this job.
* Leading-zero King tax parcel ids (`0000800015`, ...) are **not** the trigger. All 200, all echoing correctly.
* **60 consecutive requests at the exact production pacing (`asyncio.sleep(0.1)`), from the production worker's own IP: 60/60 = 200, avg 0.29 s, p95 0.45 s, 23.7 s wall.** Effective serial rate is **2.5 req/s**, not the ~10 req/s the code's docstring assumes.

So the upstream condition is over, the endpoint has not moved, the request format is unchanged, and production pacing at this volume does not self-throttle.

### 1.4 Why property succeeded and mailing did not

They are **different sources**.

* Property address: `county_gis.batch_enrich_parcels_gis` to the King **ArcGIS** parcel layer, batched. Not gated by `king_erealproperty` health. 153 parcels in 3 seconds, 109 rows filled.
* Mailing address: **eRealProperty** HTTP (phase 1, for the tax-bill URL) then **payment.kingcounty.gov** via Playwright (phase 2). Gated by `check_source_or_raise(KING_EREALPROPERTY)`, which raised before request #1.

The property pass never touched the throttled source. That is the whole explanation.

---

## 2. Root cause, in layers

**L1 - upstream, transient, and over.** Something at King answered a run of phase-1 fetches with failures on 09-04 and again on 09-07 (the 50th was a `302`). It is no longer happening. What made it happen is *not* provable from the telemetry we kept (see L4). The 09-04 event coincides with **three 17,157-parcel King jobs running concurrently** with no cross-job rate coordination, which is the most plausible trigger for the first trip.

**L2 - THE REAL BUG: the cooldown has no recovery path.**
`sources_due_for_probe()`, `mark_probe_failed()` and `mark_source_healthy()` in `src/scrapers/enrichment/source_health.py` are called **only from `tests/test_source_health.py`**. There is no production caller. The module docstring promises a source stays unhealthy "until a canary clears it". That canary was never built. `canary_check` in the beat schedule probes **county connectors**, not enrichment sources.

The database proves it: `last_probe_at = NULL` and `consecutive_probe_failures = 0` after three days of being throttled. The only way out is passive expiry, and then the next King job spends 50 requests rediscovering the block and re-arms another 24 hours.

**L3 - "deferred" is a dead end.** `enrichment_data["mailing_lookup_deferred"] = True` is written at `src/workers/tasks_helpers/enrich.py:715` and **read nowhere in production code**. The comment says "so a later sweep can find them (never a silent gap)". There is no sweep. Deferred means *permanently skipped* unless a human re-runs the job. Outstanding today: 153 + 17,107 + 16,983 + 16,984 parcels.

**L4 - we cannot attribute the failures.** The breaker's reason keeps only `last status=`. The other 49 failures could be any mix of non-200s and exceptions; the exception path logs at DEBUG and production runs at INFO. **I cannot claim all 50 were 302 - only the 50th was.**

**L5 - the job reported success it did not have.** "Enrichment complete - addresses added" after 0/153 mailing lookups, and the user-facing log copy contains em dashes.

---

## 3. Counts, reconciled (no billing defect in the headline numbers)

| Number | Meaning | Verified |
|---|---|---|
| 155 | rows scraped and persisted to `results` | `count(*) = 155` |
| 46 | `is_duplicate = true` | yes |
| 109 | `is_duplicate = false` | yes |
| 76 | non-duplicate **and** actionable (has property or mailing address) = `record_count` = `billed_count` | yes |
| 33 | non-duplicate with **no** address | yes |
| 13 | duplicate with no address | yes |
| 46 | "no deliverable address" = 33 + 13 | reconciles exactly |

**108 vs 109 is real but harmless.** "108 new leads" is `len(claimed_hashes)`, the count of distinct dedup hashes claimed. Two non-duplicate rows share one hash, so 109 rows claimed 108 hashes.

> **Separate pre-existing defect, found while reconciling (NOT part of this fix):** those two rows are `SANNES RICHARD` and `SANNES RICHARD / SANNES HEIDI`, same parcel `2154900130`, same address, different recording dates, **same `dedup_hash`, both `is_duplicate = false`, both actionable** - so the same property was billed twice in this job. Intra-job hash collisions are not being collapsed. Reporting for a separate PR; it is a different subsystem and mixing it in here would be wrong.

---

## 4. CSV timing (not a bug)

The CSV is built at 13:32:40 from the **persisted rows**, uploaded, and then **re-exported after enrichment to the same R2 object key** (`src/workers/tasks.py:1569-1590`, guarded on the post-enrichment refetch succeeding). The first export's comment says so explicitly: "Mailing is NULL pre-enrichment; the later re-export refreshes it."

Still to verify with the actual file: the R2 credentials available to me have no read grant (`GetObject` returns 401), so I will confirm through the app's own download path during live verification.

---

## 5. Tracerfy: nothing was charged and nothing is at risk

* All 155 rows are `skip_trace_status = 'not_attempted'`, `pending_skip_trace_rows` for this job = 0. This config has `skip_trace_enabled` off, so the enqueue returned before any work.
* Skip trace keys off **`property_address`**, not mailing. A deferred mailing lookup neither blocks nor degrades it.
* The enqueue filter is `skip_trace_status == 'not_attempted' AND is_duplicate is False AND property_address IS NOT NULL AND actionable_condition()`. Any recovery that only fills `mailing_address` cannot re-trigger a trace, and the recovery sweep will not call the enqueue at all.

---

## 6. Plan

Phased, each phase small enough to verify on its own.

### Phase 1 - the missing canary (the headline fix)
- [ ] Beat task that calls `sources_due_for_probe()`, issues **one** cheap probe per due source, then `mark_source_healthy()` or `mark_probe_failed()`. Uses only functions that already exist and are already tested.
- [ ] Register it in `src/workers/scheduler.py`.
- [ ] Tests: recovery after cooldown, probe failure escalates, healthy source is not probed.

### Phase 2 - proportionate first cooldown rung
- [ ] Ladder `24/48/72h` becomes `1h/6h/24h/48h/72h`. A transient blip clears in an hour; a genuinely angry source still escalates to days. Justified by evidence: both outages were over within hours yet blocked us for days. This is **not** shortening a cooldown to silence a warning; the canary added in Phase 1 is what actually decides recovery.
- [ ] Tests for each rung.

### Phase 3 - deferred mailing recovery
- [ ] Bounded beat sweep: rows with `mailing_lookup_deferred` and no mailing address, oldest first, capped per tick, respecting the source-health gate.
- [ ] Must not enqueue skip trace, must not create a job, must not touch quota or billing, must clear the marker on success.
- [ ] Tests: retry fills mailing, does not duplicate a lead, does not consume quota, does not enqueue Tracerfy, preserves existing property/mailing values, leaves missing source values NULL.

### Phase 4 - breaker diagnostics
- [ ] Record a status-code / exception-class histogram plus the redirect target host and path (no query string) in the persisted reason and in one structured log line.
- [ ] **Deliberately NOT switching eRealProperty to `safe_get_following`.** `parcel_page_is_for()` trusts a page with no parcel cell when the requested id is a well-formed 10-digit King PIN, so following a 302 to a block page would give us a 200 we would then record as "this parcel has no data", exactly what the breaker exists to prevent. Keep `allow_redirects=False`; only record where the redirect pointed, so the next occurrence names itself.

### Phase 5 - honest status and no em dashes
- [ ] Report partial enrichment accurately when property succeeded and mailing was deferred.
- [ ] Keep user-facing copy free of internal service names, exception class names and breaker details.
- [ ] Zero em dashes in modified user-facing copy.

### Phase 6 - cross-job request-rate bound
- [ ] Shared lease so concurrent King jobs serialise their eRealProperty use instead of multiplying the rate (three concurrent 17k jobs on 09-04 is the evidence).

### Deliberate non-changes
- Not raising `30/50` or `50/50`. Not disabling the breaker. Not touching billing, plan limits or Tracerfy. Not changing other counties' scrapers.

---

## 7. Review section

(to be filled in after implementation)
