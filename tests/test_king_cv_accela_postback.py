"""King County Accela: the search waits for the RIGHT postback and never blurs a date box.

The live portal (fix/accela-search-form-postback, #551): the record-type dropdown posts
back from a setTimeout inside an UpdatePanel, so a networkidle wait right after
select_option can return before that postback starts, and the late refresh then resets
dates already typed. _postback therefore waits for the response of THAT control's POST
(its __EVENTTARGET), and the search is submitted with a DOM click so the focused date box
is never left. Until now only the live check exercised either (Codex #551 r2 P3).

The browser tests run real Chromium against a local page served for the portal's host
(page.route), shaped like the portal: a delayed record-type postback, a decoy control that
posts back first, and a search button that posts back on click.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from src.scrapers.king_cv_sources import kingco_accela as ka

_PAGE = f"https://{ka._HOST}/KINGCO/fake-search.aspx"
_RECORD_TYPE_ID = ka.SEL_RECORD_TYPE.lstrip("#")
_START_ID = ka.SEL_START_DATE.lstrip("#")
_SEARCH_ID = ka.SEL_SEARCH.lstrip("#")

_HTML = f"""<!doctype html><html><body>
<select id="{_RECORD_TYPE_ID}"><option value="">--</option>
  <option value="{ka.RECORD_TYPE_VALUE}">Enforcement</option></select>
<input id="{_START_ID}" value="">
<button id="decoy" type="button">x</button>
<button id="{_SEARCH_ID}" type="button">Search</button>
<script>
  window.posted = [];
  function postBack(target) {{
    return fetch("/KINGCO/postback.aspx", {{method: "POST",
      headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
      body: "__EVENTTARGET=" + encodeURIComponent(target)}})
      .then(() => window.posted.push(target));
  }}
  // The portal's shape: the dropdown's postback starts LATER, from a timer.
  document.getElementById("{_RECORD_TYPE_ID}").onchange = () => {{
    postBack("other$control");                                  // a different control, at once
    setTimeout(() => postBack("{_RECORD_TYPE_ID.replace('_', '$')}"), 1500);
  }};
  document.getElementById("{_SEARCH_ID}").onclick = () => postBack("{_SEARCH_ID.replace('_', '$')}");
</script></body></html>"""


def test_the_event_target_is_the_control_id_with_dollars():
    assert ka.postback_target(ka.SEL_SEARCH) == "ctl00$PlaceHolderMain$btnNewSearch"
    assert ka.postback_target(None) is None


@pytest.mark.parametrize("method,url,body,target,ours", [
    ("POST", _PAGE, "__EVENTTARGET=ctl00%24PlaceHolderMain%24btnNewSearch",
     "ctl00$PlaceHolderMain$btnNewSearch", True),
    ("POST", _PAGE, "__EVENTTARGET=other%24control", "ctl00$PlaceHolderMain$btnNewSearch", False),
    ("POST", _PAGE, None, "ctl00$PlaceHolderMain$btnNewSearch", False),
    ("GET", _PAGE, "__EVENTTARGET=ctl00%24PlaceHolderMain%24btnNewSearch",
     "ctl00$PlaceHolderMain$btnNewSearch", False),
    ("POST", "https://evil.example/KINGCO/x", "__EVENTTARGET=ctl00%24PlaceHolderMain%24btnNewSearch",
     "ctl00$PlaceHolderMain$btnNewSearch", False),
    ("POST", _PAGE, "__EVENTTARGET=anything", None, True),          # no control: any portal POST
])
def test_only_the_named_controls_portal_post_counts(method, url, body, target, ours):
    assert ka.is_portal_postback(method, url, body, target) is ours


async def _page(browser, received: list | None = None):
    """The fake portal; every POST's __EVENTTARGET and arrival time go into ``received``."""
    page = await browser.new_page()

    async def serve(route):
        if route.request.method == "POST":
            if received is not None:
                target = parse_qs(route.request.post_data or "").get("__EVENTTARGET", [""])[0]
                received.append((target, time.monotonic()))
            await asyncio.sleep(0.05)
            await route.fulfill(status=200, body="ok")
        else:
            await route.fulfill(status=200, content_type="text/html", body=_HTML)

    await page.route(f"https://{ka._HOST}/**", serve)
    await page.goto(_PAGE)
    return page


def _portal(page):
    async def no_pace():
        return None
    return SimpleNamespace(page=page, _pace=no_pace)


@pytest.mark.asyncio
async def test_the_record_type_wait_ignores_an_earlier_postback_and_catches_the_delayed_one():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            received: list = []
            page = await _page(browser, received)
            await ka.AccelaPortal._postback(
                _portal(page), lambda: page.select_option(ka.SEL_RECORD_TYPE, ka.RECORD_TYPE_VALUE),
                ka.SEL_RECORD_TYPE)
            returned_at = time.monotonic()
            arrived = dict(received)
            # The decoy posted first, and the wait still held until the dropdown's OWN
            # postback (started 1.5 s later) had reached the portal (Codex P2: decided by
            # arrival time at return, not by what is posted afterwards).
            assert received[0][0] == "other$control"
            own = ka.postback_target(ka.SEL_RECORD_TYPE)
            assert own in arrived and arrived[own] < returned_at
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_the_dom_click_submits_without_leaving_the_focused_date_box():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await _page(browser)
            await page.fill(ka.SEL_START_DATE, "10/01/2026")
            await page.focus(ka.SEL_START_DATE)
            await ka.AccelaPortal._postback(
                _portal(page), lambda: page.locator(ka.SEL_SEARCH).evaluate("el => el.click()"),
                ka.SEL_SEARCH)
            assert await page.evaluate("document.activeElement.id") == _START_ID
            assert await page.input_value(ka.SEL_START_DATE) == "10/01/2026"   # the typed date stands
            assert ka.postback_target(ka.SEL_SEARCH) in await page.evaluate("window.posted")
            # The contrast that made the DOM click necessary: a real click moves focus.
            await page.focus(ka.SEL_START_DATE)
            await page.click(ka.SEL_SEARCH)
            assert await page.evaluate("document.activeElement.id") == _SEARCH_ID
        finally:
            await browser.close()
