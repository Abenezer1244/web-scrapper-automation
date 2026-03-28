"""Playwright-only base scraper for all BridgeLeads county connectors."""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import requests
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

_logger = setup_logger("scraper.base")


@dataclass
class ScrapedRecord:
    """Normalised record extracted by any county connector."""

    date_recorded: str | None = None
    party_name: str | None = None
    heirs: str | None = None
    legal_description: str | None = None
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
        self.on_progress: Any | None = None  # callback(page_current, page_total, record_count)

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
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        if self._playwright:
            await self._playwright.stop()
        _logger.info("Browser context closed")

    # ─── Core navigation ──────────────────────────────────────────────────────

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        """Navigate to a URL. Validates against SSRF allowlist before any request."""
        validate_scraping_target(url)

        for attempt in range(1, settings.MAX_RETRIES + 1):
            try:
                await self.page.goto(url, wait_until=wait_until, timeout=settings.DEFAULT_TIMEOUT * 1000)
                _logger.info("Navigated to %s", url)
                return
            except Exception as exc:
                _logger.warning("Navigate attempt %d/%d failed: %s", attempt, settings.MAX_RETRIES, exc)
                if attempt == settings.MAX_RETRIES:
                    raise
                await asyncio.sleep(2 ** attempt)  # exponential backoff

    def get_soup(self) -> BeautifulSoup:
        """Return a BeautifulSoup parse of the current page content."""
        if not self.page:
            raise RuntimeError("BridgeScraper not started — use 'async with BridgeScraper()'")
        # page.content() is a coroutine — callers must await this helper or use get_soup_async
        raise RuntimeError("Use await get_soup_async() instead of get_soup()")

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
        validate_scraping_target(url)
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "BridgeLeads-Probe/1.0"})
            if resp.status_code == 200 and len(resp.text) > 500:
                return "static"
        except Exception:
            pass
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
