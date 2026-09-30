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


def test_the_pacs_fallback_urls_moved_with_identical_values():
    from src.scrapers.enrichment.assessor_urls import KNOWN_ASSESSOR_URLS

    assert KNOWN_ASSESSOR_URLS == {
        "pierce_WA": "https://atip.piercecountywa.gov/app/parcelSearch",
        "island_WA": "https://assessor.islandcountywa.gov/propertyaccess/?cid=0",
    }


def test_settings_still_load_with_the_retired_variables_set(monkeypatch):
    """Railway keeps the old variables until the owner deletes them after the
    rollback window; the new image must boot with them present."""
    from src.config.settings import Settings

    for name, value in (("ANTHROPIC_API_KEY", "sk-ant-x"), ("AI_ENRICHMENT_ENABLED", "false"),
                        ("AI_SCRAPER_ENABLED", "true"), ("AI_MODEL", "m")):
        monkeypatch.setenv(name, value)
    loaded = Settings()
    assert not hasattr(loaded, "ANTHROPIC_API_KEY")


def test_nothing_in_the_code_refers_to_the_deleted_llm_path():
    """Repository-wide, not just the modules the import-cycle test loads: a lazy
    import inside a function would only fail when it ran."""
    needles = ("src.scrapers.ai", "scrapers.ai.", "ai_assessor", "anthropic", "ai_cost",
               "ask_claude")
    hits = []
    for base in ("src", "scripts", "main.py"):
        root = _ROOT / base
        files = [root] if root.is_file() else root.rglob("*.py")
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            hits += [f"{path.relative_to(_ROOT)}: {n}" for n in needles if n in text]
    assert hits == []
