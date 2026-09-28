"""D5-03 (audit #5): every browser connection leaves through one SOCKS5 proxy that
resolves the host once, refuses a blocked address, and dials the address it checked.

Two layers, real sockets and real Chromium throughout:
  * the proxy itself, driven by a minimal SOCKS5 client: what it refuses and what it
    relays (a positive control admits loopback, and only loopback, so "refused"
    means the policy, not a broken relay);
  * the browser wired to it: the channel no Playwright route sees (WebRTC
    TURN-over-TCP) is refused, and traffic that IS allowed provably flows through it.
"""
from __future__ import annotations

import asyncio
import socket
import struct

import pytest
import websockets

from src.api.middleware.security import _ip_is_blocked
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper
from src.scrapers.egress_proxy import ALLOWED_PORTS, EgressProxy
from src.scrapers.enrichment.pierce_atip_owner import AtipOwnerPlainBrowser


class _AnyPortProxy(EgressProxy):
    """The real address policy with the port policy lifted, so a local listener on
    a random port isolates the address check."""

    _allowed_ports = frozenset(range(1, 65536))


class _LoopbackAdmittingProxy(_AnyPortProxy):
    """Admits loopback, and only loopback, for positive controls."""

    connects: list[tuple[str, int]] = []

    _is_blocked = staticmethod(lambda addr: False if addr.is_loopback else _ip_is_blocked(addr))

    async def _connect(self, host, port):
        type(self).connects.append((host, port))
        return await super()._connect(host, port)


