/**
 * Responsive device check for bridgeleads-web (frontend lives in the sibling
 * repo; this script is kept here with the sweep handoff).
 *
 * WHY THIS EXISTS. The responsive sweep sizes every touch target with the
 * `pointer-coarse:` variant, which keys off pointer TYPE, not viewport width. A
 * resized desktop window — including the Claude-in-Chrome MCP browser — reports
 * `pointer: fine` and silently skips every one of those rules while looking
 * plausible. Playwright device descriptors set hasTouch + isMobile, so
 * `(pointer: coarse)` genuinely matches.
 *
 * It asserts that as a CONTROL before measuring anything. Without the control a
 * "clean" run is meaningless, so the script says so rather than passing quietly.
 *
 * Checks, per page and viewport:
 *   1. nav width vs viewport width
 *   2. the primary CTA's box — is any part of it outside the viewport?
 *   3. whether the page can actually be scrolled sideways to reach it
 *   4. footer link heights under a coarse pointer
 *
 * Two traps this deliberately avoids:
 *   - `documentElement.scrollWidth` COUNTS position:fixed elements and reports
 *     horizontal scroll that does not exist. Use body.scrollWidth plus a real
 *     window.scrollTo delta (this produced a false production alarm once).
 *   - WebKit allows programmatic scrolling past overflow:hidden, so a non-zero
 *     delta there is not proof that a user can swipe to it.
 *
 * Usage:
 *   MSYS_NO_PATHCONV=1 node scripts/responsive-device-check.mjs <baseUrl> [chromium|webkit]
 *
 * The MSYS_NO_PATHCONV prefix matters in Git Bash, which otherwise rewrites a
 * leading-slash path argument into C:/Program Files/Git/...
 *
 * Run BOTH engines. WebKit approximates iOS Safari and catches things Chromium
 * hides — it was the only engine that surfaced horizontal scroll on /pricing.
 */
import { chromium, webkit, devices } from "playwright";

const BASE = (process.argv[2] || "").replace(/\/$/, "");
const ENGINE = (process.argv[3] || "chromium").toLowerCase();
if (!BASE) { console.error("usage: node navfix_verify.mjs <baseUrl> [engine]"); process.exit(2); }

const engine = ENGINE === "webkit" ? webkit : chromium;
const browser = await engine.launch();
let fails = 0;

for (const w of [320, 390]) {
  const ctx = await browser.newContext({ ...devices["iPhone 13"], viewport: { width: w, height: 720 } });
  const page = await ctx.newPage();

  for (const path of ["/pricing", "/coverage", "/"]) {
    try {
      await page.goto(BASE + path, { waitUntil: "domcontentloaded", timeout: 45000 });
    } catch (e) { console.log(`\n${path} @${w}: NAV ERROR ${String(e).slice(0, 70)}`); continue; }
    await page.waitForTimeout(900);

    const r = await page.evaluate(async (vw) => {
      const coarse = matchMedia("(pointer: coarse)").matches;
      const nav = document.querySelector("nav");
      const navW = nav ? Math.round(nav.getBoundingClientRect().width) : null;

      // The CTA is the nav's register link — the money button.
      const cta = nav && nav.querySelector('a[href="/register"]');
      const c = cta ? cta.getBoundingClientRect() : null;

      const beforeX = window.scrollX;
      window.scrollTo(9999, 0);
      await new Promise((res) => setTimeout(res, 300));
      const movedX = window.scrollX - beforeX;
      window.scrollTo(0, 0);

      // footer links under coarse pointer
      const footLinks = [...document.querySelectorAll("footer a")].map((el) => {
        const b = el.getBoundingClientRect();
        return { t: (el.textContent || "").trim().slice(0, 18), h: Math.round(b.height), w: Math.round(b.width) };
      }).filter((x) => x.h > 0);
      const shortFoot = footLinks.filter((x) => x.h < 44);

      return {
        coarse, navW,
        cta: c ? { left: Math.round(c.left), right: Math.round(c.right), h: Math.round(c.height) } : null,
        offscreen: c ? c.right > vw + 0.5 || c.left < -0.5 : null,
        movedX,
        docSW: document.documentElement.scrollWidth,
        bodySW: document.body.scrollWidth,
        footTotal: footLinks.length, footShort: shortFoot.length,
        footSample: shortFoot.slice(0, 4),
      };
    }, w);

    if (!r.coarse) { console.log(`\n${path} @${w}: !! CONTROL FAILED — pointer:coarse false`); fails++; continue; }

    const bad = r.offscreen === true;
    if (bad) fails++;
    console.log(`\n${path} @${w}px  [coarse=${r.coarse}]`);
    console.log(`  nav width ${r.navW} (viewport ${w})   doc.scrollW=${r.docSW} body.scrollW=${r.bodySW}`);
    if (r.cta) console.log(`  CTA box ${r.cta.left}..${r.cta.right} h=${r.cta.h}  -> ${bad ? "!! OFF-SCREEN" : "on-screen"}`);
    else console.log("  CTA: not found");
    console.log(`  page scrolls sideways: ${r.movedX > 0 ? "YES " + r.movedX + "px" : "no"}`);
    console.log(`  footer links ${r.footTotal}, under 44px: ${r.footShort}` +
      (r.footSample.length ? "  e.g. " + r.footSample.map((f) => `${f.w}x${f.h} "${f.t}"`).join(", ") : ""));
  }
  await ctx.close();
}
await browser.close();
console.log(`\n=== ${ENGINE}: off-screen CTA / control failures = ${fails} ===`);
