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
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from src.api.middleware.security import sanitize_for_csv
from src.utils.lead_formatting import (
    normalize_phone_for_dialer,
    parse_property_for_display,
    split_owner_for_display,
)
from src.utils.lead_signals import auction_reference_date, derive_signals

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
    # Derived signals (Tier 0, src/utils/lead_signals.py): computed at export,
    # never stored. months_delinquent + wa_foreclosure_eligible are tax-only;
    # freshness_days + contactability_score apply to every record type.
    "months_delinquent", "wa_foreclosure_eligible",
    "freshness_days", "contactability_score",
    # Owner-location flags (Tier 0, migration 057): tri-state Yes/No/blank(unknown).
    # No property_state here — it already exists above as the dialer-split column.
    # (Those were the same value until migration 085: the split column now reads
    # the STORED property_state first and only falls back to parsing, so for a
    # street-only property_address they agree where it matters and the stored
    # value wins where the parse has nothing.)
    "absentee_owner", "out_of_state_owner", "owner_state",
    # NTS Tier 1 (migration 059): matched trustee-sale auction data (pre_foreclosure).
    # auction_date + default_amount are stored columns; trustee/ts# from
    # enrichment_data["nts"]; days_to_auction is the derived urgency clock.
    "auction_date", "days_to_auction", "default_amount", "trustee", "ts_number",
    # Probate honesty label (2026-06-23) — APPENDED at end (compatibility contract:
    # new columns are extra/appended so ordinal consumers of the existing fields do
    # not shift, Codex P2). Signal subtype set at insert: probate_death_inheritance
    # vs tod_living_owner_estate_planning vs nonprobate_transfer. Blank for non-probate.
    "lead_subtype",
    # Mailing-address split (2026-07-01, user request): same street/city/state/zip
    # split the property address already gets, for the owner's MAILING address
    # (direct-mail lists need it structured). Appended at END per this file's
    # backward-compat convention. Blank parts mean "couldn't parse confidently" —
    # the full mailing_address column above remains authoritative.
    "mailing_street", "mailing_city", "mailing_state", "mailing_zip",
    # Probate current-owner reconciliation (2026-07-04, user request): the King
    # Assessor's CURRENT owner/taxpayer vs the deceased party_name. Display-only —
    # current_owner is who holds title NOW (often an heir/trust); title_status is a
    # humble scan aid ("Different owner on title" / "Held by trust or entity" / blank).
    # King probate/death only; blank elsewhere. Appended at END (back-compat).
    "current_owner", "title_status",
]


# ── Lean per-record-type export profile ─────────────────────────────────────
# A SINGLE-record-type export drops the columns that record type can NEVER
# populate (a probate CSV shouldn't ship tax/code-violation/auction columns).
# This is STRUCTURAL, not data-driven: a column is dropped for a type only when
# no scraper/enrichment/derived-signal for that type can ever fill it. "All rows
# empty in this export" is NOT a reason to drop — that can mean failed enrichment,
# a view-filter excluding populated rows, or async data (skip-trace/NTS) not landed
# yet (Codex, consult 019f23f0). Combined/batch + segment exports intentionally
# keep the FULL superset (they merge multiple record types) — they never call the
# resolver. Unknown/new record types fall back to FULL so we never silently drop
# data. Only the FIELDNAMES are restricted; build_lead_export_row stays full-width.
#
# Column applicability verified against the scrapers/enrichment/signals:
#   heirs            -> BASE (NOT type-specific). It is a shared "secondary party"
#                       column that many recorded-document types populate: actual
#                       heirs (probate), the other spouse (divorce), AND the
#                       opposite party/company on a pre_foreclosure filing
#                       (orient_pre_foreclosure_party -> record.heirs in clark_wa,
#                       king_wa_probate, pierce_wa_probate). Because it can be
#                       populated by more types than a naive map predicts, it is
#                       kept for EVERY type (dropping it risked customer data loss —
#                       Codex review flagged pre_foreclosure specifically).
#   lead_subtype     -> probate only (set at insert ONLY when record_type=='probate',
#                       tasks.py; blank for every other type incl. death_certificate)
#   tax block        -> tax_delinquent (delinquent_* stored, tax_* enrichment,
#                       months_delinquent/wa_foreclosure_eligible derived)
#   code_violation_* -> code_violation enrichment
#   NTS/auction block-> pre_foreclosure (auction_date/default_amount stored,
#                       trustee/ts_number from enrichment_data["nts"], days_to_auction derived)
_TYPE_EXTRA_COLUMNS: dict[str, tuple[str, ...]] = {
    "probate": ("lead_subtype", "current_owner", "title_status"),
    "death_certificate": ("current_owner", "title_status"),
    "divorce": (),
    "eviction": (),
    "tax_delinquent": (
        "delinquent_amount", "delinquent_bill_year",
        "tax_billed_amount", "tax_paid_amount", "tax_account_status",
        "months_delinquent", "wa_foreclosure_eligible",
    ),
    "code_violation": (
        "code_violation_type", "code_violation_status",
        "code_violation_description", "code_violation_last_inspection",
    ),
    "pre_foreclosure": (
        "auction_date", "days_to_auction", "default_amount", "trustee", "ts_number",
    ),
    # trustee_sale (Auction Leads) is sourced FROM the NTS cache, so every row has the
    # same auction block as a matched pre_foreclosure lead. heirs/legal_description are
    # BASE (shared, not per-type droppable — see the note above); they stay present but
    # blank for auction leads, matching how pre_foreclosure handles them.
    "trustee_sale": (
        "auction_date", "days_to_auction", "default_amount", "trustee", "ts_number",
    ),
}

