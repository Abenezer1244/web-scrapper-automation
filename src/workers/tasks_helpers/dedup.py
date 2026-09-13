"""Cross-list overlap + structured-tax-field helpers, extracted from tasks.py.

Holds the property-membership rollup, the results.property_key stamp, and the
source-gated tax-field extraction. Moved verbatim — behavior is byte-identical
to the originals in tasks.py.
"""

import time
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError

from src.utils.logger import setup_logger
from src.workers.property_identity import compute_property_key as _compute_property_key
from src.workers.property_identity import legacy_strong_signature

_logger = setup_logger("worker.task")


def _upsert_property_membership(
    db, rows, user_id: str, record_type: str, county: str | None, state: str | None
) -> int:
    """Phase 1: roll up strong-identity property sightings for cross-list overlap.

    `rows` = post-enrichment Result objects (only .parcel_id / .property_address
    are read). Pre-aggregates by property_key in Python so a single multi-row
    INSERT never hits the same conflict key twice ("cannot affect row a second
    time"). Deadlock-ordered by property_key; retried on serialization/deadlock.
    Returns the number of distinct strong properties upserted.

    Advisory only: sighting_count is not idempotent across job re-runs. Failures
    are caller-handled — this never participates in billing.
    """
    agg: dict[str, dict] = {}
    for res in rows:
        # County/state-scoped overlap key (2026-06-12) — config context required.
        key = _compute_property_key(res.parcel_id, res.property_address, county, state)
        if not key:
            continue
        cur = agg.get(key)
        if cur is None:
            agg[key] = {
                "parcel_id": (res.parcel_id or None),
                "property_address": (res.property_address or None),
                "count": 1,
            }
        else:
            cur["count"] += 1
            cur["parcel_id"] = cur["parcel_id"] or res.parcel_id
            cur["property_address"] = cur["property_address"] or res.property_address
    if not agg:
        return 0

    items = sorted(agg.items())  # deterministic lock order (deadlock guard)
    for i in range(0, len(items), 500):
        chunk = items[i:i + 500]
        values_sql = ",".join(
            f"(:uid_{k}, :rt_{k}, :pk_{k}, :pid_{k}, :addr_{k}, :cnt_{k}, NOW(), NOW())"
            for k in range(len(chunk))
        )
        params: dict = {}
        for k, (key, v) in enumerate(chunk):
            params[f"uid_{k}"] = user_id
            params[f"rt_{k}"] = record_type
            params[f"pk_{k}"] = key
            params[f"pid_{k}"] = (v["parcel_id"] or None)
            params[f"addr_{k}"] = (v["property_address"] or None)
            params[f"cnt_{k}"] = v["count"]
        stmt = sa_text(f"""
            INSERT INTO property_list_membership
                (user_id, record_type, property_key, parcel_id,
                 property_address, sighting_count, first_seen_at, last_seen_at)
            VALUES {values_sql}
            ON CONFLICT (user_id, record_type, property_key) DO UPDATE SET
                sighting_count   = property_list_membership.sighting_count + EXCLUDED.sighting_count,
                first_seen_at    = LEAST(property_list_membership.first_seen_at, EXCLUDED.first_seen_at),
                last_seen_at     = GREATEST(property_list_membership.last_seen_at, EXCLUDED.last_seen_at),
                parcel_id        = COALESCE(property_list_membership.parcel_id, EXCLUDED.parcel_id),
                property_address = COALESCE(property_list_membership.property_address, EXCLUDED.property_address)
        """)
        for attempt in range(3):
            try:
                db.execute(stmt, params)
                db.commit()
                break
            except OperationalError as exc:
                db.rollback()
                # psycopg2 (this stack) exposes SQLSTATE as .pgcode, NOT .sqlstate.
                pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
                if pgcode not in ("40001", "40P01") or attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
    return len(agg)


def _write_result_property_keys(
    db, rows, user_id: str, county: str | None, state: str | None
) -> tuple[int, int]:
    """Phase 3: stamp results.property_key on post-enrichment rows.

    property_key is the SAME strong-identity key membership stores
    (compute_property_key) — it lets the combine/overlap export join overlap
    property_keys back to full Result rows. Computed here, in the same
    post-enrichment spot as the membership rollup, so enrichment-resolved
    parcels/addresses are reflected.

    `rows` = post-enrichment Result objects (only .id / .parcel_id /
    .property_address are read). We do NOT mutate the ORM objects — an
    attribute-set would make the shared session dirty and a stray autoflush
    could push writes before the membership block commits, or poison the
    session on error (Codex review). Instead: an explicit bulk UPDATE by id in
    its OWN transaction, idempotent via `property_key IS NULL` (never clobbers a
    value, safe on task retry). Returns (updated_count, weak_skipped_count).

    Failure-isolated by the caller (like membership): on hard failure we never
    fail an already-delivered job; scripts/backfill_result_property_key.py heals.
    """
    pairs: list[tuple[str, str]] = []
    weak = 0
    for res in rows:
        # County/state-scoped overlap key (2026-06-12) — config context required.
        key = _compute_property_key(res.parcel_id, res.property_address, county, state)
        if not key:
            weak += 1
            continue
        pairs.append((str(res.id), key))
    if not pairs:
        return (0, weak)

    updated = 0
    for i in range(0, len(pairs), 500):
        chunk = pairs[i:i + 500]
        values_sql = ",".join(f"(:id_{k}, :pk_{k})" for k in range(len(chunk)))
        params: dict = {"uid": user_id}
        for k, (rid, key) in enumerate(chunk):
            params[f"id_{k}"] = rid
            params[f"pk_{k}"] = key
        # UPDATE ... FROM (VALUES ...) — one statement per chunk (no N+1).
        # data.id::uuid casts the bound text to the column type. The
        # property_key IS NULL guard makes this idempotent on re-run.
        stmt = sa_text(f"""
            UPDATE results
            SET property_key = data.pk
            FROM (VALUES {values_sql}) AS data(id, pk)
            WHERE results.id = data.id::uuid
              AND results.user_id = :uid
              AND results.property_key IS NULL
        """)
        res_proxy = db.execute(stmt, params)
        db.commit()
        updated += res_proxy.rowcount or 0
    return (updated, weak)


