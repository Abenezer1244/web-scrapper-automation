"""Trustee Sale finalizer — populate Result auction columns DIRECTLY from source.

Unlike ``pre_foreclosure`` (``nts_matcher_task``, fuzzy parcel/address match), a
``trustee_sale`` lead IS a specific ``nts_notices`` row: the scraper stamped that
row's id + auction fields into ``enrichment_data["nts_source"]``. This writes
``results.auction_date`` / ``default_amount`` / ``nts_notice_id`` + the
``enrichment_data["nts"]`` blob (the SAME shape ``pre_foreclosure`` produces, so
exports/UI are byte-identical), keyed on that known id — no matching, no ambiguity.

FAIL-CLOSED (Codex High): if any of the job's ``trustee_sale`` results ends without
``auction_date`` + ``nts_notice_id``, raise ``TrusteeSaleFinalizeError``. The scraper
only emits notices with ``auction_date >= today``, so a NULL here means the pipeline
broke — never deliver an Auction Lead with blank auction/default/trustee data (the
exact bug this feature exists to prevent). The worker runs this BEFORE billing, so a
raise fails the job without charging the user.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text as _sa_text

from src.utils.logger import setup_logger

_logger = setup_logger("workers.trustee_sale_finalize")


class TrusteeSaleFinalizeError(RuntimeError):
    """A trustee_sale job could not be fully populated with auction data."""


def _nts_update_params(row_id: Any, src: dict) -> dict:
    """Build the UPDATE params for one result from its ``nts_source`` blob.

    Pure (no DB) so the fail-closed contract is unit-testable. Raises
    ``TrusteeSaleFinalizeError`` if ``notice_id`` is missing — the one field the
    scraper always sets and the finalizer cannot proceed without.
    """
    notice_id = src.get("notice_id")
    if not notice_id:
        raise TrusteeSaleFinalizeError(
            f"result {row_id} has no enrichment_data['nts_source']['notice_id'] — "
            "trustee_sale scraper contract broken (cannot populate auction data)"
        )
    # Same blob shape nts_matcher_task._write_match writes, so export/UI read
    # enrichment_data['nts'] identically. confidence=1.0: exact source row, not fuzzy.
    nts_blob = {
        "ts_number": src.get("ts_number"),
        "trustee": src.get("trustee"),
        "beneficiary": src.get("beneficiary"),
        "auction_time": src.get("auction_time"),
        "auction_location": src.get("auction_location"),
        "source": src.get("source"),
        "source_url": src.get("source_url"),
        "matched_at": datetime.now(UTC).isoformat(),
        "confidence": 1.0,
    }
    return {
        "auction_date": src.get("auction_date"),
        "default_amount": src.get("default_amount"),
        "notice_id": notice_id,
        "nts": json.dumps(nts_blob),
        "rid": row_id,
    }


def _sibling_duplicate_ids(rows: list[dict]) -> list:
    """Ids to mark duplicate so each ``dedup_hash`` keeps ONE (soonest-auction) row.

    Groups by ``dedup_hash`` — the app-wide billing key (parcel|address) — because the
    product decision (2026-07-03) is that Auction Leads dedups EXACTLY like every other
    list, no more aggressively. The shared cross-job scan enforces one-per-hash ACROSS
    jobs but leaves same-JOB rows sharing a hash all is_duplicate=false, so this
    collapses them. Pure (no DB) so the rule is unit-testable. Each row is a dict with
    ``id`` / ``dedup_hash`` / ``auction_date``.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row.get("dedup_hash")].append(row)
    dup_ids: list = []
    for grp in groups.values():
        if len(grp) <= 1:
            continue
        # Keep the most-urgent (soonest auction_date; None sorts last), stable by id.
        ordered = sorted(grp, key=lambda r: (r.get("auction_date") or date.max, str(r.get("id"))))
        dup_ids.extend(r.get("id") for r in ordered[1:])
    return dup_ids


