"""Outbound HTTP whose socket goes only to an address the SSRF policy approved.

`validate_outbound_webhook()` resolves a customer's webhook host and rejects a
private/metadata answer, but `requests` then resolves the host AGAIN to connect.
A TTL=0 record can answer public to the check and private to the connect (DNS
rebinding, audit finding F-03). This module removes the second lookup.

urllib3 opens every new TCP connection through ``HTTPConnection._new_conn()``,
and ``HTTPSConnection.connect()`` wraps TLS around that socket afterwards with
``server_hostname=self.host``. Overriding ``_new_conn()`` therefore pins the
socket without touching TLS: SNI, certificate verification against the hostname,
and the Host header all keep using the name the customer configured.

``_new_conn()`` below resolves once, refuses the whole host if ANY answer is
blocked (same rule as ``_assert_resolved_ips_safe``), then connects to the exact
``sockaddr`` it checked, never re-resolving.

A keep-alive connection reused from the pool skips ``_new_conn()``. That is safe:
its socket stays connected to the address that was checked when it was opened,
so a later DNS change cannot move it.
"""
import ipaddress
import socket

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NameResolutionError,
    NewConnectionError,
)
from urllib3.util.connection import _set_socket_options, allowed_gai_family
from urllib3.util.timeout import _DEFAULT_TIMEOUT

from src.api.middleware.security import _ip_is_blocked


class BlockedDestinationError(NewConnectionError):
    """The host resolved to an address the SSRF policy forbids. Never retry it."""


class _PinnedConnectionMixin:
    # The SSRF policy. A test may narrow it (e.g. to admit loopback for a local
    # server); production code never overrides it.
    _is_blocked = staticmethod(_ip_is_blocked)

    def _new_conn(self) -> socket.socket:
        host = self._dns_host.strip("[]")
        try:
            infos = socket.getaddrinfo(host, self.port, allowed_gai_family(), socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc
        if not infos:
            raise NewConnectionError(self, "Failed to establish a new connection: no address")

        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
            except ValueError:
                raise BlockedDestinationError(self, "Destination resolved to an invalid address") from None
            if self._is_blocked(addr):
                # Host only, never the address: the caller logs this message.
                raise BlockedDestinationError(self, f"Destination {self.host} resolves to a blocked address")

        err: OSError | None = None
        for family, socktype, proto, _canonname, sockaddr in infos:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                _set_socket_options(sock, self.socket_options)
                if self.timeout is not _DEFAULT_TIMEOUT:
                    sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)  # the checked sockaddr, no second lookup
                return sock
            except OSError as exc:  # TimeoutError included
                if sock is not None:
                    sock.close()
                err = exc
        if isinstance(err, TimeoutError):
            raise ConnectTimeoutError(
                self, f"Connection to {self.host} timed out. (connect timeout={self.timeout})"
            ) from err
        raise NewConnectionError(self, f"Failed to establish a new connection: {err}") from err


class PinnedHTTPConnection(_PinnedConnectionMixin, HTTPConnection):
    pass


class PinnedHTTPSConnection(_PinnedConnectionMixin, HTTPSConnection):
    pass


class _PinnedHTTPPool(HTTPConnectionPool):
    ConnectionCls = PinnedHTTPConnection


class _PinnedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = PinnedHTTPSConnection


class PinnedAdapter(HTTPAdapter):
    """requests adapter whose pools only ever build pinned connections."""

    pool_classes = {"http": _PinnedHTTPPool, "https": _PinnedHTTPSPool}

    def __init__(self) -> None:
        # max_retries=0: a blocked destination must surface once, not be retried
        # inside urllib3 (it is not retryable anywhere, see is_blocked_destination).
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs) -> None:
        super().init_poolmanager(*args, **kwargs)
        # Before any pool exists: pools are only created on the first request.
        self.poolmanager.pool_classes_by_scheme = dict(self.pool_classes)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        # Through a proxy the proxy resolves and connects, outside this check.
        raise ValueError("Proxies are not permitted for pinned outbound requests")


def pinned_session(adapter_cls: type[PinnedAdapter] = PinnedAdapter) -> requests.Session:
    """A Session that resolves and connects locally, to approved addresses only."""
    session = requests.Session()
    session.trust_env = False  # an ambient HTTPS_PROXY would bypass the pin
    session.mount("http://", adapter_cls())
    session.mount("https://", adapter_cls())
    return session


def is_blocked_destination(exc: BaseException) -> bool:
    """True if ``exc`` was caused by a BlockedDestinationError at connect time.

    requests wraps it as ConnectionError(MaxRetryError(reason=...)), so walk the
    cause/context chain, ``.reason`` and ``args``, visiting each object once.
    """
    seen: set[int] = set()
    stack: list[object] = [exc]
    while stack:
        cur = stack.pop()
        if not isinstance(cur, BaseException) or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, BlockedDestinationError):
            return True
        stack.extend((cur.__cause__, cur.__context__, getattr(cur, "reason", None), *cur.args))
    return False
