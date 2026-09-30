"""The local docker-compose stack never consumes the checkout's `.env`.

A checkout's `.env` may name PRODUCTION (the synced OneDrive one does). The local
stack used to read it three ways: `env_file: .env`, a `.:/app` mount that let
pydantic's `env_file=".env"` find it inside the container, and compose's own
`${...}` interpolation. `docker compose up` there would have scraped, spent and
migrated against production. Now it reads `.env.local`, and pins in
`environment:` (which beats `env_file:`) the database and Redis URLs, blank
encryption keys, and the paid and destructive switches. Docker is not installed where this was built, so the
compose file itself is what these tests read (Codex local-env consult).
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE_TEXT = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
SERVICES = COMPOSE["services"]
APP_SERVICES = ("migrate", "api", "worker", "beat")  # run this repo's code
APP_MOUNTS = {"./src:/app/src", "./main.py:/app/main.py"}

_LOCAL_DB = "bridgeleads:bridgeleads_local_only@postgres/bridgeleads"
# Pinned in every app service's `environment:`, which beats `env_file:`. The
# database URLs carry NO port: src/db/session.py rewrites ":5432/" to ":6543/".
PINNED = {
    "DATABASE_URL": f"postgresql+asyncpg://{_LOCAL_DB}",
    "DATABASE_URL_SYNC": f"postgresql+psycopg2://{_LOCAL_DB}",
    "DATABASE_URL_MIGRATE": f"postgresql+psycopg2://{_LOCAL_DB}",
    "REDIS_URL": "redis://:bridgeleads_local_only@redis:6379/0",
    "ALLOWED_ORIGINS": "http://localhost:3000,http://127.0.0.1:3000",
    "FRONTEND_URL": "http://localhost:3000",
    "API_BASE_URL": "http://localhost:8000",
    "FIELD_ENCRYPTION_KEY": "",
    "BLIND_INDEX_KEY": "",
    "PII_ENCRYPTION_STRICT": "false",
    "TRACERFY_WEBHOOK_SECRET": "",
    "ENVIRONMENT": "development",
    "SKIP_TRACE_ENABLED": "false",
    "TRACERFY_API_TOKEN": "",
    "CAPTCHA_ENABLED": "false",
    "REGRID_ENABLED": "false",
    "RETENTION_PURGE_ENABLED": "false",
    "RETENTION_PURGE_DRY_RUN": "true",
}


def test_the_service_list_is_the_one_these_tests_know():
    """A new service must be classified here (app code or infrastructure) before it
    can slip past the checks below."""
    assert set(SERVICES) == {"postgres", "redis", *APP_SERVICES}


def test_nothing_is_interpolated():
    """Compose fills ${...} from the project `.env`, i.e. possibly production."""
    code = "\n".join(line.split("#", 1)[0] for line in COMPOSE_TEXT.splitlines())
    assert not re.search(r"\$\{?[A-Za-z_]", code), "no ${VAR} / $VAR in docker-compose.yml"


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_no_service_reads_the_dotenv_file(name):
    env_file = SERVICES[name].get("env_file")
    files = [env_file] if isinstance(env_file, str) else list(env_file or [])
    files = [f["path"] if isinstance(f, dict) else f for f in files]
    if name in APP_SERVICES:
        assert files == [".env.local"]
    else:
        assert files == []


def _binds(svc: dict) -> set[str]:
    """Host bind mounts as "source:target", short or long syntax."""
    out = set()
    for v in svc.get("volumes", []):
        if isinstance(v, dict):
            if v.get("type") == "bind" or str(v.get("source", "")).startswith((".", "/", "~")):
                out.add(f"{v.get('source')}:{v.get('target')}")
        elif v.split(":", 1)[0].startswith((".", "/", "~")):
            out.add(":".join(v.split(":")[:2]))
    return out


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_no_service_mounts_the_checkout(name):
    """Only the hot-reload source mounts, never `.` (whose `.env` pydantic would read
    as /app/.env) and never a `.env` file."""
    if name in APP_SERVICES and name != "migrate":
        assert _binds(SERVICES[name]) == APP_MOUNTS
    else:
        assert _binds(SERVICES[name]) == set()


@pytest.mark.parametrize("name", APP_SERVICES)
def test_every_app_service_pins_everything_that_could_reach_production(name):
    """Exact values: the local containers, local-only keys, switches off."""
    env = SERVICES[name]["environment"]
    assert isinstance(env, dict)
    assert all(v is not None for v in env.values()), "a null entry passes the host value through"
    assert {k: env.get(k) for k in PINNED} == PINNED
    for key in ("DATABASE_URL", "DATABASE_URL_SYNC", "DATABASE_URL_MIGRATE"):
        assert (urlparse(env[key]).hostname, urlparse(env[key]).port) == ("postgres", None)
    # No signing key is committed (security rule: no secrets in code); the
    # developer's own local SECRET_KEY comes from .env.local.
    assert "SECRET_KEY" not in env


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_published_ports_are_loopback_only_and_no_build_secrets(name):
    svc = SERVICES[name]
    for port in svc.get("ports", []):
        host_ip = port.get("host_ip") if isinstance(port, dict) else str(port).split(":", 1)[0]
        assert host_ip == "127.0.0.1", port
    assert "secrets" not in svc
    assert "network_mode" not in svc  # host networking would bypass all of the above
    build = svc.get("build")
    assert build is None or build == "."  # no build args to carry values in


def test_the_app_starts_only_after_the_locked_migration_on_healthy_containers():
    assert SERVICES["migrate"]["command"] == "python scripts/migrate.py"
    healthy = {"condition": "service_healthy"}
    done = {"condition": "service_completed_successfully"}
    assert SERVICES["migrate"]["depends_on"] == {"postgres": healthy}
    for name in ("api", "worker"):
        assert SERVICES[name]["depends_on"] == {
            "postgres": healthy, "redis": healthy, "migrate": done}, name
    assert SERVICES["beat"]["depends_on"] == {"redis": healthy, "migrate": done}


def test_the_image_never_contains_a_dotenv_file():
    lines = {ln.strip() for ln in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    assert {".env", ".env.*", "!.env.example"} <= lines
