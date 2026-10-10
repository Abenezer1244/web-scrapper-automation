"""The production migration path: scripts/migrate.py and scripts/wait_for_schema.py.

The API is the only migrator (advisory locked, fail closed); worker and beat wait
for the schema (docs/deployment/migrations.md). Nothing pinned the lock before.
Each test gets a FRESH scratch database on the test server, named after the
validated test database and ending in `_test`, so alembic/env.py's test guard
accepts it, and drops it afterwards. Never production.
"""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import migrate  # noqa: E402
import wait_for_schema  # noqa: E402

LOCK = {"c": migrate.LOCK_CLASSID, "o": migrate.LOCK_OBJID}
SCRIPT = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
HEAD = SCRIPT.get_current_head()


@pytest.fixture
def scratch_url():
    base = make_url(os.environ["TEST_DATABASE_URL_SYNC"])
    name = f"{base.database}_mig{uuid.uuid4().hex[:8]}_test"
    admin = create_engine(base.set(database="template1"), poolclass=NullPool,
                          isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _env(url: str) -> dict:
    env = dict(os.environ)
    env.update(DATABASE_URL_MIGRATE=url, DATABASE_URL_SYNC=url, TEST_DATABASE_URL_SYNC=url)
    return env


def _spawn(url: str, *args: str, out=subprocess.PIPE) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, *args], cwd=ROOT, env=_env(url),
                            stdout=out, stderr=subprocess.STDOUT, text=True)


class _Migrator:
    """migrate.py writing to a FILE, not a pipe: Alembic logs every revision, and a
    lock holder blocked on a full, undrained pipe would hold the lock forever."""

    def __init__(self, url: str, tmp_path: Path):
        self.log = tmp_path / f"migrate-{uuid.uuid4().hex[:6]}.log"
        self._fh = self.log.open("w")
        self.proc = _spawn(url, "scripts/migrate.py", out=self._fh)

    def finish(self) -> tuple[int, str]:
        self.proc.wait(timeout=600)
        self._fh.close()
        return self.proc.returncode, self.log.read_text()


def _run_migrate(url: str, tmp_path: Path) -> tuple[int, str]:
    return _Migrator(url, tmp_path).finish()


def _versions(url: str) -> list[str]:
    e = create_engine(url, poolclass=NullPool)
    try:
        with e.connect() as c:
            if c.execute(text("SELECT to_regclass('public.alembic_version')")).scalar() is None:
                return []
            return [r[0] for r in c.execute(text("SELECT version_num FROM alembic_version"))]
    finally:
        e.dispose()


def _lock_is_free(url: str) -> bool:
    e = create_engine(url, poolclass=NullPool)
    try:
        with e.connect() as c:
            got = c.execute(text("SELECT pg_try_advisory_lock(:c, :o)"), LOCK).scalar()
            if got:
                c.execute(text("SELECT pg_advisory_unlock(:c, :o)"), LOCK)
            return bool(got)
    finally:
        e.dispose()


def test_a_failed_migration_releases_the_lock_and_a_restart_reaches_head(scratch_url, tmp_path):
    # A table the first revision creates, already there: the first upgrade fails.
    e = create_engine(scratch_url, poolclass=NullPool)
    with e.begin() as c:
        c.execute(text("CREATE TABLE users (id int)"))
    code, out = _run_migrate(scratch_url, tmp_path)
    assert code != 0, out
    assert _versions(scratch_url) != [HEAD]
    assert _lock_is_free(scratch_url), "a failed run must not leave the lock held"

    with e.begin() as c:
        c.execute(text("DROP TABLE users"))
    e.dispose()
    code, out = _run_migrate(scratch_url, tmp_path)
    assert code == 0, out
    assert _versions(scratch_url) == [HEAD]


def test_runners_blocked_on_the_lock_both_reach_head_once_and_a_rerun_is_a_noop(scratch_url, tmp_path):
    holder = create_engine(scratch_url, poolclass=NullPool).connect()
    assert holder.execute(text("SELECT pg_try_advisory_lock(:c, :o)"), LOCK).scalar()
    holder.commit()
    runners = [_Migrator(scratch_url, tmp_path) for _ in range(2)]
    try:
        time.sleep(8)  # several poll intervals: both must be waiting, neither migrating
        assert all(r.proc.poll() is None for r in runners)
        assert _versions(scratch_url) == []
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:c, :o)"), LOCK)
        holder.commit()
        holder.close()
    done = [r.finish() for r in runners]
    outs = [o for _, o in done]
    assert [c for c, _ in done] == [0, 0], outs
    assert all("migration lock held by another replica; waiting" in o for o in outs)
    assert all("lock acquired as role " in o for o in outs)
    assert _versions(scratch_url) == [HEAD]

    code, out = _run_migrate(scratch_url, tmp_path)
    assert code == 0 and _versions(scratch_url) == [HEAD], out


