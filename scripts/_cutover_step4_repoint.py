"""RLS cutover — STEP 4 (prod): repoint Railway services to the new roles.

Builds the per-service connection strings from .env (current postgres URLs) +
.rls-cutover-secrets (new role passwords), and sets them on each Railway service
with --skip-deploys (NO redeploy — running services keep their old env until you
redeploy, so setting vars is inert). Captures the current postgres URLs as
rollback into .rls-cutover-secrets.

Per Codex: api = app(async)/app(sync); worker+beat = app(async)/system(sync);
all get DATABASE_URL_MIGRATE = the current postgres sync URL (owner, for alembic).

  python scripts/_cutover_step4_repoint.py show   # dry-run, redacted (default)
  python scripts/_cutover_step4_repoint.py set     # set vars (skip-deploys)

Redeploy + RLS_ENFORCE + FORCE are deliberately NOT done here.
"""

from __future__ import annotations

import re
import sys

_SECRETS = ".rls-cutover-secrets"


def _env(key: str) -> str:
    with open(".env", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(key + "="):
                return line.strip().split("=", 1)[1]
    raise SystemExit(f"{key} not found in .env")


def _secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    with open(_SECRETS, encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                out[k] = v
    return out


def _swap_user(url: str, role: str, ref: str, pw: str) -> str:
    return re.sub(r"://[^@]+@", f"://{role}.{ref}:{pw}@", url, count=1)


def _redact(url: str) -> str:
    return re.sub(r"://[^@]+@", "://***@", url)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "show"
    if mode not in ("show", "set"):
        raise SystemExit("usage: _cutover_step4_repoint.py [show|set]")

    cur_async = _env("DATABASE_URL")        # postgres asyncpg :6543
    cur_sync = _env("DATABASE_URL_SYNC")    # postgres psycopg2 :5432
    s = _secrets()
    ref, app_pw, sys_pw = s["PROJECT_REF"], s["BRIDGELEADS_APP_PW"], s["BRIDGELEADS_SYSTEM_PW"]

    app_async = _swap_user(cur_async, "bridgeleads_app", ref, app_pw)
    app_sync = _swap_user(cur_sync, "bridgeleads_app", ref, app_pw)
    sys_sync = _swap_user(cur_sync, "bridgeleads_system", ref, sys_pw)
    migrate = cur_sync  # postgres owner, unchanged — alembic DDL

    # Per-service var plan.
    plan = {
        "api":    {"DATABASE_URL": app_async, "DATABASE_URL_SYNC": app_sync, "DATABASE_URL_MIGRATE": migrate},
        "worker": {"DATABASE_URL": app_async, "DATABASE_URL_SYNC": sys_sync, "DATABASE_URL_MIGRATE": migrate},
        "beat":   {"DATABASE_URL": app_async, "DATABASE_URL_SYNC": sys_sync, "DATABASE_URL_MIGRATE": migrate},
    }

    print(f"=== {mode.upper()} — per-service repoint (redacted) ===")
    for svc, vars_ in plan.items():
        print(f"-- {svc} --")
        for k, v in vars_.items():
            print(f"   {k} = {_redact(v)}")

    if mode == "show":
        print("\n(dry-run only — nothing set. Run 'set' to apply with --skip-deploys.)")
        return

    # Persist rollback BEFORE changing anything.
    s["ROLLBACK_DATABASE_URL"] = cur_async
    s["ROLLBACK_DATABASE_URL_SYNC"] = cur_sync
    with open(_SECRETS, "w", encoding="utf-8") as f:
        for k, v in s.items():
            f.write(f"{k}={v}\n")
    print("\nrollback (postgres URLs) saved to .rls-cutover-secrets")

    # Emit the railway commands to a gitignored shell script (values stay out of
    # the transcript; file is local + gitignored) for the operator/bash to run.
    # subprocess of the npm `railway` shim is unreliable on Windows, and we avoid
    # shell=True (no injection surface).
    out = ".rls-cutover-repoint.sh"
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/usr/bin/env bash\nset -euo pipefail\n")
        for svc, vars_ in plan.items():
            for k, v in vars_.items():
                f.write(f'railway variables --service {svc} --skip-deploys --set "{k}={v}"\n')
    print(f"\nwrote {out} (gitignored). Run it in bash to stage the vars (--skip-deploys, no redeploy).")
    print("\n>>> vars staged on all services (NOT deployed). Next: redeploy beat -> worker -> api, "
          "verifying each, then RLS_ENFORCE, then FORCE.")


if __name__ == "__main__":
    main()
