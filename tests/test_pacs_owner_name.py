"""The PACS mailing answer names the assessor's current owner, for probate/death leads.

Benton, 2026-11-15: a death certificate recorded for a sale named a parcel an unrelated
buyer now owns. The probate family records that owner beside the decedent with a
display-only title_status (classify_probate_title_status), as King's path already did.
"""
from pathlib import Path

from src.scrapers.enrichment import pacs_parcel as pp
from src.scrapers.enrichment.pacs import pacs_owner_names
from src.scrapers.enrichment.pacs_tax import parse_tax
from src.utils.lead_formatting import classify_probate_title_status

_FIXTURES = Path(__file__).parent / "fixtures"
_BENTON_PAGE = (_FIXTURES / "benton_pacs_detail_58808.html").read_text(encoding="utf-8")
_BENTON_PARCEL = "131073011125003"


def test_the_benton_detail_page_names_its_owner():
    answer = pp.parse_detail(_BENTON_PAGE, _BENTON_PARCEL)
    assert answer.is_found and answer.owner_name == "OWNER REDACTED"  # redacted in the saved page


def test_co_owners_are_joined_once_each():
    html = ("<table><tr><td>Name:</td><td>MACIAS REUBEN</td></tr>"
            "<tr><td>Name:</td><td>MACIAS CECELIA</td></tr>"
            "<tr><td>Name:</td><td>MACIAS REUBEN</td></tr></table>")
    assert pacs_owner_names(html) == "MACIAS REUBEN & MACIAS CECELIA"
    assert pacs_owner_names("<table><tr><td>Owner ID:</td><td>1</td></tr></table>") is None


def test_the_tax_reader_still_reads_the_same_owner():
    """parse_tax now shares the reader; its answer must not change."""
    assert parse_tax(_BENTON_PAGE, _BENTON_PARCEL).owner_name == "OWNER REDACTED"
    assert pacs_owner_names(_BENTON_PAGE) == "OWNER REDACTED"


def test_a_not_found_answer_carries_no_owner():
    assert pp.parse_detail(_BENTON_PAGE, "131073011125000").owner_name is None  # another parcel


def test_the_flag_on_the_four_recovered_benton_records():
    """The assessor's owner on 2026-11-15 for each recovered parcel (read-only lookup)."""
    assert classify_probate_title_status("LAMBORN, JAMES FRANCIS", "SANTIAGO ADRIAN CASTRO") == "current_owner_name_differs"
    assert classify_probate_title_status("HAY, JAMES LEROY", "HAY BARBARA L") == ""
    assert classify_probate_title_status("PEARSON, THOMAS JEFFERSON", "PEARSON SAMMYE SHARON") == ""
    assert classify_probate_title_status("MACIAS, REUBEN", "MACIAS REUBEN & CECELIA") == ""


def test_the_recovery_sweep_writes_the_same_owner_keys_for_the_probate_family():
    """Benton 2026-11-15: every PACS mailing in the recovery run came through the sweep,
    which recorded no owner, so the flag never landed."""
    from types import SimpleNamespace

    from src.workers.mailing_recovery import _owner_keys

    death = SimpleNamespace(record_type="death_certificate", party_name="LAMBORN, JAMES FRANCIS")
    assert _owner_keys(death, "SANTIAGO ADRIAN CASTRO") == {
        "assessor_current_owner": "SANTIAGO ADRIAN CASTRO", "title_status": "current_owner_name_differs"}
    probate = SimpleNamespace(record_type="probate", party_name="HAY, JAMES LEROY")
    assert _owner_keys(probate, "HAY BARBARA L") == {"assessor_current_owner": "HAY BARBARA L",
                                                     "title_status": ""}
    # Other record types, King rows (no record_type) and no owner: nothing written.
    assert _owner_keys(SimpleNamespace(record_type="pre_foreclosure", party_name="X"), "Y") == {}
    assert _owner_keys(SimpleNamespace(party_name="X"), "Y") == {}
    assert _owner_keys(death, None) == {}
