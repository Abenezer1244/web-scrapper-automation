"""AI-powered county assessor enrichment.

Uses the existing AI scraper infrastructure (Claude + Playwright) to
navigate county assessor/property search websites and look up parcel data.

Cost: ~$0.01 per lookup (Claude API). After first lookup per county,
navigation actions are cached in Redis — subsequent lookups replay free.

This is the fallback when free GIS REST APIs aren't available.
"""

import json

from src.config import settings
from src.scrapers.ai.client import ask_claude
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.ai_assessor")

# ─── Known assessor URLs (built-in) ──────────────────────────────────────────
_KNOWN_ASSESSOR_URLS: dict[str, str] = {
    "pierce_WA": "https://atip.piercecountywa.gov/app/parcelSearch",
    # Tyler PACS PropertyAccess (same vendor as Chelan/Douglas AcclaimWeb PACS)
    "island_WA": "https://assessor.islandcountywa.gov/propertyaccess/?cid=0",
}

_SYSTEM_PROMPT = """You are a browser automation agent looking at a county property/assessor search website.

I will show you a screenshot of the page and give you a parcel number to search for.

Your job:
1. If there is a search results page showing property data, extract the property address and mailing address.
2. If there is a search form, return JSON actions to fill the parcel number and submit.

For EXTRACTION (results visible), return:
```json
{"mode": "extract", "property_address": "...", "mailing_address": "...", "owner_name": "..."}
```

For NAVIGATION (search form visible), return:
```json
{"mode": "navigate", "actions": [
  {"action": "fill", "selector": "...", "value": "PARCEL_ID", "description": "..."},
  {"action": "click", "selector": "...", "description": "submit search"}
]}
```

Rules:
- Use CSS selectors or JavaScript evaluation for actions
- Prefer JavaScript (evaluate action) over CSS selectors for reliability
- Return ONLY valid JSON, no markdown, no explanation
- If no data found, return: {"mode": "extract", "property_address": null, "mailing_address": null}
"""


async def enrich_parcel_ai(
    parcel_id: str,
    county: str,
    state: str,
    assessor_url: str | None = None,
) -> dict[str, str | None]:
    """Look up a parcel by navigating a county assessor website with AI.

    Uses Claude to analyze screenshots and navigate the search form,
    then extracts property/mailing address from results.

    Args:
        parcel_id: The assessor parcel number (APN).
        county: County slug (e.g. "pierce").
        state: 2-letter state code (e.g. "WA").
        assessor_url: Optional override URL. If None, looks up from known URLs.

    Returns:
        Dict with: property_address, mailing_address. Any may be None.
    """
    if not settings.AI_ENRICHMENT_ENABLED:
        return _empty()

    if not settings.ANTHROPIC_API_KEY:
        return _empty()

    county_key = f"{county.lower()}_{state.upper()}"

    # Resolve assessor URL
    url = assessor_url or _KNOWN_ASSESSOR_URLS.get(county_key)
    if not url:
        return _empty()

    try:
        from src.api.middleware.security import add_scrape_domain
        from src.scrapers.base_scraper import BridgeScraper
        from urllib.parse import urlparse

        domain = urlparse(url).hostname
        if domain:
            add_scrape_domain(domain)

        async with BridgeScraper() as scraper:
            await scraper.navigate(url)
            await scraper.page.wait_for_timeout(2_000)

            # Take screenshot for Claude
            screenshot = await scraper.page.screenshot(type="png")

            # Ask Claude to analyze the page and search for our parcel
            user_msg = (
                f"I need to find property data for parcel number: {parcel_id}\n\n"
                f"County: {county}, State: {state}\n\n"
                "Look at the screenshot. If there's a search form, tell me how to "
                "fill in the parcel number and submit. If there are results visible, "
                "extract the property address and mailing address."
            )

            result = await ask_claude(
                _SYSTEM_PROMPT,
                user_msg,
                images=[screenshot],
                max_tokens=1024,
            )

            response_text = result.get("text", "")
            parsed = _parse_json_response(response_text)

            if not parsed:
                _logger.warning("AI assessor: could not parse Claude response for %s", parcel_id)
                return _empty()

            mode = parsed.get("mode", "")

            # If Claude returned navigation actions, execute them
            if mode == "navigate":
                actions = parsed.get("actions", [])
                for action in actions:
                    # Replace placeholder with actual parcel ID
                    if action.get("value") == "PARCEL_ID":
                        action["value"] = parcel_id
                    if action.get("js"):
                        action["js"] = action["js"].replace("PARCEL_ID", parcel_id)

                from src.scrapers.ai.navigator import _execute_action

                for action in actions:
                    await _execute_action(scraper.page, action)

                await scraper.page.wait_for_timeout(3_000)

                # Take new screenshot and extract results
                screenshot2 = await scraper.page.screenshot(type="png")
                result2 = await ask_claude(
                    _SYSTEM_PROMPT,
                    f"I searched for parcel {parcel_id}. Extract the property address "
                    f"and mailing address from the results shown.",
                    images=[screenshot2],
                    max_tokens=1024,
                )

                parsed = _parse_json_response(result2.get("text", ""))
                if not parsed:
                    return _empty()

            # Extract addresses from parsed response
            property_address = parsed.get("property_address")
            mailing_address = parsed.get("mailing_address")

            if property_address:
                property_address = str(property_address).strip()
                if property_address.lower() in ("null", "none", ""):
                    property_address = None

            if mailing_address:
                mailing_address = str(mailing_address).strip()
                if mailing_address.lower() in ("null", "none", ""):
                    mailing_address = None

            if not mailing_address and property_address:
                mailing_address = property_address

            if property_address:
                _logger.info(
                    "AI assessor enriched parcel %s: %s",
                    parcel_id, property_address,
                )

            return {
                "property_address": property_address,
                "mailing_address": mailing_address,
            }

    except Exception as exc:
        _logger.warning("AI assessor failed for parcel %s: %s", parcel_id, str(exc)[:80])
        return _empty()


def _parse_json_response(text: str) -> dict | None:
    """Extract JSON from Claude's response, handling markdown code blocks."""
    text = text.strip()

    # Strip markdown code blocks
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (```json and ```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                return None
    return None


def _empty() -> dict[str, str | None]:
    return {"property_address": None, "mailing_address": None}
