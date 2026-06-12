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
from decimal import Decimal, InvalidOperation
from typing import Any

from src.api.middleware.security import sanitize_for_csv
from src.utils.lead_formatting import (
    normalize_phone_for_dialer,
    parse_property_for_display,
    split_owner_for_display,
)

# Canonical column order. Existing reference/legacy columns first, dialer-import
# split columns + enrichment passthrough appended at END (backward-compatible for
# header-mapped consumers — old importers keep working, new columns are extra).
LEAD_CSV_COLUMNS: list[str] = [
    "date_recorded", "party_name", "heirs", "parcel_id",
    "property_address", "mailing_address", "legal_description", "doc_type",
    "delinquent_amount", "delinquent_bill_year",
    "phone", "phone_type", "email",
    "phone_2", "phone_3", "email_2", "email_3",
    "first_name", "last_name",
    "property_street", "property_city", "property_state", "property_zip",
    # Enrichment passthrough (2026-06-12, gap-analysis Tier 0): structured data
    # we already scrape into enrichment_data but never exported. Blank for record
    # types that don't carry the field (same convention as delinquent_amount).
    "assessed_value", "instrument_number",
    "code_violation_type", "code_violation_status",
    "code_violation_description", "code_violation_last_inspection",
    "tax_billed_amount", "tax_paid_amount", "tax_account_status",
]


def _enrichment(record: Any) -> dict:
    """The record's enrichment_data as a dict (empty if absent/malformed).

    Read straight from enrichment_data, NOT source-gated like _extract_tax_fields
    in workers/tasks.py. The gate there guards delinquent_amount — a FILTER +
    billing column where a mislabeled value changes what a user sees and pays for.
    These columns are display-only passthrough: a stray value in the wrong column
    is cosmetic, and the keys (instrument_number, billed_amount, last_inspection,
    …) are specific to the scrapers that emit them. Dict exports (segments) may
    not carry enrichment_data at all → blank, which is correct.
    """
    data = _get(record, "enrichment_data")
    return data if isinstance(data, dict) else {}


def _enrich_str(data: dict, key: str) -> str:
    """Sanitized string value of enrichment_data[key], '' when absent."""
    val = data.get(key)
    return sanitize_for_csv(val) if val not in (None, "") else ""


def _enrich_num(data: dict, key: str) -> str:
    """Plain numeric string of enrichment_data[key], '' when absent/non-numeric.

    Accepts numbers or numeric strings (scrapers store either). Renders without
    currency symbols/commas so the value is dialer/spreadsheet clean. Digits-only
    output after coercion is inherently CSV-injection-safe.
    """
    val = data.get(key)
    if val in (None, ""):
        return ""
    try:
        d = Decimal(str(val).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        # Non-numeric (unexpected) — fall back to a sanitized string rather than drop.
        return sanitize_for_csv(val)
    if not d.is_finite():
        return ""
    # Plain decimal string — no scientific notation, no currency formatting.
    return format(d, "f")


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
    enr = _enrichment(record)

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
        # Enrichment passthrough (Tier 0): see _enrichment() for why these read
        # the JSON keys directly. Numeric fields rendered plainly; rest sanitized.
        "assessed_value": _enrich_num(enr, "assessed_value"),
        "instrument_number": _enrich_str(enr, "instrument_number"),
        "code_violation_type": _enrich_str(enr, "record_type"),
        "code_violation_status": _enrich_str(enr, "status"),
        "code_violation_description": _enrich_str(enr, "description"),
        "code_violation_last_inspection": _enrich_str(enr, "last_inspection"),
        "tax_billed_amount": _enrich_num(enr, "billed_amount"),
        "tax_paid_amount": _enrich_num(enr, "paid_amount"),
        "tax_account_status": _enrich_str(enr, "account_status"),
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


# Overlap/combine CSV (Lists page + batch scrape). Same dialer-ready semantics as
# the canonical row, with the overlap signal up front in "caller-first" order so a
# human opening it in Excel sees the hottest leads and the contact fields first.
# Reuses build_lead_export_row so the split/normalize/sanitize logic can never
# drift from the per-job export. Segments provide multi-contact phones/emails
# (decrypted arrays) so phone_2/3 + email_2/3 populate; columns the segment
# query doesn't provide (heirs, legal_description, doc_type, tax) come through
# blank — kept for header parity with the per-job CSV.
OVERLAP_LEAD_COLUMNS: list[str] = [
    "overlap", "lists_count", "lists", "counties",
    "first_name", "last_name",
    "phone", "phone_type", "email", "phone_2", "phone_3", "email_2", "email_3",
    "property_street", "property_city", "property_state", "property_zip",
    "filed_date", "doc_type", "delinquent_amount", "delinquent_bill_year",
    "party_name", "mailing_address", "parcel_id", "heirs", "legal_description",
    "property_address",
]


def build_overlap_export_row(record: Any, overlap: dict[str, Any]) -> dict[str, str]:
    """One overlap-CSV row from a lead record + its overlap metadata.

    `overlap` carries `lists_count` (distinct record types this property is on),
    `lists` (human-readable, already "; "-joined), and `counties`. The `overlap`
    flag is the WORD "Overlap" when on 2+ lists, else blank (more scannable than
    TRUE/FALSE). All other fields come straight from the canonical dialer-ready
    row so formatting stays identical across exports.
    """
    base = build_lead_export_row(record)
    try:
        count = int(overlap.get("lists_count") or 0)
    except (TypeError, ValueError):
        count = 0  # never let a malformed count crash the export (Codex P2)
    row: dict[str, str] = {
        "overlap": "Overlap" if count >= 2 else "",
        "lists_count": str(count) if count else "",
        "lists": sanitize_for_csv(overlap.get("lists")),
        "counties": sanitize_for_csv(overlap.get("counties")),
        # .get not [] so a future builder rename can't raise here (Codex P2).
        "filed_date": base.get("date_recorded", ""),
    }
    for col in OVERLAP_LEAD_COLUMNS:
        if col not in row:
            row[col] = base.get(col, "")
    return row


def write_lead_csv_with_overlap(rows: list[tuple[Any, dict[str, Any]]], filelike) -> None:
    """Write the overlap/combine CSV. `rows` = iterable of (record, overlap_dict),
    already ordered by the caller (hottest-first). Header + rows, no footer."""
    writer = csv.DictWriter(filelike, fieldnames=OVERLAP_LEAD_COLUMNS)
    writer.writeheader()
    for record, overlap in rows:
        writer.writerow(build_overlap_export_row(record, overlap))