# BASE = columns common to EVERY type = the canonical set minus every column that
# is type-specific to SOME type. Derived from the map above so the two can't drift.
_ALL_TYPE_SPECIFIC_COLUMNS: frozenset[str] = frozenset(
    col for cols in _TYPE_EXTRA_COLUMNS.values() for col in cols
)
LEAN_BASE_COLUMNS: tuple[str, ...] = tuple(
    c for c in LEAD_CSV_COLUMNS if c not in _ALL_TYPE_SPECIFIC_COLUMNS
)


def resolve_lead_export_columns(record_type: str | None) -> list[str]:
    """Ordered CSV columns for a SINGLE-record-type (lean) export.

    Returns the canonical column order filtered to those a record type can ever
    populate. Unknown/None ``record_type`` returns the FULL ``LEAD_CSV_COLUMNS``
    (never silently drop data). The result is always an ordered subset of
    ``LEAD_CSV_COLUMNS`` — callers pass it as ``columns=`` to ``write_lead_csv`` /
    ``DataExporter``. Combined/batch/segment exports do NOT call this (superset).
    """
    if record_type not in _TYPE_EXTRA_COLUMNS:
        return list(LEAD_CSV_COLUMNS)
    allowed = set(LEAN_BASE_COLUMNS) | set(_TYPE_EXTRA_COLUMNS[record_type])
    return [c for c in LEAD_CSV_COLUMNS if c in allowed]


# User-controllable OUTPUT visibility (delivery/view preference — NOT scrape scope;
# the scraper always collects everything because the rest of the pipeline needs it).
# Only these three columns may be suppressed from the delivered file. The wizard's
# other four "identity" fields (party_name, parcel_id, property_address,
# date_recorded) are deliberately NOT hideable: they are what make a lead callable,
# mailable, and county-reconcilable, and several are load-bearing for
# enrichment/dedup/skip-trace upstream. Suppression BLANKS the value and KEEPS the
# header, so dialer/webhook consumers bound to a fixed column set never break.
HIDEABLE_OUTPUT_FIELDS: frozenset[str] = frozenset(
    {"mailing_address", "heirs", "legal_description"}
)

# Columns DERIVED from a hideable field: hiding the parent must blank these too,
# or the hide would leak the value through its split columns. Deliberately NOT in
# HIDEABLE_OUTPUT_FIELDS themselves — they are never independently selectable;
# they follow their parent.
_HIDEABLE_DEPENDENT_COLUMNS: dict[str, tuple[str, ...]] = {
    "mailing_address": ("mailing_street", "mailing_city", "mailing_state", "mailing_zip"),
}


