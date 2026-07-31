"""Tests for the scraper browser identity resolution.

Pure functions over real strings — no mocks, no browser needed. The values
asserted here are the exact ones observed from live Playwright runs:
playwright 1.49.1 reports "131.0.6778.33", 1.60.0 reports "148.0.7778.96".
"""

import pytest

from src.scrapers.browser_identity import (
    HTTP_BROWSER_LIKE_UA,
    LEGACY_BROWSER_UA,
    VALID_UA_MODES,
    browser_major,
    build_chromium_ua,
    resolve_playwright_user_agent,
)

# Real values observed from the two versions this module was written for.
PW_149_VERSION = "131.0.6778.33"
PW_160_VERSION = "148.0.7778.96"


def test_browser_major_parses_real_chromium_versions():
    assert browser_major(PW_149_VERSION) == 131
    assert browser_major(PW_160_VERSION) == 148


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "148",
        "148.0",
        "148.0.7778",
        "Chrome/148.0.7778.96",
        "HeadlessChrome/148.0.7778.96",
        "v148.0.7778.96",
        "148.0.7778.96-beta",
    ],
)
def test_browser_major_rejects_unexpected_formats(bad):
    """A wrong-but-plausible UA is worse than a loud failure.

    Playwright 1.57 repackaged Chromium to Chrome for Testing; if a future
    release changes the version string shape we want an exception, not a
    silently malformed 'Chrome/Headless.0.0.0' shipped to every portal.
    """
    with pytest.raises(ValueError):
        browser_major(bad)


def test_browser_major_rejects_none():
    with pytest.raises(ValueError):
        browser_major(None)  # type: ignore[arg-type]


def test_build_chromium_ua_uses_reduced_major_form():
    ua = build_chromium_ua(148, "linux")
    assert "Chrome/148.0.0.0 Safari/537.36" in ua
    assert "X11; Linux x86_64" in ua
    assert "Windows" not in ua


def test_build_chromium_ua_windows_platform():
    ua = build_chromium_ua(148, "windows")
    assert "Windows NT 10.0; Win64; x64" in ua
    assert "Linux" not in ua


@pytest.mark.parametrize("bad_platform", ["macos", "", "Linux", "x11"])
def test_build_chromium_ua_rejects_unknown_platform(bad_platform):
    with pytest.raises(ValueError):
        build_chromium_ua(148, bad_platform)


@pytest.mark.parametrize("bad_major", [0, -1])
def test_build_chromium_ua_rejects_invalid_major(bad_major):
    with pytest.raises(ValueError):
        build_chromium_ua(bad_major, "linux")


def test_legacy_mode_is_byte_identical_to_the_old_hardcoded_string():
    """`legacy` must be a provable no-op — this is what makes shipping safe.

    The literal below is the string that was hardcoded at base_scraper.py
    lines 189-193 before browser_identity.py existed.
    """
    old_hardcoded = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    assert LEGACY_BROWSER_UA == old_hardcoded
    assert resolve_playwright_user_agent(PW_160_VERSION, mode="legacy") == old_hardcoded


def test_legacy_mode_ignores_the_real_browser_version():
    """Legacy is deliberately frozen, even on a much newer browser."""
    assert resolve_playwright_user_agent(PW_149_VERSION, mode="legacy") == LEGACY_BROWSER_UA
    assert resolve_playwright_user_agent(PW_160_VERSION, mode="legacy") == LEGACY_BROWSER_UA


def test_linux_dynamic_tracks_the_actual_browser():
    assert "Chrome/131.0.0.0" in resolve_playwright_user_agent(
        PW_149_VERSION, mode="linux_dynamic"
    )
    ua = resolve_playwright_user_agent(PW_160_VERSION, mode="linux_dynamic")
    assert "Chrome/148.0.0.0" in ua
    # The whole point: coherent with the Linux container and navigator.platform.
    assert "X11; Linux x86_64" in ua


def test_windows_dynamic_fixes_version_but_keeps_the_platform_claim():
    """Documents that this mode is for A/B canary only, not a target state."""
    ua = resolve_playwright_user_agent(PW_160_VERSION, mode="windows_dynamic")
    assert "Chrome/148.0.0.0" in ua
    assert "Windows NT 10.0" in ua


def test_override_wins_over_every_mode():
    custom = "SomePortalDemandsThis/1.0"
    for mode in VALID_UA_MODES:
        assert resolve_playwright_user_agent(PW_160_VERSION, mode=mode, override=custom) == custom


def test_empty_override_is_ignored():
    assert resolve_playwright_user_agent(PW_160_VERSION, mode="legacy", override="") == LEGACY_BROWSER_UA
    assert resolve_playwright_user_agent(PW_160_VERSION, mode="legacy", override=None) == LEGACY_BROWSER_UA


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        resolve_playwright_user_agent(PW_160_VERSION, mode="chaos")


def test_unknown_mode_still_raises_on_a_bad_browser_version():
    with pytest.raises(ValueError):
        resolve_playwright_user_agent("garbage", mode="linux_dynamic")


def test_http_ua_is_separate_from_the_browser_identity():
    """Plain-HTTP callers must not silently inherit the browser's identity.

    A requests call claiming a bleeding-edge Chrome, with no browser TLS
    fingerprint and no Sec-CH-UA headers, is a mismatch of its own.
    """
    dynamic = resolve_playwright_user_agent(PW_160_VERSION, mode="linux_dynamic")
    assert HTTP_BROWSER_LIKE_UA != dynamic
    assert "Chrome/148" not in HTTP_BROWSER_LIKE_UA
