"""timed_step: the worker-log line that splits a scrape job's time per step.

Runs real statements through the real sync session, so the counts are what the
event listener actually saw, not what the test assumes.
"""

import logging

from sqlalchemy import text
import pytest

from src.db.session import SyncSessionLocal
from src.workers.tasks_helpers.step_timing import timed_step


def _step_lines(caplog, step: str) -> list[str]:
    return [rec.getMessage() for rec in caplog.records if f"step {step} took" in rec.getMessage()]


def test_logs_elapsed_statements_and_parameter_sets(caplog):
    with SyncSessionLocal() as sdb, caplog.at_level(logging.INFO, logger="worker.task"):
        sdb.execute(text("SELECT 1"))  # outside the step: must not be counted
        with timed_step(sdb, "job-1", "probe"):
            sdb.execute(text("SELECT 1"))
            sdb.execute(text("SELECT 2"))
            # One executemany with three parameter sets: under psycopg2 that is three
            # round trips, which is the number a write-bound step is judged by.
            sdb.execute(text("SELECT CAST(:v AS int)"), [{"v": 1}, {"v": 2}, {"v": 3}])
    lines = _step_lines(caplog, "probe")
    assert len(lines) == 1
    assert lines[0].startswith("Job job-1: step probe took ")
    assert "(3 statements, 5 parameter sets)" in lines[0]


def test_counts_stop_when_the_step_ends(caplog):
    with SyncSessionLocal() as sdb, caplog.at_level(logging.INFO, logger="worker.task"):
        with timed_step(sdb, "job-1", "first"):
            sdb.execute(text("SELECT 1"))
        # The listener is removed on exit, so a later step starts from zero and the
        # first step's line is not rewritten by later statements.
        with timed_step(sdb, "job-1", "second"):
            pass
        sdb.execute(text("SELECT 1"))
    assert "(1 statements, 1 parameter sets)" in _step_lines(caplog, "first")[0]
    assert "(0 statements, 0 parameter sets)" in _step_lines(caplog, "second")[0]


def test_a_failing_step_is_still_timed_and_its_error_propagates(caplog):
    with SyncSessionLocal() as sdb, caplog.at_level(logging.INFO, logger="worker.task"):
        with pytest.raises(ValueError, match="boom"):
            with timed_step(sdb, "job-1", "failing"):
                sdb.execute(text("SELECT 1"))
                raise ValueError("boom")
    assert "(1 statements, 1 parameter sets)" in _step_lines(caplog, "failing")[0]
