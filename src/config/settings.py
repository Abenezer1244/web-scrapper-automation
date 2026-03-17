import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class Settings:
    """Global configuration settings for the scraper."""

    # Project paths
    BASE_DIR = Path(__file__).parent.parent.parent
    DATA_DIR = BASE_DIR / 'data'
    EXPORTS_DIR = DATA_DIR / 'exports'
    LOGS_DIR = BASE_DIR / 'logs'

    # Browser configuration
    HEADLESS = os.getenv('HEADLESS', 'true').lower() == 'true'
    BROWSER = os.getenv('BROWSER', 'chrome')

    # Scraping configuration
    DEFAULT_TIMEOUT = int(os.getenv('DEFAULT_TIMEOUT', 10))
    MAX_RETRIES = int(os.getenv('MAX_RETRIES', 3))
    RETRY_DELAY = int(os.getenv('RETRY_DELAY', 2))

    # Export configuration
    EXPORT_FORMAT = os.getenv('EXPORT_FORMAT', 'csv')
    EXPORT_DIR = os.getenv('EXPORT_DIR', 'data/exports')

    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_DIR = os.getenv('LOG_DIR', 'logs')

    @classmethod
    def ensure_dirs(cls):
        """Create necessary directories if they don't exist."""
        cls.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        cls.LOGS_DIR.mkdir(parents=True, exist_ok=True)
