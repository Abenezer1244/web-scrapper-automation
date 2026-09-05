"""The grant-drift guard.

scripts/provision_rls_roles.sql and scripts/_cutover_step2_grants_policies.py are
supposed to be the same grant block. They drifted by ONE line — the cutover
script (which actually provisioned prod) ran `REVOKE ALL ON delivered_records`
and never re-granted DELETE. The worker's five dedup-claim-release paths then
failed with InsufficientPrivilege, were swallowed as caught exceptions, and
16,761 delivered_records claims were stranded — permanently suppressing those
leads as duplicates for that user. An over-quota run also failed outright,
because that release runs inside the plan-cap transaction.

Nothing compared the two files, so the drift was invisible. These tests compare
them, and pin the one grant whose absence caused the incident.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SQL = _ROOT / "scripts" / "provision_rls_roles.sql"
_PY = _ROOT / "scripts" / "_cutover_step2_grants_policies.py"
_ROLE = "bridgeleads_system"


def _delete_grants(text: str) -> set[str]:
    """Tables granted DELETE to the system role, from either file's statements."""
    out: set[str] = set()
    for m in re.finditer(
        r"GRANT\s+DELETE\s+ON\s+([A-Za-z0-9_,\s]+?)\s+TO\s+" + _ROLE, text, re.IGNORECASE
    ):
        out.update(t.strip() for t in m.group(1).split(",") if t.strip())
    return out


def test_cutover_script_mirrors_the_sql_grant_block():
    # The cutover script's docstring claims it "mirrors provision_rls_roles.sql".
    # Assert it, don't trust it.
    sql_grants = _delete_grants(_SQL.read_text(encoding="utf-8"))
    py_grants = _delete_grants(_PY.read_text(encoding="utf-8"))
    assert sql_grants, "parsed no DELETE grants from provision_rls_roles.sql"
    missing = sql_grants - py_grants
    assert not missing, (
        f"_cutover_step2_grants_policies.py is missing DELETE grants present in "
        f"provision_rls_roles.sql: {sorted(missing)} — this is the exact drift that "
        f"cost production 16,761 stranded dedup claims"
    )


def test_delivered_records_delete_is_granted_in_both_files():
    # Pinned explicitly: this is the grant whose absence caused the incident, and
    # a generic set-comparison would still pass if BOTH files lost it.
    for path in (_SQL, _PY):
        assert "delivered_records" in _delete_grants(path.read_text(encoding="utf-8")), (
            f"{path.name} does not GRANT DELETE ON delivered_records TO {_ROLE}; "
            "the worker's dedup-claim release paths cannot run without it"
        )


def test_verify_list_covers_every_granted_delete_table():
    # The positive verify added to the cutover script must actually cover the
    # tables being granted, or it verifies nothing.
    py_text = _PY.read_text(encoding="utf-8")
    block = re.search(r"_SYSTEM_DELETE_TABLES\s*=\s*\((.*?)\)", py_text, re.DOTALL)
    assert block, "_SYSTEM_DELETE_TABLES not found in the cutover script"
    verified = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    granted = _delete_grants(py_text)
    assert granted <= verified, (
        f"granted but NOT verified: {sorted(granted - verified)} — a future drift "
        "on these would again go undetected"
    )


def test_ops_script_required_tables_match_the_cutover_verify():
    # scripts/verify_worker_delete_grants.py is the operator's drift check; if its
    # list falls behind, an operator gets a clean bill of health on a broken role.
    ops = (_ROOT / "scripts" / "verify_worker_delete_grants.py").read_text(encoding="utf-8")
    block = re.search(r"REQUIRED_DELETE_TABLES\s*=\s*\((.*?)\)", ops, re.DOTALL)
    assert block, "REQUIRED_DELETE_TABLES not found"
    ops_tables = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    py_block = re.search(
        r"_SYSTEM_DELETE_TABLES\s*=\s*\((.*?)\)",
        _PY.read_text(encoding="utf-8"), re.DOTALL,
    )
    assert ops_tables == set(re.findall(r'"([a-z_]+)"', py_block.group(1)))


def test_no_later_statement_revokes_the_system_delete_grant():
    """Order matters: _GRANTS is executed top-to-bottom, so a REVOKE placed after
    the GRANT would undo it and every text-level "is the grant present?" check
    would still pass. This is the ordered-execution invariant (Codex)."""
    py_text = _PY.read_text(encoding="utf-8")
    grant_at = py_text.index("GRANT DELETE ON delivered_records TO bridgeleads_system")
    after = py_text[grant_at:]
    offenders = [
        m.group(0)
        for m in re.finditer(r"REVOKE[^\"']*?FROM\s+" + _ROLE, after, re.IGNORECASE | re.DOTALL)
        if "delivered_records" in m.group(0) or "ALL TABLES" in m.group(0).upper()
    ]
    assert not offenders, (
        f"a REVOKE after the GRANT would strip it again: {offenders}"
    )


def test_alert_helper_is_not_passed_an_orm_attribute():
    """Every _alert_dedup_release_failed call runs after db.rollback(), which
    expires ORM instances — reading job.user_id there would emit a refresh SELECT
    on the session that just failed, and if it raised it would skip the job's real
    failure handling. The cached plain value must be used instead."""
    tasks = (_ROOT / "src" / "workers" / "tasks.py").read_text(encoding="utf-8")
    calls = re.findall(r"_alert_dedup_release_failed\(([^)]*)\)", tasks)
    assert calls, "no call sites found — did the helper get renamed?"
    for call in calls:
        assert "job.user_id" not in call, (
            "pass the cached _boot_user_id, not the ORM attribute job.user_id: " + call
        )
