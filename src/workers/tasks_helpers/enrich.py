"""Scraper-run + inline-enrichment + skip-trace helpers, extracted from tasks.py.

Holds the async scraper runner, the duplicate-lead enrichment reuse, the inline
GIS/PACS/King enrichment pass, and the skip-trace enqueue. Moved verbatim —
behavior is byte-identical to the originals in tasks.py.
"""

import asyncio
import re
import time as _time
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

# Seattle SDCI code-violation statuses that are never sent to a paid skip trace.
SETTLED_COMPLAINT_STATUSES = frozenset({"Completed", "Open Duplicate"})

# King tax owner-name state, per lead, in enrichment_data. The owner name is the
# field these leads lose most (eRealProperty is the only source, one page per
# parcel), so a NULL party_name has to say which of two very different things it
# means. `OWNER_DEFERRED_KEY` True: this run did not get an answer, and
# `OWNER_DEFERRED_REASON_KEY` says why (not_admitted, source_unavailable,
# budget_exhausted, lease_lost, breaker_tripped, timeout, error,
# transient_failure, parcel_mismatch). `OWNER_OUTCOME_KEY` == not_on_record: the
# parcel's own county page named it and showed no owner, a settled answer.
OWNER_DEFERRED_KEY = "owner_lookup_deferred"
OWNER_DEFERRED_REASON_KEY = "owner_lookup_deferred_reason"
OWNER_OUTCOME_KEY = "owner_lookup_outcome"
OWNER_NOT_ON_RECORD = "not_on_record"


def enrichment_completion_log(summary: dict) -> tuple[str, str]:
    """(level, message) for the line that closes a job's enrichment.

    One clause per field that is actually incomplete, with its own count. The old
    line always said "Property addresses were added" and counted only mailing,
    so a job that added no property address and named none of its owners still
    read as nearly done. The success wording is unchanged: the results endpoint
    matches its "Enrichment complete" prefix.
    """
    mail = int(summary.get("mailing_deferred") or 0)
    owner = int(summary.get("owner_deferred") or 0)
    if not mail and not owner:
        return "success", "Enrichment complete: addresses added"
    parts = ["Address enrichment partly complete."]
    if mail:
        verb = "lookup is" if mail == 1 else "lookups are"
        parts.append(f"{mail:,} mailing address {verb} still pending.")
    if owner:
        noun = "name" if owner == 1 else "names"
        parts.append(f"{owner:,} owner {noun} could not be looked up during this run.")
    return "info", " ".join(parts)


def _tax_parcel_priority(pid_map: dict[str, list]) -> list[str]:
    """Tax parcels in the order the plan cap will deliver their leads.

    King's per-parcel lookups (owner name, site address) run on a wall-clock budget
    that reaches a few hundred of a job's parcels, BEFORE the cap decides which
    leads ship. Walking parcels in set order spent that budget on leads the
    customer would never receive. This mirrors the cap's tax ranking
    (plan_cap._TAX_RANK_ORDER): largest balance, older delinquency, parcel id.
    Only non-duplicate rows count (a duplicate is never delivered); a parcel with
    no such row goes last.
    """
    def _key(pid: str):
        live = [res for res in pid_map[pid] if not res.is_duplicate]
        amounts = [res.delinquent_amount for res in live if res.delinquent_amount is not None]
        top = max(amounts) if amounts else None
        years = [res.delinquent_bill_year for res in live
                 if res.delinquent_amount == top and res.delinquent_bill_year is not None]
        return (not live, top is None, -(top or 0), min(years) if years else 9999, pid)

    return sorted(pid_map, key=_key)


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


def _fill_king_mailing_from_extract(pid_map: dict[str, list], job_id: str) -> int:
    """Fill missing King mailing addresses from the Assessor extract. Returns rows filled.

    Only rows with no mailing address are touched, and only from an unambiguous answer
    (every account on the parcel agrees). Provenance is stamped so a later reader can
    tell this value from a tax-bill page lookup. The caller commits.
    """
    from src.scrapers.enrichment.king_rpacct import SOURCE, resolve_pins

    # Keyed by the PIN the extract knows: a recorder account number resolved to its
    # PIN is looked up under that PIN, while parcel_id keeps what the recorder printed.
    by_pin: dict[str, list] = {}
    for pid, rows in pid_map.items():
        for res in rows:
            by_pin.setdefault(_king_lookup_pin(res, pid), []).append(res)
    wanted = {pin for pin, rows in by_pin.items()
              if any(not res.mailing_address for res in rows)}
    if not wanted:
        return 0
    resolved = resolve_pins(wanted)
    if resolved is None:
        _logger.info("Job %s: King extract unavailable, mailing uses the tax-bill pages", job_id)
        return 0
    answers, snapshot = resolved
    filled = 0
    for pid, answer in answers.items():
        if answer.status != "found":
            continue
        for res in by_pin.get(pid, []):
            if res.mailing_address:
                continue
            res.mailing_address = answer.mailing_address
            ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
            ed["mailing_source"] = SOURCE
            ed["mailing_rpacct_snapshot"] = snapshot
            res.enrichment_data = ed
            filled += 1
    _logger.info("Job %s: King extract filled %d row(s) across %d requested parcel(s)",
                 job_id, filled, len(wanted))
    return filled


# enrichment_data["resolved_by"] for a recorder account number mapped to its PIN through
# the Assessor extract. Exact, so it is the only resolution King lookups key on.
KING_ACCOUNT_RESOLVER = "rpacct_account_number"
_KING_ACCOUNT_RE = re.compile(r"\d{12}")
_KING_PIN_RE = re.compile(r"\d{10}")


def _king_lookup_pin(res, default: str | None = None) -> str:
    """The PIN to ask King sources about for this row. Never a replacement for parcel_id.

    parcel_id stays what the recorder printed (it feeds the frozen dedup_hash). Only an
    exact account-number resolution changes what is looked up.
    """
    ed = getattr(res, "enrichment_data", None)
    if isinstance(ed, dict) and ed.get("resolved_by") == KING_ACCOUNT_RESOLVER:
        pin = str(ed.get("resolved_parcel_id") or "")
        if _KING_PIN_RE.fullmatch(pin):
            return pin
    return default if default is not None else (getattr(res, "parcel_id", None) or "").strip()


# Guarded writes. The enrichment pass holds ORM objects for a whole job while the
# mailing recovery sweep may update the same rows, so a field is filled only if it is
# still empty IN THE DATABASE, and enrichment_data is merged key by key rather than
# replaced. The ORM object is then synced to what was committed, so later passes in
# this job see the real value and nothing is flushed twice.
# enrichment_data merges only into a JSON object. A JSON null is treated as empty; an
# array or scalar (damaged by an old merge bug) is skipped, never replaced: jsonb || on
# anything but an object builds an array.
ED_MERGEABLE_SQL = "(enrichment_data IS NULL OR jsonb_typeof(enrichment_data::jsonb) IN ('object', 'null'))"
ED_MERGE_SQL = ("((CASE WHEN jsonb_typeof(enrichment_data::jsonb) = 'object' "
                "THEN enrichment_data::jsonb ELSE '{}'::jsonb END) || CAST(:patch AS jsonb))::json")
_PROPERTY_IS_EMPTY = ("coalesce(btrim(property_address), '') IN ('', '(enrichment unavailable)')")

