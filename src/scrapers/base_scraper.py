import time
import logging
from typing import Optional
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.service import Service as FirefoxService
from bs4 import BeautifulSoup

from src.config import Settings

logger = logging.getLogger(__name__)


class BaseScraper:
    """Base class for web scrapers using Selenium."""

    def __init__(self, headless: Optional[bool] = None, browser: str = 'chrome'):
        """
        Initialize the scraper.

        Args:
            headless: Run browser in headless mode. Defaults to Settings.HEADLESS.
            browser: Browser to use ('chrome' or 'firefox'). Defaults to 'chrome'.
        """
        self.headless = headless if headless is not None else Settings.HEADLESS
        self.browser = browser.lower()
        self.driver = None
        self.wait = None
        Settings.ensure_dirs()

    def _setup_driver(self):
        """Set up the Selenium WebDriver."""
        if self.browser == 'chrome':
            options = webdriver.ChromeOptions()
            if self.headless:
                options.add_argument('--headless')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_experimental_option('excludeSwitches', ['enable-logging'])

            service = ChromeService(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)

        elif self.browser == 'firefox':
            options = webdriver.FirefoxOptions()
            if self.headless:
                options.add_argument('--headless')

            service = FirefoxService(GeckoDriverManager().install())
            self.driver = webdriver.Firefox(service=service, options=options)

        else:
            raise ValueError(f"Unsupported browser: {self.browser}")

        self.wait = WebDriverWait(self.driver, Settings.DEFAULT_TIMEOUT)
        logger.info(f"WebDriver initialized: {self.browser} (headless={self.headless})")

    def start(self):
        """Start the browser."""
        if self.driver is None:
            self._setup_driver()

    def stop(self):
        """Stop the browser and clean up."""
        if self.driver:
            self.driver.quit()
            self.driver = None
            self.wait = None
            logger.info("WebDriver closed")

    def get_page(self, url: str, retries: int = None) -> bool:
        """
        Navigate to a URL with retry logic.

        Args:
            url: The URL to navigate to.
            retries: Number of retries. Defaults to Settings.MAX_RETRIES.

        Returns:
            True if successful, False otherwise.
        """
        if not self.driver:
            self.start()

        retries = retries if retries is not None else Settings.MAX_RETRIES

        for attempt in range(retries):
            try:
                self.driver.get(url)
                logger.info(f"Successfully loaded: {url}")
                return True
            except WebDriverException as e:
                logger.warning(f"Attempt {attempt + 1}/{retries} failed for {url}: {e}")
                if attempt < retries - 1:
                    time.sleep(Settings.RETRY_DELAY)

        logger.error(f"Failed to load {url} after {retries} attempts")
        return False

    def wait_for_element(self, by: By, value: str, timeout: int = None) -> Optional[any]:
        """
        Wait for an element to be present on the page.

        Args:
            by: Selenium By locator strategy.
            value: The value to locate.
            timeout: Custom timeout in seconds.

        Returns:
            The element if found, None otherwise.
        """
        timeout = timeout if timeout is not None else Settings.DEFAULT_TIMEOUT
        try:
            wait = WebDriverWait(self.driver, timeout)
            element = wait.until(EC.presence_of_element_located((by, value)))
            return element
        except TimeoutException:
            logger.warning(f"Element not found: {by}={value}")
            return None

    def get_page_source(self) -> str:
        """Get the current page source."""
        return self.driver.page_source if self.driver else ""

    def get_soup(self) -> BeautifulSoup:
        """Get BeautifulSoup object of current page."""
        return BeautifulSoup(self.get_page_source(), 'lxml')

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()
