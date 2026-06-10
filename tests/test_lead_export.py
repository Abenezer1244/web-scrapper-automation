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
