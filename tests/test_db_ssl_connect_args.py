"""Tests for `_ssl_connect_args` — the M5 DB transport-security helper.

Forces TLS on non-local Postgres connections (Supabase ships URLs with no
sslmode → libpq 'prefer' silently allows plaintext). Pins the contract:

  * localhost / CI Postgres        -> {} (no TLS forced; dev/test unaffected)
  * remote, no sslmode             -> ssl/sslmode = require (encrypt)
  * remote, already-encrypted mode -> {} (respect the URL)
  * remote, plaintext-capable mode + production -> raises (fail fast)
  * remote, plaintext-capable mode + non-prod   -> {} (respect the choice)

Pure function, no DB needed.
"""

import pytest

from src.config import settings
from src.db.session import _ssl_connect_args


@pytest.fixture
def restore_env():
    saved = settings.ENVIRONMENT
    yield
    settings.ENVIRONMENT = saved


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_local_hosts_never_force_tls(host):
    url = f"postgresql://u:p@{host}:5432/db"
    assert _ssl_connect_args(url, async_driver=True) == {}
    assert _ssl_connect_args(url, async_driver=False) == {}


def test_remote_no_sslmode_forces_require():
    url = "postgresql+asyncpg://u:p@db.abc.supabase.co:5432/postgres"
    assert _ssl_connect_args(url, async_driver=True) == {"ssl": "require"}
    sync_url = "postgresql://u:p@db.abc.supabase.co:6543/postgres"
    assert _ssl_connect_args(sync_url, async_driver=False) == {"sslmode": "require"}


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_remote_already_encrypted_is_left_alone(mode):
    url = f"postgresql://u:p@db.abc.supabase.co:6543/postgres?sslmode={mode}"
    assert _ssl_connect_args(url, async_driver=False) == {}


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_remote_plaintext_mode_fails_fast_in_production(restore_env, mode):
    settings.ENVIRONMENT = "production"
    url = f"postgresql://u:p@db.abc.supabase.co:6543/postgres?sslmode={mode}"
    with pytest.raises(RuntimeError, match="plaintext-capable"):
        _ssl_connect_args(url, async_driver=False)


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_remote_plaintext_mode_respected_in_non_production(restore_env, mode):
    settings.ENVIRONMENT = "development"
    url = f"postgresql://u:p@db.abc.supabase.co:6543/postgres?sslmode={mode}"
    assert _ssl_connect_args(url, async_driver=False) == {}
