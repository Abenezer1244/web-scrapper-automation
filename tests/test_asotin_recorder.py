"""Asotin recorder (Helion, behind a reCAPTCHA): page reading, paging checks, and leads.

The HTML below copies the live portal's structure (read 2026-11-16). Every person and
parcel in it is invented.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.scrapers import asotin_wa_recorder as ar
from src.scrapers.enrichment.asotin_parcels import LayerError
from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.reliability import ScraperExecutionError

_START, _END = datetime(2026, 7, 15), datetime(2026, 11, 14)


def _card(number, doc_type, direct=(), indirect=(), parcels=(), recorded="11/04/2026 03:02 PM"):
    lis = "".join(f'<li class="result-subline"><strong class="bold-light">DIRECT:</strong> {d}</li>'
                  for d in direct)
    lis2 = "".join(f'<li class="result-subline"><strong class="bold-light">INDIRECT:</strong> {i}</li>'
                   for i in indirect)
    tax = "".join(
        '<li class="margin-right-50 padding-0"><span class="result-subline">'
        '<span class="bold-light">Tax#</span><span class="visually-hidden">Tax Parcel #</span>'
        f"<span>{p}</span></span></li>" for p in parcels)
    return f"""<div class="search-result">
      <div class="container row-header">
        <div class="inline margin-right-10"><h3 class="font-size-h4 inline">
          <a class="recording-link" href="/DigitalResearchRoom/Document/1">{number}</a></h3></div>
        <p class="inline margin-bottom-0 margin-right-10"><strong class="bold-light">Date Recorded:</strong> {recorded}</p>
        <p class="inline margin-bottom-0 margin-right-10"><strong class="bold-light">Doc Type:</strong> {doc_type}</p>
        <hr class="document-result-header-divider"/></div>
      <div class="container"><div class="flex-container">
        <div class="margin-right-10"><ul class="list-unstyled">{lis}</ul></div>
        <div class="margin-right-10"><ul class="list-unstyled">{lis2}</ul></div></div>
        <ul class="width-full list-unstyled list-inline margin-left-0">{tax}</ul></div></div>"""


def _results(cards, total=None, first=1, label="DEATH CERTIFICATE", dates=("7/15/2026", "11/14/2026")):
    last = first + len(cards) - 1
    total = last if total is None else total
    return f"""<html><body><main><div id="search-results-header">{total} Results Found
      Displaying results {first} - {last} <a>Skip to Results</a> Search Criteria: Date: {dates[0]}
       - {dates[1]} {label} View By: Summary Party Map</div>{''.join(cards)}
      <div class="results-toolbar-section"><button class="btn btn-default previous-page-button"
       {'disabled="disabled"' if last >= total else ''}>Next 50</button></div></main></body></html>"""


def _empty(start="2026-07-15", end="2026-11-14", value="109", label="DEATH CERTIFICATE"):
    return f"""<html><body><main><div>Error No matching results found</div>
      <input id="Criteria_Filter_RecordingDateStart" type="date" value="{start}"/>
      <input id="Criteria_Filter_RecordingDateEnd" type="date" value="{end}"/>
      <select id="Filter_DocumentSubtype"><option value="">-</option>
        <option value="{value}" selected="selected">{label}</option></select></main></body></html>"""


_DISCLAIMER = """<html><body><form action="/DigitalResearchRoom/Disclaimer" method="post">
  <div class="g-recaptcha" data-sitekey="x"></div><button type="submit">I Agree</button></form></body></html>"""


def _check(page, expected_first=1, label="DEATH CERTIFICATE"):
    ar.check_page(page, expected_first=expected_first, start=_START, end=_END, subtype_label=label)


# --- reading pages ---------------------------------------------------------------------

def test_a_results_page_reads_every_field_of_every_card():
    page = ar.parse_page(_results([
        _card("2026-391515", "DEATH CERTIFICATE", ["DOE, ADA M"], ["ROE, JANE"]),
        _card("2026-391516", "DEATH CERTIFICATE", ["POE, EDGAR"], ["ROE, JANE"]),
    ]))
    assert (page.state, page.total, page.first, page.last) == ("results", 2, 1, 2)
    assert page.criteria == ("7/15/2026", "11/14/2026", "DEATH CERTIFICATE")
    card = page.cards[0]
    assert (card.number, card.recorded, card.doc_type) == (
        "2026-391515", "11/04/2026 03:02 PM", "DEATH CERTIFICATE")
    assert (card.direct, card.indirect, card.parcels) == (("DOE, ADA M",), ("ROE, JANE",), ())
    _check(page)


def test_every_parcel_is_kept_as_printed():
    page = ar.parse_page(_results([_card("2026-1", "Lis Penden", ["BANK OF NOWHERE"], ["DOE, ADA M"],
                                         ["10510000400000000", "SEE ATTACHED", "06004140010010000"])],
                                  label="Lis Penden"))
    assert page.cards[0].parcels == ("10510000400000000", "SEE ATTACHED", "06004140010010000")


def test_an_empty_answer_is_recognised_and_must_echo_this_search():
    page = ar.parse_page(_empty())
    assert page.state == "empty"
    _check(page)
    for other in (_empty(start="2026-07-16"), _empty(value="21", label="Decree Dis")):
        with pytest.raises(ScraperExecutionError, match="another search"):
            _check(ar.parse_page(other))


def test_the_disclaimer_is_an_expired_session_not_a_zero():
    assert ar.parse_page(_DISCLAIMER).state == "disclaimer"
    assert ar.parse_page("<html></html>", ar.DISCLAIMER_URL).state == "disclaimer"
    with pytest.raises(ar.SessionExpiredError):
        _check(ar.parse_page(_DISCLAIMER))


def test_an_unreadable_page_fails_instead_of_counting_as_empty():
    for html in ("<html><body>Hang On...</body></html>",
                 "<html><body><div id='search-results-header'>Something else</div></body></html>"):
        with pytest.raises(ScraperExecutionError, match="unreadable"):
            _check(ar.parse_page(html))


# --- the page has to be the one this search should be on -------------------------------

def test_results_for_another_window_or_type_fail():
    cards = [_card("2026-1", "DEATH CERTIFICATE", ["DOE, ADA M"])]
    with pytest.raises(ScraperExecutionError, match="another search"):
        _check(ar.parse_page(_results(cards, dates=("7/16/2026", "11/14/2026"))))
    with pytest.raises(ScraperExecutionError, match="another search"):
        _check(ar.parse_page(_results(cards, label="Deed")))
    # A card of another type inside the right search: the filter was not applied.
    mixed = _results([_card("2026-1", "DEATH CERTIFICATE", ["DOE, ADA M"]),
                      _card("2026-2", "WARR DEED", ["POE, EDGAR"])])
    with pytest.raises(ScraperExecutionError, match="WARR DEED"):
        _check(ar.parse_page(mixed))


def test_a_skipped_or_repeated_page_fails():
    cards = [_card(f"2026-{i}", "DEATH CERTIFICATE", ["DOE, ADA M"]) for i in range(3)]
    second = ar.parse_page(_results(cards, total=53, first=51))
    _check(second, expected_first=51)
    with pytest.raises(ScraperExecutionError, match="expected to start at 1"):
        _check(second, expected_first=1)       # the first page again would be a repeat
    with pytest.raises(ScraperExecutionError, match="cards for results"):
        _check(ar.parse_page(_results(cards, total=60, first=1).replace(
            "Displaying results 1 - 3", "Displaying results 1 - 4")))


def test_a_card_without_number_date_or_type_fails():
    with pytest.raises(ScraperExecutionError, match="without number"):
        _check(ar.parse_page(_results([_card("", "DEATH CERTIFICATE", ["DOE, ADA M"])])))


# --- death certificates: the estate-marked owner is the only tie -----------------------

def _row(name, parcel="10412300600050000", billing="PO BOX 1", city="CLARKSTON"):
    return {"PARCEL_ID": parcel, "Prop_ID": 1, "NAME": name, "BILLING1": billing, "BILLING2": None,
            "CITY": city, "ST": "WA", "ZIP": "99403", "FULL_ADD": "1 MAIN ST",
            "SITE_CITY": "CLARKSTON", "SITE_ZIP": "99403"}


def _death(direct="DOE, ADA M", indirect=("ROE, JANE",)):
    return ar.Card("2026-391515", "11/04/2026 03:02 PM", "DEATH CERTIFICATE", (direct,), tuple(indirect), ())


def test_an_estate_marked_parcel_of_the_decedent_is_a_lead():
    rows = [_row("DOE, ADA M (ESTATE)"), _row("DOE, JOHN", parcel="10412300600099999")]
    asked = []
    records, held = ar.death_certificate_records(_death(), lambda s: asked.append(s) or rows)
    assert asked == ["DOE"] and held == []
    [rec] = records
    assert (rec.party_name, rec.parcel_id, rec.property_address, rec.property_state) == (
        "DOE, ADA M", "10412300600050000", "1 MAIN ST", "WA")
    assert rec.mailing_address and rec.date_recorded == "11/04/2026"
    assert rec.doc_type == "DEATH CERTIFICATE"
    assert rec.enrichment_data["parcel_ties"] == ["estate_marked"]
    assert rec.enrichment_data["instrument_number"] == "2026-391515"
    # Stable identity: the same certificate and parcel give the same lead.
    again, _ = ar.death_certificate_records(_death(), lambda s: rows)
    assert again[0].raw_html_hash == rec.raw_html_hash


def test_a_name_match_without_the_estate_mark_is_held():
    records, held = ar.death_certificate_records(_death(), lambda s: [_row("DOE, ADA M")])
    assert records == [] and held


def test_the_other_party_on_the_certificate_is_never_a_tie():
    # The INDIRECT party named on the parcel ties nothing: there is no representative rule here.
    records, _ = ar.death_certificate_records(
        _death(indirect=("ROE, JANE",)), lambda s: [_row("DOE, ADA M & ROE, JANE")])
    assert records == []


def test_an_unreadable_decedent_or_surname_is_held_and_a_dead_layer_raises():
    assert ar.death_certificate_records(_death(direct="DOE"), lambda s: [])[1] == [
        "decedent_name_unreadable"]

    def refuse(_surname):
        raise ValueError("surname is not a plain name")
    assert ar.death_certificate_records(_death(), refuse)[1] == ["surname_not_queryable"]

    def down(_surname):
        raise LayerError("layer HTTP 503")
    with pytest.raises(LayerError):
        ar.death_certificate_records(_death(), down)


@pytest.mark.parametrize(("recorder", "natural"), [
    ("HOLLOWAY, MARGARET J", "MARGARET J HOLLOWAY"),
    ("DOE, JOHN A. JR", "JOHN A DOE JR"),
    ("DOE,ADA", "ADA DOE"),
])
def test_recorder_order_becomes_natural_order(recorder, natural):
    assert ar.recorder_to_natural(recorder) == natural


@pytest.mark.parametrize("name", ["DOE ADA", "DOE,", ", ADA"])
def test_a_name_without_both_parts_is_not_turned_around(name):
    assert ar.recorder_to_natural(name) is None


# --- lis pendens ------------------------------------------------------------------------

def _lis(parcels, direct=("BANK OF NOWHERE NA",), indirect=("DOE, ADA M",)):
    return ar.Card("2026-1", "03/02/2026 10:00 AM", "Lis Penden", tuple(direct), tuple(indirect),
                   tuple(parcels))


def test_one_lis_pendens_lead_per_printed_parcel_labelled_honestly():
    records, held = ar.lis_pendens_records(_lis(["10510000400000000", "SEE ATTACHED", "10490010100690000"]))
    assert held == []
    assert [r.parcel_id for r in records] == ["10510000400000000", "10490010100690000"]
    assert {r.doc_type for r in records} == {"LIS PENDENS"}
    assert {r.party_name for r in records} == {"DOE, ADA M"}
    assert records[0].enrichment_data["all_parcels"] == [
        "10510000400000000", "SEE ATTACHED", "10490010100690000"]
    assert len({r.raw_html_hash for r in records}) == 2


def test_a_lis_pendens_without_a_parcel_or_a_person_is_held():
    assert ar.lis_pendens_records(_lis(["SEE ATTACHED"])) == ([], ["no_parcel_on_record"])
    assert ar.lis_pendens_records(_lis([]))[1] == ["no_parcel_on_record"]
    assert ar.lis_pendens_records(_lis(["10510000400000000"], indirect=("ACME TRUSTEE SERVICES LLC",))) == (
        [], ["no_person_party"])


# --- construction -----------------------------------------------------------------------

def test_only_the_offered_types_and_a_selection_that_includes_lis_pendens():
    assert ar.AsotinWARecorderScraper("death_certificate")._subtype == "109"
    assert ar.AsotinWARecorderScraper("pre_foreclosure", doc_types=["lis_pendens"])._subtype == "39"
    for bad in ("divorce", "probate"):
        with pytest.raises(ValueError):
            ar.AsotinWARecorderScraper(bad)
    with pytest.raises(ValueError):
        ar.AsotinWARecorderScraper("pre_foreclosure", doc_types=["notice_of_default"])
    with pytest.raises(ValueError):
        ar.AsotinWARecorderScraper("death_certificate", doc_types=["lis_pendens"])


async def test_an_unusable_window_fails_before_any_request():
    s = ar.AsotinWARecorderScraper("death_certificate")
    with pytest.raises(ScraperExecutionError, match="unusable date window"):
        await s.scrape("2026-11-01", "11/14/2026")
    with pytest.raises(ScraperExecutionError, match="inverted"):
        await s.scrape("11/14/2026", "11/01/2026")


def test_the_scope_shows_what_is_collected_and_the_module_is_allowlisted():
    assert "src.scrapers.asotin_wa_recorder" in _ALLOWED_SCRAPER_MODULES
    assert ar.AsotinWARecorderScraper.collection_scope("divorce") is None
