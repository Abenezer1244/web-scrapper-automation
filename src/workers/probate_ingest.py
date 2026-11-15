"""Store published probate notices and the parcels tied to each estate (migration 160).

Called from the nwmarket crawl (workers/nts_crawler.crawl_nts_nwmarket_asotin) on every
advert's text, before the trustee-sale filter. A notice that is an Asotin probate is
matched to parcels right away (enrichment/asotin_parcels, owner rule "name + family
tie") and stored with the outcome:
  matched   one or more tied parcels (``parcels``), each a lead for the probate scraper
  held      no parcel could be tied (``held_reasons`` says why); re-checked for 30 days
  failed    the county layer gave no answer; re-checked
  withheld  a privacy rule covers a tied parcel: the notice keeps no person (tombstone)

The layer is queried BEFORE the write, so no transaction waits on the network. A
tombstoned notice is never written again (the upsert skips it). Notices are deleted
RETENTION_DAYS after first publication (the creditor-claims window is four months);
tombstones stay, so a republished notice cannot bring the person back.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta

from sqlalchemy import text

from src.scrapers.enrichment import asotin_parcels as ap
from src.scrapers.sources import probate_nwmarket as pn
from src.utils.logger import setup_logger

_logger = setup_logger("worker.probate_ingest")

STATE = "WA"
RETENTION_DAYS = 180
REMATCH_DAYS = 30
REMATCH_EVERY = timedelta(hours=20)
REMATCH_BATCH = 20

Lookup = Callable[[str], list[dict]]
Gate = Callable[[list[dict], str], "str | None"]

_UPSERT = text("""
    INSERT INTO probate_notices (
        id, county, state, source, cause_no, first_published, decedent, personal_reps,
        claims_address, advert_id, pdf_url, match_outcome, held_reasons, parcels,
        matched_at, fetched_at, privacy_suppression_id)
    VALUES (gen_random_uuid(), :county, :state, :source, :cause, :published, :decedent,
        CAST(:reps AS json), :claims, :advert, :pdf, :outcome, CAST(:held AS json),
        CAST(:parcels AS json), :now, :now, CAST(:suppression AS uuid))
    ON CONFLICT (county, state, cause_no) DO UPDATE SET
        first_published = LEAST(probate_notices.first_published, EXCLUDED.first_published),
        decedent = EXCLUDED.decedent, personal_reps = EXCLUDED.personal_reps,
        claims_address = EXCLUDED.claims_address, advert_id = EXCLUDED.advert_id,
        pdf_url = EXCLUDED.pdf_url,
        -- A layer outage on a re-read never replaces an answer the layer already gave.
        match_outcome = CASE WHEN EXCLUDED.match_outcome = 'failed'
            AND probate_notices.match_outcome IN ('matched', 'held')
            THEN probate_notices.match_outcome ELSE EXCLUDED.match_outcome END,
        held_reasons = CASE WHEN EXCLUDED.match_outcome = 'failed'
            AND probate_notices.match_outcome IN ('matched', 'held')
            THEN probate_notices.held_reasons ELSE EXCLUDED.held_reasons END,
        parcels = CASE WHEN EXCLUDED.match_outcome = 'failed'
            AND probate_notices.match_outcome IN ('matched', 'held')
            THEN probate_notices.parcels ELSE EXCLUDED.parcels END,
        matched_at = CASE WHEN EXCLUDED.match_outcome = 'failed'
            AND probate_notices.match_outcome IN ('matched', 'held')
            THEN probate_notices.matched_at ELSE EXCLUDED.matched_at END,
        fetched_at = EXCLUDED.fetched_at,
        privacy_suppression_id = EXCLUDED.privacy_suppression_id
    WHERE probate_notices.privacy_suppression_id IS NULL
    RETURNING id
""")
_REMATCH_DUE = text("""
    SELECT id::text AS id, county, decedent, personal_reps, claims_address, fetched_at,
           matched_at
      FROM probate_notices
     WHERE privacy_suppression_id IS NULL AND match_outcome IN ('held', 'failed')
       AND first_published >= :since AND (matched_at IS NULL OR matched_at < :stale)
     ORDER BY matched_at NULLS FIRST LIMIT :n
""")
# Every rematch write is conditional on the row being exactly as it was read (no crawl
# re-read it since: fetched_at; no other rematch answered: matched_at), so a slow
# rematch can never overwrite a newer answer. The same WHERE in all three.
_SET_MATCH = text("""
    UPDATE probate_notices SET match_outcome = :outcome, held_reasons = CAST(:held AS json),
           parcels = CAST(:parcels AS json), matched_at = :now
     WHERE id = CAST(:id AS uuid) AND privacy_suppression_id IS NULL
       AND match_outcome IN ('held', 'failed') AND fetched_at = :seen_fetched
       AND matched_at IS NOT DISTINCT FROM :seen_matched
""")
_TOMBSTONE = text("""
    UPDATE probate_notices SET decedent = NULL, personal_reps = NULL, claims_address = NULL,
           parcels = NULL, match_outcome = 'withheld', held_reasons = NULL, matched_at = :now,
           privacy_suppression_id = CAST(:suppression AS uuid)
     WHERE id = CAST(:id AS uuid) AND privacy_suppression_id IS NULL
       AND match_outcome IN ('held', 'failed') AND fetched_at = :seen_fetched
       AND matched_at IS NOT DISTINCT FROM :seen_matched
""")
# A row whose re-check raised: pushed back one interval so it cannot starve the others.
_BACK_OFF = text("""
    UPDATE probate_notices SET matched_at = :now
     WHERE id = CAST(:id AS uuid) AND privacy_suppression_id IS NULL
       AND match_outcome IN ('held', 'failed') AND fetched_at = :seen_fetched
       AND matched_at IS NOT DISTINCT FROM :seen_matched
