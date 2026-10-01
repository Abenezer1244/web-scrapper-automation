"""Template scrapers for common county recorder platforms.

One template scraper per standardized recorder platform: a county on a supported
platform needs only its portal URL, no county-specific code.

Supported platforms:
- EagleWeb (Tyler Technologies) — 16+ WA counties
- LandmarkWeb (Hyland) — King County (largest in WA)
- AcclaimWeb (Tyler) — 3 WA counties
- AVA Fidlar — Yakima County (Angular SPA)
"""

from src.scrapers.templates.acclaimweb import AcclaimWebScraper
from src.scrapers.templates.ava_fidlar import AvaFidlarScraper
from src.scrapers.templates.eagleweb import EagleWebScraper
from src.scrapers.templates.landmarkweb import LandmarkWebScraper

__all__ = ["AcclaimWebScraper", "AvaFidlarScraper", "EagleWebScraper", "LandmarkWebScraper"]
