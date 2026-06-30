"""Scraper-run + inline-enrichment + skip-trace helpers, extracted from tasks.py.

Holds the async scraper runner, the duplicate-lead enrichment reuse, the inline
GIS/PACS/King enrichment pass, and the skip-trace enqueue. Moved verbatim —
behavior is byte-identical to the originals in tasks.py.
"""

import asyncio
from typing import TYPE_CHECKING

import redis as sync_redis

from src.config import settings
from src.utils.logger import setup_logger
from src.workers.property_identity import legacy_strong_signature as _legacy_strong_signature
from src.workers.tasks_helpers.status import _now, _publish_log

if TYPE_CHECKING:
    from src.scrapers.base_scraper import ProgressCallback

_logger = setup_logger("worker.task")

# How many parcels the GIS sweep enriches+commits per transaction. Small enough
# that a hard-kill loses at most one batch of work; large enough to keep the
# commit overhead negligible against the per-chunk (50-parcel) HTTP cost.
_GIS_COMMIT_BATCH = 500


async def _run_scraper(
    scraper_class,
    date_from: str,
    date_to: str,
    r: sync_redis.Redis,
    job_id: str,
    on_progress: "ProgressCallback | None" = None,
    record_type: str | None = None,
    doc_types: list | None = None,
):
    """Run the async scraper and stream progress logs back to Redis."""
    # Pass record_type / doc_types ONLY to scrapers whose constructor accepts
    # them (template/AI/partial scrapers may not). doc_types=None means legacy
    # behavior. An EXPLICIT selection (including the degenerate [] of a stale
    # config) must reach the constructor so it can fail closed — hence
    # `is not None`, not truthiness, so [] is passed through rather than silently
    # treated as legacy/full (Codex High).
    import inspect
    kwargs = {}
    try:
        params = inspect.signature(scraper_class).parameters
    except (ValueError, TypeError):
        params = {}
    if record_type and "record_type" in params:
        kwargs["record_type"] = record_type
    if doc_types is not None and "doc_types" in params:
        kwargs["doc_types"] = doc_types
    async with scraper_class(**kwargs) as scraper:
        if on_progress:
            scraper.on_progress = on_progress
        records = await scraper.scrape(date_from, date_to)

        # Log AI usage if this was an AI-powered scrape
        if hasattr(scraper, "ai_cost") and scraper.ai_cost > 0:
            tokens = scraper.ai_tokens
            _publish_log(
                r, job_id, "info",
                f"AI usage: ${scraper.ai_cost:.4f} "
                f"({tokens['input_tokens']} input + {tokens['output_tokens']} output tokens)",
            )

    return records


