"""Migration 102: a spent row's credit weight is a database fact.

The daily credit cap (1b-1b-ii) charges each claimed row by its trace_type
(normal = 1 Tracerfy credit, advanced = 2) over a rolling 24h window of
`submitted_at`. These tests try to do the two things that would let an account
spend credits the cap never saw, and require the DATABASE to refuse:

  * give a row a type the cap cannot weigh (CHECK);
  * re-type a row that has already been spent, which would rewrite what the cap
    charged it (trigger).

Schema comes from `alembic upgrade head` (not create_all). Each forbidden
statement gets its own test and the session is rolled back straight after the
failure, never reused (the 78-minute lesson in the 1b-1a handoff).
"""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session


def _pending(user_id: str, **row) -> str:
    """user -> job -> result -> one pending row, committed; returns the pending id."""
    sc_id, job_id, rid, pid = (str(uuid.uuid4()) for _ in range(4))
    cols = {"trace_type": "normal", "status": "queued", "submitted_at": None,
            "tracerfy_queue_id": None, **row}
    with system_sync_session() as db:
        db.execute(text("""
            INSERT INTO scraper_configs (id, user_id, name, county, state, record_type,
                fields, enrichment, schedule, deliver, skip_trace_enabled, active)
            VALUES (:sc, :u, 'weight test', 'pierce', 'WA', 'probate', '[]'::json,
                    '[]'::json, '{"frequency":"manual"}'::json,
                    '{"format":"csv","emails":[]}'::json, true, true)
        """), {"sc": sc_id, "u": user_id})
        db.execute(text("""
            INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger, page_current,
                              page_total, record_count, retry_count)
            VALUES (:j, :u, :sc, 'done', 'manual', 0, 0, 0, 0)
        """), {"j": job_id, "u": user_id, "sc": sc_id})
        db.execute(text("""
            INSERT INTO results (id, job_id, user_id, is_duplicate, skip_trace_status,
                                 property_address, enrichment_data, created_at)
            VALUES (:r, :j, :u, false, 'queued', '1 WEIGHT ST', '{}'::json, now())
        """), {"r": rid, "j": job_id, "u": user_id})
        db.execute(text("""
            INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id,
                property_address, city, state, trace_type, status, enqueued_at,
                submitted_at, tracerfy_queue_id)
            VALUES (:p, :j, :r, :u, '1 WEIGHT ST', 'TACOMA', 'WA', :tt, :st, now(), :sub, :q)
        """), {"p": pid, "j": job_id, "r": rid, "u": user_id, "tt": cols["trace_type"],
               "st": cols["status"], "sub": cols["submitted_at"], "q": cols["tracerfy_queue_id"]})
        db.commit()
    return pid


def _retype(pid: str, new_type: str, extra_set: str = "") -> None:
    with system_sync_session() as db:
        try:
            db.execute(text(
                f"UPDATE pending_skip_trace_rows SET trace_type = :t{extra_set} WHERE id = :p"
            ), {"t": new_type, "p": pid})
            db.commit()
        except Exception:
            db.rollback()
            raise


def _type_of(pid: str) -> str:
    with system_sync_session() as db:
        return db.execute(text("SELECT trace_type FROM pending_skip_trace_rows WHERE id = :p"),
                          {"p": pid}).scalar_one()


# ── Only weighable types exist ────────────────────────────────────────────────


async def test_a_row_the_cap_cannot_weigh_is_refused(starter_user):
    with pytest.raises(Exception) as exc:
        _pending(starter_user.id, trace_type="premium")
    assert "ck_pending_skip_trace_rows_trace_type" in str(exc.value)


# ── A spent row keeps its weight ─────────────────────────────────────────────


@pytest.mark.parametrize(("evidence", "why"), [
    ({"status": "submitting", "submitted_at": datetime.now(UTC)}, "claimed, outcome unknown"),
    ({"status": "submitted", "submitted_at": datetime.now(UTC), "tracerfy_queue_id": 424242},
     "accepted by Tracerfy"),
    ({"status": "errored", "tracerfy_queue_id": 434343},
     "legacy charged-but-unmatched: a queue id and no submit time"),
])
async def test_a_spent_row_cannot_be_retyped(starter_user, evidence, why):
    pid = _pending(starter_user.id, trace_type="advanced", **evidence)
    with pytest.raises(Exception) as exc:
        _retype(pid, "normal")
    assert "trace_type of a spent row cannot change" in str(exc.value), why
    assert _type_of(pid) == "advanced"


async def test_retyping_while_stamping_the_spend_in_one_update_is_refused(starter_user):
    # The NEW row carries the evidence even though the OLD one did not.
    pid = _pending(starter_user.id, trace_type="advanced")
    with pytest.raises(Exception) as exc:
        _retype(pid, "normal", ", status = 'submitting', submitted_at = now()")
    assert "trace_type of a spent row cannot change" in str(exc.value)
    assert _type_of(pid) == "advanced"


async def test_an_unsent_row_may_still_change_type(starter_user):
    # The probate repair's name refresh does exactly this, on queued rows with no
    # submission evidence; it must keep working.
    pid = _pending(starter_user.id, trace_type="advanced")
    _retype(pid, "normal")
    assert _type_of(pid) == "normal"


async def test_the_spend_itself_is_not_blocked(starter_user):
    # The dispatcher's claim and bookkeeping never touch trace_type, so the
    # trigger (BEFORE UPDATE OF trace_type) must not fire for them.
    pid = _pending(starter_user.id, trace_type="advanced")
    with system_sync_session() as db:
        db.execute(text("UPDATE pending_skip_trace_rows SET status = 'submitting', "
                        "submitted_at = now() WHERE id = :p"), {"p": pid})
        db.execute(text("UPDATE pending_skip_trace_rows SET status = 'submitted', "
                        "tracerfy_queue_id = 454545 WHERE id = :p"), {"p": pid})
        db.commit()
    assert _type_of(pid) == "advanced"


# ── The cap's spent query has its index ──────────────────────────────────────


async def test_the_spent_index_has_the_shape_the_cap_reads(starter_user):
    with system_sync_session() as db:
        row = db.execute(text(
            "SELECT i.indisvalid, pg_get_expr(i.indpred, i.indrelid) AS predicate, "
            "       ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(n, o) "
            "             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n "
            "             ORDER BY k.o) AS cols, i.indnkeyatts "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = 'ix_pending_skip_trace_spent'"
        )).one()
    assert row.indisvalid
    assert row.predicate == "(submitted_at IS NOT NULL)"
    assert list(row.cols) == ["submitted_at", "user_id", "trace_type"]
    assert row.indnkeyatts == 1  # user_id and trace_type are INCLUDE payload, not keys
