"""Phase 2a: EagleWeb-captured doc_type must survive on ScrapedRecord."""
from src.scrapers.base_scraper import ScrapedRecord


def test_scraped_record_carries_doc_type_through_to_dict():
    r = ScrapedRecord(party_name="DOE, JOHN", doc_type="NOTICE OF TRUSTEE SALE")
    assert r.to_dict()["doc_type"] == "NOTICE OF TRUSTEE SALE"
