"""scripts/release_stranded_dedup_claims.py never writes a privacy-withheld subject to its
backup (Phase 6 P7).

Real database, one session rolled back at the end; the script's own claim statement
(_Q_CLAIMS) runs against it. One job holds two claims; the first result behind one of
them is withheld. Every claim is still selected (the release and --expect cover all of
them, and the backup must stay restorable), but the withheld one carries no parcel or
address. Synthetic people only.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from sqlalchemy import text

from tests.test_privacy_erase import _owner, _result
from tests.test_privacy_script_verify_tracerfy_provenance import _withhold, s  # noqa: F401


def _script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "release_stranded_dedup_claims.py"
    spec = importlib.util.spec_from_file_location("_release_stranded", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_withheld_claim_is_released_without_its_subject(s):  # noqa: F811
    uid, job = _owner(s)
    kept = _result(s, uid, job, "DOE JANE", "1111111111")
    withheld = _result(s, uid, job, "SMITH JOHN", "2222222222")
    _withhold(s, withheld)
    for rid, parcel in ((kept, "1111111111"), (withheld, "2222222222")):
        s.execute(text(
            "INSERT INTO delivered_records (id, user_id, dedup_hash, first_result_id, "
            "first_job_id, parcel_id, property_address) "
            "VALUES (gen_random_uuid(), :u, :h, :r, :j, :p, '1 TEST ST')"),
            {"u": uid, "h": uuid.uuid4().hex, "r": rid, "j": job, "p": parcel})

    rows = {r.rid: r for r in s.execute(text(_script()._Q_CLAIMS), {"j": job, "u": uid})}

    assert set(rows) == {kept, withheld}     # every claim is still released and counted
    assert (rows[kept].parcel_id, rows[kept].property_address, rows[kept].withheld) == (
        "1111111111", "1 TEST ST", False)
    assert (rows[withheld].parcel_id, rows[withheld].property_address,
            rows[withheld].withheld) == (None, None, True)
