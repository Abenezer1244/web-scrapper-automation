"""A prefork child never reuses its parent's pooled DB connections.

Two processes on one inherited TLS connection corrupt it ("SSL error: wrong version
number" in dispatch_scheduled_jobs, 2026-10-31). Real engine, real Celery signal.
"""
import os
import select
import signal
import time

import pytest
from sqlalchemy import text


def test_a_new_worker_child_starts_with_none_of_the_parents_connections():
    from celery.signals import worker_process_init

    import src.workers  # noqa: F401 -- connects the receiver
    from src.db.session import sync_engine

    with sync_engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        inherited = conn.connection.dbapi_connection
    assert sync_engine.pool.checkedin() >= 1

    worker_process_init.send(sender=None)

    try:
        assert sync_engine.pool.checkedin() == 0
        with sync_engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
            assert conn.connection.dbapi_connection is not inherited
        assert not inherited.closed        # close=False: the parent's socket is left alone
    finally:
        inherited.close()


def _read_child_report(read_fd, pid, deadline_s=30.0):
    """The child's report and exit status, or the child is killed at the deadline."""
    deadline = time.monotonic() + deadline_s
    chunks, status = [], None
    try:
        while True:                        # until EOF: the child's write end closes at exit
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"forked child did not finish within {deadline_s}s")
            ready, _, _ = select.select([read_fd], [], [], min(remaining, 0.5))
            if ready:
                chunk = os.read(read_fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
        while status is None:
            done, status_raw = os.waitpid(pid, os.WNOHANG)
            if done:
                status = status_raw
            elif time.monotonic() >= deadline:
                raise AssertionError(f"forked child did not exit within {deadline_s}s")
            else:
                time.sleep(0.05)
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(read_fd)
    return b"".join(chunks).decode(errors="replace"), os.waitstatus_to_exitcode(status)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork (POSIX); prefork children fork")
def test_a_really_forked_child_gets_its_own_postgres_session_and_leaves_the_parents_alone():
    """The same thing across a real fork(), the way a recycled prefork child is born.

    Without the receiver the child reuses the inherited pooled connection (same backend
    pid); with dispose(close=True) it would terminate the parent's session. Either fails.
    """
    from celery.signals import worker_process_init

    import src.workers  # noqa: F401 -- connects the receiver
    from src.db.session import sync_engine

    sync_engine.dispose()                  # exactly one pooled connection: the inherited one
    try:
        with sync_engine.connect() as conn:
            parent_pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
            inherited = conn.connection.dbapi_connection
        assert sync_engine.pool.checkedin() == 1

        read_fd, write_fd = os.pipe()
        try:
            pid = os.fork()
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        if pid == 0:                       # child: no pytest, no teardown, always _exit
            code = 1
            try:
                try:
                    os.close(read_fd)
                    for _receiver, response in worker_process_init.send(sender=None):
                        if isinstance(response, BaseException):
                            raise response
                    with sync_engine.connect() as conn:
                        child_pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
                    report, code = f"pid={child_pid}", 0
                except BaseException as exc:  # noqa: BLE001 -- reported to the parent
                    report = f"error={exc!r}"
                os.write(write_fd, report[:2000].encode())
            finally:
                os._exit(code)             # nothing below this line ever runs in the child

        os.close(write_fd)
        report, exit_code = _read_child_report(read_fd, pid)
        assert exit_code == 0, report
        assert report.startswith("pid="), report
        assert int(report[len("pid="):]) != parent_pid    # the child opened its own session

        # The parent's own socket, checked directly (pool_pre_ping would hide a dead one).
        cur = inherited.cursor()
        cur.execute("SELECT pg_backend_pid()")
        assert cur.fetchone()[0] == parent_pid
        cur.close()
        with sync_engine.connect() as conn:
            assert conn.execute(text("SELECT pg_backend_pid()")).scalar() == parent_pid
    finally:
        sync_engine.dispose()
