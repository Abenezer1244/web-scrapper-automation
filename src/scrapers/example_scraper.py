import logging
from selenium.webdriver.common.by import By
from typing import List, Dict, Any

from src.scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class ExampleScraper(BaseScraper):
    """Example scraper that collects quotes from quotes.toscrape.com."""

    BASE_URL = "https://quotes.toscrape.com"

    def scrape_page(self) -> List[Dict[str, Any]]:
        """
        Scrape quotes from the current page.

        Returns:
            List of dicts with 'text', 'author', and 'tags' keys.
        """
        soup = self.get_soup()
        quotes = []

        for quote_div in soup.select('div.quote'):
            text = quote_div.select_one('span.text')
            author = quote_div.select_one('small.author')
            tags = quote_div.select('a.tag')

            quotes.append({
                'text': text.get_text(strip=True) if text else '',
                'author': author.get_text(strip=True) if author else '',
                'tags': ', '.join(t.get_text(strip=True) for t in tags),
            })

        logger.info(f"Scraped {len(quotes)} quotes from current page")
        return quotes

    def scrape_all(self, max_pages: int = 5) -> List[Dict[str, Any]]:
        """
        Scrape quotes across multiple pages.

        Args:
            max_pages: Maximum number of pages to scrape.

        Returns:
            Combined list of quotes from all pages.
        """
        all_quotes = []

        for page in range(1, max_pages + 1):
            url = f"{self.BASE_URL}/page/{page}/"
            if not self.get_page(url):
                logger.warning(f"Could not load page {page}, stopping.")
                break

            quotes = self.scrape_page()
            if not quotes:
                logger.info("No more quotes found, stopping.")
                break

            all_quotes.extend(quotes)
            logger.info(f"Total quotes collected: {len(all_quotes)}")

        return all_quotes
