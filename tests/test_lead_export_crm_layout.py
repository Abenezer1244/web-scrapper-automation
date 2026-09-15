"""crm_v1 export layout (src/utils/lead_export.py).

Pure tests (no DB). They read the WRITTEN CSV back with the csv module and assert
cell values, not just header names: one lead is one row, each value sits under its
CRM header, and nothing is invented to fill a column.
"""
import csv
import io

import pytest

from src.utils.lead_export import (
    CRM_V1_LABELS,
    LAYOUT_CRM_V1,
    LAYOUT_LEGACY_V1,
    LEAD_CSV_COLUMNS,
    name_order_for,
    resolve_export_layout,
    resolve_lead_export_columns,
    write_lead_csv,
)

CORE_HEADER = [
    "First Name", "Last Name", "Party Name",
    "Property Address", "Property City", "Property State", "Property Zip",
    "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
    "Phone 1", "Phone 2", "Phone 3", "Email 1", "Email 2", "Email 3",
    "Parcel ID", "County", "County State", "Record Type", "Date Recorded",
]
REFERENCE_HEADER = [
    "Heirs", "Full Property Address", "Full Mailing Address", "Legal Description",
    "Doc Type", "Instrument Number", "Phone 1 Type", "Assessed Value", "Absentee Owner",
    "Out Of State Owner", "Owner State", "Freshness Days", "Contactability Score",
]


def _export(records, record_type, context=None, hidden_fields=None, layout=LAYOUT_CRM_V1):
    columns, labels = resolve_export_layout(layout, record_type)
    buf = io.StringIO(newline="")
    write_lead_csv(
        records, buf, hidden_fields=hidden_fields, columns=columns,
        labels=labels, context=context,
    )
    rows = list(csv.reader(io.StringIO(buf.getvalue(), newline="")))
    header, body = rows[0], rows[1:]
    return header, [dict(zip(header, r, strict=True)) for r in body], buf.getvalue()


KING_PREFC_CTX = {"county": "king", "state": "WA", "record_type": "pre_foreclosure"}


def _prefc_lead(**overrides):
    lead = {
        "party_name": "SMITH JOHN / SMITH JANE",
        "property_address": "123 MAIN ST",  # street-only frozen key (post-#188)
        "property_city": "SEATTLE", "property_state": "WA", "property_zip": "98101",
        "mailing_address": "PO BOX 500, BELLEVUE, WA 98004",
        "phone": "2065551111",
        "phones": [
            {"number": "2065551111", "type": "Mobile"},
            {"number": "(425) 555-2222", "type": "Landline"},
        ],
        "email": "john@example.com",
        "emails": ["john@example.com"],
        "parcel_id": "0007200015",
        "date_recorded": "09/01/2026",
        "default_amount": "296494.82",
        "enrichment_data": {"nts": {"trustee": "QUALITY LOAN SERVICE CORP", "ts_number": "WA-26-1"}},
    }
    lead.update(overrides)
    return lead


