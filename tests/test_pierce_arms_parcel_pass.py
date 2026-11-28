"""Pierce ARMS: the parcel pass must start on results page 1, or the run retries.

The parcel pass walks the results forward from wherever the grid is. Before
2026-11-27 a failed "First page" click was swallowed, so on a multi-page search the
pass started on the last page and every earlier page's rows were delivered without a
parcel (and without the address lookups that need one) while the run reported DONE.

Real Chromium on a local page that mimics the ARMS pager: a page-index dropdown and a
First button whose click handler is controlled per test.
"""
import pytest

from src.scrapers import pierce_wa_probate as arms
from src.scrapers.pierce_wa_probate import PierceWAProbateScraper
from src.scrapers.reliability import TransientScrapeError

_SELECT_ID = "cphNoMargin_cphNoMargin_OptionsBar1_ItemList"


def _html(on_first_click: str, *, with_button: bool = True) -> str:
    button = (f"<input type='image' id='OptionsBar1_imgFirst' onclick=\"{on_first_click}\">"
              if with_button else "")
    options = "".join(f"<option>{i}</option>" for i in range(1, 5))
    return (f"<html><body><select id='{_SELECT_ID}'>{options}</select>{button}"
            f"<script>document.getElementById('{_SELECT_ID}').selectedIndex = 3;</script>"
            "</body></html>")


_RESET_NOW = f"document.getElementById('{_SELECT_ID}').selectedIndex = 0; return false;"
_RESET_LATE = (f"setTimeout(() => document.getElementById('{_SELECT_ID}').selectedIndex = 0,"
               " 1200); return false;")
_NO_RESET = "return false;"


@pytest.fixture
async def scraper(monkeypatch):
    from playwright.async_api import async_playwright

    monkeypatch.setattr(arms, "_PAGE_ADVANCE_POLL_MS", 300)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            s = PierceWAProbateScraper()
            s.page = await browser.new_page()
            yield s
        finally:
            await browser.close()


async def test_reset_to_page_one_is_confirmed(scraper):
    await scraper.page.set_content(_html(_RESET_NOW))
    await scraper._return_to_first_page(pages_walked=4)
    assert await scraper._page_index() == 0


async def test_a_late_reset_is_still_accepted(scraper):
    """ARMS can apply the postback after the click's own wait has given up."""
    await scraper.page.set_content(_html(_RESET_LATE))
    await scraper._return_to_first_page(pages_walked=4)
    assert await scraper._page_index() == 0


async def test_a_reset_landing_in_the_final_wait_is_accepted(scraper, monkeypatch):
    """The reset lands after the last in-loop check (Codex Low): no needless retry."""
    monkeypatch.setattr(arms, "_PAGE_ADVANCE_POLLS", 1)
    late = (f"setTimeout(() => document.getElementById('{_SELECT_ID}').selectedIndex = 0,"
            " 100); return false;")
    await scraper.page.set_content(_html(late))
    await scraper._return_to_first_page(pages_walked=4)


async def test_a_reset_that_never_lands_retries_the_run(scraper):
    await scraper.page.set_content(_html(_NO_RESET))
    with pytest.raises(TransientScrapeError):
        await scraper._return_to_first_page(pages_walked=4)


async def test_a_missing_first_button_on_a_multipage_search_retries_the_run(scraper):
    await scraper.page.set_content(_html(_NO_RESET, with_button=False))
    with pytest.raises(TransientScrapeError):
        await scraper._return_to_first_page(pages_walked=4)


async def test_a_single_page_search_needs_no_reset(scraper):
    """One page: no First control, no dropdown, nothing to do."""
    await scraper.page.set_content("<html><body>results</body></html>")
    await scraper._return_to_first_page(pages_walked=1)
