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
    from collections.abc import Callable

    from src.scrapers.base_scraper import ProgressCallback

_logger = setup_logger("worker.task")

# How many parcels the GIS sweep enriches+commits per transaction. Small enough
# that a hard-kill loses at most one batch of work; large enough to keep the
# commit overhead negligible against the per-chunk (50-parcel) HTTP cost.
_GIS_COMMIT_BATCH = 500

# Anchored trailing ZIP only ("… PL 4C 98023" / "… 98023-1234"): never a 5-digit
# token inside the street (house numbers, road numbers).
_TRAILING_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")

def _is_settled_complaint(ed: object) -> bool:
    """A code-violation case its source settled: never sent to a paid skip trace.

    The one list is src/scrapers/king_cv_sources.SETTLED_STATUSES, the list the plan cap
    ranks by, so a case the cap delivers as ordinary is traced like one. A source with no
    list (Tacoma: "Open" / "Closed") has no settled cases.
    """
    from src.scrapers.king_cv_sources import is_settled

    return isinstance(ed, dict) and is_settled(ed.get("source"), ed.get("status"))

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
    # A count of rows that still have NO mailing address after the sweep, whether or
    # not this run deferred them. Without it the line reported plain success for a
    # job that obtained 0 mailing addresses: deferral was the ONLY thing it measured,
    # so a county answering "nothing" without erroring (Snohomish, 2026-09-18) read
    # as fully enriched. Reports what is missing, not what this pass happened to mark.
    missing_mail = int(summary.get("mailing_missing") or 0)
    # Rows with no property AND no mailing address (not leads), and owner-name
    # lookups the county site never answered. Either one makes "addresses added" false.
    no_address = int(summary.get("no_address") or 0)
    lookup_failed = int(summary.get("name_lookup_failed") or 0)
    if not mail and not owner and not missing_mail and not no_address and not lookup_failed:
        return "success", "Enrichment complete: addresses added"
    parts = ["Address enrichment partly complete."]
    if no_address:
        noun = "record has" if no_address == 1 else "records have"
        parts.append(f"{no_address:,} {noun} no property or mailing address.")
    if lookup_failed:
        noun = "lookup" if lookup_failed == 1 else "lookups"
        parts.append(f"{lookup_failed:,} address {noun} failed.")
    if missing_mail and not mail:
        noun = "lead has" if missing_mail == 1 else "leads have"
        parts.append(f"{missing_mail:,} {noun} no mailing address available.")
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
    on_stage: "Callable[[str], None] | None" = None,
):
    """Run the async scraper and stream progress logs back to Redis."""
    # Pass record_type / doc_types ONLY to scrapers whose constructor accepts
    # them (template/partial scrapers may not). doc_types=None means legacy
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
    # Construct, wire the callbacks, THEN enter. The callbacks used to be attached
    # inside the `async with`, i.e. after __aenter__ had already launched the browser
    # — so anything a scraper reported during startup went nowhere. Startup is the
    # slowest and least visible part of a county run, which makes it the part most
    # worth hearing about.
    scraper = scraper_class(**kwargs)
    if on_progress:
        scraper.on_progress = on_progress
    if on_stage:
        scraper.on_stage = on_stage
    async with scraper:
        records = await scraper.scrape(date_from, date_to)

        # A connector that merges several sources ships what succeeded when one source
        # fails, and says so here (e.g. King code violations). Customer-facing copy.
        for warning in getattr(scraper, "scrape_warnings", None) or ():
            _publish_log(r, job_id, "warning", warning)

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
    import uuid as _uuid

    from sqlalchemy import select as sa_select
    from sqlalchemy import text as _sa_text

    from src.db.models import Result
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

    # WHOSE contacts may be copied (migration 098). dedup_hash is
    # sha256(parcel|address) and carries NO owner name, so a match means "the same
    # property", never "the same owner". Run 1 traces the owner, probate moves the
    # property, run 3 re-scrapes it under the heir, the hashes agree, and the
    # heir's lead inherits the dead owner's phone.
    #
    # The two sides are deliberately asymmetric, and that is the whole fix:
    #   - the TARGET's subject is COMPUTED from its current state, because that is
    #     what we would buy for it right now (the same payload the enqueue builds);
    #   - the SOURCE's subject is READ from the durable column, never recomputed.
    # Recomputing the source is what does not work: party_name is rewritten by
    # owner recovery after the lookup, so a recomputed source subject reads as the
    # CURRENT owner while the phone stored beside it still belongs to the previous
    # one. The comparison would pass and copy exactly the leak it is here to stop.
    #
    # A target whose payload cannot be built (no address, not traceable) gets a
    # NULL subject and therefore no PII, while still receiving the address and
    # enrichment fill below. Pre-098 sources have a NULL hash and donate nothing.
    # Both directions fail CLOSED.
    from src.scrapers.enrichment.skip_trace import (
        build_pending_row_payload,
        payload_subject_key,
    )

    subject_by_id: dict[str, str | None] = {}
    for _row in db.execute(
        sa_select(Result).where(
            Result.id.in_([_uuid.UUID(i) for i in strong_ids]),
            Result.user_id == _uuid.UUID(uid),  # tenant-pinned: system session
        )
    ).scalars():
        _payload = build_pending_row_payload(_row)
        subject_by_id[str(_row.id)] = (
            payload_subject_key(uid, _payload) if _payload else None
        )
    # One entry per strong id, so the unnest join below never drops a target that
    # simply has no computable subject.
    subj_ids = list(strong_ids)
    subj_hashes = [subject_by_id.get(i) for i in subj_ids]

    ttl = int(getattr(settings, "SKIP_TRACE_CACHE_DAYS", 90))
    # The settled-reuse predicate, written ONCE. It used to be copy-pasted into
    # all nine skip-trace columns below; adding the 098 subject gate to nine
    # near-identical 400-character lines is how one of them silently keeps
    # copying. This is a fixed module-local constant with no user data in it, so
    # interpolating it changes nothing about the injection surface: every VALUE is
    # still a bound parameter (:ids, :uid, :ttl, :subj_ids, :subj_hashes,
    # :atip_blocked, :atip_source), and membership is still restricted to the
    # Python-verified strong_ids.
    #
    # s.subject_hash is the TARGET's subject and ro.skip_trace_subject_hash is what
    # the SOURCE's answer was actually bought for. Either being NULL makes the
    # comparison NULL, the CASE takes its ELSE, and nothing is copied: a pre-098
    # source and an untraceable target both fail closed.
    _reuse_ok = (
        "ro.skip_trace_status IN ('hit','miss') "
        "AND ro.skip_trace_attempted_at IS NOT NULL "
        "AND ro.skip_trace_attempted_at >= NOW() - make_interval(days => :ttl) "
        "AND rn.skip_trace_status = 'not_attempted' "
        "AND ro.skip_trace_subject_hash IS NOT NULL "
        "AND ro.skip_trace_subject_hash = s.subject_hash "
        "AND NOT (CAST(:atip_blocked AS boolean) "
        "AND COALESCE(rn.enrichment_data->>'source', '') = 'tacoma_code_violations' "
        "AND COALESCE(rn.enrichment_data->>'owner_source', '') = :atip_source)"
    )

    def _copy(col: str, value: str | None = None) -> str:
        """`col = CASE WHEN <reuse ok> THEN <value> ELSE <current> END,`"""
        return (
            f"{col} = CASE WHEN {_reuse_ok} THEN {value or f'ro.{col}'} "
            f"ELSE rn.{col} END"
        )

    # Address and enrichment fields are NOT gated by the subject: they describe the
    # property, not its owner, and fill-missing (COALESCE current-first) never
    # clobbers a fresh scrape or GIS value.
    sql = f"""
        UPDATE results AS rn SET
            property_address     = COALESCE(rn.property_address, ro.property_address),
            mailing_address      = COALESCE(rn.mailing_address, ro.mailing_address),
            delinquent_amount    = COALESCE(rn.delinquent_amount, ro.delinquent_amount),
            delinquent_bill_year = COALESCE(rn.delinquent_bill_year, ro.delinquent_bill_year),
            {_copy("phone")},
            {_copy("phone_type")},
            {_copy("phone_dnc_flag")},
            {_copy("email")},
            {_copy("skip_trace_status")},
            {_copy("skip_trace_source", "'reused'")},
            {_copy("skip_trace_attempted_at")},
            {_copy("skip_trace_subject_hash", "s.subject_hash")},
            {_copy("phones")},
            {_copy("emails")}
        FROM delivered_records dr
        JOIN results ro
          ON ro.id = dr.first_result_id
         AND ro.user_id = CAST(:uid AS uuid)
        CROSS JOIN unnest(CAST(:subj_ids AS uuid[]), CAST(:subj_hashes AS text[]))
              AS s(id, subject_hash)
        WHERE rn.id = ANY(CAST(:ids AS uuid[]))
          AND rn.user_id = CAST(:uid AS uuid)
          AND dr.user_id = CAST(:uid AS uuid)
          AND dr.dedup_hash = rn.dedup_hash
          AND rn.id <> dr.first_result_id
          AND s.id = rn.id
        RETURNING rn.id
    """
    # An ATIP-named Tacoma lead never receives contact data while the paid switch is off:
    # the duplicate-reuse copy spends no new credit but would still attach phones and
    # emails to a name legal cleared for NAMING only (Codex). Addresses still copy.
    # COALESCE: a row with no `source` key made this clause NULL, and a NULL CASE
    # condition takes the ELSE, so such a duplicate silently never received reuse.
    from src.scrapers.enrichment.pierce_atip_owner import OWNER_SOURCE as _PIERCE_OWNER_SOURCE

    params = {
        "ids": strong_ids, "uid": uid, "ttl": ttl,
        "subj_ids": subj_ids, "subj_hashes": subj_hashes,
        "atip_blocked": not settings.PIERCE_CV_OWNER_SKIP_TRACE_ENABLED,
        "atip_source": _PIERCE_OWNER_SOURCE,
    }
    touched = {str(i) for i in db.execute(_sa_text(sql), params).scalars()}

    # The first delivery is not the only place a trace can live. Run 1 delivered the
    # lead with skip trace off, run 2 traced ITS already-delivered row, and run 3 must
    # reuse run 2's answer: reading first_result_id alone would buy it again. So an
    # already-delivered row still untouched takes the NEWEST settled answer of any
    # other run of this account for the same strong key, inside the same TTL. Targets
    # are already-delivered rows only (never a same-run sibling); the source excludes
    # this job; both legs are pinned to this account. Static SQL, bound params only.
    from src.api.results_category import already_delivered_sql

    later_sql = f"""
        UPDATE results AS rn SET
            phone = src.phone, phone_type = src.phone_type,
            phone_dnc_flag = src.phone_dnc_flag, email = src.email,
            phones = src.phones, emails = src.emails,
            skip_trace_status = src.skip_trace_status,
            skip_trace_attempted_at = src.skip_trace_attempted_at,
            skip_trace_source = 'reused',
            skip_trace_subject_hash = src.skip_trace_subject_hash
        FROM (
            -- Newest per (dedup_hash, SUBJECT), not per dedup_hash (098). One
            -- property can now hold several owners' answers, and picking the
            -- newest by property alone would hand whichever owner was traced last
            -- to every lead at that address. Still one bounded query: the source
            -- set is restricted to the target hash set, so this is not a scan.
            SELECT DISTINCT ON (ro.dedup_hash, ro.skip_trace_subject_hash)
                   ro.dedup_hash, ro.skip_trace_subject_hash,
                   ro.phone, ro.phone_type, ro.phone_dnc_flag, ro.email,
                   ro.phones, ro.emails, ro.skip_trace_status, ro.skip_trace_attempted_at
              FROM results ro
             WHERE ro.user_id = CAST(:uid AS uuid)
               AND ro.job_id <> CAST(:jid AS uuid)
               AND ro.dedup_hash IN (SELECT dedup_hash FROM results
                                      WHERE id = ANY(CAST(:ids AS uuid[]))
                                        AND user_id = CAST(:uid AS uuid))
               AND ro.skip_trace_status IN ('hit', 'miss')
               -- A pre-098 answer cannot say whose it is, so it donates nothing.
               AND ro.skip_trace_subject_hash IS NOT NULL
               AND ro.skip_trace_attempted_at >= NOW() - make_interval(days => :ttl)
               AND ro.skip_trace_attempted_at <= NOW() + interval '5 minutes'
             ORDER BY ro.dedup_hash, ro.skip_trace_subject_hash,
                      ro.skip_trace_attempted_at DESC, ro.id
        ) AS src
        CROSS JOIN unnest(CAST(:subj_ids AS uuid[]), CAST(:subj_hashes AS text[]))
              AS s(id, subject_hash)
        WHERE rn.id = ANY(CAST(:ids AS uuid[]))
          AND rn.user_id = CAST(:uid AS uuid)
          AND rn.dedup_hash = src.dedup_hash
          AND s.id = rn.id
          -- The target's own subject must be the one that answer was bought for.
          AND src.skip_trace_subject_hash = s.subject_hash
          AND rn.skip_trace_status = 'not_attempted'
          AND {already_delivered_sql("rn")}
          AND NOT (CAST(:atip_blocked AS boolean)
                   AND COALESCE(rn.enrichment_data->>'source', '') = 'tacoma_code_violations'
                   AND COALESCE(rn.enrichment_data->>'owner_source', '') = :atip_source)
        RETURNING rn.id
    """
    touched |= {str(i) for i in db.execute(
        _sa_text(later_sql), {**params, "jid": job_id}).scalars()}
    db.commit()
    return len(touched)


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


