"""AI navigation agent: uses Claude to fill and submit any county search form.

Given a Playwright page on a county public records portal, this module:
1. Takes a screenshot + accessibility snapshot
2. Asks Claude to identify form fields and return actions
3. Executes those actions via Playwright
4. Verifies results loaded
"""

import json

from playwright.async_api import Page

from src.scrapers.ai.client import ask_claude
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.ai.navigator")

_SYSTEM_PROMPT = """You are a browser automation agent for a county public records search system.
You will receive a screenshot and accessibility snapshot of a county government website.
Your job is to return a JSON array of actions to search for specific record types within a date range.

Return ONLY a valid JSON array. No markdown fences, no explanation outside the JSON.

Each action is an object with one of these shapes:
- {"action": "click", "selector": "<css or role selector>", "description": "<why>"}
- {"action": "fill", "selector": "<css or role selector>", "value": "<text to type>", "description": "<why>"}
- {"action": "check", "selector": "<css or role selector>", "description": "<why>"}
- {"action": "select", "selector": "<css or role selector>", "value": "<option text>", "description": "<why>"}
- {"action": "evaluate", "js": "<javascript code>", "description": "<why>"}
- {"action": "wait", "ms": 2000, "description": "<why>"}

Rules:
- If there is a disclaimer or terms page, include actions to accept it first.
- Use CSS selectors that are specific (IDs, unique attributes). Avoid fragile selectors.
- For date fields that use custom JS widgets, prefer the "evaluate" action with JS to set values.
- For checkbox lists with many items, use "evaluate" with JS to check the right box.
- End with a click on the search/submit button.
- Add a wait action (2000ms) after any page-changing action (disclaimer accept, form submit).
- Keep the array as short as possible. Only include necessary actions."""


async def ai_navigate_form(
    page: Page,
    record_type: str,
    date_from: str,
    date_to: str,
) -> dict:
    """Use Claude to fill and submit a county search form.

    Args:
        page: Playwright page currently on the search portal.
        record_type: Record type to search for (e.g. "probate").
        date_from: Start date in MM/DD/YYYY format.
        date_to: End date in MM/DD/YYYY format.

    Returns:
        Dict with keys: actions (list), ai_usage (dict with token/cost info)
    """
    # Take screenshot and get accessibility snapshot
    screenshot = await page.screenshot(type="png", full_page=True)
    snapshot = await _get_accessibility_snapshot(page)

    user_message = (
        f"I need to search for **{record_type.upper()}** records "
        f"from **{date_from}** to **{date_to}**.\n\n"
        f"Current URL: {page.url}\n\n"
        f"Page accessibility snapshot:\n```\n{snapshot}\n```\n\n"
        f"Return a JSON array of actions to fill and submit this search form."
    )

    response = await ask_claude(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_message,
        images=[screenshot],
    )

    actions = _parse_actions(response["text"])
    _logger.info("Claude returned %d navigation actions", len(actions))

    # Execute each action
    for i, action in enumerate(actions):
        _logger.info("Action %d/%d: %s — %s", i + 1, len(actions), action["action"], action.get("description", ""))
        await _execute_action(page, action)

    return {
        "actions": actions,
        "ai_usage": {
            "input_tokens": response["input_tokens"],
            "output_tokens": response["output_tokens"],
            "cost_usd": response["cost_usd"],
        },
    }


async def ai_handle_disclaimer(page: Page) -> dict | None:
    """Check if the current page has a disclaimer and accept it.

    Returns AI usage dict if Claude was called, None if no disclaimer detected.
    """
    screenshot = await page.screenshot(type="png", full_page=True)
    snapshot = await _get_accessibility_snapshot(page)

    # Quick check: does the page look like a disclaimer?
    disclaimer_keywords = ["disclaimer", "terms", "conditions", "agree", "accept", "acknowledge"]
    snapshot_lower = snapshot.lower()
    if not any(kw in snapshot_lower for kw in disclaimer_keywords):
        _logger.info("No disclaimer detected on page")
        return None

    user_message = (
        f"This page appears to have a disclaimer or terms page.\n"
        f"Current URL: {page.url}\n\n"
        f"Page accessibility snapshot:\n```\n{snapshot}\n```\n\n"
        f"Return a JSON array of actions to accept/acknowledge the disclaimer "
        f"and proceed to the main site. If this is NOT a disclaimer page, return an empty array []."
    )

    response = await ask_claude(
        system_prompt=_SYSTEM_PROMPT,
        user_message=user_message,
        images=[screenshot],
    )

    actions = _parse_actions(response["text"])
    if not actions:
        _logger.info("Claude confirmed: not a disclaimer page")
        return None

    _logger.info("Claude returned %d disclaimer actions", len(actions))
    for i, action in enumerate(actions):
        _logger.info("Disclaimer action %d/%d: %s", i + 1, len(actions), action.get("description", ""))
        await _execute_action(page, action)

    return {
        "input_tokens": response["input_tokens"],
        "output_tokens": response["output_tokens"],
        "cost_usd": response["cost_usd"],
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _get_accessibility_snapshot(page: Page) -> str:
    """Get a text representation of the page's accessibility tree.

    This gives Claude structural info about form fields, buttons, etc.
    without needing to parse raw HTML.
    """
    try:
        snapshot = await page.accessibility.snapshot()
        if snapshot:
            return _flatten_a11y(snapshot, depth=0, max_depth=4)
    except Exception:
        pass
    # Fallback: return page title + visible text summary
    title = await page.title()
    return f"Page title: {title}\n(Accessibility snapshot unavailable)"


def _flatten_a11y(node: dict, depth: int, max_depth: int) -> str:
    """Flatten an accessibility tree into a readable text format."""
    if depth > max_depth:
        return ""

    indent = "  " * depth
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")

    parts = [role]
    if name:
        parts.append(f'"{name}"')
    if value:
        parts.append(f'value="{value}"')

    line = f"{indent}{' '.join(parts)}"
    lines = [line]

    for child in node.get("children", []):
        lines.append(_flatten_a11y(child, depth + 1, max_depth))

    return "\n".join(lines)


def _parse_actions(text: str) -> list[dict]:
    """Parse Claude's response into a list of action dicts."""
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        actions = json.loads(text)
        if isinstance(actions, list):
            return actions
    except json.JSONDecodeError:
        _logger.error("Failed to parse Claude's response as JSON: %s", text[:200])

    return []


async def _execute_action(page: Page, action: dict) -> None:
    """Execute a single navigation action on the page."""
    action_type = action.get("action", "")
    selector = action.get("selector", "")
    value = action.get("value", "")

    if action_type == "click":
        el = page.locator(selector).first
        await el.scroll_into_view_if_needed(timeout=10_000)
        await el.click(timeout=10_000)

    elif action_type == "fill":
        el = page.locator(selector).first
        await el.scroll_into_view_if_needed(timeout=10_000)
        await el.fill(value, timeout=10_000)

    elif action_type == "check":
        el = page.locator(selector).first
        await el.scroll_into_view_if_needed(timeout=10_000)
        await el.check(timeout=10_000)

    elif action_type == "select":
        el = page.locator(selector).first
        await el.select_option(label=value, timeout=10_000)

    elif action_type == "evaluate":
        js_code = action.get("js", "")
        if js_code:
            await page.evaluate(js_code)

    elif action_type == "wait":
        ms = action.get("ms", 2000)
        await page.wait_for_timeout(ms)

    else:
        _logger.warning("Unknown action type: %s", action_type)