# Scrapers whose tax_delinquent rows carry trustworthy structured
# delinquent_amount + bill_year (the only sources _extract_tax_fields trusts).
# Adding a county = add its exact source string here AFTER confirming the scraper
# emits a real bill year + a clean owed amount. NEVER widen this to a generic
# "if the keys exist" check — that would mis-populate any scraper reusing the
# key names with a different meaning (Codex).
_TRUSTED_TAX_SOURCES = frozenset({
    "king_county_delinquent_taxes",        # King — Socrata API
    "snohomish_county_delinquent_taxes",   # Snohomish — Treasurer bulk Current Tax List
})


def _extract_tax_fields(
    enrichment_data, record_type: str
) -> tuple[Decimal | None, int | None]:
    """Phase 4: SOURCE-GATED structured tax fields for amount/age filtering.

    Returns (delinquent_amount, bill_year). ONLY scrapers in
    ``_TRUSTED_TAX_SOURCES`` carry trustworthy structured data, so anything else
    returns (None, None) and never matches a tax filter — a generic "if the keys
    exist" extraction would mis-populate any future scraper that reuses those key
    names with a different meaning (Codex). Values are coerced + bounded so a
    malformed scrape can't poison the filter columns. Raw enrichment_data is
    untouched.
    """
    if record_type != "tax_delinquent" or not isinstance(enrichment_data, dict):
        return (None, None)
    if enrichment_data.get("source") not in _TRUSTED_TAX_SOURCES:
        return (None, None)

    amount: Decimal | None = None
    raw_amt = enrichment_data.get("delinquent_amount")
    if raw_amt is not None:
        try:
            # Decimal(str(...)) — never Decimal(float) (binary-float drift).
            d = Decimal(str(raw_amt)).quantize(Decimal("0.01"))
            if d.is_finite() and Decimal("0") <= d <= Decimal("99999999.99"):
                amount = d
        except (InvalidOperation, ValueError, TypeError):
            amount = None

    year: int | None = None
    raw_year = enrichment_data.get("bill_year")
    if raw_year not in (None, ""):
        try:
            y = int(str(raw_year).strip())
            if 1900 <= y <= datetime.now(UTC).year + 1:
                year = y
        except (ValueError, TypeError):
            year = None

    return (amount, year)


class TaxDelinquentInvariantError(RuntimeError):
    """A ``tax_delinquent`` job produced a record that violates the product
    invariant: either its source is not a qualified tax source, or it is missing
    the defining structured fields (delinquent_amount + bill_year).

    Raised so the job is marked FAILED rather than persisting recorder docs
    (deeds, etc.) as tax leads — the Clark 2026-04 incident, where an immature
    scraper wrote 1,968 DEED rows as ``tax_delinquent`` with no amount/year.
    """


def validate_tax_delinquent_records(records, record_type: str) -> None:
    """Enforce the ``tax_delinquent`` product invariant BEFORE any DB insert.

    No-op for every non-tax record type. For ``tax_delinquent``, every record
    MUST (1) come from a source in ``_TRUSTED_TAX_SOURCES`` — the curated,
    doc-commented registry that IS the "documented exception" mechanism (adding a
    tax county = add its source id there) — and (2) yield both a non-null
    ``delinquent_amount`` and ``bill_year`` via ``_extract_tax_fields`` (same
    coercion + bounds the persist path uses, so "has the keys" is not enough).

    Raises ``TaxDelinquentInvariantError`` on the FIRST violation. Callers run
    this over the COMPLETE record set before the batched insert loop, so a
    violation fails the whole job atomically — never a partially-committed batch
    (Codex: validate before insert; canary threshold is > 0, not a percentage,
    because a percentage threshold invites the very regression this prevents).
    """
    if record_type != "tax_delinquent":
        return
    for idx, rec in enumerate(records):
        enrichment = getattr(rec, "enrichment_data", None)
        enrichment = enrichment if isinstance(enrichment, dict) else {}
        source = enrichment.get("source")
        parcel = getattr(rec, "parcel_id", None)
        if source not in _TRUSTED_TAX_SOURCES:
            raise TaxDelinquentInvariantError(
                f"tax_delinquent record #{idx} from untrusted source {source!r} "
                f"(parcel={parcel!r}). Only {sorted(_TRUSTED_TAX_SOURCES)} are "
                f"qualified tax sources — a tax_delinquent job must never run an "
                f"unregistered connector. Add the source id to _TRUSTED_TAX_SOURCES "
                f"(with county-qualification sign-off + fixtures) before enabling it."
            )
        amount, year = _extract_tax_fields(enrichment, record_type)
        if amount is None or year is None:
            raise TaxDelinquentInvariantError(
                f"tax_delinquent record #{idx} from {source!r} is missing required "
                f"structured tax fields (delinquent_amount={amount!r}, "
                f"bill_year={year!r}; parcel={parcel!r}, "
                f"date={getattr(rec, 'date_recorded', None)!r}). A tax lead must "
                f"carry both an owed amount and a bill year."
            )


# ─── Plan-cap dedup-claim release ──────────────────────────────────────────────

