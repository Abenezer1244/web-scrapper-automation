"""The skip-trace PAUSE STATE in Redis: when a spend cap binds, when lookups resume.

Phase 1b-1b-iii (contract: tasks/todo-lookup-contacts.md, "FINAL contract"). The
dispatcher computes the resume times (it alone can read the queue tables) and
publishes them here on EVERY tick; the API (Phase 1c) reads its own account's view
with `read_pause_state()`. ADVISORY only: nothing here gates spending, the
dispatcher's in-lock database read does.

One hash, `KEY`:

  fence            the publisher's Postgres transaction id, 20 digits, zero-padded
  published_at     ISO UTC
  fresh_until      ISO UTC; past it the state is UNKNOWN (the heartbeat stopped)
  global           JSON scope (below), always present
  account_default  JSON scope for any account without its own field, always present
  <user_id>        JSON scope, only for an account whose advanced lookup does not fit

A scope is exactly {"normal_resume_at": iso|null, "advanced_resume_at": iso|"never"|null}:
null = fits now, "never" = the cap is below that lookup's cost. No spend or cap
numbers: a customer only learns when lookups resume.

Writes go through one Lua script that replaces the hash only when its fence is
newer than the stored one, so a slow tick that read the database before another
tick's claim can never put back the older view. Imports only the stdlib (the
caller passes the beat interval and the Redis client): the API imports this
module, and importing `src.workers` would build the Celery app.
"""
from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

KEY = "bridgeleads:skip_trace:pause:v1"
NEVER = "never"
# Slack on the heartbeat beyond two beat intervals: the beat container gap on a
# deploy (39-50 s) plus a slow tick (two 30 s Tracerfy POSTs).
GRACE = timedelta(seconds=120)

PAUSED = "paused"
NOT_PAUSED = "not_paused"
UNKNOWN = "unknown"

_FENCE_RE = re.compile(r"\d{20}")
_SCOPE_KEYS = {"normal_resume_at", "advanced_resume_at"}