class TestHeaderOrder:
    def test_pre_foreclosure_header_exact(self):
        header, _, _ = _export([], "pre_foreclosure")
        assert header == CORE_HEADER + [
            "Auction Date", "Days To Auction", "Principal Owing", "Trustee", "TS Number",
        ] + REFERENCE_HEADER

    def test_trustee_sale_shares_auction_block(self):
        assert _export([], "trustee_sale")[0] == _export([], "pre_foreclosure")[0]

    def test_tax_delinquent_header_exact(self):
        header, _, _ = _export([], "tax_delinquent")
        assert header == CORE_HEADER + [
            "Tax Balance Owed", "Oldest Tax Year", "Months Delinquent",
            "WA Foreclosure Eligible", "Tax Billed Amount", "Tax Paid Amount",
            "Tax Account Status",
        ] + REFERENCE_HEADER

    def test_code_violation_header_exact(self):
        header, _, _ = _export([], "code_violation")
        assert header == CORE_HEADER + [
            "Case ID", "Violation Type", "Violation Status", "Violation Description",
            "Last Inspection", "Parcel Source",
        ] + REFERENCE_HEADER

    def test_probate_and_death_certificate_headers(self):
        assert _export([], "probate")[0] == CORE_HEADER + [
            "Lead Subtype", "Current Owner", "Title Status",
        ] + REFERENCE_HEADER
        assert _export([], "death_certificate")[0] == CORE_HEADER + [
            "Current Owner", "Title Status",
        ] + REFERENCE_HEADER

    def test_divorce_has_core_and_reference_only(self):
        assert _export([], "divorce")[0] == CORE_HEADER + REFERENCE_HEADER

    def test_unknown_record_type_keeps_every_block(self):
        header, _, _ = _export([], None)
        for label in ("Principal Owing", "Tax Balance Owed", "Case ID", "Lead Subtype"):
            assert label in header
        assert len(header) == len(set(header))  # no duplicated column

    def test_order_is_stable_across_calls(self):
        assert _export([], "probate")[0] == _export([], "probate")[0]

    def test_labels_cover_every_resolved_key(self):
        for rt in (None, "probate", "tax_delinquent", "code_violation", "pre_foreclosure"):
            columns, labels = resolve_export_layout(LAYOUT_CRM_V1, rt)
            assert set(columns) <= set(labels)
        assert set(CRM_V1_LABELS.values()) >= set(CORE_HEADER)


class TestLegacyLayoutUnchanged:
    def test_legacy_and_unknown_layouts_resolve_to_legacy_headers(self):
        for layout in (LAYOUT_LEGACY_V1, None, "", "crm_v9"):
            columns, labels = resolve_export_layout(layout, "probate")
            assert labels is None
            assert columns == resolve_lead_export_columns("probate")

    def test_legacy_csv_header_is_the_snake_case_contract(self):
        header, _, _ = _export([], None, layout=LAYOUT_LEGACY_V1)
        assert header == LEAD_CSV_COLUMNS


