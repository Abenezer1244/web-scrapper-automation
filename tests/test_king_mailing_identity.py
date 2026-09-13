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
                    "requested_pids": [], "mailing_attempted": 0}

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
        assert "1234500000" in st["requested_pids"]
        assert "1234500000" not in st["unreached"]

    def test_a_page_for_another_parcel_defers_the_parcel(self):
        st = self._run_stats(_SOMEONE_ELSE)
        assert "1234500000" in st["deferred"]

    def test_a_real_answer_does_not_defer(self):
        st = self._run_stats(_OURS)
        assert st["deferred"] == []
        st_none = self._run_stats("Parcel 123450-0000\nBilling Details\n")
        assert st_none["deferred"] == []


class TestPartialRenderIsNeverTerminal:
    """A page we never saw finish is not evidence that a section is empty.

    `wait_for_function` has a 4s timeout whose exception was swallowed, so a page
    that had rendered the parcel number but not yet the mailing section satisfied
    "our parcel, no Mailing Address block" and became a TERMINAL `none`. The sweep
    then cleared the deferred marker after that single attempt: permanent silent
    loss, the exact class of defect this whole change exists to remove (Codex).
    """

    class _NeverSettles(_Page):
        async def wait_for_function(self, *_a, **_k):
            raise TimeoutError("render never settled")

    def _run_unsettled(self, body: str, pid: str = "1234500000") -> dict:
        import contextlib

        class _S(_Scraper):
            def __init__(self, b):
                self.page = TestPartialRenderIsNeverTerminal._NeverSettles(b)

        results = {pid: {}}
        tax_urls = {pid: "https://payment.kingcounty.gov/Home/Index?Search=" + pid}
        st: dict = {"requested": 1, "deferred": [], "unreached": [],
                    "requested_pids": [], "mailing_attempted": 0}

        async def _go():
            return await kca._king_mailing_phase(results, tax_urls, st, lambda: False, 0.0)

        with contextlib.ExitStack() as stack:
            mp = stack.enter_context(pytest.MonkeyPatch.context())
            mp.setattr(kca, "BridgeScraper", lambda *a, **k: _S(body))
            real_sleep = asyncio.sleep

            async def _noop(_s):
                await real_sleep(0)

            mp.setattr(kca.asyncio, "sleep", _noop)
            asyncio.run(_go())
        return {"result": results[pid], "stats": st}

    def test_a_page_that_never_settled_is_not_none(self):
        out = self._run_unsettled("Parcel 123450-0000\nLoading...\n")
        # Unknown, so the parcel stays recoverable instead of being written off.
        assert out["result"]["mailing_lookup"] != "none"
        assert "1234500000" in out["stats"]["deferred"]

    def test_an_explicit_no_accounts_is_still_terminal_even_unsettled(self):
        # The county's own answer needs no render guarantee.
        out = self._run_unsettled("No accounts found for this search\n")
        assert out["result"]["mailing_lookup"] == "none"


class TestMailingBlockParser:
    """Blocks copied from King tax-bill pages rendered live on 2026-09-13."""

    def test_two_line_address(self):
        body = "Mailing Address\n750 BERING DRIVE, STE 500\nHOUSTON TX 77057\nPay by mail\nBilling Details\n"
        assert kca.parse_mailing_block(body) == "750 BERING DRIVE, STE 500, HOUSTON TX 77057"

    def test_three_line_address_keeps_city_state_zip_even_when_glued_to_the_next_label(self):
        body = ("Mailing Address\n2250 NW FLANDERS ST\nSUITE GARDEN 02\n"
                "PORTLAND OR 97210Pay by mail\nAnnual statement requested by\n")
        assert kca.parse_mailing_block(body) == "2250 NW FLANDERS ST, SUITE GARDEN 02, PORTLAND OR 97210"

    def test_a_po_box_number_does_not_end_the_block(self):
        body = "Mailing Address\nPO BOX 12345\nSEATTLE WA 98111-2345\nPay by mail\n"
        assert kca.parse_mailing_block(body) == "PO BOX 12345, SEATTLE WA 98111-2345"

    def test_canadian_postal_code(self):
        body = "Mailing Address\n310-1501 WEST BROADWAY\nVANCOUVER BC V6J 4Z6\nPay by mail\n"
        assert kca.parse_mailing_block(body) == "310-1501 WEST BROADWAY, VANCOUVER BC V6J 4Z6"

    def test_a_block_without_a_postal_line_is_not_an_address(self):
        assert kca.parse_mailing_block("Mailing Address\n400 KC ADMIN BLDG/4TH AVE\nSTE #830\nPay by mail\n") is None
        assert kca.parse_mailing_block("Mailing Address\nPay by mail\n") is None
        assert kca.parse_mailing_block("Billing Details\n") is None
        assert kca.parse_mailing_block("Mailing Address\n400 MAIN ST ZZ 12345\nPay by mail\n") is None

    def test_a_truncated_render_stays_unknown_in_the_phase(self):
        out = _run("Parcel 123450-0000\nMailing Address\n2250 NW FLANDERS ST\nSUITE GARDEN 02\nPay by mail\n")
        assert out.get("mailing_address") is None
        assert out["mailing_lookup"] == "error"

    def test_a_three_line_address_is_found_in_the_phase(self):
        out = _run("Parcel 123450-0000\nMailing Address\n2250 NW FLANDERS ST\nSUITE GARDEN 02\n"
                   "PORTLAND OR 97210Pay by mail\n")
        assert out["mailing_address"] == "2250 NW FLANDERS ST, SUITE GARDEN 02, PORTLAND OR 97210"
        assert out["mailing_lookup"] == "found"