def release_capped_dedup_claims(db, user_id: str, job_id: str, capped_ids: list[str]) -> int:
    """Hand back the dedup claims of rows the plan cap is NOT delivering.

    Keeping a claim for a lead that was never delivered makes it permanently
    unreachable: every later run drops it as "already delivered" while the user
    has never seen it once. So the cap releases what it excluded.

    The NOT EXISTS guard is the other half, and it is load-bearing in the
    opposite direction. Two rows in ONE job can share a dedup_hash (the same
    parcel|address filed twice). If the cap excludes one and SHIPS the other, an
    unguarded delete drops the claim anyway, so a lead that WAS delivered and
    billed no longer holds it, and the next run delivers and bills the same
    identity again.

    "Still needed" therefore means exactly the set that ships: non-duplicate, not
    capped, and address-actionable. The address half matters both ways: without
    it an address-less row (never exported, never billed) would pin the claim and
    suppress that lead forever. It is the same predicate the cap's own ranking
    uses to choose ``capped_ids``, so the two cannot drift apart.

    Lives here rather than inline in tasks.py so the tests can exercise the REAL
    statement. They used to hold a copy, which meant deleting the guard in
    production left every one of them green (Codex).

    Returns the number of claims released.
    """
    from src.api.lead_actionability import address_actionable_sql

    if not capped_ids:
        return 0
    result = db.execute(
        sa_text(
            'DELETE FROM delivered_records dr USING results r '
            'WHERE dr.user_id = CAST(:uid AS uuid) '
            '  AND dr.first_job_id = :jid '
            '  AND dr.dedup_hash = r.dedup_hash '
            '  AND r.id = ANY(CAST(:ids AS uuid[])) '
            '  AND r.user_id = CAST(:uid AS uuid) '
            '  AND r.dedup_hash IS NOT NULL '
            '  AND NOT EXISTS ( '
            '        SELECT 1 FROM results keep '
            '        WHERE keep.job_id = :jid '
            '          AND keep.user_id = CAST(:uid AS uuid) '
            '          AND keep.dedup_hash = dr.dedup_hash '
            '          AND keep.is_duplicate = false '
            '          AND NOT (keep.id = ANY(CAST(:ids AS uuid[]))) '
            '          AND {keep_rule} '
            '  )'.format(keep_rule=address_actionable_sql("keep"))
        ),
        {"uid": str(user_id), "jid": job_id, "ids": capped_ids},
    )
    return result.rowcount or 0


# ─── Same-run sibling collapse (all record types) ──────────────────────────────

# Every column the grouping, the ranking and the source-field merge read. ONE
# list so the collapse and the reconciliation cannot select different shapes and
# then rank the same rows differently.
_GROUP_COLUMNS = (
    "id, dedup_hash, parcel_id, property_address, mailing_address, "
    "party_name, date_recorded, heirs, legal_description, enrichment_data, "
    "auction_date"
)


def _usable(v) -> bool:
    """True when a value is a real, deliverable string rather than blank or the
    '(enrichment unavailable)' placeholder."""
    from src.api.lead_actionability import ADDRESS_PLACEHOLDER

    v = (v or "").strip()
    return bool(v) and v != ADDRESS_PLACEHOLDER


def survivor_sort_key(row: dict) -> tuple:
    """Ordering that decides which row of a property group survives. Lowest wins.

      1. ACTIONABLE first. A row whose only address is blank or the
         '(enrichment unavailable)' placeholder is not deliverable anywhere the
         customer looks, and keeping it would hide the usable sibling while the
         property stayed claimed (Codex P2).
      2. then most complete: mailing address, parcel, party name.
      3. then oldest by (date_recorded, id), so a re-run picks the same winner
         and the delivered set does not shrink on every pass.

    ONE function so the first collapse and the post-enrichment reconciliation
    cannot rank differently. A reconciliation that disagreed with the collapse
    would swap the survivor back and forth on every retry.

    It reads ONLY address/parcel/name/date/id. It deliberately does NOT read
    ``heirs``, ``legal_description`` or ``enrichment_data`` -- exactly the fields
    _merged_survivor_fields writes ONTO the winner. If a merged field ever became
    a ranking input, a retry would rank on values the previous pass merged in and
    the election would stop being idempotent (Codex). A test pins this.
    """
    return (
        not (_usable(row.get("property_address")) or _usable(row.get("mailing_address"))),
        not _usable(row.get("mailing_address")),
        not (row.get("parcel_id") or "").strip(),
        not (row.get("party_name") or "").strip(),
        (row.get("date_recorded") or ""),
        str(row.get("id")),
    )


def auction_survivor_sort_key(row: dict) -> tuple:
    """Ordering for trustee_sale groups: soonest auction wins, stable by id.

    Auction Leads keeps the most URGENT notice rather than the most complete row
    -- a product decision from 2026-07-03. Lives here beside survivor_sort_key so
    finalize_trustee_sale_job and the reconciliation read the same definition;
    they were separate before, and the reconciliation re-ranked trustee_sale
    groups by actionability, quietly replacing the soonest-auction survivor with
    a later one (Codex).
    """
    return (row.get("auction_date") or date.max, str(row.get("id")))


def sort_key_for(record_type):
    """The survivor ranking THIS record type collapses by.

    The reconciliation must re-order a group by the same rule that formed it. Any
    other key would not be re-electing the group's survivor, it would be
    overriding the collapse's product rule after the fact.
    """
    if (record_type or "").strip().lower() == "trustee_sale":
        return auction_survivor_sort_key
    return survivor_sort_key


# Record types whose ``heirs`` column holds a LIST of names. Everything else
# stores a single SECONDARY PARTY there -- divorce keeps the OTHER SPOUSE in it
# (src/scrapers/divorce.py) -- and unioning two filings' values would invent a
# multi-party string no source ever asserted. Two filings on one property can
# also REVERSE which party is primary, which is why the union additionally drops
# the survivor's own party_name (Codex). An unknown record_type falls back to
# fill-only, the conservative side.
HEIR_LIST_RECORD_TYPES: frozenset = frozenset({"probate"})


