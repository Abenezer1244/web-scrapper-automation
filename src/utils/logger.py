import logging
import colorlog
from pathlib import Path
from datetime import datetime

from src.config import Settings


def setup_logger(name: str = None, log_file: bool = True) -> logging.Logger:
    """
    Set up a logger with colored console output and optional file logging.

    Args:
        name: Logger name. If None, returns root logger.
        log_file: Whether to log to file.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, Settings.LOG_LEVEL))

    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger

    # Console handler with colors
    console_handler = colorlog.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_formatter = colorlog.ColoredFormatter(
        '%(log_color)s%(levelname)-8s%(reset)s %(blue)s%(name)s%(reset)s - %(message)s',
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        }
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler
    if log_file:
        Settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_filename = f"scraper_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(
            Settings.LOGS_DIR / log_filename,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    return logger