def _reuse_enrichment_for_duplicates(db, job, job_id: str) -> int:
    """Copy enrichment + settled skip-trace from the originally-delivered Result
    onto THIS job's is_duplicate rows, so a re-scrape of already-seen leads does
    not re-hit county GIS or re-pay Tracerfy. Returns the number of rows updated.

    Runs first in the ENRICHING phase: once a duplicate row has an address +
    settled skip-trace status copied in, the existing selectors below skip it
    (GIS needs a missing address; skip-trace needs status='not_attempted').

    SECURITY (multi-tenant): every join leg is filtered by job.user_id. The
    worker runs on the SYSTEM db session (which is not constrained by RLS), so
    this explicit user_id filter — not RLS — is the tenant boundary; it makes a
    cross-tenant copy impossible. Reuse is gated to PROVABLY-STRONG identity:
    we recompute legacy_strong_signature(parcel_id, property_address) per
    candidate (the FROZEN scheme dedup_hash stores — NOT the 2026-06-12 overlap
    property_key) and reuse ONLY rows whose dedup_hash IS that strong key — so
    the hash must have come from the parcel/address branch, never the weak
    NAME|DATE fallback. A blank/placeholder parcel ('', 'N/A', whitespace) makes
    legacy_strong_signature return None (is_strong_identity=False), so it can't
    match and is excluded;
    `parcel_id IS NOT NULL` alone was insufficient (Codex P1). Thus one
    homeowner's PII can never be copied onto an unrelated record that merely
    shares a name + filing date. Address/source fields are FILL-MISSING (COALESCE
    current-first — never clobber a fresh scrape/GIS value); skip-trace PII is
    copied only from a SETTLED (hit/miss) prior trace within the 90-day cache
    TTL, onto a row that has not itself been attempted.
    """
    from sqlalchemy import text as _sa_text

    from src.workers.property_identity import normalize_address, normalize_parcel

    uid = str(job.user_id)

    # Placeholder/junk parcels (all-zeros, a single repeated char, <4 chars, no
    # digit, or known junk tokens) pass is_strong_identity but are NOT a real
    # property identity — unrelated homeowners can share one, so reusing PII
    # across them would leak phone/email (Codex P1). Only safe to ignore the
    # parcel when a SPECIFIC address anchors the identity instead.
    _PARCEL_JUNK = {"NA", "NONE", "NULL", "UNKNOWN", "NOPARCEL", "PENDING", "TBD", "TEST"}

    def _reusable(parcel_id, property_address, dedup_hash) -> bool:
        # 1) must be the STRONG (parcel|address) hash, never weak NAME|DATE.
        # Compares against legacy_strong_signature — dedup_hash stores the
        # FROZEN legacy scheme, NOT the (2026-06-12, county-scoped) overlap
        # property_key. Comparing the new key here would silently disable all
        # enrichment/skip-trace reuse (Codex P1).
        if _legacy_strong_signature(parcel_id, property_address) != dedup_hash:
            return False
        # 2) a specific address makes the identity safe regardless of parcel.
        addr = normalize_address(property_address)
        if len(addr) >= 8 and any(c.isalpha() for c in addr):
            return True
        # 3) no real address -> identity rests on the parcel alone; reject junk.
        p = normalize_parcel(parcel_id)
        if (
            len(p) < 4
            or len(set(p)) <= 1          # all-zeros / single repeated char
            or p.lstrip("0") == ""
            or not any(c.isdigit() for c in p)
            or p in _PARCEL_JUNK
        ):
            return False
        return True

    # Strong-identity gate: recompute per candidate (tenant-scoped to this job +
    # user) and keep ONLY rows that are safe to reuse.
    candidates = db.execute(
        _sa_text(
            "SELECT id, parcel_id, property_address, dedup_hash FROM results "
            "WHERE job_id = CAST(:jid AS uuid) AND user_id = CAST(:uid AS uuid) "
            "AND is_duplicate = true AND dedup_hash IS NOT NULL"
        ),
        {"jid": job_id, "uid": uid},
    ).fetchall()
    strong_ids = [
        str(row.id)
        for row in candidates
        if _reusable(row.parcel_id, row.property_address, row.dedup_hash)
    ]
    if not strong_ids:
        return 0

    ttl = int(getattr(settings, "SKIP_TRACE_CACHE_DAYS", 90))
    # Fully-STATIC SQL — no string interpolation at all (the settled-reuse
    # predicate is written out per skip-trace column) and every value is a bound
    # parameter (:ids, :uid, :ttl), so there is no injection surface. Membership
    # is restricted to the Python-verified strong_ids; skip-trace PII is copied
    # only from a SETTLED (hit/miss) prior trace inside the TTL window, onto a
    # row that has not itself been attempted.
    sql = """
        UPDATE results AS rn SET
            property_address     = COALESCE(rn.property_address, ro.property_address),
            mailing_address      = COALESCE(rn.mailing_address, ro.mailing_address),
            delinquent_amount    = COALESCE(rn.delinquent_amount, ro.delinquent_amount),
            delinquent_bill_year = COALESCE(rn.delinquent_bill_year, ro.delinquent_bill_year),
            phone = CASE WHEN ro.skip_trace_status IN ('hit','miss') AND ro.skip_trace_attempted_at IS NOT NULL AND ro.skip_trace_attempted_at >= NOW() - make_interval(days => :ttl) AND rn.skip_trace_status = 'not_attempted' THEN ro.phone ELSE rn.phone END,
            phone_type = CASE WHEN ro.skip_trace_status IN ('hit','miss') AND ro.skip_trace_attempted_at IS NOT NULL AND ro.skip_trace_attempted_at >= NOW() - make_interval(days => :ttl) AND rn.skip_trace_status = 'not_attempted' THEN ro.phone_type ELSE rn.phone_type END,
            phone_dnc_flag = CASE WHEN ro.skip_trace_status IN ('hit','miss') AND ro.skip_trace_attempted_at IS NOT NULL AND ro.skip_trace_attempted_at >= NOW() - make_interval(days => :ttl) AND rn.skip_trace_status = 'not_attempted' THEN ro.phone_dnc_flag ELSE rn.phone_dnc_flag END,
            email = CASE WHEN ro.skip_trace_status IN ('hit','miss') AND ro.skip_trace_attempted_at IS NOT NULL AND ro.skip_trace_attempted_at >= NOW() - make_interval(days => :ttl) AND rn.skip_trace_status = 'not_attempted' THEN ro.email ELSE rn.email END,
            skip_trace_status = CASE WHEN ro.skip_trace_status IN ('hit','miss') AND ro.skip_trace_attempted_at IS NOT NULL AND ro.skip_trace_attempted_at >= NOW() - make_interval(days => :ttl) AND rn.skip_trace_status = 'not_attempted' THEN ro.skip_trace_status ELSE rn.skip_trace_status END,
            skip_trace_attempted_at = CASE WHEN ro.skip_trace_status IN ('hit','miss') AND ro.skip_trace_attempted_at IS NOT NULL AND ro.skip_trace_attempted_at >= NOW() - make_interval(days => :ttl) AND rn.skip_trace_status = 'not_attempted' THEN ro.skip_trace_attempted_at ELSE rn.skip_trace_attempted_at END,
            phones = CASE WHEN ro.skip_trace_status IN ('hit','miss') AND ro.skip_trace_attempted_at IS NOT NULL AND ro.skip_trace_attempted_at >= NOW() - make_interval(days => :ttl) AND rn.skip_trace_status = 'not_attempted' THEN ro.phones ELSE rn.phones END,
            emails = CASE WHEN ro.skip_trace_status IN ('hit','miss') AND ro.skip_trace_attempted_at IS NOT NULL AND ro.skip_trace_attempted_at >= NOW() - make_interval(days => :ttl) AND rn.skip_trace_status = 'not_attempted' THEN ro.emails ELSE rn.emails END
        FROM delivered_records dr
        JOIN results ro
          ON ro.id = dr.first_result_id
         AND ro.user_id = CAST(:uid AS uuid)
        WHERE rn.id = ANY(CAST(:ids AS uuid[]))
          AND rn.user_id = CAST(:uid AS uuid)
          AND dr.user_id = CAST(:uid AS uuid)
          AND dr.dedup_hash = rn.dedup_hash
          AND rn.id <> dr.first_result_id
    """
    result = db.execute(_sa_text(sql), {"ids": strong_ids, "uid": uid, "ttl": ttl})
    db.commit()
    return result.rowcount or 0