def resolve_hidden_output_fields(config_fields: Any) -> set[str]:
    """Which hideable columns the user deselected on a scraper config.

    `config_fields` is the persisted `ScraperConfig.fields` JSON — a dict like
    ``{"party_name": True, ..., "legal_description": False}``. Legacy/empty values
    (None, [], {}) mean "show everything": only an EXPLICIT ``False`` on a hideable
    field suppresses it. Identity fields are never hideable, so a stray ``False`` on
    one is ignored here (the UI should lock them; the backend refuses regardless).
    """
    if not isinstance(config_fields, dict):
        return set()
    return {f for f in HIDEABLE_OUTPUT_FIELDS if config_fields.get(f) is False}


def _apply_visibility(row: dict[str, str], hidden_fields: set[str] | None) -> dict[str, str]:
    """Blank the deselected hideable columns in a built export row (header stays).

    Mutates and returns ``row``. Defensive: intersects with HIDEABLE_OUTPUT_FIELDS
    so a miswired caller can never blank an identity or derived column. A hidden
    field's dependent split columns (_HIDEABLE_DEPENDENT_COLUMNS) are blanked with
    it — hiding mailing_address must not leak it through mailing_street/city/….
    """
    if not hidden_fields:
        return row
    for col in hidden_fields & HIDEABLE_OUTPUT_FIELDS:
        for dep in (col, *_HIDEABLE_DEPENDENT_COLUMNS.get(col, ())):
            if dep in row:
                row[dep] = ""
    return row


def _yes_no_blank(val: object) -> str:
    """Tri-state boolean → Yes / No / '' (blank = unknown). Scannable in a dialer CSV."""
    if val is True:
        return "Yes"
    if val is False:
        return "No"
    return ""


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


def _nts(enr: dict) -> dict:
    """The nested 'nts' object the matcher wrote into enrichment_data (or empty)."""
    nts = enr.get("nts")
    return nts if isinstance(nts, dict) else {}


def _plain(val: Any) -> str:
    """Plain string for a stored date/numeric column ('' when None). A date renders
    ISO (2026-07-10), a Decimal renders without exponent — both dialer/sheet clean
    and digit-only after rendering, so no CSV-injection escaping is needed."""
    return "" if val in (None, "") else f"{val}"


def _enrich_str(data: dict, *keys: str) -> str:
    """Sanitized string value of the first present enrichment_data key, '' if none.

    Accepts multiple key aliases because supported scrapers store the same datum
    under different names (Codex review): the recorder instrument is
    `instrument_number` (King probate JSON) | `recording_number` (Clark, King
    LandmarkWeb) | `record_number` (King code-violation); the violation category
    is `record_type` (Seattle SDCI) | `case_type` (Tacoma/Pierce).
    """
    for key in keys:
        val = data.get(key)
        if val not in (None, ""):
            return sanitize_for_csv(val)
    return ""


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


def _is_synthetic_tax_date(raw_date: Any, bill_year: Any, record_type: Any = None) -> bool:
    """True iff date_recorded is the SYNTHETIC tax date the tax scrapers write —
    the exact string ``f"01/01/{bill_year}"`` (king_wa_tax_delinquent.py + Snohomish).

    Two guards, both required, so the fabricated tax date blanks but a REAL date never
    does (Codex):
      1. record_type: only a ``tax_delinquent`` row's date is synthetic. The batch
         combined export coalesces an overlapping tax hit's delinquent_bill_year onto
         a PROBATE representative row whose date is a real death-cert date — even a
         genuine Jan-1 death date must survive. When record_type is known and is NOT
         tax_delinquent, the date is real regardless of the string. ``None`` (per-job
         Result rows carry no record_type) falls through to the pattern check, keeping
         the existing per-job tax behavior.
      2. exact pattern: keying on ``01/01/{bill_year}`` (not merely "bill_year present")
         is what lets enrich.py's copied bill_year on a real-date duplicate row survive.
    A genuine tax row is unaffected — its date IS "01/01/{bill_year}", so it blanks.
    """
    if bill_year in (None, ""):
        return False
    if record_type is not None and record_type != "tax_delinquent":
        return False
    return str(raw_date).strip() == f"01/01/{bill_year}"


