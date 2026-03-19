"""Claude API client for AI-powered scraping.

Handles sending screenshots + text prompts to Claude and parsing responses.
Includes rate limit handling with exponential backoff and token usage logging.
"""

import asyncio
import base64
import time

import anthropic

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.ai.client")

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to your .env file to enable AI-powered extraction."
            )
        _client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


async def ask_claude(
    system_prompt: str,
    user_message: str,
    images: list[bytes] | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Send a prompt with optional screenshots to Claude and return the response.

    Args:
        system_prompt: The system-level instruction for Claude.
        user_message: The user-level message/question.
        images: Optional list of PNG screenshot bytes to include.
        max_tokens: Override for max response tokens.

    Returns:
        Dict with keys:
        - text: Claude's response text
        - input_tokens: Number of input tokens used
        - output_tokens: Number of output tokens used
        - cost_usd: Estimated cost in USD
    """
    client = _get_client()
    model = settings.AI_MODEL
    tokens = max_tokens or settings.AI_MAX_TOKENS

    # Build the message content with optional images
    content = []
    for img_bytes in (images or []):
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(img_bytes).decode("ascii"),
            },
        })
    content.append({"type": "text", "text": user_message})

    # Retry with exponential backoff on rate limits
    for attempt in range(1, settings.MAX_RETRIES + 1):
        try:
            start = time.monotonic()
            response = await client.messages.create(
                model=model,
                max_tokens=tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )
            elapsed = time.monotonic() - start

            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost = _estimate_cost(model, input_tokens, output_tokens)
            text = response.content[0].text if response.content else ""

            _logger.info(
                "Claude response: %d input + %d output tokens, $%.4f, %.1fs",
                input_tokens, output_tokens, cost, elapsed,
            )

            return {
                "text": text,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
            }

        except anthropic.RateLimitError:
            wait = 2 ** attempt
            _logger.warning("Claude rate limited (attempt %d/%d). Waiting %ds.", attempt, settings.MAX_RETRIES, wait)
            if attempt == settings.MAX_RETRIES:
                raise
            await asyncio.sleep(wait)

        except anthropic.APIStatusError as exc:
            _logger.error("Claude API error: %s %s", exc.status_code, exc.message)
            raise


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost based on model pricing.

    Pricing as of 2025 (per 1M tokens):
    - claude-sonnet-4-6: $3 input, $15 output
    - claude-haiku-4-5:  $0.80 input, $4 output
    - claude-opus-4-6:   $15 input, $75 output
    """
    pricing = {
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (0.80, 4.0),
        "claude-opus-4-6": (15.0, 75.0),
    }
    input_rate, output_rate = pricing.get(model, (3.0, 15.0))
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