class TestCoreValues:
    def test_one_lead_is_one_row_with_separate_columns(self):
        _, rows, _ = _export([_prefc_lead()], "pre_foreclosure", context=KING_PREFC_CTX)
        assert len(rows) == 1
        row = rows[0]
        assert (row["First Name"], row["Last Name"]) == ("JOHN", "SMITH")
        assert row["Party Name"] == "SMITH JOHN / SMITH JANE"  # all owners preserved
        assert (row["Property Address"], row["Property City"], row["Property State"],
                row["Property Zip"]) == ("123 MAIN ST", "SEATTLE", "WA", "98101")
        assert (row["Mailing Address"], row["Mailing City"], row["Mailing State"],
                row["Mailing Zip"]) == ("PO BOX 500", "BELLEVUE", "WA", "98004")
        assert (row["Phone 1"], row["Phone 2"], row["Phone 3"]) == (
            "2065551111", "4255552222", "")
        assert (row["Email 1"], row["Email 2"], row["Email 3"]) == ("john@example.com", "", "")
        assert row["Parcel ID"] == "0007200015"
        assert (row["County"], row["County State"], row["Record Type"]) == (
            "King", "WA", "Pre-Foreclosure")
        assert row["Date Recorded"] == "09/01/2026"
        assert row["Principal Owing"] == "296494.82"
        assert row["Trustee"] == "QUALITY LOAN SERVICE CORP" and row["TS Number"] == "WA-26-1"
        assert row["Full Property Address"] == "123 MAIN ST"
        assert row["Full Mailing Address"] == "PO BOX 500, BELLEVUE, WA 98004"
        assert row["Phone 1 Type"] == ""  # only phone_type is the Phone 1 type column

    def test_three_phones_and_emails_in_slot_order(self):
        lead = _prefc_lead(
            phones=[{"number": "2065551111", "type": "Mobile"},
                    {"number": "4255552222", "type": "Mobile"},
                    {"number": "+1 253-555-3333", "type": "Landline"}],
            emails=["a@x.com", "b@x.com", "c@x.com"], email="a@x.com",
        )
        row = _export([lead], "pre_foreclosure", context=KING_PREFC_CTX)[1][0]
        assert (row["Phone 1"], row["Phone 2"], row["Phone 3"]) == (
            "2065551111", "4255552222", "2535553333")
        assert (row["Email 1"], row["Email 2"], row["Email 3"]) == ("a@x.com", "b@x.com", "c@x.com")

    def test_leading_zeros_survive_as_text(self):
        lead = _prefc_lead(parcel_id="0040000055", property_zip="00501")
        _, rows, raw = _export([lead], "pre_foreclosure", context=KING_PREFC_CTX)
        assert rows[0]["Parcel ID"] == "0040000055"
        assert rows[0]["Property Zip"] == "00501"
        assert ",0040000055," in raw  # no ="..." wrapper, no apostrophe, no float

    def test_zip_plus_four_kept(self):
        lead = _prefc_lead(mailing_address="4002 22ND ST SE, PUYALLUP, WA, 98374-4108")
        row = _export([lead], "pre_foreclosure", context=KING_PREFC_CTX)[1][0]
        assert row["Mailing Zip"] == "98374-4108"

    def test_missing_mailing_is_never_copied_from_property(self):
        lead = _prefc_lead(mailing_address=None)
        row = _export([lead], "pre_foreclosure", context=KING_PREFC_CTX)[1][0]
        for col in ("Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
                    "Full Mailing Address"):
            assert row[col] == "", col
        assert row["Property Address"] == "123 MAIN ST"

    def test_missing_property_address_leaves_parts_blank(self):
        lead = _prefc_lead(property_address=None, property_city=None,
                           property_state=None, property_zip=None)
        row = _export([lead], "pre_foreclosure", context=KING_PREFC_CTX)[1][0]
        for col in ("Property Address", "Property City", "Property State", "Property Zip"):
            assert row[col] == "", col

    def test_international_mailing_keeps_original_and_blank_parts(self):
        lead = _prefc_lead(mailing_address="716-42 WESTERN BATTERY RD\nTORONTO ON\nM6K3P1\nCANADA")
        row = _export([lead], "pre_foreclosure", context=KING_PREFC_CTX)[1][0]
        # CR/LF are flattened by the CSV sanitizer; the text itself is intact.
        assert row["Mailing Address"] == "716-42 WESTERN BATTERY RD TORONTO ON M6K3P1 CANADA"
        assert row["Mailing City"] == row["Mailing State"] == row["Mailing Zip"] == ""

    def test_entity_owner_keeps_party_name_and_blank_first_last(self):
        lead = _prefc_lead(party_name="ABC HOLDINGS LLC")
        row = _export([lead], "pre_foreclosure", context=KING_PREFC_CTX)[1][0]
        assert row["First Name"] == row["Last Name"] == ""
        assert row["Party Name"] == "ABC HOLDINGS LLC"

    def test_empty_record_emits_blank_cells_not_placeholders(self):
        row = _export([{}], "probate", context={"county": "king", "state": "WA",
                                                "record_type": "probate"})[1][0]
        for col in CORE_HEADER:
            if col in ("County", "County State", "Record Type"):
                continue
            assert row[col] == "", col


class TestSourceAwareNames:
    def test_trustee_sale_natural_order(self):
        lead = _prefc_lead(party_name="SHIRLEY A JOHNSON")
        ctx = {"county": "pierce", "state": "WA", "record_type": "trustee_sale"}
        row = _export([lead], "trustee_sale", context=ctx)[1][0]
        assert (row["First Name"], row["Last Name"]) == ("SHIRLEY", "JOHNSON")

    def test_record_value_wins_over_context(self):
        # A batch/Lists row carries its own record_type; the context must not override it.
        lead = _prefc_lead(party_name="SHIRLEY A JOHNSON", record_type="trustee_sale",
                           county="snohomish")
        row = _export([lead], None, context=KING_PREFC_CTX)[1][0]
        assert (row["First Name"], row["Last Name"]) == ("SHIRLEY", "JOHNSON")
        assert row["Record Type"] == "Trustee Sale" and row["County"] == "Snohomish"

    def test_no_source_context_blanks_first_last(self):
        row = _export([_prefc_lead()], "pre_foreclosure", context=None)[1][0]
        assert row["First Name"] == row["Last Name"] == ""
        assert row["County"] == row["Record Type"] == ""

    @pytest.mark.parametrize("record_type, county, expected", [
        ("probate", "king", "recorder"),
        ("pre_foreclosure", "king", "recorder"),
        ("pre_foreclosure", "Snohomish", "natural"),
        ("trustee_sale", "king", "natural"),
        ("probate", "okanogan", "comma_only"),
        ("code_violation", "pierce", None),
        ("code_violation", "king", "recorder"),
        ("eviction", "king", None),
        (None, None, None),
    ])
    def test_name_order_map(self, record_type, county, expected):
        assert name_order_for(record_type, county) == expected


