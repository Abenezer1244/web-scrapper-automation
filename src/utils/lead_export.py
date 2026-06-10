"""Canonical lead-CSV export — ONE dialer-ready format for every export path.

Both the in-app live download (src/api/routes/jobs.py) and the scheduled/emailed
R2 export (src/utils/data_exporter.py) build their CSV through this module, so the
two outputs can't drift (they used to: the download had dialer-ready split columns
while the scheduled export had a stale set). DataExporter owns file/R2 I/O; this
module owns lead-CSV SEMANTICS (columns, phone normalization, name/address split,
sanitization).

The DNC/TCPA disclaimer is deliberately NOT in the CSV — a `#` line is still a row
to many dialer importers (garbage contact). The notice lives in the delivery email
body + the download UI instead (placement is not compliance — the real obligation
is DNC-registry scrubbing + records, which is the dialer/process layer).

Input is duck-typed: each record may be an ORM object (attribute access) OR a dict
(scheduled exports hand dict lists). Secondary contacts are read from EITHER the
`phones`/`emails` arrays OR already-flattened `phone_2`/`email_2` keys, so dict
exports never silently drop them.
"""
import csv
from typing import Any

from src.api.middleware.security import sanitize_for_csv
from src.utils.lead_formatting import (
    normalize_phone_for_dialer,
    parse_property_for_display,
    split_owner_for_display,
)

# Canonical column order. Existing reference/legacy columns first, dialer-import
# split columns appended at END (backward-compatible for header-mapped consumers).
LEAD_CSV_COLUMNS: list[str] = [
    "date_recorded", "party_name", "heirs", "parcel_id",
    "property_address", "mailing_address", "legal_description", "doc_type",
    "delinquent_amount", "delinquent_bill_year",
    "phone", "phone_type", "email",
    "phone_2", "phone_3", "email_2", "email_3",
    "first_name", "last_name",
    "property_street", "property_city", "property_state", "property_zip",
]


def _get(record: Any, name: str) -> Any:
    """Read a field from either a dict (.get) or an ORM/object (getattr)."""
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _nth_phone(record: Any, i: int) -> Any:
    """i-th phone (0-based): prefer the `phones` array, fall back to flattened key."""
    phones = _get(record, "phones")
    if isinstance(phones, list) and i < len(phones) and isinstance(phones[i], dict):
        num = phones[i].get("number")
        if isinstance(num, str) and num:
            return num
    return _get(record, f"phone_{i + 1}")  # phone_2 for i=1, phone_3 for i=2


def _nth_email(record: Any, i: int) -> Any:
    """i-th email (0-based): prefer the `emails` array, fall back to flattened key."""
    emails = _get(record, "emails")
    if isinstance(emails, list) and i < len(emails) and isinstance(emails[i], str):
        if emails[i]:
            return emails[i]
    return _get(record, f"email_{i + 1}")  # email_2 for i=1, email_3 for i=2


def build_lead_export_row(record: Any) -> dict[str, str]:
    """Build one canonical CSV row dict from an ORM Result or a plain dict.

    Parses raw name/address, THEN sanitizes each emitted value (never before
    parsing — escaping changes the string shape). Phones are normalized to bare
    10-digit (digits-only output is inherently CSV-injection-safe). Numerics are
    rendered plainly. Keys exactly match LEAD_CSV_COLUMNS.
    """
    first, last = split_owner_for_display(_get(record, "party_name"))
    prop = parse_property_for_display(_get(record, "property_address"))

    amt = _get(record, "delinquent_amount")
    year = _get(record, "delinquent_bill_year")

    return {
        "date_recorded": sanitize_for_csv(_get(record, "date_recorded")),
        "party_name": sanitize_for_csv(_get(record, "party_name")),
        "heirs": sanitize_for_csv(_get(record, "heirs")),
        "parcel_id": sanitize_for_csv(_get(record, "parcel_id")),
        "property_address": sanitize_for_csv(_get(record, "property_address")),
        "mailing_address": sanitize_for_csv(_get(record, "mailing_address")),
        "legal_description": sanitize_for_csv(_get(record, "legal_description")),
        "doc_type": sanitize_for_csv(_get(record, "doc_type")),
        "delinquent_amount": "" if amt is None else f"{amt}",
        "delinquent_bill_year": "" if year is None else f"{year}",
        "phone": normalize_phone_for_dialer(_get(record, "phone")),
        "phone_type": sanitize_for_csv(_get(record, "phone_type")),
        "email": sanitize_for_csv(_get(record, "email")),
        "phone_2": normalize_phone_for_dialer(_nth_phone(record, 1)),
        "phone_3": normalize_phone_for_dialer(_nth_phone(record, 2)),
        "email_2": sanitize_for_csv(_nth_email(record, 1)),
        "email_3": sanitize_for_csv(_nth_email(record, 2)),
        "first_name": sanitize_for_csv(first),
        "last_name": sanitize_for_csv(last),
        "property_street": sanitize_for_csv(prop["street"]),
        "property_city": sanitize_for_csv(prop["city"]),
        "property_state": sanitize_for_csv(prop["state"]),
        "property_zip": sanitize_for_csv(prop["zip"]),
    }


def write_lead_csv(records: list[Any], filelike) -> None:
    """Write the canonical lead CSV (header + rows) to an open text file/StringIO.

    No footer rows — the machine-import file stays clean (the DNC disclaimer lives
    in the delivery email + download UI). Caller owns opening/closing the stream.
    """
    writer = csv.DictWriter(filelike, fieldnames=LEAD_CSV_COLUMNS)
    writer.writeheader()
    for rec in records:
        writer.writerow(build_lead_export_row(rec))