async def _socks_connect(
    proxy_port: int, host: str, port: int, *, cmd: int = 1, reserved: int = 0
):
    """A minimal SOCKS5 client: returns (reply_code, reader, writer)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    assert await reader.readexactly(2) == b"\x05\x00"
    if ":" in host:
        addr = b"\x04" + socket.inet_pton(socket.AF_INET6, host)
    else:
        try:
            addr = b"\x01" + socket.inet_aton(host)
        except OSError:
            name = host.encode()
            addr = b"\x03" + bytes([len(name)]) + name
    writer.write(b"\x05" + bytes([cmd, reserved]) + addr + struct.pack(">H", port))
    await writer.drain()
    reply = await reader.readexactly(10)
    return reply[1], reader, writer


@pytest.fixture
async def internal_echo():
    """A loopback TCP service standing in for an internal endpoint; echoes, counts."""
    hits: list[int] = []

    async def handle(reader, writer):
        hits.append(1)
        data = await reader.read(1024)
        writer.write(data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1], hits
    server.close()
    await server.wait_closed()


async def _started(cls):
    proxy = cls()
    return proxy, await proxy.start()


# --- the proxy -----------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
async def test_an_internal_destination_is_refused_before_any_packet(internal_echo, host):
    """'localhost' is the DNS-rebinding case: the NAME is harmless, its answer is not.
    The proxy judges the answer it will dial."""
    port, hits = internal_echo
    proxy, proxy_port = await _started(_AnyPortProxy)
    try:
        code, _r, w = await _socks_connect(proxy_port, host, port)
        w.close()
    finally:
        await proxy.stop()
    assert code == 0x02  # connection not allowed by ruleset
    assert hits == []


@pytest.mark.parametrize("literal", [
    "::ffff:127.0.0.1",      # IPv4-mapped
    "64:ff9b::7f00:1",       # NAT64
    "::1",
    "64:ff9b::a9fe:a9fe",    # NAT64 metadata
])
async def test_ipv6_forms_of_internal_addresses_are_refused(internal_echo, literal):
    port, hits = internal_echo
    proxy, proxy_port = await _started(_AnyPortProxy)
    try:
        code, _r, w = await _socks_connect(proxy_port, literal, port)
        w.close()
    finally:
        await proxy.stop()
    assert code == 0x02
    assert hits == []


async def test_a_malformed_request_is_refused():
    proxy, proxy_port = await _started(EgressProxy)
    try:
        code, _r, w = await _socks_connect(proxy_port, "93.184.215.14", 443, reserved=1)
        w.close()
    finally:
        await proxy.stop()
    assert code == 0x01


async def test_a_port_outside_the_allowlist_is_refused():
    assert 22 not in ALLOWED_PORTS
    proxy, proxy_port = await _started(EgressProxy)
    try:
        # A public literal: the port check refuses before any connect is attempted.
        code, _r, w = await _socks_connect(proxy_port, "93.184.215.14", 22)
        w.close()
    finally:
        await proxy.stop()
    assert code == 0x02


@pytest.mark.parametrize("cmd", [2, 3])  # BIND, UDP ASSOCIATE
async def test_only_connect_is_supported(cmd):
    proxy, proxy_port = await _started(EgressProxy)
    try:
        code, _r, w = await _socks_connect(proxy_port, "93.184.215.14", 443, cmd=cmd)
        w.close()
    finally:
        await proxy.stop()
    assert code == 0x07


async def test_an_admitted_destination_is_relayed_both_ways(internal_echo):
    """Positive control for the refusals above."""
    port, hits = internal_echo
    proxy, proxy_port = await _started(_LoopbackAdmittingProxy)
    try:
        code, reader, writer = await _socks_connect(proxy_port, "127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        echoed = await asyncio.wait_for(reader.read(4), 5)
        writer.close()
    finally:
        await proxy.stop()
    assert code == 0x00
    assert echoed == b"ping"
    assert hits == [1]


# --- the browser wired to it ------------------------------------------------------------

_TURN_OVER_TCP = """
async (port) => {
  const pc = new RTCPeerConnection({
    iceServers: [{urls: `turn:127.0.0.1:${port}?transport=tcp`, username: "u", credential: "p"}],
    iceTransportPolicy: "relay",
  });
  pc.createDataChannel("d");
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((r) => setTimeout(r, 3000));
  pc.close();
}
"""




@pytest.mark.parametrize("scraper_cls", [BridgeScraper, AtipOwnerPlainBrowser])
async def test_a_turn_over_tcp_candidate_cannot_reach_an_internal_address(
    internal_echo, monkeypatch, scraper_cls
):
    """No Playwright route sees WebRTC. With the proxy it arrives there and is
    refused; without it, the browser dials the internal listener directly."""
    port, hits = internal_echo
    monkeypatch.setattr(settings, "SCRAPER_EGRESS_PROXY_ENABLED", True)
    async with scraper_cls() as scraper:
        await scraper.page.set_content("<html></html>")
        await scraper.page.evaluate(_TURN_OVER_TCP, port)
    await asyncio.sleep(0.2)
    assert hits == []


async def test_without_the_proxy_turn_over_tcp_reaches_the_internal_address(
    internal_echo, monkeypatch
):
    """The control that makes the test above mean something."""
    port, hits = internal_echo
    monkeypatch.setattr(settings, "SCRAPER_EGRESS_PROXY_ENABLED", False)
    async with BridgeScraper() as scraper:
        await scraper.page.set_content("<html></html>")
        await scraper.page.evaluate(_TURN_OVER_TCP, port)
    await asyncio.sleep(0.2)
    assert hits, "the direct TURN-over-TCP connection was expected to arrive"


class _ProxiedLoopbackScraper(BridgeScraper):
    """Loopback admitted at the route layer AND by the proxy, so an allowed
    connection can be followed end to end through the proxy."""

    _egress_proxy_cls = _LoopbackAdmittingProxy

    async def _ssrf_target_allowed(self, url, *, require_allowlisted):
        return True


async def test_allowed_browser_traffic_really_flows_through_the_proxy(monkeypatch):
    hits: list[str] = []

    async def echo(conn):
        hits.append(conn.request.path)
        async for message in conn:
            await conn.send(message)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    _LoopbackAdmittingProxy.connects = []
    monkeypatch.setattr(settings, "SCRAPER_EGRESS_PROXY_ENABLED", True)
    try:
        async with _ProxiedLoopbackScraper() as scraper:
            await scraper.page.set_content("<html></html>")
            echoed = await scraper.page.evaluate(
                """(url) => new Promise((resolve) => {
                     const ws = new WebSocket(url);
                     ws.onopen = () => ws.send("ping");
                     ws.onmessage = (e) => { resolve(e.data); ws.close(); };
                     ws.onerror = () => resolve("error");
                     setTimeout(() => resolve("timeout"), 5000);
                   })""",
                f"ws://127.0.0.1:{port}/via-proxy",
            )
    finally:
        server.close()
        await server.wait_closed()
    assert echoed == "ping"
    assert hits == ["/via-proxy"]
    assert ("127.0.0.1", port) in _LoopbackAdmittingProxy.connects


async def test_the_proxy_stops_with_the_browser(monkeypatch):
    monkeypatch.setattr(settings, "SCRAPER_EGRESS_PROXY_ENABLED", True)
    scraper = BridgeScraper()
    async with scraper:
        assert scraper._egress_proxy is not None
        port = scraper._egress_proxy._server.sockets[0].getsockname()[1]
    assert scraper._egress_proxy is None
    with pytest.raises(OSError):
        await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
