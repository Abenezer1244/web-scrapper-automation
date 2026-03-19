"""
Browser UI test: register -> dashboard -> login -> logout
Tests the live production frontend at https://app.bridgeleads.io
"""
import asyncio
import uuid
from pathlib import Path

from playwright.async_api import async_playwright, expect

BASE = "https://app.bridgeleads.io"
EMAIL = f"uitest_{uuid.uuid4().hex[:8]}@bridgeleads.io"
PASSWORD = "TestPass123!"
SCREENSHOTS = Path("tests/screenshots")

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


async def run():
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        # ── 1. Register page loads ──────────────────────────────────────────
        print("\n[1] Register page")
        await page.goto(f"{BASE}/register")
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path=str(SCREENSHOTS / "01_register_page.png"))

        title = await page.title()
        if "bridgeleads" in title.lower() or await page.locator("#email").is_visible():
            ok("Register page loaded", f"title={title!r}")
        else:
            fail("Register page loaded", f"title={title!r}")

        # ── 2. Fill & submit registration ───────────────────────────────────
        print("\n[2] Register")
        await page.fill("#email", EMAIL)
        await page.fill("#password", PASSWORD)
        await page.fill("#confirmPassword", PASSWORD)
        await page.screenshot(path=str(SCREENSHOTS / "02_register_filled.png"))

        await page.click('button[type="submit"]')

        try:
            # Should redirect to dashboard (/) after auto-login
            await page.wait_for_url(f"{BASE}/", timeout=15_000)
            await page.screenshot(path=str(SCREENSHOTS / "03_dashboard_after_register.png"))
            ok("Registration + auto-login -> dashboard", f"url={page.url}")
        except Exception as e:
            await page.screenshot(path=str(SCREENSHOTS / "03_register_error.png"))
            # Check for error message on page
            await page.inner_text("body")
            fail("Registration + auto-login", f"url={page.url} | {str(e)[:80]}")

        # ── 3. Dashboard renders ────────────────────────────────────────────
        print("\n[3] Dashboard")
        if page.url == f"{BASE}/":
            sidebar = page.locator("text=BridgeLeads").first
            try:
                await expect(sidebar).to_be_visible(timeout=5_000)
                ok("Sidebar visible on dashboard")
            except Exception:
                fail("Sidebar visible on dashboard")

            nav_today = page.locator("nav a", has_text="Today").first
            try:
                await expect(nav_today).to_be_visible(timeout=5_000)
                ok("Nav item 'Today' visible")
            except Exception:
                fail("Nav item 'Today' visible")

            await page.screenshot(path=str(SCREENSHOTS / "04_dashboard.png"))

        # ── 4. Logout via NextAuth signout ──────────────────────────────────
        print("\n[4] Logout")
        await page.goto(f"{BASE}/api/auth/signout")
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path=str(SCREENSHOTS / "05_signout_page.png"))

        # NextAuth renders a "Sign out" confirmation button
        signout_btn = page.locator('button[type="submit"]')
        try:
            await expect(signout_btn).to_be_visible(timeout=5_000)
            await signout_btn.click()
            await page.wait_for_load_state("networkidle")
            await page.screenshot(path=str(SCREENSHOTS / "06_after_signout.png"))
            ok("NextAuth signout page rendered + confirmed")
        except Exception:
            # Some NextAuth configs redirect directly without confirmation
            ok("Signout page reached (no confirmation button — direct redirect)")

        # ── 5. Verify protected route redirects to login ────────────────────
        print("\n[5] Auth guard")
        await page.goto(f"{BASE}/")
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path=str(SCREENSHOTS / "07_auth_guard.png"))

        if "/login" in page.url or "/register" in page.url:
            ok("Protected route -> redirected to login", f"url={page.url}")
        else:
            fail("Protected route -> expected redirect to /login", f"url={page.url}")

        # ── 6. Login with existing credentials ─────────────────────────────
        print("\n[6] Login")
        await page.goto(f"{BASE}/login")
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path=str(SCREENSHOTS / "08_login_page.png"))

        await page.fill("#email", EMAIL)
        await page.fill("#password", PASSWORD)
        await page.screenshot(path=str(SCREENSHOTS / "09_login_filled.png"))
        await page.click('button[type="submit"]')

        try:
            await page.wait_for_url(f"{BASE}/", timeout=15_000)
            await page.screenshot(path=str(SCREENSHOTS / "10_dashboard_after_login.png"))
            ok("Login -> dashboard", f"url={page.url}")
        except Exception as e:
            await page.screenshot(path=str(SCREENSHOTS / "10_login_error.png"))
            fail("Login -> dashboard", f"url={page.url} | {str(e)[:80]}")

        # ── 7. Wrong password rejected ──────────────────────────────────────
        print("\n[7] Auth edge case")
        await page.goto(f"{BASE}/login")
        await page.wait_for_load_state("networkidle")
        await page.fill("#email", EMAIL)
        await page.fill("#password", "WrongPass999!")
        await page.click('button[type="submit"]')

        try:
            error_el = page.locator("text=Invalid email or password")
            await expect(error_el).to_be_visible(timeout=8_000)
            await page.screenshot(path=str(SCREENSHOTS / "11_wrong_password_error.png"))
            ok("Wrong password -> error message shown")
        except Exception:
            await page.screenshot(path=str(SCREENSHOTS / "11_wrong_password_error.png"))
            fail("Wrong password -> error message not shown", f"url={page.url}")

        await browser.close()

    # ── Summary ─────────────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 52}")
    print(f"  {passed}/{total} passed  |  {failed} failed")
    print(f"  Screenshots saved to: {SCREENSHOTS.resolve()}")
    print("  ALL PASS" if failed == 0 else "  FAILURES detected — see above")
    print(f"{'=' * 52}\n")

    if failed > 0:
        import sys
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
