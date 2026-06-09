"""Credential helper for the dev/audit scripts in this directory.

H4 (security checklist audit 2026-06-08): the audit/E2E scripts used to
hardcode real and throwaway BridgeLeads passwords as string literals. That
put a privileged admin password in plaintext across several local files and
left `scripts/` one `git add` away from committing it. These helpers pull
credentials from the environment instead, so no secret lives in code.

Two flavours:

- ``admin_creds()`` — the scripts that log in as the real platform admin to
  audit production. The email defaults to the well-known admin address (not a
  secret); the PASSWORD must come from ``BRIDGELEADS_ADMIN_PASSWORD`` with no
  default, so a missing env fails loudly instead of silently using a baked-in
  secret.

- ``test_password()`` — the E2E scripts that register a fresh throwaway
  account (random email) and then log into it within the same run. The
  password is not a pre-existing secret, just a value reused across the
  register+login calls of one run, so it comes from
  ``BRIDGELEADS_TEST_PASSWORD`` or is generated as a strong random string that
  satisfies the API's 10–72 char policy.

Usage:
    from _creds import admin_creds, test_password
    EMAIL, PASSWORD = admin_creds()      # raises if env not set
    password = test_password()           # env or generated
"""

import os
import secrets
import sys

DEFAULT_ADMIN_EMAIL = "admin@bridgeleads.io"
DEFAULT_FIXTURE_EMAIL = "preforeclosure_test@bridgeleads.io"


def admin_creds() -> tuple[str, str]:
    """Return (email, password) for the real platform admin from the env.

    Email: ``BRIDGELEADS_ADMIN_EMAIL`` (defaults to the known admin address).
    Password: ``BRIDGELEADS_ADMIN_PASSWORD`` — required, no default. Exits with
    a clear message if unset so a script never falls back to a hardcoded secret.
    """
    email = os.environ.get("BRIDGELEADS_ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL)
    password = os.environ.get("BRIDGELEADS_ADMIN_PASSWORD")
    if not password:
        sys.exit(
            "BRIDGELEADS_ADMIN_PASSWORD is not set. Export it before running "
            "this script, e.g.:\n"
            "  export BRIDGELEADS_ADMIN_PASSWORD='<admin password>'\n"
            "(PowerShell: $env:BRIDGELEADS_ADMIN_PASSWORD='<admin password>')"
        )
    return email, password


def fixture_creds() -> tuple[str, str]:
    """Return (email, password) for the persistent E2E fixture account.

    A few UI E2E scripts reuse one pre-registered account
    (``preforeclosure_test@bridgeleads.io``) rather than registering a fresh
    one each run. Email defaults to that known address; password comes from
    ``BRIDGELEADS_FIXTURE_PASSWORD`` (required, no default) so the real
    account password is never baked into code.
    """
    email = os.environ.get("BRIDGELEADS_FIXTURE_EMAIL", DEFAULT_FIXTURE_EMAIL)
    password = os.environ.get("BRIDGELEADS_FIXTURE_PASSWORD")
    if not password:
        sys.exit(
            "BRIDGELEADS_FIXTURE_PASSWORD is not set. Export the password for "
            f"the E2E fixture account ({email}) before running this script."
        )
    return email, password


def test_password() -> str:
    """Return a password for a freshly-registered throwaway E2E account.

    Uses ``BRIDGELEADS_TEST_PASSWORD`` if set, else generates a strong random
    password (>=16 chars, mixed) that satisfies the API password policy. The
    same value is reused for the register + login of a single run.
    """
    env = os.environ.get("BRIDGELEADS_TEST_PASSWORD")
    if env:
        # The API enforces a 10–72 char password policy (src/api/schemas.py);
        # fail loudly on a bad override instead of a confusing 422 at register.
        if not (10 <= len(env) <= 72):
            sys.exit(
                "BRIDGELEADS_TEST_PASSWORD must be 10–72 characters "
                f"(got {len(env)}); fix it or unset to use a generated one."
            )
        return env
    # token_urlsafe(16) is ~22 chars; append fixed class chars so the policy
    # (length + has-upper/lower/digit/symbol, if enforced) is always satisfied.
    return f"E2e!{secrets.token_urlsafe(16)}aA1"
