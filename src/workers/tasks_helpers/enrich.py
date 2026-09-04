"""Scraper-run + inline-enrichment + skip-trace helpers, extracted from tasks.py.

Holds the async scraper runner, the duplicate-lead enrichment reuse, the inline
GIS/PACS/King enrichment pass, and the skip-trace enqueue. Moved verbatim —
behavior is byte-identical to the originals in tasks.py.
"""

import asyncio
import re
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

# Anchored trailing ZIP only ("… PL 4C 98023" / "… 98023-1234"): never a 5-digit
# token inside the street (house numbers, road numbers).
_TRAILING_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")


def _keep_situs_parts(res, gis_data: dict) -> None:
    """Fill results.property_city / property_state / property_zip (migration 085)
    from REAL sources only, without touching property_address itself.

    Order: the scraper's own full situs line (a notice's "commonly known as"),
    parsed BEFORE the GIS street-only line replaces it; then the GIS row's
    structured situs parts (statewide SITUS_*, or Pierce when Delivery ==
    Site). Each part is filled only when still empty. Nothing is inferred.
    """
    from src.utils.lead_formatting import parse_property_for_display

    if res.property_address and not (res.property_city and res.property_zip):
        parsed = parse_property_for_display(res.property_address)
        if parsed.get("city") and not res.property_city:
            res.property_city = parsed["city"][:128]
        if parsed.get("state") and not res.property_state:
            res.property_state = parsed["state"][:2]
        if parsed.get("zip") and not res.property_zip:
            res.property_zip = parsed["zip"][:10]
    for col, width in (("property_city", 128), ("property_state", 2), ("property_zip", 10)):
        val = gis_data.get(col)
        if val and not getattr(res, col):
            val = str(val).strip()
            if col == "property_state" and not re.fullmatch(r"[A-Za-z]{2}", val):
                continue  # only a clean 2-letter abbreviation is a state (Codex P2)
            setattr(res, col, val[:width])


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
                prop = gis_data.get("property_address")
                mail = gis_data.get("mailing_address")
                for res in parcel_map.get(pid, []):
                    # Migration 085 (#188) — capture the REAL situs parts BEFORE the
                    # assessor's street-only line replaces the scraper's fuller one.
                    # Runs for every branch below, including vacant land, so a parcel
                    # with no street still records WHERE it is.
                    _keep_situs_parts(res, gis_data)
                    if prop:
                        res.property_address = prop
                        # Only a REAL mailing overwrites (King never echoes the
                        # property into mailing — Codex): never clobber an existing
                        # value with None.
                        if mail:
                            res.mailing_address = mail
                        batch_updated += 1
                    elif mail:
                        # No street, but a real mailing (e.g. a Pierce parcel with a
                        # Delivery_Address but null Site_Address) — keep it rather
                        # than drop it into the vacant branch (Codex P2).
                        res.mailing_address = mail
                        batch_updated += 1
                    elif gis_data.get("vacant_no_situs"):
                        # Matched but no street (vacant/raw land, ~1/3 of King
                        # delinquent parcels). Keep property_address NULL — skip
                        # trace BILLS off it, so a city-only pseudo-address would
                        # buy a lookup for an address we do not have — but record
                        # WHERE the parcel is for display (Codex).
                        ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
                        ed["gis_matched"] = True
                        ed["vacant_no_situs"] = True
                        for k in ("situs_city", "situs_state", "situs_zip"):
                            if gis_data.get(k):
                                ed[k] = gis_data[k]
                        res.enrichment_data = ed
                        # #153 predates migration 085 and could only stash the situs
                        # in enrichment_data. The real columns exist now, and this is
                        # exactly what they are for: a vacant parcel with a known city
                        # and ZIP can still answer out_of_state_owner. Fill-only —
                        # never overwrite a value a real source already set.
                        for _col, _src, _w in (("property_city", "situs_city", 128),
                                               ("property_state", "situs_state", 2),
                                               ("property_zip", "situs_zip", 10)):
                            _v = gis_data.get(_src)
                            if _v and not getattr(res, _col, None):
                                setattr(res, _col, str(_v).strip()[:_w])
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

    # Pierce (WA): legal-description parcel repair + assessor (ATIP) address
    # fallback. Extracted so scripts/rerun_pierce_address_recovery.py can re-run
    # the SAME production path for an already-delivered job.
    pierce_address_recovery(db, r, job_id, config, all_results)

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
            from src.scrapers.enrichment.king_parcel_repair import owner_matches_party
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
            # Internal time budget (200s) returns PARTIAL results; the outer
            # wait_for(240) is only a last-resort kill switch. Either way the
            # rest of enrichment (owner repair, unactionable summary, SKIP-TRACE
            # ENQUEUE) must still run — before 2026-09-02 a TimeoutError here
            # aborted all of it on every large King tax job (172+ parcels).
            king_stats: dict = {}
            king_error: str | None = None
            try:
                # party_names lets the malformed-PID resolver break a tie between
                # several REAL candidate parcels by matching the assessor owner to
                # this lead's own party. Only consulted for a confirmed mismatch.
                party_names = {
                    pid: list(dict.fromkeys(
                        res.party_name for res in pid_map.get(pid, []) if res.party_name
                    ))
                    for pid in pids
                }
                enriched = asyncio.run(asyncio.wait_for(
                    batch_enrich_king_county(pids, time_budget_s=200, stats=king_stats,
                                             party_names=party_names),
                    timeout=240,
                ))
            except Exception as exc:  # noqa: BLE001 — best-effort county lookup
                king_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                _logger.warning("Job %s: King mailing lookup failed: %s", job_id, king_error)
                enriched = {}
                king_stats.setdefault("deferred", list(pids))
            # King tax-delinquent rows ship with a placeholder party_name because
            # the Socrata source has no owner column. The eRealProperty lookup
            # above now also yields the owner name; swap it in here. Dual gate:
            # job-level record_type (belt) + the exact placeholder shape
            # (suspenders, so probate/death-cert King rows sharing this enrichment
            # path are never touched).
            from src.scrapers.king_wa_tax_delinquent import is_tax_placeholder_party
            from src.utils.lead_formatting import classify_probate_title_status
            is_tax_delinquent = config.record_type == "tax_delinquent"
            # Probate + death-cert: party_name is the DECEASED. The Assessor's owner
            # is who holds title NOW — often an heir/trust. Surface it + a conservative
            # flag so the user isn't mailing a decedent.
            is_probate_family = config.record_type in ("probate", "death_certificate")
            for pid, data in enriched.items():
                prop = data.get("property_address")
                mail = data.get("mailing_address")
                owner = data.get("owner_name")
                for res in pid_map.get(pid, []):
                    if data.get("resolved_by") == "gis_plus_owner_match" and not (
                        # Compare against the assessor OWNER that actually proved the
                        # parcel, not against the other lead's party. Party-to-party
                        # is NON-TRANSITIVE: "SMITH JOHN B" matches "SMITH JOHN" but
                        # not owner "SMITH JOHN A", so gating on the party would hand
                        # B the parcel A's evidence chose (Codex P1).
                        owner_matches_party(res.party_name, data.get("resolved_owner_match"))
                    ):
                        # The parcel was resolved by matching ANOTHER lead's party.
                        # Two leads can share one malformed PID with different
                        # parties, and that evidence does not transfer (Codex P1).
                        continue
                    if prop and not res.property_address:
                        res.property_address = prop
                    if prop and not res.property_zip:
                        # eRealProperty's Site Address sometimes ends in the ZIP
                        # ("2019 SW 318TH PL 4C 98023"): anchored trailing token only,
                        # no city inferred from it (Codex).
                        _z = _TRAILING_ZIP_RE.search(prop)
                        if _z:
                            res.property_zip = _z.group(1)
                    if mail:
                        res.mailing_address = mail
                    if (
                        is_tax_delinquent
                        and owner
                        and (not res.party_name or is_tax_placeholder_party(res.party_name))
                    ):
                        res.party_name = owner
                    # Display-only: record the Assessor's current owner + a humble
                    # "differs/entity" flag. NEVER overwrite party_name, NEVER drop the
                    # lead (Assessor lag; heirs are valid motivated sellers).
                    if is_probate_family and owner:
                        ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
                        ed["assessor_current_owner"] = owner
                        ed["title_status"] = classify_probate_title_status(res.party_name, owner)
                        res.enrichment_data = ed
                    # The county printed a malformed parcel and we recovered the
                    # real one. parcel_id STAYS as the county printed it (it feeds
                    # the frozen dedup_hash); the resolved PIN + the evidence that
                    # chose it are recorded beside it (Codex).
                    if data.get("parcel_lookup") in ("recovered", "mismatch"):
                        ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
                        ed["parcel_lookup"] = data["parcel_lookup"]
                        for k, v in data.items():
                            if k.startswith("resolved_") or k == "source_parcel_id":
                                ed[k] = v
                        res.enrichment_data = ed
            try:
                db.commit()
            except Exception as exc:
                _logger.warning(
                    "Job %s: King enrichment commit failed: %s", job_id, str(exc)[:120]
                )
                db.rollback()
                db.commit()
            found = sum(1 for d in enriched.values() if d.get("mailing_address"))
            # Durable marker for parcels the budget/cap/failure never reached, so a
            # later sweep can find them (never a silent gap — Codex).
            deferred = [p for p in dict.fromkeys(king_stats.get("deferred", [])) if p in pid_map]
            for pid in deferred:
                for res in pid_map.get(pid, []):
                    if res.mailing_address:
                        continue
                    ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
                    ed["mailing_lookup_deferred"] = True
                    res.enrichment_data = ed
            if deferred:
                try:
                    db.commit()
                except Exception as exc:
                    _logger.warning("Job %s: deferred-marker commit failed: %s", job_id, str(exc)[:120])
                    db.rollback()
            if king_error or deferred:
                _publish_log(
                    r, job_id, "warning",
                    f"King County mailing lookup stopped early: {len(pids)} parcels requested, "
                    f"{king_stats.get('mailing_attempted', 0)} looked up, {found} mailing addresses found; "
                    f"{len(deferred)} deferred (property address kept)"
                    + (f" — {king_error}" if king_error else ""),
                    db=db,
                )
            else:
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
                from src.scrapers.enrichment.king_county_assessor import (
                    KingOwnerLookupBlockedError,
                    batch_extract_king_owners,
                )
                from src.scrapers.enrichment.source_health import SourceUnavailableError
                o_pid_map: dict[str, list] = {}
                for res in owner_needs:
                    o_pid_map.setdefault(res.parcel_id.strip(), []).append(res)
                o_pids_all = list(o_pid_map.keys())
                # Keep inline eRealProperty owner repair tiny. Bulk owner fill is
                # operationally sensitive and belongs in a monitored/offline flow
                # backfill script — logged here so the cap is never silent.
                _MAX_KING_OWNER_PARCELS = 25
                overflow = max(0, len(o_pids_all) - _MAX_KING_OWNER_PARCELS)
                o_pids = o_pids_all[:_MAX_KING_OWNER_PARCELS]
                if overflow:
                    _logger.info(
                        "King owner lookup capped at %d/%d parcels this job; %d deferred to backfill",
                        _MAX_KING_OWNER_PARCELS, len(o_pids_all), overflow,
                    )
                try:
                    owners = asyncio.run(asyncio.wait_for(
                        batch_extract_king_owners(
                            o_pids,
                            delay=1.0,
                            circuit_window=20,
                            max_transient_rate=0.10,
                            max_unresolved_rate=0.50,
                            fetch_attempts=1,
                        ),
                        timeout=180,
                    ))
                except (KingOwnerLookupBlockedError, SourceUnavailableError) as exc:
                    _logger.warning(
                        "Job %s: King owner-only lookup aborted: %s",
                        job_id, str(exc)[:180],
                    )
                    try:
                        _publish_log(
                            r, job_id, "warning",
                            "Owner-name resolution paused because King County appears to be throttling lookups.",
                            db=db,
                        )
                    except Exception:
                        db.rollback()
                    owners = {}
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
        # Break down WHY so a genuinely-unrecoverable row (no parcel AND no legal
        # — e.g. a probate court filing with no property recorded) is
        # distinguished from an enrichment gap. The "legal but no parcel" bucket
        # is the signal to revisit a parcel-less legal fallback if it ever grows
        # (today it is 0 for Pierce probate) — Codex.
        def _has(v) -> bool:
            return bool(v and str(v).strip())
        no_parcel_no_legal = sum(
            1 for res in unactionable
            if not _has(res.parcel_id) and not _has(res.legal_description)
        )
        has_parcel = sum(1 for res in unactionable if _has(res.parcel_id))
        legal_no_parcel = sum(
            1 for res in unactionable
            if _has(res.legal_description) and not _has(res.parcel_id)
        )
        _publish_log(
            r, job_id, "info",
            f"{len(unactionable)} records have no deliverable address "
            f"(no parcel+legal: {no_parcel_no_legal}, has parcel: {has_parcel}, "
            f"legal-only: {legal_no_parcel})",
            db=db,
        )
        _logger.info(
            "Job %s: %d/%d unactionable — no_parcel_no_legal=%d has_parcel=%d legal_no_parcel=%d",
            job_id, len(unactionable), len(fresh),
            no_parcel_no_legal, has_parcel, legal_no_parcel,
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


def pierce_address_recovery(db, r, job_id: str, config, all_results) -> None:
    """Pierce/WA only: (1) repair a recorder-typo parcel from the legal description
    (free GIS, strict guards), then (2) fill still-missing addresses from the
    assessor portal (ATIP, captcha-gated, address only). Both steps are fill-missing,
    commit their own work and publish job-log lines; a source failure never raises.
    Called inline at the end of every job's enrichment and by
    scripts/rerun_pierce_address_recovery.py for an existing job.
    """
    # Pierce probate + pre_foreclosure: repair a typo'd parcel_id from the legal
    # description. ARMS occasionally indexes a non-existent parcel (wrong plat
    # prefix, one substituted digit, or a dropped digit), so the GIS-by-parcel
    # pass above yields no address. Recover the property from the legal and
    # replace the CONFIRMED-nonexistent parcel with the assessor's own, under
    # strict guards (src/scrapers/enrichment/pierce_legal_repair.py): hard GIS
    # negative -> exact plat/lot(/block) legal filters -> then the parcel guard.
    _is_pierce = config.county.lower() == "pierce" and config.state.upper() == "WA"
    if _is_pierce and config.record_type in ("probate", "pre_foreclosure"):
        from src.scrapers.enrichment.pierce_legal_repair import (
            find_pierce_parcels_by_legal,
            legal_plat_adjacent,
            parcel_hard_negative,
            parcel_repair_method,
            parse_pierce_legal,
            same_lot_suffix,
        )
        repair_targets = [
            res for res in all_results
            if res.legal_description and not res.property_address
            and res.parcel_id and len(res.parcel_id.strip()) >= 6
        ]
        repaired = 0
        for res in repair_targets:
            # Only touch a parcel Pierce GIS PROVABLY lacks (hard negative) —
            # never a transient lookup failure (Codex P1).
            if not parcel_hard_negative(res.parcel_id):
                continue
            legal_matches = [
                m for m in find_pierce_parcels_by_legal(res.legal_description)
                if m.get("property_address")
            ]
            if not legal_matches:
                continue
            parsed_legal = parse_pierce_legal(res.legal_description) or (None, None, None)
            if len(legal_matches) == 1:
                # ONE exact-legal survivor: accept the shared-suffix class OR a
                # single-digit recorder typo (edit distance 1). The parcel guard
                # runs AFTER the legal filters and never chooses between
                # neighbours (Codex). The edit-1 class has no lot-suffix anchor,
                # so it additionally requires the GIS legal to name the plat
                # IMMEDIATELY before the lot (no "DIV 2"-style qualifier between).
                only = legal_matches[0]
                method = parcel_repair_method(res.parcel_id, only["parcel_id"])
                if method == "plat_lot_unique_edit1" and not (
                    parsed_legal[0]
                    and legal_plat_adjacent(
                        only.get("gis_legal_description"), parsed_legal[0], parsed_legal[1], parsed_legal[2]
                    )
                ):
                    method = None
                candidates = [only] if method else []
            else:
                # Several subdivisions share the plat+lot: ONLY the 6-digit lot
                # suffix may disambiguate, and it must leave exactly one.
                candidates = [
                    m for m in legal_matches if same_lot_suffix(res.parcel_id, m["parcel_id"])
                ]
                method = "plat_lot_unique_suffix"
            if len(candidates) != 1:
                continue
            match = candidates[0]
            old_parcel = res.parcel_id
            res.property_address = match["property_address"]
            if match.get("mailing_address"):
                res.mailing_address = match["mailing_address"]
            res.parcel_id = match["parcel_id"]
            ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
            ed.update({
                "parcel_source": "gis_legal_match",
                "raw_scraped_parcel": old_parcel,
                "gis_match_parcel": match["parcel_id"],
                "gis_match_method": method,
                "gis_legal_description": match.get("gis_legal_description"),
                "gis_legal_parsed": {"plat": parsed_legal[0], "lot": parsed_legal[1], "block": parsed_legal[2]},
                "gis_legal_survivors": len(legal_matches),  # audit: exact-lot survivors
                "gis_suffix_matches": len(candidates),       # audit: after the parcel guard
                "gis_parcel_hard_negative": True,            # scraped parcel confirmed absent
            })
            res.enrichment_data = ed  # reassign so SQLAlchemy flags the JSON dirty
            repaired += 1
        if repair_targets:
            try:
                db.commit()
            except Exception as exc:
                _logger.warning("Job %s: Pierce legal-repair commit failed: %s", job_id, str(exc)[:120])
                db.rollback()
            _publish_log(
                r, job_id, "info",
                f"Pierce legal repair: recovered {repaired}/{len(repair_targets)} "
                "addresses + corrected parcel typos from legal description",
                db=db,
            )

    # Pierce assessor (ATIP) fallback for parcels the GIS layers cannot resolve —
    # in practice personal-property MOBILE HOME accounts (a Notice of Foreclosure
    # by a mobile-home park). Paid (captcha solve, ~$0.003/batch) so it runs LAST,
    # only for rows still without a property address after the free GIS + legal
    # passes, and only takes the ADDRESS (never the taxpayer name — see the
    # module docstring for the RCW 42.56.070(8) boundary). Fill-missing only.
    if _is_pierce:
        atip_targets = [
            res for res in all_results
            if not res.property_address and res.parcel_id and len(res.parcel_id.strip()) >= 6
        ]
        if atip_targets:
            from src.scrapers.enrichment.pierce_atip import lookup_atip_addresses
            _publish_log(
                r, job_id, "info",
                f"Looking up {len(atip_targets)} addresses via the Pierce assessor...",
                db=db,
            )
            atip_map: dict[str, list] = {}
            for res in atip_targets:
                atip_map.setdefault(res.parcel_id.strip(), []).append(res)
            atip_results, atip_stats = lookup_atip_addresses(list(atip_map.keys()))
            atip_filled = 0
            for pid, data in atip_results.items():
                for res in atip_map.get(pid, []):
                    filled_fields: list[str] = []
                    if not res.property_address and data.get("property_address"):
                        res.property_address = data["property_address"]
                        filled_fields.append("property_address")
                    if not res.mailing_address and data.get("mailing_address"):
                        res.mailing_address = data["mailing_address"]
                        filled_fields.append("mailing_address")
                    if not filled_fields:
                        continue
                    # Provenance (audit boundary — ADDRESS only, never the taxpayer
                    # name): which parcel/account was queried and which fields it filled.
                    ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
                    ed.update({
                        "address_source": "pierce_atip",
                        "atip_parcel": pid,
                        "atip_filled_fields": filled_fields,
                        "atip_account_type": data.get("atip_account_type"),
                        "atip_use_code": data.get("atip_use_code"),
                    })
                    res.enrichment_data = ed
                    if "property_address" in filled_fields:
                        atip_filled += 1
            try:
                db.commit()
            except Exception as exc:
                _logger.warning("Job %s: ATIP enrichment commit failed: %s", job_id, str(exc)[:120])
                db.rollback()
            _publish_log(
                r, job_id, "info",
                f"Assessor lookup: {atip_filled}/{len(atip_targets)} addresses found "
                f"({atip_stats['not_found']} not on file, {atip_stats['hard_failure']} errors)",
                db=db,
            )


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
        legacy_cache_locality,
    )
    from src.utils.address_intel import street_is_placeholder

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

    # Reload the surviving results after the unactionable drop. Exclude is_duplicate
    # rows: a duplicate is never delivered or billed as a lead, so paying Tracerfy for
    # it is pure waste. _reuse_enrichment_for_duplicates (run first) already copies a
    # SETTLED prior trace onto cross-job dupes that have one; the remainder — including
    # the same-job siblings the trustee_sale collapse marks, which have no prior row to
    # copy from — must NOT be enqueued for a fresh paid lookup (Codex).
    from src.api.lead_actionability import actionable_condition

    eligible = db.execute(
        sa_select(Result).where(
            Result.job_id == job_id,
            Result.user_id == job.user_id,
            Result.property_address.isnot(None),
            # Standing rule: a quarantined row (no real property AND no mailing
            # address, incl. "(enrichment unavailable)" / blanks) is not a lead —
            # never pay Tracerfy for it (Codex).
            actionable_condition(),
            Result.skip_trace_status == "not_attempted",
            Result.is_duplicate.is_(False),
        )
    ).scalars().all()

    # A PLACEHOLDER street is not an address, and skip trace bills per lookup.
    # Worse than the money: address_cache_key() hashes the ADDRESS, so every row
    # sharing one placeholder string collapses to ONE cache key — measured in
    # production 2026-09-03, 'UNKNOWN UNKNOWN, UNKNOWN WA' is shared by 328
    # DISTINCT parcels. A single Tracerfy result would then be copied onto all 328
    # unrelated leads, stamping one person's phone/email across properties they have
    # nothing to do with. Nothing has been traced yet (all 408 rows are
    # 'not_attempted'), so this gate is preventative, not a cleanup (Codex).
    placeholder_rows = [rec for rec in eligible if street_is_placeholder(rec.property_address)]
    if placeholder_rows:
        eligible = [rec for rec in eligible if not street_is_placeholder(rec.property_address)]
        _publish_log(
            r, job_id, "warning",
            f"Skip trace skipped for {len(placeholder_rows)} lead(s): the county supplied a "
            "placeholder property address, so a lookup would be billed against an address "
            "we do not have",
            db=db,
        )

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
        if cached is None:
            # Miss under the current key: this row may already be PAID FOR under
            # the pre-2026-09-03 key, when the locality came from the owner's
            # mailing address instead of the property's own situs. Those differ
            # for every absentee owner, so without this second look we would buy
            # the same address twice. Read-only convergence — no alias row is
            # written, so no duplicate PII is stored (Codex: the dual-read
            # belongs at enqueue, not in tracerfy_ingest).
            _legacy_city, _legacy_state = legacy_cache_locality(rec)
            if (_legacy_city, _legacy_state) != (payload["city"], payload["state"]):
                cached = db.get(SkipTraceCache, address_cache_key(
                    job.user_id,
                    payload["property_address"],
                    _legacy_city,
                    _legacy_state,
                ))
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