""")
_PURGE = text("""
    DELETE FROM probate_notices
     WHERE county = :county AND state = :state AND first_published < :before
       AND privacy_suppression_id IS NULL
""")


def match_notice(decedent: str, reps: list[str], claims: str | None,
                 lookup: Lookup) -> tuple[str, list[str], list[dict]]:
    """(outcome, held reasons, tied parcels) for one notice. Network via ``lookup``."""
    name = ap.notice_name(decedent)
    if name is None:
        return "held", ["decedent_name_unreadable"], []
    try:
        rows = lookup(name.last)
    except ap.LayerError as exc:
        _logger.warning("Probate parcel lookup failed: %s", str(exc)[:120])
        return "failed", ["layer_unavailable"], []
    except ValueError:
        return "held", ["surname_not_queryable"], []
    m = ap.match_estate(decedent, reps, claims, rows)
    leads = [{
        "parcel_id": lead.parcel_id, "party_name": lead.party_name, "prop_id": lead.prop_id,
        "owner_name": lead.owner_name, "property_address": lead.property_address,
        "property_city": lead.property_city, "property_zip": lead.property_zip,
        "mailing_address": lead.mailing_address, "ties": list(lead.ties),
    } for lead in m.leads]
    return ("matched" if leads else "held"), m.held, leads


def privacy_gate(db) -> Gate:
    """A notice-level gate: the id of a rule that covers any tied parcel, else None.
    The lead is judged as the probate scraper delivers it (decedent as party)."""
    from src.privacy.authorize import ROW_COLUMNS, authorize, load_rule_snapshot

    with db.begin_nested():
        snapshot = load_rule_snapshot(db)

    def gate(parcels: list[dict], county: str) -> str | None:
        if not snapshot.by_anchor:
            return None
        for p in parcels:
            lead = dict.fromkeys(ROW_COLUMNS) | {
                "parcel_id": p["parcel_id"], "party_name": p["party_name"],
                "enrichment_data": {"assessor_current_owner": p["owner_name"]}}
            decision = authorize(snapshot, lead, record_type="probate", county=county,
                                 state=STATE)
            if not decision.deliverable:
                return decision.suppression_ids[0]
        return None

    return gate


def ingest_text(db, notice_text: str, *, advert_id: str | None, pdf_url: str | None,
                now: datetime, lookup: Lookup, gate: Gate) -> str | None:
    """Store the notice in ``notice_text`` if it is an Asotin probate. Returns its
    outcome, or None when the text is not one. The caller commits."""
    notice = pn.parse_probate_notice(notice_text)
    if notice is None:
        return None
    reps = list(notice.personal_reps)
    outcome, held, parcels = match_notice(notice.decedent, reps, notice.claims_address, lookup)
    suppression = gate(parcels, pn.COUNTY) if parcels else None
    if suppression:
        outcome, held, parcels = "withheld", [], []
    params = {
        "county": pn.COUNTY, "state": STATE, "source": pn.SOURCE, "cause": notice.cause_no,
        "published": notice.first_published, "now": now, "advert": advert_id, "pdf": pdf_url,
        "outcome": outcome, "suppression": suppression,
        "decedent": None if suppression else notice.decedent,
        "reps": None if suppression else json.dumps(reps),
        "claims": None if suppression else notice.claims_address,
        "held": json.dumps(held) if held else None,
        "parcels": json.dumps(parcels) if parcels else None,
    }
    if db.execute(_UPSERT, params).first() is None:
        return "withheld"   # already a tombstone: never written again
    return outcome


def rematch_due(db, *, today: date, now: datetime, lookup: Lookup, gate: Gate,
                deadline: float | None = None) -> int:
    """Re-check recent held/failed notices (an owner record may since name the estate, or
    the layer is back). Returns how many were re-checked. Commits per notice; stops at
    ``deadline`` (time.monotonic()); one notice failing never stops the others."""
    due = db.execute(_REMATCH_DUE, {"since": today - timedelta(days=REMATCH_DAYS),
                                    "stale": now - REMATCH_EVERY, "n": REMATCH_BATCH}).all()
    db.commit()   # no transaction waits on the layer
    done = 0
    for row in due:
        if deadline is not None and time.monotonic() > deadline:
            break
        seen = {"id": row.id, "now": now, "seen_fetched": row.fetched_at,
                "seen_matched": row.matched_at}
        try:
            reps = row.personal_reps if isinstance(row.personal_reps, list) else []
            outcome, held, parcels = match_notice(row.decedent, reps, row.claims_address, lookup)
            suppression = gate(parcels, row.county) if parcels else None
            if suppression:
                db.execute(_TOMBSTONE, {**seen, "suppression": suppression})
            else:
                db.execute(_SET_MATCH, {**seen, "outcome": outcome,
                                        "held": json.dumps(held) if held else None,
                                        "parcels": json.dumps(parcels) if parcels else None})
            db.commit()
        except Exception as exc:  # noqa: BLE001 — one notice must not starve the rest
            db.rollback()
            _logger.warning("Probate re-check of %s failed: %s", row.id, str(exc)[:120])
            db.execute(_BACK_OFF, seen)
            db.commit()
        done += 1
    return done


def purge_expired(db, *, today: date) -> int:
    """Delete notices past retention (tombstones stay). The caller commits."""
    return db.execute(_PURGE, {"county": pn.COUNTY, "state": STATE,
                               "before": today - timedelta(days=RETENTION_DAYS)}).rowcount or 0