def _as_dict(value) -> dict:
    """enrichment_data as a dict. The column is JSON, and a raw text() SELECT can
    hand back a dict, a JSON string, or None depending on the driver path."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        import json

        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _merge_heirs(survivor: dict, members: list[dict], record_type) -> str | None:
    """Deduplicated union of the group's ``heirs``, or None when it does not apply.

    Returns None only when the union DOES NOT APPLY (not an heir-list record
    type); an applicable union that comes out empty returns "". The caller needs
    that distinction: treating an empty union as "does not apply" sent it down
    the fill-only path, which copied back the very name the exclusion below had
    just removed (Codex).

    Only for record types where heirs is a name LIST; every other type gets
    fill-only, handled by the caller. Names equal to the SURVIVOR's own
    party_name are dropped: on a reversed-party filing the sibling's heirs field
    holds the survivor's own party, and splicing it in would assert that the
    survivor is their own heir (Codex).
    """
    if (record_type or "").strip().lower() not in HEIR_LIST_RECORD_TYPES:
        return None
    own = (survivor.get("party_name") or "").strip().casefold()
    merged: list[str] = []
    seen: set = set()
    for member in [survivor] + [m for m in members if m is not survivor]:
        for name in (member.get("heirs") or "").split(","):
            name = name.strip()
            key = name.casefold()
            if not name or key in seen or (own and key == own):
                continue
            seen.add(key)
            merged.append(name)
    return ", ".join(merged)


def _merged_survivor_fields(survivor: dict, members: list[dict], record_type) -> dict:
    """Column updates carrying the group's source-only facts onto the winner.

    The collapse marked a loser and stopped, so anything the loser ALONE carried
    -- the only heir on a later filing, the only legal description -- left the
    per-job export with it. Ranking never looked at these fields, so nothing was
    weighing them when the winner was chosen (Codex P2).

    Deliberately narrow, an ALLOWLIST rather than a blacklist (Codex):

    - ``heirs``: union for heir-list record types (see _merge_heirs), otherwise
      fill-only.
    - ``legal_description``: FILL-ONLY, never overwritten. Two filings can carry
      two genuinely different legals, and concatenating them would present one
      invented string as the property's authoritative description.
    - ``lead_subtype`` inside enrichment_data: elected by the SAME priority order
      the combined export aggregates with, read from the one shared constant so
      the two cannot drift.
    - EVERY OTHER enrichment_data key is left alone. Copying keys individually
      across two filings manufactures an object no single source ever produced (a
      billed_amount from one filing beside a paid_amount from another), and the
      blob also carries per-ROW state such as the plan-cap exclusion key, which
      must never travel to a different row.

    Losers keep their own values -- nothing is deleted, so a later swap re-merges
    from the same set and lands on the same answer.
    """
    from src.utils.lead_export import probate_subtype_rank

    updates: dict = {}
    others = [m for m in members if m is not survivor]

    heirs = _merge_heirs(survivor, members, record_type)
    if heirs is not None:
        # An empty union CLEARS the column rather than leaving it: the only way
        # to get here with a value already present is that every name in it was
        # the survivor's own party, which is not an heir relationship.
        if heirs != (survivor.get("heirs") or ""):
            updates["heirs"] = heirs or None
    elif not (survivor.get("heirs") or "").strip():
        for member in others:
            candidate = (member.get("heirs") or "").strip()
            if candidate:
                updates["heirs"] = candidate
                break

    if not (survivor.get("legal_description") or "").strip():
        for member in others:
            candidate = (member.get("legal_description") or "").strip()
            if candidate:
                updates["legal_description"] = candidate
                break

    best = None
    best_rank = None
    for member in members:
        subtype = (_as_dict(member.get("enrichment_data")).get("lead_subtype") or "").strip()
        if not subtype:
            continue
        rank = probate_subtype_rank(subtype)
        if best_rank is None or rank < best_rank:
            best, best_rank = subtype, rank
    if best:
        current = _as_dict(survivor.get("enrichment_data"))
        if (current.get("lead_subtype") or "").strip() != best:
            current["lead_subtype"] = best
            updates["enrichment_data"] = current

    return updates


def _apply_survivor_merge(db, user_id, survivor_id, updates: dict) -> None:
    """Write _merged_survivor_fields' output. Does NOT commit; the caller's
    transaction owns the write, exactly like the flag updates beside it."""
    if not updates:
        return
    import json

    sets = []
    params = {"rid": str(survivor_id), "uid": str(user_id)}
    for column, value in updates.items():
        if column == "enrichment_data":
            sets.append("enrichment_data = CAST(:enrichment_data AS json)")
            params["enrichment_data"] = json.dumps(value)
        else:
            sets.append(f"{column} = :{column}")
            params[column] = value
    db.execute(
        sa_text(
            f"UPDATE results SET {', '.join(sets)} "
            "WHERE id = CAST(:rid AS uuid) AND user_id = CAST(:uid AS uuid)"
        ),
        params,
    )


def _repoint_claim_anchor(db, user_id, dedup_hash: str, survivor_id, loser_ids: list) -> int:
    """Point this hash's delivered_records claim at the row that SURVIVED.

    The cross-job dedup writes ``first_result_id`` from whichever row PostgreSQL
    reached first inside the batched ``INSERT ... ON CONFLICT DO NOTHING``; the
    collapse elects its survivor by actionability. Nothing coordinated the two,
    so the claim could name a row the collapse then flagged is_duplicate (Codex).

    That is not only the "claims naming a row that is itself flagged
    is_duplicate" invariant. ``_reuse_enrichment_for_duplicates`` joins
    ``results ro ON ro.id = dr.first_result_id`` and copies address plus settled
    skip-trace PII FROM that row, so an anchor left on a collapsed loser makes
    the reuse source a row the run deliberately suppressed.

    Only moves an anchor that is one of THIS group's losers, so a claim owned by
    an earlier run is never touched. Returns the number of claims repointed.
    """
    if not loser_ids:
        return 0
    result = db.execute(
        sa_text(
            "UPDATE delivered_records SET first_result_id = CAST(:sid AS uuid) "
            "WHERE user_id = CAST(:uid AS uuid) "
            "  AND dedup_hash = :hash "
            "  AND first_result_id = ANY(CAST(:losers AS uuid[]))"
        ),
        {
            "sid": str(survivor_id),
            "uid": str(user_id),
            "hash": dedup_hash,
            "losers": [str(i) for i in loser_ids],
        },
    )
    return result.rowcount or 0


def _collapse_loser_ids(rows: list[dict]) -> list:
    """Ids to mark duplicate so each PROPERTY keeps ONE row.

    Thin view over _collapse_groups, kept because the ids alone are what the
    is_duplicate write needs and what the collapse tests assert on.
    """
    return [
        loser.get("id")
        for _survivor, losers in _collapse_groups(rows)
        for loser in losers
    ]


def _collapse_groups(rows: list[dict]) -> list:
    """``(survivor, losers)`` per PROPERTY group. Pure, so the rule is
    unit-testable without a database.

    Only groups rows whose dedup_hash came from the STRONG branch. That branch is
    ``legacy_strong_signature(parcel_id, property_address)``; when it returns
    None the worker falls back to a ``NAME|DATE`` hash, which identifies a FILING
    rather than a property. Two addressless filings by the same party on the same
    date share that hash without being the same lead, and collapsing them would
    silently stop delivering one (Codex P1). The function itself is called here
    rather than reimplemented in SQL, so the rule cannot drift from the one that
    produced the hash.

    Survivors are ranked by survivor_sort_key -- see it for the order and for why
    the merged source-only fields are deliberately absent from it.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        # Require the strong signature of the row's CURRENT parcel + address to
        # EQUAL its stored dedup_hash. Checking only that a signature exists is
        # not enough (Codex P1): the hash was computed from the values at INSERT
        # time, and enrichment mutates property_address afterwards. On a watchdog
        # retry a row that hashed weakly as NAME|DATE, and has since had an
        # address filled in, would pass an existence check and then be grouped by
        # that weak hash -- collapsing two filings that are not the same property.
        #
        # Equality also covers insert-time truncation, and any other drift
        # between the hashed inputs and what is in the row now.
        #
        # It deliberately UNDER-collapses: a strong row whose address was later
        # rewritten no longer matches, so it is skipped and that property bills
        # twice, exactly as it does today. Billing one property twice is the
        # status quo; silently not delivering a lead is not.
        if legacy_strong_signature(
            row.get("parcel_id"), row.get("property_address")
        ) != row.get("dedup_hash"):
            continue
        groups[row.get("dedup_hash")].append(row)

    collapsed: list = []
    for grp in groups.values():
        if len(grp) <= 1:
            continue
        ordered = sorted(grp, key=survivor_sort_key)
        collapsed.append((ordered[0], ordered[1:]))
    return collapsed