# Human labels for the probate title_status enum (classify_probate_title_status).
# Factual, NOT "transferred" — a name/entity difference is a signal to check, not deed
# proof (Codex). Unknown/empty enum -> blank cell.
_TITLE_STATUS_LABELS: dict[str, str] = {
    "current_owner_name_differs": "Different owner on title",
    "current_owner_entity_or_trust": "Held by trust or entity",
}


def build_lead_export_row(
    record: Any, today: date | None = None, *, auction_today: date | None = None
) -> dict[str, str]:
    """Build one canonical CSV row dict from an ORM Result or a plain dict.

    Parses raw name/address, THEN sanitizes each emitted value (never before
    parsing — escaping changes the string shape). Phones are normalized to bare
    10-digit (digits-only output is inherently CSV-injection-safe). Numerics are
    rendered plainly. Keys exactly match LEAD_CSV_COLUMNS. `today` is injected for
    the derived freshness/delinquency signals (defaults to UTC today) so exports
    are reproducible and tests don't freeze the clock.
    """
    if today is None:
        today = datetime.now(UTC).date()
    # Defaults to `today` so a caller that freezes ONE date still gets a deterministic
    # auction clock. The county-local date is injected by the CSV writers below, which
    # are the real entry points — this module never reads a hidden clock of its own.
    if auction_today is None:
        auction_today = today
    first, last = split_owner_for_display(_get(record, "party_name"))
    prop = parse_property_for_display(_get(record, "property_address"))
    # Same parser for the mailing address — it is address-generic (validated
    # state/zip, PO-Box/unit aware) despite the property-flavored name. Parts it
    # can't read confidently stay blank; the full mailing_address column is kept.
    mail = parse_property_for_display(_get(record, "mailing_address"))

    amt = _get(record, "delinquent_amount")
    year = _get(record, "delinquent_bill_year")
    enr = _enrichment(record)
    sig = derive_signals(record, today, auction_today=auction_today)

    return {
        # Tax-delinquent rows carry a SYNTHETIC date_recorded ("01/01/{bill_year}")
        # — county tax data has no real per-record event date. Emitting it as a real
        # `date_recorded` ships a fabricated event date into dialers/CRMs that sort,
        # dedupe, and trigger campaigns off it (Codex Critical). The honest tax
        # temporal signal is the separate `delinquent_bill_year` + derived
        # `months_delinquent` columns. `sig` above is derived from the record
        # (date_recorded intact), so only the emitted string is blanked. Blank ONLY
        # the exact synthetic "01/01/{bill_year}" string, not any row that merely has a
        # bill_year — a coalesced overlap row (probate death-cert date + tax bill_year)
        # keeps its real date (Codex).
        "date_recorded": (
            ""
            if _is_synthetic_tax_date(
                _get(record, "date_recorded"), year, _get(record, "record_type")
            )
            else sanitize_for_csv(_get(record, "date_recorded"))
        ),
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
        # Stored STRUCTURED situs first (migration 085), parsed second. These
        # columns hold what the source actually said the property's city/zip
        # are; parsing property_address is only an inference, and since #188
        # froze property_address to a street-only line for statewide- and
        # assessor-enriched rows, the parse now yields blanks for exactly the
        # rows the structured columns were added to describe. Falling back to
        # the parse keeps every pre-085 row exporting as it always did.
        "property_city": sanitize_for_csv(_get(record, "property_city") or prop["city"]),
        "property_state": sanitize_for_csv(_get(record, "property_state") or prop["state"]),
        "property_zip": sanitize_for_csv(_get(record, "property_zip") or prop["zip"]),
        # Enrichment passthrough (Tier 0): see _enrichment() for why these read
        # the JSON keys directly. Numeric fields rendered plainly; rest sanitized.
        "assessed_value": _enrich_num(enr, "assessed_value"),
        "instrument_number": _enrich_str(
            enr, "instrument_number", "recording_number", "record_number"
        ),
        # Probate honesty label (blank for non-probate). Per-job exports read it from
        # enrichment_data (set at insert in tasks.py); the combined/segment SQL
        # exporters build SimpleNamespaces that don't carry enrichment_data, so they
        # SELECT it as a top-level `lead_subtype` scalar — read whichever is present.
        "lead_subtype": _enrich_str(enr, "lead_subtype") or sanitize_for_csv(_get(record, "lead_subtype")),
        "code_violation_type": _enrich_str(enr, "record_type", "case_type"),
        "code_violation_status": _enrich_str(enr, "status"),
        "code_violation_description": _enrich_str(enr, "description"),
        "code_violation_last_inspection": _enrich_str(enr, "last_inspection"),
        "tax_billed_amount": _enrich_num(enr, "billed_amount"),
        "tax_paid_amount": _enrich_num(enr, "paid_amount"),
        "tax_account_status": _enrich_str(enr, "account_status"),
        # Derived signals — blank/Yes rendering keeps the dialer CSV scannable.
        "months_delinquent": "" if sig["months_delinquent"] is None else str(sig["months_delinquent"]),
        "wa_foreclosure_eligible": "Yes" if sig["wa_foreclosure_eligible"] else "",
        "freshness_days": "" if sig["freshness_days"] is None else str(sig["freshness_days"]),
        "contactability_score": str(sig["contactability_score"]),
        # Owner-location flags (stored columns 057): tri-state Yes/No/blank.
        "absentee_owner": _yes_no_blank(_get(record, "absentee_owner")),
        "out_of_state_owner": _yes_no_blank(_get(record, "out_of_state_owner")),
        "owner_state": sanitize_for_csv(_get(record, "owner_state")),
        # NTS auction data (059): stored cols + nts blob in enrichment_data.
        "auction_date": _plain(_get(record, "auction_date")),
        "days_to_auction": "" if sig["days_to_auction"] is None else str(sig["days_to_auction"]),
        "default_amount": _plain(_get(record, "default_amount")),
        "trustee": _enrich_str(_nts(enr), "trustee"),
        "ts_number": _enrich_str(_nts(enr), "ts_number"),
        # Mailing-address split (kept in sync with the property split above).
        "mailing_street": sanitize_for_csv(mail["street"]),
        "mailing_city": sanitize_for_csv(mail["city"]),
        "mailing_state": sanitize_for_csv(mail["state"]),
        "mailing_zip": sanitize_for_csv(mail["zip"]),
        # Probate current-owner reconciliation (King probate/death, display-only).
        # current_owner is the Assessor's owner NOW; title_status is a factual scan aid.
        "current_owner": _enrich_str(enr, "assessor_current_owner"),
        "title_status": _TITLE_STATUS_LABELS.get(enr.get("title_status"), ""),
    }


