"""Phase C — unified overlap/combine CSV (dialer-ready, not a dump).

Pure tests (no DB). Verify the overlap CSV builder reuses the canonical
dialer-ready row (first/last + address split + 10-digit phone), puts the overlap
signal first, uses the word "Overlap"/blank, and that segment exports sort
hottest-first.
"""
from io import StringIO

from src.api.routes.segments import _filing_sort_key, _label
from src.utils.lead_export import (
    OVERLAP_LEAD_COLUMNS,
    build_overlap_export_row,
    write_lead_csv_with_overlap,
)


class TestOverlapCsvBuilder:
    def test_columns_caller_first(self):
        assert OVERLAP_LEAD_COLUMNS[:4] == ["overlap", "lists_count", "lists", "counties"]
        assert OVERLAP_LEAD_COLUMNS[4:6] == ["first_name", "last_name"]

    def test_overlap_word_when_multi_list_and_dialer_ready_fields(self):
        rec = {
            "party_name": "DOE, JOHN",
            "property_address": "123 MAIN ST, KENT WA 98031",
            "phone": "(206) 555-1234",
            "date_recorded": "03/14/2026",
        }
        row = build_overlap_export_row(
            rec, {"lists_count": 2, "lists": "Probate; Pre-Foreclosure", "counties": "King"}
        )
        assert row["overlap"] == "Overlap"
        assert row["lists_count"] == "2"
        assert row["lists"] == "Probate; Pre-Foreclosure"
        assert row["first_name"] == "JOHN" and row["last_name"] == "DOE"
        assert row["property_street"] == "123 MAIN ST" and row["property_city"] == "KENT"
        assert row["property_state"] == "WA" and row["property_zip"] == "98031"
        assert row["phone"] == "2065551234"  # normalized 10-digit
        assert row["filed_date"] == "03/14/2026"

    def test_blank_overlap_flag_for_single_list(self):
        rec = {"party_name": "LEE, ROBERT", "property_address": "9 PINE ST, EVERETT WA 98201"}
        row = build_overlap_export_row(rec, {"lists_count": 1, "lists": "Probate", "counties": "Snohomish"})
        assert row["overlap"] == ""
        assert row["lists_count"] == "1"

    def test_writer_emits_header_and_rows(self):
        rec = {"party_name": "DOE, JANE", "property_address": "5 OAK AVE, TACOMA WA 98402"}
        buf = StringIO()
        write_lead_csv_with_overlap([(rec, {"lists_count": 2, "lists": "Probate; Divorce", "counties": "Pierce"})], buf)
        lines = buf.getvalue().splitlines()
        assert lines[0].split(",")[0] == "overlap"
        assert lines[1].startswith("Overlap,2,")

    def test_hidden_fields_blank_in_overlap_row(self):
        # Batch field-visibility applies to the combined CSV too (header kept).
        rec = {
            "party_name": "DOE, JOHN",
            "property_address": "123 MAIN ST, KENT WA 98031",
            "mailing_address": "PO BOX 7, KENT WA 98031",
            "heirs": "DOE, JANE",
            "legal_description": "LOT 9 BLK 1",
        }
        ov = {"lists_count": 1, "lists": "Probate", "counties": "King"}
        row = build_overlap_export_row(rec, ov, hidden_fields={"legal_description", "mailing_address"})
        assert row["legal_description"] == "" and row["mailing_address"] == ""
        assert row["heirs"] == "DOE, JANE"           # not hidden -> kept
        assert row["party_name"] == "DOE, JOHN"      # identity -> intact

    def test_mailing_split_columns_in_overlap_csv(self):
        # Combined/batch CSV carries the mailing split too (auto-copied from the
        # canonical row); hiding mailing_address blanks the splits here as well.
        rec = {
            "party_name": "DOE, JOHN",
            "property_address": "123 MAIN ST, KENT WA 98031",
            "mailing_address": "5520 SEELEY LAKE DR SW, LAKEWOOD, WA, 98499-2817",
        }
        ov = {"lists_count": 1, "lists": "Probate", "counties": "King"}
        row = build_overlap_export_row(rec, ov)
        assert row["mailing_street"] == "5520 SEELEY LAKE DR SW"
        assert row["mailing_city"] == "LAKEWOOD"
        assert row["mailing_state"] == "WA"
        assert row["mailing_zip"] == "98499-2817"
        hidden = build_overlap_export_row(rec, ov, hidden_fields={"mailing_address"})
        assert hidden["mailing_address"] == ""
        for col in ("mailing_street", "mailing_city", "mailing_state", "mailing_zip"):
            assert hidden[col] == "", col

    def test_writer_threads_hidden_fields(self):
        rec = {"party_name": "DOE, JANE", "property_address": "5 OAK AVE, TACOMA WA 98402",
               "legal_description": "LOT 4"}
        buf = StringIO()
        write_lead_csv_with_overlap(
            [(rec, {"lists_count": 1, "lists": "Probate", "counties": "Pierce"})],
            buf, hidden_fields={"legal_description"},
        )
        import csv as _csv
        row = next(_csv.DictReader(StringIO(buf.getvalue())))
        assert row["legal_description"] == ""
        assert row["party_name"] == "DOE, JANE"


class TestSegmentSortHelpers:
    def test_label_known_and_fallback(self):
        assert _label("pre_foreclosure") == "Pre-Foreclosure"
        assert _label("something_new") == "Something New"

    def test_filing_sort_key_recent_first(self):
        # More recent date -> smaller (more negative) key -> sorts first ascending.
        recent = _filing_sort_key("06/01/2026")
        older = _filing_sort_key("01/01/2026")
        assert recent < older
        assert _filing_sort_key("") == 0
        assert _filing_sort_key("garbage") == 0
