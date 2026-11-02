"""P0-03 5c: worker task failures and beat publish failures reach Sentry, sanitised.

The receiver tests run in a fresh interpreter (as in test_error_tracking). The
process tests start a REAL `celery worker` (prefork on Linux, as in production;
solo on Windows, where prefork is unsupported) and a REAL `celery beat` against
the test Redis, through tests/_p003_celery_app.py, and read the envelopes they
write to disk. They are skipped when the test Redis is unreachable.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
import redis

from src.config import settings
from tests.test_error_tracking import (
    _CANARY_EMAIL,
    _CANARY_SQL,
    _GOOD_DSN,
    _ROOT,
    _SHA,
    _assert_clean,
    _run,
)

_QUEUE = "p003test"
_TASK = "p003.fail_owner_lookup"


def _redis_up() -> bool:
    host = (urlparse(settings.REDIS_URL).hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "::1", "redis"}:
        return False
    try:
        return bool(redis.from_url(settings.REDIS_URL, socket_timeout=2).ping())
    except redis.RedisError:
        return False


needs_redis = pytest.mark.skipif(not _redis_up(), reason="needs the local/CI test Redis")


def test_worker_receiver_initialises_the_worker_client():
    out = _run(
        """
        from src.utils import error_tracking
        from src.workers import _init_worker_error_tracking

        _init_worker_error_tracking()
        report(active=sentry_sdk.get_client().is_active(), service=error_tracking._initialised[1])
        """
    )
    assert out["active"] is True and out["service"] == "worker"


def test_beat_receiver_initialises_beat_and_attaches_the_capture_once():
    out = _run(
        """
        import logging
        from src.utils import error_tracking
        from src.workers import _init_beat_error_tracking

        _init_beat_error_tracking()
        _init_beat_error_tracking()
        handlers = [h for h in logging.getLogger("celery.beat").handlers
                    if isinstance(h, error_tracking._BeatErrorCapture)]
        report(active=sentry_sdk.get_client().is_active(), service=error_tracking._initialised[1],
               handlers=len(handlers))
        """
    )
    assert (out["active"], out["service"], out["handlers"]) == (True, "beat", 1)


def test_receivers_stay_off_without_a_dsn():
    out = _run(
        """
        import logging
        from src.utils import error_tracking
        from src.workers import _init_beat_error_tracking, _init_worker_error_tracking

        _init_worker_error_tracking()
        _init_beat_error_tracking()
        handlers = [h for h in logging.getLogger("celery.beat").handlers
                    if isinstance(h, error_tracking._BeatErrorCapture)]
        report(active=sentry_sdk.get_client().is_active(), handlers=len(handlers))
        """,
        dsn=None,
    )
    assert out["active"] is False and out["handlers"] == 0


def test_beat_capture_sends_exceptions_only_never_the_message():
    out = _run(
        f"""
        import logging
        from src.utils.error_tracking import capture_beat_errors, init_error_tracking

        assert init_error_tracking("beat", transport=transport)
        capture_beat_errors()
        log = logging.getLogger("celery.beat")
        try:
            raise ConnectionRefusedError("refused")
        except ConnectionRefusedError:
            # logged without exc_info while an exception is active: still not sent
            log.error("beat: Connection error to {_CANARY_EMAIL}. Trying again in 2.0 seconds...")
        try:
            raise ValueError("cannot encode {_CANARY_SQL}")
        except ValueError:
            log.error("Message Error: {_CANARY_SQL}", exc_info=True)
        log.warning("not an error", exc_info=(ValueError, ValueError("x"), None))
        sentry_sdk.flush()
        report()
        """
    )
    assert out["item_types"] == ["event"]  # only the ERROR record that carried an exception
    _assert_clean(out["envelopes"][0])
    event = json.loads(out["envelopes"][0].splitlines()[-1])
    assert event["tags"] == {"service": "beat"}
    assert event["exception"]["values"][-1]["type"] == "ValueError"


def test_worker_capture_ignores_a_non_worker_client():
    out = _run(
        """
        import logging
        from src.utils import error_tracking

        assert error_tracking.init_error_tracking("worker", transport=transport)
        error_tracking.capture_beat_errors()
        handlers = [h for h in logging.getLogger("celery.beat").handlers
                    if isinstance(h, error_tracking._BeatErrorCapture)]
        report(handlers=len(handlers))
        """
    )
    assert out["handlers"] == 0


def _spawn(tmp_path: Path, service: str, *args: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        SENTRY_DSN=_GOOD_DSN,
        RAILWAY_GIT_COMMIT_SHA=_SHA,
        P003_SERVICE=service,
        P003_ENVELOPE_DIR=str(tmp_path),
    )
    env.pop("SENTRY_PRINT_ENVELOPES", None)
    # Output goes to a file, not a pipe: an unread pipe fills up and blocks the
    # worker mid-log, before it ever reaches the task.
    log = (tmp_path / f"{service}.log").open("w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "celery", "-A", "tests._p003_celery_app", *args],
        cwd=_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
    )


def _wait_for(tmp_path: Path, count: int, proc: subprocess.Popen, timeout: float = 90.0) -> list[Path]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        files = sorted(tmp_path.glob("*.envelope"))
        if len(files) >= count:
            time.sleep(1.0)  # let a late duplicate land, if there is one
            return sorted(tmp_path.glob("*.envelope"))
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    _stop(proc)
    output = "".join(p.read_text(encoding="utf-8", errors="replace") for p in tmp_path.glob("*.log"))
    pytest.fail(f"expected {count} envelope(s), got {len(list(tmp_path.glob('*.envelope')))}\n{output[-4000:]}")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@needs_redis
def test_real_worker_child_failures_are_captured_and_sanitised(tmp_path):
    from src.workers import app

    prefork = sys.platform != "win32"
    pool = ["-P", "prefork", "-c", "1", "--max-tasks-per-child", "1"] if prefork else ["-P", "solo"]
    proc = _spawn(tmp_path, "worker", "worker", *pool, "-Q", _QUEUE, "-n", "p003@%h", "--loglevel=INFO")
    try:
        for _ in range(2):
            app.send_task(_TASK, args=[_CANARY_SQL], kwargs={}, queue=_QUEUE, headers={"email": _CANARY_EMAIL})
        files = _wait_for(tmp_path, 2, proc)
    finally:
        _stop(proc)
    assert len(files) == 2  # once per failure, not once per layer
    for path in files:
        envelope = path.read_text(encoding="utf-8")
        _assert_clean(envelope)
        event = json.loads(envelope.splitlines()[-1])
        assert event["tags"] == {"service": "worker"}
        assert event["transaction"] == _TASK
        assert event["exception"]["values"][-1]["type"] == "ConnectionError"
        assert event["release"] == _SHA
    if prefork:
        pids = {int(p.name.split("-")[0]) for p in files}
        assert proc.pid not in pids  # captured in the pool children, not the parent
        assert len(pids) == 2  # and still captured after the child was recycled


@needs_redis
def test_real_beat_publish_failure_is_captured_as_beat(tmp_path):
    proc = _spawn(tmp_path, "beat", "beat", "-s", str(tmp_path / "schedule"), "--loglevel=INFO")
    try:
        files = _wait_for(tmp_path, 1, proc)
    finally:
        _stop(proc)
    event = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
    assert event["tags"] == {"service": "beat"}
    types = [v["type"] for v in event["exception"]["values"]]
    # beat re-raises the publish failure as SchedulingError, chained to its cause
    assert types[-1] == "SchedulingError" and "EncodeError" in types
    assert {v["value"] for v in event["exception"]["values"]} == {"[redacted]"}