def collapse_same_run_siblings(db, job_id: str, user_id, record_type=None) -> int:
    """Collapse rows in ONE job that share a PROPERTY, so it bills once.

    ``dedup_hash`` (parcel|address) is the app-wide BILLING key. The cross-job
    dedup only records that a hash was CLAIMED once; it leaves same-JOB rows
    sharing a hash all ``is_duplicate=false``, and billing counts ROWS. So a run
    that scraped two filings on one property charged for both.

    trustee_sale has collapsed its own siblings since 2026-07-03. Nothing else
    did, and an audit on 2026-09-08 found 8 completed jobs across probate and
    pre_foreclosure that had charged 50 records for properties already billed in
    the same run, including a 122-record job that covered 120 properties.

    Losers are marked ``duplicate_reason='same_run'`` pointing at this job, so
    the results page says "combined" rather than "already delivered" -- they were
    never delivered before, they are being seen for the first time.

    Returns the number NEWLY collapsed, for the caller's dup_count. Does NOT
    commit; the caller's transaction owns the write.

    NOTE for the caller: a collapsed sibling leaves the plan cap's view, because
    the cap ranks non-duplicates only. If the SURVIVOR is later capped, its
    siblings must inherit that exclusion, or a property nobody paid for is still
    delivered through the duplicate-keeping paths (lists, batch combine). The cap
    block in tasks.py propagates it; see DELIVERY_EXCLUDED_KEY there.
    """
    rows = db.execute(
        sa_text(
            f"SELECT {_GROUP_COLUMNS} "
            "FROM results "
            "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
            "  AND dedup_hash IS NOT NULL AND is_duplicate = false"
        ),
        {"jid": job_id, "uid": str(user_id)},
    ).fetchall()

    groups = _collapse_groups([dict(r._mapping) for r in rows])
    if not groups:
        return 0

    losers = [loser.get("id") for _s, group_losers in groups for loser in group_losers]
    result = db.execute(
        sa_text(
            "UPDATE results SET is_duplicate = true, "
            "  duplicate_reason = 'same_run', "
            "  duplicate_source_job_id = :jid, "
            "  duplicate_source_at = NULL "
            "WHERE id = ANY(CAST(:ids AS uuid[])) "
            "  AND user_id = CAST(:uid AS uuid)"
        ),
        {"ids": [str(i) for i in losers], "jid": job_id, "uid": str(user_id)},
    )

    for survivor, group_losers in groups:
        _apply_survivor_merge(
            db,
            user_id,
            survivor.get("id"),
            _merged_survivor_fields(survivor, [survivor] + group_losers, record_type),
        )
        _repoint_claim_anchor(
            db,
            user_id,
            survivor.get("dedup_hash"),
            survivor.get("id"),
            [loser.get("id") for loser in group_losers],
        )

    return result.rowcount or 0


