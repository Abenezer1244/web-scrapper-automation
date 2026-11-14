"""iDocMarket (Columbia COLWA1) template, run in real Chromium against fixture pages.

Card markup and the counter text copy the live portal (read 2026-11-14); names are
invented. Regressions covered: the date fields became <duet-date-picker> components
(page.fill on the old id threw, so every run failed), a missing or short counter was
read as a complete result, PR deeds went out as probate with the personal
representative as the "owner", and death certificates were mixed into probate.
"""

import pytest

from src.scrapers.templates.idocmarket import IDocMarketScraper, check_counter, keep_doc_type

_URL = "https://www.idocmarket.com/COLWA1/Document/Search"


def _card(doc_type, docnum, date, grantors, grantees, legal=""):
    g = "".join(f"<span class='party-value'>{n}</span>" for n in grantors)
    e = "".join(f"<span class='party-value'>{n}</span>" for n in grantees)
    num = f'<span sort-desc="docnum">#{docnum} <small></small></span>' if docnum else ""
    return f"""<div class="row result-item">
      <div class="col-md-4"><div><div class="doc-title" sort-desc="instrument">{doc_type}</div></div>{num}</div>
      <div class="col-md-1"><p class="mb-0" aria-label="Record Date: {date}">{date}</p>
        <p class="mb-0" aria-label="2 Pages">2 Pages</p></div>
      <div class="full-parties"><div class="grantor-line">{g}</div><div class="grantee-line">{e}</div></div>
      <div class="first-legals"><span>Legal</span><span>{legal}</span></div></div>"""


_CARDS = [
    _card("CERTIFICATE OF DEATH", "90001", "10/1/2026", ["DOE, JANE A., DECEASED"], ["DOE, JOHN"], "SAMPLE ADD 9 5"),
    _card("CERTIFICATE OF DEATH", "90002", "10/2/2026", ["ROE, RICHARD"], ["THE PUBLIC"]),
    _card("PERSONAL REPRESENTATIVE DEED", "90003", "10/3/2026",
          ["PRO, PAT, PERSONAL REPRESENTATIVE OF"], ["BUYER, BOB"], "T09N R39E 19"),
    _card("LACK OF PROBATE AFFIDAVIT", "90004", "10/4/2026", ["SMITH, SAM D."], ["SMITH, SUE O."], "T09N R39E 11 S2SE4"),
    _card("TRANSFER ON DEATH DEED", "90005", "10/5/2026", ["LIVING, LEE"], ["LIVING, LIZ"]),
    _card("NOTICE OF TRUSTEE'S SALE", "90006", "10/6/2026", ["BORROWER, BEA"], ["SAMPLE MORTGAGE CORPORATION"]),
    _card("DEED OF TRUST", "90007", "10/7/2026", ["OWNER, OLLIE"], ["SAMPLE BANK"]),
]


def _page_html(cards, counter=True, shown=None, total=None):
    n = len(cards)
    count = f"<div>Showing: {shown if shown is not None else n} of {total if total is not None else n} results</div>"
    return f"<html><body>{count if counter else ''}{''.join(cards)}</body></html>"


@pytest.fixture
async def browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            yield b
        finally:
            await b.close()


async def _extract(browser, html, record_type):
    page = await browser.new_page()
    await page.set_content(html)
    s = IDocMarketScraper(_URL, "columbia", "wa", record_types=[record_type], record_type=record_type)
    s.page = page
    return await s._extract_results(record_type)


async def test_death_certificates_are_their_own_type_with_the_decedent_as_party(browser):
    recs = await _extract(browser, _page_html(_CARDS), "death_certificate")
    assert [r.enrichment_data["instrument_number"] for r in recs] == ["90001", "90002"]
    assert recs[0].party_name == "DOE, JANE A."  # ", DECEASED" is a status, not the name
    assert recs[1].party_name == "ROE, RICHARD"
    assert recs[1].heirs is None  # "THE PUBLIC" is a placeholder, not an heir
    assert recs[0].date_recorded == "10/01/2026"


async def test_probate_drops_pr_deeds_death_certs_and_tod_deeds(browser):
    recs = await _extract(browser, _page_html(_CARDS), "probate")
    assert [r.enrichment_data["instrument_number"] for r in recs] == ["90004"]
    assert recs[0].party_name == "SMITH, SAM D."
    assert recs[0].heirs == "SMITH, SUE O."
    assert recs[0].legal_description == "T09N R39E 11 S2SE4"


async def test_pre_foreclosure_keeps_the_borrower(browser):
    recs = await _extract(browser, _page_html(_CARDS), "pre_foreclosure")
    assert [(r.enrichment_data["instrument_number"], r.party_name) for r in recs] == [("90006", "BORROWER, BEA")]