def finalize_trustee_sale_job(db, job_id: str, user_id: Any) -> int:
    """Populate auction columns on every trustee_sale Result of ``job_id``.

    Returns the number of same-parcel siblings NEWLY collapsed to duplicates (so the
    caller can fold it into the job's dup_count for correct user-facing counts). Raises
    ``TrusteeSaleFinalizeError`` if a result is missing its ``nts_source`` contract, or
    if any result still lacks ``auction_date`` / ``nts_notice_id`` after the pass
    (fail-closed). Does NOT commit — the caller's transaction owns the write (committed
    with billing).
    """
    rows = db.execute(
        _sa_text(
            "SELECT id, enrichment_data FROM results "
            "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid)"
        ),
        {"jid": job_id, "uid": str(user_id)},
    ).fetchall()

    populated = 0
    for row in rows:
        enrichment = row.enrichment_data or {}
        if isinstance(enrichment, str):  # JSON column may deserialize as text
            enrichment = json.loads(enrichment)
        src = (enrichment or {}).get("nts_source") or {}
        params = _nts_update_params(row.id, src)
        res = db.execute(
            _sa_text(
                """
                UPDATE results SET
                    auction_date = :auction_date,
                    default_amount = :default_amount,
                    nts_match_confidence = 1.0,
                    nts_notice_id = :notice_id,
                    enrichment_data = COALESCE(enrichment_data, '{}'::json)::jsonb
                                      || jsonb_build_object('nts', CAST(:nts AS jsonb))
                WHERE id = :rid
                """
            ),
            params,
        )
        populated += res.rowcount or 0

    # Collapse same-dedup_hash siblings to ONE billed lead. The scraper gives each
    # notice a distinct insert fingerprint so both SURVIVE insert (never silently drop
    # a real auction), but the shared cross-job dedup scan leaves same-JOB rows sharing
    # a dedup_hash all is_duplicate=false (it only records the hash was claimed once),
    # so without this BOTH would bill. dedup_hash IS the app-wide billing key
    # (parcel|address) — trustee_sale dedups EXACTLY like every other list, no more
    # aggressively (product decision 2026-07-03); cross-job dedup is already handled by
    # the shared delivered_records claim on the same key. Only rows still
    # is_duplicate=false are candidates, so the count is NET-NEW and folds cleanly into
    # the caller's dup_count. Runs before billing.
    sib_rows = db.execute(
        _sa_text(
            "SELECT id, dedup_hash, auction_date FROM results "
            "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
            "AND dedup_hash IS NOT NULL AND is_duplicate = false"
        ),
        {"jid": job_id, "uid": str(user_id)},
    ).fetchall()
    dup_ids = _sibling_duplicate_ids([dict(r._mapping) for r in sib_rows])
    collapsed = len(dup_ids)
    if dup_ids:
        db.execute(
            _sa_text(
                "UPDATE results SET is_duplicate = true "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [str(i) for i in dup_ids]},
        )

    # Fail-closed verification: no trustee_sale result may reach delivery without the
    # two load-bearing fields — auction_date (the urgency signal + freshness gate) and
    # nts_notice_id (the source identity). These are exactly the fields is_valid_nts
    # gates on, so this contract matches the codebase's own definition of a usable NTS
    # notice. default_amount (principal_owing) and trustee are DELIBERATELY excluded:
    # both are nullable in nts_notices and optional throughout the NTS system (the
    # crawler and _write_match accept null principal_owing; the UI/CSV render "—"), so
    # a parser that couldn't extract an amount must not drop a real upcoming auction or
    # fail the whole job. Requiring them here would contradict is_valid_nts and diverge
    # from pre_foreclosure. (Codex flagged the gap; kept optional by is_valid_nts.)
    missing = db.execute(
        _sa_text(
            "SELECT count(*) FROM results "
            "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
            "AND (auction_date IS NULL OR nts_notice_id IS NULL)"
        ),
        {"jid": job_id, "uid": str(user_id)},
    ).scalar() or 0
    if missing:
        raise TrusteeSaleFinalizeError(
            f"{missing} trustee_sale result(s) still missing auction_date/nts_notice_id "
            f"after finalize (job {job_id}) — refusing to deliver blank Auction Leads"
        )

    _logger.info(
        "Job %s: trustee_sale finalize populated %d leads, collapsed %d same-parcel siblings",
        job_id, populated, collapsed,
    )
    return collapsed