# KEYS[1] = the hash; ARGV[1] = incoming fence; ARGV[2] = TTL seconds; ARGV[3..] =
# field, value pairs (the fence among them). A stored fence that is not exactly 20
# digits is treated as absent, so a corrupt value cannot block every later publish.
# Equal-length digit strings compare as numbers. HSET runs pair by pair: one call
# with every account would overrun Lua's unpack limit at a few thousand fields.
_PUBLISH_LUA = """
local cur = redis.call('HGET', KEYS[1], 'fence')
if cur and string.len(cur) == 20 and string.match(cur, '^%d+$') and cur >= ARGV[1] then
  return 0
end
redis.call('DEL', KEYS[1])
for i = 3, #ARGV, 2 do
  redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""


class ScopeResume(NamedTuple):
    """When each kind of lookup can run again: None = now, NEVER, or a UTC time."""
    normal: datetime | str | None
    advanced: datetime | str | None


NOT_BINDING = ScopeResume(None, None)


class PauseState(NamedTuple):
    status: str  # PAUSED, NOT_PAUSED or UNKNOWN
    normal_resume_at: datetime | str | None = None
    advanced_resume_at: datetime | str | None = None


def fence_str(xid: int | str) -> str:
    """A transaction id as the 20-digit string the script compares."""
    s = f"{int(xid):020d}"
    if not _FENCE_RE.fullmatch(s):
        raise ValueError(f"fence out of range: {xid!r}")
    return s


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _encode_value(v: datetime | str | None) -> str | None:
    if v is None or v == NEVER:
        return v
    return _iso(v)


def encode_scope(scope: ScopeResume) -> str:
    return json.dumps({"normal_resume_at": _encode_value(scope.normal),
                       "advanced_resume_at": _encode_value(scope.advanced)})


def heartbeat_seconds(interval_s: int) -> timedelta:
    """How long one publish stays fresh: two beat intervals plus GRACE. A plain
    beat interval restarts in full on every deploy, so the gap between two ticks
    can reach two intervals and the container gap."""
    return timedelta(seconds=2 * interval_s) + GRACE


def ttl_seconds(now: datetime, interval_s: int, scopes: list[ScopeResume]) -> int:
    """The key's TTL: the heartbeat, or longer to cover the latest finite resume
    time (NEVER has none). Rounded UP: EXPIRE takes whole seconds, and rounding
    down could expire the key before `fresh_until`."""
    span = heartbeat_seconds(interval_s)
    for s in scopes:
        for v in s:
            if isinstance(v, datetime):
                span = max(span, v - now + GRACE)
    return max(1, math.ceil(span.total_seconds()))


def _run_script(r, fence: str, ttl: int, fields: dict[str, str]) -> bool:
    argv: list[str] = [fence, str(ttl)]
    for k, v in fields.items():
        argv += [k, v]
    return bool(r.register_script(_PUBLISH_LUA)(keys=[KEY], args=argv))


def publish(
    r,
    *,
    fence: str,
    now: datetime,
    interval_s: int,
    global_scope: ScopeResume,
    account_default: ScopeResume,
    accounts: dict[str, ScopeResume],
) -> bool:
    """Replace the hash with this tick's view unless a newer fence already wrote
    it. True when written."""
    fields = {
        "fence": fence,
        "published_at": _iso(now),
        "fresh_until": _iso(now + heartbeat_seconds(interval_s)),
        "global": encode_scope(global_scope),
        "account_default": encode_scope(account_default),
    }
    for user_id, scope in accounts.items():
        fields[str(user_id)] = encode_scope(scope)
    ttl = ttl_seconds(now, interval_s, [global_scope, account_default, *accounts.values()])
    return _run_script(r, fence, ttl, fields)


def publish_tombstone(r, *, fence: str, interval_s: int) -> bool:
    """Dispatch is switched off: replace the hash with a fenced marker that reads
    UNKNOWN at once. A plain DEL would let an older in-flight publish recreate a
    'paused' view; the fence stops that."""
    ttl = math.ceil(heartbeat_seconds(interval_s).total_seconds())
    return _run_script(r, fence, ttl, {"fence": fence, "state": "disabled"})


class _MalformedError(Exception):
    pass


def _text(v) -> str | None:
    if v is None:
        return None
    return v.decode("utf-8") if isinstance(v, bytes) else v


def _parse_utc(s) -> datetime:
    if not isinstance(s, str):
        raise _MalformedError
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise _MalformedError from None
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise _MalformedError
    return dt


def _parse_scope(raw: str | None) -> ScopeResume:
    if raw is None:
        raise _MalformedError
    try:
        obj = json.loads(raw)
    except ValueError:
        raise _MalformedError from None
    if not isinstance(obj, dict) or set(obj) != _SCOPE_KEYS:
        raise _MalformedError
    normal = obj["normal_resume_at"]
    advanced = obj["advanced_resume_at"]
    return ScopeResume(
        None if normal is None else _parse_utc(normal),
        None if advanced is None else NEVER if advanced == NEVER else _parse_utc(advanced),
    )


def _combine(a, b, now: datetime):
    """One cost across two scopes: NEVER dominates, else the later time still
    ahead of now; a time already past counts as 'fits now'."""
    if a == NEVER or b == NEVER:
        return NEVER
    ahead = [t for t in (a, b) if t is not None and t > now]
    return max(ahead) if ahead else None


def read_pause_state(r, user_id: str, now: datetime) -> PauseState:
    """This account's pause state. Reads a FIXED field list (its own field, never
    the whole hash or another tenant's). Anything missing, stale or malformed, or
    Redis failing, is UNKNOWN: never read as 'not paused'."""
    try:
        vals = r.hmget(KEY, ["published_at", "fresh_until", "fence", "global",
                             "account_default", str(user_id)])
    except Exception:  # noqa: BLE001 - the contract: Redis down reads as UNKNOWN
        return PauseState(UNKNOWN)
    published_at, fresh_until, fence, glob, default, own = (_text(v) for v in vals)
    try:
        if fence is None or not _FENCE_RE.fullmatch(fence):
            raise _MalformedError
        _parse_utc(published_at)
        if _parse_utc(fresh_until) <= now:
            return PauseState(UNKNOWN)
        g = _parse_scope(glob)
        a = _parse_scope(own if own is not None else default)
        if own is not None:
            _parse_scope(default)
    except _MalformedError:
        return PauseState(UNKNOWN)
    normal = _combine(g.normal, a.normal, now)
    advanced = _combine(g.advanced, a.advanced, now)
    if normal is None and advanced is None:
        return PauseState(NOT_PAUSED)
    return PauseState(PAUSED, normal, advanced)


__all__ = [
    "GRACE", "KEY", "NEVER", "NOT_BINDING", "NOT_PAUSED", "PAUSED", "UNKNOWN",
    "PauseState", "ScopeResume", "encode_scope", "fence_str", "heartbeat_seconds",
    "publish", "publish_tombstone", "read_pause_state", "ttl_seconds",
]