_SQL_RESOLVE_ACCOUNT = f"""
    UPDATE results
       SET enrichment_data = {ED_MERGE_SQL}
     WHERE id = :rid AND user_id = :uid AND {ED_MERGEABLE_SQL}
       AND (enrichment_data->>'resolved_parcel_id') IS NULL
 RETURNING enrichment_data
"""

_SQL_FILL_PROPERTY = f"""
    UPDATE results
       SET property_address = :address,
           property_city = COALESCE(property_city, :city),
           property_state = COALESCE(property_state, :state),
           property_zip = COALESCE(property_zip, :zip),
           enrichment_data = {ED_MERGE_SQL}
     WHERE id = :rid AND user_id = :uid AND {ED_MERGEABLE_SQL} AND {_PROPERTY_IS_EMPTY}
 RETURNING property_address, property_city, property_state, property_zip, enrichment_data
"""

_SQL_MARK_PROPERTY = f"""
    UPDATE results
       SET enrichment_data = {ED_MERGE_SQL}
     WHERE id = :rid AND user_id = :uid AND {ED_MERGEABLE_SQL} AND {_PROPERTY_IS_EMPTY}
 RETURNING enrichment_data
"""


def _guarded_update(db, res, sql: str, params: dict, columns: tuple[str, ...]) -> bool:
    """Run one guarded UPDATE for ``res``; sync the ORM object to what was written."""
    import json

    from sqlalchemy import text as _sa_text
    from sqlalchemy.orm.attributes import set_committed_value

    db.flush()
    # A None value is "nothing to say", never an instruction to blank a stored key.
    patch = {k: v for k, v in params["patch"].items() if v is not None}
    row = db.execute(_sa_text(sql), {
        **params, "rid": str(res.id), "uid": str(res.user_id), "patch": json.dumps(patch),
    }).first()
    if row is None:
        # The guard refused: the database no longer matches what this job loaded.
        # Reload, so no later pass in this job writes its stale copy back over it. A row
        # deleted meanwhile (its job was removed) is dropped from the session instead:
        # raising here would roll back every fill this sweep already made.
        from sqlalchemy.exc import InvalidRequestError

        try:
            db.refresh(res)
        except InvalidRequestError:
            db.expunge(res)
        return False
    for col in columns:
        set_committed_value(res, col, row._mapping[col])
    return True


def _resolve_king_account_parcels(db, rows: list, job_id: str) -> int:
    """Map recorder-printed 12-digit account numbers to their PIN. Returns rows resolved.

    Only rows the King recorder index produced qualify: a 12-digit value from any other
    source is not known to be an account number. The caller commits.
    """
    from src.scrapers.enrichment.king_rpacct import resolve_account_pins

    candidates = [
        res for res in rows
        if _KING_ACCOUNT_RE.fullmatch((res.parcel_id or "").strip())
        and isinstance(res.enrichment_data, dict)
        and res.enrichment_data.get("source") == "king_landmark_json"
        and not res.enrichment_data.get("resolved_parcel_id")
    ]
    if not candidates:
        return 0
    resolved = resolve_account_pins({res.parcel_id.strip() for res in candidates})
    if resolved is None:
        _logger.info("Job %s: King extract unavailable, %d account-number parcel(s) unresolved",
                     job_id, len(candidates))
        return 0
    pins, snapshot = resolved
    done = 0
    for res in candidates:
        pin = pins.get(res.parcel_id.strip())
        if not pin:
            continue
        patch = {"resolved_parcel_id": pin, "source_parcel_id": res.parcel_id.strip(),
                 "resolved_by": KING_ACCOUNT_RESOLVER, "resolved_snapshot": snapshot}
        if _guarded_update(db, res, _SQL_RESOLVE_ACCOUNT, {"patch": patch}, ("enrichment_data",)):
            done += 1
    _logger.info("Job %s: %d of %d King account-number parcel(s) resolved to a PIN",
                 job_id, done, len(candidates))
    return done


# Property recovery markers (read by src/workers/property_recovery.py).
PROPERTY_DEFERRED_KEY = "property_lookup_deferred"
PROPERTY_OUTCOME_KEY = "property_lookup_outcome"
# Outcomes that settle a parcel: no source can give it a property address.
PROPERTY_SETTLED_OUTCOMES = ("no_site_address", "parcel_mismatch")

_SQL_MARK_PROPERTY_DEFERRED = f"""
    UPDATE results
       SET enrichment_data = {ED_MERGE_SQL}
     WHERE id = ANY(CAST(:ids AS uuid[])) AND user_id = CAST(:uid AS uuid)
       AND job_id = CAST(:jid AS uuid) AND {ED_MERGEABLE_SQL} AND {_PROPERTY_IS_EMPTY}
       -- Re-checked in SQL, not only on this job's ORM copy: a sweep may have settled
       -- the row meanwhile, and re-marking it would start a pointless retry.
       AND coalesce(enrichment_data::jsonb->>'property_lookup_outcome', '')
           NOT IN ('found', 'no_site_address', 'parcel_mismatch', 'gave_up')
"""


def _mark_king_property_deferred(db, rows: list, job_id: str, user_id: str) -> int:
    """Mark this job's King leads whose property address is still unknown. Returns rows.

    One set-based, guarded UPDATE: a row filled in the meantime is left alone. The ORM
    copies of enrichment_data are expired so no later flush in this job writes the
    unmarked version back. The caller commits.
    """
    import json

    from sqlalchemy import text as _sa_text

    ids = []
    for res in rows:
        ed = res.enrichment_data if isinstance(res.enrichment_data, dict) else {}
        if (res.property_address and res.property_address != "(enrichment unavailable)"):
            continue
        if (not _KING_PIN_RE.fullmatch(_king_lookup_pin(res)) or ed.get("vacant_no_situs")
                or ed.get(PROPERTY_OUTCOME_KEY) in PROPERTY_SETTLED_OUTCOMES
                or ed.get(PROPERTY_DEFERRED_KEY) is True):
            continue
        ids.append(res)
    if not ids:
        return 0
    db.flush()
    result = db.execute(_sa_text(_SQL_MARK_PROPERTY_DEFERRED), {
        "ids": [str(res.id) for res in ids], "uid": user_id, "jid": job_id,
        "patch": json.dumps({PROPERTY_DEFERRED_KEY: True}),
    })
    for res in ids:
        db.expire(res, ["enrichment_data"])
    _logger.info("Job %s: %d King lead(s) still have no property address; queued for recovery",
                 job_id, result.rowcount or 0)
    return result.rowcount or 0


