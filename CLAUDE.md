# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## ALERT

**THIS IS NOT A MOCK OR TEST OR DUMMY PROJECT. IT IS A REAL WORLD ENTERPRISE LEVEL SAAS SO NEVER ADD MOCK, TEST, OR DUMMY CODE.**

---

## Working Instructions

When reading files, read the whole file chunk by chunk to ensure nothing is missed.

1. First think through the problem, read the codebase for relevant files, and write a plan to `tasks/todo.md`.
2. The plan should have a list of todo items that you can check off as you complete them.
3. Before beginning work, check in with the user to verify the plan.
4. Then begin working on the todo items, marking them as complete as you go.
5. At every step, give a high level explanation of what changes were made.
6. Make every task and code change as simple as possible. Avoid massive or complex changes. Every change should impact as little code as possible. Everything is about simplicity.
7. Finally, add a review section to `tasks/todo.md` with a summary of the changes made and any notes.

---

## Project Overview

Web scraper automation framework built on Selenium + BeautifulSoup. Supports multi-page scraping, retry logic, colored logging, and data export to CSV, JSON, or Excel.

## Project Structure

```
web-scrapper-automation/
├── main.py                        # CLI entry point
├── requirements.txt
├── .env.example                   # Copy to .env and configure
├── src/
│   ├── config/
│   │   └── settings.py            # Central settings (reads from .env)
│   ├── scrapers/
│   │   ├── base_scraper.py        # BaseScraper (Selenium + BS4)
│   │   └── example_scraper.py     # Example: quotes.toscrape.com
│   └── utils/
│       ├── data_exporter.py       # CSV / JSON / Excel export
│       └── logger.py              # Colored console + file logging
├── tests/
│   ├── test_settings.py
│   └── test_data_exporter.py
├── data/exports/                  # Scrape output files (git-ignored)
└── logs/                          # Log files (git-ignored)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

## Running the Scraper

```bash
# Run with defaults (headless Chrome, CSV export, 5 pages)
python main.py

# Custom options
python main.py --scraper example --format json --pages 10 --no-headless
```

## Running Tests

```bash
pytest
pytest --cov=src tests/
```

## Adding a New Scraper

1. Create `src/scrapers/my_scraper.py` extending `BaseScraper`
2. Implement `scrape_page()` and any multi-page logic
3. Export it from `src/scrapers/__init__.py`
4. Wire it into `main.py` behind a `--scraper` flag

## Environment Variables

See `.env.example` for all available options:
- `HEADLESS` — run browser headlessly (default: true)
- `BROWSER` — `chrome` or `firefox` (default: chrome)
- `DEFAULT_TIMEOUT` — element wait timeout in seconds (default: 10)
- `MAX_RETRIES` — page load retries (default: 3)
- `EXPORT_FORMAT` — `csv`, `json`, or `excel` (default: csv)
