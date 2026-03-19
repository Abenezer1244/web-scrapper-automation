"""AI record extractor: uses Claude to extract structured records from results pages.

Given a Playwright page showing search results, this module:
1. Takes a screenshot + gets the visible HTML
2. Asks Claude to extract records as structured JSON
3. Parses and validates the records into ScrapedRecord objects
"""

import json
import re

from playwright.async_api import Page

from src.scrapers.ai.client import ask_claude
from src.scrapers.base_scraper import ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.ai.extractor")

_SYSTEM_PROMPT = """You are a data extraction agent for county public records.
You will receive a screenshot and HTML snippet of a search results page from a county government website.
Your job is to extract every record from the results table as structured JSON.

Return ONLY a valid JSON array. No markdown fences, no explanation outside the JSON.

Each record is an object with these fields (use null if a field is not found on the page):
- "date_recorded": string in MM/DD/YYYY format
- "party_name": string (the primary name on the record)
- "heirs": string or null (comma-separated if multiple names associated)
- "legal_description": string or null (land description — lots, blocks, sections)
- "parcel_id": string or null (numeric parcel/APN identifier, usually 8-12 digits)
- "property_address": string or null
- "mailing_address": string or null

Rules:
- Extract ALL rows from the results table, not just the first few.
- If a row has multiple names, put the primary in party_name and others in heirs.
- If you cannot determine which name is primary, use the first listed name.
- Parcel IDs are usually numeric strings. Do not confuse them with instrument/document numbers.
- Skip header rows and pagination rows — only extract data rows.
- If the page shows "No records found" or similar, return an empty array [].
- Dates should be normalized to MM/DD/YYYY format."""


async def ai_extract_records(page: Page) -> tuple[list[ScrapedRecord], dict]:
    """Extract structured records from the current results page using Claude.

    Args:
        page: Playwright page showing search results.

    Returns:
        Tuple of (list of ScrapedRecord, ai_usage dict with token/cost info)
    """
    screenshot = await page.screenshot(type="png", full_page=True)

    # Get a trimmed HTML snippet of the results area (avoid sending the full DOM)
    html_snippet = await _get_results_html(page)

    user_message = (
        f"Extract all records from this search results page.\n\n"
        f"Current URL: {page.url}\n\n"
    )
    if html_snippet:
        user_message += f"Results HTML (trimmed):\n```html\n{html_snippet}\n```\n\n"
    user_message += "Return a JSON array of record objects."

    response = await ask_claude(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_message,
        images=[screenshot],
    )

    raw_records = _parse_records(response["text"])
    records = _validate_records(raw_records)

    _logger.info("Claude extracted %d valid records from page", len(records))

    ai_usage = {
        "input_tokens": response["input_tokens"],
        "output_tokens": response["output_tokens"],
        "cost_usd": response["cost_usd"],
    }
    return records, ai_usage


async def _get_results_html(page: Page) -> str:
    """Extract the results table HTML, trimmed to a reasonable size for Claude."""
    try:
        # Try common result table selectors
        for selector in [
            "table[id*='Grid']", "table[id*='grid']",
            "table[id*='Result']", "table[id*='result']",
            "table[id*='Data']", "table[id*='data']",
            "#searchResults", ".search-results",
            "table.results", "table.data",
        ]:
            el = page.locator(selector).first
            if await el.count() > 0:
                html = await el.inner_html()
                # Trim to ~15K chars to stay within reasonable token limits
                if len(html) > 15_000:
                    html = html[:15_000] + "\n<!-- ... trimmed ... -->"
                return html
    except Exception:
        pass

    # Fallback: get the main content area
    try:
        body = await page.locator("main, #content, .content, body").first.inner_html()
        if len(body) > 15_000:
            body = body[:15_000] + "\n<!-- ... trimmed ... -->"
        return body
    except Exception:
        return ""


def _parse_records(text: str) -> list[dict]:
    """Parse Claude's JSON response into a list of record dicts."""
    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        _logger.error("Failed to parse extraction response as JSON: %s", text[:200])

    return []


def _validate_records(raw_records: list[dict]) -> list[ScrapedRecord]:
    """Validate and convert raw dicts into ScrapedRecord objects."""
    records = []
    for raw in raw_records:
        # Must have at least one identifying field
        has_date = bool(raw.get("date_recorded"))
        has_name = bool(raw.get("party_name"))
        has_parcel = bool(raw.get("parcel_id"))

        if not any([has_date, has_name, has_parcel]):
            continue

        # Normalize date format
        date = raw.get("date_recorded")
        if date:
            date = _normalize_date(date)

        record = ScrapedRecord(
            date_recorded=date,
            party_name=_clean(raw.get("party_name")),
            heirs=_clean(raw.get("heirs")),
            legal_description=_clean(raw.get("legal_description")),
            parcel_id=_clean(raw.get("parcel_id")),
            property_address=_clean(raw.get("property_address")),
            mailing_address=_clean(raw.get("mailing_address")),
        )
        records.append(record)

    return records


def _normalize_date(date_str: str) -> str:
    """Normalize a date string to MM/DD/YYYY format."""
    # Already in MM/DD/YYYY
    if re.match(r"\d{1,2}/\d{1,2}/\d{4}", date_str):
        return date_str
    # YYYY-MM-DD → MM/DD/YYYY
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str)
    if m:
        return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
    return date_str


def _clean(val) -> str | None:
    """Clean a value: strip whitespace, return None for empty/null."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None
