"""NTS matcher (DB + beat): attach matched auction data onto pre_foreclosure leads.

Loads active, future-dated nts_notices and the unmatched pre_foreclosure Results
they could belong to, runs the pure scorer (scrapers/sources/nts_matcher), and on
a high-confidence UNAMBIGUOUS match writes the auction columns + enrichment_data
["nts"] onto the Result. The scorer is the false-match firewall; this module is
just the plumbing.

Two entry points (Codex): a daily beat task that re-matches recent leads against the
freshly-crawled cache, and match_results_inline() the scrape pipeline calls at the end
of a pre_foreclosure job for that job's rows only. Both scope to
record_type=pre_foreclosure and a single county.

Notices are considered in TWO ordered passes (2026-09-04). The live pass is the original
behavior: active, future-dated notices. The historical pass then attaches an
already-past sale to a lead still carrying nothing — a lead whose notice ran before we
scraped it used to show a blank Auction Date / Default Owed that was indistinguishable
from "this county's source has no notice for this property", when we in fact held the
real sale date and the real amount owed. Live always wins: a live notice may replace an
attached PAST date (so a postponed or re-noticed sale is not frozen on the stale one),
while a historical notice only ever claims a lead that is still unset.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("workers.nts_matcher")

# Beat re-match window for un-enriched pre_foreclosure leads, anchored on the
# Result's created_at. It must cover the STATUTORY lag between a lead being
# recorded and its trustee-sale notice reaching the newspaper cache:
# RCW 61.24.040(1) records the notice of sale >= 90 days (120 with a 61.24.031
# letter) before the sale, and 61.24.040(5) publishes it between the 35th–28th and
# 14th–7th day before the sale — so publication lands ~55–150 days AFTER recording
# (prod-observed recording→auction gap on matched leads: mostly 114–179 days). The
# previous 45-day window aged leads out before their notice was ever published:
# 2026-09-02 audit found 21 Pierce leads (created 6/23–7/1) with an EXACT parcel
# match to an ACTIVE notice fetched 9/2 that were never enriched. 180 days from
# creation covers the horizon (a lead is created no later than its scrape, which
# is itself after recording); candidate volume is small (~1.8k rows total).
# Caveat (Codex): an exact-parcel active notice attached to an older unmatched lead
# CAN be a later re-notice of the same property after a cured default — still the
# same property in active distress (the product's "urgency clock"), but not
# necessarily the same recorded instrument as that lead's row.
_RECENT_DAYS = 180

# Counties with an NTS cache source wired up (Pierce=Tacoma Daily Index, Snohomish=
# Snohomish County Tribune, King=Queen Anne & Magnolia News, Clark=The Columbian
# classifieds). Matching is scoped PER COUNTY — a notice only ever matches a lead in the
# SAME county — so a same street+zip in a different county can never cross-match (the
# address key isn't county-unique).
NTS_MATCH_COUNTIES = ("pierce", "snohomish", "king", "clark")

# How far back a HISTORICAL (already-held) sale may be attached to a lead. Bounded so a
# long-past sale is never presented as this lead's event: an auction older than this is
# no longer describing the lead's current distress. Matches the crawler's cache horizon.
_PAST_AUCTION_DAYS = 180
# Mirrors nts_crawler._CACHE_DAYS. is_active cannot filter staleness for past sales (the
# expiry pass flips it false the day an auction passes, for every notice), so the past
# pass filters on fetched_at against the same horizon instead.
_CACHE_DAYS = 90


@app.task(name="src.workers.nts_matcher_task.match_nts_notices")
def match_nts_notices() -> dict:
    """Beat: match active notices onto recent unmatched pre_foreclosure leads, per county."""
    from sqlalchemy import text as _sa_text

    from src.db.session import system_sync_session

    cutoff = datetime.now(UTC) - _td_days(_RECENT_DAYS)
    total_candidates = total_matched = 0
    with system_sync_session() as db:
        for county in NTS_MATCH_COUNTIES:
            result_rows = db.execute(
                _sa_text(
                    """
                    SELECT r.id, r.parcel_id, r.property_address, r.party_name
                    FROM results r JOIN jobs j ON j.id = r.job_id
                    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
                    WHERE sc.record_type = 'pre_foreclosure'
                      AND lower(sc.county) = :county
                      -- NULL *or already past*: a lead carrying a historical sale must
                      -- stay a candidate so a later live re-notice can replace it.
                      AND (r.auction_date IS NULL OR r.auction_date < :today)
                      AND r.created_at >= :cutoff
                    """
                ),
                {"county": county, "cutoff": cutoff,
                 "today": datetime.now(UTC).date()},
            ).fetchall()
            matched = _match_and_write(db, [dict(r._mapping) for r in result_rows], county=county)
            total_candidates += len(result_rows)
            total_matched += matched
    _logger.info("NTS match (beat): %d leads enriched from %d candidates across %s",
                 total_matched, total_candidates, ",".join(NTS_MATCH_COUNTIES))
    return {"candidates": total_candidates, "matched": total_matched}


def match_results_inline(db, result_dicts: list[dict[str, Any]], county: str) -> int:
    """Match the given Result rows (id/parcel_id/property_address/party_name). Caller commits.

    Only rows whose auction_date is already None should be passed. `county` scopes the
    notices considered (must be the leads' county) so matching stays county-aligned.
    """
    return _match_and_write(db, result_dicts, county=county, commit=False)


def match_job_inline(db, job_id: str) -> int:
    """Inline path for the scrape pipeline: match THIS job's unmatched pre_foreclosure
    Results. Queries the job's rows itself + commits, so the caller's later
    post-enrichment refetch + re-export see the auction fields (the write must land
    before the refetch — Codex). Returns the number of leads enriched.

    Derives the job's county from its scraper_config so it matches against the SAME
    county's notices (county-aligned). A job whose county has no NTS source matches
    nothing (empty notice set) — harmless.
    """
    from sqlalchemy import text as _sa_text

    county = db.execute(
        _sa_text(
            """
            SELECT lower(sc.county)
            FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id
            WHERE j.id = :jid
            """
        ),
        {"jid": job_id},
    ).scalar()
    if not county:
        return 0
    rows = db.execute(
        _sa_text(
            """
            SELECT id, parcel_id, property_address, party_name
            FROM results
            WHERE job_id = :jid
              AND user_id = (SELECT user_id FROM jobs WHERE id = :jid)
              AND (auction_date IS NULL OR auction_date < :today)
            """
        ),
        {"jid": job_id, "today": datetime.now(UTC).date()},
    ).fetchall()
    return _match_and_write(db, [dict(r._mapping) for r in rows], county=county, commit=True)


def _match_and_write(
    db, result_dicts: list[dict[str, Any]], *, county: str, commit: bool = True
) -> int:
    """Core: index candidates, score each active notice (scoped to `county`), write wins.

    Only notices for `county` are considered, and the candidates are that county's
    leads — so a notice can never match a lead in another county (the scorer keys on
    parcel/address, which aren't globally county-unique).
    """
    from sqlalchemy import text as _sa_text

    from src.scrapers.sources.nts_matcher import best_match_group, result_match_candidate

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
    _COLS = """
            SELECT id, parcel, property_address, property_address_normalized,
                   grantor, auction_date, auction_time, auction_location, trustee,
                   beneficiary, ts_number, principal_owing, source, source_url
            FROM nts_notices
    """
    live = db.execute(
        _sa_text(
            _COLS + """
            WHERE is_active AND auction_date IS NOT NULL AND auction_date >= :today
              AND lower(county) = :county
            ORDER BY auction_date ASC, id
            """
        ),
        {"today": today, "county": county},
    ).fetchall()
    # Second pass: sales that have ALREADY happened. A lead whose notice was published
    # before we scraped it (or whose sale ran while the cache was stale) used to render
    # a blank Auction Date / Default Owed — indistinguishable from "this source has no
    # notice", when in fact we hold the real sale date and the real amount owed. A past
    # date is factual and useful (the sale already ran); a blank is just less
    # information. Bounded lookback so we never attach an ancient sale, and is_active is
    # NOT usable here — the expiry pass flips it false the day an auction passes — so
    # staleness is filtered on fetched_at instead, the same _CACHE_DAYS horizon.
    # Deliberately a SEPARATE, LOWER-priority pass: a live sale must always win over a
    # past one for the same property (a postponed or re-noticed sale is the value).
    past = db.execute(
        _sa_text(
            _COLS + """
            WHERE auction_date IS NOT NULL AND auction_date < :today
              AND auction_date >= :floor AND lower(county) = :county
              AND fetched_at IS NOT NULL AND fetched_at >= :stale_before
            ORDER BY auction_date DESC
            """
        ),
        {"today": today, "floor": today - _td_days(_PAST_AUCTION_DAYS),
         "stale_before": datetime.now(UTC) - _td_days(_CACHE_DAYS),
         "county": county},
    ).fetchall()
    notices = list(live) + list(past)
    live_ids = {r._mapping["id"] for r in live}

    matched = 0
    used_result_ids: set = set()
    from src.scrapers.preforeclosure import strip_trailing_labels

    for n in notices:
        nm = dict(n._mapping)
        # An already-cached grantor may have bled-in downstream labels appended to the
        # owner name ("<owner> Grantee(s): <trustee> ..."). Strip them before scoring so
        # the name signal matches on the real owner — self-heals stale rows without a
        # backfill. (The parser _STOP fix keeps freshly-crawled grantors clean.)
        nm["grantor"] = strip_trailing_labels(nm.get("grantor"))
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
        # Attach the PUBLIC auction data to EVERY Result for the same property —
        # multiple tenants can each track the same foreclosure, and they should
        # all get it (best_match's single-winner bail silently dropped that case).
        # best_match_group still returns [] on a different-property tie (ambiguous).
        group = best_match_group(nm, candidates)
        is_live = nm["id"] in live_ids
        for rid, conf in group:
            used_result_ids.add(rid)
            # Count only an actual write — the guard in _write_match means a row a
            # concurrent beat/inline pass already claimed updates 0 rows (Codex).
            if _write_match(db, rid, nm, conf, live=is_live, today=today) == 1:
                matched += 1

    if commit and matched:
        db.commit()
    return matched


def _write_match(
    db, result_id: Any, notice: dict, confidence: float, *, live: bool = True, today=None
) -> int:
    """Write the auction columns + enrichment_data['nts'] onto one Result.

    enrichment_data is merged (not overwritten) so existing scrape/enrichment keys
    survive. Returns the rowcount (1 = written, 0 = another pass already claimed it).

    Two claim rules, because attaching PAST sales made the old blanket
    "only if auction_date IS NULL" guard wrong in one direction:

      live=True   claims a row that is unset OR already carries a PAST auction. Without
                  this, a sale postponed or re-noticed to a new date could never
                  replace the stale one we had already attached — the lead would be
                  frozen on a sale that no longer happens, which is worse than blank.
      live=False  claims a row that is unset, or one holding a STRICTLY OLDER past
                  sale. It can never touch a row holding a live (future) sale, and
                  never moves an attachment backwards in time.

                  The "strictly older" clause is not redundant with the pass ordering
                  (Codex P1): the archive sweep is capped at _ARCHIVE_MAX_FETCH issues
                  per run, so during a multi-week catch-up the matcher can attach an old
                  sale on Monday and only learn about the newer re-notice on Tuesday.
                  Ordering alone only holds WITHIN one run.
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
    res = db.execute(
        _sa_text(
            """
            UPDATE results SET
                auction_date = :auction_date,
                default_amount = :default_amount,
                nts_match_confidence = :confidence,
                nts_notice_id = :notice_id,
                enrichment_data = COALESCE(enrichment_data, '{}'::json)::jsonb
                                  || jsonb_build_object('nts', CAST(:nts AS jsonb))
            WHERE id = :rid
              AND (auction_date IS NULL
                   OR (:live AND auction_date < :today)
                   OR (NOT :live AND auction_date < :today
                       AND auction_date < :auction_date))
            """
        ),
        {
            "auction_date": notice.get("auction_date"),
            "default_amount": notice.get("principal_owing"),
            "confidence": confidence,
            "notice_id": notice.get("id"),
            "nts": json.dumps(nts_blob),
            "rid": result_id,
            "live": bool(live),
            "today": today or datetime.now(UTC).date(),
        },
    )
    return res.rowcount or 0


def _td_days(days: int):
    from datetime import timedelta
    return timedelta(days=days)
