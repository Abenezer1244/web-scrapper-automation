"""Erase one deletion rule's subject from every store (design section 7, decision D3).

Discovery is here, in Python, because the HMAC keys are: each step scans one bounded
batch of one store, authorizes every row against a snapshot of THIS rule only, and hands
the matches to the database function ``privacy_erase_batch()`` (migration 135), which
erases them as the purge role. The caller commits after each step and stores the cursor
it returns, so a pass can stop anywhere and resume.

What counts as the subject's row: authorize() says WITHHELD by this rule (an exact
anchor AND an exact subject). REVIEW candidates (a name without the parcel, or a name
that cannot be parsed) stay withheld from delivery but are never erased: design section
5 says they never apply automatically, and erasure cannot be undone.

Discovery never relies on the privacy flag (results.privacy_suppression_id): a row first
flagged by an opt-out rule for the same person carries THAT rule's id, and a row the
enforcer has not reached yet carries none (Codex consult). Every row of every store is
scanned, flagged or not.

Stores, in order: results (every tenant), king_parcel_owners, nts_notices,
county_records. The function also reaches each matched result's skip-trace cache
entries (its subject hash, plus the keys of its pending lookups computed here),
lookups, Tracerfy links, dialer rows and exports.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, text

from src.config.constants import ALL_RECORD_TYPES
from src.db.models import Job, Result, ScraperConfig
from src.privacy.authorize import (
    ROW_COLUMNS,
    WITHHELD,
    RuleSnapshot,
    RuleSnapshotError,
    authorize,
    build_snapshot,
    load_rule_rows,
)
from src.scrapers.enrichment.skip_trace import pending_row_subject_key

STORES = ("results", "king", "nts", "county")
BATCH = 500
_MAX_CACHE_KEYS = 2000  # privacy_erase_batch's own bound
_NIL = str(uuid.UUID(int=0))

_CALL = text(
    "SELECT privacy_erase_batch(CAST(:rule AS uuid), CAST(:res AS uuid[]), "
    "CAST(:keys AS text[]), CAST(:king AS text[]), CAST(:nts AS uuid[]), "
    "CAST(:county AS uuid[]))"
)
_PENDING_SQL = text(
    "SELECT user_id, property_address, city, state, trace_type, first_name, last_name "
    "FROM pending_skip_trace_rows WHERE result_id = ANY(CAST(:ids AS uuid[]))"
)
_KING_SQL = text(
    "SELECT parcel_id, owner_name FROM king_parcel_owners "
    "WHERE outcome = 'found' AND owner_name IS NOT NULL AND parcel_id > :after "
    "ORDER BY parcel_id LIMIT :n"
)
# The grantor is a notice's only person field. A notice withheld by ANY rule already
# has none (132's ck_nts_notices_privacy_withheld: rule set => grantor NULL, inactive),
# so every notice that can still name the subject is one with a grantor.
_NTS_SQL = text(
    "SELECT id::text AS id, grantor, parcel, county, state FROM nts_notices "
    "WHERE grantor IS NOT NULL AND id > CAST(:after AS uuid) ORDER BY id LIMIT :n"
)
_COUNTY_SQL = text(
    "SELECT id::text AS id, county, state, party_name, heirs, parcel_id, enrichment_data "
    "FROM county_records WHERE id > CAST(:after AS uuid) ORDER BY id LIMIT :n"
)
# county_records carries no record type, so its name order is unknown: a row is the
# subject's if ANY order reads it as an exact anchor + subject match. Nothing writes
# the table any more (only the weekly retention purge deletes from it), so no source
# can be recorded for the rows left; they age out. A wrong-order false match needs the
# reversed name on the SAME anchor, and only deletes a row of this orphaned store.
_COUNTY_ORDERS = (None, *sorted(ALL_RECORD_TYPES))


@dataclass(frozen=True)
class ErasureRule:
    rule_id: str
    rule_row: str            # the ACTIVE privacy_suppressions row (results' flag target)
    snapshot: RuleSnapshot   # this rule only
    row_ids: frozenset[str]  # every active row id of the rule (one per key version)


@dataclass
class Step:
    counts: dict[str, int] = field(default_factory=dict)
    scanned: int = 0
    cursor: dict = field(default_factory=dict)
    done: bool = False


def load_erasure_rule(db, rule_id: str, rule_row: str) -> ErasureRule:
    """A snapshot of one rule. Raises RuleSnapshotError unless ``rule_row`` is one of
    its active rows (the database function re-checks that it is a deletion)."""
    rows = [r for r in load_rule_rows(db) if str(r["rule_id"]) == rule_id]
    snapshot = build_snapshot(rows)
    row_ids = frozenset(r.suppression_id for rules in snapshot.by_anchor.values()
                        for r in rules)
    if rule_row not in row_ids:
        raise RuleSnapshotError(f"rule {rule_id} is not active as {rule_row}")
    return ErasureRule(rule_id, rule_row, snapshot, row_ids)


def _is_subject(rule: ErasureRule, row, *, record_type, county, state) -> bool:
    decision = authorize(rule.snapshot, row, record_type=record_type, county=county,
                         state=state)
    return decision.outcome == WITHHELD and bool(rule.row_ids & set(decision.suppression_ids))


def _lead(**values) -> dict:
    return dict.fromkeys(ROW_COLUMNS) | values


def _scan_results(db, rule: ErasureRule, after: str | None) -> tuple[list, list[str], dict]:
    rows = db.execute(
        select(Result.id, *(getattr(Result, c) for c in ROW_COLUMNS),
               ScraperConfig.record_type, ScraperConfig.county, ScraperConfig.state)
        .join(Job, Job.id == Result.job_id)
        .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
        .where(Result.id > (after or _NIL))
        .order_by(Result.id)
        .limit(BATCH)
    ).mappings().all()
    matched = [str(m["id"]) for m in rows
               if _is_subject(rule, m, record_type=m["record_type"], county=m["county"],
                              state=m["state"])]
    keys: list[str] = []
    if matched:
        # Every lookup ever made for these leads, collected BEFORE the function blanks
        # the names it is keyed on (several per lead: normal and advanced traces).
        keys = sorted({pending_row_subject_key(p)
                       for p in db.execute(_PENDING_SQL, {"ids": matched}).all()})
        if len(keys) > _MAX_CACHE_KEYS:
            raise RuntimeError(f"{len(keys)} lookup keys in one batch exceeds the bound")
    last = str(rows[-1]["id"]) if rows else None
    return rows, matched, {"res": matched, "keys": keys, "_last": last}


def _scan_king(db, rule: ErasureRule, after: str | None):
    rows = db.execute(_KING_SQL, {"after": after or "", "n": BATCH}).all()
    matched = [r.parcel_id for r in rows if _is_subject(
        rule, _lead(parcel_id=r.parcel_id,
                    enrichment_data={"assessor_current_owner": r.owner_name}),
        record_type=None, county="king", state="WA")]
    return rows, matched, {"king": matched, "_last": rows[-1].parcel_id if rows else None}


def _scan_nts(db, rule: ErasureRule, after: str | None):
    rows = db.execute(_NTS_SQL, {"after": after or _NIL, "n": BATCH}).all()
    # As the crawler's own gate: the grantor is the trustee-sale lead's party.
    matched = [r.id for r in rows if _is_subject(
        rule, _lead(parcel_id=r.parcel, party_name=r.grantor),
        record_type="trustee_sale", county=r.county, state=r.state)]
    return rows, matched, {"nts": matched, "_last": rows[-1].id if rows else None}


def _scan_county(db, rule: ErasureRule, after: str | None):
    rows = db.execute(_COUNTY_SQL, {"after": after or _NIL, "n": BATCH}).all()
    matched = [r.id for r in rows if any(_is_subject(
        rule, _lead(parcel_id=r.parcel_id, party_name=r.party_name, heirs=r.heirs,
                    enrichment_data=r.enrichment_data),
        record_type=order, county=r.county, state=r.state) for order in _COUNTY_ORDERS)]
    return rows, matched, {"county": matched, "_last": rows[-1].id if rows else None}


_SCANS = {"results": _scan_results, "king": _scan_king, "nts": _scan_nts,
          "county": _scan_county}


def erase_step(db, rule: ErasureRule, cursor: dict | None) -> Step:
    """Scan one batch of the store the cursor points at and erase its matches, in the
    caller's transaction (the caller commits and stores ``Step.cursor``)."""
    cursor = cursor or {}
    store = cursor.get("store", STORES[0])
    if store not in _SCANS:
        raise ValueError(f"unknown erasure store {store!r}")
    rows, matched, args = _SCANS[store](db, rule, cursor.get("after"))
    step = Step(scanned=len(rows))
    if matched:
        params = {"rule": rule.rule_row, "res": [], "keys": [], "king": [], "nts": [],
                  "county": []}
        params.update({k: v for k, v in args.items() if not k.startswith("_")})
        step.counts = dict(db.execute(_CALL, params).scalar())
    if len(rows) == BATCH:
        step.cursor = {"store": store, "after": args["_last"]}
    else:
        nxt = STORES.index(store) + 1
        step.done = nxt == len(STORES)
        step.cursor = {} if step.done else {"store": STORES[nxt]}
    return step
