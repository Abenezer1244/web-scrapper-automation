"""Template scrapers for common county recorder platforms.

Instead of burning Claude AI tokens on every county, template scrapers
handle standardized platforms with zero AI cost.

Supported platforms:
- EagleWeb (Tyler Technologies) — 16+ WA counties
- LandmarkWeb (Hyland) — 3 WA counties (has reCAPTCHA on some)
- AcclaimWeb (Tyler) — 3 WA counties
"""

from src.scrapers.templates.eagleweb import EagleWebScraper

__all__ = ["EagleWebScraper"]