class TestRecordTypeValues:
    def test_tax_delinquent_values(self):
        lead = {
            "party_name": "CISSNA RICHARD C/KATHRYN A",
            "parcel_id": "0268000490",
            "date_recorded": "01/01/2019",  # synthetic tax date -> blanked
            "delinquent_amount": "1234.50", "delinquent_bill_year": 2019,
            "enrichment_data": {"billed_amount": "$2,000.00", "paid_amount": "765.50",
                                "account_status": "Delinquent"},
        }
        ctx = {"county": "snohomish", "state": "WA", "record_type": "tax_delinquent"}
        row = _export([lead], "tax_delinquent", context=ctx)[1][0]
        assert (row["First Name"], row["Last Name"]) == ("RICHARD", "CISSNA")
        assert row["Tax Balance Owed"] == "1234.50"   # plain machine number
        assert row["Oldest Tax Year"] == "2019"
        assert row["Tax Billed Amount"] == "2000.00"  # $ and comma stripped
        assert row["Tax Paid Amount"] == "765.50"
        assert row["Tax Account Status"] == "Delinquent"
        assert row["Date Recorded"] == ""
        assert row["Parcel ID"] == "0268000490"

    def test_king_code_violation_values(self):
        lead = {
            "party_name": "GOSS WESLEY+MARIE",
            "property_address": "9259 42ND AVE S, SEATTLE WA 98118",
            "enrichment_data": {"record_number": "1052183-CN", "violation_category": "Vacant Building",
                                "record_type": "Complaint", "status": "Open",
                                "description": "Vacant, open to entry", "last_inspection": "2026-08-01"},
        }
        ctx = {"county": "king", "state": "WA", "record_type": "code_violation"}
        row = _export([lead], "code_violation", context=ctx)[1][0]
        assert (row["First Name"], row["Last Name"]) == ("WESLEY", "GOSS")
        assert row["Case ID"] == "1052183-CN"
        assert row["Violation Type"] == "Vacant Building"
        assert row["Violation Status"] == "Open"
        assert row["Violation Description"] == "Vacant, open to entry"
        assert row["Last Inspection"] == "2026-08-01"

    def test_pierce_code_violation_case_number_and_no_fake_name(self):
        lead = {
            "party_name": "Nuisance - 3711 S D ST",
            "enrichment_data": {"case_number": "CE26-0042", "case_type": "Nuisance"},
        }
        ctx = {"county": "pierce", "state": "WA", "record_type": "code_violation"}
        row = _export([lead], "code_violation", context=ctx)[1][0]
        assert row["Case ID"] == "CE26-0042"
        assert row["Violation Type"] == "Nuisance"
        assert row["First Name"] == row["Last Name"] == ""

    def test_probate_values(self):
        lead = {
            "party_name": "JOHNSON WILLIAM EST OF",
            "heirs": "JOHNSON MARY",
            "enrichment_data": {"lead_subtype": "probate_death_inheritance",
                                "assessor_current_owner": "JOHNSON MARY",
                                "title_status": "current_owner_name_differs"},
        }
        ctx = {"county": "pierce", "state": "WA", "record_type": "probate"}
        row = _export([lead], "probate", context=ctx)[1][0]
        assert (row["First Name"], row["Last Name"]) == ("WILLIAM", "JOHNSON")
        assert row["Lead Subtype"] == "probate_death_inheritance"
        assert row["Current Owner"] == "JOHNSON MARY"
        assert row["Title Status"] == "Different owner on title"
        assert row["Heirs"] == "JOHNSON MARY"

    def test_death_certificate_values(self):
        lead = {"party_name": "KAUR RAJWANT / PUBLIC",
                "enrichment_data": {"assessor_current_owner": "SINGH HARPREET"}}
        ctx = {"county": "king", "state": "WA", "record_type": "death_certificate"}
        row = _export([lead], "death_certificate", context=ctx)[1][0]
        assert (row["First Name"], row["Last Name"]) == ("RAJWANT", "KAUR")
        assert row["Current Owner"] == "SINGH HARPREET"
        assert row["Record Type"] == "Death Certificate"

    def test_auction_date_and_amount(self):
        from datetime import date
        from decimal import Decimal

        lead = _prefc_lead(auction_date=date(2026, 10, 30), default_amount=Decimal("1500.00"))
        ctx = {"county": "snohomish", "state": "WA", "record_type": "trustee_sale"}
        row = _export([lead], "trustee_sale", context=ctx)[1][0]
        assert row["Auction Date"] == "2026-10-30"
        assert row["Principal Owing"] == "1500.00"
        assert row["Days To Auction"].lstrip("-").isdigit()


