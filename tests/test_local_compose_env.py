"""The local docker-compose stack can never run on production config.

A checkout's `.env` may name PRODUCTION (the synced OneDrive one does). The local
stack used to read it three ways: `env_file: .env`, a `.:/app` mount that let
pydantic's `env_file=".env"` find it inside the container, and compose's own
`${...}` interpolation. `docker compose up` there would have scraped, spent and
migrated against production. Docker is not installed where this was built, so
the compose file itself is what these tests read (Codex local-env consult).
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

# Pinned in every app service's `environment:`, which beats `env_file:`.
PINNED_SWITCHES = {
    "ENVIRONMENT": "development",
    "SKIP_TRACE_ENABLED": "false",
    "TRACERFY_API_TOKEN": "",
    "CAPTCHA_ENABLED": "false",
    "REGRID_ENABLED": "false",
    "AI_ENRICHMENT_ENABLED": "false",
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


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_no_service_mounts_the_checkout(name):
    """Only the hot-reload source mounts, never `.` (whose `.env` pydantic would read
    as /app/.env) and never a `.env` file."""
    binds = [v for v in SERVICES[name].get("volumes", [])
             if isinstance(v, str) and v.split(":", 1)[0].startswith((".", "/", "~"))]
    if name in APP_SERVICES and name != "migrate":
        assert set(binds) == APP_MOUNTS
    else:
        assert binds == []


@pytest.mark.parametrize("name", APP_SERVICES)
def test_every_app_service_pins_the_database_and_redis_to_the_local_containers(name):
    env = SERVICES[name]["environment"]
    assert isinstance(env, dict)
    assert all(v is not None for v in env.values()), "a null entry passes the host value through"
    for key in ("DATABASE_URL", "DATABASE_URL_SYNC", "DATABASE_URL_MIGRATE"):
        url = urlparse(env[key])
        assert (url.hostname, url.port) == ("postgres", None), key
        # src/db/session.py rewrites ":5432/" to ":6543/" on the sync URL.
        assert ":5432/" not in env[key], key
    redis = urlparse(env["REDIS_URL"])
    assert redis.hostname == "redis"


@pytest.mark.parametrize("name", APP_SERVICES)
def test_every_app_service_pins_paid_and_destructive_switches_off(name):
    env = SERVICES[name]["environment"]
    assert {k: env.get(k) for k in PINNED_SWITCHES} == PINNED_SWITCHES


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_published_ports_are_loopback_only_and_no_build_secrets(name):
    svc = SERVICES[name]
    for port in svc.get("ports", []):
        assert str(port).startswith("127.0.0.1:"), port
    assert "secrets" not in svc
    build = svc.get("build")
    assert build is None or build == "."  # no build args to carry values in


def test_the_app_starts_only_after_the_locked_migration():
    assert SERVICES["migrate"]["command"] == "python scripts/migrate.py"
    for name in ("api", "worker", "beat"):
        dep = SERVICES[name]["depends_on"]["migrate"]
        assert dep == {"condition": "service_completed_successfully"}, name


def test_the_image_never_contains_a_dotenv_file():
    lines = {ln.strip() for ln in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    assert {".env", ".env.*", "!.env.example"} <= lines
