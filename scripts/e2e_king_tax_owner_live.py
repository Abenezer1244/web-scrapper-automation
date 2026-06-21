"""LIVE headed-Chromium verification of the King tax-delinquent owner-name fix (PR #80).

Confirms, against PRODUCTION, that King tax_delinquent leads now carry the REAL
owner name (from eRealProperty enrichment) instead of the
"Tax Delinquent — $X owed (Parcel …)" placeholder.

Flow:
  1. API login as admin (handles MFA gate loudly).
  2. Create a King WA tax_delinquent scraper config (property lookup ON).
  3. Trigger the job.
  4. Open a VISIBLE Chromium, log in through the real UI, sit on the live job page.
  5. Poll the API until the job is terminal.
  6. Pull /jobs/{id}/results and classify every party_name: real owner vs placeholder.
  7. Screenshot the UI results and print the verdict.

Run:
  BRIDGELEADS_ADMIN_PASSWORD=... python scripts/e2e_king_tax_owner_live.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests as req

from src.scrapers.king_wa_tax_delinquent import is_tax_placeholder_party

API = os.getenv("BRIDGELEADS_API", "https://api.bridgeleads.io")
APP = os.getenv("BRIDGELEADS_APP", "https://app.bridgeleads.io")
EMAIL = os.getenv("BRIDGELEADS_ADMIN_EMAIL", "admin@bridgeleads.io")
PASSWORD = os.environ["BRIDGELEADS_ADMIN_PASSWORD"]  # fail loud if unset


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def api_login() -> str:
    r = req.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=20)
    if r.status_code != 200:
        sys.exit(f"LOGIN FAILED {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("mfa_required"):
        sys.exit(
            "LOGIN needs MFA — this admin has 2FA enabled. Re-run with a TOTP code "
            "(the script would need /auth/login/mfa). Paste a current code if you want me to wire it in."
        )
    print(f"  API login OK ({EMAIL})")
    return data["access_token"]


def create_config(tok: str) -> str:
    body = {
        "name": f"King Tax-Delinquent OWNER VERIFY {int(time.time())}",
        "county": "king", "state": "wa", "record_type": "tax_delinquent",
        "fields": {"party_name": True, "parcel_id": True, "property_address": True,
                   "mailing_address": True, "heirs": False, "legal_description": True,
                   "date_recorded": True},
        "enrichment": {"property_lookup": True, "skip_tracing": False},
        "schedule": {"frequency": "manual", "date_range_mode": "custom",
                     "date_from": "2025-01-01", "date_to": "2026-06-21"},
        "deliver": {"emails": [], "formats": ["csv"]},
    }
    r = req.post(f"{API}/scrapers", headers=_hdr(tok), json=body, timeout=30)
    if r.status_code != 201:
        sys.exit(f"CREATE CONFIG FAILED {r.status_code}: {r.text[:300]}")
    cid = r.json()["id"]
    print(f"  Config created: {cid}")
    return cid


def trigger_job(tok: str, cid: str) -> str:
    r = req.post(f"{API}/jobs", headers=_hdr(tok), json={"scraper_config_id": cid, "trigger": "manual"}, timeout=30)
    if r.status_code != 201:
        sys.exit(f"TRIGGER FAILED {r.status_code}: {r.text[:300]}")
    jid = r.json()["id"]
    print(f"  Job triggered: {jid}")
    return jid


def poll_job(tok: str, jid: str, timeout_s: int = 1500) -> dict:
    terminal = {"done", "failed", "cancelled"}
    start = time.time()
    last = None
    while time.time() - start < timeout_s:
        r = req.get(f"{API}/jobs/{jid}", headers=_hdr(tok), timeout=20)
        if r.status_code == 200:
            j = r.json()
            st = j.get("status")
            if st != last:
                print(f"  [{int(time.time()-start):>4}s] status={st}  records={j.get('record_count')}")
                last = st
            if st in terminal:
                return j
        time.sleep(15)
    sys.exit(f"TIMEOUT after {timeout_s}s waiting for job {jid}")


def fetch_results(tok: str, jid: str) -> list[dict]:
    rows, page, per = [], 1, 200
    while True:
        r = req.get(f"{API}/jobs/{jid}/results", headers=_hdr(tok),
                    params={"page": page, "per_page": per}, timeout=30)
        if r.status_code != 200:
            sys.exit(f"RESULTS FETCH FAILED {r.status_code}: {r.text[:200]}")
        data = r.json()
        batch = data.get("results") or data.get("items") or data.get("records") or []
        rows.extend(batch)
        total = data.get("total")
        if not batch or (total is not None and len(rows) >= total) or len(batch) < per:
            break
        page += 1
    return rows


async def drive_ui(jid: str):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=120)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        print("\n[UI] Opening app + logging in (visible browser)...")
        await page.goto(f"{APP}/login", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
        try:
            await page.locator('input[type="email"], input[name="email"]').first.fill(EMAIL)
            await page.locator('input[type="password"]').first.fill(PASSWORD)
            await page.locator('button[type="submit"]').first.click()
            await page.wait_for_timeout(6000)
        except Exception as e:
            print(f"[UI] login interaction note: {e}")
        print(f"[UI] now at: {page.url}")
        await page.screenshot(path="king_tax_owner_01_dashboard.png")
        # Sit on the live job page so the run is visible.
        for path in (f"{APP}/jobs/{jid}", f"{APP}/scrapers"):
            try:
                await page.goto(path, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3000)
                print(f"[UI] viewing: {page.url}")
                break
            except Exception:
                continue
        await page.screenshot(path="king_tax_owner_02_job.png")
        return browser, page


def classify(rows: list[dict]) -> dict:
    placeholders, owners = [], []
    for x in rows:
        pn = x.get("party_name")
        (placeholders if is_tax_placeholder_party(pn) else owners).append(pn)
    return {"total": len(rows), "owners": owners, "placeholders": placeholders}


async def main():
    print("=" * 64)
    print("LIVE VERIFY — King tax_delinquent owner name (PR #80, prod)")
    print("=" * 64)
    tok = api_login()
    cid = create_config(tok)
    jid = trigger_job(tok, cid)

    browser, page = await drive_ui(jid)
    try:
        print("\n[POLL] waiting for job to finish (scrape + enrichment)...")
        job = poll_job(tok, jid)
        print(f"\n[POLL] terminal status: {job.get('status')}  record_count={job.get('record_count')}")

        rows = fetch_results(tok, jid)
        c = classify(rows)
        print("\n" + "=" * 64)
        print("RESULT")
        print("=" * 64)
        print(f"  records returned : {c['total']}")
        print(f"  REAL owner names : {len(c['owners'])}")
        print(f"  still placeholder: {len(c['placeholders'])}  (capped/missed enrichment)")
        print("\n  sample REAL owner names (proof the fix is live):")
        for nm in c["owners"][:12]:
            print(f"    • {nm}")
        if c["placeholders"]:
            print("\n  sample remaining placeholders:")
            for nm in c["placeholders"][:3]:
                print(f"    • {nm}")

        try:
            await page.goto(f"{APP}/jobs/{jid}/results", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3500)
        except Exception:
            pass
        await page.screenshot(path="king_tax_owner_03_results.png", full_page=True)
        print("\n  screenshots: king_tax_owner_01_dashboard.png / _02_job.png / _03_results.png")

        verdict = "PASS — real owner names present" if c["owners"] else "FAIL — no real owner names"
        print(f"\nVERDICT: {verdict}")
    finally:
        # The visible browser is only for proof — never let a closed window
        # (manual close / Ctrl-C) mask the API-side verdict above.
        try:
            await page.wait_for_timeout(4000)
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
