"""Cross-process admission control for a shared external enrichment source.

The pacing inside the King enrichment loop is a per-call `asyncio.sleep`, which
bounds ONE pass and knows nothing about any other. So N concurrent King jobs made
N independent request streams against one county server, and nothing in the
system could see the total.

That is not hypothetical. On 2026-09-04 two 17,157-parcel King tax jobs overlapped
for roughly half an hour (07:40-10:33 and 09:03-09:32), each issuing a serial
eRealProperty stream, and the first circuit-breaker trip landed at 11:15 that
morning. Measured serial throughput is about 2.5 requests/second per pass, so the
combined load was roughly double what any single pass was designed to produce,
with no component aware of the total.

This module is the missing shared bound: a Redis lease that admits ONE King
enrichment pass at a time. Concurrent jobs then SERIALISE against the county
instead of multiplying against it.

Design notes:
  * FAIL-OPEN. If Redis is unavailable this admits the caller. Enrichment is
    best-effort and losing it entirely because a lock backend blipped would be a
    worse outcome than briefly running unbounded, which is exactly today's
    behaviour anyway.
  * OWNERSHIP TOKEN, not a bare key. Releasing by key alone lets a stalled holder
    whose TTL expired delete the lease a DIFFERENT worker has since acquired
    (Codex). Release is a compare-and-delete on a token only this holder knows.
  * BOUNDED WAIT. A caller that cannot get in soon gives up rather than spending
    its whole enrichment budget queueing. Its parcels are deferred and the
    background recovery sweep picks them up, which is a better outcome than a job
    that blocks for ten minutes and then does nothing.
  * The lease is a RATE bound, not a correctness lock. Nothing is corrupted if it
    is bypassed; the source is just asked more often than we would like.
"""
from __future__ import annotations

import time
import uuid

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("enrichment.source_admission")

# Long enough for a full chunked King pass (the caller's own budget is 600s) plus
# headroom, so a live holder is never evicted mid-pass. A crashed holder costs at
# most this long before the source frees up again.
_LEASE_TTL_S = 900

# How long a caller waits for admission before giving up and deferring its work.
_MAX_WAIT_S = 60.0
_POLL_S = 2.0


def _key(source_key: str) -> str:
    return f"bl:source_admission:{source_key}"


def _client():
    import redis as sync_redis

    return sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())


class SourceAdmission:
    """Context manager: `with SourceAdmission(KEY) as ok:` -- `ok` is False if we timed out.

    Never raises. `admitted` is True when the caller either holds the lease or was
    let through because Redis was unavailable.
    """

    def __init__(self, source_key: str, *, max_wait_s: float = _MAX_WAIT_S) -> None:
        self.source_key = source_key
        self.max_wait_s = max_wait_s
        self.admitted = False
        self._token = uuid.uuid4().hex
        self._client = None

    def __enter__(self) -> SourceAdmission:
        try:
            self._client = _client()
        except Exception as exc:  # noqa: BLE001 -- fail OPEN
            _logger.warning(
                "Source admission: Redis unavailable (%s) — admitting %s unbounded",
                str(exc)[:120], self.source_key,
            )
            self.admitted = True
            self._client = None
            return self
        deadline = time.monotonic() + self.max_wait_s
        while True:
            try:
                if self._client.set(_key(self.source_key), self._token,
                                    nx=True, ex=_LEASE_TTL_S):
                    self.admitted = True
                    return self
            except Exception as exc:  # noqa: BLE001 -- fail OPEN
                _logger.warning(
                    "Source admission: lease check failed (%s) — admitting %s",
                    str(exc)[:120], self.source_key,
                )
                self.admitted = True
                self._client = None
                return self
            if time.monotonic() >= deadline:
                _logger.info(
                    "Source admission: %s busy for %.0fs — deferring this pass",
                    self.source_key, self.max_wait_s,
                )
                self.admitted = False
                return self
            time.sleep(_POLL_S)

    def still_held(self) -> bool:
        """Renew the lease if we still own it. False means we LOST it.

        A fixed TTL with no renewal is not exclusion: an owner backfill paces
        2,000 rows at 0.6 s, which exceeds the lease on pacing alone, and at
        expiry a second worker enters while the first keeps going. Token-protected
        release stops us deleting a successor's lease; it does nothing about the
        overlap itself (Codex). A long-running caller therefore renews as it goes
        and STOPS issuing requests the moment renewal fails.

        Fails OPEN on a Redis error, for the same reason acquisition does: losing
        enrichment entirely because a lock backend blipped is the worse outcome.
        """
        if not self._client or not self.admitted:
            return True  # never acquired a real lease; nothing to lose
        try:
            renewed = self._client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
                1, _key(self.source_key), self._token, _LEASE_TTL_S,
            )
            if not renewed:
                _logger.warning(
                    "Source admission: lost the %s lease mid-pass — stopping requests",
                    self.source_key,
                )
                self.admitted = False
                return False
            return True
        except Exception as exc:  # noqa: BLE001 -- fail OPEN
            _logger.warning(
                "Source admission: renewal check failed (%s) — continuing",
                str(exc)[:120],
            )
            return True

    def __exit__(self, *exc_info) -> None:
        if not self._client or not self.admitted:
            return
        try:
            # Compare-and-delete: only the holder that wrote this token may
            # release it. A bare DEL would let a stalled holder whose TTL already
            # expired delete the lease another worker legitimately holds.
            self._client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end",
                1, _key(self.source_key), self._token,
            )
        except Exception:  # noqa: BLE001, S110 -- the TTL releases it anyway
            pass