def reconcile_same_run_survivors(db, job_id: str, user_id, record_type=None) -> int:
    """Re-elect each same-run group's survivor once enrichment has settled.

    collapse_same_run_siblings runs BEFORE inline enrichment, because the export
    and the billing count both need the collapse already applied. That means it
    ranks on the addresses as SCRAPED. Two addressless rows sharing a strong
    parcel hash are therefore separated by completeness alone -- and then the
    Pierce legal-description repair can fill an address onto the row that lost.
    The survivor stays undeliverable, the one actionable row is flagged
    is_duplicate, and no retry ever revisits it because the collapse SELECT reads
    ``is_duplicate = false`` (Codex P2).

    So this runs AFTER enrichment and re-ranks what the collapse already grouped.

    Membership is READ, never recomputed. Passing the rows back through
    _collapse_groups would be wrong: enrichment rewrites property_address, so a
    member can stop satisfying that function's hash-equality admission test and
    simply vanish from its result -- and "not returned as a loser" is not the
    same as "elected winner" (Codex). Grouping already happened under equality at
    collapse time; this pass only re-orders the members it finds. Membership by
    dedup_hash is stable because the hash is computed once at INSERT and never
    recomputed.

    A group must have EXACTLY ONE non-duplicate member. With k of them the
    duplicate count would move by k-1 and billing with it, so a malformed group
    is logged and skipped rather than "repaired" (Codex).

    Rows are locked FOR UPDATE so two elections cannot race.

    Returns the number of groups whose survivor actually changed. The per-group
    duplicate count is invariant by construction -- exactly one survivor before,
    exactly one after -- so the caller's dup_count and the charge do not move.
    """
    rows = db.execute(
        sa_text(
            f"SELECT {_GROUP_COLUMNS}, is_duplicate "
            "FROM results "
            "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
            "  AND dedup_hash IS NOT NULL "
            "  AND (is_duplicate = false OR (duplicate_reason = 'same_run' "
            "       AND duplicate_source_job_id = :jid)) "
            "ORDER BY id "
            "FOR UPDATE"
        ),
        {"jid": job_id, "uid": str(user_id)},
    ).fetchall()

    groups: dict = defaultdict(list)
    for row in rows:
        groups[row.dedup_hash].append(dict(row._mapping))

    swapped = 0
    for dedup_hash, members in groups.items():
        collapsed = [m for m in members if m.get("is_duplicate")]
        if not collapsed:
            continue  # nothing was collapsed here; the collapse pass owns it
        standing = [m for m in members if not m.get("is_duplicate")]
        if len(standing) != 1:
            _logger.warning(
                "Job %s: same-run group %s has %d standing rows (expected 1) — "
                "skipped, not reconciled",
                job_id, str(dedup_hash)[:12], len(standing),
            )
            continue

        current = standing[0]
        elected = sorted(members, key=sort_key_for(record_type))[0]

        if elected.get("id") != current.get("id"):
            db.execute(
                sa_text(
                    "UPDATE results SET is_duplicate = false, "
                    "  duplicate_reason = NULL, "
                    "  duplicate_source_job_id = NULL, "
                    "  duplicate_source_at = NULL "
                    "WHERE id = CAST(:rid AS uuid) AND user_id = CAST(:uid AS uuid)"
                ),
                {"rid": str(elected.get("id")), "uid": str(user_id)},
            )
            db.execute(
                sa_text(
                    "UPDATE results SET is_duplicate = true, "
                    "  duplicate_reason = 'same_run', "
                    "  duplicate_source_job_id = :jid, "
                    "  duplicate_source_at = NULL "
                    "WHERE id = CAST(:rid AS uuid) AND user_id = CAST(:uid AS uuid)"
                ),
                {"rid": str(current.get("id")), "jid": job_id, "uid": str(user_id)},
            )
            swapped += 1

        _apply_survivor_merge(
            db,
            user_id,
            elected.get("id"),
            _merged_survivor_fields(elected, members, record_type),
        )
        _repoint_claim_anchor(
            db,
            user_id,
            dedup_hash,
            elected.get("id"),
            [m.get("id") for m in members if m.get("id") != elected.get("id")],
        )

    return swapped


# ─── Claim transfer: a claim nobody was ever delivered ─────────────────────────

# When "a row with no property AND no mailing address is not a lead" started
# deciding BILLING: the merge of #191. Jobs billed before it charged for and
# exported address-less rows, so an address-less claim anchor from then WAS a
# delivery and must keep suppressing. Production shows no job holding such rows
# was billed between 2026-09-02 09:38 (old rule) and 2026-09-04 09:32 (new
# rule, billed 0), so the merge instant is exact enough on both sides.
NO_ADDRESS_NOT_BILLED_SINCE = datetime(2026, 9, 3, 12, 5, 28, tzinfo=UTC)

# From when a NULL jobs.billing_applied_at proves a job never charged. Migration
# 063 added the stamp with PR #59 (merged and deployed 2026-06-18 01:35 UTC);
# an older job could charge records_used and still end failed or cancelled with
# no stamp. Compared with jobs.created_at (a job's lifetime; started_at resets
# on every watchdog retry). The day of margin only makes a few newer jobs keep
# suppressing, which is the safe direction.
BILLING_STAMP_RELIABLE_SINCE = datetime(2026, 6, 19, tzinfo=UTC)