def _run_inline_enrichment(db, job, r, job_id: str, config) -> None:
    """Run GIS + King County enrichment inline (before job marks done)."""
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from src.db.models import Result

    # Reuse prior enrichment for duplicate leads BEFORE any external lookup, so a
    # re-scrape of already-seen records doesn't re-hit county GIS or re-pay
    # Tracerfy. Non-fatal: on any failure we roll back and fall through to the
    # normal full-enrichment path (correctness over the cost optimization).
    try:
        reused = _reuse_enrichment_for_duplicates(db, job, job_id)
        if reused:
            _publish_log(
                r, job_id, "info",
                f"Reused prior enrichment for {reused} duplicate leads "
                "(skipped re-lookup + skip-trace charge)",
                db=db,
            )
    except Exception as exc:
        _logger.warning("Duplicate enrichment reuse skipped: %s", str(exc)[:160])
        try:
            db.rollback()
        except Exception:
            pass

    all_results = db.execute(
        sa_select(Result).where(Result.job_id == job_id, Result.user_id == job.user_id)
    ).scalars().all()

    # GIS batch enrichment for property AND mailing addresses
    # Run for records missing either property address or mailing address
    results_need_addr = [
        res for res in all_results
        if res.parcel_id and len(res.parcel_id.strip()) >= 6
        and (not res.property_address or res.property_address == "(enrichment unavailable)"
             or not res.mailing_address)
    ]
    if results_need_addr:
        _publish_log(r, job_id, "info", f"Looking up {len(results_need_addr)} property addresses...", db=db)
        from src.scrapers.enrichment.county_gis import batch_enrich_parcels_gis
        parcel_map: dict[str, list] = {}
        for res in results_need_addr:
            pid = res.parcel_id.strip()
            if pid not in parcel_map:
                parcel_map[pid] = []
            parcel_map[pid].append(res)
        # Commit the GIS sweep INCREMENTALLY (per parcel batch) instead of once at
        # the end: a final-only commit meant a hard-kill mid-sweep persisted
        # nothing, so a re-run restarted the whole sweep and never converged. With
        # per-batch commits, filled rows survive a kill and the results_need_addr
        # filter excludes them on re-run, so each resume does strictly less work.
        all_pids = list(parcel_map.keys())
        rows_updated = 0
        commit_failures = 0
        for i in range(0, len(all_pids), _GIS_COMMIT_BATCH):
            batch_pids = all_pids[i:i + _GIS_COMMIT_BATCH]
            gis_results = batch_enrich_parcels_gis(batch_pids, config.county, config.state)
            batch_updated = 0
            for pid, gis_data in gis_results.items():
                if not gis_data.get("property_address"):
                    continue
                for res in parcel_map.get(pid, []):
                    res.property_address = gis_data["property_address"]
                    res.mailing_address = gis_data.get("mailing_address") or res.mailing_address
                    batch_updated += 1
            try:
                db.commit()
            except Exception as exc:
                # Don't rollback-then-empty-commit (that would discard this batch's
                # fills while reporting success). Roll back (recovers the session
                # for the next batch) and skip the progress log. Enrichment is
                # best-effort by design — the caller wraps this whole function in a
                # try/except and delivers the job DONE without enriched fields on
                # failure (tasks.py) — so a commit hiccup must not fail the job. The
                # unfilled rows stay in results_need_addr and are re-attempted if
                # the job is re-run; the end-of-sweep summary below surfaces it.
                db.rollback()
                commit_failures += 1
                _logger.warning(
                    "Job %s: GIS batch commit failed at %d/%d: %s",
                    job_id, i, len(all_pids), str(exc)[:120],
                )
                continue
            rows_updated += batch_updated
            _publish_log(
                r, job_id, "info",
                f"Property lookup progress: {min(i + _GIS_COMMIT_BATCH, len(all_pids))}"
                f"/{len(all_pids)} parcels ({rows_updated} rows updated)",
                db=db,
            )
        if commit_failures:
            _logger.warning(
                "Job %s: GIS sweep finished with %d batch commit failure(s) — some "
                "addresses are unfilled (best-effort; re-run to fill)",
                job_id, commit_failures,
            )

    # Name-based PACS fallback for records with no parcel (e.g. probate
    # estate filings: Cert of Death, Letters Testamentary, Personal Rep
    # Deed). These carry owner/heir names but no parcel_id in the
    # recording index. The county connector's assessor_url points at the
    # Tyler PACS PropertyAccess portal; we search it by owner name to
    # hydrate property_address. Skip trace downstream requires an
    # address, so this is what unlocks phone/email lookup for probate.
    from src.db.models import CountyConnector
    _conn_row = db.execute(
        sa_select(CountyConnector).where(
            func.lower(CountyConnector.county) == config.county.lower(),
            func.upper(CountyConnector.state) == config.state.upper(),
            CountyConnector.active,
        )
    ).scalars().first()
    connector_assessor_url = getattr(_conn_row, "assessor_url", None) if _conn_row else None
    # Fall back to the hardcoded _KNOWN_ASSESSOR_URLS map so PACS enrichment
    # works even when the connector row's assessor_url is still NULL
    # (e.g. migration 022 not yet applied to this environment).
    if not connector_assessor_url:
        from src.scrapers.enrichment.ai_assessor import _KNOWN_ASSESSOR_URLS
        key = f"{config.county.lower()}_{config.state.upper()}"
        connector_assessor_url = _KNOWN_ASSESSOR_URLS.get(key)
    from src.scrapers.enrichment.pacs import batch_lookup_pacs_by_name, is_pacs_url
    if connector_assessor_url and is_pacs_url(connector_assessor_url):
        results_no_addr = [
            res for res in all_results
            if res.party_name
            and not res.property_address
        ]
        if results_no_addr:
            _publish_log(
                r, job_id, "info",
                f"Looking up {len(results_no_addr)} addresses via PACS by owner name...",
                db=db,
            )
            names = [res.party_name for res in results_no_addr]
            pacs_results = batch_lookup_pacs_by_name(
                connector_assessor_url, names, max_workers=5,
            )
            name_hits = 0
            for res, pacs in zip(results_no_addr, pacs_results, strict=False):
                if not pacs:
                    continue
                if pacs.get("address"):
                    res.property_address = pacs["address"]
                if pacs.get("mailing") and not res.mailing_address:
                    res.mailing_address = pacs["mailing"]
                # parcel_id is intentionally NOT taken from the owner-name PACS
                # lookup (Codex point C): it's weak evidence and parcel_id is the
                # identity/billing/dedup key (parcel-primary compute_property_key).
                # _parse_pacs_result_html no longer returns it; this is the
                # explicit provenance boundary at the consumer.
                name_hits += 1
            if name_hits:
                try:
                    db.commit()
                except Exception as exc:
                    _logger.warning(
                        "Job %s: PACS enrichment commit failed (%d fills discarded): %s",
                        job_id, name_hits, str(exc)[:120],
                    )
                    db.rollback()
                    db.commit()
            _publish_log(
                r, job_id, "info",
                f"Found {name_hits}/{len(results_no_addr)} addresses via PACS",
                db=db,
            )

    # King County: eRealProperty + Tax Bill for property + mailing
    if config.county.lower() == "king" and config.state.upper() == "WA":
        needs = [
            res for res in all_results
            if res.parcel_id and len(res.parcel_id.strip()) >= 6
            and not res.mailing_address
        ]
        if needs:
            _publish_log(r, job_id, "info", f"Looking up {len(needs)} mailing addresses...", db=db)
            from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county
            pids = list({res.parcel_id.strip() for res in needs})
            pid_map: dict[str, list] = {}
            for res in needs:
                pid = res.parcel_id.strip()
                if pid not in pid_map:
                    pid_map[pid] = []
                pid_map[pid].append(res)
            # Cap at 300 parcels to avoid 10+ minute hangs on large batches.
            # King County assessor is slow (~0.5s per parcel) — 1354 parcels
            # takes ~11 min which exceeds the enrichment timeout.
            _MAX_KING_PARCELS = 300
            if len(pids) > _MAX_KING_PARCELS:
                _logger.info("Capping King County mailing lookup to %d/%d parcels", _MAX_KING_PARCELS, len(pids))
                pids = pids[:_MAX_KING_PARCELS]
            enriched = asyncio.run(asyncio.wait_for(batch_enrich_king_county(pids), timeout=240))
            # King tax-delinquent rows ship with a placeholder party_name because
            # the Socrata source has no owner column. The eRealProperty lookup
            # above now also yields the owner name; swap it in here. Dual gate:
            # job-level record_type (belt) + the exact placeholder shape
            # (suspenders, so probate/death-cert King rows sharing this enrichment
            # path are never touched).
            from src.scrapers.king_wa_tax_delinquent import is_tax_placeholder_party
            is_tax_delinquent = config.record_type == "tax_delinquent"
            for pid, data in enriched.items():
                prop = data.get("property_address")
                mail = data.get("mailing_address")
                owner = data.get("owner_name")
                for res in pid_map.get(pid, []):
                    if prop and not res.property_address:
                        res.property_address = prop
                    if mail:
                        res.mailing_address = mail
                    if (
                        is_tax_delinquent
                        and owner
                        and (not res.party_name or is_tax_placeholder_party(res.party_name))
                    ):
                        res.party_name = owner
            try:
                db.commit()
            except Exception as exc:
                _logger.warning(
                    "Job %s: King enrichment commit failed: %s", job_id, str(exc)[:120]
                )
                db.rollback()
                db.commit()
            found = sum(1 for d in enriched.values() if d.get("mailing_address"))
            _publish_log(r, job_id, "info", f"Found {found}/{len(pids)} mailing addresses", db=db)

        # Owner-only repair for King tax-delinquent rows that ALREADY have a
        # mailing address (so the missing-mailing pass above skipped them — e.g.
        # mailing was COALESCE-copied onto a duplicate by
        # _reuse_enrichment_for_duplicates) yet still carry the placeholder
        # party_name. King tax is a point-in-time snapshot, so fresh jobs are
        # ~100% duplicates that all hit this path. We resolve the owner with an
        # HTTP-only lookup (no Playwright mailing fetch — that data is already
        # present), under the SAME dual gate as the swap above: record_type ==
        # tax_delinquent (belt) + exact placeholder shape (suspenders).
        if config.record_type == "tax_delinquent":
            from src.scrapers.king_wa_tax_delinquent import is_tax_placeholder_party
            owner_needs = [
                res for res in all_results
                if res.parcel_id and len(res.parcel_id.strip()) >= 6
                and res.mailing_address
                and (not res.party_name or is_tax_placeholder_party(res.party_name))
            ]
            if owner_needs:
                _publish_log(
                    r, job_id, "info",
                    f"Resolving owner names for {len(owner_needs)} tax-delinquent leads...",
                    db=db,
                )
                from src.scrapers.enrichment.king_county_assessor import batch_extract_king_owners
                o_pid_map: dict[str, list] = {}
                for res in owner_needs:
                    o_pid_map.setdefault(res.parcel_id.strip(), []).append(res)
                o_pids_all = list(o_pid_map.keys())
                # Owner-only HTTP is fast (~0.2s/parcel), so a higher cap than the
                # Playwright mailing path is safe; still bounded to protect the
                # enrichment time budget. Overflow is left for the (re-runnable)
                # backfill script — logged here so the cap is never silent.
                _MAX_KING_OWNER_PARCELS = 500
                overflow = max(0, len(o_pids_all) - _MAX_KING_OWNER_PARCELS)
                o_pids = o_pids_all[:_MAX_KING_OWNER_PARCELS]
                if overflow:
                    _logger.info(
                        "King owner lookup capped at %d/%d parcels this job; %d deferred to backfill",
                        _MAX_KING_OWNER_PARCELS, len(o_pids_all), overflow,
                    )
                owners = asyncio.run(asyncio.wait_for(batch_extract_king_owners(o_pids), timeout=180))
                swapped = 0
                for pid, owner in owners.items():
                    for res in o_pid_map.get(pid, []):
                        # Fill only a BLANK or placeholder party_name (never clobber
                        # a real owner). King tax rows now ship blank, so accept
                        # None as well as the legacy placeholder.
                        if not res.party_name or is_tax_placeholder_party(res.party_name):
                            res.party_name = owner
                            swapped += 1
                # Decide persisted-vs-failed on the OWNER commit alone, THEN publish.
                # _publish_log(db=db) commits too, so folding the success log into
                # the same try would mislabel a persisted swap as "not persisted"
                # if only the log's commit failed.
                committed = False
                try:
                    db.commit()
                    committed = True
                except Exception as exc:
                    _logger.warning(
                        "Job %s: King owner-only commit failed (%d swaps not persisted): %s",
                        job_id, swapped, str(exc)[:120],
                    )
                    db.rollback()
                # Guard the post-commit log: _publish_log(db=db) commits, and a
                # failure HERE must not crash after the swaps already persisted nor
                # skip the skip-trace enqueue that follows this block.
                if committed:
                    deferred = f" ({overflow} deferred to backfill)" if overflow else ""
                    msg, level = (
                        f"Resolved {swapped} owner names from {len(owners)}/{len(o_pids)} parcels{deferred}",
                        "info",
                    )
                else:
                    msg, level = (
                        "Owner-name resolution failed to persist (will retry next run)",
                        "warning",
                    )
                try:
                    _publish_log(r, job_id, level, msg, db=db)
                except Exception as exc:
                    _logger.warning("Job %s: owner-only progress log failed: %s", job_id, str(exc)[:120])
                    db.rollback()  # log write failed; swaps already settled — keep going

    # ── Post-enrichment: log unactionable records (kept for visibility) ──
    # Records with no property_address and no mailing_address can't be
    # mailed, but we keep them in the DB so users see what was scraped.
    # The frontend shows them with empty address fields ("—").
    fresh = db.execute(
        sa_select(Result).where(Result.job_id == job_id, Result.user_id == job.user_id)
    ).scalars().all()
    unactionable = [
        res for res in fresh
        if not (res.property_address and res.property_address != "(enrichment unavailable)")
        and not res.mailing_address
    ]
    if unactionable:
        _publish_log(
            r, job_id, "info",
            f"{len(unactionable)} records have no deliverable address "
            f"(upstream data gap — parcel had no GIS address)",
            db=db,
        )
        _logger.info(
            "Job %s: %d/%d records have no deliverable address (kept for visibility)",
            job_id, len(unactionable), len(fresh),
        )

    # ── Sprint 4: skip trace enqueue ─────────────────────────────────────
    # Only runs if:
    #   1. SKIP_TRACE_ENABLED globally (env flag)
    #   2. The user's scraper config has skip_trace_enabled=True
    #   3. The user's plan permits skip trace (Starter blocked)
    #   4. TRACERFY_API_TOKEN is configured
    # Matching records are either hydrated from skip_trace_cache (free)
    # or inserted into pending_skip_trace_rows for the dispatcher to
    # submit in a batch. Actual Tracerfy calls happen in the dispatcher;
    # this step is instant and never blocks scrape completion.
    _enqueue_skip_trace_rows(db, job, r, job_id, config)


