"""AI-powered universal scraper.

Uses Claude to navigate any county public records site, extract records,
and handle pagination — no per-county selector code needed.

Usage:
    async with AIScraper(base_url="https://...", record_types=["probate"]) as scraper:
        records = await scraper.scrape("01/01/2025", "03/01/2025")
"""

# SSRF validation happens at connector creation time (scrapers.py route handler)
from src.scrapers.ai.cache import get_cached_actions, invalidate_cache, save_cached_actions
from src.scrapers.ai.extractor import ai_extract_records
from src.scrapers.ai.navigator import (
    _execute_action,
    ai_handle_disclaimer,
    ai_navigate_form,
)
from src.scrapers.ai.paginator import ai_go_next_page, ai_has_next_page
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.enrichment import enrich_parcel
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.ai_scraper")


class AIScraper(BridgeScraper):
    """Universal AI-powered scraper that works with any county website.

    Instead of hand-coded selectors, this uses Claude to:
    1. Accept disclaimers
    2. Fill and submit search forms
    3. Extract structured records from results
    4. Handle pagination
    """

    def __init__(self, base_url: str, county: str, state: str, record_types: list[str] | None = None):
        super().__init__()
        self.base_url = base_url
        self.county = county
        self.state = state
        self.record_types = record_types or []
        self._total_ai_cost = 0.0
        self._total_input_tokens = 0
        self._total_output_tokens = 0

        # Domain is validated at connector creation time via validate_scraping_target()
        # No dynamic domain registration — only pre-approved domains are allowed

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Run full AI-powered scrape for the given date range.

        Args:
            date_from: Start date in MM/DD/YYYY format.
            date_to: End date in MM/DD/YYYY format.

        Returns:
            List of ScrapedRecord instances, deduplicated by row hash.
        """
        record_type = self.record_types[0] if self.record_types else "all"
        _logger.info(
            "AI Scraper — %s/%s %s — scraping %s to %s",
            self.county, self.state, record_type, date_from, date_to,
        )

        # Step 1: Navigate to the county portal
        await self.navigate(self.base_url)

        # Step 2: Try cached navigation actions first (free, no Claude call)
        cached_actions = get_cached_actions(self.base_url, record_type)
        nav_succeeded = False

        if cached_actions:
            _logger.info("Replaying %d cached actions", len(cached_actions))
            try:
                # Inject dynamic values (dates) into cached actions
                actions = _inject_dates(cached_actions, date_from, date_to)
                for i, action in enumerate(actions):
                    _logger.info("Cached action %d/%d: %s", i + 1, len(actions), action.get("description", ""))
                    await _execute_action(self.page, action)
                nav_succeeded = True
                _logger.info("Cached navigation replay succeeded")
            except Exception as exc:
                _logger.warning("Cached actions failed: %s — falling back to Claude", exc)
                invalidate_cache(self.base_url, record_type)
                # Reload the page for a fresh start
                await self.navigate(self.base_url)

        # Step 3: If cache miss or cache failed, use Claude
        if not nav_succeeded:
            # Handle disclaimer if present
            disclaimer_usage = await ai_handle_disclaimer(self.page)
            if disclaimer_usage:
                self._track_usage(disclaimer_usage)

            # Fill and submit the search form via AI
            nav_result = await ai_navigate_form(
                self.page, record_type, date_from, date_to,
            )
            self._track_usage(nav_result["ai_usage"])

            # Cache the actions for future runs
            save_cached_actions(self.base_url, record_type, nav_result["actions"])

        # Step 4: Extract records with pagination
        all_records: list[ScrapedRecord] = []
        seen_hashes: set[str] = set()
        page_num = 0
        max_pages = 50  # Safety limit

        while page_num < max_pages:
            page_num += 1
            _logger.info("Processing page %d", page_num)

            # Extract records from current page
            page_records, extract_usage = await ai_extract_records(self.page)
            self._track_usage(extract_usage)

            # Deduplicate
            new_count = 0
            for record in page_records:
                h = self.make_hash(record.to_dict())
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    record.raw_html_hash = h
                    all_records.append(record)
                    new_count += 1

            _logger.info(
                "Page %d — %d new records (total: %d)",
                page_num, new_count, len(all_records),
            )

            # Check for next page
            if page_num >= max_pages:
                _logger.warning("Reached max page limit (%d)", max_pages)
                break

            has_next, next_action, page_usage = await ai_has_next_page(self.page)
            self._track_usage(page_usage)

            if not has_next or next_action is None:
                break

            await ai_go_next_page(self.page, next_action)
            await self.polite_delay()

        # Step 5: Enrichment pass
        _logger.info("Enriching %d records with parcel data", len(all_records))
        for record in all_records:
            if record.parcel_id:
                enriched = await enrich_parcel(record.parcel_id, self.county, self.state)
                record.property_address = enriched.get("property_address") or record.property_address
                record.mailing_address = enriched.get("mailing_address") or record.mailing_address
                record.enrichment_data = enriched
                await self.polite_delay()

        _logger.info(
            "AI Scraper complete — %d records, AI cost: $%.4f (%d input + %d output tokens)",
            len(all_records), self._total_ai_cost,
            self._total_input_tokens, self._total_output_tokens,
        )
        return all_records

    def _track_usage(self, usage: dict) -> None:
        """Accumulate AI token/cost usage."""
        self._total_input_tokens += usage.get("input_tokens", 0)
        self._total_output_tokens += usage.get("output_tokens", 0)
        self._total_ai_cost += usage.get("cost_usd", 0.0)

    @property
    def ai_cost(self) -> float:
        """Total Claude API cost for this scrape session."""
        return self._total_ai_cost

    @property
    def ai_tokens(self) -> dict:
        """Total token usage for this scrape session."""
        return {
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
        }


def _inject_dates(actions: list[dict], date_from: str, date_to: str) -> list[dict]:
    """Replace date placeholders in cached actions with actual dates.

    Cached actions store the original dates. On replay, we substitute
    the new date range into fill/evaluate actions that contain date values.
    """
    import copy
    import re

    result = []
    date_pattern = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")

    # M10 (full-SaaS review): track "first date used" with a local
    # variable instead of a function attribute. The previous
    # implementation used `_inject_dates._used_from` which is a
    # module-level variable and scrambles date injection when two
    # AI scrapers run concurrently in the same process (e.g., a
    # Celery worker running two scraper tasks in parallel). The
    # local variable is per-call and therefore thread-safe.
    used_from = False

    for action in actions:
        a = copy.deepcopy(action)

        if a.get("action") == "fill" and a.get("value"):
            # If the value looks like a date, replace it
            if date_pattern.fullmatch(a["value"].strip()):
                # First date fill → date_from, second → date_to
                if not used_from:
                    a["value"] = date_from
                    used_from = True
                else:
                    a["value"] = date_to
                    used_from = False  # reset for next pair

        elif a.get("action") == "evaluate" and a.get("js"):
            # Replace date literals in JS code
            dates_in_js = date_pattern.findall(a["js"])
            if len(dates_in_js) >= 2:
                a["js"] = a["js"].replace(dates_in_js[0], date_from, 1)
                a["js"] = a["js"].replace(dates_in_js[1], date_to, 1)
            elif len(dates_in_js) == 1:
                a["js"] = a["js"].replace(dates_in_js[0], date_from, 1)

        result.append(a)

    return result
