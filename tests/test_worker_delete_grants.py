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


# ── O-D: pending_skip_trace_rows ─────────────────────────────────────────────


def _quoted_tables(text_: str, name: str) -> set[str]:
    block = re.search(name + r"\s*=\s*\((.*?)\)", text_, re.DOTALL)
    assert block, f"{name} not found"
    return set(re.findall(r'"([a-z_]+)"', block.group(1)))


def test_pending_skip_trace_rows_delete_is_granted_in_both_files():
    """Pinned like delivered_records: without it the claim's lost-race withdrawal
    raises and rolls back the whole claim, live scrape enqueue included (O-D)."""
    for path in (_SQL, _PY):
        assert "pending_skip_trace_rows" in _delete_grants(path.read_text(encoding="utf-8")), (
            f"{path.name} does not GRANT DELETE ON pending_skip_trace_rows TO {_ROLE}"
        )


def test_every_system_delete_list_is_the_same_set():
    """EXACT equality, all four ways (Codex O-D AK4): the SQL block, the cutover's
    grant statements, the cutover's positive verify, and the operator's drift check.
    A subset test would let an extra grant in one file go unnoticed."""
    py_text = _PY.read_text(encoding="utf-8")
    ops = (_ROOT / "scripts" / "verify_worker_delete_grants.py").read_text(encoding="utf-8")
    sql_grants = _delete_grants(_SQL.read_text(encoding="utf-8"))
    assert sql_grants, "parsed no DELETE grants from provision_rls_roles.sql"
    assert sql_grants == _delete_grants(py_text)
    assert sql_grants == _quoted_tables(py_text, "_SYSTEM_DELETE_TABLES")
    assert sql_grants == _quoted_tables(ops, "REQUIRED_DELETE_TABLES")


def _pending_row_deletes() -> list[str]:
    """Every pending_skip_trace_rows DELETE in src/, as "path:function".

    An AST scan, not a grep (Codex O-D AK3): every string constant is read with
    Python's implicit concatenation already merged by the parser, normalized for
    case, whitespace and a `public.` prefix; docstrings and comments are never
    looked at; ORM `delete(PendingSkipTraceRow)` calls are caught too. SQL built at
    RUNTIME (f-strings with the table name interpolated, string joins) cannot be seen
    by any static scan; that is what review is for.
    """
    import ast

    found: list[str] = []
    for path in sorted((_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        scopes = [(tree, "<module>")] + [
            (n, n.name) for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        owner: dict[int, str] = {}
        for scope, name in scopes:  # innermost function wins (walked later)
            for node in ast.walk(scope):
                owner[id(node)] = name
        rel = path.relative_to(_ROOT).as_posix()
        for node in ast.walk(tree):
            hit = False
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                sql = re.sub(r"\s+", " ", node.value.lower()).replace("public.", "")
                hit = "delete from pending_skip_trace_rows" in sql
            elif isinstance(node, ast.Call):
                fn = node.func
                fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                hit = fname == "delete" and any(
                    isinstance(a, ast.Name) and a.id == "PendingSkipTraceRow" for a in node.args
                )
            if hit:
                found.append(f"{rel}:{owner.get(id(node), '<module>')}")
    return found


def test_the_claim_withdrawal_is_the_only_pending_row_delete():
    """The worker now holds DELETE on billing evidence (O-D). The one site it was
    granted for is the claim's lost-race withdrawal, by its own just-inserted ids. A
    new delete site must be reviewed, not ride in on this grant."""
    assert _pending_row_deletes() == ["src/workers/skip_trace_claim.py:claim_skip_trace_rows"]
