"""Phase-1 request accounting for the King eRealProperty pass.

Three defects here turned a transient upstream blip into a multi-day outage that
could not be explained afterwards. Each one gets a test that fails on the old
shape:

  1. A failed fetch `continue`d PAST the pacing sleep, so the loop sped up by
     roughly 6x exactly when the source was asking us to slow down.
  2. The breaker threshold was only evaluated after a SUCCESSFUL `safe_get`, so
     an exception-only outage (DNS, TLS, timeouts) never tripped it at all.
  3. Parcels that failed BEFORE the trip got no `deferred` marker, so no later
     sweep could ever find them. The production job proves it: 17,157 requested,
     17,107 deferred, exactly 50 unaccounted for.

No mocks of our own code. A tiny fake stands in for `requests`' Response and for
the network itself, because the alternative is asking King County for fifty
failures on purpose.
"""
from __future__ import annotations

import asyncio

import pytest

from src.scrapers.enrichment import king_county_assessor as kca


class _Resp:
    """The two attributes the phase-1 loop reads off a Response."""

    def __init__(self, status: int, text: str = "", location: str | None = None,
                 url: str = "https://blue.kingcounty.com/x"):
        self.status_code = status
        self.text = text
        self.url = url
        self.headers = {"Location": location} if location else {}


def _pids(n: int) -> list[str]:
    # Well-formed 10-digit King PINs so nothing is rejected before the fetch.
    return [f"12345{i:05d}" for i in range(n)]


@pytest.fixture
def no_admission(monkeypatch):
    """Bypass the Redis lease: these tests are about the fetch loop, not admission."""
    class _Always:
        admitted = True

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(
        "src.scrapers.enrichment.source_admission.SourceAdmission", _Always
    )


@pytest.fixture
def offline(monkeypatch):
    """Never touch the network, the DB health gate, or the durable block record."""
    monkeypatch.setattr(kca, "check_source_or_raise", lambda *_a, **_k: None)
    monkeypatch.setattr(kca, "record_source_blocked", lambda *_a, **_k: None)


