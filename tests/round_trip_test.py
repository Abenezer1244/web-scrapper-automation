"""
Full production round-trip test.
Covers: register, login, auth edge cases, profile, connectors,
        scraper config CRUD, job create/get/cancel, billing, api-key, logout
"""
import uuid
import requests
import sys

BASE = "https://api.bridgeleads.io"
EMAIL = f"test_{uuid.uuid4().hex[:8]}@bridgeleads.io"
PASSWORD = "TestPass123!"

session = requests.Session()
passed = 0
failed = 0


def ok(label, detail=""):
    global passed
    passed += 1
    print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))


def fail(label, detail=""):
    global failed
    failed += 1
    print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


def run():
    token = None
    scraper_config_id = None
    job_id = None

    # ── 1. Health ─────────────────────────────────────────────────────────────
    print("\n[1] Health Check")
    r = session.get(f"{BASE}/health")
    if r.status_code == 200 and r.json().get("status") == "ok":
        ok("GET /health", str(r.json()))
    else:
        fail("GET /health", f"{r.status_code} {r.text}")

    # ── 2. Register ───────────────────────────────────────────────────────────
    print(f"\n[2] Register  ({EMAIL})")
    r = session.post(f"{BASE}/auth/register", json={"email": EMAIL, "password": PASSWORD})
    if r.status_code == 201:
        token = r.json().get("access_token")
        ok("POST /auth/register", "token received")
    else:
        fail("POST /auth/register", f"{r.status_code} {r.text}")
        sys.exit(1)

    auth = {"Authorization": f"Bearer {token}"}

    # ── 3. Login ──────────────────────────────────────────────────────────────
    print("\n[3] Login")
    r = session.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    if r.status_code == 200:
        login_token = r.json().get("access_token")
        ok("POST /auth/login", "token received")
        auth = {"Authorization": f"Bearer {login_token}"}
    else:
        fail("POST /auth/login", f"{r.status_code} {r.text}")

    # ── 4. Auth edge cases ────────────────────────────────────────────────────
    print("\n[4] Auth edge cases")
    r = session.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": "WrongPass999!"})
    if r.status_code == 401:
        ok("Wrong password -> 401")
    else:
        fail("Wrong password", f"expected 401 got {r.status_code}")

    r = session.get(f"{BASE}/auth/me")
    if r.status_code == 401:
        ok("No token -> 401")
    else:
        fail("No token", f"expected 401 got {r.status_code}")

    r = session.get(f"{BASE}/auth/me", headers={"Authorization": "Bearer fake.token.here"})
    if r.status_code == 401:
        ok("Fake token -> 401")
    else:
        fail("Fake token", f"expected 401 got {r.status_code}")

    # ── 5. Profile ────────────────────────────────────────────────────────────
    print("\n[5] Profile")
    r = session.get(f"{BASE}/auth/me", headers=auth)
    if r.status_code == 200:
        me = r.json()
        ok("GET /auth/me", f"id={me.get('id')} plan={me.get('plan')} records={me.get('records_used')}/{me.get('records_limit')}")
    else:
        fail("GET /auth/me", f"{r.status_code} {r.text}")

    # ── 6. Connectors (public) ────────────────────────────────────────────────
    print("\n[6] Connectors")
    r = session.get(f"{BASE}/scrapers/connectors")
    if r.status_code == 200 and len(r.json()) > 0:
        connectors = r.json()
        ok("GET /scrapers/connectors (public)", f"{len(connectors)} connector(s)")
        for c in connectors:
            print(f"         {c['county']}/{c['state']} types={c['record_types']} active={c['active']}")
    else:
        fail("GET /scrapers/connectors", f"{r.status_code} {r.text}")

    # ── 7. Create scraper config ──────────────────────────────────────────────
    print("\n[7] Scraper Configs")
    r = session.post(f"{BASE}/scrapers", json={
        "name": "Pierce County Probate - Test",
        "county": "pierce",
        "state": "WA",
        "record_type": "probate",
        "schedule": {"frequency": "manual"},
        "deliver": {"emails": [EMAIL], "format": "csv"},
    }, headers=auth)
    if r.status_code == 201:
        scraper_config_id = r.json().get("id")
        ok("POST /scrapers", f"id={scraper_config_id} county={r.json().get('county')}")
    else:
        fail("POST /scrapers", f"{r.status_code} {r.text}")

    # List scraper configs
    r = session.get(f"{BASE}/scrapers", headers=auth)
    if r.status_code == 200:
        ok("GET /scrapers", f"{len(r.json())} config(s)")
    else:
        fail("GET /scrapers", f"{r.status_code} {r.text}")

    # Get single config
    if scraper_config_id:
        r = session.get(f"{BASE}/scrapers/{scraper_config_id}", headers=auth)
        if r.status_code == 200:
            ok("GET /scrapers/{id}")
        else:
            fail("GET /scrapers/{id}", f"{r.status_code} {r.text}")

    # ── 8. Jobs ───────────────────────────────────────────────────────────────
    print("\n[8] Jobs")
    r = session.get(f"{BASE}/jobs", headers=auth)
    if r.status_code == 200:
        ok("GET /jobs (empty)", f"{len(r.json())} job(s)")
    else:
        fail("GET /jobs", f"{r.status_code} {r.text}")

    # Create job
    if scraper_config_id:
        r = session.post(f"{BASE}/jobs", json={
            "scraper_config_id": scraper_config_id,
            "trigger": "manual",
        }, headers=auth)
        if r.status_code == 201:
            job_id = r.json().get("id")
            ok("POST /jobs", f"id={job_id} status={r.json().get('status')}")
        else:
            fail("POST /jobs", f"{r.status_code} {r.text}")

    # Get job detail
    if job_id:
        r = session.get(f"{BASE}/jobs/{job_id}", headers=auth)
        if r.status_code == 200:
            ok("GET /jobs/{id}", f"status={r.json().get('status')}")
        else:
            fail("GET /jobs/{id}", f"{r.status_code} {r.text}")

        # Cancel job
        r = session.delete(f"{BASE}/jobs/{job_id}", headers=auth)
        if r.status_code == 204:
            ok("DELETE /jobs/{id} (cancel)")
        else:
            fail("DELETE /jobs/{id}", f"{r.status_code} {r.text}")

        # Confirm cancelled
        r = session.get(f"{BASE}/jobs/{job_id}", headers=auth)
        if r.status_code == 200 and r.json().get("status") == "cancelled":
            ok("Job status=cancelled confirmed")
        else:
            fail("Job cancel confirmation", f"status={r.json().get('status') if r.status_code == 200 else r.status_code}")

    # ── 9. Billing ────────────────────────────────────────────────────────────
    print("\n[9] Billing")
    r = session.get(f"{BASE}/billing/usage", headers=auth)
    if r.status_code == 200:
        u = r.json()
        ok("GET /billing/usage", f"plan={u.get('plan')} used={u.get('records_used')}/{u.get('records_limit')}")
    else:
        fail("GET /billing/usage", f"{r.status_code} {r.text}")

    r = session.get(f"{BASE}/billing/plans")
    if r.status_code == 200:
        ok("GET /billing/plans (public)", f"{len(r.json())} plans")
    else:
        fail("GET /billing/plans", f"{r.status_code} {r.text}")

    # ── 10. API key (starter -> 403) ──────────────────────────────────────────
    print("\n[10] API key")
    r = session.post(f"{BASE}/auth/api-key", headers=auth)
    if r.status_code == 403:
        ok("POST /auth/api-key (starter) -> 403 as expected")
    elif r.status_code == 201:
        ok("POST /auth/api-key", "key issued")
    else:
        fail("POST /auth/api-key", f"{r.status_code} {r.text}")

    # ── 11. Delete scraper config ─────────────────────────────────────────────
    print("\n[11] Cleanup")
    if scraper_config_id:
        r = session.delete(f"{BASE}/scrapers/{scraper_config_id}", headers=auth)
        if r.status_code == 204:
            ok("DELETE /scrapers/{id} (soft delete)")
        else:
            fail("DELETE /scrapers/{id}", f"{r.status_code} {r.text}")

    # ── 12. Logout ────────────────────────────────────────────────────────────
    print("\n[12] Logout")
    r = session.post(f"{BASE}/auth/logout", headers=auth)
    if r.status_code == 204:
        ok("POST /auth/logout -> 204")
    else:
        fail("POST /auth/logout", f"{r.status_code} {r.text}")

    r = session.get(f"{BASE}/auth/me", headers=auth)
    if r.status_code == 401:
        ok("Token blacklisted after logout -> 401")
    else:
        fail("Token blacklist", f"expected 401 got {r.status_code}")

    # ── Summary ───────────────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'='*52}")
    print(f"  {passed}/{total} passed  |  {failed} failed")
    print("  ALL PASS - Production is healthy" if failed == 0 else "  FAILURES detected - see above")
    print(f"{'='*52}\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run()
