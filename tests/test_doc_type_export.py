"""Phase 2a: doc_type is part of the export column order + ScrapedRecord shape."""
from src.scrapers.base_scraper import ScrapedRecord
from src.utils.lead_export import LEAD_CSV_COLUMNS


def test_doc_type_in_export_column_order_right_after_legal_description():
    assert "doc_type" in LEAD_CSV_COLUMNS
    assert LEAD_CSV_COLUMNS.index("doc_type") == LEAD_CSV_COLUMNS.index("legal_description") + 1


def test_scraped_record_doc_type_flows_to_dict():
    r = ScrapedRecord(party_name="DOE, JOHN", doc_type="NOTICE OF TRUSTEE SALE")
    assert r.to_dict().get("doc_type") == "NOTICE OF TRUSTEE SALE"
