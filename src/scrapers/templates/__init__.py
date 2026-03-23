"""Template scrapers for common county recorder platforms.

Instead of burning Claude AI tokens on every county, template scrapers
handle standardized platforms with zero AI cost.

Supported platforms:
- EagleWeb (Tyler Technologies) — 16+ WA counties
- LandmarkWeb (Hyland) — King County (largest in WA)
- AcclaimWeb (Tyler) — 3 WA counties
"""

from src.scrapers.templates.acclaimweb import AcclaimWebScraper
from src.scrapers.templates.eagleweb import EagleWebScraper
from src.scrapers.templates.landmarkweb import LandmarkWebScraper

__all__ = ["AcclaimWebScraper", "EagleWebScraper", "LandmarkWebScraper"]
