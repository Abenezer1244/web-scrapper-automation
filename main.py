#!/usr/bin/env python3
"""
Web Scraper Automation - Entry Point

Usage:
    python main.py [--scraper SCRAPER] [--format FORMAT] [--pages PAGES] [--no-headless]
"""

import argparse
import sys

from src.config import Settings
from src.utils import setup_logger, DataExporter
from src.scrapers.example_scraper import ExampleScraper


def parse_args():
    parser = argparse.ArgumentParser(description='Web Scraper Automation')
    parser.add_argument(
        '--scraper', default='example',
        choices=['example'],
        help='Scraper to run (default: example)'
    )
    parser.add_argument(
        '--format', default=None,
        choices=['csv', 'json', 'excel'],
        help=f'Export format (default: {Settings.EXPORT_FORMAT})'
    )
    parser.add_argument(
        '--pages', type=int, default=5,
        help='Number of pages to scrape (default: 5)'
    )
    parser.add_argument(
        '--no-headless', action='store_true',
        help='Run browser in visible mode'
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logger = setup_logger('main')

    logger.info("Starting web scraper automation")

    headless = not args.no_headless
    export_format = args.format or Settings.EXPORT_FORMAT
    exporter = DataExporter()

    if args.scraper == 'example':
        logger.info(f"Running ExampleScraper (pages={args.pages}, headless={headless})")
        with ExampleScraper(headless=headless) as scraper:
            data = scraper.scrape_all(max_pages=args.pages)

        if data:
            output_path = exporter.export(data, filename='quotes', format=export_format)
            logger.info(f"Export complete: {output_path}")
        else:
            logger.warning("No data was scraped.")
            return 1

    logger.info("Scraping finished successfully")
    return 0


if __name__ == '__main__':
    sys.exit(main())
