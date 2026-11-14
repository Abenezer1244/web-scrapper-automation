"""Clark LandmarkWeb completeness: a window is accepted only when the rows read are the
whole answer to the submitted search (src/scrapers/clark_wa.verify_chunk).

Header strings are copied from the live portal (2026-11-14)."""
import pytest

from src.scrapers.clark_wa import ClarkWAScraper, resolve_total, verify_chunk
from src.scrapers.reliability import ScraperExecutionError, TransientScrapeError

_ZERO = ('Your Document Search for Document Types "NTS,TRSL", Begin Date "11/8/2026", and '
         'End Date "11/8/2026" returned the following results:\nReturned 0 records of 0\n')
_288 = ('Your Document Search for Document Types "WILL,LPA,TODD,DEATH", Begin Date "1/1/2026", '
        'and End Date "3/31/2026" returned the following results:\nReturned 288 records of 288\n')


def _check(summary, *, types="WILL,LPA,TODD,DEATH", frm="01/01/2026", to="03/31/2026",
           total=288, ids=None, empty=""):
    verify_chunk(summary, expected_types=types, date_from=frm, date_to=to, true_total=total,
                 doc_ids=[str(i) for i in range(288)] if ids is None else ids,
                 empty_text=empty, record_type="probate")


def test_complete_read_passes():
    _check(_288)


def test_verified_zero_needs_explicit_marker():
    _check(_ZERO, types="NTS,TRSL", frm="11/08/2026", to="11/08/2026", total=0, ids=[], empty="No results.")
    for other in ("", "Processing...", "An error occurred"):
        with pytest.raises(ScraperExecutionError, match="explicit empty"):
            _check(_ZERO, types="NTS,TRSL", frm="11/08/2026", to="11/08/2026", total=0, ids=[],
                   empty=other)


def test_short_read_fails():
    with pytest.raises(ScraperExecutionError, match="read 287 documents, portal reports 288"):
        _check(_288, ids=[str(i) for i in range(287)])


def test_rows_without_the_portals_numeric_id_fail():
    # Row ids are "doc_<documentId>_<row>"; a changed format must not be counted as documents.
    with pytest.raises(ScraperExecutionError, match="numeric document id"):
        _check(_288, ids=[str(i) for i in range(287)] + [""])


def test_duplicate_rows_across_pages_fail():
    with pytest.raises(ScraperExecutionError, match="duplicate"):
        _check(_288, ids=[str(i) for i in range(287)] + ["0"])


def test_stale_table_from_other_window_fails():
    with pytest.raises(ScraperExecutionError, match="not the submitted window"):
        _check(_288, frm="04/01/2026", to="04/30/2026")


def test_other_doc_types_fail():
    with pytest.raises(ScraperExecutionError, match="doc types"):
        _check(_288, types="NTS,TRSL")
    with pytest.raises(ScraperExecutionError, match="doc types"):
        _check(_288, types="DEATH,LPA,TODD")


def test_doc_type_order_is_the_portals_own():
    # Live: submitted textarea "DEATH,LPA,TODD,WILL", header echoes "WILL,LPA,TODD,DEATH".
    _check(_288, types="DEATH,LPA,TODD,WILL")


def test_missing_header_fails():
    with pytest.raises(ScraperExecutionError, match="header"):
        _check("Hide Results\nClear Results")


def test_total_prefers_show_count_then_numeric_header():
    assert resolve_total(_288, 729, record_type="probate") == 729
    multipage = _288.replace("Returned 288 records of 288", "Returned 500 records of 729")
    assert resolve_total(multipage, None, record_type="probate") == 729


def test_no_full_count_is_transient_never_rows_loaded():
    partial = _288.replace("Returned 288 records of 288", "Returned 500 records of ")
    with pytest.raises(TransientScrapeError, match="no full result count"):
        resolve_total(partial, None, record_type="probate")
    with pytest.raises(TransientScrapeError):
        resolve_total("garbage", None, record_type="probate")


def test_rows_become_leads_with_raw_pid_string():
    s = ClarkWAScraper("pre_foreclosure", doc_types=["notice_of_trustee_sale"])
    rows = [{  # real row shape (instrument 6358783), names replaced
        "docId": "6758467", "grantor": "DOE JANE",
        "grantee": 'QUALITY LOAN SERVICE CORPORATION<div class="nameSeperator"></div>MIDFIRST BANK',
        "date": "11/02/2026", "docType": "NOTICE OF TRUSTEE SALE",
        "legal": "SUB: GREEN GARDEN NORTH 2  BKG P943 LOT: 10 PID: 109583514", "recNum": "6358783",
    }, {
        "docId": "1", "grantor": "X", "grantee": "Y", "date": "11/02/2026",
        "docType": "DEED OF TRUST", "legal": "PID: 012345678", "recNum": "2",
    }]
    recs = s._records_from_rows(rows)
    assert len(recs) == 1
    assert recs[0].parcel_id == "109583514"
    assert recs[0].party_name == "DOE JANE"
    assert recs[0].enrichment_data["recording_number"] == "6358783"


def test_death_certificate_selects_code_62_and_keeps_the_decedent():
    s = ClarkWAScraper("death_certificate")
    assert s._checkbox_values == ["62"]
    rows = [{  # real shape (instrument 6353261): portal labels grantor "Decedent", grantee "Public"
        "docId": "1", "grantor": "ROE RICHARD", "grantee": "PUBLIC", "date": "10/05/2026",
        "docType": "DEATH CERTIFICATE", "legal": "SUB: PARK EST BK311 P363 LOT: 16 PID: 185188032",
        "recNum": "6353261",
    }, {  # no parcel: not property-linked, never a lead
        "docId": "2", "grantor": "ROE JANE", "grantee": "PUBLIC", "date": "10/05/2026",
        "docType": "DEATH CERTIFICATE", "legal": "", "recNum": "6353262",
    }]
    recs = s._records_from_rows(rows)
    assert [(r.party_name, r.heirs, r.parcel_id) for r in recs] == [("ROE RICHARD", None, "185188032")]
    scope = ClarkWAScraper.collection_scope("death_certificate")
    assert scope is not None and [i.label for i in scope.items] == ["Death Certificate"]
