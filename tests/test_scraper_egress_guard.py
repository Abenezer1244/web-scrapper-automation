"""The scraper browser's egress guard covers the channels ``context.route`` does not.

Audit #5 / S3-14. ``context.route("**/*")`` sees HTTP(S) requests from pages, frames
and workers, but NOT WebSockets, NOT service-worker fetches, and not UDP (WebRTC,
QUIC). County pages are untrusted content, so each of those is a way for a hostile
page to reach an internal address from the worker, which holds every secret.

Real headless Chromium throughout; the loopback listener is a real WebSocket server
with a positive control proving it counts connections.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

from src.scrapers.base_scraper import UNROUTED_CHANNEL_ARGS, BridgeScraper


class _PlainScraper(BridgeScraper):
    """The stock-browser mode (Pierce ATIP owner lookup) must be guarded the same."""

    _plain_browser = True


@pytest.fixture
async def loopback_ws():
    """A real WebSocket server on 127.0.0.1 that counts handshakes."""
    hits: list[str] = []

    async def handler(conn):
        hits.append(conn.request.path)
        await conn.close()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, hits
    finally:
        server.close()
        await server.wait_closed()


async def test_loopback_listener_counts_a_direct_connection(loopback_ws):
    """Positive control: without the browser in the way, a connection is counted,
    so a zero in the tests below means refused, not a deaf listener."""
    port, hits = loopback_ws
    async with websockets.connect(f"ws://127.0.0.1:{port}/control"):
        pass
    await asyncio.sleep(0.1)
    assert hits == ["/control"]


_OPEN_WS = """
(url) => new Promise((resolve) => {
  let ws;
  try { ws = new WebSocket(url); } catch (e) { resolve("threw"); return; }
  ws.onopen = () => resolve("open");
  ws.onerror = () => resolve("error");
  ws.onclose = () => resolve("closed");
  setTimeout(() => resolve("timeout"), 5000);
})
"""


@pytest.mark.parametrize("scraper_cls", [BridgeScraper, _PlainScraper])
async def test_page_websocket_to_loopback_is_refused(loopback_ws, scraper_cls):
    port, hits = loopback_ws
    async with scraper_cls() as scraper:
        await scraper.page.set_content("<html><body>county page</body></html>")
        outcome = await scraper.page.evaluate(_OPEN_WS, f"ws://127.0.0.1:{port}/hostile")
    await asyncio.sleep(0.2)
    assert hits == [], f"page reached the loopback WebSocket ({outcome})"
    assert outcome != "open"


@pytest.mark.parametrize("scraper_cls", [BridgeScraper, _PlainScraper])
async def test_service_worker_registration_is_blocked(scraper_cls):
    """A service worker's fetches bypass context.route entirely, so a page must not
    be able to install one."""
    origin = "https://sw-probe.test"

    async def serve(route):
        if route.request.url.endswith("/sw.js"):
            await route.fulfill(
                status=200, content_type="text/javascript",
                body="self.addEventListener('fetch', () => {});",
            )
        else:
            await route.fulfill(status=200, content_type="text/html", body="<html></html>")

    async with scraper_cls() as scraper:
        # A service worker's script is fetched through CONTEXT routes (a page route
        # never sees it). Context routes run newest first, so this one answers the
        # probe origin before the SSRF guard, without any network or DNS.
        await scraper._context.route(f"{origin}/**", serve)
        await scraper.page.goto(f"{origin}/")
        # Blocked, register() resolves with no registration (Playwright's
        # "block"), so the proof is what the browser actually holds afterwards.
        installed = await scraper.page.evaluate(
            """async () => {
                 if (!('serviceWorker' in navigator)) return 0;
                 try { await navigator.serviceWorker.register('/sw.js'); } catch (e) {}
                 await new Promise((r) => setTimeout(r, 800));
                 return (await navigator.serviceWorker.getRegistrations()).length;
               }"""
        )
    assert installed == 0


async def test_guard_fails_closed_when_it_cannot_decide():
    """An internal error in the guard must refuse the request, not wave it through."""

    class _Unreadable:
        """A request whose URL cannot be read (the guard's own failure, not a
        validation verdict)."""

        @property
        def url(self):
            raise RuntimeError("request object unusable")

    assert await BridgeScraper()._ssrf_nav_allowed(_Unreadable()) is False


def test_launch_disables_quic_and_non_proxied_webrtc_udp():
    """QUIC (and WebTransport over it) never passes through the route guard, so the
    browser launches with it off. Asserted on the argument list __aenter__ passes
    (no page API reveals it); WebRTC is proven by behaviour below."""
    assert "--disable-quic" in UNROUTED_CHANNEL_ARGS


_GATHER_UDP_CANDIDATES = """
async () => {
  const pc = new RTCPeerConnection({iceServers: []});
  pc.createDataChannel("probe");
  const seen = [];
  pc.onicecandidate = (e) => { if (e.candidate) seen.push(e.candidate.candidate); };
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((r) => {
    if (pc.iceGatheringState === "complete") return r();
    pc.onicegatheringstatechange = () => pc.iceGatheringState === "complete" && r();
    setTimeout(r, 4000);
  });
  pc.close();
  return seen.filter((c) => / udp /i.test(c));
}
"""


@pytest.mark.parametrize("scraper_cls", [BridgeScraper, _PlainScraper])
async def test_page_gathers_no_webrtc_udp_candidates(scraper_cls):
    """WebRTC UDP (STUN, direct peers) bypasses every route; with the policy flag
    the browser gathers no UDP candidate at all."""
    async with scraper_cls() as scraper:
        await scraper.page.set_content("<html></html>")
        udp = await scraper.page.evaluate(_GATHER_UDP_CANDIDATES)
    assert udp == []