@pytest.mark.parametrize(("date", "docnum"), [("", "90009"), ("10/9/2026", "")])
async def test_a_kept_card_without_date_or_number_is_schema_drift(browser, date, docnum):
    cards = [_card("CERTIFICATE OF DEATH", docnum, date, ["DOE, JANE"], ["THE PUBLIC"])]
    with pytest.raises(RuntimeError, match="without record date or document number"):
        await _extract(browser, _page_html(cards), "death_certificate")


async def test_a_counted_card_without_a_title_fails_instead_of_vanishing(browser):
    cards = [_card("", "90010", "10/9/2026", ["DOE, JANE"], ["THE PUBLIC"])]
    with pytest.raises(RuntimeError, match="without a document type"):
        await _extract(browser, _page_html(cards), "death_certificate")


async def test_a_kept_card_without_party_markup_fails(browser):
    cards = [_card("CERTIFICATE OF DEATH", "90011", "10/9/2026", [], [])]
    with pytest.raises(RuntimeError, match="without party names"):
        await _extract(browser, _page_html(cards), "death_certificate")


async def test_a_named_grantee_is_never_promoted_to_decedent(browser):
    # Placeholder grantor beside a person: that person is an heir, not a death.
    cards = [_card("CERTIFICATE OF DEATH", "90012", "10/9/2026", ["THE PUBLIC"], ["DOE, JOHN"])]
    with pytest.raises(RuntimeError, match="without a readable grantor"):
        await _extract(browser, _page_html(cards), "death_certificate")


async def test_an_unknown_record_type_is_a_misconfiguration_not_a_zero():
    s = IDocMarketScraper(_URL, "columbia", "wa", record_types=["divorce"], record_type="divorce")
    with pytest.raises(ValueError, match="no doc-type filter"):
        await s.scrape("10/01/2026", "10/02/2026")


async def test_an_empty_window_is_a_verified_zero(browser):
    assert await _extract(browser, _page_html([]), "probate") == []


async def test_a_page_without_the_counter_fails_instead_of_reporting_zero(browser):
    with pytest.raises(RuntimeError, match="counter not found"):
        await _extract(browser, _page_html([], counter=False), "probate")


async def test_a_capped_result_fails_the_chunk(browser):
    with pytest.raises(RuntimeError, match="incomplete"):
        await _extract(browser, _page_html(_CARDS, shown=7, total=1200), "probate")


async def test_cards_that_disagree_with_the_counter_fail_the_chunk(browser):
    # Selector drift: the portal says 9 rows, the page read 7.
    with pytest.raises(RuntimeError, match="7 cards read"):
        await _extract(browser, _page_html(_CARDS, shown=9, total=9), "probate")


# A stand-in for <duet-date-picker>: an inner text input that writes the ISO value
# to the hidden field on blur, as the live component does.
_FORM = """<html><body><form>
<duet-date-picker id="StartRecordDate"><input id="start-record-date"></duet-date-picker>
<input type="hidden" name="StartRecordDate" value="">
<duet-date-picker id="EndRecordDate"><input id="end-record-date"></duet-date-picker>
<input type="hidden" name="EndRecordDate" value="">
</form><script>
for (const [vis, hid] of [["start-record-date","StartRecordDate"],["end-record-date","EndRecordDate"]]) {
  document.getElementById(vis).addEventListener("blur", e => {
    const m = e.target.value.match(/^(\\d{1,2})\\/(\\d{1,2})\\/(\\d{4})$/);
    document.querySelector(`input[name=${hid}]`).value =
      m ? `${m[3]}-${m[1].padStart(2,"0")}-${m[2].padStart(2,"0")}` : "";
  });
}
</script></body></html>"""


async def test_dates_go_into_the_picker_and_reach_the_submitted_fields(browser):
    page = await browser.new_page()
    await page.set_content(_FORM)
    s = IDocMarketScraper(_URL, "columbia", "wa", record_types=["probate"], record_type="probate")
    s.page = page
    await s._fill_dates("08/16/2026", "11/13/2026")
    assert await page.eval_on_selector("input[name=StartRecordDate]", "e => e.value") == "2026-08-16"
    assert await page.eval_on_selector("input[name=EndRecordDate]", "e => e.value") == "2026-11-13"