def _fill_king_condo_unit_situs(db, rows: list, job_id: str) -> int:
    """Fill King condo UNIT property addresses from the Assessor condo extract.

    King GIS has no feature for a unit PIN, so without this a unit's address came only
    from the per-parcel eRealProperty page. Returns rows filled; the caller commits.
    A unit whose address cannot be completed (no site address, ZIP conflict, no
    corroborated city) is left empty with the reason and extract date, so the page
    lookup can still fill it and a later sweep knows why it is blank.
    """
    from src.scrapers.enrichment import county_gis
    from src.scrapers.enrichment.king_condo_units import (
        SOURCE,
        complex_pin,
        compose_fill,
        resolve_units,
    )

    todo: dict[str, list] = {}
    for res in rows:
        if res.property_address and res.property_address != "(enrichment unavailable)":
            continue
        pin = _king_lookup_pin(res)
        if _KING_PIN_RE.fullmatch(pin):
            todo.setdefault(pin, []).append(res)
    if not todo:
        return 0
    resolved = resolve_units(set(todo))
    if resolved is None:
        _logger.info("Job %s: King condo extract unavailable, unit addresses use the pages", job_id)
        return 0
    answers, snapshot = resolved
    units = {pin: situs for pin, situs in answers.items() if situs.status != "absent"}
    complexes = sorted({complex_pin(pin) for pin, s in units.items() if s.status == "found" and s.zip})
    complex_gis: dict = {}
    if complexes:
        try:
            complex_gis = county_gis.batch_enrich_parcels_gis(complexes, "king", "WA")
        except Exception as exc:  # noqa: BLE001 -- no locality means no fill, never a guess
            if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
                raise
            _logger.warning("Job %s: King complex locality lookup failed: %s", job_id, str(exc)[:120])
    filled = 0
    for pin, situs in units.items():
        cpin = complex_pin(pin)
        fill = compose_fill(situs, complex_gis.get(cpin))
        for res in todo[pin]:
            if fill is None:
                status = "no_locality" if situs.status == "found" else situs.status
                patch = {"condo_unit_status": status, "condo_unit_snapshot": snapshot,
                         "condo_unit_nbr": situs.unit_nbr}
                _guarded_update(db, res, _SQL_MARK_PROPERTY, {"patch": patch}, ("enrichment_data",))
                continue
            patch = {"property_source": SOURCE, "property_source_snapshot": snapshot,
                     "property_locality_source": f"king_gis_complex:{cpin}",
                     "condo_unit_nbr": situs.unit_nbr,
                     "condo_unit_status": "found", "condo_unit_snapshot": snapshot}
            if _guarded_update(
                db, res, _SQL_FILL_PROPERTY,
                {"address": fill.property_address, "city": fill.city, "state": fill.state,
                 "zip": fill.zip, "patch": patch},
                ("property_address", "property_city", "property_state", "property_zip",
                 "enrichment_data"),
            ):
                filled += 1
    _logger.info("Job %s: King condo extract filled %d of %d unit row(s)", job_id, filled,
                 sum(len(todo[p]) for p in units))
    return filled


