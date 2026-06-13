"""NTS matcher (DB + beat): attach matched auction data onto pre_foreclosure leads.

Loads active, future-dated nts_notices and the unmatched pre_foreclosure Results
they could belong to, runs the pure scorer (scrapers/sources/nts_matcher), and on
a high-confidence UNAMBIGUOUS match writes the auction columns + enrichment_data
["nts"] onto the Result. The scorer is the false-match firewall; this module is
just the plumbing.

Two entry points (Codex): a daily beat task that re-matches recent Pierce leads
against the freshly-crawled cache, and match_results_inline() the scrape pipeline
calls at the end of a Pierce pre_foreclosure job for that job's rows only. Both
scope to: county=pierce, record_type=pre_foreclosure, auction_date IS NULL
(never re-match or clobber an already-matched lead), notices active + future.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("workers.nts_matcher")

_RECENT_DAYS = 45  # beat re-match window for un-enriched Pierce pre_foreclosure leads


@app.task(name="src.workers.nts_matcher_task.match_nts_notices")
def match_nts_notices() -> dict:
    """Beat: match active Pierce notices onto recent unmatched pre_foreclosure leads."""
    from sqlalchemy import text as _sa_text

    from src.db.session import system_sync_session

    cutoff = datetime.now(UTC) - _td_days(_RECENT_DAYS)
    with system_sync_session() as db:
        result_rows = db.execute(
            _sa_text(
                """
                SELECT r.id, r.parcel_id, r.property_address, r.party_name
                FROM results r JOIN jobs j ON j.id = r.job_id
                JOIN scraper_configs sc ON sc.id = j.scraper_config_id
                WHERE sc.record_type = 'pre_foreclosure'
                  AND lower(sc.county) = 'pierce'
                  AND r.auction_date IS NULL
                  AND r.created_at >= :cutoff
                """
            ),
            {"cutoff": cutoff},
        ).fetchall()
        matched = _match_and_write(db, [dict(r._mapping) for r in result_rows])
    _logger.info("NTS match (beat): %d leads enriched from %d candidates", matched, len(result_rows))
    return {"candidates": len(result_rows), "matched": matched}


def match_results_inline(db, result_dicts: list[dict[str, Any]]) -> int:
    """Inline path: match the given job's pre_foreclosure Result rows. Caller commits.

    `result_dicts` carry id/parcel_id/property_address/party_name (already loaded by
    the scrape pipeline). Only rows with auction_date already None should be passed.
    """
    return _match_and_write(db, result_dicts, commit=False)


def _match_and_write(db, result_dicts: list[dict[str, Any]], commit: bool = True) -> int:
    """Core: index candidates, score each active notice, write the unambiguous wins."""
    from sqlalchemy import text as _sa_text

    from src.scrapers.sources.nts_matcher import best_match, result_match_candidate

    if not result_dicts:
        return 0

    # Build matcher candidates (precompute addr_key) + indexes by addr_key + parcel.
    from src.scrapers.sources.nts_matcher import _norm_parcel
    cands = [result_match_candidate(r) for r in result_dicts]
    by_addr: dict[str, list[dict]] = {}
    by_parcel: dict[str, list[dict]] = {}
    for c in cands:
        if c["addr_key"]:
            by_addr.setdefault(c["addr_key"], []).append(c)
        np_ = _norm_parcel(c["parcel"])
        if np_:
            by_parcel.setdefault(np_, []).append(c)

    today = datetime.now(UTC).date()
    notices = db.execute(
        _sa_text(
            """
            SELECT id, parcel, property_address, property_address_normalized,
                   grantor, auction_date, auction_time, auction_location, trustee,
                   beneficiary, ts_number, principal_owing, source, source_url
            FROM nts_notices
            WHERE is_active AND auction_date IS NOT NULL AND auction_date >= :today
              AND lower(county) = 'pierce'
            """
        ),
        {"today": today},
    ).fetchall()

    matched = 0
    used_result_ids: set = set()
    for n in notices:
        nm = dict(n._mapping)
        # Candidate Results for this notice = union of same addr_key + same parcel.
        pool: dict[Any, dict] = {}
        if nm.get("property_address_normalized"):
            for c in by_addr.get(nm["property_address_normalized"], []):
                pool[c["id"]] = c
        npn = _norm_parcel(nm.get("parcel"))
        if npn:
            for c in by_parcel.get(npn, []):
                pool[c["id"]] = c
        # Don't let one Result be claimed by two notices in the same pass.
        candidates = [c for c in pool.values() if c["id"] not in used_result_ids]
        if not candidates:
            continue
        hit = best_match(nm, candidates)
        if hit is None:
            continue
        rid, conf = hit
        used_result_ids.add(rid)
        _write_match(db, rid, nm, conf)
        matched += 1

    if commit and matched:
        db.commit()
    return matched


def _write_match(db, result_id: Any, notice: dict, confidence: float) -> None:
    """Write the auction columns + enrichment_data['nts'] onto one Result.

    enrichment_data is merged (not overwritten) so existing scrape/enrichment keys
    survive. Only fills auction fields that are still NULL — never clobbers.
    """
    from sqlalchemy import text as _sa_text

    nts_blob = {
        "ts_number": notice.get("ts_number"),
        "trustee": notice.get("trustee"),
        "beneficiary": notice.get("beneficiary"),
        "auction_time": notice.get("auction_time"),
        "auction_location": notice.get("auction_location"),
        "source": notice.get("source"),
        "source_url": notice.get("source_url"),
        "matched_at": datetime.now(UTC).isoformat(),
        "confidence": confidence,
    }
    import json
    db.execute(
        _sa_text(
            """
            UPDATE results SET
                auction_date = :auction_date,
                default_amount = :default_amount,
                nts_match_confidence = :confidence,
                nts_notice_id = :notice_id,
                enrichment_data = COALESCE(enrichment_data, '{}'::json)::jsonb
                                  || jsonb_build_object('nts', CAST(:nts AS jsonb))
            WHERE id = :rid AND auction_date IS NULL
            """
        ),
        {
            "auction_date": notice.get("auction_date"),
            "default_amount": notice.get("principal_owing"),
            "confidence": confidence,
            "notice_id": notice.get("id"),
            "nts": json.dumps(nts_blob),
            "rid": result_id,
        },
    )


def _td_days(days: int):
    from datetime import timedelta
    return timedelta(days=days)
