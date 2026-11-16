"""Rolling per-parcel tax crawl for counties that publish no delinquent list (Asotin).

Asotin's Treasurer publishes no delinquent roll; its PACS detail page shows every
parcel's tax statements (pacs_tax.parse_tax). This beat task walks the county's whole
parcel inventory a batch at a time and keeps ``pacs_tax_status`` current; the
tax_delinquent scraper (scrapers/asotin_wa_tax_delinquent) only reads that table.

* Inventory: the WA statewide parcel layer (Asotin = FIPS_NR NULL, see
  county_gis._wa_fips_filter), refreshed weekly; new parcels are added, none removed.
* Each run asks the least-recently-attempted parcels through pacs_parcel.resolve_tax,
  which holds the county's PACS lease and paces every request exactly as the mailing
  pass does (they share the lease, so they never talk to the county at once).
* ``attempted_at`` moves only for a parcel the pass actually asked about, so a lost
  lease or cooldown does not send a batch to the back of a multi-day queue.
* ``checked_at`` moves only on a settled answer; a failure keeps the last good row.
* Owner name and addresses are stored only while a parcel owes past-due tax, only
  when the privacy rules allow them, and cleared once it no longer owes.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import text

from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("workers.pacs_tax_crawler")

# Counties with no delinquent list. Ferry reads TaxSifter Treasurer pages instead of PACS
# (owner approved its first full crawl 2026-11-15).
COUNTIES = ("asotin", "columbia", "ferry")
_TAX_SOURCE = {"asotin": "pacs", "columbia": "pacs", "ferry": "taxsifter"}
_STATE = "WA"
_BATCH = 400                        # more than one budget can reach; the rest wait
_BUDGET_S = 20 * 60                 # inside the hourly slot, leaving the lease free
_INVENTORY_EVERY = timedelta(days=7)
_INVENTORY_PAGE = 2000              # the layer's maxRecordCount
_MAX_BLANK_SHARE = 0.01
# A tax answer needs an inventory refreshed this recently (it runs weekly: one miss allowed).
INVENTORY_MAX_AGE = timedelta(days=14)
INVENTORY_KEY = "pacs_tax_inventory:{}".format  # external_source_health heartbeat
# The page's "as of" is the county's own today; a date further off is a stale or broken
# snapshot, never a fresh answer.
_AS_OF_SLACK = timedelta(days=3)
# TaxSifter stamps the date its data was last loaded ("Data current as of"), not today:
# Ferry's read 3 days old on 2026-11-15. Two weeks covers a weekly load plus one miss.
_AS_OF_SLACK_BY_COUNTY = {"ferry": timedelta(days=14)}
# No settled answer for this long (lost year, layout, a block): alert even when the pass
# could ask nothing at all, which "asked but not settled" alone misses.
_SILENT_AFTER = timedelta(hours=6)
_LAST_SETTLED = text(
    "SELECT max(checked_at) FROM pacs_tax_status WHERE county = :county AND state = :state"
)

_INSERT = text(
    "INSERT INTO pacs_tax_status (id, county, state, parcel_id) "
    "SELECT gen_random_uuid(), :county, :state, p FROM unnest(CAST(:ids AS text[])) AS p "
    "ON CONFLICT (county, state, parcel_id) DO NOTHING"
)
_NEXT = text(
    "SELECT parcel_id FROM pacs_tax_status WHERE county = :county AND state = :state "
    "ORDER BY attempted_at NULLS FIRST, parcel_id LIMIT :n"
)
_SETTLE = text(
    """
    UPDATE pacs_tax_status SET
        outcome = :outcome, as_of = :as_of, delinquent_amount = :amount,
        oldest_delinquent_year = :oldest, years_owing = CAST(:years AS json),
        -- A privacy tombstone is never cleared here (lifting is the privacy flow's job),
        -- and a row under one never takes a name back, even from a racing crawl.
        owner_name = CASE WHEN coalesce(privacy_suppression_id, CAST(:suppression AS uuid))
                          IS NULL THEN :owner END,
        property_address = CASE WHEN coalesce(privacy_suppression_id,
                          CAST(:suppression AS uuid)) IS NULL THEN :situs END,
        mailing_address = CASE WHEN coalesce(privacy_suppression_id,
                          CAST(:suppression AS uuid)) IS NULL THEN :mailing END,
        privacy_suppression_id = coalesce(privacy_suppression_id, CAST(:suppression AS uuid)),
        attempted_at = :now, checked_at = :now
    WHERE county = :county AND state = :state AND parcel_id = :parcel
    """
)
_TOUCH = text(
    "UPDATE pacs_tax_status SET attempted_at = :now "
    "WHERE county = :county AND state = :state AND parcel_id = :parcel"
)


def statewide_parcel_ids(county: str, fetch) -> list[str]:
    """Every parcel id the statewide layer lists for ``county``. ``fetch(url) -> dict``.

    Raises when a page is not a feature list, so a broken layer can never be read as
    an empty county."""
    from src.scrapers.enrichment.county_gis import _WA_STATEWIDE_ENDPOINT, _wa_fips_filter

    scope = _wa_fips_filter(county)
    if not scope:
        raise ValueError(f"no statewide scope for {county}")
    ids: list[str] = []
    offset = 0
    while True:
        query = urlencode({
            "where": "1=1" + scope, "outFields": "ORIG_PARCEL_ID", "returnGeometry": "false",
            "orderByFields": "ORIG_PARCEL_ID", "resultOffset": offset,
            "resultRecordCount": _INVENTORY_PAGE, "f": "json",
        })
        data = fetch(f"{_WA_STATEWIDE_ENDPOINT}?{query}")
        features = data.get("features") if isinstance(data, dict) else None
        if features is None:
            raise ValueError(f"statewide layer answered without features: {str(data)[:120]}")
        ids += [str((f.get("attributes") or {}).get("ORIG_PARCEL_ID") or "").strip()
                for f in features]
        offset += len(features)
        if not data.get("exceededTransferLimit"):
            break
    # The layer's own count for the same scope: a short read (a dropped page, a missing
    # transfer-limit flag) would otherwise shrink coverage's denominator.
    counted = fetch(f"{_WA_STATEWIDE_ENDPOINT}?" + urlencode(
        {"where": "1=1" + scope, "returnCountOnly": "true", "f": "json"}))
    total = counted.get("count") if isinstance(counted, dict) else None
    if total != offset:
        raise ValueError(f"statewide layer listed {offset} features for {county}, counted {total}")
    # A feature without a parcel id cannot be crawled, so it is not a missing parcel: the
    # live layer has a few (Columbia 7 of 5,477) and duplicate features for multi-part
    # parcels (190), checked 2026-11-14. More than 1% blank is a renamed or emptied field.
    blank = sum(1 for i in ids if not i)
    if blank > offset * _MAX_BLANK_SHARE:
        raise ValueError(f"statewide layer: {blank} of {offset} {county} features have no parcel id")
    distinct = sorted({i for i in ids if i})
    if not distinct:  # no county here has zero parcels: never renew the heartbeat on it
        raise ValueError(f"statewide layer listed no parcels for {county}")
    _logger.info("PACS tax inventory %s: %d features, %d distinct parcels, %d without an id",
                 county, offset, len(distinct), blank)
    return distinct


def _settled_row(answer, gate) -> dict:
    """The column values one settled answer writes."""
    from src.scrapers.enrichment.pacs_tax import FOUND

    row = {"outcome": answer.outcome, "as_of": None, "amount": None, "oldest": None,
           "years": None, "owner": None, "situs": None, "mailing": None, "suppression": None}
    if answer.outcome != FOUND:
        return row
    row.update(as_of=answer.as_of, amount=answer.delinquent_amount,
               oldest=answer.oldest_delinquent_year,
               years=json.dumps({str(s.year): str(s.amount_due) for s in answer.past_due}))
    # Personal fields only with an owner: an ownerless row could neither be delivered
    # (the scraper holds it) nor be found by erasure, which keys on the owner.
    if answer.delinquent_amount > 0 and answer.owner_name:
        row.update(owner=answer.owner_name, situs=answer.property_address,
                   mailing=answer.mailing_address)
        row.update(gate(row))
    return row


def _privacy_gate(db, county: str):
    """A row filter: withholds the owner/addresses a privacy rule covers."""
    from src.privacy.authorize import ROW_COLUMNS, authorize, load_rule_snapshot

    with db.begin_nested():
        snapshot = load_rule_snapshot(db)

    def gate(row: dict) -> dict:
        if not snapshot.by_anchor or not row["owner"]:
            return {}
        lead = dict.fromkeys(ROW_COLUMNS) | {"parcel_id": row["parcel"], "party_name": row["owner"]}
        decision = authorize(snapshot, lead, record_type="tax_delinquent", county=county,
                             state=_STATE)
        if decision.deliverable:
            return {}
        return {"owner": None, "situs": None, "mailing": None,
                "suppression": decision.suppression_ids[0]}

    return gate


def crawl_county(county: str, db, *, fetch, resolve, now: datetime | None = None) -> dict:
    """One run for one county. ``fetch`` reads the statewide layer, ``resolve`` is
    pacs_parcel.resolve_tax; both injected so the run is testable without the network."""
    from src.scrapers.enrichment.pacs_parcel import SOURCE_UNAVAILABLE

    now = now or datetime.now(UTC)
    summary = {"county": county, "inventory_added": 0, "asked": 0, "settled": 0, "owing": 0}
    from src.scrapers.enrichment.source_health import get_source_state, mark_source_healthy

    last = (get_source_state(db, INVENTORY_KEY(county)) or {}).get("last_success_at")
    if last is None or now - last > _INVENTORY_EVERY:
        ids = statewide_parcel_ids(county, fetch)
        summary["inventory_added"] = db.execute(
            _INSERT, {"county": county, "state": _STATE, "ids": ids}).rowcount or 0
        db.commit()
        mark_source_healthy(db, INVENTORY_KEY(county))  # refreshed: next one in a week
    batch = [r.parcel_id for r in db.execute(
        _NEXT, {"county": county, "state": _STATE, "n": _BATCH})]
    if not batch:
        return summary
    answers = resolve(county, batch, time_budget_s=_BUDGET_S)
    gate = _privacy_gate(db, county)
    for parcel, answer in answers.items():
        if answer.outcome == SOURCE_UNAVAILABLE:
            continue  # never asked: keeps its place at the front of the queue
        summary["asked"] += 1
        key = {"county": county, "state": _STATE, "parcel": parcel, "now": now}
        stale = (answer.outcome == "found"
                 and abs(answer.as_of - now.date())
                 > _AS_OF_SLACK_BY_COUNTY.get(county, _AS_OF_SLACK))
        if answer.outcome in _SETTLED and not stale:
            row = _settled_row(answer, lambda r, p=parcel: gate({**r, "parcel": p}))
            db.execute(_SETTLE, {**key, **row})
            summary["settled"] += 1
            summary["owing"] += bool(row["amount"] and row["amount"] > 0)
        else:
            db.execute(_TOUCH, key)  # asked, no answer (ambiguous, unparsed, failed, stale)
    db.commit()
    return summary


def silent_since(county: str, db, now: datetime) -> datetime | None:
    """Since when ``county``'s crawl has learned nothing, if longer than _SILENT_AFTER.

    Measured from the last settled parcel, or for a county that never settled one (a
    portal broken from day one) from when its inventory was loaded; None before that."""
    from src.scrapers.enrichment.source_health import get_source_state

    last = db.execute(_LAST_SETTLED, {"county": county, "state": _STATE}).scalar()
    if last is None:
        last = (get_source_state(db, INVENTORY_KEY(county)) or {}).get("last_success_at")
    return last if last is not None and now - last > _SILENT_AFTER else None


# What a settled answer is HERE: a mismatch page proves nothing about the requested
# parcel, so it neither replaces the last good row nor counts as coverage.
_SETTLED = frozenset({"found", "parcel_not_found"})


@app.task(name="src.workers.pacs_tax_crawler.crawl_pacs_tax")
def crawl_pacs_tax() -> dict:
    """Beat entry: one bounded run per county; never raises for a source problem."""
    from src.db.session import system_sync_session
    from src.scrapers.enrichment import pacs_parcel, taxsifter
    from src.utils.safe_http import safe_get

    def fetch(url: str) -> dict:
        resp = safe_get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    resolvers = {"pacs": pacs_parcel.resolve_tax, "taxsifter": taxsifter.resolve_tax}
    results = []
    for county in COUNTIES:
        try:
            with system_sync_session() as db:
                summary = crawl_county(county, db, fetch=fetch,
                                       resolve=resolvers[_TAX_SOURCE[county]])
        except Exception as exc:  # noqa: BLE001 — one county must not stop the others
            _logger.error("PACS tax crawl %s failed: %s", county, str(exc)[:200])
            summary = {"county": county, "error": type(exc).__name__}
        from src.workers.ops_alerts import send_ops_alert

        if summary.get("asked") and not summary.get("settled"):
            send_ops_alert(
                kind="pacs_tax_unsettled", key=f"pacs_tax_unsettled:{county}",
                subject=f"PACS tax crawl settled nothing ({county})",
                body=f"{summary['asked']} parcels asked, 0 answered. The PACS layout or "
                     "the tax table may have changed.",
            )
        try:
            with system_sync_session() as db:
                quiet = silent_since(county, db, datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001 — the heartbeat must not stop the crawl
            _logger.error("PACS tax heartbeat %s unreadable: %s", county, type(exc).__name__)
            quiet = None
        if quiet is not None:
            send_ops_alert(
                kind="pacs_tax_silent", key=f"pacs_tax_silent:{county}",
                subject=f"PACS tax crawl has settled nothing for {_SILENT_AFTER} ({county})",
                body=f"No parcel answered since {quiet.isoformat()}. Check the county "
                     "portal (year selector, layout, a block) and the PACS lease.",
            )
        _logger.info("PACS tax crawl: %s", summary)  # counts only, no parcels or names
        results.append(summary)
    return {"counties": results}