class TestEscapingAndInjection:
    def test_commas_quotes_apostrophes_round_trip(self):
        lead = _prefc_lead(party_name='O\'BRIEN, "SEAN" PATRICK',
                           mailing_address="1 A ST, STE 2, SEATTLE, WA 98101")
        _, rows, _ = _export([lead], "pre_foreclosure", context=KING_PREFC_CTX)
        assert len(rows) == 1
        assert rows[0]["Party Name"] == 'O\'BRIEN, "SEAN" PATRICK'
        assert rows[0]["Mailing Address"] == "1 A ST, STE 2"

    def test_embedded_newline_cannot_split_the_row(self):
        lead = _prefc_lead(party_name="SMITH JOHN\r\nEVIL ROW", legal_description="LOT 1\nBLK 2")
        _, rows, raw = _export([lead], "pre_foreclosure", context=KING_PREFC_CTX)
        assert len(rows) == 1
        assert rows[0]["Party Name"] == "SMITH JOHN  EVIL ROW"
        assert raw.count("\r\n") == 2  # header + one row, no stray line breaks

    @pytest.mark.parametrize("field, column", [
        ("party_name", "Party Name"),
        ("email", "Email 1"),
        ("mailing_address", "Full Mailing Address"),
        ("parcel_id", "Parcel ID"),
    ])
    @pytest.mark.parametrize("payload", ["=HYPERLINK(\"http://x\")", "+SUM(A1)", "-2+3", "@cmd",
                                         " =1+1", "\"=1+1"])
    def test_formula_triggers_neutralized(self, field, column, payload):
        lead = _prefc_lead(**{field: payload, "emails": [payload] if field == "email" else []})
        row = _export([lead], "pre_foreclosure", context=KING_PREFC_CTX)[1][0]
        assert row[column].startswith("'"), row[column]

    def test_header_labels_are_not_formula_triggers(self):
        for label in CRM_V1_LABELS.values():
            assert not label.startswith(("=", "+", "-", "@"))

    def test_money_stays_numeric_not_apostrophe_prefixed(self):
        row = _export([_prefc_lead(default_amount="296494.82")], "pre_foreclosure",
                      context=KING_PREFC_CTX)[1][0]
        assert row["Principal Owing"] == "296494.82"


class TestHiddenFields:
    def test_hiding_mailing_blanks_every_crm_mailing_column(self):
        row = _export([_prefc_lead()], "pre_foreclosure", context=KING_PREFC_CTX,
                      hidden_fields={"mailing_address"})[1][0]
        for col in ("Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
                    "Full Mailing Address"):
            assert row[col] == "", col
        assert row["Property City"] == "SEATTLE"  # not hidden

    def test_hiding_heirs_and_legal_keeps_headers(self):
        header, rows, _ = _export([_prefc_lead(heirs="X", legal_description="LOT 4")],
                                  "pre_foreclosure", context=KING_PREFC_CTX,
                                  hidden_fields={"heirs", "legal_description"})
        assert "Heirs" in header and "Legal Description" in header
        assert rows[0]["Heirs"] == rows[0]["Legal Description"] == ""
