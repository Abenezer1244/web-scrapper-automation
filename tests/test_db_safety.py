"""The test-database guard and Alembic's refusal to reach anything else.

A pytest teardown has wiped PRODUCTION twice. `tests/_db_safety.py` pins the suite
to a validated test database; `alembic/env.py` must never find production either:
not through a `.env` file (it used to call `load_dotenv()`, which searches upward
from alembic/ and so found a checkout's production `.env`), not through
`DATABASE_URL_MIGRATE` (which the guard did not pin), not through a libpq variable
that reroutes an explicit DSN. What counts as a test database: `src/db_safety.py`.

The Alembic cases run in a SUBPROCESS on a copy of the real `alembic/` directory,
with a trap `.env` inside it naming an unreachable "production" host, so nothing
Alembic loads stays in this process and nothing can reach a real database but the
suite's own test database (Codex safety-PR consult, round 1).
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from src.db_safety import AMBIENT_REDIRECT_VARS
from tests._db_safety import assert_engine_is_test, enforce_test_database

REPO = Path(__file__).resolve().parents[1]
TEST_SYNC = os.environ["TEST_DATABASE_URL_SYNC"]
TRAP_URL = "postgresql+psycopg2://owner:secret@dotenv-prod.invalid:5432/postgres"
PROD_LIKE = "postgresql+psycopg2://owner:secret@prod.invalid:5432/postgres"

_CHILD = """\
import os
import sys

from alembic import command
from alembic.config import Config

cfg = Config()  # no ini file: fileConfig never runs
cfg.set_main_option("script_location", sys.argv[1])
handed = os.environ.get("HANDED_URL")
if handed:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    engine = create_engine(handed, poolclass=NullPool)
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn  # as scripts/migrate.py does
        command.current(cfg)
else:
    command.current(cfg)
print("CURRENT_OK MIGRATE_IN_ENV=" + str("DATABASE_URL_MIGRATE" in os.environ))
"""


# ── The guard ─────────────────────────────────────────────────────────────────

_GUARDED = ("DATABASE_URL", "DATABASE_URL_SYNC", "DATABASE_URL_MIGRATE", "ENVIRONMENT",
            "TEST_DATABASE_URL", "TEST_DATABASE_URL_SYNC", *AMBIENT_REDIRECT_VARS)


@pytest.fixture
def guard_env(monkeypatch):
    """Every variable the guard reads or writes, registered so the suite's own
    pinned environment is restored afterwards (an absent one is set then deleted,
    so monkeypatch removes it again if the guard creates it)."""
    for name in _GUARDED:
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
    return monkeypatch


def test_the_guard_pins_the_migration_url_over_a_production_one(guard_env):
    guard_env.setenv("DATABASE_URL_MIGRATE", PROD_LIKE)

    enforce_test_database()

    assert os.environ["DATABASE_URL_MIGRATE"] == TEST_SYNC


@pytest.mark.parametrize("var", AMBIENT_REDIRECT_VARS)
def test_the_guard_refuses_libpq_variables_that_reroute_a_dsn(guard_env, var):
    guard_env.setenv(var, "127.0.0.1")
    with pytest.raises(SystemExit, match=var):
        enforce_test_database()


@pytest.mark.parametrize("dsn", [
    TEST_SYNC + "?service=prod",                                      # pg_service.conf decides
    TEST_SYNC.replace(f":{make_url(TEST_SYNC).port}/", "/", 1),       # PGPORT decides
    make_url(TEST_SYNC).set(database="postgres").render_as_string(hide_password=False),
], ids=["service-key", "no-port", "not-a-test-name"])
def test_the_guard_refuses_a_dsn_that_is_not_certainly_the_test_database(guard_env, dsn):
    guard_env.setenv("TEST_DATABASE_URL_SYNC", dsn)
    with pytest.raises(SystemExit):
        enforce_test_database()


def test_the_teardown_check_refuses_libpq_variables_set_mid_run(guard_env):
    """assert_engine_is_test runs right before the destructive teardown; a libpq
    variable set since the guard ran would reroute the engine's next connection."""
    guard_env.setenv("PGHOSTADDR", "127.0.0.1")
    with pytest.raises(SystemExit, match="PGHOSTADDR"):
        assert_engine_is_test(os.environ["TEST_DATABASE_URL"])


# ── Alembic, in a subprocess ──────────────────────────────────────────────────


