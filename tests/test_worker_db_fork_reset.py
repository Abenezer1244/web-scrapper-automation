"""A prefork child never reuses its parent's pooled DB connections.

Two processes on one inherited TLS connection corrupt it ("SSL error: wrong version
number" in dispatch_scheduled_jobs, 2026-10-31). Real engine, real Celery signal.
"""
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
