"""UI-driven live county audit — drive the real bridgeleads.io UI in a VISIBLE
Chromium window (Playwright), one county at a time.

This logs into the actual BridgeLeads web app, then for each healthy county +
record type it walks the real "New Scraper" wizard (select state -> county ->
record type -> Continue x3 -> Test run), which creates the scraper, triggers a
real job, and lands on the live `/live/[id]` monitor page. You watch the whole
thing happen in the browser. Completion + record counts are read from the API
(same admin auth) for precise, non-flaky detection.

The scraping engine itself runs server-side on the SaaS (Railway, headed
Chromium via Xvfb) — this drives the product UI that starts and watches it.

Usage:
    python scripts/ui_county_audit.py                  # all healthy combos
    python scripts/ui_county_audit.py --county pierce
    python scripts/ui_county_audit.py --county king --record-type probate
    python scripts/ui_county_audit.py --headless       # CI / no window
"""

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright

from _creds import admin_creds

WEB = "https://bridgeleads.io"
API = "https://api.bridgeleads.io"
EMAIL, PASSWORD = admin_creds()

RECORD_TYPE_LABELS = {
    "probate": "Probate",
    "death_certificate": "Death Certificate",
    "pre_foreclosure": "Pre-Foreclosure",
    "tax_delinquent": "Tax Delinquent",
    "divorce": "Divorce",
    "code_violation": "Code Violation",
    "eviction": "Eviction",
}

JOB_TIMEOUT = 1500      # max seconds to watch a single job (slow probate counties)
POLL_EVERY = 6          # seconds between API status polls
OUT_DIR = Path(__file__).resolve().parent / "audit_out_ui"
SHOT_DIR = OUT_DIR / "screenshots"
FIELDS = ("party_name", "parcel_id", "property_address", "mailing_address", "date_recorded", "heirs")


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


# ─── API helpers (matrix + job polling, separate from the UI session) ──────────

_TOKEN = {"v": None}  # API JWT for polling; refreshed on 401 over long runs.


def api_token() -> str:
    r = requests.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def _auth_get(url, **kw):
    """GET with the polling token; transparently re-login once on 401."""
    if _TOKEN["v"] is None:
        _TOKEN["v"] = api_token()
    headers = {"Authorization": f"Bearer {_TOKEN['v']}"}
    r = requests.get(url, headers=headers, **kw)
    if r.status_code == 401:
        _TOKEN["v"] = api_token()
        headers = {"Authorization": f"Bearer {_TOKEN['v']}"}
        r = requests.get(url, headers=headers, **kw)
    return r


def healthy_matrix(county_filter=None, rt_filter=None):
    """Healthy connectors only (degraded counties are not clickable in the UI)."""
    r = _auth_get(f"{API}/scrapers/connectors", timeout=20)
    r.raise_for_status()
    merged = {}
    for c in r.json():
        if not c.get("active") or c.get("health_status") != "healthy":
            continue
        county = c["county"].lower()
        merged.setdefault(county, {"county": county, "state": c["state"].lower(), "types": []})
        for rt in c.get("record_types") or []:
            if rt not in merged[county]["types"]:
                merged[county]["types"].append(rt)
    combos = []
    for county in sorted(merged):
        if county_filter and county != county_filter.lower():
            continue
        for rt in merged[county]["types"]:
            if rt_filter and rt != rt_filter.lower():
                continue
            combos.append((county, merged[county]["state"], rt))
    return combos


def poll_job(job_id: str) -> dict:
    deadline = time.time() + JOB_TIMEOUT
    last = ""
    while time.time() < deadline:
        try:
            r = _auth_get(f"{API}/jobs/{job_id}", timeout=15)
            if r.status_code == 200:
                job = r.json()
                line = f"{job.get('progress_label', job.get('status'))} | {job.get('record_count', 0)} rec"
                if line != last:
                    print(f"      [{datetime.now():%H:%M:%S}] {line}")
                    last = line
                if job.get("status") in ("done", "failed", "cancelled"):
                    return job
        except Exception:
            pass
        time.sleep(POLL_EVERY)
    return {"status": "timeout", "record_count": 0, "id": job_id}