def _enqueue_skip_trace_rows(db, job, r, job_id: str, config) -> None:
    """Enqueue eligible Result rows into pending_skip_trace_rows.

    Runs at the end of _run_inline_enrichment, after GIS + county
    assessor + post-enrichment cleanup. Only records that survived the
    cleanup (have a property_address) are eligible.
    """
    # Local imports — sa_select must be imported here because the module-
    # level import is scoped inside _run_inline_enrichment, not globally
    from sqlalchemy import select as sa_select

    from src.db.models import PendingSkipTraceRow, Result, SkipTraceCache
    from src.scrapers.enrichment.skip_trace import (
        address_cache_key,
        build_pending_row_payload,
    )

    if not settings.SKIP_TRACE_ENABLED:
        return
    if not settings.TRACERFY_API_TOKEN:
        _publish_log(
            r, job_id, "warning",
            "Skip trace requested but TRACERFY_API_TOKEN not configured",
            db=db,
        )
        return
    if not getattr(config, "skip_trace_enabled", False):
        return
    # Plan gate: Starter excluded. Pro/Business/Agency allowed.
    if (job.user.plan or "starter").lower() == "starter":
        _publish_log(
            r, job_id, "warning",
            "Skip trace requested but user plan (starter) does not include it. "
            "Upgrade to Pro to unlock skip trace ($0.08/lookup).",
            db=db,
        )
        return

    # Reload the surviving results after the unactionable drop
    eligible = db.execute(
        sa_select(Result).where(
            Result.job_id == job_id,
            Result.user_id == job.user_id,
            Result.property_address.isnot(None),
            Result.skip_trace_status == "not_attempted",
        )
    ).scalars().all()

    if not eligible:
        return

    cache_hits = 0
    cache_misses = 0
    enqueued_normal = 0
    enqueued_advanced = 0

    for rec in eligible:
        # Parse the combined address to get canonical city/state for the cache key
        payload = build_pending_row_payload(rec)
        if payload is None:
            continue

        cache_key = address_cache_key(
            job.user_id,  # per-tenant cache: no cross-tenant PII reuse
            payload["property_address"],
            payload["city"],
            payload["state"],
        )
        cached = db.get(SkipTraceCache, cache_key)
        cache_valid = False
        if cached:
            # 90-day TTL check
            age = _now() - cached.fetched_at
            if age.days < settings.SKIP_TRACE_CACHE_DAYS:
                cache_valid = True

        if cache_valid:
            # Copy cached values directly to the Result — no Tracerfy call
            rec.phone = cached.phone
            rec.phone_type = cached.phone_type
            rec.phone_dnc_flag = cached.phone_dnc_flag
            rec.email = cached.email
            rec.phones = cached.phones
            rec.emails = cached.emails
            rec.skip_trace_status = "hit" if (cached.phone or cached.email) else "miss"
            rec.skip_trace_attempted_at = _now()
            cache_hits += 1
        else:
            # Enqueue for the dispatcher. Truncate string fields to fit
            # VARCHAR(128) — code violation descriptions can be 250+ chars
            # and crash the INSERT with StringDataRightTruncation, which
            # poisons the session with PendingRollbackError and hangs the job.
            def _trunc128(v: str | None) -> str | None:
                return v[:128] if v and len(v) > 128 else v

            # property_address + mail_address columns are VARCHAR(512); truncating
            # them to 128 (a) corrupts the skip-trace cache key (the read path in
            # _enqueue hashes the FULL Result.property_address, so a 128-truncated
            # write key would never match -> re-paid traces) and (b) drops real
            # mailing data sent to Tracerfy. Truncate to the actual column width.
            def _trunc512(v: str | None) -> str | None:
                return v[:512] if v and len(v) > 512 else v

            try:
                pending = PendingSkipTraceRow(
                    job_id=payload["job_id"],
                    result_id=payload["result_id"],
                    user_id=payload["user_id"],
                    property_address=_trunc512(payload["property_address"]),
                    city=_trunc128(payload["city"]),
                    state=_trunc128(payload["state"]),
                    zip=_trunc128(payload["zip"]),
                    first_name=_trunc128(payload["first_name"]),
                    last_name=_trunc128(payload["last_name"]),
                    mail_address=_trunc512(payload["mail_address"]),
                    mail_city=_trunc128(payload["mail_city"]),
                    mail_state=_trunc128(payload["mail_state"]),
                    mail_zip=_trunc128(payload["mail_zip"]),
                    trace_type=payload["trace_type"],
                    status="queued",
                )
                db.add(pending)
            except Exception as exc:
                # REDTEAM MED I3: log the non-PII Result id, never the
                # homeowner's party_name, in application logs.
                _logger.warning("Skip trace enqueue failed for result %s: %s", rec.id, str(exc)[:80])
                db.rollback()
                continue
            rec.skip_trace_status = "queued"
            cache_misses += 1
            if payload["trace_type"] == "advanced":
                enqueued_advanced += 1
            else:
                enqueued_normal += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        db.commit()

    _publish_log(
        r, job_id, "info",
        f"Skip trace: {cache_hits} cache hits, {cache_misses} queued "
        f"({enqueued_normal} normal + {enqueued_advanced} advanced). "
        f"Dispatcher submits batches every 5 min.",
        db=db,
    )
    _logger.info(
        "Job %s skip trace enqueue: cache_hits=%d queued=%d (normal=%d advanced=%d)",
        job_id, cache_hits, cache_misses, enqueued_normal, enqueued_advanced,
    )