def write_lead_csv(
    records: list[Any], filelike, hidden_fields: set[str] | None = None,
    columns: list[str] | None = None,
) -> None:
    """Write the canonical lead CSV (header + rows) to an open text file/StringIO.

    No footer rows — the machine-import file stays clean (the DNC disclaimer lives
    in the delivery email + download UI). Caller owns opening/closing the stream.

    `hidden_fields` (from `resolve_hidden_output_fields`) blanks the user-deselected
    hideable columns; the header set is unchanged. None/empty = show everything.

    `columns` (from `resolve_lead_export_columns`) restricts the HEADER/fieldnames to
    a lean per-record-type subset. None => full `LEAD_CSV_COLUMNS`. The row is still
    built full-width; `extrasaction="ignore"` drops the keys not in `columns`, so the
    lean file and the full file share identical values for the columns they have in
    common (no separate builder, no drift).
    """
    # One consistent pair of "today"s for the whole file: UTC for the tax signals,
    # county-local for the auction countdown (lead_signals.AUCTION_TZ).
    today = datetime.now(UTC).date()
    auction_today = auction_reference_date()
    writer = csv.DictWriter(
        filelike, fieldnames=columns or LEAD_CSV_COLUMNS, extrasaction="ignore"
    )
    writer.writeheader()
    for rec in records:
        writer.writerow(_apply_visibility(
            build_lead_export_row(rec, today, auction_today=auction_today), hidden_fields
        ))


