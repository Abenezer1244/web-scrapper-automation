"""Live Chromium UI test: verify the tax-delinquent Date-column change in prod.

Logs into app.bridgeleads.io, opens a tax-delinquent job's results and a non-tax
job's results, and asserts:
  - tax job: "Date" column cells render the em-dash (—); "Tax Balance Owed" +
    "Oldest Tax Year" headers are present.
  - non-tax job: "Date" column shows a real date (proves the gating didn't
    over-apply).

Credentials come from env (BL_EMAIL / BL_PASS). Screenshots land in scripts/ui_out/.
This drives the REAL production site — read-only navigation, no data mutation.
"""
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE = "https://app.bridgeleads.io"
EMAIL = os.environ["BL_EMAIL"]
PASS = os.environ["BL_PASS"]
OUT = Path(__file__).resolve().parent / "ui_out"
OUT.mkdir(exist_ok=True)
EMDASH = "—"


def shot(page, name):
    p = OUT / f"{name}.png"
    page.screenshot(path=str(p), full_page=True)
    print(f"  [shot] {p.name}")


def date_col_index(page):
    """0-based index of the 'Date' header in the results detail table."""
    heads = page.locator("table thead th")
    n = heads.count()
    for i in range(n):
        if (heads.nth(i).inner_text() or "").strip().lower() == "date":
            return i
    return -1


def column_cells(page, col_idx, max_rows=8):
    """First text line of the given column for the first max_rows body rows.

    The Date cell renders the date (or em-dash) followed by a New/Old freshness
    badge on its own line, so we compare against the first line only.
    """
    out = []
    rows = page.locator("table tbody tr")
    for r in range(min(rows.count(), max_rows)):
        cells = rows.nth(r).locator("td")
        if cells.count() > col_idx:
            raw = (cells.nth(col_idx).inner_text() or "").strip()
            out.append(raw.split("\n")[0].strip())
    return out


def main():
    results = {"login": "?", "tax": "?", "nontax": "?"}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=250)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        # ---- 1. Login ---------------------------------------------------
        print("[1] Login")
        page.goto(f"{BASE}/login", wait_until="networkidle", timeout=60_000)
        page.fill("#email", EMAIL)
        page.fill("#password", PASS)
        shot(page, "01_login_filled")
        page.click("button[type=submit]")

        # Wait for one of: dashboard, MFA code step, or an error alert.
        try:
            page.wait_for_function(
                """() => location.pathname.startsWith('/dashboard')
                       || !!document.querySelector('input[autocomplete=\"one-time-code\"], #totp-code, [data-input-otp]')
                       || !!document.querySelector('[role=alert]')""",
                timeout=30_000,
            )
        except PWTimeout:
            pass
        time.sleep(1.5)

        if page.locator("[data-input-otp], #totp-code, input[autocomplete='one-time-code']").count() > 0:
            shot(page, "02_mfa_blocked")
            results["login"] = "BLOCKED_MFA"
            print("  MFA code step reached — automated login cannot continue without a TOTP.")
            _summary(results)
            browser.close()
            sys.exit(2)

        if "/dashboard" not in page.url and page.locator("[role=alert]").count() > 0:
            err = page.locator("[role=alert]").first.inner_text()
            shot(page, "02_login_error")
            results["login"] = f"ERROR: {err}"
            _summary(results)
            browser.close()
            sys.exit(2)

        # Ensure we're authenticated.
        if "/dashboard" not in page.url:
            page.goto(f"{BASE}/dashboard", wait_until="networkidle", timeout=60_000)
        results["login"] = "OK" if "/dashboard" in page.url else f"UNKNOWN ({page.url})"
        print(f"  login -> {results['login']}")
        shot(page, "03_dashboard")

        # ---- 2. Results list: classify jobs by record type --------------
        print("[2] Results list")
        page.goto(f"{BASE}/results", wait_until="networkidle", timeout=60_000)
        time.sleep(2)
        shot(page, "04_results_list")

        rows = page.locator("table tbody tr")
        tax_href = None
        nontax_href = None
        nontax_label = None
        for r in range(rows.count()):
            row = rows.nth(r)
            txt = (row.inner_text() or "").lower()
            link = row.locator("a[href^='/results/']").first
            if link.count() == 0:
                continue
            href = link.get_attribute("href")
            if "tax delinquent" in txt and tax_href is None:
                tax_href = href
            elif "tax delinquent" not in txt and nontax_href is None:
                nontax_href = href
                nontax_label = txt.split("\n")[0]
        print(f"  tax job: {tax_href}")
        print(f"  non-tax job: {nontax_href} ({nontax_label})")

        # ---- 3. Tax job detail ------------------------------------------
        if tax_href:
            print("[3] Tax-delinquent results")
            page.goto(f"{BASE}{tax_href}", wait_until="networkidle", timeout=60_000)
            page.wait_for_selector("table tbody tr", timeout=30_000)
            time.sleep(1.5)
            shot(page, "05_tax_detail")
            body = page.inner_text("body").lower()  # headers render via CSS uppercase
            has_tax_headers = ("tax balance owed" in body) and ("oldest tax year" in body)
            di = date_col_index(page)
            cells = column_cells(page, di) if di >= 0 else []
            all_emdash = bool(cells) and all(c == EMDASH for c in cells)
            print(f"  tax headers present: {has_tax_headers}")
            print(f"  Date col index: {di}; first cells: {cells[:6]}")
            results["tax"] = (
                "PASS" if (has_tax_headers and all_emdash)
                else f"FAIL (headers={has_tax_headers}, date_cells={cells[:6]})"
            )
        else:
            results["tax"] = "SKIP (no tax-delinquent job found)"

        # ---- 4. Non-tax job detail --------------------------------------
        if nontax_href:
            print("[4] Non-tax results")
            page.goto(f"{BASE}{nontax_href}", wait_until="networkidle", timeout=60_000)
            page.wait_for_selector("table tbody tr", timeout=30_000)
            time.sleep(1.5)
            shot(page, "06_nontax_detail")
            body = page.inner_text("body").lower()
            no_tax_headers = ("tax balance owed" not in body)
            di = date_col_index(page)
            cells = column_cells(page, di) if di >= 0 else []
            # A real date renders via formatDate -> contains a digit; em-dash does not.
            has_real_date = any(re.search(r"\d", c) for c in cells)
            none_emdash_only = not (cells and all(c == EMDASH for c in cells))
            print(f"  Date col index: {di}; first cells: {cells[:6]}")
            results["nontax"] = (
                "PASS" if (has_real_date and none_emdash_only and no_tax_headers)
                else f"FAIL (date_cells={cells[:6]}, tax_headers_absent={no_tax_headers})"
            )
        else:
            results["nontax"] = "SKIP (no non-tax job found)"

        time.sleep(1)
        browser.close()

    _summary(results)
    bad = [k for k, v in results.items() if not str(v).startswith(("OK", "PASS", "SKIP"))]
    sys.exit(1 if bad else 0)


def _summary(results):
    print("\n==================== UI TEST SUMMARY ====================")
    for k, v in results.items():
        print(f"  {k:8} : {v}")
    print(f"  screenshots in: {OUT}")
    print("========================================================")


if __name__ == "__main__":
    main()