def coverage(job_id: str) -> tuple[int, dict, list]:
    """Return (scraped_total, field_coverage, samples).

    scraped_total comes from results `total` and INCLUDES deduplicated records
    (is_duplicate=True) — a re-scrape of an already-stored window has
    record_count(new)=0 but total>0, which still proves the scraper works.
    """
    r = _auth_get(f"{API}/jobs/{job_id}/results",
                  params={"page": 1, "page_size": 200}, timeout=20)
    if r.status_code != 200:
        return -1, {}, []  # sentinel: results unavailable (not a real zero)
    data = r.json()
    total = data.get("total", 0)
    items = data.get("items", [])
    n = len(items)
    cov = {f: {"count": sum(1 for x in items if x.get(f)),
               "pct": (sum(1 for x in items if x.get(f)) * 100 // n) if n else 0}
           for f in FIELDS}
    return total, cov, items[:3]


# ─── Report ────────────────────────────────────────────────────────────────────

def write_report(results: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    def cov(r, f):
        return r.get("coverage", {}).get(f, {}).get("pct", "")

    lines = [
        "# UI-Driven Live County Audit (real bridgeleads.io wizard + /live)",
        "",
        f"Window: rolling_90 (~3 months) | Updated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        "| County | Type | Verdict | Job | Scraped | New | name% | parcel% | propAddr% | mailAddr% | date% | Dur(s) | Note |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    npass = nempty = nfail = 0
    for r in results:
        st = r["status"]
        if st == "done" and r.get("record_count", 0) > 0:
            v = "PASS"; npass += 1
        elif st == "done":
            v = "EMPTY"; nempty += 1
        else:
            v = st.upper(); nfail += 1
        lines.append(
            f"| {r['county']} | {r['record_type']} | {v} | {str(r.get('job_id') or '')[:8]} | "
            f"{r.get('record_count', 0)} | {r.get('new_count', 0)} | {cov(r,'party_name')} | {cov(r,'parcel_id')} | "
            f"{cov(r,'property_address')} | {cov(r,'mailing_address')} | {cov(r,'date_recorded')} | "
            f"{r.get('duration_s','')} | {(r.get('note') or '')[:60]} |"
        )
    lines += ["", f"**PASS={npass}  EMPTY={nempty}  FAIL={nfail}  TOTAL={len(results)}**"]
    (OUT_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")


# ─── UI driving ────────────────────────────────────────────────────────────────

async def ui_login(page) -> None:
    print("  Logging in via the UI...")
    await page.goto(f"{WEB}/login", wait_until="domcontentloaded")
    await page.fill("#email", EMAIL)
    await page.fill("#password", PASSWORD)
    await page.click('button[type="submit"]')
    await page.wait_for_url("**/dashboard", timeout=30000)
    print(f"  Logged in -> {page.url}")


async def ensure_new_scraper_page(page) -> None:
    """Open /scrapers/new with the state <select> present, re-logging in if the
    NextAuth session dropped mid-run (which redirects to /login)."""
    for _ in range(3):
        await page.goto(f"{WEB}/scrapers/new", wait_until="domcontentloaded")
        await page.wait_for_timeout(700)
        if "/login" in page.url or "/scrapers/new" not in page.url:
            await ui_login(page)
            continue
        try:
            await page.locator("select").first.wait_for(state="visible", timeout=10000)
            # Confirm it's the state picker (has a WA option), not some other select.
            if await page.locator('select option[value="WA"]').count() > 0:
                return
        except Exception:
            if "/login" in page.url:
                await ui_login(page)
                continue
        await page.wait_for_timeout(1500)  # transient render delay; retry
    # last try — surface the real error if the state picker truly never appears
    await page.locator('select option[value="WA"]').first.wait_for(state="attached", timeout=10000)


async def run_combo_ui(page, county, state, record_type) -> dict:
    label = RECORD_TYPE_LABELS.get(record_type, record_type)
    cap = _cap(county)
    print(f"\n{'='*60}\n  UI: {cap} County / {label}\n{'='*60}")
    res = {"county": county, "state": state, "record_type": record_type,
           "status": "error", "record_count": 0, "new_count": 0,
           "coverage": {}, "samples": [],
           "job_id": None, "note": None, "duration_s": 0,
           "ran_at": datetime.now().isoformat(timespec="seconds")}
    t0 = time.time()
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await ensure_new_scraper_page(page)
        # 1) state
        await page.locator("select").first.select_option("WA")
        # 2) county (only healthy ones are enabled)
        county_btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(cap)} County")).first
        await county_btn.wait_for(state="visible", timeout=20000)
        await county_btn.click()
        # 3) record-type chip
        chip = page.get_by_role("button", name=label, exact=True).first
        await chip.wait_for(state="visible", timeout=10000)
        await chip.click()
        # 4) Continue x3 (defaults: all fields, property enrichment, rolling_90)
        for _ in range(3):
            cont = page.get_by_role("button", name=re.compile(r"^Continue")).first
            await cont.wait_for(state="visible", timeout=10000)
            await cont.click()
            await page.wait_for_timeout(500)
        # 5) Test run -> creates scraper + job, redirects to /live/[id]
        await page.get_by_role("button", name=re.compile(r"Test run", re.I)).first.click()
        await page.wait_for_url("**/live/**", timeout=45000)
        m = re.search(r"/live/([0-9a-fA-F-]{8,})", page.url)
        if not m:
            raise RuntimeError(f"no job id in live URL: {page.url}")
        job_id = m.group(1)
        res["job_id"] = job_id
        print(f"  Live page: {page.url}")
        await page.screenshot(path=str(SHOT_DIR / f"{county}_{record_type}_live.png"))

        # 6) poll API for completion while the live page is on screen
        job = poll_job(job_id)
        res["status"] = job.get("status", "unknown")
        res["new_count"] = job.get("record_count", 0)
        res["note"] = job.get("error_message")
        if res["status"] == "done":
            total, cov, samples = coverage(job_id)
            if total < 0:
                # results endpoint failed — don't mislabel a real job as EMPTY
                res["record_count"] = res["new_count"]
                res["note"] = "results fetch failed; used job record_count"
            else:
                res["record_count"] = total  # scraped total (incl. dedup) = real signal
                res["coverage"], res["samples"] = cov, samples
                if total > 0 and res["new_count"] == 0:
                    res["note"] = "all records were duplicates (already stored) — scraper OK"
        try:
            await page.wait_for_timeout(1500)
            await page.screenshot(path=str(SHOT_DIR / f"{county}_{record_type}_final.png"))
        except Exception:
            pass
    except Exception as exc:
        res["status"] = "error"
        res["note"] = repr(exc)[:300]
        try:
            await page.screenshot(path=str(SHOT_DIR / f"{county}_{record_type}_ERROR.png"))
        except Exception:
            pass

    res["duration_s"] = round(time.time() - t0, 1)
    icon = "OK" if (res["status"] == "done" and res["record_count"] > 0) else (
        "EMPTY" if res["status"] == "done" else "FAIL")
    print(f"  [{icon}] {res['status']} — {res['record_count']} records ({res['duration_s']}s)")
    if res["note"]:
        print(f"  note: {res['note'][:200]}")
    return res


async def main_async(args):
    _TOKEN["v"] = api_token()
    matrix = healthy_matrix(args.county, args.record_type)
    if not matrix:
        sys.exit("No healthy combos match (degraded counties aren't runnable in the UI).")

    # Resume: keep combos that previously completed cleanly (status 'done',
    # i.e. ran through the harness without a session-drop error), re-run the rest.
    results = []
    if args.resume and (OUT_DIR / "summary.json").exists():
        prev = json.loads((OUT_DIR / "summary.json").read_text(encoding="utf-8"))
        latest = {}  # latest row per (county, record_type)
        for p in prev:
            latest[(p["county"], p["record_type"])] = p
        if args.county or args.record_type:
            # Targeted re-run: re-run exactly the filtered matrix, keep EVERY
            # other prior row (incl. failures we're not touching) untouched.
            rerun = {(m[0], m[2]) for m in matrix}
            results = [v for k, v in latest.items() if k not in rerun]
            print(f"  [resume] targeted: re-running {len(matrix)}, keeping {len(results)} others")
        else:
            # Full resume: keep clean 'done' combos, re-run anything that didn't.
            good = {k for k, v in latest.items() if v.get("status") == "done"}
            results = [latest[k] for k in good]
            matrix = [m for m in matrix if (m[0], m[2]) not in good]
            print(f"  [resume] keeping {len(good)} clean combos, re-running {len(matrix)}")

    if not matrix:
        write_report(results)
        sys.exit("Nothing to run — all combos already completed cleanly.")

    print(f"\n{'#'*60}\n# UI-DRIVEN LIVE AUDIT — {len(matrix)} combos (healthy counties)\n{'#'*60}")
    for c, s, rt in matrix:
        print(f"  - {c}/{rt}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless, slow_mo=0 if args.headless else 120)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        try:
            await ui_login(page)
            for i, (county, state, rt) in enumerate(matrix, 1):
                print(f"\n[{i}/{len(matrix)}]", end="")
                results.append(await run_combo_ui(page, county, state, rt))
                write_report(results)
        finally:
            await page.wait_for_timeout(2000)
            await browser.close()

    print(f"\nDONE. Report: {OUT_DIR / 'summary.md'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--county")
    ap.add_argument("--record-type")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="Keep prior clean ('done') combos; re-run only failed/unrun ones")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