@pytest.fixture
def instant_sleep(monkeypatch):
    """Make the pacing free so a 120-parcel loop runs in milliseconds.

    Captures the REAL asyncio.sleep first: patching the module attribute and then
    calling asyncio.sleep inside the replacement calls the replacement.
    """
    real_sleep = asyncio.sleep

    async def _noop(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(kca.asyncio, "sleep", _noop)


class TestPacingOnFailure:
    def test_a_failed_fetch_still_pays_the_pace(self, monkeypatch, offline, no_admission):
        slept: list[float] = []

        async def _sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(kca.asyncio, "sleep", _sleep)
        monkeypatch.setattr(kca, "safe_get", lambda *a, **k: _Resp(302, location="/blocked"))

        stats: dict = {}
        asyncio.run(kca.batch_enrich_king_county(
            _pids(10), stats=stats, do_mailing=False, pace_s=0.1,
        ))
        # Ten failed fetches must produce ten paces. The old shape produced ZERO:
        # `if r.status_code != 200: continue` jumped over the sleep at the bottom
        # of the loop, so a refusing source was polled as fast as the network
        # allowed. That is a positive feedback loop, not a backoff.
        assert len(slept) == 10

    def test_an_exception_also_pays_the_pace(self, monkeypatch, offline, no_admission):
        slept: list[float] = []

        async def _sleep(seconds):
            slept.append(seconds)

        def _boom(*_a, **_k):
            raise OSError("connection reset")

        monkeypatch.setattr(kca.asyncio, "sleep", _sleep)
        monkeypatch.setattr(kca, "safe_get", _boom)

        asyncio.run(kca.batch_enrich_king_county(
            _pids(5), stats={}, do_mailing=False, pace_s=0.1,
        ))
        assert len(slept) == 5


class TestBreakerSeesEveryFailureMode:
    def test_an_exception_only_outage_trips_the_breaker(self, monkeypatch, offline,
                                                        no_admission, instant_sleep):
        recorded: list[str] = []
        monkeypatch.setattr(kca, "record_source_blocked",
                            lambda _k, reason, *a, **kw: recorded.append(reason))

        def _boom(*_a, **_k):
            raise TimeoutError("read timeout")

        monkeypatch.setattr(kca, "safe_get", _boom)

        stats: dict = {}
        asyncio.run(kca.batch_enrich_king_county(
            _pids(120), stats=stats, do_mailing=False, pace_s=0.1,
        ))
        # The old shape evaluated the threshold INSIDE the try, after a successful
        # fetch, so a total connection outage appended `True` forever and never
        # tripped. A hard outage is the case the breaker exists for.
        assert recorded, "an exception-only outage must trip the breaker"
        assert "TimeoutError" in recorded[0]

    def test_the_persisted_reason_carries_an_outcome_histogram(self, monkeypatch, offline,
                                                               no_admission, instant_sleep):
        recorded: list[str] = []
        monkeypatch.setattr(kca, "record_source_blocked",
                            lambda _k, reason, *a, **kw: recorded.append(reason))
        monkeypatch.setattr(
            kca, "safe_get",
            lambda *a, **k: _Resp(302, location="https://kingcounty.gov/wall?tok=secret"),
        )

        asyncio.run(kca.batch_enrich_king_county(
            _pids(60), stats={}, do_mailing=False, pace_s=0.1,
        ))
        assert recorded
        reason = recorded[0]
        # "last status=302" described ONE request out of fifty. The histogram is
        # what makes the next incident explainable without guessing.
        assert "HTTP302x50" in reason
        assert "kingcounty.gov/wall" in reason
        # The query string can carry the parcel and a session token. Never logged.
        assert "secret" not in reason


class TestDeferredCoversEveryUnresolvedParcel:
    def test_parcels_that_failed_before_the_trip_are_deferred(self, monkeypatch, offline,
                                                              no_admission, instant_sleep):
        monkeypatch.setattr(kca, "record_source_blocked", lambda *_a, **_k: None)
        monkeypatch.setattr(kca, "safe_get", lambda *a, **k: _Resp(503))

        pids = _pids(120)
        stats: dict = {}
        asyncio.run(kca.batch_enrich_king_county(
            pids, stats=stats, do_mailing=False, pace_s=0.1,
        ))
        # EVERY parcel is unresolved, so every parcel must carry the marker. The
        # old shape deferred only `clean[i:]` — the tail — leaving the parcels
        # that had already failed with no durable marker and no way back. In
        # production that was exactly 50 rows: 17,157 requested, 17,107 deferred.
        assert set(stats["deferred"]) == set(pids)

    def test_an_ordinary_non_200_defers_that_parcel(self, monkeypatch, offline,
                                                    no_admission, instant_sleep):
        monkeypatch.setattr(kca, "safe_get", lambda *a, **k: _Resp(404))

        pids = _pids(3)
        stats: dict = {}
        asyncio.run(kca.batch_enrich_king_county(
            pids, stats=stats, do_mailing=False, pace_s=0.1,
        ))
        # A failed lookup is UNKNOWN, not "this parcel has no data".
        assert set(stats["deferred"]) == set(pids)


class TestSuccessPathUnchanged:
    _PAGE = (
        "<table>"
        "<td>Parcel Number</td><td>123450-0000</td>"
        "<td>Name</td><td>DOE JANE</td>"
        "<td>Site Address</td><td>1 MAIN ST 98101</td>"
        '<a href="https://payment.kingcounty.gov/Home/Index?Search=1234500000">tax</a>'
        "</table>"
    )

    def test_a_healthy_page_still_yields_property_owner_and_tax_url(
        self, monkeypatch, offline, no_admission, instant_sleep
    ):
        monkeypatch.setattr(kca, "safe_get", lambda *a, **k: _Resp(200, self._PAGE))

        tax_urls: dict[str, str] = {}
        stats: dict = {}
        out = asyncio.run(kca.batch_enrich_king_county(
            ["1234500000"], stats=stats, do_mailing=False,
            tax_urls_out=tax_urls, pace_s=0.1,
        ))
        assert out["1234500000"]["property_address"] == "1 MAIN ST 98101"
        assert out["1234500000"]["owner_name"] == "DOE JANE"
        assert tax_urls["1234500000"].startswith("https://payment.kingcounty.gov")
        assert stats["deferred"] == []
        assert stats["property_found"] == 1


class TestUnreachedIsNotTheSameAsDeferred:
    """`deferred` is the durable marker set; `unreached` is what we never tried.

    A retrying caller needs the difference. Charging a retry attempt to a parcel
    that was never tried burns its ceiling on work that never happened; NOT
    charging one that was tried and failed lets a permanently unanswerable parcel
    retry forever, which is how a bounded sweep starves its own backlog (Codex).
    """

    def test_a_failed_fetch_is_deferred_but_not_unreached(self, monkeypatch, offline,
                                                          no_admission, instant_sleep):
        monkeypatch.setattr(kca, "safe_get", lambda *a, **k: _Resp(404))
        pids = _pids(3)
        stats: dict = {}
        asyncio.run(kca.batch_enrich_king_county(
            pids, stats=stats, do_mailing=False, pace_s=0.1,
        ))
        assert set(stats["deferred"]) == set(pids)
        # We DID ask the county about all three. They must be chargeable.
        assert stats["unreached"] == []

    def test_the_budget_tail_is_both_deferred_and_unreached(self, monkeypatch, offline,
                                                            no_admission):
        real_sleep = asyncio.sleep

        async def _slow(_s):
            await real_sleep(0.02)

        monkeypatch.setattr(kca.asyncio, "sleep", _slow)
        monkeypatch.setattr(kca, "safe_get", lambda *a, **k: _Resp(200, ""))

        pids = _pids(40)
        stats: dict = {}
        asyncio.run(kca.batch_enrich_king_county(
            pids, stats=stats, do_mailing=False, pace_s=0.1, time_budget_s=0.05,
        ))
        assert stats["budget_exhausted"] is True
        assert stats["unreached"], "the parcels the budget never reached must be marked"
        # Everything unreached is also deferred; the reverse is not true.
        assert set(stats["unreached"]).issubset(set(stats["deferred"]))

    def test_the_parcel_that_tripped_the_breaker_counts_as_attempted(
        self, monkeypatch, offline, no_admission, instant_sleep
    ):
        monkeypatch.setattr(kca, "record_source_blocked", lambda *_a, **_k: None)
        monkeypatch.setattr(kca, "safe_get", lambda *a, **k: _Resp(503))

        pids = _pids(120)
        stats: dict = {}
        asyncio.run(kca.batch_enrich_king_county(
            pids, stats=stats, do_mailing=False, pace_s=0.1,
        ))
        # The breaker trips on the 50th request, so parcels 0..49 were all tried.
        assert set(stats["deferred"]) == set(pids)
        assert set(stats["unreached"]) == set(pids[50:])
        assert pids[49] not in stats["unreached"]
