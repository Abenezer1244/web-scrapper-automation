"""Playwright-only base scraper for all BridgeLeads county connectors."""

import asyncio
import functools
import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from bs4 import BeautifulSoup
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from src.api.middleware.security import validate_scraping_target
from src.config import settings
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.base")


# ─── Party-name normalization ────────────────────────────────────────────────
# Recorder portals stack multiple parties (co-owners, or borrower + trustee)
# inside ONE name cell, separated by structural markup — LandmarkWeb uses
# `<div class='nameSeperator'></div>` (sic), others use `<br>`. Blanket tag
# stripping (`re.sub(r'<[^>]+>', '', s)`) deletes those with NO replacement, so
# the names collapse together: "BOYLE DAVID E" + "QUALITY LOAN SERVICE CORP"
# becomes "BOYLE DAVID EQUALITY LOAN SERVICE CORP". That corrupts the displayed
# party_name and degrades skip-trace matching. `normalize_party_text` converts
# the structural boundaries to " / " BEFORE removing the remaining inline tags.
_NAME_SEPARATOR_RE = re.compile(r"(?i)<div[^>]*\bnameSeperator\b[^>]*>\s*</div>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_TAG_RE = re.compile(r"<[^>]+>")
_LANDMARK_PREFIX_RE = re.compile(r"(?i)(?:nobreak_|unclickable_)")
_MULTI_DELIM_RE = re.compile(r"\s*/\s*(?:/\s*)+")
_WS_RE = re.compile(r"\s+")


def normalize_party_text(raw: str | None) -> str:
    """Clean an HTML party/name cell to text, preserving multi-owner boundaries
    as ' / '.

    Converts the structural party separators (LandmarkWeb's nameSeperator div and
    `<br>`) to " / ", THEN strips remaining inline tags with no separator (so a
    name wrapped in inline markup like ``MA<b>RRS</b>`` is not split mid-token).
    Decodes HTML entities, drops LandmarkWeb CSS-class prefixes baked into cell
    text, and collapses whitespace / repeated and stray delimiters.
    """
    if not raw:
        return ""
    s = str(raw)
    s = _NAME_SEPARATOR_RE.sub(" / ", s)
    s = _BR_RE.sub(" / ", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _LANDMARK_PREFIX_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    s = _MULTI_DELIM_RE.sub(" / ", s)
    s = re.sub(r"^\s*/\s*|\s*/\s*$", "", s)
    return s.strip()


class ProgressCallback(Protocol):
    """Signature every BridgeScraper subclass invokes on its `on_progress` slot.

    workers/tasks.py installs a callable matching this shape; some
    scrapers also pass a 4th `phase` arg (e.g. "parcel_lookup",
    "enriching") which defaults to "scraping" when omitted.
    """

    def __call__(
        self,
        page_current: int,
        page_total: int,
        record_count: int,
        phase: str = "scraping",
    ) -> None: ...


@dataclass
class ScrapedRecord:
    """Normalised record extracted by any county connector."""

    date_recorded: str | None = None
    party_name: str | None = None
    heirs: str | None = None
    legal_description: str | None = None
    doc_type: str | None = None
    parcel_id: str | None = None
    property_address: str | None = None
    mailing_address: str | None = None
    enrichment_data: dict[str, Any] = field(default_factory=dict)
    raw_html_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date_recorded": self.date_recorded,
            "party_name": self.party_name,
            "heirs": self.heirs,
            "legal_description": self.legal_description,
            "doc_type": self.doc_type,
            "parcel_id": self.parcel_id,
            "property_address": self.property_address,
            "mailing_address": self.mailing_address,
            "enrichment_data": self.enrichment_data,
            "raw_html_hash": self.raw_html_hash,
        }


class BridgeScraper:
    """Async Playwright scraper base class.

    All county connectors must subclass this and implement `scrape()`.

    Usage:
        async with BridgeScraper() as scraper:
            records = await scraper.scrape()

    The context manager handles browser lifecycle, including cleanup on error.
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.on_progress: ProgressCallback | None = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "BridgeScraper":
        import os

        self._playwright = await async_playwright().start()

        # Use headed mode if DISPLAY is set (Xvfb virtual display on Railway).
        # This fixes EagleWeb sites where headless mode breaks JS redirects.
        has_display = bool(os.environ.get("DISPLAY"))
        use_headless = settings.PLAYWRIGHT_HEADLESS and not has_display

        self._browser = await self._playwright.chromium.launch(
            headless=use_headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-translate",
                "--no-first-run",
                "--js-flags=--max-old-space-size=512",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        # Per-hop SSRF enforcement: validate every DOCUMENT navigation
        # (initial load AND each redirect hop) BEFORE the request leaves the
        # browser. Without this, validate_scraping_target only sees the
        # initial and final URLs — a portal that 302s through an internal /
        # metadata host would already have made that request by the time we
        # re-check the landing URL. Aborting at the route layer closes that.
        await self._context.route("**/*", self._ssrf_route_guard)

        self.page = await self._context.new_page()

        # Anti-headless-detection: override navigator.webdriver
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            window.chrome = {runtime: {}};
        """)

        _logger.info("Browser context started (headless=%s, DISPLAY=%s)", use_headless, os.environ.get("DISPLAY", "unset"))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        # H13 (full-SaaS review): defensively close every layer with
        # individual try/except so a failure on one doesn't skip the
        # next. Previously a failure in context.close() would skip
        # browser.close() AND playwright.stop(), leaking Chromium
        # processes. We also explicitly null the references so a
        # subsequent __aenter__ on the same instance can re-create
        # cleanly.
        try:
            if self._context is not None:
                await self._context.close()
        except Exception as exc:
            _logger.warning("context.close failed (leak risk): %s", str(exc)[:120])
        self._context = None

        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception as exc:
            _logger.warning("browser.close failed (leak risk): %s", str(exc)[:120])
        self._browser = None

        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as exc:
            _logger.warning("playwright.stop failed (leak risk): %s", str(exc)[:120])
        self._playwright = None

        self.page = None
        _logger.info("Browser context closed")

    # ─── Core navigation ──────────────────────────────────────────────────────

    async def _ssrf_nav_allowed(self, request) -> bool:
        """Return False if this request must be aborted (SSRF).

        S1: validates EVERY http(s) request, not just document loads. In-page
        fetch()/XHR, scripts and images are a first-class egress channel — JS
        running in a loaded page (including anything the AI navigator is
        prompt-injected into emitting via ``evaluate``) could otherwise reach
        169.254.169.254 or any internal host completely outside this guard,
        because the route is registered for ``**/*`` and previously waved every
        non-document request straight through. Only the MAIN-FRAME DOCUMENT
        must be on the scrape allowlist; sub-frames (e.g. a reCAPTCHA iframe)
        and sub-resources (CDN JS/CSS/images, XHR) only need the blocked-IP /
        DNS-rebinding check, so legitimate third-party assets keep working.
        Non-HTTP(S) schemes (about:blank, data:, blob:) carry no host egress
        and pass through. getaddrinfo is sync, so it runs in the executor (no
        event-loop block; the OS resolver cache keeps repeat hosts cheap).
        Never raises — on an internal error it allows (continue) so the guard
        can't wedge a scrape. Shared by the context route guard AND any
        page-level route (which takes precedence over the context route).
        """
        try:
            url = request.url
            scheme = url.split(":", 1)[0].lower()
            if scheme not in ("http", "https"):
                return True
            try:
                is_subframe = request.frame.parent_frame is not None
            except Exception:
                is_subframe = False
            require_allowlisted = request.resource_type == "document" and not is_subframe
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                functools.partial(
                    validate_scraping_target,
                    url,
                    require_allowlisted=require_allowlisted,
                    resolve=True,
                ),
            )
            return True
        except ValueError:
            _logger.error("SSRF: blocked navigation to disallowed target %s", request.url)
            return False
        except Exception as exc:  # never let the guard itself wedge navigation
            _logger.debug("SSRF nav check passthrough (%s): %s", request.url[:80], exc)
            return True

    async def _ssrf_route_guard(self, route) -> None:
        """Context route guard: abort disallowed document navigations pre-flight."""
        if not await self._ssrf_nav_allowed(route.request):
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass
            return
        try:
            await route.continue_()
        except Exception:
            pass

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        """Navigate to a URL. Validates against SSRF allowlist before any request.

        ``resolve=True`` adds a DNS-rebinding check (rejects a host that
        resolves to a private/metadata IP). Intermediate redirect hops are
        enforced per-hop by ``_ssrf_route_guard`` (registered on the context),
        which aborts a disallowed document navigation before the request is
        sent — so this check plus the guard cover initial, intermediate, and
        final URLs.
        """
        validate_scraping_target(url, resolve=True)

        for attempt in range(1, settings.MAX_RETRIES + 1):
            try:
                response = await self.page.goto(
                    url, wait_until=wait_until, timeout=settings.DEFAULT_TIMEOUT * 1000
                )
                # M6 (full-SaaS review): Playwright follows redirects
                # automatically, so validate_scraping_target(url) above
                # only checks the INITIAL URL. A county portal that
                # 302s to a non-allowlisted domain would otherwise
                # land on the blocked target without the SSRF firewall
                # noticing. Re-validate the final URL after
                # navigation. If it differs from the request and
                # fails validation, we immediately close the page and
                # raise so no content is read from the disallowed
                # origin.
                final_url = response.url if response else self.page.url
                if final_url and final_url != url:
                    try:
                        validate_scraping_target(final_url, resolve=True)
                    except ValueError as ssrf_exc:
                        _logger.error(
                            "SSRF: redirect from %s landed on disallowed target %s",
                            url, final_url,
                        )
                        try:
                            await self.page.goto("about:blank", timeout=5000)
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"Navigation redirected to disallowed target: {ssrf_exc}"
                        ) from ssrf_exc
                _logger.info("Navigated to %s", url)
                return
            except Exception as exc:
                _logger.warning("Navigate attempt %d/%d failed: %s", attempt, settings.MAX_RETRIES, exc)
                if attempt == settings.MAX_RETRIES:
                    raise
                await asyncio.sleep(2 ** attempt)  # exponential backoff

    async def safe_goto(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 15_000,
    ):
        """SSRF-guarded ``page.goto`` for templates that can't use ``navigate()``.

        Some portals require a raw single-shot goto (e.g. King's reCAPTCHA
        flow, where ``navigate()``'s retry loop would re-trip the captcha).
        This validates the target (with DNS resolution) before navigating and
        re-validates the final landing URL after redirects. Intermediate hops
        are aborted pre-flight by ``_ssrf_route_guard`` on the context.
        Returns the goto response.
        """
        validate_scraping_target(url, resolve=True)
        response = await self.page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        final_url = response.url if response else self.page.url
        if final_url and final_url != url:
            try:
                validate_scraping_target(final_url, resolve=True)
            except ValueError as ssrf_exc:
                _logger.error(
                    "SSRF: redirect from %s landed on disallowed target %s",
                    url, final_url,
                )
                try:
                    await self.page.goto("about:blank", timeout=5000)
                except Exception:
                    pass
                raise RuntimeError(
                    f"Navigation redirected to disallowed target: {ssrf_exc}"
                ) from ssrf_exc
        return response

    async def get_soup_async(self) -> BeautifulSoup:
        """Return a BeautifulSoup parse of the current page content."""
        if not self.page:
            raise RuntimeError("BridgeScraper not started — use 'async with BridgeScraper()'")
        content = await self.page.content()
        return BeautifulSoup(content, "lxml")

    # ─── Render mode probe ────────────────────────────────────────────────────

    @staticmethod
    def probe(url: str) -> str:
        """Determine if a URL requires Playwright (JS) or can be fetched statically.

        Returns:
            'static' if requests.get returns the expected content,
            'playwright' otherwise.
        """
        try:
            # safe_get validates (resolve=True, allowlisted), disables
            # redirects, and uses a no-ambient-proxy session. A redirecting or
            # blocked probe raises/falls through to playwright (safe default).
            resp = safe_get(
                url,
                require_allowlisted=True,
                headers={"User-Agent": "BridgeLeads-Probe/1.0"},
                timeout=10,
            )
            if resp.status_code == 200 and len(resp.text) > 500:
                return "static"
        except Exception as exc:
            _logger.debug("Render-mode probe failed for %s: %s", url, exc)
        return "playwright"

    # ─── Utilities ────────────────────────────────────────────────────────────

    @staticmethod
    def make_hash(row_dict: dict[str, Any]) -> str:
        """MD5 fingerprint of a scraped row for deduplication.

        Normalises the dict to a stable JSON string before hashing so that
        field order differences do not produce different hashes.
        """
        stable = json.dumps(row_dict, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.md5(stable.encode("utf-8")).hexdigest()  # noqa: S324 (dedup only, not security)

    def dedupe_extend(
        self,
        new_records: list[ScrapedRecord],
        seen_hashes: set[str],
        all_records: list[ScrapedRecord],
    ) -> int:
        """Append non-duplicate records to ``all_records`` and return the new count.

        Mutates both ``seen_hashes`` (adds new fingerprints) and
        ``all_records`` (appends accepted records). Sets each kept
        record's ``raw_html_hash`` to its fingerprint. Returns the number
        of records that were genuinely new this batch — useful for the
        per-chunk / per-page progress log lines templates emit. The
        per-template chunk and pagination loops are deliberately kept
        site-specific because each portal's navigation differs; only
        this dedup tail is portable, so it lives here.
        """
        new_count = 0
        for record in new_records:
            h = self.make_hash(record.to_dict())
            if h not in seen_hashes:
                seen_hashes.add(h)
                record.raw_html_hash = h
                all_records.append(record)
                new_count += 1
        return new_count

    @staticmethod
    def clean(text: str | None) -> str | None:
        """Strip control characters and normalise whitespace in scraped text."""
        if text is None:
            return None
        # Remove control chars (except normal whitespace)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        # Collapse multiple spaces/newlines
        text = re.sub(r"\s+", " ", text)
        return text.strip() or None

    async def polite_delay(self) -> None:
        """Wait the configured polite delay between requests."""
        await asyncio.sleep(settings.POLITE_DELAY_MS / 1000)

    # ─── Subclass interface ───────────────────────────────────────────────────

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Run the full scrape for a date range. Must be implemented by subclasses."""
        raise NotImplementedError("Each county connector must implement scrape()")
