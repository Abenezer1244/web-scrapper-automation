"""scripts/backfill_assumed_mailing.py stamps each row with ITS OWN attempt count.

The apply loop once reused the planning loop's leftover `attempts` (the last row's), so
every row in a batch got one row's count: a row could give up after one failure or
retry for ever. Real database: main() runs for rule S (Snohomish: no county call) with
--apply, its session bound to one outer transaction that is rolled back. Synthetic
people only.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.session import sync_engine

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_assumed_mailing.py"
_spec = importlib.util.spec_from_file_location("_attempts_backfill_assumed_mailing", _SCRIPT)
bam = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bam)

_SITUS = "9 ATTEMPTS ST"


def _q(conn, sql: str, /, **params):
    return conn.execute(text(sql), params)


def test_each_row_keeps_its_own_attempt_count(monkeypatch, tmp_path):
    with sync_engine.connect() as conn:
        outer = conn.begin()
        try:
            owner, cfg, job = (str(uuid.uuid4()) for _ in range(3))
            _q(conn, "INSERT INTO users (id, email, password_hash, email_hmac) "
                     "VALUES (:i, :e, 'x', :h)", i=owner, e=f"{owner}@example.test",
               h=uuid.uuid4().hex)
            _q(conn, "INSERT INTO scraper_configs (id, user_id, name, county, state, "
                     "record_type) VALUES (:c, :u, 'Attempts', 'snohomish', 'WA', "
                     "'pre_foreclosure')", c=cfg, u=owner)
            _q(conn, "INSERT INTO jobs (id, user_id, scraper_config_id, status) "
                     "VALUES (:j, :u, :c, 'done')", j=job, u=owner, c=cfg)
            prior = {}
            for n in (0, 2):  # different prior counts, in candidate order
                rid = str(uuid.uuid4())
                _q(conn, "INSERT INTO results (id, job_id, user_id, party_name, parcel_id, "
                         "property_address, mailing_address, enrichment_data, dedup_hash) "
                         "VALUES (:i, :j, :u, 'DOE JANE', '00000000000000', :p, :m, "
                         "CAST(:ed AS json), :d)",
                   i=rid, j=job, u=owner, p=_SITUS, m=f"{_SITUS}, EVERETT WA",
                   ed=json.dumps({bam._ATTEMPTS_KEY: n}), d=uuid.uuid4().hex)
                prior[rid] = n

            @contextmanager
            def _session():
                s = Session(bind=conn, join_transaction_mode="create_savepoint")
                try:
                    yield s
                finally:
                    s.close()

            monkeypatch.setattr(bam, "system_sync_session", _session)
            monkeypatch.setattr(sys, "argv", [
                "backfill_assumed_mailing.py", "--apply", "--rules", "S",
                "--report", str(tmp_path / "report.jsonl")])
            assert bam.main() == 0

            stamped = dict(_q(conn, "SELECT id::text, (enrichment_data ->> :k)::int "
                                    "FROM results WHERE id = ANY(CAST(:ids AS uuid[]))",
                              k=bam._ATTEMPTS_KEY, ids=list(prior)).all())
            assert stamped == {rid: n + 1 for rid, n in prior.items()}
        finally:
            outer.rollback()