_SQL_BULK_MAILING = f"""
    UPDATE results
       SET mailing_address = :mail, enrichment_data = {ED_MERGE_SQL}
     WHERE id = :rid AND user_id = :uid AND {ED_MERGEABLE_SQL}
       AND mailing_address IS NULL
 RETURNING mailing_address, enrichment_data
"""


def _apply_bulk_mailing(db, fills: list[tuple], job_id: str) -> tuple[int, list]:
    """Write bulk-export mailing addresses fill-only, guarded in the DATABASE.

    A bulk answer comes from a MONTHLY snapshot, so it must never replace a fresher
    value. Testing the ORM object is not enough: it was loaded before the lookup,
    and the recovery sweep can fill the same row while that network call is in
    flight, leaving this object holding NULL and overwriting the newer address
    (Codex High). ``mailing_address IS NULL`` in the WHERE clause is the real guard.

    Returns (written, failed). A row whose write RAISED is returned in ``failed``:
    the resolver already removed its parcel from county_unreached, so without a
    deferral marker the batch would commit a NULL mailing that nothing ever revisits
    — the silent permanent gap this whole change exists to remove (Codex High). A
    row the guard merely REFUSED is not a failure: the database already holds a
    mailing address, which is the fill-only rule working.
    """
    written = 0
    failed: list = []
    # Settle everything this sweep already dirtied ONCE, at batch scope, before any
    # savepoint. _guarded_update flushes as its first statement, so leaving pending
    # ORM state would push that flush INSIDE a per-row savepoint: one bad row would
    # roll back the property addresses every other row just earned, and a flush
    # failure there escapes before the caller can mark anything deferred (Codex).
    try:
        db.flush()
    except Exception as exc:  # noqa: BLE001
        # The transaction cannot be committed now. Recover the session and hand back
        # every row so the caller still marks them retryable; the property fills in
        # this batch are lost with the rollback, but those rows stay in
        # results_need_addr and a re-run refills them.
        db.rollback()
        _logger.warning(
            "Job %s: pre-write flush failed, %d bulk fill(s) deferred: %s",
            job_id, len(fills), str(exc)[:120],
        )
        return 0, [res for res, _ in fills]
    for res, gis_data in fills:
        patch = {
            "mailing_source": gis_data.get("mailing_source"),
            "mailing_source_role": gis_data.get("mailing_role"),
            "mailing_source_revision": gis_data.get("mailing_revision"),
        }
        try:
            # SAVEPOINT per row: a failed statement poisons the enclosing
            # transaction until rolled back, so swallowing one without a savepoint
            # would take down the whole batch commit, including the property
            # addresses this sweep just filled.
            with db.begin_nested():
                ok = _guarded_update(
                    db, res, _SQL_BULK_MAILING,
                    {"patch": patch, "mail": gis_data.get("mailing_address")},
                    ("mailing_address", "enrichment_data"),
                )
            if ok:
                written += 1
            else:
                # The guard refused. USUALLY that means a mailing address already
                # landed, which is the fill-only rule working. But it also refuses a
                # row whose enrichment_data is not a JSON object, and that row can
                # still be NULL — unresolved, not settled, so it stays retryable
                # (Codex). _guarded_update has already reloaded or expunged it.
                try:
                    if getattr(res, "mailing_address", None) is None:
                        failed.append(res)
                except Exception:  # noqa: BLE001, S110 -- expunged row: nothing to mark
                    pass
        except Exception as exc:  # noqa: BLE001 -- enrichment is best-effort
            failed.append(res)
            _logger.warning(
                "Job %s: bulk mailing write failed for row %s: %s",
                job_id, str(res.id)[:8], str(exc)[:120],
            )
    return written, failed


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
            has_mailing_source,
        )
        # Only a county with a mailing source (its GIS layer, or a bulk/page source
        # such as Clark's) can have its mailing lookup "not happen". Everywhere else
        # there was never a lookup to defer.
        gis_mailing_source = has_mailing_source(config.county, config.state)
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
            # (row, gis_data) pairs whose mailing came from a BULK county export.
            # Written below through a guarded UPDATE instead of the ORM.
            _bulk_fills: list[tuple] = []
            for pid, gis_data in gis_results.items():
                prop = gis_data.get("property_address")
                mail = gis_data.get("mailing_address")
                # A bulk county export is a MONTHLY snapshot. A row can reach this
                # sweep because its PROPERTY address was missing while already
                # holding a good, fresher mailing address, and the live-layer
                # branches below overwrite mailing whenever they have one. Letting a
                # snapshot replace a better value that way is a silent downgrade, so
                # a bulk answer is fill-only (Codex).
                bulk_source = gis_data.get("mailing_source")
                for res in parcel_map.get(pid, []):
                    # A bulk answer is fill-only, and the check has to happen in the
                    # DATABASE, not against ORM state loaded before the lookup: the
                    # network round trip is long enough for the recovery sweep to
                    # fill the same row, and this object would still hold NULL and
                    # overwrite the newer address (Codex High). The guarded UPDATE
                    # below mirrors mailing_recovery's writer; `row_mail` keeps the
                    # ORM branches from writing it a second time.
                    row_mail = None if bulk_source else mail
                    if mail and bulk_source:
                        _bulk_fills.append((res, gis_data))
                    # Migration 085 (#188) — capture the REAL situs parts BEFORE the
                    # assessor's street-only line replaces the scraper's fuller one.
                    # Runs for every branch below, including vacant land, so a parcel
                    # with no street still records WHERE it is.
                    _keep_situs_parts(res, gis_data)
                    # The source answered and has no mailing address for this parcel
                    # (clark_pic: none / parcel_not_found / parcel_mismatch). Recorded
                    # under recovery's durable outcome key, so the row reads "looked
                    # up, nothing there" rather than "never looked up", and the
                    # historical requeue does not queue it again.
                    if (gis_data.get("mailing_lookup") in ("none", "parcel_not_found",
                                                           "parcel_mismatch")
                            and not res.mailing_address):
                        _ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
                        _ed["mailing_recovery_outcome"] = gis_data["mailing_lookup"]
                        _ed["mailing_source"] = gis_data.get("mailing_source")
                        res.enrichment_data = _ed
                    if prop:
                        res.property_address = prop
                        # Only a REAL mailing overwrites (King never echoes the
                        # property into mailing — Codex): never clobber an existing
                        # value with None.
                        if row_mail:
                            res.mailing_address = row_mail
                        batch_updated += 1
                    elif row_mail:
                        # No street, but a real mailing (e.g. a Pierce parcel with a
                        # Delivery_Address but null Site_Address) — keep it rather
                        # than drop it into the vacant branch (Codex P2).
                        res.mailing_address = row_mail
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
            if _bulk_fills:
                _n, _failed = _apply_bulk_mailing(db, _bulk_fills, job_id)
                batch_updated += _n
                for _res in _failed:
                    # Its write did not land, so it must stay retryable.
                    _ed = dict(_res.enrichment_data) if isinstance(_res.enrichment_data, dict) else {}
                    if _ed.get("mailing_lookup_deferred") is not True:
                        _ed["mailing_lookup_deferred"] = True
                        _res.enrichment_data = _ed
                        # Count it, or the completion line reports fewer pending
                        # recoveries than there are (Codex).
                        if _res.parcel_id:
                            batch_deferred.add(_res.parcel_id.strip())
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
    # Fall back to the built-in KNOWN_ASSESSOR_URLS map so PACS enrichment
    # works even when the connector row's assessor_url is still NULL
    # (e.g. migration 022 not yet applied to this environment).
    if not connector_assessor_url:
        from src.scrapers.enrichment.assessor_urls import KNOWN_ASSESSOR_URLS
        key = f"{config.county.lower()}_{config.state.upper()}"
        connector_assessor_url = KNOWN_ASSESSOR_URLS.get(key)
    from src.scrapers.enrichment.pacs import (
        LOOKUP_FAILED,
        PACS_NAME_LOOKUP_KEY,
        batch_lookup_pacs_by_name,
        is_pacs_url,
    )
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
            name_hits = name_failed = 0
            for res, (outcome, pacs) in zip(results_no_addr, pacs_results, strict=True):
                # Every row looked up records what the lookup came to, so the results
                # page can tell "the county site did not answer" from "no property
                # under this name" instead of showing both as a blank address.
                ed = dict(res.enrichment_data) if isinstance(res.enrichment_data, dict) else {}
                ed[PACS_NAME_LOOKUP_KEY] = outcome
                res.enrichment_data = ed
                if outcome == LOOKUP_FAILED:
                    name_failed += 1
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
            save_failed = False
            try:
                db.commit()
            except Exception as exc:
                _logger.warning(
                    "Job %s: PACS enrichment commit failed (%d fills discarded): %s",
                    job_id, name_hits, type(exc).__name__,
                )
                db.rollback()
                db.commit()
                # Nothing was kept, markers included: every row is unanswered, and the
                # line below must not report fills that were thrown away (Codex).
                name_hits, name_failed, save_failed = 0, len(results_no_addr), True
            # A failed lookup is not a miss. Folding the two together is how a run
            # whose every request failed read "Found 0/71" and then "addresses added".
            failed_note = (
                f" ({name_failed} lookup{'' if name_failed == 1 else 's'} failed: the county site did not answer)"
                if name_failed else ""
            )
            if summary is not None:
                summary["name_lookup_failed"] = name_failed
            _publish_log(
                r, job_id, "warning" if name_failed else "info",
                f"Address lookups via PACS could not be saved; {name_failed} records are left without one"
                if save_failed else
                f"Found {name_hits}/{len(results_no_addr)} addresses via PACS{failed_note}",
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
            _pcv_fetched = lookup_parcels(list(_pcv_map), source="tacoma_code_violations",
                                          budget_s=240, stats=_pcv_stats)
            try:
                _pcv_plans, _ = plan_owner_decisions(_pcv_map, _pcv_fetched)
                _pcv_counts = write_owner_decisions(db, _pcv_plans, checked_at=_now().isoformat())
                _publish_log(r, job_id, "info",
                             f"Found {_pcv_counts.get('matched', 0)} property owners for "
                             "code violations.", db=db)
            except Exception as exc:
                db.rollback()
                # Type only: a DB error string can carry the bound taxpayer name (Codex r11).
                _logger.warning("Job %s: Pierce code violation owner write failed: %s",
                                job_id, type(exc).__name__)

    # Seattle SDCI code violations carry coordinates but no parcel, so the parcel-keyed
    # passes below can never give them a mailing address. Locate the parcel strictly (one
    # polygon, same normalized street and ZIP); when that fails, try King's own address
    # points (src/scrapers/enrichment/king_address_points.py), and take the mailing from
    # the Assessor extract for a tier that allows it. The PIN is stored beside the lead,
    # never in parcel_id (dedup/billing). Rows without coordinates are included: they
    # get the terminal no_coordinates status and can only reach the hidden address_only tier.
    # Bellevue, Burien and King County Accela rows are never located here, even without a
    # printed parcel: their owner and skip trace are keyed on the printed PIN only, and
    # those with one take the ordinary parcel-keyed King passes below instead.
    if (config.county.lower() == "king" and config.state.upper() == "WA"
            and config.record_type == "code_violation"):
        from src.scrapers.king_cv_sources import PARCEL_AT_SCRAPE_SOURCES

        _cv_rows = {
            str(res.id): res for res in all_results
            if not res.parcel_id and not res.mailing_address
            and isinstance(res.enrichment_data, dict)
            and res.enrichment_data.get("source") not in PARCEL_AT_SCRAPE_SOURCES
            and ((res.enrichment_data.get("latitude") and res.enrichment_data.get("longitude"))
                 or (res.property_address or "").strip())
            and not res.enrichment_data.get("kc_pin_status")
        }
        if _cv_rows:
            from src.scrapers.enrichment.king_address_points import (
                EVIDENCE_KEY as _KC_AP_EVIDENCE,
            )
            from src.scrapers.enrichment.king_parcel_locate import (
                resolve_code_violation_mailing,
            )

            _publish_log(r, job_id, "info",
                         f"Matching {len(_cv_rows)} code violations to King County parcels...",
                         db=db)
            # Budget covers the WHOLE step: 420 s of parcel lookups (15 s request
            # timeout, so the last call ends by ~435 s) + one extract scan (~15 s) +
            # commit. It is counted in the code_violation budget sum below, beside the
            # owner pass and the (shorter) parcel-keyed King pass. The address-point
            # fallback runs inside the same 420 s and only starts while both of its
            # requests can still time out before the deadline.
            # Rows not reached keep no status and are picked up by
            # scripts/backfill_king_code_violation_mailing.py; rows whose fallback was not
            # reached keep their point status and are picked up by
            # scripts/backfill_king_code_violation_owner.py --address-points.
            try:
                _cv_decisions, _cv_snapshot = resolve_code_violation_mailing(
                    [(k, res.enrichment_data.get("latitude"), res.enrichment_data.get("longitude"),
                      res.property_address) for k, res in _cv_rows.items()],
                    budget_s=420, address_points=True,
                    property_zips={k: res.property_zip for k, res in _cv_rows.items()},
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
                # kc_pin_source comes from the decision: the strict point rule and the
                # address points stamp different sources, and each tier is only trusted
                # with its own (src/utils/located_parcel.py).
                ed.update({key: d[key] for key in ("kc_pin_status", "kc_pin", "kc_parcel_address",
                                                   "kc_pin_match", "kc_pin_source",
                                                   _KC_AP_EVIDENCE)
                           if key in d})
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

        # No source names the owner, so party_name arrives empty. The PIN Bellevue,
        # Burien or King County Accela printed (parcel_id), or a shown located SDCI PIN
        # (exact, street-level or address point; never address_only or a condo complex),
        # names the owner through the same owner-only eRealProperty path King tax uses:
        # lease-guarded, paced, breaker-protected, and it drops any page the county served
        # for a different parcel. The tax-only owner pass below never runs for this job;
        # this takes its 300 s slot in the budget sum. Rows not reached keep no owner and
        # are named later by the beat sweep src/workers/cv_owner_recovery.py; printed-PIN
        # parcels, the ones we are sure of, are asked first.
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
            # left for persistence, export, billing and delivery. A code_violation job
            # also runs the SDCI parcel match (~450s) before its owner pass, so this
            # pass gets 240s there: 1800 + 450 + 300 + (240 + 60) = 2850s, ~750s left.
            # Its parcel rows (Bellevue, Burien) are about a hundred a month and the
            # extract usually fills their mailing first. Raise a budget only by
            # re-doing that sum.
            _KING_TOTAL_BUDGET_S = 240 if config.record_type == "code_violation" else 600
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

    # Truth for the completion line: parcel-bearing rows that still have no mailing
    # address. Counted from the rows themselves rather than from what a pass happened
    # to MARK, so a re-run cannot announce success while the same leads are still
    # empty. Measured HERE, at the very end, because the PACS and Pierce-legal passes
    # above can still fill a mailing address — counting it beside the GIS sweep
    # reported addresses missing that were recovered moments later (Codex Medium).
    #
    # Counted for EVERY county, with or without a mailing source. Gating it on
    # has_mailing_source() is what let a county with no source print "Enrichment
    # complete: addresses added" over 1,335 leads and 0 mailing addresses (Clark job
    # 62404bd0, 2026-10-02); "N leads have no mailing address available" is the
    # honest line there too.
    if summary is not None:
        try:
            summary["mailing_missing"] = len([
                res for res in all_results
                if not res.is_duplicate and not res.mailing_address
                and res.parcel_id and len(res.parcel_id.strip()) >= 6
            ])
        except Exception as exc:  # noqa: BLE001 -- a report must never fail the job
            _logger.warning("mailing_missing count skipped: %s", str(exc)[:120])
        # Rows left with neither address. mailing_missing above counts parcel-bearing
        # rows only, so a run whose records carry no parcel at all (Island probate,
        # 71 of 71) reported nothing missing and closed on "addresses added".
        # Address half of the delivery rule only: a retried job can still carry an
        # earlier attempt's over-quota marker, which is not a missing address (Codex).
        try:
            from src.api.lead_actionability import has_address
            summary["no_address"] = sum(1 for res in all_results if not has_address(res))
        except Exception as exc:  # noqa: BLE001 -- a report must never fail the job
            _logger.warning("no_address count skipped: %s", type(exc).__name__)

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


# ── Shared by the scrape enqueue and the contact-lookup action worker ────────
# Phase 1b-2 (consult r1 Q4): the action worker must decide "charged but unanswered"
# and "already answered in the cache" EXACTLY as the enqueue does, or the two paths
# drift and one of them pays for a lead the other would not. So both call these,
# rather than the worker carrying a copy. Neither commits: each caller owns its
# transaction and holds `lock_job_for_claim` around the cache write.


def settle_charged_unanswered(db, user_id, rows: list) -> tuple[list, int]:
    """Drop and settle leads whose earlier lookup was charged but unmatched.

    A lookup Tracerfy already CHARGED for and we could not attribute ('unmatched')
    is not retried on every run: the same address would most likely fail the same
    way, and each retry is billed. Inside the freshness window such an
    already-delivered lead is settled as 'errored' (what its earlier row already
    shows); past it, it is asked again like any stale lead. A transport failure or a
    pre-submit rejection leaves no 'unmatched' row, so it IS retried (Codex).

    The enqueue runs this TWICE (Security Master Review pass 4): once before the
    job lock, and again under it. The dispatcher and ingest do not take that lock,
    so a row can become 'unmatched' -- charged, with no answer -- between the two
    passes, and buying it again is a second charge for a question the vendor
    already failed to answer.

    Returns the rows still to decide and how many were settled. Does not commit.
    """
    from sqlalchemy import text as _sa_text

    delivered_before = [rec for rec in rows if rec.is_duplicate and rec.dedup_hash]
    if not delivered_before:
        return rows, 0
    charged_unanswered = set(db.execute(
        _sa_text(
            "SELECT DISTINCT r.dedup_hash FROM pending_skip_trace_rows p "
            "JOIN results r ON r.id = p.result_id AND r.user_id = p.user_id "
            "WHERE p.user_id = CAST(:uid AS uuid) AND p.status = 'unmatched' "
            "  AND r.dedup_hash = ANY(CAST(:hashes AS text[])) "
            "  AND COALESCE(p.submitted_at, p.enqueued_at) "
            "      >= NOW() - make_interval(days => :ttl)"
        ),
        {"uid": str(user_id), "ttl": int(settings.SKIP_TRACE_CACHE_DAYS),
         "hashes": sorted({rec.dedup_hash for rec in delivered_before})},
    ).scalars())
    if not charged_unanswered:
        return rows, 0
    settled = [rec for rec in delivered_before if rec.dedup_hash in charged_unanswered]
    for rec in settled:
        rec.skip_trace_status = "errored"
        rec.skip_trace_attempted_at = _now()
    settled_ids = {rec.id for rec in settled}
    return [rec for rec in rows if rec.id not in settled_ids], len(settled)


def copy_cached_answer(db, user_id, rec, payload: dict) -> bool:
    """Copy a still-fresh cached answer for this exact lookup onto `rec`. True if it did.

    v2 (migration 098 cutover): the key is the SUBJECT this lookup would be
    bought for — account, address, trace type and the exact names in the
    payload — not the address alone. Two owners at one address are now two
    answers, so an heir's lead can no longer be served the deceased owner's
    phone. The subject comes from the payload actually built, never
    recomputed from party_name.

    The legacy address-only read (and its mailing-locality fallback) is GONE
    rather than kept as a fallback: a legacy row cannot tell us whose answer
    it holds, so reading one is the leak. Those rows are inert and age out
    with the 90-day retention; deleting them is a separate PII-hygiene step.
    Cost of the cutover: a repeat address may be paid for again inside that
    window.

    An ORM write: phone/email are EncryptedString/EncryptedJSON, so this must never
    become raw SQL (it would store plaintext PII). Does not commit.
    """
    from src.db.models import SkipTraceCache
    from src.scrapers.enrichment.skip_trace import payload_subject_key

    cache_key = payload_subject_key(user_id, payload)
    cached = db.get(SkipTraceCache, cache_key)
    if not cached:
        return False
    # 90-day TTL check
    age = _now() - cached.fetched_at
    if age.days >= settings.SKIP_TRACE_CACHE_DAYS:
        return False
    # Copy cached values directly to the Result — no Tracerfy call
    rec.phone = cached.phone
    rec.phone_type = cached.phone_type
    rec.phone_dnc_flag = cached.phone_dnc_flag
    rec.email = cached.email
    rec.phones = cached.phones
    rec.emails = cached.emails
    rec.skip_trace_status = "hit" if (cached.phone or cached.email) else "miss"
    # When the data was obtained (the cache entry), not now: attempted_at is the
    # 365-day PII retention clock, and a copy must not restart it.
    rec.skip_trace_attempted_at = cached.fetched_at
    rec.skip_trace_source = "reused"  # no lookup bought for this row
    # WHOSE answer this is (098). Recorded now, while the subject is
    # known, because it cannot be reconstructed later: party_name gets
    # rewritten by owner recovery, and a recomputed subject would then
    # name the current owner while these contacts belong to the previous
    # one. The duplicate-reuse passes require an exact match on this.
    rec.skip_trace_subject_hash = cache_key
    return True


def _enqueue_skip_trace_rows(
    db, job, r, job_id: str, config, *, on_begin=None, attempt_token=None,
) -> None:
    """Enqueue eligible Result rows into pending_skip_trace_rows.

    Called by run_scrape_job AFTER enrichment AND the plan cap, so the
    actionable_condition() below already excludes rows the cap marked
    over_quota: a lead that will not be delivered is never traced. Only
    records with a property_address are eligible.

    Runs only if SKIP_TRACE_ENABLED, TRACERFY_API_TOKEN is set, the config has
    skip_trace_enabled and the plan is not Starter. Cache hits are copied onto
    the row for free; misses are queued for the dispatcher, which makes the
    actual (paid) Tracerfy calls.

    ``on_begin`` is called ONCE, after every one of those gates has passed, there
    is at least one eligible row, and at least one of those rows would actually
    produce a lookup payload as the row reads at that moment. The caller uses it to enter the `queuing_contacts`
    stage. It lives here rather than at the call site so the gates are stated
    once: a copy of them next to the stage write would drift, and the version
    that drifted announced the stage for every run whose plan, config or
    eligible-row count meant nothing would be queued at all (Codex round 7).

    It is an ANNOUNCEMENT, not a guarantee, and the difference is load-bearing.
    It must fire before the advisory lock is taken, because the caller's stage
    write commits and a transaction-scoped lock does not survive a commit — so
    it cannot wait for the claim to prove itself. The gates it cannot speak for
    are enumerated at the call site below. It is therefore correct to read this
    as "this run is about to try", and wrong to read it as "rows were queued":
    the pending rows themselves are the only evidence of that.

    ``attempt_token`` (run_scrape_job's `AttemptToken`) fences the claim to the
    attempt that owns the job: see the check under the claim lock below.
    """
    # Local imports — sa_select must be imported here because the module-
    # level import is scoped inside _run_inline_enrichment, not globally
    from sqlalchemy import select as sa_select

    from src.db.models import Result
    from src.scrapers.enrichment.skip_trace import (
        build_pending_row_payload,
        code_violation_skip_trace_allowed,
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

    # ONE ENQUEUE PER JOB AT A TIME (Codex round 15 diff review, round 4).
    #
    # run_scrape_job's atomic claim stops a job being double-SCRAPED, but
    # watchdog_stuck_jobs re-queues a job that merely looks stuck, and a slow but
    # still-living worker can then be joined by a second one. Two concurrent
    # enqueues of the same job read the same 'not_attempted' rows and both claim
    # them. With migration 100 applied the index refuses the second; without it
    # (the fail-open path) both rows survive, and because owner recovery can
    # rewrite party_name between the two reads they may carry DIFFERENT
    # trace_types -- which the dispatcher's submission-collision key does not
    # collapse, so both get submitted and the customer is charged twice.
    #
    # A transaction-scoped advisory lock on the job id serializes the whole
    # read-decide-claim, so the second enqueue sees the rows already 'queued' and
    # claims nothing. Transaction-scoped: released by the commit below, and by a
    # rollback, so a crash cannot hold it. Keyed on the job, so jobs never wait
    # on each other.
    #
    # Taken BELOW rather than here, deliberately: the charged-unanswered branch
    # commits mid-function, and a transaction-scoped lock taken before it would
    # be released by that commit and cover nothing that matters. It is acquired
    # immediately before the cache-and-claim loop, which runs to the final commit
    # with no commit in between.

    # Reload the surviving results after the unactionable drop. Eligible: the rows this
    # run delivers, AND the rows an earlier run of this account already delivered.
    # Delivered and traced are separate facts: a lead delivered with skip trace off
    # still gets its first lookup when a later run turns skip trace on (owner,
    # 2026-09-18; this line used to drop every duplicate, so it never could).
    # _reuse_enrichment_for_duplicates (run first) has already copied any settled trace
    # of this account inside the TTL, so a row reaching here still 'not_attempted' has
    # nothing reusable; the cache check below is the second chance before paying.
    # Same-run siblings (e.g. the trustee_sale collapse) stay out: another row of this
    # run is the same property and is the one traced (Codex).
    from src.api.lead_actionability import actionable_condition
    from src.api.results_category import skip_trace_eligible_condition

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
            skip_trace_eligible_condition(),
        )
    ).scalars().all()

    # Charged-but-unanswered leads are settled, not bought again: see
    # settle_charged_unanswered (shared with the contact-lookup action worker).
    eligible, _settled_n = settle_charged_unanswered(db, job.user_id, eligible)
    if _settled_n:
        # Committed here: `if not eligible: return` below would otherwise drop it.
        db.commit()
        _publish_log(
            r, job_id, "info",
            f"Skip trace not repeated for {_settled_n} already delivered lead(s): an "
            "earlier lookup was charged but could not be matched to the lead",
            db=db,
        )

    # A PLACEHOLDER street is not an address, and skip trace bills per lookup.
    # Worse than the money: the cache key hashes the ADDRESS (along with the owner
    # since 098), so every row sharing one placeholder string AND one owner name
    # collapses to ONE cache key — measured in
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
    # 2026-09-13). Exact status values; "Closed" is deliberately still traced. A King County
    # Accela case voided or closed with no violation is settled the same way.
    if config.record_type == "code_violation":
        settled_rows = [rec for rec in eligible if _is_settled_complaint(rec.enrichment_data)]
        if settled_rows:
            _settled_ids = {rec.id for rec in settled_rows}
            eligible = [rec for rec in eligible if rec.id not in _settled_ids]
            _publish_log(
                r, job_id, "info",
                f"Skip trace skipped for {len(settled_rows)} code violation lead(s) whose "
                "case the city already settled (completed, duplicate, voided or no violation)",
                db=db,
            )

    if not eligible:
        return

    # Contact lookups are about to be ATTEMPTED — not necessarily queued; the
    # exceptions are listed at the end of this comment. Safe to commit on its own
    # here for the same reason the caller's write was: everything before this
    # point either committed itself or was read-only, so this commits nothing but
    # the stage, and it must not stay pending — an open UPDATE holds a lock on
    # the jobs row, which is the row Cancel Run writes.
    #
    # It fires HERE, before the lock below, and it has to: `_set_stage` COMMITS,
    # and a transaction-scoped advisory lock is released by any commit. Moving
    # this announcement below the lock to make it more accurate would release the
    # lock the claim depends on — a P2 traded for a P1.
    #
    # That places it before the per-row gates, so this pre-check stands in for
    # them. The case it exists for is a run whose every party_name is a case
    # DESCRIPTION rather than a person ("Weeds ? 1819 HARVARD AVE", the shape
    # code-violation scrapers write): every payload is then None, and the run
    # announces "queuing contact lookups" and queues nothing, every time. That
    # is the same false label Codex round 7 removed from the call site,
    # reintroduced from below instead of above.
    #
    # NOTE, because the obvious guess is wrong: a lead with NO party_name is not
    # this case. It still queues, as an address-only advanced trace, so a
    # missing name announces truthfully. `build_pending_row_payload` is the
    # authority on what will not queue, which is why the check calls it rather
    # than re-deriving the rule — and it already applies the ATIP policy gate
    # internally, so naming that gate again here would only invite it to drift.
    # It reads the row and process settings only: no query, no I/O and no
    # mutation of the Result (it returns a fresh dict), so asking it early is
    # safe, but it is NOT a pure function of the row alone — `code_violation_
    # skip_trace_allowed` reads a global flag, and API and worker carry separate
    # env (15-14). `any()` stops at the first lead that would produce a payload,
    # so the common case parses one row and only an all-rejected run scans the
    # set, which is exactly the run this exists to catch. This check therefore
    # PARSES A ROW TWICE — once here and once in the loop below — and that
    # second parse is deliberate, not an oversight to be optimised away later.
    # Carrying these payloads down into the loop would remove it, and must not:
    # the loop re-reads its rows under the lock with populate_existing, so a
    # payload computed from the pre-lock row would reintroduce the
    # stale-subject class of bug that 14-B exists to prevent. Paying the parse
    # twice is the cost of the loop staying authoritative. This check can only
    # ever SUPPRESS an announcement, never authorise a claim.
    #
    # Two ways it is still not the truth, both accepted, neither costing money:
    #  * It can OVERSTATE. Every lead may turn out to be a cache hit (free
    #    reuse, nothing queued, but knowing needs a DB read per lead); the
    #    post-lock re-read or `_settle_charged_unanswered` may empty the set
    #    concurrently; or the fail-closed `ClaimUnenforcedError` may fire when
    #    migration 100 is absent. Each is either real work for the customer or a
    #    genuinely rare race, not a run that was never going to queue anything.
    #  * It can UNDERSTATE, and this one is a real race (Codex): owner recovery
    #    rewrites `party_name` for these same rows, so a lead that had no
    #    payload here can have one by the time the post-lock re-read refreshes
    #    it, and the run then queues without ever announcing the stage. A
    #    missing label is strictly better than the false one it replaces, and
    #    the pending rows — never this callback — are the evidence of what was
    #    queued. Fixing it properly means announcing after the claim, which the
    #    commit rule above forbids.
    if on_begin is not None and any(
        build_pending_row_payload(rec) is not None for rec in eligible
    ):
        on_begin()

    cache_hits = 0
    cache_misses = 0
    enqueued_normal = 0
    enqueued_advanced = 0
    # Cutover observability (Phase 1a): proves from production that this path is
    # reading v2 keys, rather than assuming the deploy took. Pairs with the
    # warning `address_cache_key` now logs if anything still reads a legacy key.
    _v2_key_reads = 0

    # ONE ENQUEUE PER JOB AT A TIME (Codex round 15 diff review, rounds 4-5).
    #
    # run_scrape_job's atomic claim stops a job being double-SCRAPED, but
    # watchdog_stuck_jobs re-queues a job that merely looks stuck, and a slow but
    # still-living worker can then be joined by a second one. Two concurrent
    # enqueues of the same job read the same 'not_attempted' rows and both claim
    # them. With migration 100 applied the index refuses the second; without it
    # (the scrape's fail-open path) both rows can survive, and because owner
    # recovery can rewrite party_name between the two reads they may carry
    # DIFFERENT trace_types -- which the dispatcher's submission-collision key
    # does not collapse, so both are submitted and the customer is charged twice.
    #
    # Acquired HERE, not at the top of the function: the charged-unanswered
    # branch above commits, and a transaction-scoped lock taken before it would
    # have been released by that commit. From this point to the final commit
    # there is no commit, so the lock genuinely spans the read-decide-claim.
    # Transaction-scoped, so both a commit and a rollback release it and a crash
    # cannot hold it. Keyed on the job, so jobs never wait on each other.
    from src.workers.skip_trace_claim import lock_job_for_claim

    lock_job_for_claim(db, job_id)
    # ONLY THE ATTEMPT THAT OWNS THE JOB BUYS LOOKUPS FOR IT (2c-bis, Codex diff
    # r6 P1). The watchdog can re-queue a stalled run while this one is still
    # alive, and a replacement then owns the job. A stale attempt reaching here
    # would queue paid lookups and copy cache hits for leads it no longer owns.
    # `attempt_state` locks the jobs row FOR UPDATE and that lock, like the
    # advisory one above, holds to the final commit below, so a re-queue cannot
    # land between this answer and the claim. A terminal job buys nothing either;
    # its cleanup belongs to whoever finds it terminal. Lock order (advisory, then
    # jobs row) is safe: no other claimer of this lock touches the jobs row.
    # `None` is the legacy call form (tests and scripts), unfenced as before;
    # run_scrape_job always passes its token.
    if attempt_token is not None:
        from src.workers.tasks_helpers.status import attempt_state

        if not attempt_state(db, job_id, job.user_id, attempt_token).owned:
            db.rollback()
            # Engineering log only: after the rollback, so it cannot release the
            # lock early, and never published onto the replacement's live stream.
            _logger.info(
                "Job %s: attempt token changed or job terminal; not queuing contact "
                "lookups", job_id,
            )
            return
    # Re-read the leads under the lock. The set read before it is stale by now:
    # a concurrent enqueue may have claimed some of them, and the claim's own
    # join would drop those anyway, but re-reading keeps the cache-hit path from
    # copying an answer onto a row another writer already owns.
    # populate_existing: these Result objects are already in the identity map
    # from the read above, so without it SQLAlchemy returns the SAME instances
    # with their stale attributes. The WHERE clause would still filter correctly
    # (it runs in the database), but the cache-hit path below reads and writes
    # these objects, and it must see what is committed right now.
    #
    # It repeats EVERY predicate of the first read, not just the status. A lead
    # can become over quota, undeliverable or a superseded duplicate between the
    # two reads, and re-checking only 'not_attempted' would queue and pay for it
    # anyway. Narrowing by id is not a substitute: those ids qualified when they
    # were read, which is exactly the thing that may have changed.
    eligible = list(db.execute(
        sa_select(Result).where(
            Result.id.in_([rec.id for rec in eligible]),
            Result.user_id == job.user_id,
            Result.property_address.isnot(None),
            actionable_condition(),
            Result.skip_trace_status == "not_attempted",
            skip_trace_eligible_condition(),
        ).execution_options(populate_existing=True)
    ).scalars().all())
    # The two PYTHON filters above ran on the pre-lock objects, so their verdicts
    # are as stale as the rows were. Re-apply them to the refreshed ones: a lead
    # whose address became a placeholder, or whose code-violation case the city
    # settled, between the two reads would otherwise keep a verdict of "eligible"
    # that is no longer true and be paid for. Re-running is free (both are pure
    # functions of the row) and it cannot ADD anything, because `eligible` is
    # already bounded by the ids that survived the first pass. No second log
    # line: the counts were reported above and this only ever removes stragglers.
    eligible = [rec for rec in eligible
                if not street_is_placeholder(rec.property_address)]
    if config.record_type == "code_violation":
        eligible = [rec for rec in eligible
                    if not _is_settled_complaint(rec.enrichment_data)]
    # And the charged-unanswered rule, again, for the same reason. The first pass
    # ran before the lock and had to commit (its log line commits), so the
    # dispatcher or ingest can have marked one of these leads 'unmatched' since:
    # charged, unanswered, and about to be bought a second time. No log line
    # here, because logging commits and that would release the lock; the count
    # is reported after the final commit below.
    eligible, _late_settled = settle_charged_unanswered(db, job.user_id, eligible)
    if not eligible:
        # The settles above are real writes and must not be dropped by returning.
        db.commit()
        if _late_settled:
            # Reported HERE as well as at the end: this early return is the path
            # where the late pass settled every remaining lead, and it is exactly
            # the case worth telling the customer about.
            _publish_log(
                r, job_id, "info",
                f"Skip trace not repeated for {_late_settled} further already "
                "delivered lead(s): an earlier lookup was charged but could not be "
                "matched to the lead",
                db=db,
            )
        return

    skipped_ineligible = 0
    skipped_atip_policy = 0
    # Payloads for leads with no usable cached answer, claimed in one statement
    # after the loop rather than added row by row inside it.
    to_claim: list[dict] = []
    for rec in eligible:
        # An ATIP-named Tacoma owner may be shown, not spent on: counted and reported on
        # its own line, never as "no traceable owner name", which would send whoever
        # reads the job log looking for missing data instead of a switch (Codex).
        if not code_violation_skip_trace_allowed(rec):
            skipped_atip_policy += 1
            continue
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

        # The v2 subject-keyed cache read and the ORM copy: copy_cached_answer
        # (shared with the contact-lookup action worker).
        _v2_key_reads += 1
        if copy_cached_answer(db, job.user_id, rec, payload):
            cache_hits += 1
        else:
            # Collected, not inserted here. The claim is ONE set-based statement
            # after the loop (see below) so that a conflict is a row-level
            # no-op instead of a batch-level failure. Truncation to the column
            # widths now lives in claim_skip_trace_rows, beside the insert it
            # protects and beside the widths lookup_subject_key hashes against,
            # so the cache read and the queue write cannot diverge.
            to_claim.append(payload)

    # THE CLAIM (Codex round 15, finding 15-1). This used to be db.add() per row
    # plus `rec.skip_trace_status = 'queued'`, flushed at one commit() whose
    # handler was `except Exception: db.rollback(); db.commit()`. Migration 100
    # adds a partial unique index on pending_skip_trace_rows(result_id) for
    # active rows, and under that index a single conflicting row -- which the
    # Phase 1b "look up contacts" action can now cause by claiming the same lead
    # concurrently -- would have raised IntegrityError at that commit, rolled
    # back THE WHOLE JOB'S enqueue (every pending row and every results update)
    # and then committed an empty transaction. Silent, total, unreported loss.
    #
    # The shared helper instead reports exactly which leads it won, and advances
    # `results` for those and only those. A lost race claims nothing and strands
    # nothing. It deliberately does not commit: the transaction stays ours, which
    # is what lets the action worker later write its dispositions in the same one.
    claimed_ids: list[str] = []
    # What the claim held back because the account may not buy it (trial allowance
    # spent, Starter, frozen, ended). Told to the customer after the commit.
    claim_report: dict = {}
    _held_line: str | None = None
    if to_claim:
        from src.workers.skip_trace_claim import (
            ClaimUnenforcedError,
            claim_skip_trace_rows,
        )

        # Fails closed if migration 100's index is absent: the leads stay
        # 'not_attempted' and are claimed by the next run once the migration
        # lands. That is a pause; proceeding unenforced would risk charging a
        # customer twice for one lead, which trying again later cannot undo.
        #
        # Caught HERE rather than left to tasks.py (Security Master Review pass
        # 2). tasks.py rolls the whole enqueue transaction back, which would
        # also discard the cache hits copied above -- contacts this account
        # ALREADY PAID FOR, free to reuse, and silently missing from the
        # delivered export. Swallowing the claim but keeping the hits means the
        # paid work survives and only the unbought lookups wait.
        try:
            claimed_ids = claim_skip_trace_rows(db, to_claim, report=claim_report)
        except ClaimUnenforcedError as exc:
            _logger.error("Job %s skip trace claim refused: %s", job_id, exc)
            # PAGE someone. Swallowing this keeps the job healthy, which is the
            # point, but it also means the paid lookup pipeline can sit paused
            # indefinitely while every worker looks fine. The alert is the only
            # thing that makes the pause visible.
            try:
                from src.workers.ops_alerts import send_ops_alert

                send_ops_alert(
                    "skip_trace_claim_unenforced", "enqueue",
                    "Skip-trace claims are refused: migration 100 is not in place",
                    f"{len(to_claim)} lead(s) on job {job_id} were not queued "
                    f"because the unique index that stops a lead being looked up "
                    f"twice is missing, invalid or not the expected index. "
                    f"Contact lookups are PAUSED and stay paused until it is "
                    f"applied. Nothing was charged and no lead was lost. {exc}",
                )
            except Exception:  # noqa: BLE001 - an alert failure must not fail the job
                _logger.exception("skip-trace unenforced-claim alert failed to send")
            _publish_log(
                r, job_id, "warning",
                f"Contact lookups are paused for {len(to_claim)} lead(s): a "
                "database safeguard that stops a lead being looked up twice is "
                "not in place. No new lookup was charged. These leads stay "
                "pending and become eligible again on the next run once the "
                "safeguard is restored.",
                db=db,
            )
            to_claim = []
        claimed = set(claimed_ids)
        for payload in to_claim:
            if str(payload["result_id"]) not in claimed:
                continue
            cache_misses += 1
            if payload["trace_type"] == "advanced":
                enqueued_advanced += 1
            else:
                enqueued_normal += 1
        # Held leads were refused on purpose and are reported to the customer
        # below; they are not part of this unexplained count. Only when there IS
        # a line to explain them: an access value with no message must still
        # show up here, or the leads would vanish from both logs.
        if claim_report.get("held"):
            from src.config import settings as _settings
            from src.workers.skip_trace_claim import held_lookup_message

            _held_line = held_lookup_message(
                claim_report.get("access", ""), claim_report["held"],
                _settings.SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE,
            )
        explained = claim_report["held"] if _held_line else 0
        lost = max(0, len(to_claim) - len(claimed) - explained)
        if lost:
            # Deliberately does NOT say "already claimed". The claim refuses a
            # lead for several reasons -- an active claim elsewhere, a lead that
            # settled or was deleted in between, a payload whose state or
            # trace_type cannot be stored -- and this count cannot tell them
            # apart. Naming one of them would send whoever reads this looking for
            # a race that may not exist. The claim logs the unwritable ones by id
            # separately. Not an error either way; a persistently large number
            # here is the signal worth chasing.
            _logger.info(
                "Job %s skip trace: %d of %d lead(s) were not claimed (active claim "
                "elsewhere, settled in between, or an unusable payload)",
                job_id, lost, len(to_claim),
            )

    try:
        db.commit()
    except Exception:
        # RAISES, where the original swallowed. The old handler rolled back and
        # then committed an empty transaction, so every count below still
        # reported cache hits and queued leads that no longer existed: the job
        # log told the customer their leads were queued while the rows were
        # gone. Nothing here is safe to report as success unless the commit
        # actually happened.
        #
        # Raising is safe. tasks.py catches this, logs it and lets the job
        # finish, so leads are still delivered; and every lead whose claim did
        # not commit is still 'not_attempted', so the next run picks it up. The
        # cache-hit copies are lost with it, but they are free to redo -- unlike
        # a queued row that was reported as bought and was not.
        _logger.exception("Job %s skip trace enqueue commit failed; rolling back", job_id)
        db.rollback()
        raise

    if _held_line:
        # After the commit: _publish_log commits, and the job lock had to stay held.
        _publish_log(r, job_id, "info", _held_line, db=db)

    if _late_settled:
        # Reported only now: this log commits, and until the line above it the
        # job lock had to stay held.
        _publish_log(
            r, job_id, "info",
            f"Skip trace not repeated for {_late_settled} further already delivered "
            "lead(s): an earlier lookup was charged but could not be matched to the "
            "lead",
            db=db,
        )

    if skipped_atip_policy:
        _publish_log(
            r, job_id, "info",
            f"Skip trace skipped for {skipped_atip_policy} Tacoma code violation lead(s): "
            "their owner name comes from the county's property record, which we may show "
            "but not use for a paid contact lookup. The leads keep their owner name.",
            db=db,
        )

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
        "Job %s skip trace enqueue: cache_hits=%d queued=%d (normal=%d advanced=%d) "
        "v2_key_reads=%d",
        job_id, cache_hits, cache_misses, enqueued_normal, enqueued_advanced,
        _v2_key_reads,
    )
