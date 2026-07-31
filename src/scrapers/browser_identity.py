"""Single source of truth for the identity our scrapers present to portals.

WHY THIS EXISTS
---------------
The User-Agent was hardcoded in 8 places across 3 different values
(``Chrome/120.0.0.0``, ``Chrome/120.0``, ``Chrome/121.0``) and had drifted far
from reality: the Playwright context claimed Chrome/120 while actually running
Chromium 131 (playwright 1.49), and 148 after the 1.60 bump. A hardcoded
version string is guaranteed to go stale on every browser bump, so the fix is
to derive it from the browser we are actually running.

TWO IDENTITIES, DELIBERATELY NOT SHARED
---------------------------------------
A real browser and a bare ``requests`` call are different clients. An HTTP
client claiming to be Chrome — with no browser TLS fingerprint, no
``Sec-Fetch-*``/``Sec-CH-UA`` headers and no navigation pattern — is arguably
*more* suspicious than one that is simply boring. So:

* ``resolve_playwright_user_agent()`` — for the real browser context; tracks the
  actual bundled Chromium major.
* ``HTTP_BROWSER_LIKE_UA`` — a static, boring, browser-ish UA for the plain-HTTP
  paths that talk to legacy portals which reject non-browser clients.

THE PLATFORM CONTRADICTION
--------------------------
The legacy UA claims ``Windows NT 10.0`` while production runs Linux
containers, and the anti-detection init script does NOT override
``navigator.platform`` or ``navigator.userAgentData`` — both of which report the
real OS. A UA/platform contradiction is a stronger bot signal than a stale
version number, so bumping the version while keeping the Windows claim would
improve the weak signal and leave the strong one. ``linux_dynamic`` is
therefore the coherent target: it matches the container, ``navigator.platform``
and UA-CH. Full Windows spoofing is only safer if the entire fingerprint
surface is maintained, which this codebase does not do.

ROLLOUT
-------
Changing the UA can change how a portal responds (content negotiation, bot
rules, redirects, cookie gates), and scraper navigation has no unit coverage.
So the default stays ``legacy`` — byte-identical to the previous hardcoded
string — until a canary says otherwise. Flip via ``SCRAPER_BROWSER_UA_MODE``.
"""

from __future__ import annotations

import re

# The exact string that was hardcoded at base_scraper.py:189-193 before this
# module existed. Kept verbatim so `legacy` mode is provably a no-op.
LEGACY_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# For plain-HTTP callers that need to look browser-ish to a legacy portal.
# Deliberately static and deliberately NOT chasing the newest Chrome: without
# matching TLS/CH headers, a bleeding-edge claim is a mismatch of its own.
HTTP_BROWSER_LIKE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_PLATFORM_TOKENS = {
    "linux": "X11; Linux x86_64",
    "windows": "Windows NT 10.0; Win64; x64",
}

# Chromium reports e.g. "148.0.7778.96". Anchored so a repackaging surprise
# (1.57 moved Chromium to Chrome for Testing) fails loudly instead of silently
# producing "Chrome/Headless.0.0.0".
_VERSION_RE = re.compile(r"^(\d+)\.\d+\.\d+\.\d+$")

VALID_UA_MODES = ("legacy", "linux_dynamic", "windows_dynamic")


def browser_major(version: str) -> int:
    """Extract the Chromium major from ``browser.version``.

    Raises ValueError on anything unexpected — a wrong-but-plausible UA is
    worse than a loud failure, because it would silently ship a broken
    identity to every portal.
    """
    match = _VERSION_RE.match((version or "").strip())
    if not match:
        raise ValueError(f"Unexpected browser version format: {version!r}")
    return int(match.group(1))


def build_chromium_ua(major: int, platform: str) -> str:
    """Build a Chrome UA string for a major version and platform.

    Uses ``MAJOR.0.0.0`` — the reduced form real Chrome sends.
    """
    token = _PLATFORM_TOKENS.get(platform)
    if token is None:
        raise ValueError(f"Unknown platform {platform!r}; expected one of {sorted(_PLATFORM_TOKENS)}")
    if major <= 0:
        raise ValueError(f"Invalid Chromium major: {major!r}")
    return (
        f"Mozilla/5.0 ({token}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def resolve_playwright_user_agent(
    browser_version: str,
    mode: str = "legacy",
    override: str | None = None,
) -> str:
    """Resolve the UA for a Playwright context.

    ``override`` wins outright (an operator escape hatch for a portal that
    demands something specific). Otherwise ``mode`` selects:

      legacy          — the previous hardcoded string, byte-identical
      linux_dynamic   — real Chromium major + Linux platform (coherent with
                        the container and navigator.platform)
      windows_dynamic — real Chromium major + Windows platform. Fixes the
                        stale version but KEEPS the platform contradiction;
                        provided only for A/B canary comparison, not as a
                        recommended default.
    """
    if override:
        return override
    if mode == "legacy":
        return LEGACY_BROWSER_UA
    if mode not in VALID_UA_MODES:
        raise ValueError(f"Unknown UA mode {mode!r}; expected one of {list(VALID_UA_MODES)}")
    platform = "linux" if mode == "linux_dynamic" else "windows"
    return build_chromium_ua(browser_major(browser_version), platform)
