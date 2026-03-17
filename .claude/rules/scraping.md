---
description: Rules for building and modifying scrapers
globs: src/scrapers/**/*.py
---

# Scraper Rules

- Always extend `BaseScraper` — never create standalone scrapers
- Never hardcode timeouts; use `Settings.DEFAULT_TIMEOUT`
- Never hardcode retry counts; use `Settings.MAX_RETRIES`
- Use `self.get_soup()` for HTML parsing, not raw `driver.page_source`
- Always use the context manager (`with MyScraper() as s:`) to ensure `driver.quit()` is called
- `scrape_page()` must return a list of dicts with consistent keys across all pages
- Multi-page logic belongs in the scraper class, not in `main.py`
- Log meaningful progress at INFO level; log errors with full context at ERROR level
- Never store credentials, cookies, or session tokens in scraper files
