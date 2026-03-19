"""AI pagination handler: uses Claude to identify and click next page controls.

Handles the wide variety of pagination styles across county websites:
- "Next" links/buttons
- Numbered page links
- ">" arrows
- ASP.NET postback pagination
"""

import json

from playwright.async_api import Page

from src.scrapers.ai.client import ask_claude
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.ai.paginator")

_SYSTEM_PROMPT = """You are a pagination detection agent for county public records search results.
You will receive a screenshot of a search results page.
Your job is to determine if there is a "next page" control and how to click it.

Return ONLY valid JSON. No markdown fences, no explanation.

Return an object with:
- "has_next": boolean — true if there are more pages of results
- "action": object or null — the action to go to the next page (null if has_next is false)

The action object should be one of:
- {"action": "click", "selector": "<css selector>"}
- {"action": "evaluate", "js": "<javascript code>"}

Rules:
- Look for: "Next" links, ">" arrows, numbered page links where the next number is clickable.
- If the current page appears to be the last page (Next is disabled, greyed out, or missing), set has_next to false.
- If there are no results at all, set has_next to false.
- Prefer simple CSS selectors. For ASP.NET postback links, use the "evaluate" action with JS."""


async def ai_has_next_page(page: Page) -> tuple[bool, dict | None, dict]:
    """Check if the results page has a next page control.

    Args:
        page: Playwright page showing search results.

    Returns:
        Tuple of (has_next, action_to_execute_or_None, ai_usage)
    """
    screenshot = await page.screenshot(type="png", full_page=True)

    response = await ask_claude(
        system_prompt=_SYSTEM_PROMPT,
        user_message="Does this results page have a next page? If yes, how do I navigate to it?",
        images=[screenshot],
        max_tokens=512,
    )

    ai_usage = {
        "input_tokens": response["input_tokens"],
        "output_tokens": response["output_tokens"],
        "cost_usd": response["cost_usd"],
    }

    result = _parse_pagination(response["text"])
    has_next = result.get("has_next", False)
    action = result.get("action")

    _logger.info("Pagination check: has_next=%s", has_next)
    return has_next, action, ai_usage


async def ai_go_next_page(page: Page, action: dict) -> None:
    """Execute the pagination action to go to the next page.

    Args:
        page: Playwright page.
        action: Action dict from ai_has_next_page.
    """
    action_type = action.get("action", "")

    if action_type == "click":
        selector = action.get("selector", "")
        el = page.locator(selector).first
        await el.scroll_into_view_if_needed(timeout=10_000)
        await el.click(timeout=10_000)
        await page.wait_for_load_state("load")
        await page.wait_for_timeout(2_000)
        _logger.info("Navigated to next page via click: %s", selector)

    elif action_type == "evaluate":
        js = action.get("js", "")
        await page.evaluate(js)
        await page.wait_for_load_state("load")
        await page.wait_for_timeout(2_000)
        _logger.info("Navigated to next page via JS evaluate")

    else:
        _logger.warning("Unknown pagination action: %s", action_type)


def _parse_pagination(text: str) -> dict:
    """Parse Claude's pagination response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        _logger.error("Failed to parse pagination response: %s", text[:200])

    return {"has_next": False, "action": None}
