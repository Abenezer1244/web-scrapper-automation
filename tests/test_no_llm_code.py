"""AI mode removal, Phase 3: the product carries no LLM code.

The Claude-driven scraper package and the Claude assessor fallback are deleted
(the fallback was already off in production: AI_ENRICHMENT_ENABLED=false). The
built-in assessor URLs the PACS enrichment needs moved to a plain module.
"""
import importlib
from pathlib import Path

import pytest

from src.config import settings

_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", ["src.scrapers.ai", "src.scrapers.enrichment.ai_assessor"])
def test_the_llm_modules_are_gone(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_no_llm_dependency():
    lines = (_ROOT / "requirements.txt").read_text(encoding="utf-8").lower().splitlines()
    assert not [ln for ln in lines if ln.strip().startswith(("anthropic", "openai"))]


@pytest.mark.parametrize("name", [
    "ANTHROPIC_API_KEY", "AI_MODEL", "AI_MAX_TOKENS", "AI_SCRAPER_ENABLED",
    "AI_COST_ALERT_THRESHOLD", "AI_ENRICHMENT_ENABLED",
])
def test_no_ai_settings(name):
    assert not hasattr(settings, name)


def test_the_pacs_fallback_urls_survive():
    from src.scrapers.enrichment.assessor_urls import KNOWN_ASSESSOR_URLS

    assert KNOWN_ASSESSOR_URLS["island_WA"].startswith("https://assessor.islandcountywa.gov/")
    assert "pierce_WA" in KNOWN_ASSESSOR_URLS
