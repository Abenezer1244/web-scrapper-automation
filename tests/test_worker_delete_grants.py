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
# Read the grant sources as EXECUTED, not as text (Codex O-D review r1): SQL with its
# `--` comments removed, the Python lists by literal evaluation. A grant that survives
# only in a comment must not count.

import ast  # noqa: E402 - the O-D block's own helpers

_OPS = _ROOT / "scripts" / "verify_worker_delete_grants.py"


def _sql_statements(sql: str) -> list[str]:
    """`--` comments and psql meta-lines (`\\gset`, `\\echo`, ...) removed, split on `;`.

    A `DO $$ ... $$` body is split too, into fragments. That is deliberate and safe in
    both directions: a GRANT only counts as a complete statement (a fragment does not
    match, so a grant hidden in a DO block FAILS the pin), and a REVOKE is searched for
    INSIDE every fragment (so one hidden in a DO block is still seen)."""
    no_comments = re.sub(r"--[^\n]*", "", sql)
    no_meta = re.sub(r"(?m)^\s*\\.*$", "", no_comments)
    return [re.sub(r"\s+", " ", s).strip() for s in no_meta.split(";") if s.strip()]


def _py_value(path: Path, name: str):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path.name}")


def _executed(path: Path) -> list[str]:
    """The statements a grant source runs, in order."""
    if path.suffix == ".sql":
        return _sql_statements(path.read_text(encoding="utf-8"))
    return [re.sub(r"\s+", " ", s).strip() for s in _py_value(path, "_GRANTS")]


def _granted(stmts: list[str]) -> set[str]:
    out: set[str] = set()
    for s in stmts:
        m = re.fullmatch(r"GRANT DELETE ON (.+?) TO " + _ROLE, s, re.IGNORECASE)
        if m:
            out.update(t.strip() for t in m.group(1).split(","))
    return out


def test_pending_skip_trace_rows_delete_is_granted_in_both_files():
    """Pinned like delivered_records: without it the claim's lost-race withdrawal
    raises and rolls back the whole claim, live scrape enqueue included (O-D)."""
    for path in (_SQL, _PY):
        assert "pending_skip_trace_rows" in _granted(_executed(path)), (
            f"{path.name} does not GRANT DELETE ON pending_skip_trace_rows TO {_ROLE}"
        )


def test_every_system_delete_list_is_the_same_set():
    """EXACT equality, all four ways (Codex O-D AK4), on executed statements: the SQL
    block, the cutover's grants, its positive verify, and the operator's drift check."""
    sql_grants = _granted(_executed(_SQL))
    assert sql_grants, "parsed no DELETE grants from provision_rls_roles.sql"
    assert sql_grants == _granted(_executed(_PY))
    assert sql_grants == set(_py_value(_PY, "_SYSTEM_DELETE_TABLES"))
    assert sql_grants == set(_py_value(_OPS, "REQUIRED_DELETE_TABLES"))


def test_no_later_statement_revokes_any_system_delete_grant():
    """Ordered execution, for EVERY granted table in BOTH sources: a later REVOKE of
    DELETE (or ALL) on that table, or on ALL TABLES, from the system role would
    silently undo the grant."""
    for path in (_SQL, _PY):
        stmts = _executed(path)
        for i, s in enumerate(stmts):
            for table in _granted([s]):
                for later in stmts[i + 1:]:
                    # search, not fullmatch: a REVOKE inside a DO block's fragment
                    # ("DO $$ BEGIN REVOKE ...") must still be seen (Codex O-D r2).
                    m = re.search(r"REVOKE (.+?) ON (.+?) FROM " + _ROLE + r"\b", later,
                                  re.IGNORECASE)
                    if not m:
                        continue
                    privs, on = m.group(1).upper(), m.group(2)
                    hits_table = (table in [t.strip() for t in on.split(",")]
                                  or "ALL TABLES" in on.upper())
                    assert not (hits_table and ("DELETE" in privs or "ALL" in privs)), (
                        f"{path.name}: `{later}` would strip DELETE on {table}"
                    )


# The ONE pending-row DELETE the grant exists for, as a lexical scope path.
_APPROVED_DELETE = ("src/workers/skip_trace_claim.py", "claim_skip_trace_rows")
_MODEL = "PendingSkipTraceRow"


def _fold(node) -> str | None:
    """A string a static reader can know: a literal (implicit concatenation is merged
    by the parser), `+` of known strings, or an f-string's literal parts with each
    interpolation replaced by a placeholder."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "\x00" for v in node.values)
    return None


def _is_pending_delete_sql(s: str) -> bool:
    s = re.sub(r"\s+", " ", s.lower()).replace('"', "")
    s = re.sub(r"public\s*\.\s*", "", s)
    return re.search(r"delete from (only )?pending_skip_trace_rows\b", s) is not None


def _pending_row_deletes() -> list[tuple[str, str]]:
    """Every pending_skip_trace_rows DELETE in src/, as (path, scope path), one per
    statement line.

    An AST scan, not a grep (Codex O-D AK3 + review r1):
    - foldable strings, with docstrings never looked at;
    - ORM deletes: `delete(<model>)` / `<x>.delete(<model>)`, and
      `query(<model>)...delete()`, with the model's import aliases resolved;
    - each hit is attributed to its full LEXICAL scope (functions, classes and
      lambdas), so only the module-level claim function itself is approved, never a
      nested function, class or lambda that happens to share its name.

    Not visible to any static scan: SQL assembled at runtime (the table name
    interpolated, joined lists), and `session.delete(instance)` on a loaded row.
    That is what review is for.
    """
    found: set[tuple[str, str, int]] = set()
    for path in sorted((_ROOT / "src").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parent = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        docstrings = {
            id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        aliases = {_MODEL} | {
            a.asname for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
            for a in n.names if a.name == _MODEL and a.asname
        }

        def names_model(node, aliases=aliases) -> bool:
            return any((isinstance(x, ast.Name) and x.id in aliases)
                       or (isinstance(x, ast.Attribute) and x.attr == _MODEL)
                       for x in ast.walk(node))

        def scope(node, parent=parent) -> str:
            parts, cur = [], parent.get(id(node))
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    parts.append(cur.name)
                elif isinstance(cur, ast.Lambda):
                    parts.append("<lambda>")
                cur = parent.get(id(cur))
            return ".".join(reversed(parts)) or "<module>"

        for node in ast.walk(tree):
            hit = False
            if id(node) not in docstrings:
                folded = _fold(node)
                hit = folded is not None and _is_pending_delete_sql(folded)
            if not hit and isinstance(node, ast.Call):
                fn = node.func
                fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if fname == "delete":
                    receiver = fn.value if isinstance(fn, ast.Attribute) else None
                    hit = (any(names_model(a) for a in node.args)
                           or (receiver is not None and names_model(receiver)))
            if hit:
                found.add((rel, scope(node), node.lineno))
    return sorted((p, s) for p, s, _line in found)


def test_the_claim_withdrawal_is_the_only_pending_row_delete():
    """The worker now holds DELETE on billing evidence (O-D). The one site it was
    granted for is the claim's lost-race withdrawal, by its own just-inserted ids. A
    new delete site, including a second one inside the claim itself, must be
    reviewed, not ride in on this grant."""
    assert _pending_row_deletes() == [_APPROVED_DELETE]