def transfer_undelivered_claims(db, job_id: str, user_id, record_type=None) -> int:
    """Hand a claim to THIS run when the run holding it never delivered the lead.

    The claim is written for every row with a hash before enrichment decides
    whether the row has an address. An address-less row is not a lead (not
    listed, exported or billed) but it keeps the claim, so a later run that finds
    the same property WITH an address flags it "already delivered" and hides it.
    Production held 419 such rows, and nothing ever lets them through: the claim
    has no expiry.

    Releasing the claim at the end of the first run was rejected (Codex): a later
    mailing backfill can give that old row an address, the old run's live
    download would then ship it, and the next run would deliver and bill it again.
    Moving the claim instead keeps exactly one holder at every instant. The old
    anchor is marked 'superseded', so it stays hidden however its address changes.

    Runs after enrichment and the survivor re-election (actionability is known)
    and BEFORE the plan cap, so the cap ranks the promoted row and billing,
    export and skip trace all see it. Each hash is its own transaction; per hash,
    every condition is re-evaluated after the holding run's rows and the claim
    are locked, so two runs transferring the same claim cannot both win (the
    second finds the claim already moved and stops).

    A claim is transferred only when all of these hold:
      - its hash is STRONG, proven from the parcel/address stored ON THE CLAIM at
        claim time (results.property_address is rewritten by enrichment, so the
        row cannot prove it). A weak NAME|DATE hash identifies a filing, and
        moving it could hide an unrelated lead.
      - its anchor row still exists for this user on another job. A NULL anchor
        means the source run was purged; delivery cannot be disproved, so the
        claim keeps suppressing.
      - the anchor's run delivered nothing chargeable for it: failed/cancelled,
        never billed and created after BILLING_STAMP_RELIABLE_SINCE (no export
        exists, so its rows' addresses do not matter), or done, billed after
        NO_ADDRESS_NOT_BILLED_SINCE, with no unflagged actionable row for the
        property (none with an address and not excluded by the plan cap).
        A run still in flight is never robbed.

    The anchor and every unflagged row of the old run for the property become
    'superseded'.

    Returns the number of claims transferred (each un-flags exactly one row).
    """
    from src.api.lead_actionability import actionable_sql

    uid = str(user_id)
    candidates = db.execute(
        sa_text(
            f"SELECT {_GROUP_COLUMNS} FROM results "
            "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) "
            "  AND dedup_hash IS NOT NULL AND is_duplicate = true "
            "  AND duplicate_reason = 'prior_run' "
            f"  AND {actionable_sql('results')}"
        ),
        {"jid": job_id, "uid": uid},
    ).fetchall()
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        groups[row.dedup_hash].append(dict(row._mapping))

    transferred = 0
    # Sorted: a stable lock order across concurrent runs of the same user.
    for dedup_hash in sorted(groups):
        try:
            if _transfer_one_claim(db, job_id, uid, user_id, dedup_hash,
                                   groups[dedup_hash], record_type):
                transferred += 1
        except Exception as exc:
            # One hash's failure must not hide the claims already moved: each
            # commits on its own, and the caller subtracts the returned count
            # from the run's duplicate total. Rolled back, logged with the job and
            # a hash prefix, and the remaining hashes still get their chance.
            db.rollback()
            _logger.warning(
                "Job %s: claim transfer failed for hash %s: %s",
                job_id, dedup_hash[:12], str(exc)[:160],
            )
    return transferred


def _transfer_one_claim(db, job_id, uid, user_id, dedup_hash, members, record_type) -> bool:
    """One hash of transfer_undelivered_claims, in its own transaction. Returns
    True when the claim moved.

    Lock order is result rows, then the claim: reconciliation, the same-run
    collapse and the plan cap all take them in that order, so the reverse would
    deadlock against them (Codex review round 7). The claim is first read without
    a lock only to learn which run holds it; every condition is then evaluated
    after both locks, and a claim that moved in between is left alone.

    A claim that no longer exists at all (the stranded-claim sweep released it
    after this run's dedup step flagged the row) is taken by this run, exactly as
    its dedup step would have taken it (Codex review round 9).
    """
    from src.api.lead_actionability import actionable_sql

    peek = db.execute(
        sa_text(
            "SELECT d.first_result_id, a.job_id "
            "FROM delivered_records d "
            "LEFT JOIN results a ON a.id = d.first_result_id AND a.user_id = d.user_id "
            "WHERE d.user_id = CAST(:uid AS uuid) AND d.dedup_hash = :hash"
        ),
        {"uid": uid, "hash": dedup_hash},
    ).first()
    # Its anchor row is gone (a purged source run cannot disprove delivery), or
    # this run already holds it.
    if peek is not None and (peek.job_id is None or str(peek.job_id) == str(job_id)):
        db.rollback()
        return False
    old_job = str(peek.job_id) if peek is not None else None

    # Every row of the holding run for this property, not only the anchor: before
    # the same-run collapse existed one run could hold two unflagged rows for a
    # property, and the one the claim names may not be the one it delivered.
    # Ordered by id with this run's rows, so concurrent transfers lock alike.
    locked = db.execute(
        sa_text(
            "SELECT r.id, r.job_id, r.is_duplicate, r.duplicate_reason, "
            f"  {actionable_sql('r')} AS actionable "
            "FROM results r "
            "WHERE r.user_id = CAST(:uid AS uuid) "
            "  AND (r.id = ANY(CAST(:mids AS uuid[])) "
            "       OR (r.job_id = CAST(:ojid AS uuid) AND r.dedup_hash = :hash)) "
            "ORDER BY r.id FOR UPDATE"
        ),
        {"uid": uid, "hash": dedup_hash, "ojid": old_job,
         "mids": [str(m.get("id")) for m in members]},
    ).fetchall()
    claim = db.execute(
        sa_text(
            "SELECT id, first_result_id, parcel_id, property_address "
            "FROM delivered_records "
            "WHERE user_id = CAST(:uid AS uuid) AND dedup_hash = :hash "
            "FOR UPDATE"
        ),
        {"uid": uid, "hash": dedup_hash},
    ).first()
    if claim is None:
        return _take_unheld_claim(db, job_id, uid, user_id, dedup_hash, members,
                                  record_type, locked)
    old_rows = [r for r in locked if str(r.job_id) == old_job]
    if (
        peek is None
        or str(claim.first_result_id) != str(peek.first_result_id)
        or not any(str(r.id) == str(claim.first_result_id) for r in old_rows)
        or legacy_strong_signature(claim.parcel_id, claim.property_address) != dedup_hash
    ):
        db.rollback()
        return False
    holder = db.execute(
        sa_text(
            "SELECT status, billing_applied_at, created_at FROM jobs "
            "WHERE id = CAST(:ojid AS uuid) AND user_id = CAST(:uid AS uuid)"
        ),
        {"ojid": old_job, "uid": uid},
    ).first()
    # The worker's own export and billing predicate. The live download adds view
    # filters on top, but never ships a row outside this set.
    delivered_a_row = any(not r.is_duplicate and r.actionable for r in old_rows)
    if holder is None or not (
        # A job can bill and only later be marked failed/cancelled (a cancel
        # racing the done-CAS), so a terminal status alone does not prove
        # "charged nothing"; the sweep and the cancellation release use the same
        # rule. Such a run never wrote an export (export_key comes only from the
        # done-CAS), so whether its rows have an address is irrelevant: nobody
        # received them (Codex review round 6). Before billing was stamped a
        # NULL stamp proves nothing (Codex review round 7).
        (
            holder.status in ("failed", "cancelled")
            and holder.billing_applied_at is None
            and holder.created_at >= BILLING_STAMP_RELIABLE_SINCE
        )
        or (
            # A done run delivered every unflagged actionable row it holds.
            holder.status == "done"
            and not delivered_a_row
            and holder.billing_applied_at is not None
            and holder.billing_applied_at >= NO_ADDRESS_NOT_BILLED_SINCE
        )
    ):
        db.rollback()
        return False

    elected = sorted(members, key=sort_key_for(record_type))[0]
    params = {"uid": uid, "jid": job_id, "eid": str(elected.get("id"))}

    db.execute(
        sa_text(
            "UPDATE delivered_records SET first_result_id = CAST(:eid AS uuid), "
            "  first_job_id = :jid, first_delivered_at = clock_timestamp() "
            "WHERE id = :cid AND user_id = CAST(:uid AS uuid)"
        ),
        {**params, "cid": str(claim.id)},
    )
    _promote_elected(db, job_id, uid, user_id, elected, members, record_type)
    # The anchor and every unflagged row of the old run for this property: none
    # was delivered, and a later mailing backfill on any of them would otherwise
    # put the property in the old run's live download next to the promoted row.
    hidden = sorted({str(claim.first_result_id)}
                    | {str(r.id) for r in old_rows if not r.is_duplicate})
    db.execute(
        sa_text(
            "UPDATE results SET is_duplicate = true, duplicate_reason = 'superseded', "
            "  duplicate_source_job_id = :jid, duplicate_source_at = clock_timestamp() "
            "WHERE id = ANY(CAST(:hidden AS uuid[])) AND user_id = CAST(:uid AS uuid)"
        ),
        {**params, "hidden": hidden},
    )
    db.commit()
    return True


