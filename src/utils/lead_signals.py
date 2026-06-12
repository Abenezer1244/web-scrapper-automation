"""Derived lead-quality signals — pure functions over data we already store.

Gap-analysis Tier 0 (2026-06-12, Codex-consulted): these are computed at
serialize/export time and NEVER stored. They decay (freshness_days) or are
trivial functions of existing columns (months_delinquent, contactability), so a
stored column would only add backfill + staleness risk for no filter benefit
that the source columns don't already give.

Every function takes its inputs explicitly + an injected `today` (date), so the
results are deterministic and testable without freezing the clock (testing rule:
never assert on a hardcoded "now"). `derive_signals()` is the one record-level
adapter the export + API layers call.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

# WA RCW 84.64: a parcel becomes tax-foreclosure eligible once a tax year is a
# full 3 years delinquent. Used by wa_foreclosure_eligible().
_WA_TAX_FORECLOSURE_YEARS = 3

_MDY = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\b")


def months_delinquent(bill_year: int | None, today: date) -> int | None:
    """Months a tax bill has been delinquent, derived from its bill year.

    Mirrors src/api/tax_filters.py exactly: WA property-tax bills issue ~Jan 1 of
    the bill year, so months = (today.year*12 + today.month-1) - bill_year*12.
    None bill_year (every non-King/Snohomish-tax row) -> None. Never negative.
    """
    if bill_year is None:
        return None
    base = today.year * 12 + (today.month - 1)
    months = base - bill_year * 12
    return months if months >= 0 else 0


def wa_foreclosure_eligible(bill_year: int | None, today: date) -> bool:
    """True when the (oldest) delinquent tax year is ≥3 full years old (WA RCW 84.64).

    The unique WA signal no national tool computes: at 3 years the county
    treasurer may file a Certificate of Delinquency and begin foreclosure. None
    bill_year -> False (not a structured tax row, so unknown = not-eligible).
    """
    if bill_year is None:
        return False
    return bill_year <= today.year - _WA_TAX_FORECLOSURE_YEARS


def _parse_mdy(value: Any) -> date | None:
    """Parse an MM/DD/YYYY recorder date string to a date; None if unparseable."""
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    m = _MDY.match(value)
    if not m:
        return None
    mm, dd, yyyy = (int(g) for g in m.groups())
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        return None


def freshness_days(
    date_recorded_parsed: date | None,
    date_recorded: Any,
    today: date,
) -> int | None:
    """Days since the lead was recorded/filed — the freshness clock investors triage on.

    Prefers the DB-generated `date_recorded_parsed` (a real DATE); falls back to
    parsing the raw MM/DD/YYYY `date_recorded` string for dict exports that don't
    carry the generated column. None when neither parses. Clamped at 0 (a future
    filing date is bad data, not negative age).
    """
    filed = date_recorded_parsed if isinstance(date_recorded_parsed, date) else _parse_mdy(date_recorded)
    if filed is None:
        return None
    delta = (today - filed).days
    return delta if delta >= 0 else 0


def contactability_score(
    phone: Any,
    email: Any,
    phones: Any,
    emails: Any,
) -> int:
    """0–6 transparent score: count of distinct phone + email contacts present.

    Deliberately NOT a black-box ML rank — a wholesaler can read it as "how many
    ways can I reach this owner." Counts the primary phone/email plus the extra
    `phones`/`emails` arrays (deduped against the primary, capped at 3 each, so
    the max is 3 phones + 3 emails = 6). A row with no contacts scores 0.
    """
    phone_nums: set[str] = set()
    if isinstance(phone, str) and phone.strip():
        phone_nums.add(_digits(phone))
    if isinstance(phones, list):
        for item in phones:
            # Tolerate dicts ({"number": ...}) and objects (PhoneContact.number):
            # the export path passes JSON dicts, the API path passes Pydantic rows.
            num = item.get("number") if isinstance(item, dict) else getattr(item, "number", None)
            if isinstance(num, str) and num.strip():
                phone_nums.add(_digits(num))
    phone_nums.discard("")

    email_set: set[str] = set()
    if isinstance(email, str) and email.strip():
        email_set.add(email.strip().lower())
    if isinstance(emails, list):
        for e in emails:
            if isinstance(e, str) and e.strip():
                email_set.add(e.strip().lower())

    return min(len(phone_nums), 3) + min(len(email_set), 3)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _get(record: Any, name: str) -> Any:
    """Read a field from a dict (.get) or an ORM/object (getattr) — mirrors lead_export."""
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def derive_signals(record: Any, today: date) -> dict[str, Any]:
    """Compute all derived signals for one record (ORM Result or export dict).

    Returns a dict with the four signal keys. The export and API layers each
    render these in their own shape; this is the single source of the math.
    """
    bill_year = _get(record, "delinquent_bill_year")
    return {
        "months_delinquent": months_delinquent(bill_year, today),
        "wa_foreclosure_eligible": wa_foreclosure_eligible(bill_year, today),
        "freshness_days": freshness_days(
            _get(record, "date_recorded_parsed"), _get(record, "date_recorded"), today
        ),
        "contactability_score": contactability_score(
            _get(record, "phone"),
            _get(record, "email"),
            _get(record, "phones"),
            _get(record, "emails"),
        ),
    }
