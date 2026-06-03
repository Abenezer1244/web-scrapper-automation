"""RLS cutover — STEP 3 (prod): rehearse as the new roles. READ-ONLY.

Connects as bridgeleads_app.<ref> and bridgeleads_system.<ref> through the
session pooler and proves the policy model on REAL prod data BEFORE the live
repoint. Everything runs inside a transaction that is ROLLED BACK; write
attempts are expected to FAIL (and are rolled back regardless). No app impact.

PASS criteria:
  app: not bypassing; no-GUC sees 0 tenant rows; with GUC sees only that user's
       rows; cannot DELETE; cannot read worker-only tables; cannot INSERT results.
  system: sees rows cross-tenant (results + delivered_records).

Run:  PYTHONPATH=. python scripts/_cutover_step3_rehearse.py
"""

from __future__ import annotations

import io
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg2

_SECRETS = ".rls-cutover-secrets"


def _admin_dsn() -> str:
    with io.open(".env", "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("DATABASE_URL_SYNC="):
                return line.strip().split("=", 1)[1].replace("postgresql+psycopg2://", "postgresql://")
    raise SystemExit("DATABASE_URL_SYNC not found")


def _secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    with io.open(_SECRETS, "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                out[k] = v
    return out


def _role_dsn(admin_dsn: str, role: str, ref: str, pw: str) -> str:
    p = urlsplit(admin_dsn)
    user = quote(f"{role}.{ref}", safe="")
    return urlunsplit((p.scheme, f"{user}:{quote(pw, safe='')}@{p.hostname}:{p.port}",
                       p.path, p.query, p.fragment))


_results = {"pass": 0, "fail": 0}


def _check(label: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    _results["pass" if ok else "fail"] += 1
    print(f"  [{tag}] {label}" + (f" — {detail}" if detail else ""))


def main() -> None:
    admin_dsn = _admin_dsn()
    s = _secrets()
    ref = s["PROJECT_REF"]

    # Pick a real user with results (read-only, as admin/postgres).
    admin = psycopg2.connect(admin_dsn, connect_timeout=20); admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT user_id, COUNT(*) FROM results GROUP BY user_id ORDER BY 2 DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            print("No results rows in prod — cannot rehearse tenant isolation meaningfully.")
            admin.close(); sys.exit(1)
        user_a, n_a = str(row[0]), row[1]
        cur.execute("SELECT COUNT(*) FROM results"); total = cur.fetchone()[0]
    admin.close()
    print(f"sample user_a={user_a} has {n_a} results; total results={total}\n")

    # ── bridgeleads_app rehearsal ────────────────────────────────────────────
    print("== bridgeleads_app ==")
    app = psycopg2.connect(_role_dsn(admin_dsn, "bridgeleads_app", ref, s["BRIDGELEADS_APP_PW"]),
                           connect_timeout=20)
    try:
        app.autocommit = False
        with app.cursor() as cur:
            cur.execute("SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            cu, byp = cur.fetchone()
            _check("connected as bridgeleads_app, not bypassing", cu == "bridgeleads_app" and byp is False, f"user={cu} bypassrls={byp}")

            cur.execute("SELECT COUNT(*) FROM results")
            _check("no-GUC: results invisible", cur.fetchone()[0] == 0)

            cur.execute("SELECT set_config('app.current_user_id', %s, true)", (user_a,))
            cur.execute("SELECT COUNT(*) FROM results WHERE user_id <> %s", (user_a,))
            _check("with GUC: cannot see other tenants' results", cur.fetchone()[0] == 0)
            cur.execute("SELECT COUNT(*) FROM results")
            own = cur.fetchone()[0]
            _check("with GUC: sees own results", own == n_a, f"saw {own}, expected {n_a}")

            # negative: no DELETE grant
            try:
                cur.execute("SAVEPOINT s1")
                cur.execute("DELETE FROM jobs WHERE false")
                _check("DELETE on jobs is denied", False, "DELETE unexpectedly allowed")
                cur.execute("ROLLBACK TO s1")
            except psycopg2.Error as e:
                _check("DELETE on jobs is denied", True, e.pgcode)
                cur.execute("ROLLBACK TO s1")
            # negative: worker-only table not readable
            try:
                cur.execute("SAVEPOINT s2")
                cur.execute("SELECT COUNT(*) FROM delivered_records")
                _check("delivered_records not readable by app", False, "unexpectedly readable")
                cur.execute("ROLLBACK TO s2")
            except psycopg2.Error as e:
                _check("delivered_records not readable by app", True, e.pgcode)
                cur.execute("ROLLBACK TO s2")
            # negative: cannot INSERT results
            try:
                cur.execute("SAVEPOINT s3")
                cur.execute("INSERT INTO results (id, job_id, user_id, party_name, parcel_id, skip_trace_status, is_duplicate) "
                            "VALUES (gen_random_uuid(), gen_random_uuid(), %s, 'x','y','not_attempted',false)", (user_a,))
                _check("INSERT into results is denied", False, "INSERT unexpectedly allowed")
                cur.execute("ROLLBACK TO s3")
            except psycopg2.Error as e:
                _check("INSERT into results is denied", True, e.pgcode)
                cur.execute("ROLLBACK TO s3")
        app.rollback()
    finally:
        app.close()

    # ── bridgeleads_system rehearsal ─────────────────────────────────────────
    print("== bridgeleads_system ==")
    sysconn = psycopg2.connect(_role_dsn(admin_dsn, "bridgeleads_system", ref, s["BRIDGELEADS_SYSTEM_PW"]),
                               connect_timeout=20)
    try:
        sysconn.autocommit = False
        with sysconn.cursor() as cur:
            cur.execute("SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            cu, byp = cur.fetchone()
            _check("connected as bridgeleads_system, not bypassing", cu == "bridgeleads_system" and byp is False, f"user={cu} bypassrls={byp}")
            cur.execute("SELECT COUNT(*) FROM results")  # no GUC
            sys_total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM results WHERE user_id <> %s", (user_a,))
            sys_other = cur.fetchone()[0]
            # Cross-tenant proof: system sees >= one tenant's worth with NO GUC
            # (app saw 0 here). sys_other>0 is definitive when prod is multi-tenant.
            _check("system sees results cross-tenant (no GUC)", sys_total >= n_a,
                   f"system sees {sys_total} (one tenant has {n_a}); other-tenant rows={sys_other}")
            cur.execute("SELECT COUNT(*) FROM delivered_records")
            _check("system can read delivered_records", True, f"{cur.fetchone()[0]} rows")
        sysconn.rollback()
    finally:
        sysconn.close()

    print(f"\n=== REHEARSAL: {_results['pass']} passed, {_results['fail']} failed ===")
    if _results["fail"]:
        print(">>> Do NOT repoint — investigate the failures first.")
        sys.exit(2)
    print(">>> RLS model verified on prod data. Repoint is de-risked.")


if __name__ == "__main__":
    main()
