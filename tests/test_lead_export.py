"""Golden tests for the canonical lead-CSV builder (src/utils/lead_export.py).

Covers BOTH input shapes the two export paths use: ORM-like objects (live download)
and plain dicts (scheduled/R2 export), incl. secondary contacts from either the
`phones`/`emails` arrays OR flattened `phone_2`/`email_2` keys.
"""
import io

from src.utils.lead_export import (
    LEAD_CSV_COLUMNS,
    build_lead_export_row,
    write_lead_csv,
)


class _Obj:
    """Minimal ORM-like record (attribute access)."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
    def __getattr__(self, _):  # missing attrs -> None, like a sparse ORM row
        return None


class TestBuildRow:
    def test_keys_match_columns_exactly(self):
        row = build_lead_export_row({})
        assert set(row.keys()) == set(LEAD_CSV_COLUMNS)

    def test_dict_with_phones_array(self):
        rec = {
            "party_name": "SMITH JOHN",
            "property_address": "123 MAIN ST, TACOMA, WA 98401",
            "phone": "(206) 555-1234",
            "phones": [
                {"number": "(206) 555-1234", "type": "Mobile"},
                {"number": "253-555-9876", "type": "Landline"},
                {"number": "1-360-555-0000", "type": "Mobile"},
            ],
            "emails": ["a@x.com", "b@x.com", "c@x.com"],
        }
        row = build_lead_export_row(rec)
        assert row["first_name"] == "JOHN" and row["last_name"] == "SMITH"
        assert row["property_city"] == "TACOMA" and row["property_state"] == "WA"
        # all phones normalized to bare 10-digit
        assert row["phone"] == "2065551234"
        assert row["phone_2"] == "2535559876"
        assert row["phone_3"] == "3605550000"  # leading country code dropped
        assert row["email_2"] == "b@x.com" and row["email_3"] == "c@x.com"

    def test_dict_with_flattened_secondary_keys(self):
        # No phones/emails arrays — secondary contacts only as flattened keys.
        rec = {
            "party_name": "DOE JANE",
            "phone": "2065551234",
            "phone_2": "253-555-0001",
            "email": "j@x.com",
            "email_2": "j2@x.com",
        }
        row = build_lead_export_row(rec)
        assert row["phone_2"] == "2535550001"
        assert row["email_2"] == "j2@x.com"
        assert row["phone_3"] == "" and row["email_3"] == ""

    def test_orm_like_object(self):
        rec = _Obj(
            party_name="VAN DYKE JOHN",
            property_address="500 PINE ST SEATTLE WA 98101",
            phone="206.555.7777",
            delinquent_amount=None,
        )
        row = build_lead_export_row(rec)
        assert row["first_name"] == "JOHN" and row["last_name"] == "VAN DYKE"
        assert row["phone"] == "2065557777"
        assert row["property_state"] == "WA" and row["property_zip"] == "98101"
        assert row["delinquent_amount"] == ""

    def test_entity_blanks_name(self):
        row = build_lead_export_row({"party_name": "ACME HOMES LLC"})
        assert row["first_name"] == "" and row["last_name"] == ""
        assert row["party_name"] == "ACME HOMES LLC"  # full name retained

    def test_invalid_phone_blanks(self):
        assert build_lead_export_row({"phone": "555-1234"})["phone"] == ""


class TestEnrichmentPassthrough:
    """Tier 0: structured enrichment_data we scrape but used to drop from the CSV."""

    def test_code_violation_fields_exported(self):
        rec = {
            "party_name": "DISTRESSED OWNER",
            "property_address": "456 OAK AVE, SEATTLE, WA 98101",
            "enrichment_data": {
                "source": "seattle_sdci_code_violations",
                "record_type": "Housing/Building",
                "status": "Open",
                "description": "Vacant building, unsecured; structural hazard",
                "last_inspection": "2026-05-01",
                "latitude": 47.6,  # captured but intentionally NOT a column
            },
        }
        row = build_lead_export_row(rec)
        assert row["code_violation_type"] == "Housing/Building"
        assert row["code_violation_status"] == "Open"
        assert row["code_violation_description"].startswith("Vacant building")
        assert row["code_violation_last_inspection"] == "2026-05-01"
        # tax columns stay blank for a non-tax row
        assert row["tax_billed_amount"] == "" and row["tax_account_status"] == ""

    def test_tax_fields_exported_and_numeric_normalized(self):
        rec = {
            "enrichment_data": {
                "source": "king_county_delinquent_taxes",
                "billed_amount": "5400.00",
                "paid_amount": 1200,
                "account_status": "DELINQUENT",
            },
        }
        row = build_lead_export_row(rec)
        assert row["tax_billed_amount"] == "5400.00"
        assert row["tax_paid_amount"] == "1200"
        assert row["tax_account_status"] == "DELINQUENT"
        assert row["code_violation_type"] == ""  # not a code-violation row

    def test_assessed_value_strips_currency_formatting(self):
        row = build_lead_export_row({"enrichment_data": {"assessed_value": "$325,000"}})
        assert row["assessed_value"] == "325000"

    def test_instrument_number_passthrough(self):
        row = build_lead_export_row({"enrichment_data": {"instrument_number": "20260101001234"}})
        assert row["instrument_number"] == "20260101001234"

    def test_instrument_number_key_aliases(self):
        # Clark / King LandmarkWeb store the instrument under recording_number;
        # King code-violation under record_number (Codex review).
        assert build_lead_export_row(
            {"enrichment_data": {"recording_number": "REC-9"}}
        )["instrument_number"] == "REC-9"
        assert build_lead_export_row(
            {"enrichment_data": {"record_number": "CV-42"}}
        )["instrument_number"] == "CV-42"

    def test_violation_type_case_type_alias(self):
        # Tacoma/Pierce store the violation category under case_type, not record_type.
        row = build_lead_export_row(
            {"enrichment_data": {"source": "tacoma_code_violations",
                                 "case_type": "Junk/Debris", "status": "Active"}}
        )
        assert row["code_violation_type"] == "Junk/Debris"
        assert row["code_violation_status"] == "Active"

    def test_missing_enrichment_blanks_all(self):
        row = build_lead_export_row({"party_name": "X"})
        for col in (
            "assessed_value", "instrument_number", "code_violation_type",
            "code_violation_status", "code_violation_description",
            "code_violation_last_inspection", "tax_billed_amount",
            "tax_paid_amount", "tax_account_status",
        ):
            assert row[col] == "", f"{col} should be blank with no enrichment_data"

    def test_malformed_enrichment_data_does_not_raise(self):
        # enrichment_data is sometimes None or (defensively) a non-dict
        for bad in (None, [], "oops", 42):
            row = build_lead_export_row({"enrichment_data": bad})
            assert row["assessed_value"] == ""

    def test_orm_object_enrichment(self):
        obj = _Obj(enrichment_data={"assessed_value": 410000, "status": "Closed"})
        row = build_lead_export_row(obj)
        assert row["assessed_value"] == "410000"
        assert row["code_violation_status"] == "Closed"

    def test_csv_injection_sanitized_in_passthrough(self):
        row = build_lead_export_row(
            {"enrichment_data": {"description": "=cmd|'/c calc'!A1", "account_status": "@evil"}}
        )
        assert not row["code_violation_description"].startswith("=")
        assert not row["tax_account_status"].startswith("@")


class TestDerivedSignalColumns:
    """Tier 0: months_delinquent / wa_foreclosure_eligible / freshness / contactability."""

    def test_tax_row_signals_with_injected_today(self):
        from datetime import date
        rec = {
            "delinquent_bill_year": 2022,
            "date_recorded": "06/01/2026",
            "phone": "2065551234",
            "emails": ["a@x.com"],
        }
        row = build_lead_export_row(rec, today=date(2026, 6, 12))
        assert row["months_delinquent"] != ""  # populated for a tax row
        assert row["wa_foreclosure_eligible"] == "Yes"  # 2022 <= 2026-3
        assert row["freshness_days"] == "11"
        assert row["contactability_score"] == "2"  # 1 phone + 1 email

    def test_non_tax_row_blank_tax_signals(self):
        from datetime import date
        row = build_lead_export_row({"party_name": "X"}, today=date(2026, 6, 12))
        assert row["months_delinquent"] == ""
        assert row["wa_foreclosure_eligible"] == ""
        assert row["contactability_score"] == "0"  # always present, never blank


class TestTaxRowDateBlanked:
    """Tax-delinquent rows have NO real per-record event date — the scraper stores a
    SYNTHETIC date_recorded ("01/01/{bill_year}"). It must NOT ship into the CSV as a
    real event date (dialers/CRMs sort, dedupe, and trigger campaigns off it). The
    honest temporal signal is the separate delinquent_bill_year + months_delinquent
    columns; non-tax rows keep their real date_recorded untouched."""

    def test_tax_row_date_recorded_blanked_but_signals_intact(self):
        from datetime import date
        rec = {
            "delinquent_bill_year": 2024,
            "date_recorded": "01/01/2024",  # synthetic, no real event happened
            "parcel_id": "1234567890",
        }
        row = build_lead_export_row(rec, today=date(2026, 6, 15))
        assert row["date_recorded"] == ""  # fabricated date stripped from export
        assert row["delinquent_bill_year"] == "2024"  # honest year kept
        assert row["months_delinquent"] != ""  # derived from bill_year, unaffected

    def test_non_tax_row_date_recorded_preserved(self):
        # No delinquent_bill_year => a real filing date flows through unchanged.
        row = build_lead_export_row({"date_recorded": "03/15/2026", "doc_type": "probate"})
        assert row["date_recorded"] == "03/15/2026"


class TestOwnerFlagColumns:
    """Tier 0 (057): absentee / out_of_state / owner_state tri-state CSV columns."""

    def test_absentee_yes_no_blank(self):
        assert build_lead_export_row({"absentee_owner": True})["absentee_owner"] == "Yes"
        assert build_lead_export_row({"absentee_owner": False})["absentee_owner"] == "No"
        assert build_lead_export_row({"absentee_owner": None})["absentee_owner"] == ""
        assert build_lead_export_row({})["absentee_owner"] == ""  # absent -> blank

    def test_out_of_state_and_owner_state(self):
        row = build_lead_export_row({"out_of_state_owner": True, "owner_state": "OR"})
        assert row["out_of_state_owner"] == "Yes"
        assert row["owner_state"] == "OR"

    def test_no_duplicate_property_state_column(self):
        # property_state exists ONCE (the dialer-split column), not duplicated
        assert LEAD_CSV_COLUMNS.count("property_state") == 1
        assert "owner_state" in LEAD_CSV_COLUMNS


class TestWriteCsv:
    def test_header_and_rows_no_footer(self):
        out = io.StringIO()
        write_lead_csv(
            [{"party_name": "SMITH JOHN", "phone": "2065551234"}], out
        )
        text = out.getvalue()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        header = lines[0].split(",")
        assert header == LEAD_CSV_COLUMNS
        assert len(lines) == 2  # header + 1 data row, NO disclaimer footer
        assert not any(ln.startswith("#") for ln in lines)

    def test_empty_records_header_only(self):
        out = io.StringIO()
        write_lead_csv([], out)
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1 and lines[0].split(",") == LEAD_CSV_COLUMNS


class TestNtsAuctionColumns:
    """NTS Tier 1 (059): auction_date / days_to_auction / default_amount / trustee / ts#."""

    def test_auction_fields_from_columns_and_nts_blob(self):
        from datetime import date
        rec = {
            "auction_date": date(2026, 7, 10),
            "default_amount": __import__("decimal").Decimal("185895.06"),
            "enrichment_data": {"nts": {"trustee": "North Star Trustee, LLC", "ts_number": "25-76127"}},
        }
        row = build_lead_export_row(rec, today=date(2026, 6, 12))
        assert row["auction_date"] == "2026-07-10"
        assert row["days_to_auction"] == "28"
        assert row["default_amount"] == "185895.06"
        assert row["trustee"] == "North Star Trustee, LLC"
        assert row["ts_number"] == "25-76127"

    def test_blank_when_no_auction(self):
        row = build_lead_export_row({"party_name": "X"})
        for col in ("auction_date", "days_to_auction", "default_amount", "trustee", "ts_number"):
            assert row[col] == "", col
