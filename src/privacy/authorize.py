"""The privacy authorization service: may this lead row be delivered? (design section 6)

One decision for every enforcement layer. The P4 send paths (dialer outbox, Tracerfy
dispatcher, ingest) and the P5 enforcer sweep call it; read paths use the stored flag
(results.privacy_suppressed_at, src/api/lead_actionability.py) the sweep writes from it.
Nothing calls it before P4, and no rule can be active before P7 (migration 125's guard).

LOADING (``load_rule_snapshot``): ONE statement reads every rule row, so the whole set is
one consistent snapshot, loaded once per operation and indexed in memory (no per-row
query). Rule rows are append-only; a rule's state is its history per (rule_id,
key_version, normalization_version), which must be exactly one of
    pending                    recorded, not enforced
    pending -> active          ENFORCED
    pending -> active -> lifted  no longer enforced
in created_at order. Any other history, an active rule whose key version is not live or
whose normalisation version is not the current one, an active anchor kind that cannot be
computed (mailing), or a scope other than 'subject' raises ``RuleSnapshotError``: the
operation fails and retries, and NEVER runs without a rule it cannot evaluate.

DECIDING (``authorize``), per row:
  anchor match + a rule's own subject on the row  -> WITHHELD (the whole row: D2, co-owners
                                                     are not delivered either)
  anchor match + an unresolved subject on the row -> REVIEW (cannot tell who it names)
  a rule's subject on the row WITHOUT its anchor  -> REVIEW (an address-only row with no
                                                     parcel, a moved or re-parcelled
                                                     property: never silently allowed)
  otherwise                                       -> ALLOWED (a shared parcel or phone
                                                     alone never suppresses other people)
Callers treat REVIEW exactly like WITHHELD until an operator decides (P7).

ANCHORS: the property PARCEL (keys.property_anchor_value), every phone and every email
on the row. Street addresses are never an automatic anchor (keys.py says why).
SUBJECTS: ``party_name`` (role party) and ``heirs`` (role heir), read in the source's
declared name order, plus every OWNER-OF-RECORD name a customer receives from
``enrichment_data`` (``_OWNER_OF_RECORD_KEYS``; exported as "current_owner"). A
trustee sale's grantor IS its party_name (src/scrapers/trustee_sale.py).

ACCESS: migration 125 revokes privacy_suppressions from every runtime role. Workers
load rules through ``public.privacy_rule_rows()`` (migration 129, SECURITY DEFINER,
EXECUTE granted to bridgeleads_system only), which returns the loader's columns and
nothing from the request tables.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from src.privacy import keys
from src.privacy.subjects import (
    KIND_UNRESOLVED,
    NORMALIZATION_VERSION,
    email_key_value,
    phone_key_value,
    subject_key_values,
    subjects_from,
)

ALLOWED = "allowed"
WITHHELD = "withheld"
REVIEW = "review"

REVIEW_UNRESOLVED_SUBJECT = "unresolved_subject_on_matched_anchor"
REVIEW_SUBJECT_WITHOUT_ANCHOR = "subject_match_without_anchor"

_STATE_RANK = {"pending": 0, "active": 1, "lifted": 2}
_HISTORIES = {
    ("pending",): False,
    ("pending", "active"): True,
    ("pending", "active", "lifted"): False,
}
_AUTOMATIC_ANCHOR_KINDS = frozenset({"property", "phone", "email"})

# Every enrichment_data key holding an owner-of-record name that is delivered to a
# customer (src/utils/lead_export.py). Today only the King Assessor's current owner
# (written in tasks_helpers/enrich.py); a new owner key must be added here.
_OWNER_OF_RECORD_KEYS = ("assessor_current_owner",)
# The Assessor's owner feed is the one tax-delinquent party_name comes from, so it is
# read in that source's declared order.
_OWNER_OF_RECORD_ORDER_SOURCE = "tax_delinquent"

# Through migration 129's SECURITY DEFINER function, never the table: the worker role
# holds no table privilege, and RLS (no policy) would show it zero rows, i.e. allow all.
_LOAD_SQL = text(
    "SELECT id, rule_id, anchor_kind, anchor_hmac, subject_kind, subject_hmac, scope, "
    "key_version, normalization_version, state, created_at "
    "FROM public.privacy_rule_rows()"
)


class RuleSnapshotError(RuntimeError):
    """The rules cannot be evaluated: fail the operation, never proceed unchecked."""


@dataclass(frozen=True)
class Decision:
    outcome: str                              # allowed | withheld | review
    suppression_ids: tuple[str, ...] = ()     # privacy_suppressions.id of the active rows
    reason: str | None = None                 # review only

    @property
    def deliverable(self) -> bool:
        return self.outcome == ALLOWED


@dataclass(frozen=True)
class _Rule:
    suppression_id: str   # the ACTIVE row's id: what results.privacy_suppression_id stores
    rule_id: str
    key_version: str
    subject_kind: str
    subject_hmac: str


@dataclass(frozen=True)
class RuleSnapshot:
    by_anchor: dict[tuple[str, str, str], tuple[_Rule, ...]]   # (kind, version, hmac)
    by_subject: dict[tuple[str, str, str], tuple[_Rule, ...]]  # (kind, version, hmac)
    key_versions: tuple[str, ...]
    # The number of 'active' rule rows ever written, read in the same statement as the
    # rules. Rule rows are append-only, so it only grows, and only an activation grows
    # it: results.privacy_checked_gen below it means "not checked against these rules".
    generation: int = 0

    @property
    def active_rules(self) -> int:
        return len({r.suppression_id for rules in self.by_anchor.values() for r in rules})


# Every column authorize() reads. A Mapping row (a column projection) must carry all of
# them: a projection that silently left one out would read it as empty, i.e. allow.
ROW_COLUMNS = ("party_name", "heirs", "parcel_id", "phone", "phones", "email", "emails",
               "enrichment_data")


def _get(row: Any, name: str):
    # Any mapping (a dict, a SQLAlchemy RowMapping) by key; an ORM row by attribute.
    # A RowMapping read by attribute would yield None for every field: allow-all.
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


def build_snapshot(rows) -> RuleSnapshot:
    """Validate every rule history and index the ACTIVE ones (pure; the loader's core)."""
    groups: dict[tuple[str, str, str], list] = defaultdict(list)
    for r in rows:
        state = _get(r, "state")
        if state not in _STATE_RANK:
            raise RuleSnapshotError(f"rule row {_get(r, 'id')} has unknown state {state!r}")
        groups[(_get(r, "rule_id"), _get(r, "key_version"),
                _get(r, "normalization_version"))].append(r)

    try:
        live = set(keys.live_versions()) if groups else set()
    except (KeyError, RuntimeError, ValueError) as exc:  # keyring misconfigured
        raise RuleSnapshotError(f"privacy keyring unavailable: {exc}") from exc
    by_anchor: dict[tuple[str, str, str], list[_Rule]] = defaultdict(list)
    by_subject: dict[tuple[str, str, str], list[_Rule]] = defaultdict(list)
    versions: set[str] = set()
    for (rule_id, version, norm), history in groups.items():
        # created_at order; a tie (one transaction) is ordered by the lifecycle.
        history.sort(key=lambda h: (_get(h, "created_at"), _STATE_RANK[_get(h, "state")]))
        states = tuple(_get(h, "state") for h in history)
        if states not in _HISTORIES:
            raise RuleSnapshotError(f"rule {rule_id} ({version}/{norm}) has history {states}")
        if not _HISTORIES[states]:
            continue
        active = history[1]
        if version not in live:
            raise RuleSnapshotError(f"active rule {rule_id} uses key {version}, not live")
        if norm != NORMALIZATION_VERSION:
            raise RuleSnapshotError(
                f"active rule {rule_id} uses normalisation {norm}, not {NORMALIZATION_VERSION}")
        if _get(active, "scope") != "subject":
            raise RuleSnapshotError(f"active rule {rule_id} has unsupported scope")
        if _get(active, "anchor_kind") not in _AUTOMATIC_ANCHOR_KINDS:
            raise RuleSnapshotError(
                f"active rule {rule_id} has anchor {_get(active, 'anchor_kind')!r}, "
                "which cannot be computed from a row")
        rule = _Rule(_get(active, "id"), rule_id, version,
                     _get(active, "subject_kind"), _get(active, "subject_hmac"))
        by_anchor[(_get(active, "anchor_kind"), version, _get(active, "anchor_hmac"))].append(rule)
        by_subject[(rule.subject_kind, version, rule.subject_hmac)].append(rule)
        versions.add(version)
    return RuleSnapshot(
        by_anchor={k: tuple(v) for k, v in by_anchor.items()},
        by_subject={k: tuple(v) for k, v in by_subject.items()},
        key_versions=tuple(sorted(versions)),
        generation=sum(1 for history in groups.values() for h in history
                       if _get(h, "state") == "active"),
    )


def load_rule_snapshot(db) -> RuleSnapshot:
    """Every rule row in one statement (sync Session or Connection). Raises
    ``RuleSnapshotError`` for any history or version it cannot evaluate."""
    try:
        rows = db.execute(_LOAD_SQL).mappings().all()
    except Exception as exc:  # noqa: BLE001 -- re-raised as the fail-closed type
        raise RuleSnapshotError(f"rules could not be loaded: {type(exc).__name__}") from exc
    return build_snapshot(rows)


def _anchor_values(row: Any, county: str | None, state: str | None) -> set[tuple[str, str]]:
    values: set[tuple[str, str]] = set()
    parcel = keys.property_anchor_value(_get(row, "parcel_id"), county, state)
    if parcel:
        values.add(("property", parcel))
    phones = [_get(row, "phone")] + [
        p.get("number") if isinstance(p, dict) else p for p in (_get(row, "phones") or [])]
    emails = [_get(row, "email")] + list(_get(row, "emails") or [])
    for raw in phones:
        v = phone_key_value(raw) if isinstance(raw, str) else None
        if v:
            values.add(("phone", v))
    for raw in emails:
        v = email_key_value(raw) if isinstance(raw, str) else None
        if v:
            values.add(("email", v))
    return values


def _subjects(row: Any, record_type: str | None, county: str | None):
    # Imported here: src.utils.lead_export imports src.api, whose package imports
    # lead_export back, so a module-level import would depend on who imported first.
    from src.utils.lead_export import name_order_for

    order = name_order_for(record_type, county)
    subjects = (subjects_from(_get(row, "party_name"), role="party", source_field="party_name",
                              name_order=order)
                + subjects_from(_get(row, "heirs"), role="heir", source_field="heirs",
                                name_order=order))
    enrichment = _get(row, "enrichment_data")
    if isinstance(enrichment, Mapping):
        owner_order = name_order_for(_OWNER_OF_RECORD_ORDER_SOURCE, county)
        for key in _OWNER_OF_RECORD_KEYS:
            value = enrichment.get(key)
            if value is not None:  # never skipped by type: subjects_from reads str(value)
                subjects += subjects_from(value, role="owner_of_record",
                                          source_field=f"enrichment_data.{key}",
                                          name_order=owner_order)
    return subjects


def _hmac(kind: str, value: str, version: str) -> str:
    try:
        return keys.privacy_hmac(kind, value, version)
    except (KeyError, RuntimeError, ValueError) as exc:
        # A key that vanished mid-operation, or a value the key library refuses: the
        # decision cannot be made, so the operation fails rather than allowing the row.
        raise RuleSnapshotError(f"privacy key {version} unavailable: {exc}") from exc


def authorize(snapshot: RuleSnapshot, row: Any, *, record_type: str | None,
              county: str | None, state: str | None) -> Decision:
    """Whether ``row`` (a Result or a dict with its columns) may be delivered."""
    if not snapshot.by_anchor:
        return Decision(ALLOWED)
    if isinstance(row, Mapping):
        missing = [c for c in ROW_COLUMNS if c not in row]
        if missing:  # a caller's projection bug: fail the operation, never allow
            raise RuleSnapshotError(f"row is missing authorize() columns {missing}")
    subjects = _subjects(row, record_type, county)
    anchors = _anchor_values(row, county, state)
    unresolved = any(s.kind == KIND_UNRESOLVED for s in subjects)
    keyed_subjects = [kv for s in subjects for kv in subject_key_values(s)]

    on_row: set[tuple[str, str, str]] = set()
    matched_rules: list[_Rule] = []
    for version in snapshot.key_versions:
        for kind, value in keyed_subjects:
            on_row.add((kind, version, _hmac(kind, value, version)))
        for kind, value in anchors:
            matched_rules.extend(
                snapshot.by_anchor.get((kind, version, _hmac(kind, value, version)), ()))

    hits = sorted({r.suppression_id for r in matched_rules
                   if (r.subject_kind, r.key_version, r.subject_hmac) in on_row})
    if hits:
        return Decision(WITHHELD, tuple(hits))
    if matched_rules and unresolved:
        return Decision(REVIEW, tuple(sorted({r.suppression_id for r in matched_rules})),
                        REVIEW_UNRESOLVED_SUBJECT)
    named = sorted({r.suppression_id for key in on_row for r in snapshot.by_subject.get(key, ())})
    if named:
        return Decision(REVIEW, tuple(named), REVIEW_SUBJECT_WITHOUT_ANCHOR)
    return Decision(ALLOWED)