def _run_inline_enrichment(db, job, r, job_id: str, config, summary: dict | None = None) -> None:
    """Run GIS + King County enrichment inline (before job marks done).

    ``summary`` is an optional out-parameter the caller may pass to learn what
    actually happened, so the completion log can tell the truth. It was added
    because a job whose mailing pass looked up 0 of 153 parcels still announced
    "Enrichment complete", which is the one thing the user reading that line
    needs to know is false. An out-param rather than a return value on purpose:
    this function has many early-exit paths, and every one of them should leave
    the caller with "nothing to report" rather than needing its own return.

    Keys (all optional): ``mailing_deferred`` -- parcels still missing a mailing
    address whose lookup did not happen and is now queued for background recovery.
    ``owner_deferred`` -- King tax leads still missing an owner name because this
    run's lookup did not get an answer (see OWNER_DEFERRED_KEY).
    """
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

    is_king = config.county.lower() == "king" and config.state.upper() == "WA"
    if is_king:
        # Before any parcel-keyed lookup, so GIS, the RPAcct mailing prefill and the condo
        # extract all ask about the real PIN. One streamed extract scan, only when a
        # recorder account number is present. Best-effort: unresolved rows are simply
        # looked up as printed, exactly as before.
        try:
            if _resolve_king_account_parcels(db, all_results, job_id):
                db.commit()
        except Exception as exc:  # noqa: BLE001 -- enrichment is best-effort
            if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
                raise
            db.rollback()
            _logger.warning("Job %s: King account-number resolution failed: %s",
                            job_id, str(exc)[:120])

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
        from src.scrapers.enrichment.county_gis import (
            batch_enrich_parcels_gis,
            has_gis_mailing_source,
        )
        # Only a county whose own layer publishes mailing can have its mailing lookup
        # "not happen". Everywhere else there was never a lookup to defer.
        gis_mailing_source = has_gis_mailing_source(config.county, config.state)
        gis_mailing_deferred = 0
        parcel_map: dict[str, list] = {}
        for res in results_need_addr:
            pid = _king_lookup_pin(res) if is_king else res.parcel_id.strip()
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
            gis_stats: dict = {}
            gis_results = batch_enrich_parcels_gis(
                batch_pids, config.county, config.state, stats=gis_stats
            )
            batch_updated = 0
            # Parcels, not rows: one lookup serves every lead on a parcel, and the
            # King summary counts parcels too (Codex P2).
            batch_deferred: set[str] = set()
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
            if gis_mailing_source:
                # The county request for these parcels failed (HTTP error, timeout,
                # ArcGIS error body), so their mailing lookup never happened. Without a
                # marker they read exactly like "the county has no mailing address"
                # and nothing ever asks again. Mark them for the background recovery
                # sweep, the same contract King's deferral uses. Fill-only: a row that
                # already has a mailing address is left alone.
                for pid in gis_stats.get("county_unreached", []):
                    for res in parcel_map.get(pid, []):
                        if res.mailing_address:
                            continue
                        ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
                        if ed.get("mailing_lookup_deferred") is not True:
                            ed["mailing_lookup_deferred"] = True
                            res.enrichment_data = ed
                            batch_deferred.add(pid)
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
                if gis_mailing_source:
                    # The rollback discarded this batch's mailing fills AND any
                    # deferral markers, so none of its rows got the mailing answer
                    # the lookup produced. Leave a marker-only write behind so the
                    # recovery sweep asks again; without it they read as "no address"
                    # forever (Codex P1). Counted only once it is actually stored.
                    batch_deferred = set()
                    try:
                        for pid in batch_pids:
                            for res in parcel_map.get(pid, []):
                                if res.mailing_address:
                                    continue
                                ed = (dict(res.enrichment_data)
                                      if isinstance(res.enrichment_data, dict) else {})
                                if ed.get("mailing_lookup_deferred") is not True:
                                    ed["mailing_lookup_deferred"] = True
                                    res.enrichment_data = ed
                                    batch_deferred.add(pid)
                        db.commit()
                        gis_mailing_deferred += len(batch_deferred)
                    except Exception as mark_exc:
                        db.rollback()
                        _logger.warning(
                            "Job %s: deferral markers after a failed GIS commit were "
                            "not stored either: %s", job_id, str(mark_exc)[:120],
                        )
                continue
            gis_mailing_deferred += len(batch_deferred)
            rows_updated += batch_updated
            _publish_log(
                r, job_id, "info",
                f"Property lookup progress: {min(i + _GIS_COMMIT_BATCH, len(all_pids))}"
                f"/{len(all_pids)} parcels ({rows_updated} rows updated)",
                db=db,
            )
        if gis_mailing_deferred:
            _logger.warning(
                "Job %s: county GIS unreachable for %d row(s); mailing deferred to recovery",
                job_id, gis_mailing_deferred,
            )
            if summary is not None:
                summary["mailing_deferred"] = (
                    int(summary.get("mailing_deferred") or 0) + gis_mailing_deferred
                )
        if commit_failures:
            _logger.warning(
                "Job %s: GIS sweep finished with %d batch commit failure(s) — some "
                "addresses are unfilled (best-effort; re-run to fill)",
                job_id, commit_failures,
            )

    if is_king:
        # Condo UNIT addresses, which King GIS never has. After the GIS sweep (so only its
        # misses are asked) and before the eRealProperty pass, which still runs for owner
        # and mailing and still fills any unit this could not complete. One streamed scan
        # of a ~7 MB weekly file plus one GIS request per 50 complexes.
        try:
            _condo_filled = _fill_king_condo_unit_situs(db, all_results, job_id)
            db.commit()
            if _condo_filled:
                _publish_log(r, job_id, "info",
                             f"Found {_condo_filled} condo unit property addresses in King "
                             "County assessor records.", db=db)
        except Exception as exc:  # noqa: BLE001 -- enrichment is best-effort
            if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
                raise
            db.rollback()
            _logger.warning("Job %s: King condo unit lookup failed: %s", job_id, str(exc)[:120])

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

    # Tacoma code violations name the case, never the owner, so party_name arrives
    # empty. The parcel came from the source record itself; the owner is the Pierce
    # taxpayer of record for it, accepted only under pierce_atip_owner's rules (echoed
    # parcel, real property, not a reference parcel, same situs). Owner decision
    # 2026-09-14 scopes this to code violations; no other Pierce record type is named
    # from ATIP. Bounded here; rows not reached keep no owner_status and the
    # background sweep (src/workers/pierce_cv_owner_recovery.py) retries them.
    if (config.county.lower() == "pierce" and config.state.upper() == "WA"
            and config.record_type == "code_violation" and settings.PIERCE_CV_OWNER_ENABLED):
        from src.scrapers.enrichment.pierce_atip_owner import (
            lookup_parcels,
            owner_lookup_parcels,
            plan_owner_decisions,
            write_owner_decisions,
        )

        _pcv_map = owner_lookup_parcels(all_results)
        if _pcv_map:
            # _publish_log commits, so no transaction is held open across the lookups.
            _publish_log(r, job_id, "info",
                         f"Looking up property owners for {len(_pcv_map)} code violation "
                         "parcels...", db=db)
            _pcv_stats: dict = {}
            _pcv_fetched = lookup_parcels(list(_pcv_map), budget_s=240, stats=_pcv_stats)
            try:
                _pcv_plans, _ = plan_owner_decisions(_pcv_map, _pcv_fetched)
                _pcv_counts = write_owner_decisions(db, _pcv_plans, checked_at=_now().isoformat())
                _publish_log(r, job_id, "info",
                             f"Found {_pcv_counts.get('matched', 0)} property owners for "
                             "code violations.", db=db)
            except Exception as exc:
                db.rollback()
                _logger.warning("Job %s: Pierce code violation owner write failed: %s",
                                job_id, str(exc)[:120])

    # King code violations carry coordinates but no parcel, so the parcel-keyed passes
    # below can never give them a mailing address. Locate the parcel strictly (one
    # polygon, same normalized street and ZIP) and take the mailing from the Assessor
    # extract. The PIN is stored beside the lead, never in parcel_id (dedup/billing).
    if (config.county.lower() == "king" and config.state.upper() == "WA"
            and config.record_type == "code_violation"):
        _cv_rows = {
            str(res.id): res for res in all_results
            if not res.parcel_id and not res.mailing_address
            and isinstance(res.enrichment_data, dict)
            and res.enrichment_data.get("latitude") and res.enrichment_data.get("longitude")
            and not res.enrichment_data.get("kc_pin_status")
        }
        if _cv_rows:
            from src.scrapers.enrichment.king_parcel_locate import (
                SOURCE as _KC_PIN_SOURCE,
            )
            from src.scrapers.enrichment.king_parcel_locate import (
                resolve_code_violation_mailing,
            )

            _publish_log(r, job_id, "info",
                         f"Matching {len(_cv_rows)} code violations to King County parcels...",
                         db=db)
            # Budget covers the WHOLE step: 420 s of parcel lookups (15 s request
            # timeout, so the last call ends by ~435 s) + one extract scan (~15 s) +
            # commit. A code_violation job runs no eRealProperty pass (no parcel_id),
            # so this replaces rather than adds to the King budget in the sum below.
            # Rows not reached keep no status and are picked up by
            # scripts/backfill_king_code_violation_mailing.py.
            try:
                _cv_decisions, _cv_snapshot = resolve_code_violation_mailing(
                    [(k, res.enrichment_data["latitude"], res.enrichment_data["longitude"],
                      res.property_address) for k, res in _cv_rows.items()],
                    budget_s=420,
                )
            except Exception as exc:  # noqa: BLE001 -- enrichment is best-effort
                if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
                    raise
                _logger.warning("Job %s: code violation parcel match failed: %s",
                                job_id, str(exc)[:120])
                _cv_decisions, _cv_snapshot = {}, None
            _cv_mail = 0
            for k, d in _cv_decisions.items():
                res = _cv_rows[k]
                ed = dict(res.enrichment_data)
                ed.update({key: d[key] for key in ("kc_pin_status", "kc_pin", "kc_parcel_address",
                                                   "kc_pin_match")
                           if key in d})
                if d.get("kc_pin"):
                    ed["kc_pin_source"] = _KC_PIN_SOURCE
                if d.get("mailing_address") and not res.mailing_address:
                    res.mailing_address = d["mailing_address"]
                    ed["mailing_source"] = "king_rpacct"
                    ed["mailing_rpacct_snapshot"] = _cv_snapshot
                    _cv_mail += 1
                res.enrichment_data = ed
            try:
                db.commit()
                _publish_log(r, job_id, "info",
                             f"Found {_cv_mail} mailing addresses for code violations.", db=db)
            except Exception as exc:
                db.rollback()
                _logger.warning("Job %s: code violation mailing commit failed: %s",
                                job_id, str(exc)[:120])

        # SDCI names the complaint, never the owner, so party_name arrives empty. An
        # exact or street-level located PIN names the owner through the same owner-only eRealProperty
        # path King tax uses: lease-guarded, paced, breaker-protected, and it drops any
        # page the county served for a different parcel. This job has no parcel_id, so
        # the parcel-keyed owner pass below never runs for it; this takes its 300 s
        # slot in that budget sum. Rows not reached keep no owner and are named later
        # by the beat sweep src/workers/cv_owner_recovery.py.
        from src.scrapers.enrichment.king_parcel_locate import (
            apply_owner_names,
            owner_lookup_pins,
        )
        _cv_owner_map = owner_lookup_pins(all_results)
        if _cv_owner_map:
            from src.scrapers.enrichment.king_county_assessor import (
                KingOwnerLookupBlockedError,
                batch_extract_king_owners,
            )
            from src.scrapers.enrichment.source_health import SourceUnavailableError

            _publish_log(r, job_id, "info",
                         f"Looking up property owners for {len(_cv_owner_map)} code violation "
                         "parcels...", db=db)
            _cv_owners: dict[str, str] = {}
            try:
                asyncio.run(asyncio.wait_for(
                    batch_extract_king_owners(
                        list(_cv_owner_map), delay=1.0, circuit_window=20,
                        max_transient_rate=0.10, max_unresolved_rate=0.50,
                        fetch_attempts=1, out=_cv_owners, time_budget_s=240,
                    ),
                    timeout=300,
                ))
            except TimeoutError:
                _logger.warning("Job %s: code violation owner lookup hit the hard timeout; "
                                "keeping %d names", job_id, len(_cv_owners))
            except (KingOwnerLookupBlockedError, SourceUnavailableError) as exc:
                # Names read before the breaker tripped are still real; keep them.
                _logger.warning("Job %s: code violation owner lookup aborted: %s",
                                job_id, str(exc)[:180])
            _cv_named = apply_owner_names(
                _cv_owner_map, _cv_owners, checked_at=_now().isoformat())
            try:
                db.commit()
                _publish_log(r, job_id, "info",
                             f"Found {_cv_named} property owners for code violations.", db=db)
            except Exception as exc:
                db.rollback()
                _logger.warning("Job %s: code violation owner commit failed: %s",
                                job_id, str(exc)[:120])

    # King County: eRealProperty + Tax Bill for property + mailing
    if config.county.lower() == "king" and config.state.upper() == "WA":
        # A row qualifies if it is missing a mailing address OR (tax-delinquent) is
        # missing its owner name. The owner comes FREE from the same eRealProperty
        # page phase 1 already fetches for the property address, so gating this
        # pass on "missing mailing" alone silently starved owner resolution for any
        # row whose mailing had already been filled upstream — the shape that left
        # a real 384-lead King job with 0 owner names.
        is_tax_delinquent = config.record_type == "tax_delinquent"
        needs = [
            res for res in all_results
            if res.parcel_id and len(res.parcel_id.strip()) >= 6
            and (not res.mailing_address or (is_tax_delinquent and not res.party_name))
        ]
        if needs:
            from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county
            from src.scrapers.enrichment.king_parcel_repair import owner_matches_party
            pids = list({res.parcel_id.strip() for res in needs})
            # Parcels, not rows, and not only mailing: this pass also fetches the
            # owner and the site address.
            _publish_log(r, job_id, "info",
                         f"Looking up county records for {len(pids):,} properties...", db=db)
            pid_map: dict[str, list] = {}
            for res in needs:
                pid = res.parcel_id.strip()
                if pid not in pid_map:
                    pid_map[pid] = []
                pid_map[pid].append(res)
            if is_tax_delinquent:
                # Spend the lookup budget on the leads the plan cap will deliver.
                pids = _tax_parcel_priority(pid_map)
            # ── Mailing from the Assessor bulk extract FIRST ──────────────────────
            # The tax-bill page below costs 5-10 s per parcel and King rate-blocks it:
            # a 16,630-parcel job on 2026-09-13 deferred every lookup and put the
            # source in cooldown. The same taxpayer mailing block is published for
            # every parcel as a weekly file, so any parcel it answers unambiguously is
            # filled here and never reaches the page (pass 2 only visits rows that
            # still lack a mailing address). Pass 1 still runs: it carries the owner
            # name, which the extract deliberately redacts. Fill-only, and a missing
            # or unreadable file falls straight through to the pages as before.
            # Budget: one streamed scan of the file (~15 s) before _king_deadline
            # starts, well inside the soft-limit headroom computed below.
            _rpacct_filled = _fill_king_mailing_from_extract(pid_map, job_id)
            if _rpacct_filled:
                try:
                    db.commit()
                    _publish_log(
                        r, job_id, "info",
                        f"Found {_rpacct_filled} mailing addresses in King County "
                        "assessor records.",
                        db=db,
                    )
                except Exception as exc:
                    db.rollback()
                    _logger.warning("Job %s: extract mailing commit failed: %s",
                                    job_id, str(exc)[:120])
            # NO fixed parcel cap. A count cap truncated the parcel list before the
            # work started, so on a 384-parcel job 84 parcels were dropped without
            # ever being attempted — and the cheap phase-1 lookup (property + OWNER,
            # one HTTP GET) was never the reason jobs ran long. The wall-clock
            # budget below is the real bound: it is checked before every single
            # lookup, returns PARTIAL results rather than losing them, and marks
            # whatever it did not reach as deferred. That keeps a pathological
            # 17k-parcel job inside the Celery soft limit while letting an ordinary
            # job resolve every parcel it has.
            # Internal time budget (200s) returns PARTIAL results; the outer
            # wait_for(240) is only a last-resort kill switch. Either way the
            # rest of enrichment (owner repair, unactionable summary, SKIP-TRACE
            # ENQUEUE) must still run — before 2026-09-02 a TimeoutError here
            # aborted all of it on every large King tax job (172+ parcels).
            king_stats: dict = {}
            king_error: str | None = None
            # party_names lets the malformed-PID resolver break a tie between
            # several REAL candidate parcels by matching the assessor owner to
            # this lead's own party. Only consulted for a confirmed mismatch.
            party_names = {
                pid: list(dict.fromkeys(
                    res.party_name for res in pid_map.get(pid, []) if res.party_name
                ))
                for pid in pids
            }
            found = 0
            # King tax-delinquent rows ship with a placeholder party_name because
            # the Socrata source has no owner column. The eRealProperty lookup
            # above now also yields the owner name; swap it in here. Dual gate:
            # job-level record_type (belt) + the exact placeholder shape
            # (suspenders, so probate/death-cert King rows sharing this enrichment
            # path are never touched).
            from src.scrapers.king_wa_tax_delinquent import is_tax_placeholder_party
            from src.utils.lead_formatting import classify_probate_title_status
            # (is_tax_delinquent computed above, where it also selects `needs`.)
            # Probate + death-cert: party_name is the DECEASED. The Assessor's owner
            # is who holds title NOW — often an heir/trust. Surface it + a conservative
            # flag so the user isn't mailing a decedent.
            is_probate_family = config.record_type in ("probate", "death_certificate")
            def _apply_king(enriched: dict) -> None:
                """Write ONE chunk's lookups onto their rows (caller commits).

                Extracted so the enrichment can run in chunks: the previous
                shape accumulated every parcel's result in memory and wrote
                NOTHING until the whole pass returned, so any interruption threw
                away every lookup already paid for.
                """
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
                        _ed_now = res.enrichment_data if isinstance(res.enrichment_data, dict) else {}
                        if (
                            _ed_now.get("resolved_by") == KING_ACCOUNT_RESOLVER
                            and data.get("resolved_parcel_id") != _ed_now.get("resolved_parcel_id")
                        ):
                            # The Assessor extract already mapped this recorder account
                            # number to its PIN exactly. The page was requested with the
                            # 12-digit value, so only a page PROVEN to be that same PIN may
                            # write onto this lead. One naming a different parcel is a
                            # conflict (recorded, never a correction); one naming none proves
                            # nothing. Either way nothing from it is written, whatever the
                            # lookup status says.
                            if data.get("resolved_parcel_id"):
                                res.enrichment_data = {**_ed_now, "resolved_conflict": {
                                    "resolved_parcel_id": data.get("resolved_parcel_id"),
                                    "resolved_by": data.get("resolved_by"),
                                }}
                            continue
                        if (not prop
                                and (not res.property_address
                                     or res.property_address == "(enrichment unavailable)")
                                and data.get("parcel_lookup") in ("verified", "mismatch")):
                            # The page answered: it names this parcel and has no site
                            # address, or it names a different parcel. Either way asking
                            # King again later cannot produce an address, so the property
                            # recovery sweep must not spend a request on it.
                            res.enrichment_data = {
                                **(res.enrichment_data if isinstance(res.enrichment_data, dict) else {}),
                                PROPERTY_OUTCOME_KEY: ("no_site_address"
                                                       if data["parcel_lookup"] == "verified"
                                                       else "parcel_mismatch"),
                            }
                        if prop and (not res.property_address
                                     or res.property_address == "(enrichment unavailable)"):
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
                        if (data.get("parcel_lookup") in ("recovered", "mismatch")
                                and _ed_now.get("resolved_by") != KING_ACCOUNT_RESOLVER):
                            ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
                            ed["parcel_lookup"] = data["parcel_lookup"]
                            for k, v in data.items():
                                if k.startswith("resolved_") or k == "source_parcel_id":
                                    ed[k] = v
                            res.enrichment_data = ed
            # ── Chunked driver ────────────────────────────────────────────
            # Enrich a slice, WRITE IT, COMMIT IT, then move on. The previous
            # shape asked for every parcel in one call and persisted nothing
            # until it returned, so ANY interruption lost the whole pass. That is
            # not hypothetical: a 17,157-parcel verification run was killed
            # mid-enrichment by an unrelated worker redeploy 6 minutes in, and
            # every lookup it had already paid for was discarded, leaving the job
            # orphaned in 'enriching' with 0 owner names. Deploys restart workers
            # routinely, so a long enrichment MUST checkpoint. Chunking also
            # bounds the in-flight set and lets the shared deadline stop the pass
            # cleanly at a chunk boundary.
            _KING_CHUNK = 200
            # BUDGET ARITHMETIC (additive within ONE Celery task):
            #   scrape 1800s (_SCRAPE_TIMEOUT) + this pass + owner-only 300s.
            # This pass can run to _KING_TOTAL_BUDGET_S plus one chunk's wait_for
            # grace (+60s), so 600 + 60 = 660s worst case:
            #   1800 + 660 + 300 = 2760s, inside soft_time_limit=3600s with ~840s
            # left for persistence, export, billing and delivery. Raise a budget
            # only by re-doing that sum.
            _KING_TOTAL_BUDGET_S = 600
            _king_deadline = _time.monotonic() + _KING_TOTAL_BUDGET_S

            def _king_left() -> float:
                return _king_deadline - _time.monotonic()

            def _run_chunk(_chunk: list, _owned: list | None = None,
                           _meta_out: dict | None = None, **kw) -> dict:
                """One chunked lookup: enrich, write, commit. Returns its stats.

                `_owned` names the parcels this chunk is RESPONSIBLE for. It is
                not always `_chunk`: the mailing pass drives phase 2 by URL and
                passes an empty parcel list, so deriving deferral from `_chunk`
                there would record an empty list and lose the chunk silently.
                """
                nonlocal king_error, found
                _own = list(_owned if _owned is not None else _chunk)
                _cs: dict = {}
                _left = _king_left()
                try:
                    _enriched = asyncio.run(asyncio.wait_for(
                        batch_enrich_king_county(
                            _chunk, time_budget_s=_left, stats=_cs, **kw
                        ),
                        timeout=_left + 60,
                    ))
                except Exception as exc:  # noqa: BLE001 — best-effort county lookup
                    king_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                    _logger.warning(
                        "Job %s: King lookup failed on a chunk: %s", job_id, king_error
                    )
                    _enriched = {}
                    # ASSIGN, never setdefault: batch_enrich_king_county seeds
                    # stats["deferred"] = [] as its FIRST action, so setdefault is a
                    # no-op here and the chunk would silently get no marker.
                    _cs["deferred"] = list(_own)
                _apply_king(_enriched)
                _n_mail = sum(1 for d in _enriched.values() if d.get("mailing_address"))
                try:
                    db.commit()
                    # Count only what actually persisted — a rollback below would
                    # otherwise leave `found` overreporting in the summary log.
                    found += _n_mail
                    if _meta_out is not None:
                        _meta_out.update(_enriched)
                except Exception as exc:
                    _logger.warning(
                        "Job %s: King enrichment commit failed: %s", job_id, str(exc)[:120]
                    )
                    db.rollback()
                    # The rollback discarded this chunk's writes and the chunk is
                    # already off the pending list, so without this it would vanish
                    # with neither data nor a deferred marker (Codex P2).
                    _cs["deferred"] = list(dict.fromkeys(list(_cs.get("deferred", [])) + _own))
                for _k, _v in _cs.items():
                    if isinstance(_v, list):
                        king_stats.setdefault(_k, []).extend(_v)
                    elif isinstance(_v, bool):
                        king_stats[_k] = king_stats.get(_k, False) or _v
                    elif isinstance(_v, int):
                        king_stats[_k] = king_stats.get(_k, 0) + _v
                    elif isinstance(_v, str) and _v:
                        # Strings used to fall through every branch and vanish, so
                        # the per-chunk outcome histogram (`phase1_outcomes`) never
                        # reached the summary and the incident log always read
                        # "n/a" — silently defeating the diagnostic it was added
                        # for. Chunks are joined so a multi-chunk pass shows each
                        # chunk's outcomes rather than only the last.
                        _prev = king_stats.get(_k)
                        king_stats[_k] = f"{_prev} | {_v}" if _prev else _v
                return _cs

            # ── Pass 1: property + OWNER for EVERY parcel (cheap HTTP) ────────
            # Phase ordering is load-bearing and must not be per-chunk: phase 2
            # (Playwright mailing, ~5-10s/parcel) would otherwise consume the whole
            # shared budget inside the FIRST chunk, starving every later parcel of
            # the cheap phase-1 lookup that carries the owner name. That is what a
            # naive chunking did in prod — 173 of 17,157 parcels reached instead of
            # ~1,200 — and it directly undercut the product decision that owner
            # names matter most. Phase 1 runs to completion across all parcels
            # first; mailing then gets whatever is left (Codex P1).
            _tax_urls: dict[str, str] = {}
            _p1_meta: dict[str, dict] = {}
            _pending = list(pids)
            while _pending:
                if _king_left() <= 5:
                    king_stats.setdefault("deferred", []).extend(_pending)
                    _logger.info(
                        "King enrichment: budget spent in phase 1, %d parcels deferred",
                        len(_pending),
                    )
                    _pending = []
                    break
                _chunk, _pending = _pending[:_KING_CHUNK], _pending[_KING_CHUNK:]
                _cs = _run_chunk(
                    _chunk,
                    _meta_out=_p1_meta,
                    party_names={k: party_names[k] for k in _chunk if k in party_names},
                    do_mailing=False,
                    tax_urls_out=_tax_urls,
                )
                # Keep phase-1 rows ONLY for parcels that produced a tax-bill URL —
                # those are the only ones pass 2 will revisit, and phase 2 needs
                # their resolved_parcel_id to validate the rendered page.
                for _k in list(_p1_meta):
                    if _k not in _tax_urls:
                        _p1_meta.pop(_k, None)
                if _cs.get("budget_exhausted"):
                    king_stats.setdefault("deferred", []).extend(_pending)
                    _pending = []

            # ── Pass 2: mailing, with whatever budget phase 1 left over ───────
            _mail_pending = [
                p for p in _tax_urls
                if any(not res.mailing_address for res in pid_map.get(p, []))
            ]
            while _mail_pending:
                if _king_left() <= 5:
                    king_stats.setdefault("deferred", []).extend(_mail_pending)
                    break
                _chunk, _mail_pending = (
                    _mail_pending[:_KING_CHUNK], _mail_pending[_KING_CHUNK:]
                )
                _cs = _run_chunk(
                    [], _owned=_chunk,
                    tax_urls_in={k: _tax_urls[k] for k in _chunk},
                    results_seed={k: _p1_meta[k] for k in _chunk if k in _p1_meta},
                )
                if _cs.get("budget_exhausted"):
                    king_stats.setdefault("deferred", []).extend(_mail_pending)
                    _mail_pending = []

            # Durable marker for parcels the budget/cap/failure never reached, so a
            # later sweep can find them (never a silent gap — Codex).
            deferred = [p for p in dict.fromkeys(king_stats.get("deferred", [])) if p in pid_map]
            # A deferred parcel is one phase 1 or 2 never reached. That is NOT the
            # same as a parcel still waiting for a MAILING address: the bulk extract
            # usually filled mailing already, and the job then told the user 16,859
            # of 16,859 mailing lookups were pending when 16,576 were done. Count
            # only parcels that still have a row with no mailing address.
            mailing_pending = 0
            for pid in deferred:
                missing = [res for res in pid_map.get(pid, []) if not res.mailing_address]
                if missing:
                    mailing_pending += 1
                for res in missing:
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
                # ENGINEERING DETAIL goes to the worker log, never to the user's
                # log stream. The old line published the raw exception straight
                # into the UI, so a paying customer read
                # "SourceUnavailableError: king_erealproperty is throttled until
                # 2026-09-08T03:02:14.040806+00:00 - King phase-1 circuit breaker
                # tripped: 50/50 rec". That names an internal service, an
                # exception class and a breaker threshold, and still does not tell
                # them the one thing that matters: their leads are not lost.
                _logger.warning(
                    "Job %s: King mailing pass incomplete — requested=%d attempted=%d "
                    "found=%d deferred=%d mailing_pending=%d phase1_outcomes=%s error=%s",
                    job_id, len(pids), king_stats.get("mailing_attempted", 0), found,
                    len(deferred), mailing_pending, king_stats.get("phase1_outcomes", "n/a"),
                    king_error or "none",
                )
                if summary is not None:
                    summary["mailing_deferred"] = mailing_pending
                # USER-FACING: what happened, what was kept, what happens next.
                if mailing_pending:
                    _publish_log(
                        r, job_id, "warning",
                        f"Mailing addresses are still being looked up for {mailing_pending:,} "
                        f"of {len(pids):,} properties. County records were slow to respond, "
                        "so those lookups will finish automatically in the background. "
                        "Property addresses already found are saved and your leads are "
                        "not affected.",
                        db=db,
                    )
            else:
                _publish_log(r, job_id, "info", f"Found {found}/{len(pids)} mailing addresses", db=db)

        # Owner-only pass for King tax-delinquent rows that still have no owner
        # name after phase 1: the rows phase 1 never reached (budget, busy source)
        # and rows it did not ask about (e.g. mailing COALESCE-copied onto a
        # duplicate by _reuse_enrichment_for_duplicates). HTTP-only, no Playwright,
        # under the SAME dual gate as the swap above: record_type == tax_delinquent
        # (belt) + blank or exact placeholder party (suspenders).
        #
        # It used to require a mailing address as well. That excluded exactly the
        # leads the bulk extract could not mail (216 of the 840 delivered on job
        # b2f2ecd5), so nothing ever asked who owns them. Phase 1 already looks up
        # owners for every row regardless of mailing; this pass must too.
        if config.record_type == "tax_delinquent":
            from src.scrapers.king_wa_tax_delinquent import is_tax_placeholder_party

            def _ed(res) -> dict:
                return dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}

            owner_needs = [
                res for res in all_results
                if res.parcel_id and len(res.parcel_id.strip()) >= 6
                # The same shape batch_extract_king_owners will actually request. A
                # digit-free id is dropped there unasked, and marking it "deferred"
                # would promise a lookup that can never happen.
                and any(c.isdigit() for c in res.parcel_id)
                and (not res.party_name or is_tax_placeholder_party(res.party_name))
                # Settled: the parcel's own county page named it and showed no owner.
                # Asking again spends a request on the same answer.
                and _ed(res).get(OWNER_OUTCOME_KEY) != OWNER_NOT_ON_RECORD
            ]
            if owner_needs:
                _publish_log(
                    r, job_id, "info",
                    f"Resolving owner names for {len(owner_needs):,} tax-delinquent leads...",
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
                # No count cap (product decision 2026-09-03): a lead without an
                # owner name is barely a lead, and a fixed 25 meant at most 6.5% of
                # a 384-row job could ever be named. Volume is bounded instead by
                # the wall-clock timeout below plus the SAME protections that
                # matter operationally — paced requests, the source-health gate,
                # and the circuit breaker that trips on a throttle/block rather
                # than recording it as "no owner". Those are safety valves, not
                # caps, and are deliberately kept: they are what stops a repeat of
                # the eRealProperty IP rate-block.
                # Largest balance first, the order the plan cap delivers in, so a
                # budget that runs out leaves the undelivered leads unnamed.
                o_pids = _tax_parcel_priority(o_pid_map)
                # Caller-owned result dicts: names AND the per-parcel outcome ledger
                # are kept even if the outer wait_for cancels or the breaker raises.
                owners: dict[str, str] = {}
                o_stats: dict = {}
                o_reason: str | None = None
                try:
                    asyncio.run(asyncio.wait_for(
                        batch_extract_king_owners(
                            o_pids,
                            delay=1.0,
                            circuit_window=20,
                            max_transient_rate=0.10,
                            max_unresolved_rate=0.50,
                            fetch_attempts=1,
                            out=owners,
                            stats=o_stats,
                            # Stop cooperatively just inside the hard timeout so the
                            # loop exits on its own terms rather than being killed.
                            time_budget_s=240,
                        ),
                        timeout=300,
                    ))
                except TimeoutError:
                    o_reason = "timeout"
                    _logger.warning(
                        "Job %s: King owner-only lookup hit the hard timeout; "
                        "keeping %d owner names already resolved",
                        job_id, len(owners),
                    )
                except (KingOwnerLookupBlockedError, SourceUnavailableError) as exc:
                    # Keep what was already resolved. The breaker guards against
                    # reading a throttle as "this parcel has no owner"; it does not
                    # make the names fetched BEFORE it tripped any less real.
                    _logger.warning(
                        "Job %s: King owner-only lookup aborted: %s",
                        job_id, str(exc)[:180],
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort county lookup, as phase 1
                    o_reason = "error"
                    _logger.warning(
                        "Job %s: King owner-only lookup failed: %s: %s",
                        job_id, type(exc).__name__, str(exc)[:160],
                    )
                o_reason = o_reason or o_stats.get("outcome") or "error"
                _transient = set(o_stats.get("transient", []))
                _mismatch = set(o_stats.get("parcel_mismatch", []))
                _no_owner = set(o_stats.get("no_owner_on_record", []))

                # One outcome per parcel, fanned out to every lead on it. A lead is
                # named, settled as having no owner on the county record, or marked
                # for another attempt with the reason this one did not happen. A
                # retry marker never claims the county said anything.
                swapped = 0
                owner_deferred_rows = 0
                not_on_record_parcels = 0
                for pid, rows in o_pid_map.items():
                    owner = owners.get(pid)
                    if not owner and pid in _no_owner:
                        not_on_record_parcels += 1
                    for res in rows:
                        if res.party_name and not is_tax_placeholder_party(res.party_name):
                            # Named since owner_needs was built. Never clobber a
                            # real owner, and a named lead needs no owner marker.
                            continue
                        ed = _ed(res)
                        if owner:
                            res.party_name = owner
                            swapped += 1
                            if OWNER_DEFERRED_KEY in ed:
                                ed[OWNER_DEFERRED_KEY] = False
                            ed.pop(OWNER_DEFERRED_REASON_KEY, None)
                        elif pid in _no_owner:
                            ed[OWNER_OUTCOME_KEY] = OWNER_NOT_ON_RECORD
                            ed[OWNER_DEFERRED_KEY] = False
                            ed.pop(OWNER_DEFERRED_REASON_KEY, None)
                        else:
                            ed[OWNER_DEFERRED_KEY] = True
                            ed[OWNER_DEFERRED_REASON_KEY] = (
                                "transient_failure" if pid in _transient
                                else "parcel_mismatch" if pid in _mismatch
                                else o_reason
                            )
                            owner_deferred_rows += 1
                        res.enrichment_data = ed
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
                if summary is not None:
                    summary["owner_deferred"] = (
                        owner_deferred_rows if committed else len(owner_needs)
                    )
                # USER-FACING, one line per fact, and only facts. "Resolved 0 owner
                # names from 0/16576 parcels" read as a finished lookup that found
                # nothing, when the county had never been asked.
                if committed:
                    lines: list[tuple[str, str]] = []
                    if owners or o_stats.get("attempted"):
                        lines.append((
                            "info",
                            f"Resolved {swapped} owner names from "
                            f"{len(owners)}/{len(o_pids)} parcels",
                        ))
                    if not_on_record_parcels:
                        lines.append((
                            "info",
                            f"{not_on_record_parcels:,} "
                            f"{'parcel has' if not_on_record_parcels == 1 else 'parcels have'} "
                            "no owner name on the county record.",
                        ))
                    if owner_deferred_rows:
                        # No promise of an automatic retry: nothing re-runs owner
                        # lookups yet, and a promise with nothing behind it is the
                        # defect this line replaces.
                        lines.append((
                            "warning",
                            f"Owner names could not be looked up for {owner_deferred_rows:,} "
                            f"{'lead' if owner_deferred_rows == 1 else 'leads'} during this run. "
                            "Names already found are saved.",
                        ))
                else:
                    lines = [(
                        "warning",
                        "Owner-name resolution failed to persist (will retry next run)",
                    )]
                # Guard the post-commit logs: _publish_log(db=db) commits, and a
                # failure HERE must not crash after the swaps already persisted nor
                # skip the skip-trace enqueue that follows this block.
                for level, msg in lines:
                    try:
                        _publish_log(r, job_id, level, msg, db=db)
                    except Exception as exc:
                        _logger.warning("Job %s: owner-only progress log failed: %s", job_id, str(exc)[:120])
                        db.rollback()  # log write failed; swaps already settled — keep going

    if is_king:
        # Every King pass is done. A lead that still has no property address, whose
        # parcel no source has settled (not vacant land, no page answer), is marked for
        # the property recovery sweep: the per-parcel page may simply not have run
        # (lease busy, breaker, budget, or the lead never needed that pass). Only this
        # job's rows, so nothing historical is swept without a deliberate repair.
        try:
            _deferred = _mark_king_property_deferred(db, all_results, job_id, str(job.user_id))
            db.commit()
            if _deferred and summary is not None:
                summary["property_deferred"] = _deferred
        except Exception as exc:  # noqa: BLE001 -- enrichment is best-effort
            if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
                raise
            db.rollback()
            _logger.warning("Job %s: property deferral markers not stored: %s", job_id, str(exc)[:120])

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

    # Skip trace is deliberately NOT enqueued here. Which rows are delivered is
    # still undecided at this point: the same-run survivor re-election, the claim
    # transfer and the plan cap all run after enrichment and can each change it.
    # A lookup bought now could land on a row that ends up suppressed, while the
    # row actually delivered is never traced. tasks.py calls
    # _enqueue_skip_trace_rows once those have settled.


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

    Called by run_scrape_job AFTER enrichment AND the plan cap, so the
    actionable_condition() below already excludes rows the cap marked
    over_quota: a lead that will not be delivered is never traced. Only
    records with a property_address are eligible.

    Runs only if SKIP_TRACE_ENABLED, TRACERFY_API_TOKEN is set, the config has
    skip_trace_enabled and the plan is not Starter. Cache hits are copied onto
    the row for free; misses are queued for the dispatcher, which makes the
    actual (paid) Tracerfy calls.
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
    from src.config.constants import normalize_plan

    if normalize_plan(job.user.plan) == "starter":
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

    # A code-violation complaint the city already closed as Completed, or filed as a
    # duplicate of another complaint, is not a lead worth a paid lookup (owner decision
    # 2026-09-13). Exact SDCI status values; "Closed" is deliberately still traced.
    if config.record_type == "code_violation":
        settled_rows = [
            rec for rec in eligible
            if isinstance(rec.enrichment_data, dict)
            and isinstance(rec.enrichment_data.get("status"), str)
            and rec.enrichment_data["status"] in SETTLED_COMPLAINT_STATUSES
        ]
        if settled_rows:
            _settled_ids = {rec.id for rec in settled_rows}
            eligible = [rec for rec in eligible if rec.id not in _settled_ids]
            _publish_log(
                r, job_id, "info",
                f"Skip trace skipped for {len(settled_rows)} code violation lead(s) whose "
                "complaint is already completed or is a duplicate",
                db=db,
            )

    if not eligible:
        return

    cache_hits = 0
    cache_misses = 0
    enqueued_normal = 0
    enqueued_advanced = 0

    skipped_ineligible = 0
    for rec in eligible:
        # Parse the combined address to get canonical city/state for the cache key
        payload = build_pending_row_payload(rec)
        if payload is None:
            # Declined: no traceable party name, or no resolvable city/state
            # (Tracerfy requires address+city+state and silently DROPS a row
            # missing one, which used to strand the lead on "Processing"
            # forever). Counted and reported below rather than vanishing —
            # the lead stays 'not_attempted', so a later situs backfill
            # re-qualifies it.
            skipped_ineligible += 1
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

    if skipped_ineligible:
        _publish_log(
            r, job_id, "info",
            f"Skip trace skipped for {skipped_ineligible} lead(s): no traceable "
            "owner name, or the property's city/state could not be determined "
            "(the provider requires both). These leads stay eligible and will be "
            "traced automatically once their address details are filled in.",
            db=db,
        )

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