def _take_unheld_claim(db, job_id, uid, user_id, dedup_hash, members, record_type,
                       locked) -> bool:
    """No claim exists for a hash this run was told was already delivered: it was
    released (a failed or cancelled run that never billed) after the dedup step
    flagged this run's rows. Take it the way the dedup step does, so a one-off run
    does not silently lose the lead. The caller holds the row locks. ON CONFLICT
    DO NOTHING keeps one winner if another run claims the hash at the same time.
    """
    still_flagged = {
        str(r.id) for r in locked
        if r.is_duplicate and r.duplicate_reason == "prior_run"
    }
    members = [m for m in members if str(m.get("id")) in still_flagged]
    if not members:
        db.rollback()
        return False
    elected = sorted(members, key=sort_key_for(record_type))[0]
    won = db.execute(
        sa_text(
            "INSERT INTO delivered_records "
            "  (id, user_id, dedup_hash, first_result_id, first_job_id, "
            "   parcel_id, property_address, first_delivered_at) "
            "VALUES (CAST(:cid AS uuid), CAST(:uid AS uuid), :hash, CAST(:eid AS uuid), "
            "        :jid, :parcel, :address, clock_timestamp()) "
            "ON CONFLICT (user_id, dedup_hash) DO NOTHING RETURNING id"
        ),
        {"cid": str(uuid.uuid4()), "uid": uid, "hash": dedup_hash,
         "eid": str(elected.get("id")), "jid": job_id,
         "parcel": elected.get("parcel_id"), "address": elected.get("property_address")},
    ).first()
    if won is None:
        db.rollback()
        return False
    _promote_elected(db, job_id, uid, user_id, elected, members, record_type)
    db.commit()
    return True


def _promote_elected(db, job_id, uid, user_id, elected, members, record_type) -> None:
    """Un-flag the elected row; its same-run siblings become 'combined' under it."""
    others = [str(m.get("id")) for m in members if m is not elected]
    params = {"uid": uid, "jid": job_id, "eid": str(elected.get("id"))}
    db.execute(
        sa_text(
            "UPDATE results SET is_duplicate = false, duplicate_reason = NULL, "
            "  duplicate_source_job_id = NULL, duplicate_source_at = NULL "
            "WHERE id = CAST(:eid AS uuid) AND user_id = CAST(:uid AS uuid)"
        ),
        params,
    )
    if others:
        # Same property, same run: exactly what the collapse makes of
        # siblings, so they read "combined", not "already delivered".
        db.execute(
            sa_text(
                "UPDATE results SET duplicate_reason = 'same_run', "
                "  duplicate_source_job_id = :jid, duplicate_source_at = NULL "
                "WHERE id = ANY(CAST(:ids AS uuid[])) "
                "  AND user_id = CAST(:uid AS uuid)"
            ),
            {**params, "ids": others},
        )
        _apply_survivor_merge(
            db, user_id, elected.get("id"),
            _merged_survivor_fields(elected, members, record_type),
        )