def _alembic(tmp_path: Path, **env_overrides: str | None) -> subprocess.CompletedProcess:
    """`alembic.command.current()` through a COPY of the real alembic/ directory,
    a trap `.env` inside it. The child runs in tmp_path (so pydantic's env_file
    finds no .env either) with the suite's environment minus DATABASE_URL_MIGRATE
    and the libpq routing variables, then `env_overrides` (None removes)."""
    alembic_dir = tmp_path / "alembic"
    if not alembic_dir.exists():
        shutil.copytree(REPO / "alembic", alembic_dir,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (alembic_dir / ".env").write_text(f"DATABASE_URL_MIGRATE={TRAP_URL}\n")
        (tmp_path / "child.py").write_text(_CHILD)
    env = {k: v for k, v in os.environ.items()
           if k != "DATABASE_URL_MIGRATE" and k not in AMBIENT_REDIRECT_VARS}
    env["PYTHONPATH"] = str(REPO)
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run(  # noqa: S603 - fixed argv, our own interpreter and script
        [sys.executable, str(tmp_path / "child.py"), str(alembic_dir)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180,
    )


def test_alembic_never_reads_a_dotenv_file(tmp_path):
    """The trap .env beside env.py names a production host. It must not be read:
    Alembic reads the TEST database and the variable never enters the process."""
    run = _alembic(tmp_path)

    assert run.returncode == 0, run.stderr[-2000:]
    assert "CURRENT_OK MIGRATE_IN_ENV=False" in run.stdout
    assert "dotenv-prod.invalid" not in run.stderr


def test_alembic_without_a_url_says_so(tmp_path):
    run = _alembic(tmp_path, DATABASE_URL_SYNC=None)

    assert run.returncode != 0
    assert "neither DATABASE_URL_MIGRATE nor DATABASE_URL_SYNC is set" in run.stderr


def test_under_test_alembic_refuses_a_database_that_is_not_a_test_database(tmp_path):
    """Both URLs on production (so an equality check alone would pass): refused
    before any connection is attempted."""
    run = _alembic(tmp_path, DATABASE_URL_MIGRATE=PROD_LIKE, TEST_DATABASE_URL_SYNC=PROD_LIKE)

    assert run.returncode != 0
    assert "refuses to migrate under ENVIRONMENT=test" in run.stderr
    assert "translate host name" not in run.stderr  # never tried to connect


def _other(**parts) -> str:
    return make_url(TEST_SYNC).set(**parts).render_as_string(hide_password=False)


@pytest.mark.parametrize("target", [
    _other(database="other_bridgeleads_test"),
    _other(port=5433),
    _other(host="localhost" if make_url(TEST_SYNC).host != "localhost" else "127.0.0.1"),
], ids=["database", "port", "host"])
def test_under_test_alembic_refuses_a_test_database_that_is_not_the_validated_one(
        tmp_path, target):
    """Every target here classifies as a test database; only the identity check
    (host, port, database of TEST_DATABASE_URL_SYNC) refuses it."""
    run = _alembic(tmp_path, DATABASE_URL_MIGRATE=target)

    assert run.returncode != 0
    assert "is not TEST_DATABASE_URL_SYNC" in run.stderr


def test_a_handed_in_connection_is_identified_despite_its_masked_password(tmp_path):
    """scripts/migrate.py's path: the connection's URL renders its password as ***;
    the identity check compares host, port and database only."""
    run = _alembic(tmp_path, HANDED_URL=TEST_SYNC)

    assert run.returncode == 0, run.stderr[-2000:]
    assert "CURRENT_OK" in run.stdout


def test_a_percent_encoded_password_reaches_the_database(tmp_path):
    """Alembic's config is a ConfigParser, where '%' starts an interpolation. The
    same password with its first character percent-encoded must still connect."""
    url = make_url(TEST_SYNC)
    encoded = f"%{ord(url.password[0]):02X}{url.password[1:]}"
    target = TEST_SYNC.replace(f":{url.password}@", f":{encoded}@", 1)
    assert target != TEST_SYNC

    run = _alembic(tmp_path, DATABASE_URL_MIGRATE=target)

    assert run.returncode == 0, run.stderr[-2000:]
    assert "CURRENT_OK" in run.stdout


@pytest.mark.parametrize("allowlisted", [True, False])
def test_an_allowlisted_remote_test_host_passes_the_belt(tmp_path, allowlisted):
    """127.0.0.2 is not in the local set, so only the allowlist admits it. Nothing
    listens on port 1 and the attempt is bounded: no DNS, no remote host."""
    remote = "postgresql+psycopg2://u:p@127.0.0.2:1/remote_test?connect_timeout=2"
    run = _alembic(tmp_path, DATABASE_URL_MIGRATE=remote, TEST_DATABASE_URL_SYNC=remote,
                   TEST_DB_HOST_ALLOWLIST="127.0.0.2" if allowlisted else "")

    assert run.returncode != 0  # nothing listens there either way
    if allowlisted:
        assert "refuses to migrate" not in run.stderr  # the belt let it through
    else:
        assert "not in TEST_DB_HOST_ALLOWLIST" in run.stderr


def _ambient(tmp_path: Path, var: str) -> dict[str, str]:
    """`var` set to a value the local test database still connects under: a service
    file that exists (its service adds nothing the explicit DSN does not override)."""
    service_file = tmp_path / "pg_service.conf"
    service_file.write_text("[bl_test]\nconnect_timeout=5\n")
    return {
        "PGHOSTADDR": {"PGHOSTADDR": "127.0.0.1"},
        "PGSERVICE": {"PGSERVICE": "bl_test", "PGSERVICEFILE": str(service_file)},
        "PGSERVICEFILE": {"PGSERVICEFILE": str(service_file)},
        "PGSYSCONFDIR": {"PGSYSCONFDIR": str(tmp_path)},
    }[var]


@pytest.mark.parametrize("handed", [False, True], ids=["bare-url", "handed-connection"])
@pytest.mark.parametrize("var", AMBIENT_REDIRECT_VARS)
def test_under_test_alembic_refuses_libpq_rerouting_variables(tmp_path, var, handed):
    """On the handed-in path the child CONNECTS first (the values still reach the
    local test database), so only env.py's own check stops the migration."""
    extra = {"HANDED_URL": TEST_SYNC} if handed else {}
    run = _alembic(tmp_path, **_ambient(tmp_path, var), **extra)

    assert run.returncode != 0
    assert "refuses to migrate" in run.stderr and var in run.stderr


# ── Migrations never build their own connection ───────────────────────────────

_ENGINE_FACTORIES = frozenset({"create_engine", "create_async_engine", "engine_from_config"})
# Dynamic access could reach any of the above without naming it. No migration uses
# these today, so they are refused outright rather than interpreted.
_DYNAMIC = frozenset({"getattr", "import_module", "__import__", "eval", "exec"})
_TELLTALES = ("DATABASE_URL", "src.db.session", *_ENGINE_FACTORIES)


def _docstrings(tree: ast.AST) -> set[int]:
    """The real docstrings: the first statement of a module, class or function."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                out.add(id(first.value))
    return out


def _literal(node: ast.AST) -> str | None:
    """A string literal, a concatenation of them, or an f-string's literal parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    return None


def _is_src_db(module: str) -> bool:
    # src.db's __init__ imports src.db.session, which builds the engines.
    return module == "src.db" or module.startswith("src.db.")


def _migration_offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstrings(tree)
    out = []
    for node in ast.walk(tree):
        where = f"{path.name}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            out += [f"{where} imports {a.name}" for a in node.names if _is_src_db(a.name)]
        elif isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            module = node.module or ""
            if (_is_src_db(module) or names & (_ENGINE_FACTORIES | _DYNAMIC)
                    or (module == "src" and "db" in names)):
                out.append(f"{where} imports {sorted(names)} from {node.module}")
        elif isinstance(node, ast.Attribute) and (
                node.attr in _ENGINE_FACTORIES | _DYNAMIC or "DATABASE_URL" in node.attr):
            out.append(f"{where} uses .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in _ENGINE_FACTORIES | _DYNAMIC:
            out.append(f"{where} uses {node.id}")
        elif id(node) not in docs:
            text = _literal(node)
            if text and any(t in text for t in _TELLTALES):
                out.append(f"{where} names {text[:60]!r}")
    return out


def test_no_migration_builds_an_engine_or_reads_a_database_url():
    """A migration runs on the connection Alembic hands it. One that built an engine
    from Settings or DATABASE_URL* would bypass everything above (Codex round 1)."""
    offenders = [o for p in sorted((REPO / "alembic" / "versions").glob("*.py"))
                 for o in _migration_offences(p)]
    assert not offenders, offenders


@pytest.mark.parametrize("source", [
    "from sqlalchemy import create_engine as ce\n",
    "import sqlalchemy as sa\nsa.create_engine('x')\n",
    "from src.db import session\n",
    "import src.db.session\n",
    "from src.db.models import Base\n",
    "import os\nos.environ['DATABASE_' + 'URL_SYNC']\n",
    "import os\nos.getenv(f'DATABASE_URL{1}')\n",
    "import importlib\nimportlib.import_module('x')\n",
    "x = getattr(object, 'y')\n",
    "def f():\n    pass\n    'DATABASE_URL'\n",  # a bare string that is not a docstring
    "from importlib import import_module as im\n",
    "from builtins import __import__ as imp\n",
    "from src import db\n",
], ids=["alias", "attribute", "from-src-db", "import-session", "src-db-models",
        "concatenated", "f-string", "import-module", "getattr", "not-a-docstring",
        "aliased-import-module", "aliased-dunder-import", "from-src-import-db"])
def test_the_migration_scan_catches_what_it_is_for(tmp_path, source):
    """The scan is only as good as what it can see: each of these must be flagged."""
    path = tmp_path / "999_probe.py"
    path.write_text(f'"""A docstring naming DATABASE_URL is fine."""\n{source}')
    assert _migration_offences(path)


def test_the_migration_scan_leaves_docstrings_and_src_db_safety_alone(tmp_path):
    path = tmp_path / "999_probe.py"
    path.write_text('"""Mentions DATABASE_URL_SYNC and create_engine."""\n'
                    "import src.db_safety\n"
                    "def upgrade():\n    \"\"\"Also DATABASE_URL.\"\"\"\n")
    assert _migration_offences(path) == []
