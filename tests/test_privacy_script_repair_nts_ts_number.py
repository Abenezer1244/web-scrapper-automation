"""scripts/repair_nts_ts_number.py never prints or rewrites a privacy-withheld lead or
notice (Phase 6 P7).

Real database, one session rolled back at the end; the script's own repair functions
run against it under a source name no crawler writes. Synthetic people only.
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

from sqlalchemy import text

from tests.test_privacy_erase import _owner, _result
from tests.test_privacy_script_verify_tracerfy_provenance import (  # noqa: F401
    _rule,
    _withhold,
    s,
)

_SOURCE = "privacy_test"
_PARCEL = "1234567890"


def _script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "repair_nts_ts_number.py"
    spec = importlib.util.spec_from_file_location("_repair_nts_ts", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SOURCE = _SOURCE
    return mod


def _notice(session, parcel: str, ts: str, *, rule: str | None = None) -> str:
    nid = str(uuid.uuid4())
    # A withheld notice carries no grantor and is inactive (132's CHECK).
    session.execute(text(
        "INSERT INTO nts_notices (id, source, ts_number, grantor, parcel, county, state, "
        "is_active, privacy_suppression_id) "
        "VALUES (:i, :src, :t, :g, :p, 'snohomish', 'WA', :a, :r)"),
        {"i": nid, "src": _SOURCE, "t": ts, "g": None if rule else "DOE JANE", "p": parcel,
         "a": rule is None, "r": rule})
    return nid


def _ts(session, rid: str) -> str:
    enr = session.execute(text("SELECT enrichment_data FROM results WHERE id = :i"),
                          {"i": rid}).scalar()
    return (enr if isinstance(enr, dict) else json.loads(enr))["nts"]["ts_number"]


def test_a_withheld_lead_is_never_printed_or_rewritten(s, capsys):  # noqa: F811
    uid, job = _owner(s)
    notice = _notice(s, _PARCEL, "TS-RIGHT")
    kept = _result(s, uid, job, "DOE JANE", _PARCEL)
    withheld = _result(s, uid, job, "SMITH JOHN", _PARCEL)
    s.execute(text(
        "UPDATE results SET enrichment_data = CAST(:e AS json), nts_notice_id = :n "
        "WHERE id = ANY(CAST(:ids AS uuid[]))"),
        {"e": json.dumps({"nts": {"ts_number": "TS-WRONG"}}), "n": notice,
         "ids": [kept, withheld]})
    _withhold(s, withheld)

    changed = _script().repair_results(s, {_PARCEL: "TS-RIGHT"}, apply=True)

    out = capsys.readouterr().out
    assert changed == 1
    assert "DOE JANE" in out and "SMITH JOHN" not in out
    assert _ts(s, kept) == "TS-RIGHT"          # control: the unwithheld lead is repaired
    assert _ts(s, withheld) == "TS-WRONG"


def test_a_withheld_notice_is_never_listed_or_renamed(s, capsys):  # noqa: F811
    _notice(s, "1111111111", "A-WRONG")
    _notice(s, "2222222222", "B-WRONG", rule=_rule(s))

    changed = _script().repair_notices(
        s, {"1111111111": "A-RIGHT", "2222222222": "B-RIGHT"}, apply=False)

    out = capsys.readouterr().out
    assert changed == 1
    assert "1111111111" in out and "2222222222" not in out
