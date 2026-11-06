"""Migration 126: the results privacy flag and the jobs breakdown bucket for it.

Real database (schema from `alembic upgrade head`). Every test runs in one transaction
that is rolled back, so nothing persists; expected failures run in a SAVEPOINT.
The fixture rows are synthetic and never leave the transaction.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.api.auth import hash_password
from src.db.session import sync_engine
from src.utils.crypto import blind_index, encrypt_field

_RESULT_COLUMNS = {
    "privacy_suppressed_at": ("timestamp with time zone", "YES"),
    "privacy_suppression_id": ("uuid", "YES"),
}


def _mig126():
    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "126_results_privacy_suppression_flag.py")
    spec = importlib.util.spec_from_file_location("_mig126", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


@pytest.fixture
def conn():
    with sync_engine.connect() as c:
        trans = c.begin()
        try:
            yield c
        finally:
            trans.rollback()


def _sqlstate(conn, sql: str, params: dict) -> str | None:
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql), params)
    except DBAPIError as exc:
        sp.rollback()
        return getattr(exc.orig, "pgcode", None)
    sp.rollback()
    return None


def _columns(conn, table: str, names) -> dict[str, tuple[str, str]]:
    rows = conn.execute(text(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t AND column_name = ANY(:n)"),
        {"t": table, "n": list(names)}).all()
    return {r.column_name: (r.data_type, r.is_nullable) for r in rows}


def _result_row(conn) -> str:
    ids = {k: str(uuid.uuid4()) for k in ("user", "config", "job", "result")}
    email = f"p126_{ids['user'][:8]}@bl.test"
    conn.execute(text("INSERT INTO users (id, email, email_hmac, password_hash) "
                      "VALUES (:user, :e, :h, :pw)"),
                 ids | {"e": encrypt_field(email), "h": blind_index(email),
                        "pw": hash_password("Pw-123456789")})
    conn.execute(text(
        "INSERT INTO scraper_configs (id, user_id, name, county, state, record_type, "
        "fields, enrichment, schedule, deliver) VALUES (:config, :user, 'Cfg', 'pierce', "
        "'WA', 'probate', '{}', '{}', '{}', '{}')"), ids)
    conn.execute(text("INSERT INTO jobs (id, user_id, scraper_config_id, status) "
                      "VALUES (:job, :user, :config, 'done')"), ids)
    conn.execute(text("INSERT INTO results (id, job_id, user_id, party_name, property_address) "
                      "VALUES (:result, :job, :user, 'Synthetic Person', '1 Test St')"), ids)
    return ids["result"]


def _rule_row(conn) -> str:
    rid = conn.execute(text(
        "INSERT INTO privacy_requests (kind, channel, deadline_at) "
        "VALUES ('opt_out', 'web', now() + interval '45 days') RETURNING id")).scalar()
    return conn.execute(text(
        "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, anchor_hmac, "
        "subject_kind, subject_hmac, scope, key_version, normalization_version, state) "
        "VALUES (gen_random_uuid(), :r, 'property', :h, 'person', :h, 'subject', 'v1', "
        "'n3', 'pending') RETURNING id"), {"r": rid, "h": "a" * 64}).scalar()


def test_126_chains_on_125() -> None:
    mig = _mig126()
    assert (mig.revision, mig.down_revision) == ("126", "125")


def test_head_has_the_nullable_flag_and_the_bucket(conn) -> None:
    assert _columns(conn, "results", _RESULT_COLUMNS) == _RESULT_COLUMNS
    assert _columns(conn, "jobs", ["breakdown_privacy_withheld"]) == {
        "breakdown_privacy_withheld": ("integer", "YES")}


def test_existing_and_new_rows_are_unflagged(conn) -> None:
    res = _result_row(conn)
    row = conn.execute(text("SELECT privacy_suppressed_at, privacy_suppression_id "
                            "FROM results WHERE id = :i"), {"i": res}).one()
    assert tuple(row) == (None, None)


_FLAG = ("UPDATE results SET privacy_suppressed_at = :at, privacy_suppression_id = :sid "
         "WHERE id = :i")


def test_the_flag_names_a_real_rule_row(conn) -> None:
    res = _result_row(conn)
    sid = _rule_row(conn)
    assert _sqlstate(conn, _FLAG, {"at": "2026-11-06T00:00:00Z", "sid": sid, "i": res}) is None
    # A rule row id that does not exist is refused (foreign key violation).
    assert _sqlstate(conn, _FLAG, {"at": "2026-11-06T00:00:00Z",
                                   "sid": str(uuid.uuid4()), "i": res}) == "23503"


@pytest.mark.parametrize("half", ["time_only", "rule_only"])
def test_the_two_columns_are_set_together_or_not_at_all(conn, half) -> None:
    res = _result_row(conn)
    sid = _rule_row(conn)
    params = ({"at": "2026-11-06T00:00:00Z", "sid": None, "i": res} if half == "time_only"
              else {"at": None, "sid": sid, "i": res})
    assert _sqlstate(conn, _FLAG, params) == "23514"  # check_violation


def test_a_rule_row_a_result_points_at_cannot_be_deleted(conn) -> None:
    res = _result_row(conn)
    sid = _rule_row(conn)
    conn.execute(text(_FLAG), {"at": "2026-11-06T00:00:00Z", "sid": sid, "i": res})
    assert _sqlstate(conn, "DELETE FROM privacy_suppressions WHERE id = :s",
                     {"s": sid}) == "23503"


def test_the_constraints_are_not_valid_by_design(conn) -> None:
    """NOT VALID skips only the scan of existing rows (all NULL); new writes are checked
    (proven above). A later VALIDATE is optional, never required for correctness."""
    rows = dict(conn.execute(text(
        "SELECT conname, convalidated FROM pg_constraint WHERE conrelid = 'results'::regclass "
        "AND conname IN ('fk_results_privacy_suppression', 'ck_results_privacy_flag_pair')"
    )).all())
    assert rows == {"fk_results_privacy_suppression": False, "ck_results_privacy_flag_pair": False}


def test_a_flagged_result_still_goes_with_its_job(conn) -> None:
    """The account purge and job deletion cascade into results; the flag must not block
    them (the FK restricts deleting the RULE row, never the result)."""
    res = _result_row(conn)
    sid = _rule_row(conn)
    conn.execute(text(_FLAG), {"at": "2026-11-06T00:00:00Z", "sid": sid, "i": res})
    job = conn.execute(text("SELECT job_id FROM results WHERE id = :i"), {"i": res}).scalar()
    assert _sqlstate(conn, "DELETE FROM jobs WHERE id = :j", {"j": job}) is None


def test_126_downgrade_drops_and_upgrade_restores_them(conn) -> None:
    mig = _mig126()
    with Operations.context(MigrationContext.configure(conn)):
        mig.downgrade()
        assert _columns(conn, "results", _RESULT_COLUMNS) == {}
        assert _columns(conn, "jobs", ["breakdown_privacy_withheld"]) == {}
        mig.upgrade()
    assert _columns(conn, "results", _RESULT_COLUMNS) == _RESULT_COLUMNS
    assert _columns(conn, "jobs", ["breakdown_privacy_withheld"]) == {
        "breakdown_privacy_withheld": ("integer", "YES")}
