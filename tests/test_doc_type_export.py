"""Phase 2a: doc_type is part of the export column order + ScrapedRecord shape."""
from src.utils.data_exporter import _COLUMN_ORDER
from src.scrapers.base_scraper import ScrapedRecord


def test_doc_type_in_export_column_order_right_after_legal_description():
    assert "doc_type" in _COLUMN_ORDER
    assert _COLUMN_ORDER.index("doc_type") == _COLUMN_ORDER.index("legal_description") + 1


def test_scraped_record_doc_type_flows_to_dict():
    r = ScrapedRecord(party_name="DOE, JOHN", doc_type="NOTICE OF TRUSTEE SALE")
    assert r.to_dict().get("doc_type") == "NOTICE OF TRUSTEE SALE"