def test_a_killed_lock_holder_releases_the_lock(scratch_url):
    hold = (
        "import os,sys,time\n"
        "from sqlalchemy import create_engine,text\n"
        "c=create_engine(os.environ['DATABASE_URL_MIGRATE']).connect()\n"
        f"c.execute(text('SELECT pg_advisory_lock({LOCK['c']},{LOCK['o']})')); c.commit()\n"
        "print('held',flush=True); time.sleep(120)\n"
    )
    p = _spawn(scratch_url, "-c", hold)
    try:
        assert p.stdout.readline().strip() == "held"
        assert not _lock_is_free(scratch_url)
        p.kill()  # no unlock, no clean close: the connection just dies
        p.wait(timeout=30)
        deadline = time.monotonic() + 15
        while not _lock_is_free(scratch_url):
            assert time.monotonic() < deadline, "lock outlived its dead holder"
            time.sleep(0.5)
    finally:
        if p.poll() is None:
            p.kill()


def test_the_lock_wait_budget_fails_closed_without_touching_the_schema(scratch_url, monkeypatch):
    holder = create_engine(scratch_url, poolclass=NullPool).connect()
    assert holder.execute(text("SELECT pg_try_advisory_lock(:c, :o)"), LOCK).scalar()
    holder.commit()
    monkeypatch.setenv("DATABASE_URL_MIGRATE", scratch_url)
    monkeypatch.setattr(migrate, "LOCK_WAIT_BUDGET_S", 3)
    try:
        assert migrate.main() == 1
        assert _versions(scratch_url) == []
    finally:
        holder.close()


# ── wait_for_schema ──────────────────────────────────────────────────────────

def test_schema_state_classifies_behind_ready_and_ahead():
    prev = SCRIPT.get_revision(HEAD).down_revision
    assert wait_for_schema.schema_state(set(), SCRIPT) == "behind"
    assert wait_for_schema.schema_state({prev}, SCRIPT) == "behind"
    assert wait_for_schema.schema_state({HEAD}, SCRIPT) == "ready"
    assert wait_for_schema.schema_state({"999_from_a_newer_release"}, SCRIPT) == "ahead"


def test_wait_for_schema_times_out_behind_then_starts_at_head_and_ahead(scratch_url, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DATABASE_URL_SYNC", scratch_url)
    monkeypatch.setattr(wait_for_schema, "WAIT_BUDGET_S", 2)
    monkeypatch.setattr(wait_for_schema, "POLL_BASE_S", 0.5)
    monkeypatch.setattr(wait_for_schema, "POLL_SPREAD_S", 0.0)
    assert wait_for_schema.main() == 1  # empty database: never reaches head
    assert "refusing to start" in capsys.readouterr().err

    code, out = _run_migrate(scratch_url, tmp_path)
    assert code == 0, out
    assert wait_for_schema.main() == 0

    e = create_engine(scratch_url, poolclass=NullPool)
    with e.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = '999_newer'"))
    e.dispose()
    assert wait_for_schema.main() == 0
    assert "AHEAD" in capsys.readouterr().out


@pytest.mark.integration
def test_the_runtime_role_sees_the_revision_only_through_116(scratch_url, tmp_path):
    """Production 2026-10-10: bridgeleads_system saw 0 rows of alembic_version (RLS
    from 027, no policy), so a worker waiting on it would never start."""
    admin = create_engine(scratch_url, poolclass=NullPool)
    with admin.connect() as c:
        if c.execute(text("SELECT count(*) FROM pg_roles WHERE rolname = 'bridgeleads_system'")).scalar() != 1:
            pytest.skip("bridgeleads_system not provisioned on this server")
    code, out = _run_migrate(scratch_url, tmp_path)
    assert code == 0, out

    def visible_as_system() -> int:
        with admin.connect() as c:
            c.execute(text("SET LOCAL ROLE bridgeleads_system"))
            return c.execute(text("SELECT count(*) FROM alembic_version")).scalar()

    try:
        assert visible_as_system() == 1
        with admin.begin() as c:  # the negative control: without 116's policy, RLS hides it
            c.execute(text("DROP POLICY alembic_version_system_select ON alembic_version"))
        assert visible_as_system() == 0
    finally:
        admin.dispose()