# Overlap/combine CSV (Lists page + batch scrape). Same dialer-ready semantics as
# the canonical row, with the overlap signal up front in "caller-first" order so a
# human opening it in Excel sees the hottest leads and the contact fields first.
# Reuses build_lead_export_row so the split/normalize/sanitize logic can never
# drift from the per-job export. Segments provide multi-contact phones/emails
# (decrypted arrays) so phone_2/3 + email_2/3 populate; columns the segment
# query doesn't provide (heirs, legal_description, doc_type, tax) come through
# blank — kept for header parity with the per-job CSV.
# Combined/segment exports dedup to ONE representative row per property, which may
# be a non-probate row even when the property also has a probate hit. Deriving
# lead_subtype from that representative row would blank it for those buckets (Codex
# P2). Instead aggregate the subtype across the bucket's probate candidates,
# preferring the stronger signal (death > nonprobate > tod > unknown). References
# the `lead_subtype` column selected in each query's candidates CTE; goes in the agg
# CTE. NULL when the bucket has no probate row -> exported blank.
PROBATE_SUBTYPE_AGG_SQL: str = (
    "(array_agg(lead_subtype ORDER BY CASE lead_subtype "
    "WHEN 'probate_death_inheritance' THEN 1 "
    "WHEN 'nonprobate_transfer' THEN 2 "
    "WHEN 'tod_living_owner_estate_planning' THEN 3 "
    "ELSE 4 END) "
    "FILTER (WHERE lead_subtype IS NOT NULL AND lead_subtype <> ''))[1] AS lead_subtype"
)


OVERLAP_LEAD_COLUMNS: list[str] = [
    "overlap", "lists_count", "lists", "counties",
    "first_name", "last_name",
    "phone", "phone_type", "email", "phone_2", "phone_3", "email_2", "email_3",
    "property_street", "property_city", "property_state", "property_zip",
    "filed_date", "doc_type", "delinquent_amount", "delinquent_bill_year",
    "party_name", "mailing_address", "parcel_id", "heirs", "legal_description",
    "property_address",
    "lead_subtype",  # appended at end (compatibility contract, Codex P2)
    # Mailing split — appended at end (same back-compat convention as the
    # canonical CSV); values auto-copied from the canonical row below.
    "mailing_street", "mailing_city", "mailing_state", "mailing_zip",
]


def build_overlap_export_row(
    record: Any,
    overlap: dict[str, Any],
    hidden_fields: set[str] | None = None,
    *,
    today: date | None = None,
    auction_today: date | None = None,
) -> dict[str, str]:
    """One overlap-CSV row from a lead record + its overlap metadata.

    `overlap` carries `lists_count` (distinct record types this property is on),
    `lists` (human-readable, already "; "-joined), and `counties`. The `overlap`
    flag is the WORD "Overlap" when on 2+ lists, else blank (more scannable than
    TRUE/FALSE). All other fields come straight from the canonical dialer-ready
    row so formatting stays identical across exports.

    `hidden_fields` (from the batch's `fields`) blanks the user-deselected hideable
    columns; the header set is unchanged, matching the per-job CSV's behavior.
    """
    base = _apply_visibility(
        build_lead_export_row(record, today, auction_today=auction_today), hidden_fields
    )
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


def write_lead_csv_with_overlap(
    rows: list[tuple[Any, dict[str, Any]]], filelike, hidden_fields: set[str] | None = None
) -> None:
    """Write the overlap/combine CSV. `rows` = iterable of (record, overlap_dict),
    already ordered by the caller (hottest-first). Header + rows, no footer.

    `hidden_fields` (from the batch's shared `fields`) blanks the user-deselected
    hideable columns, keeping the combined CSV consistent with each per-job export.
    """
    # Freeze both clocks once per file, exactly like write_lead_csv — this path used
    # to fall through to a per-ROW now(), so a long combined export could straddle a
    # date boundary and emit two different countdowns for the same auction (Codex).
    today = datetime.now(UTC).date()
    auction_today = auction_reference_date()
    writer = csv.DictWriter(filelike, fieldnames=OVERLAP_LEAD_COLUMNS)
    writer.writeheader()
    for record, overlap in rows:
        writer.writerow(build_overlap_export_row(
            record, overlap, hidden_fields, today=today, auction_today=auction_today
        ))
