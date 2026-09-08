"""The rendered tax page must NAME our parcel before we believe its address.

`_king_mailing_phase` used to decide identity and extract the address
independently:

    if "No accounts" in body or probe in body:
        mailing_lookup = "none"
    if "Mailing Address" in body:          # <- not gated on the check above
        mailing_address = extracted

so ANY rendered page carrying a "Mailing Address" block wrote its address onto
whichever parcel we happened to be asking about. A stale tab, a wrong tax URL or
a redirect to another account would attach a stranger's mailing address to a real
lead, and a paid skip trace would then be billed against it. Same class of defect
as the eRealProperty parcel truncation, which this codebase already treats as
serious enough to discard a whole page over.

Verified live against payment.kingcounty.gov on 2026-09-07: the rendered body
does carry the parcel number alongside the Mailing Address block for real
parcels, so this gate costs nothing on the happy path.
"""
from __future__ import annotations

import asyncio

import pytest

from src.scrapers.enrichment import king_county_assessor as kca


class _Page:
    def __init__(self, body: str):
        self._body = body

    async def inner_text(self, _sel):
        return self._body

    async def wait_for_function(self, *_a, **_k):
        return None


class _Scraper:
    """Stands in for BridgeScraper: only safe_goto + page.inner_text are used."""

    def __init__(self, body: str):
        self.page = _Page(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def safe_goto(self, *_a, **_k):
        return None


def _run(body: str, pid: str = "1234500000") -> dict:
    """Drive phase 2 alone against one rendered page."""
    import contextlib

    results = {pid: {}}
    tax_urls = {pid: "https://payment.kingcounty.gov/Home/Index?Search=" + pid}
    st: dict = {"requested": 1, "deferred": [], "mailing_attempted": 0}

    async def _go():
        return await kca._king_mailing_phase(
            results, tax_urls, st, lambda: False, 0.0
        )

    with contextlib.ExitStack() as stack:
        mp = stack.enter_context(pytest.MonkeyPatch.context())
        mp.setattr(kca, "BridgeScraper", lambda *a, **k: _Scraper(body))

        real_sleep = asyncio.sleep

        async def _noop(_s):
            await real_sleep(0)

        mp.setattr(kca.asyncio, "sleep", _noop)
        return asyncio.run(_go())[pid]


_OURS = (
    "Parcel 123450-0000\n"
    "Mailing Address\n"
    "PO BOX 42\nRENO NV 89501\n"
    "Pay by mail\n"
)

_SOMEONE_ELSE = (
    "Parcel 999990-0000\n"
    "Mailing Address\n"
    "1 STRANGER LANE\nMIAMI FL 33101\n"
    "Pay by mail\n"
)


class TestIdentityGate:
    def test_a_page_that_names_our_parcel_yields_its_mailing_address(self):
        out = _run(_OURS)
        assert out["mailing_address"] == "PO BOX 42, RENO NV 89501"
        assert out["mailing_lookup"] == "found"

    def test_a_page_for_another_parcel_is_discarded(self):
        out = _run(_SOMEONE_ELSE)
        # The address on that page is real, it just is not OURS. A lead with no
        # mailing address is honest; a lead with a stranger's is a wrong mailing
        # and a paid skip trace on someone else's house.
        assert out.get("mailing_address") is None
        assert out["mailing_lookup"] == "identity_unverified"

    def test_no_accounts_is_still_a_real_answer(self):
        out = _run("No accounts found for this search\n")
        assert out.get("mailing_address") is None
        assert out["mailing_lookup"] == "none"

    def test_a_page_naming_our_parcel_with_no_mailing_block_is_none(self):
        out = _run("Parcel 123450-0000\nBilling Details\n")
        assert out.get("mailing_address") is None
        assert out["mailing_lookup"] == "none"

    def test_a_recovered_parcel_is_matched_on_its_RESOLVED_pin(self):
        """A malformed county PID is keyed by the source id but the county's page
        names the RESOLVED one, so the gate must compare against that."""
        import contextlib

        pid = "64116000027"          # malformed, as the recorder printed it
        resolved = "6411600027"      # the real King PIN
        results = {pid: {"resolved_parcel_id": resolved}}
        tax_urls = {pid: "https://payment.kingcounty.gov/Home/Index?Search=" + resolved}
        st: dict = {"requested": 1, "deferred": [], "mailing_attempted": 0}
        body = f"Parcel {resolved}\nMailing Address\nPO BOX 7\nSEATTLE WA 98101\nPay by mail\n"

        async def _go():
            return await kca._king_mailing_phase(results, tax_urls, st, lambda: False, 0.0)

        with contextlib.ExitStack() as stack:
            mp = stack.enter_context(pytest.MonkeyPatch.context())
            mp.setattr(kca, "BridgeScraper", lambda *a, **k: _Scraper(body))
            real_sleep = asyncio.sleep

            async def _noop(_s):
                await real_sleep(0)

            mp.setattr(kca.asyncio, "sleep", _noop)
            out = asyncio.run(_go())[pid]
        assert out["mailing_address"] == "PO BOX 7, SEATTLE WA 98101"
        assert out["mailing_lookup"] == "found"


class TestAttemptedButUnknownIsDeferred:
    """An attempted parcel with an UNKNOWN outcome still needs the durable marker.

    Navigation and extraction errors were swallowed, and the end-of-phase
    bookkeeping only deferred parcels OUTSIDE the lookup list. So a parcel that was
    actually visited and failed had neither a mailing address nor a marker: no
    later sweep could find it, and the job could still report enrichment complete
    (Codex).
    """

    def _run_stats(self, body: str, pid: str = "1234500000") -> dict:
        import contextlib

        results = {pid: {}}
        tax_urls = {pid: "https://payment.kingcounty.gov/Home/Index?Search=" + pid}
        st: dict = {"requested": 1, "deferred": [], "unreached": [],
                    "attempted": [], "mailing_attempted": 0}

        async def _go():
            return await kca._king_mailing_phase(results, tax_urls, st, lambda: False, 0.0)

        with contextlib.ExitStack() as stack:
            mp = stack.enter_context(pytest.MonkeyPatch.context())
            mp.setattr(kca, "BridgeScraper", lambda *a, **k: _Scraper(body))
            real_sleep = asyncio.sleep

            async def _noop(_s):
                await real_sleep(0)

            mp.setattr(kca.asyncio, "sleep", _noop)
            asyncio.run(_go())
        return st

    def test_an_unreadable_page_defers_the_parcel(self):
        st = self._run_stats("some unrelated page with no mailing block\n")
        assert "1234500000" in st["deferred"]
        assert "1234500000" in st["attempted"]
        assert "1234500000" not in st["unreached"]

    def test_a_page_for_another_parcel_defers_the_parcel(self):
        st = self._run_stats(_SOMEONE_ELSE)
        assert "1234500000" in st["deferred"]

    def test_a_real_answer_does_not_defer(self):
        st = self._run_stats(_OURS)
        assert st["deferred"] == []
        st_none = self._run_stats("Parcel 123450-0000\nBilling Details\n")
        assert st_none["deferred"] == []