async def test_a_date_the_picker_did_not_take_fails_the_search(browser):
    page = await browser.new_page()
    await page.set_content(_FORM)
    await page.evaluate("""() => { const i = document.getElementById('end-record-date');
        i.replaceWith(i.cloneNode()); }""")  # drop the blur handler: the field never fills
    s = IDocMarketScraper(_URL, "columbia", "wa", record_types=["probate"], record_type="probate")
    s.page = page
    with pytest.raises(RuntimeError, match="EndRecordDate"):
        await s._fill_dates("08/16/2026", "11/13/2026")


@pytest.mark.parametrize(("text", "cards"), [("Showing: 3 of 3 results", 3), ("Showing: 1,200 of 1,200", 1200)])
def test_counter_accepts_a_complete_page(text, cards):
    check_counter(text, cards)


@pytest.mark.parametrize(("doc", "rt", "kept"), [
    ("CERTIFICATE OF DEATH", "probate", False),
    ("AFFIDAVIT OF DEATH", "death_certificate", False),
    ("EXECUTOR'S DEED", "probate", False),
    ("LACK OF PROBATE AFFIDAVIT", "probate", True),
    ("LETTERS OF ADMINISTRATION", "probate", False),  # names the representative
    ("PERSONAL REPRESENTATIVE DEED", "probate", False),
    ("NOTICE OF TRUSTEE'S SALE", "pre_foreclosure", True),
])
def test_doc_type_routing(doc, rt, kept):
    from src.scrapers.templates.idocmarket import _DOC_TYPE_MAP

    assert keep_doc_type(doc, rt, _DOC_TYPE_MAP[rt]) is kept


# The whole chunk loop on a routed portal: GET serves the form, the form POSTs back,
# and the answer for each window is chosen by its submitted StartRecordDate.
_HOST = "https://idocmarket.fixture.test"
_PORTAL = f"{_HOST}/COLWA1/Document/Search"


def _portal_form(submits=True):
    cancel = "" if submits else ' onsubmit="return false"'  # a Search click that goes nowhere
    return _FORM.replace("<form>", f'<form method="post" action="{_PORTAL}"{cancel}>').replace(
        "</form>", '<input type="submit" value="Search"></form>')


async def _run(browser, answers, date_from, date_to, submits=True):
    """answers: {StartRecordDate ISO: (status, html)}."""
    from urllib.parse import parse_qs

    page = await browser.new_page()

    async def serve(route):
        req = route.request
        if req.method == "POST":
            start = parse_qs(req.post_data or "").get("StartRecordDate", [""])[0]
            status, body = answers[start]
            await route.fulfill(status=status, content_type="text/html", body=body)
        else:
            await route.fulfill(status=200, content_type="text/html", body=_portal_form(submits))

    await page.route(f"{_HOST}/**", serve)
    s = IDocMarketScraper(_PORTAL, "columbia", "wa", record_types=["death_certificate"],
                          record_type="death_certificate")
    s.page = page

    async def navigate(url, wait_until="domcontentloaded"):  # the fixture host has no DNS
        await page.goto(url, wait_until=wait_until)

    s.navigate = navigate
    return await s.scrape(date_from, date_to)


_ONE = _page_html([_card("CERTIFICATE OF DEATH", "90020", "10/2/2026", ["DOE, JANE"], ["THE PUBLIC"])])
_TWO = _page_html([_card("CERTIFICATE OF DEATH", "90021", "11/2/2026", ["ROE, RICH"], ["THE PUBLIC"])])


async def test_every_chunk_of_the_window_is_searched_read_and_paced(browser, monkeypatch):
    import time

    from src.scrapers.templates import idocmarket

    monkeypatch.setattr(idocmarket, "_SEARCH_PAUSE_MS", 1_500)
    t0 = time.monotonic()
    recs = await _run(browser, {"2026-10-01": (200, _ONE), "2026-10-31": (200, _TWO)},
                      "10/01/2026", "11/15/2026")
    assert [r.enrichment_data["instrument_number"] for r in recs] == ["90020", "90021"]
    assert time.monotonic() - t0 >= 1.5  # the second search waited


async def test_a_blocked_later_chunk_fails_the_whole_window(browser, monkeypatch):
    from src.scrapers.templates import idocmarket

    monkeypatch.setattr(idocmarket, "_SEARCH_PAUSE_MS", 0)
    # The portal's 403 block on chunk 2 must not deliver chunk 1 as the full window.
    with pytest.raises(RuntimeError, match="HTTP 403"):
        await _run(browser, {"2026-10-01": (200, _ONE), "2026-10-31": (403, "403 Forbidden")},
                   "10/01/2026", "11/15/2026")


async def test_a_search_that_does_not_submit_fails(browser):
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    with pytest.raises(PlaywrightTimeout):
        await _run(browser, {}, "10/01/2026", "10/02/2026", submits=False)
