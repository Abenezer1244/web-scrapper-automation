"""The scraper browser's single egress point: a SOCKS5 proxy that dials only
addresses the SSRF policy approved (audit #5, D5-03).

Playwright's routes see the browser's HTTP(S) requests, and `route_web_socket` its
WebSockets, but neither sees every socket Chromium opens (WebRTC TURN-over-TCP,
speculative preconnects), and every check they make is check-then-use: Python
resolves the host, then Chromium resolves it again, so a DNS answer that changes in
between (rebinding) reaches an internal address.

With this proxy configured (``socks5://127.0.0.1:<port>`` and the ``<-loopback>``
bypass rule, so even loopback targets come here), Chromium hands over the HOSTNAME
of every TCP connection it makes and never resolves it itself. Measured on
Chromium 151, headed and headless: page loads, fetch, WebSockets and a TURN-over-TCP
candidate aimed at a loopback listener all arrived here, none went direct. This
proxy then:

  * accepts only CONNECT (BIND and UDP ASSOCIATE are refused; UDP is already off
    through ``--webrtc-ip-handling-policy=disable_non_proxied_udp``);
  * accepts only the ports in ``ALLOWED_PORTS``;
  * resolves the host ONCE, refuses the whole host if ANY answer is blocked (the
    same rule as ``src.utils.pinned_http``), and connects to the exact address it
    checked;
  * relays bytes and nothing else: TLS stays end to end, no header is read or added.

SOCKS5 rather than an HTTP proxy on purpose: an HTTP proxy must parse and re-frame
plain-HTTP requests (request smuggling, Host/URI mismatches); SOCKS5 carries every
connection as an opaque tunnel.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct

from src.api.middleware.security import _ip_is_blocked
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.egress_proxy")

# County portals, their CDNs and captcha providers. A connection to any other
# port is refused and logged (host and port only), so a portal that needs one
# shows up in the logs rather than failing silently.
ALLOWED_PORTS: frozenset[int] = frozenset({80, 443, 8080, 8443})

_HANDSHAKE_TIMEOUT = 10.0
_CONNECT_TIMEOUT = 15.0  # resolve + connect, all answers together
_IDLE_TIMEOUT = 120.0    # a read, or a write the peer will not drain
_SLOT_TIMEOUT = 1.0      # a client waits this long for a free slot, then is closed
_MAX_CONNECTIONS = 256
_CHUNK = 64 * 1024

# SOCKS5 reply codes (RFC 1928).
_OK = 0x00
_GENERAL_FAILURE = 0x01
_NOT_ALLOWED = 0x02
_HOST_UNREACHABLE = 0x04
_CMD_NOT_SUPPORTED = 0x07
_ATYP_NOT_SUPPORTED = 0x08


class _RefusedError(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code


class EgressProxy:
    """One SOCKS5 listener on 127.0.0.1. ``await start()`` returns its port;
    ``await stop()`` closes the listener and every open tunnel."""

    # The SSRF policy. A test may narrow it (to admit loopback for a local
    # server); production code never overrides it.
    _is_blocked = staticmethod(_ip_is_blocked)
    _allowed_ports: frozenset[int] = ALLOWED_PORTS

    def __init__(self) -> None:
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._slots = asyncio.Semaphore(_MAX_CONNECTIONS)

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
            except TimeoutError:
                pass
            self._server = None

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._slots.acquire(), _SLOT_TIMEOUT)
                acquired = True
            except TimeoutError:
                return  # full: close rather than queue (a slow client cannot pile up)
            await self._serve(reader, writer)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — one bad client must not take the proxy down
            _logger.debug("egress proxy client error: %s", str(exc)[:120])
        finally:
            if acquired:
                self._slots.release()
            writer.close()
            if task is not None:
                self._tasks.discard(task)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            host, port = await asyncio.wait_for(self._handshake(reader, writer), _HANDSHAKE_TIMEOUT)
        except _RefusedError as refused:
            await self._reply(writer, refused.code)
            return
        except (TimeoutError, asyncio.IncompleteReadError, ValueError):
            return
        try:
            upstream_reader, upstream_writer = await self._connect(host, port)
        except _RefusedError as refused:
            _logger.warning("egress proxy refused %s:%d (%s)", host, port, refused)
            await self._reply(writer, refused.code)
            return
        await self._reply(writer, _OK)
        try:
            await asyncio.gather(
                self._pipe(reader, upstream_writer), self._pipe(upstream_reader, writer)
            )
        finally:
            upstream_writer.close()

    async def _handshake(self, reader, writer) -> tuple[str, int]:
        version, n_methods = await reader.readexactly(2)
        if version != 5:
            raise ValueError("not SOCKS5")
        methods = await reader.readexactly(n_methods)
        if 0x00 not in methods:  # no authentication is the only method offered
            writer.write(b"\x05\xff")
            await writer.drain()
            raise ValueError("no acceptable method")
        writer.write(b"\x05\x00")
        await writer.drain()

        version, cmd, reserved, atyp = await reader.readexactly(4)
        if version != 5 or reserved != 0:
            raise _RefusedError(_GENERAL_FAILURE, "malformed request")
        if atyp == 0x01:
            host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
        elif atyp == 0x03:
            length = (await reader.readexactly(1))[0]
            try:
                host = (await reader.readexactly(length)).decode("idna").rstrip(".").lower()
            except UnicodeError as exc:
                raise _RefusedError(_GENERAL_FAILURE, "undecodable host") from exc
        elif atyp == 0x04:
            host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        else:
            raise _RefusedError(_ATYP_NOT_SUPPORTED, "address type not supported")
        port = struct.unpack(">H", await reader.readexactly(2))[0]
        if cmd != 0x01:
            raise _RefusedError(_CMD_NOT_SUPPORTED, "only CONNECT is supported")
        if not host:
            raise _RefusedError(_NOT_ALLOWED, "empty host")
        return host, port

    async def _connect(self, host: str, port: int):
        if port not in self._allowed_ports:
            raise _RefusedError(_NOT_ALLOWED, f"port {port} is not allowed")
        try:
            return await asyncio.wait_for(self._resolve_and_dial(host, port), _CONNECT_TIMEOUT)
        except TimeoutError as exc:
            raise _RefusedError(_HOST_UNREACHABLE, "resolve/connect timed out") from exc

    async def _resolve_and_dial(self, host: str, port: int):
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise _RefusedError(_HOST_UNREACHABLE, "could not resolve") from exc
        if not infos:
            raise _RefusedError(_HOST_UNREACHABLE, "no address")
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
            except ValueError as exc:
                raise _RefusedError(_NOT_ALLOWED, "resolved to an invalid address") from exc
            if self._is_blocked(addr):
                raise _RefusedError(_NOT_ALLOWED, "resolves to a blocked address")

        last: Exception | None = None
        for family, socktype, proto, _canon, sockaddr in infos:
            # Our own socket, connected to the exact sockaddr checked above: nothing
            # between the check and the connect can resolve the name again.
            sock = socket.socket(family, socktype, proto)
            sock.setblocking(False)
            try:
                await loop.sock_connect(sock, sockaddr)
                return await asyncio.open_connection(sock=sock)
            except OSError as exc:
                sock.close()
                last = exc
        raise _RefusedError(_HOST_UNREACHABLE, f"connect failed: {type(last).__name__}")

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int) -> None:
        try:
            writer.write(bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await asyncio.wait_for(reader.read(_CHUNK), _IDLE_TIMEOUT)
                if not data:
                    break
                writer.write(data)
                await asyncio.wait_for(writer.drain(), _IDLE_TIMEOUT)
        except (TimeoutError, ConnectionError, OSError):
            pass
        finally:
            try:
                if writer.can_write_eof():
                    writer.write_eof()
            except (ConnectionError, OSError, RuntimeError):
                pass
