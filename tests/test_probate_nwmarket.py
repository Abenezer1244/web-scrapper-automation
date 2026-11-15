"""Asotin probate notices to creditors (Lewiston Tribune, nwmarket.com).

Fixtures are the crawler's normalized text of real 2026 notices (names, addresses and
contact details changed): two Asotin Superior Court probates, one Spokane and one Idaho.
"""
from datetime import date
from pathlib import Path

from src.scrapers.sources.probate_nwmarket import is_asotin_probate, parse_probate_notice

_FIX = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return (_FIX / f"{name}.txt").read_text(encoding="utf-8")


def test_co_representatives_notice():
    n = parse_probate_notice(_text("asotin_probate_notice_a"))
    assert n is not None
    assert n.cause_no == "26-4-00122-02"
    assert n.decedent == "MARGARET J. HOLLOWAY"
    assert n.first_published == date(2026, 11, 4)          # printed "November 4 ,2026"
    # the attorney is under the same label ("Attorney for Co-Personal Representatives:")
    assert n.personal_reps == ("RACHEL M. DUNN", "AMANDA J. HOLLOWAY")
    assert n.claims_address == "P.O. Box 100 Clarkston, WA 99403"


def test_matter_of_caption_and_signature_block():
    n = parse_probate_notice(_text("asotin_probate_notice_b"))
    assert n is not None
    assert n.cause_no == "26-4-00117-02"
    assert n.decedent == "DOROTHY L. PRATT"
    assert n.first_published == date(2026, 11, 5)
    assert n.personal_reps == ("CYNTHIA J. MOORE",)
    assert n.claims_address == "Example Law, PLLC P .O. Drawer 800 Lewiston, ID 83501"


def test_other_courts_are_not_asotin():
    # Spokane (county code 32) and an Idaho district court share the same feed.
    for name in ("spokane_probate_notice", "idaho_probate_notice"):
        text = _text(name)
        assert not is_asotin_probate(text)
        assert parse_probate_notice(text) is None


def test_cause_code_alone_is_not_enough():
    # An Asotin-looking cause number in a non-Asotin court caption is rejected.
    text = _text("spokane_probate_notice").replace("26-4-02092-32", "26-4-02092-02")
    assert not is_asotin_probate(text)


def test_court_alone_is_not_enough():
    text = _text("asotin_probate_notice_a").replace("26-4-00122-02", "26-4-00122-32")
    assert parse_probate_notice(text) is None


def test_trustee_sale_is_not_probate():
    nts = (_FIX / "nts_nwmarket_asotin_155936.pdf")
    assert nts.exists()  # the Asotin NTS fixture the trustee-sale crawl tests use
    from src.scrapers.sources import nts_pdf

    text = nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(nts.read_bytes()))
    assert not is_asotin_probate(text)


def test_missing_first_publication_is_unparsed():
    text = _text("asotin_probate_notice_a").replace("Date of First Publication", "Dated")
    assert parse_probate_notice(text) is None


def test_alias_keeps_the_legal_name():
    text = _text("asotin_probate_notice_a").replace(
        "MARGARET J. HOLLOWAY, Deceased", "MARGARET J. HOLLOWAY, a/k/a PEGGY HOLLOWAY, Deceased")
    assert parse_probate_notice(text).decedent == "MARGARET J. HOLLOWAY"


def test_fields_in_another_order_do_not_bleed():
    text = _text("asotin_probate_notice_b")
    head, tail = text.split("DATE OF FIRST PUBLICA", 1)
    first_pub = "DATE OF FIRST PUBLICA" + tail.split(" PERSONAL REPRESENTA", 1)[0]
    rest = tail.split(" PERSONAL REPRESENTA", 1)[1]
    reordered = head + "PERSONAL REPRESENTA" + rest.replace(" ADDRESS FOR", " " + first_pub + " ADDRESS FOR", 1)
    n = parse_probate_notice(reordered)
    assert n.personal_reps == ("CYNTHIA J. MOORE",)
    assert n.first_published == date(2026, 11, 5)
    assert "PUBLICATION" not in n.claims_address.upper()


def test_rep_with_suffix_is_one_person():
    text = _text("asotin_probate_notice_b").replace("Cynthia J. Moore ADDRESS", "John A. Moore, Jr. ADDRESS")
    assert parse_probate_notice(text).personal_reps == ("JOHN A. MOORE, JR",)
