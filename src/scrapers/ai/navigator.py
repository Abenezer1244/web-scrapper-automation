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
- STRONGLY prefer "evaluate" actions with JavaScript over CSS selectors. JS is far more reliable.
- For clicking elements, use: {"action": "evaluate", "js": "document.querySelector('...').click()"}
- For filling inputs, use: {"action": "evaluate", "js": "document.querySelector('...').value = '...'"}
- Only use "click" or "fill" actions for very simple, unique selectors like "#myId".
- NEVER use CSS selectors with spaces, backslashes, or special characters.
- For date fields, always use "evaluate" with JS to set values directly.
- USE THE CURRENTLY VISIBLE FORM. Do NOT try to navigate to other tabs or pages unless the current page has no search form at all.
- If the form already has date fields and a document type field, fill them directly.
- End with a submit action.
- Add a wait action (2000ms) after any page-changing action.
- Return at most 5-7 actions per step. I will take a new screenshot and ask for more.
- When the search form has been submitted and results are visible (or loading), return an empty array []."""


async def ai_navigate_form(
    page: Page,
    record_type: str,
    date_from: str,
    date_to: str,
    max_steps: int = 5,
) -> dict:
    """Use Claude to fill and submit a county search form via multi-step navigation.

    Instead of planning all actions upfront, this takes a screenshot after each
    batch of actions so Claude can see the updated page state. This handles
    JS-heavy sites where the page changes after clicks/tab switches.

    Args:
        page: Playwright page currently on the search portal.
        record_type: Record type to search for (e.g. "probate").
        date_from: Start date in MM/DD/YYYY format.
        date_to: End date in MM/DD/YYYY format.
        max_steps: Maximum number of Claude calls for navigation.

    Returns:
        Dict with keys: actions (list), ai_usage (dict with token/cost info)
    """
    all_actions = []
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    goal = (
        f"Search for **{record_type.upper()}** records "
        f"from **{date_from}** to **{date_to}**."
    )

    for step in range(max_steps):
        # Check for CAPTCHA before proceeding
        captcha_detected = await page.locator("iframe[src*='recaptcha'], iframe[title*='reCAPTCHA'], .g-recaptcha, .h-captcha").count()
        if captcha_detected > 0:
            _logger.error("CAPTCHA detected on page — cannot automate this site")
            raise RuntimeError(
                "This county website requires CAPTCHA verification and cannot be scraped automatically. "
                "Consider using a different data source for this county."
            )

        # Fresh screenshot + snapshot each step
        screenshot = await page.screenshot(type="png", full_page=True)
        snapshot = await _get_accessibility_snapshot(page)

        if step == 0:
            user_message = (
                f"Goal: {goal}\n\n"
                f"Current URL: {page.url}\n\n"
                f"Page accessibility snapshot:\n```\n{snapshot}\n```\n\n"
                f"Return a JSON array of actions to accomplish this goal. "
                f"Keep it short — only return the NEXT few actions needed from this page state. "
                f"If you need to navigate to a different tab/section first, just return those actions. "
                f"I will take a new screenshot after executing them and ask you for the next steps.\n\n"
                f"When the search has been submitted and results are loading, "
                f"return an empty array [] to signal you're done."
            )
        else:
            user_message = (
                f"Goal: {goal}\n\n"
                f"I've executed the previous actions. Here is the current page state.\n"
                f"Current URL: {page.url}\n\n"
                f"Page accessibility snapshot:\n```\n{snapshot}\n```\n\n"
                f"Actions executed so far: {len(all_actions)}\n\n"
                f"Return the NEXT actions needed. If the search form has been submitted "
                f"and results are visible (or loading), return an empty array []."
            )

        response = await ask_claude(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_message,
            images=[screenshot],
        )
        total_usage["input_tokens"] += response["input_tokens"]
        total_usage["output_tokens"] += response["output_tokens"]
        total_usage["cost_usd"] += response["cost_usd"]

        actions = _parse_actions(response["text"])
        _logger.info("Step %d: Claude returned %d actions", step + 1, len(actions))

        # Empty array = Claude says we're done
        if not actions:
            _logger.info("Claude signaled navigation complete")
            break

        # Execute this batch of actions
        for i, action in enumerate(actions):
            desc = action.get("description", action.get("action", "?"))
            _logger.info("  Action %d: %s — %s", i + 1, action["action"], desc)
            try:
                await _execute_action(page, action)
                all_actions.append(action)
            except Exception as exc:
                _logger.warning("  Action %d failed: %s — trying JS fallback", i + 1, str(exc)[:80])
                # Quick retry: ask Claude for a single corrected action
                retry_resp = await _retry_failed_action(page, action, desc, str(exc))
                total_usage["input_tokens"] += retry_resp["usage"]["input_tokens"]
                total_usage["output_tokens"] += retry_resp["usage"]["output_tokens"]
                total_usage["cost_usd"] += retry_resp["usage"]["cost_usd"]
                if retry_resp["action"]:
                    all_actions.append(retry_resp["action"])

    return {"actions": all_actions, "ai_usage": total_usage}


async def _retry_failed_action(page: Page, action: dict, desc: str, error: str) -> dict:
    """Ask Claude for a corrected action after a failure."""
    screenshot = await page.screenshot(type="png", full_page=True)
    snapshot = await _get_accessibility_snapshot(page)

    retry_msg = (
        f"Action failed: {error[:200]}\n"
        f"Failed action: {json.dumps(action)}\n\n"
        f"Current URL: {page.url}\n"
        f"Page snapshot:\n```\n{snapshot}\n```\n\n"
        f"Return ONE corrected action as a JSON object to accomplish: {desc}\n"
        f"Prefer 'evaluate' with JS for reliability."
    )
    resp = await ask_claude(
        system_prompt=_SYSTEM_PROMPT,
        user_message=retry_msg,
        images=[screenshot],
        max_tokens=512,
    )

    text = resp["text"].strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]

    result = {"action": None, "usage": {
        "input_tokens": resp["input_tokens"],
        "output_tokens": resp["output_tokens"],
        "cost_usd": resp["cost_usd"],
    }}

    try:
        retry_action = json.loads(text)
        if isinstance(retry_action, list) and retry_action:
            retry_action = retry_action[0]
        _logger.info("  Retry: %s", retry_action.get("description", retry_action.get("action")))
        await _execute_action(page, retry_action)
        result["action"] = retry_action
    except Exception as exc:
        _logger.error("  Retry also failed: %s — skipping", str(exc)[:80])

    return result


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
    """Get a DOM listing of all interactive elements with their attributes.

    This gives Claude the actual IDs, classes, and attributes needed to
    write correct CSS selectors or JS code.
    """
    try:
        dom_info = await page.evaluate("""
            () => {
                const elements = document.querySelectorAll(
                    'input, select, button, a, textarea, [role="button"], [role="link"], [role="tab"], [onclick]'
                );
                const results = [];
                for (const el of elements) {
                    if (el.offsetParent === null && el.type !== 'hidden') continue; // skip hidden
                    const info = {
                        tag: el.tagName.toLowerCase(),
                        id: el.id || null,
                        name: el.name || null,
                        type: el.type || null,
                        class: el.className ? el.className.substring(0, 80) : null,
                        text: (el.textContent || '').trim().substring(0, 60),
                        value: el.value ? el.value.substring(0, 40) : null,
                        placeholder: el.placeholder || null,
                        href: el.href ? el.href.substring(0, 80) : null,
                    };
                    // Remove null values for cleaner output
                    const clean = {};
                    for (const [k, v] of Object.entries(info)) {
                        if (v !== null && v !== '') clean[k] = v;
                    }
                    results.push(clean);
                }
                return JSON.stringify(results.slice(0, 80), null, 1);
            }
        """)
        return dom_info
    except Exception:
        pass
    # Fallback
    title = await page.title()
    return f"Page title: {title}\n(DOM snapshot unavailable)"


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

    # Try parsing as-is
    try:
        actions = json.loads(text)
        if isinstance(actions, list):
            return actions
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in the response (Claude sometimes adds extra text)
    import re
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            actions = json.loads(match.group())
            if isinstance(actions, list):
                return actions
        except json.JSONDecodeError:
            pass

    # Try fixing truncated JSON — add closing brackets
    for suffix in ["]", "}]", "\"}]"]:
        try:
            actions = json.loads(text + suffix)
            if isinstance(actions, list):
                _logger.warning("Fixed truncated JSON by appending %r", suffix)
                return actions
        except json.JSONDecodeError:
            continue

    _logger.error("Failed to parse Claude's response as JSON: %s", text[:300])
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
