"""Lead qualification: when a discovered public record is a deliverable lead.

Owner decision 2026-10-25: a code-enforcement case is not a lead until it names
the verified owner of a known parcel and can be mailed. Run 1ad28ded (King, 2026-10-17)
billed 491 code violations while King's owner source was in cooldown, so the delivered
file named nobody; 123 of them could never be named (no strict parcel match).

Scope is STAMPED, not inferred: the worker writes
``enrichment_data.qualification_policy`` at insert for every code_violation row from this
change on (src/workers/tasks.py). A row without the stamp keeps the pre-existing rule
(lead_actionability) untouched, so no historical lead disappears, is billed again, or
loses its dedup claim. Historical rows are only re-evaluated by an owner-approved script.

A subject row qualifies only when ALL of these hold (phone and email are never required;
skip trace is optional and paid):
  * owner: party_name set, named by the assessor of the row's own county (owner_source
    allowlisted per source), for the same parcel the lead shows (owner_pin);
  * parcel: the PIN the source printed (results.parcel_id) or a shown located PIN
    (src/utils/located_parcel.py: exact, street_only, address_point; never a condo
    complex or an address-only candidate);
  * a usable property address AND a usable owner mailing address (owner decision:
    mailing is mandatory; a PO box or out-of-state address is a valid mailing address);
  * the case: a source case/record number, a record date, and a classification.

Like lead_actionability: an ORM predicate, a raw-SQL twin and a Python twin, one rule.
The SQL twin is spliced into hand-written statements; it only ever contains this module's
constants and the caller's trusted alias.
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import Boolean, literal_column

from src.scrapers.king_cv_sources import BELLEVUE, BURIEN, KINGCO_ACCELA, SEATTLE_SDCI
from src.utils.located_parcel import located_parcel_id, shown_tier_sql

# What both twins trim before judging a value. Spelled once, for both: Python's bare
# strip() also drops Unicode spaces that Postgres btrim() keeps, and a value the SQL bills
# but the Python export drops would put a charge on a lead the file does not contain.
_TRIM = " \t\r\n"
_TRIM_SQL = "E' \\t\\r\\n'"

POLICY_KEY = "qualification_policy"
POLICY_CODE_VIOLATION = "code_violation_v1"
PLACEHOLDER = "(enrichment unavailable)"

# Mirrors of king_parcel_locate.OWNER_SOURCE / pierce_atip_owner.OWNER_SOURCE (a test pins
# them equal). Not imported: those modules pull scraper dependencies into the API.
KING_OWNER_SOURCE = "king_erealproperty"
PIERCE_OWNER_SOURCE = "pierce_atip"
TACOMA = "tacoma_code_violations"

# Which assessor may name the owner of each source's parcels. A King name on a Tacoma
# case (or the reverse) is an owner from the wrong county and never qualifies.
OWNER_SOURCE_BY_SOURCE: dict[str, str] = {
    SEATTLE_SDCI: KING_OWNER_SOURCE,
    BELLEVUE: KING_OWNER_SOURCE,
    BURIEN: KING_OWNER_SOURCE,
    KINGCO_ACCELA: KING_OWNER_SOURCE,
    TACOMA: PIERCE_OWNER_SOURCE,
}

_CASE_REF_KEYS = ("record_number", "case_number", "case_id")
_CLASSIFICATION_KEYS = ("violation_category", "case_type", "record_type")

# Reason codes. qualification_reasons reports location first, then owner, addresses, case.
MISSING_OWNER = "missing_owner"
OWNER_CONFLICT = "owner_conflict"
PARCEL_UNRESOLVED = "parcel_unresolved"
ADDRESS_AMBIGUOUS = "address_ambiguous"
MISSING_PROPERTY_ADDRESS = "missing_property_address"
MAILING_UNRESOLVED = "mailing_unresolved"
INVALID_CASE_DATA = "invalid_case_data"
UNSUPPORTED_SOURCE = "unsupported_source"

# States the API shows. Derived on read, never stored: a stored state goes stale the moment
# a sweep writes a field.
QUALIFIED = "qualified"
ENRICHMENT_PENDING = "enrichment_pending"
NEEDS_REVIEW = "needs_review"
NOT_QUALIFIED_STATE = "not_qualified"

# kc_pin outcomes where the location exists but is not one parcel we can stand behind.
_AMBIGUOUS_PIN_STATUSES = frozenset({"multiple", "address_mismatch"})
_AMBIGUOUS_PIN_MATCHES = frozenset({"condo_complex", "address_only"})

_ALIAS_RE = re.compile(r"[a-z_][a-z0-9_]*")
# Every supported source's parcel is a 10-digit county PIN (King and Pierce alike). Text
# of any other shape is not a parcel, which also rejects a placeholder or a PIN that lost
# its leading zero.
_PARCEL_RE = re.compile(r"[0-9]{10}")
_HAS_ALNUM = re.compile(r"[A-Za-z0-9]")


def is_subject(enrichment_data: Any) -> bool:
    """True when the row was stamped for qualification at insert."""
    return isinstance(enrichment_data, dict) and enrichment_data.get(POLICY_KEY) == POLICY_CODE_VIOLATION


def stamp(enrichment_data: Any) -> dict:
    """enrichment_data with the code-violation policy stamped (a new dict)."""
    out = dict(enrichment_data) if isinstance(enrichment_data, dict) else {}
    out[POLICY_KEY] = POLICY_CODE_VIOLATION
    return out


# ── Python twin ──────────────────────────────────────────────────────────────

def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def _json_text(value: Any) -> str | None:
    """What Postgres ``->>`` returns for a JSON value: text, None for null."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


