"""scripts/purge_test_batch_configs.py never writes a privacy-withheld lead to its backup
(Phase 6 P7).

Real database, one session rolled back at the end; the script's own dump (_dump) runs
against it into a temp file. One test job with two leads, each with a queued skip-trace
row and a dedup claim: one lead is withheld. Its rows are counted, never written. The
purge itself (not exercised here) still deletes every target row. Synthetic people only.
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

from sqlalchemy import text

from tests.test_privacy_erase import _owner, _result
from tests.test_privacy_script_verify_tracerfy_provenance import _withhold, s  # noqa: F401


def _script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "purge_test_batch_configs.py"
    spec = importlib.util.spec_from_file_location("_purge_test_batch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_backup_leaves_out_a_withheld_lead(s, tmp_path):  # noqa: F811
    uid, job = _owner(s)
    cfg = s.execute(text("SELECT scraper_config_id::text FROM jobs WHERE id = :j"),
                    {"j": job}).scalar()
    kept = _result(s, uid, job, "DOE JANE", "1111111111")
    withheld = _result(s, uid, job, "SMITH JOHN", "2222222222")
    for rid, first, last in ((kept, "JANE", "DOE"), (withheld, "JOHN", "SMITH")):
        s.execute(text(
            "INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id, "
            "property_address, city, state, first_name, last_name, trace_type, status) "
            "VALUES (gen_random_uuid(), :j, :r, :u, '1 TEST ST', 'SEATTLE', 'WA', :f, :l, "
            "'normal', 'queued')"), {"j": job, "r": rid, "u": uid, "f": first, "l": last})
        s.execute(text(
            "INSERT INTO delivered_records (id, user_id, dedup_hash, first_result_id, "
            "first_job_id, parcel_id, property_address) "
            "VALUES (gen_random_uuid(), :u, :h, :r, :j, '1111111111', '1 TEST ST')"),
            {"u": uid, "h": uuid.uuid4().hex, "r": rid, "j": job})
    _withhold(s, withheld)
    backup = tmp_path / "backup.json"

    counts, held_back = _script()._dump(s, [cfg], [job], str(backup))

    raw = backup.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert [str(r["id"]) for r in payload["results"]] == [kept]
    assert [str(r["result_id"]) for r in payload["pending_skip_trace_rows"]] == [kept]
    assert [str(r["first_result_id"])
            for r in payload["delivered_records_affected"]] == [kept]
    assert withheld not in raw and "SMITH" not in raw
    # Accounted for by count, so the pre-delete verification still balances.
    assert held_back == {"results": 1, "pending_skip_trace_rows": 1}
    assert payload["_meta"]["privacy_withheld"] == held_back
    assert counts["results"] == 1
