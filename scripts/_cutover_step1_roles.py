"""RLS cutover — STEP 1 (prod): create the two roles + test pooler auth.

Minimal, reversible, blocker-surfacing. Creates bridgeleads_app /
bridgeleads_system (inert — they serve no traffic until the repoint) as the
postgres admin via the SESSION pooler (:5432), then makes ONE attempt to
connect as `bridgeleads_app.<PROJECT_REF>` through the pooler to answer the
make-or-break question: can a custom role authenticate via the Supabase pooler?

Idempotency + secret safety (Codex review):
- Passwords live in .rls-cutover-secrets (gitignored). Loaded on start.
- A password is generated + written DURABLY *before* CREATE ROLE, so a mid-run
  failure never orphans a role with an unknown password.
- An existing role is reused only if its password is in the secrets file;
  otherwise we refuse (can't auth-test / repoint with an unknown password).
- Fail-closed unless the admin DSN is on the :5432 session pooler.
- Bad repeated auth can trip a Supavisor circuit breaker — exactly ONE app
  auth attempt.

Run:  PYTHONPATH=. python scripts/_cutover_step1_roles.py
Reverse: DROP ROLE bridgeleads_app, bridgeleads_system;  (if unused)
"""

from __future__ import annotations

import os
import secrets
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg2

_SECRETS = ".rls-cutover-secrets"


def _raw_sync_url() -> str:
    with open(".env", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("DATABASE_URL_SYNC="):
                return line.split("=", 1)[1]
    raise SystemExit("DATABASE_URL_SYNC not found in .env")


def _load_secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    if os.path.exists(_SECRETS):
        with open(_SECRETS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k] = v
    return out


def _write_secrets(d: dict[str, str]) -> None:
    # Atomic + durable: write a temp file, fsync, os.replace (Codex review) so a
    # crash mid-write can't corrupt/lose the prod role passwords.
    tmp = _SECRETS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for k, v in d.items():
            f.write(f"{k}={v}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _SECRETS)


def main() -> None:
    raw = _raw_sync_url()
    dsn = raw.replace("postgresql+psycopg2://", "postgresql://")
    parts = urlsplit(dsn)

    if parts.port != 5432:
        raise SystemExit(
            f"refusing to run: admin DSN port is {parts.port}, not the :5432 session "
            "pooler — DDL/auth must run on the session pooler. Check .env."
        )
    if "pooler.supabase.com" not in (parts.hostname or ""):
        raise SystemExit(
            f"refusing to run: admin DSN host {parts.hostname!r} is not the Supabase "
            "shared pooler — the auth test must exercise the pooler path the app uses."
        )
    admin_user = (parts.username or "")
    if "." not in admin_user:
        raise SystemExit(f"unexpected admin user {admin_user!r} — expected postgres.<ref>")
    project_ref = admin_user.split(".", 1)[1]
    print(f"admin user = {admin_user}  (project_ref={project_ref})  host = {parts.hostname}:{parts.port}")

    secrets_d = _load_secrets()
    secrets_d.setdefault("PROJECT_REF", project_ref)

    conn = psycopg2.connect(dsn, connect_timeout=15)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for role, key in (("bridgeleads_app", "BRIDGELEADS_APP_PW"),
                              ("bridgeleads_system", "BRIDGELEADS_SYSTEM_PW")):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                exists = cur.fetchone() is not None
                if exists:
                    if key not in secrets_d:
                        raise SystemExit(
                            f"role {role} already exists but its password is not in "
                            f"{_SECRETS}. Cannot proceed safely — DROP the role and rerun, "
                            "or restore the secrets file."
                        )
                    print(f"role {role} already exists — reusing stored password")
                else:
                    # Durable-write BEFORE create so a mid-run failure can't orphan
                    # a role with an unknown password.
                    pw = secrets.token_urlsafe(32)
                    secrets_d[key] = pw
                    _write_secrets(secrets_d)
                    cur.execute(
                        f"CREATE ROLE {role} LOGIN PASSWORD %s "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS",
                        (pw,),
                    )
                    print(f"created role {role}")
                # Supabase's `postgres` admin is NOT a superuser, so it cannot
                # ALTER the SUPERUSER/BYPASSRLS attributes (even to NO). CREATE
                # already set them to the safe defaults, and a non-superuser can
                # never grant SUPERUSER/BYPASSRLS, so they cannot drift upward.
                # LOGIN is alterable by a CREATEROLE admin; re-assert just that,
                # then VERIFY the protected attributes rather than ALTER them.
                cur.execute(f"ALTER ROLE {role} LOGIN")
                cur.execute(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
                    (role,),
                )
                rs, rb = cur.fetchone()
                if rs or rb:
                    raise SystemExit(
                        f"{role} has rolsuper={rs} rolbypassrls={rb} — both must be "
                        "false. The Supabase admin cannot fix this (not a superuser); "
                        "DROP the role and recreate, or fix via the Supabase dashboard."
                    )
            cur.execute(
                "SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname LIKE 'bridgeleads_%' ORDER BY rolname"
            )
            print("\n-- role status --")
            for r in cur.fetchall():
                print(f"  {r[0]}: super={r[1]} bypassrls={r[2]} createdb={r[3]} createrole={r[4]}")
    finally:
        conn.close()

    _write_secrets(secrets_d)
    print(f"\npasswords stored in {_SECRETS} (gitignored; do NOT commit)")

    # ── ONE auth test via the session pooler ────────────────────────────────
    app_pw = secrets_d["BRIDGELEADS_APP_PW"]
    app_user = f"bridgeleads_app.{project_ref}"
    test_dsn = urlunsplit((
        parts.scheme,
        f"{quote(app_user, safe='')}:{quote(app_pw, safe='')}@{parts.hostname}:{parts.port}",
        parts.path, parts.query, parts.fragment,
    ))
    print(f"\nauth test: connecting as {app_user} via {parts.hostname}:{parts.port} (ONE attempt)")
    try:
        t = psycopg2.connect(test_dsn, connect_timeout=15)
        with t.cursor() as cur:
            cur.execute("SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            row = cur.fetchone()
            print(f"  SUCCESS: connected as {row[0]} (bypassrls={row[1]})")
        t.close()
        print("\n>>> POOLER AUTH WORKS for custom roles. OK to proceed to policies + rehearsal.")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        print("\n>>> POOLER AUTH FAILED for the custom role. Do NOT repoint. Investigate "
              "the pooler username format / endpoint before any Railway change.")
        sys.exit(2)


if __name__ == "__main__":
    main()