def _usable(value: Any) -> bool:
    """Non-blank, not the placeholder, and carrying at least one ASCII letter or digit.

    The last test rejects a value made only of spaces Postgres does not trim (a
    no-break space, Codex r3) the same way in both twins. Assessor names, addresses,
    case numbers and dates are ASCII in every supported source."""
    text_value = _json_text(value)
    if text_value is None:
        return False
    trimmed = text_value.strip(_TRIM)
    return trimmed != PLACEHOLDER and _HAS_ALNUM.search(trimmed) is not None


def _case_ref(value: Any) -> bool:
    """A usable case number: a JSON string, or a JSON number (some sources number cases).

    Never a boolean, array or object, whose JSON text would otherwise pass (Codex r4)."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    return _usable(value)


def _str(value: Any) -> str | None:
    """A JSON string value, else None (an array, object or number is never a key here)."""
    return value if isinstance(value, str) else None


def shown_parcel(row: Any) -> str | None:
    """The parcel the lead shows: the printed parcel_id, else a shown located PIN.

    None unless it is a 10-digit PIN (ASCII digits only, like located_parcel)."""
    printed = _field(row, "parcel_id")
    if isinstance(printed, str) and printed.strip(_TRIM):
        pin = printed.strip(_TRIM)
    else:
        pin = located_parcel_id(_field(row, "enrichment_data"))
    if not (isinstance(pin, str) and pin.isascii() and _PARCEL_RE.fullmatch(pin)):
        return None
    return pin


def qualification_reasons(row: Any) -> list[str]:
    """Why a subject row is not a deliverable lead; [] when it qualifies or is not subject."""
    ed = _field(row, "enrichment_data")
    if not is_subject(ed):
        return []
    reasons: list[str] = []
    expected_owner_source = OWNER_SOURCE_BY_SOURCE.get(_str(ed.get("source")) or "")
    if expected_owner_source is None:
        return [UNSUPPORTED_SOURCE]

    parcel = shown_parcel(row)
    if parcel is None:
        if (_str(ed.get("kc_pin_status")) in _AMBIGUOUS_PIN_STATUSES
                or _str(ed.get("kc_pin_match")) in _AMBIGUOUS_PIN_MATCHES):
            reasons.append(ADDRESS_AMBIGUOUS)
        else:
            reasons.append(PARCEL_UNRESOLVED)

    if not _usable(_str(_field(row, "party_name"))):
        reasons.append(MISSING_OWNER)
    elif (_str(ed.get("owner_source")) != expected_owner_source or parcel is None
          or (_str(ed.get("owner_pin")) or "").strip(_TRIM) != parcel):
        # A name exists but nothing ties it to this parcel's assessor record: it may be a
        # complainant, a tenant, a label, or another parcel's taxpayer.
        reasons.append(OWNER_CONFLICT)

    if not _usable(_str(_field(row, "property_address"))):
        reasons.append(MISSING_PROPERTY_ADDRESS)
    if not _usable(_str(_field(row, "mailing_address"))):
        reasons.append(MAILING_UNRESOLVED)
    if (not any(_case_ref(ed.get(k)) for k in _CASE_REF_KEYS)
            or not any(_usable(_str(ed.get(k))) for k in _CLASSIFICATION_KEYS)
            or not _usable(_str(_field(row, "date_recorded")))):
        reasons.append(INVALID_CASE_DATA)
    return reasons


def is_qualified(row: Any) -> bool:
    """Python twin of qualified_sql: not subject, or subject and every check passes."""
    return not qualification_reasons(row)


# Why a row was held, frozen by the hold step with the run (the hold itself is frozen: a sweep
# naming the owner later does not unhold it). Live reasons can drift to [] after a sweep, so
# the Results page reads this key to say why the run held the row.
HOLD_KEY = "qualification_hold"


def hold_record(row: Any) -> dict:
    """The value the hold step freezes under HOLD_KEY for a held row."""
    return {"reasons": qualification_reasons(row), "policy": POLICY_CODE_VIOLATION}


def qualification_state(row: Any) -> str | None:
    """The state the API shows for a subject row; None for a row not subject to the policy.

    enrichment_pending: the parcel is known and only owner/mailing is missing, and the
    owner has not been settled as absent, so a sweep or a later run can still finish it.
    needs_review: the location is ambiguous or the owner does not tie to the parcel.
    """
    ed = _field(row, "enrichment_data")
    if not is_subject(ed):
        return None
    reasons = qualification_reasons(row)
    if not reasons:
        return QUALIFIED
    if ADDRESS_AMBIGUOUS in reasons or OWNER_CONFLICT in reasons:
        return NEEDS_REVIEW
    retryable = {MISSING_OWNER, MAILING_UNRESOLVED}
    owner_settled = _str(ed.get("cv_owner_recovery_outcome")) in ("not_on_record", "gave_up")
    if set(reasons) <= retryable and not owner_settled:
        return ENRICHMENT_PENDING
    return NOT_QUALIFIED_STATE


# ── SQL twins ────────────────────────────────────────────────────────────────

def _ten_digits(expr: str) -> str:
    """``expr`` is a 10-digit PIN. Spelled without regex braces on purpose: callers splice
    this SQL into templates that are later run through str.format (segments.py), where
    ``{10}`` would be read as a placeholder."""
    return f"(length({expr}) = 10 AND {expr} ~ '^[0-9]+$')"


def _btrim(col: str) -> str:
    return f"btrim({col}, {_TRIM_SQL})"


def _usable_sql(col: str) -> str:
    return f"({_btrim(col)} <> '{PLACEHOLDER}' AND {_btrim(col)} ~ '[A-Za-z0-9]')"


def qualified_sql(alias: str) -> str:
    """Raw-SQL twin of is_qualified for hand-written queries over ``alias`` (a results alias)."""
    if not _ALIAS_RE.fullmatch(alias):
        raise ValueError(f"qualified_sql needs a table alias, got {alias!r}")
    ed = f"{alias}.enrichment_data"
    shown_pin = (
        f"CASE WHEN {ed}->>'kc_pin_status' = 'matched' AND {_ten_digits(f'({ed}->>{chr(39)}kc_pin{chr(39)})')} "
        f"AND {shown_tier_sql(ed)} THEN {ed}->>'kc_pin' END"
    )
    any_parcel = f"COALESCE(NULLIF({_btrim(f'{alias}.parcel_id')}, ''), {shown_pin})"
    parcel = f"(CASE WHEN {_ten_digits(any_parcel)} THEN {any_parcel} END)"
    owner_source = " ".join(
        f"WHEN '{source}' THEN '{owner}'" for source, owner in sorted(OWNER_SOURCE_BY_SOURCE.items()))
    case_ref = " OR ".join(
        f"(json_typeof({ed}->'{k}') IN ('string', 'number') AND {_usable_sql(f'({ed}->>{chr(39)}{k}{chr(39)})')})"
        for k in _CASE_REF_KEYS)
    classification = " OR ".join(
        f"(json_typeof({ed}->'{k}') = 'string' AND {_usable_sql(f'({ed}->>{chr(39)}{k}{chr(39)})')})"
        for k in _CLASSIFICATION_KEYS)
    checks = " AND ".join((
        _usable_sql(f"{alias}.party_name"),
        f"{ed}->>'owner_source' = (CASE {ed}->>'source' {owner_source} END)",
        f"json_typeof({ed}->'owner_pin') = 'string'",
        f"{_btrim(f'({ed}->>{chr(39)}owner_pin{chr(39)})')} = {parcel}",
        _usable_sql(f"{alias}.property_address"),
        _usable_sql(f"{alias}.mailing_address"),
        f"({case_ref})",
        f"({classification})",
        _usable_sql(f"{alias}.date_recorded"),
    ))
    # COALESCE to false: a missing parcel makes the owner_pin comparison NULL, and a NULL
    # here would make NOT qualified NULL too, so the hold step would skip the row.
    return (f"(COALESCE({ed}->>'{POLICY_KEY}', '') <> '{POLICY_CODE_VIOLATION}' "
            f"OR COALESCE(({checks}), false))")


def qualified_condition():
    """ORM predicate twin of qualified_sql, bound to the results table.

    A boolean column expression rather than text(): withheld_condition negates it, and
    SQLAlchemy cannot negate a bare TextClause."""
    return literal_column(qualified_sql("results"), Boolean)

