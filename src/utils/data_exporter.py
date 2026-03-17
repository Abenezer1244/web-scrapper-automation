import json
import csv
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
import pandas as pd
import logging

from src.config import Settings

logger = logging.getLogger(__name__)


class DataExporter:
    """Utility class for exporting scraped data to various formats."""

    def __init__(self, export_dir: str = None):
        """
        Initialize the data exporter.

        Args:
            export_dir: Directory to save exports. Defaults to Settings.EXPORTS_DIR.
        """
        self.export_dir = Path(export_dir) if export_dir else Settings.EXPORTS_DIR
        self.export_dir.mkdir(parents=True, exist_ok=True)

    def _generate_filename(self, base_name: str, extension: str) -> Path:
        """
        Generate a timestamped filename.

        Args:
            base_name: Base name for the file.
            extension: File extension (without dot).

        Returns:
            Full path to the file.
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{base_name}_{timestamp}.{extension}"
        return self.export_dir / filename

    def to_csv(self, data: List[Dict[str, Any]], filename: str = 'export') -> Path:
        """
        Export data to CSV file.

        Args:
            data: List of dictionaries containing the data.
            filename: Base filename without extension.

        Returns:
            Path to the created file.
        """
        if not data:
            logger.warning("No data to export to CSV")
            return None

        filepath = self._generate_filename(filename, 'csv')

        try:
            df = pd.DataFrame(data)
            df.to_csv(filepath, index=False, encoding='utf-8')
            logger.info(f"Data exported to CSV: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Failed to export CSV: {e}")
            raise

    def to_json(self, data: List[Dict[str, Any]], filename: str = 'export', indent: int = 2) -> Path:
        """
        Export data to JSON file.

        Args:
            data: List of dictionaries containing the data.
            filename: Base filename without extension.
            indent: JSON indentation level.

        Returns:
            Path to the created file.
        """
        if not data:
            logger.warning("No data to export to JSON")
            return None

        filepath = self._generate_filename(filename, 'json')

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=indent, ensure_ascii=False)
            logger.info(f"Data exported to JSON: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Failed to export JSON: {e}")
            raise

    def to_excel(self, data: List[Dict[str, Any]], filename: str = 'export', sheet_name: str = 'Sheet1') -> Path:
        """
        Export data to Excel file.

        Args:
            data: List of dictionaries containing the data.
            filename: Base filename without extension.
            sheet_name: Name of the Excel sheet.

        Returns:
            Path to the created file.
        """
        if not data:
            logger.warning("No data to export to Excel")
            return None

        filepath = self._generate_filename(filename, 'xlsx')

        try:
            df = pd.DataFrame(data)
            df.to_excel(filepath, index=False, sheet_name=sheet_name, engine='openpyxl')
            logger.info(f"Data exported to Excel: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Failed to export Excel: {e}")
            raise

    def export(self, data: List[Dict[str, Any]], filename: str = 'export', format: str = None) -> Path:
        """
        Export data to the specified format.

        Args:
            data: List of dictionaries containing the data.
            filename: Base filename without extension.
            format: Export format ('csv', 'json', or 'excel'). Defaults to Settings.EXPORT_FORMAT.

        Returns:
            Path to the created file.
        """
        format = (format or Settings.EXPORT_FORMAT).lower()

        if format == 'csv':
            return self.to_csv(data, filename)
        elif format == 'json':
            return self.to_json(data, filename)
        elif format in ['excel', 'xlsx']:
            return self.to_excel(data, filename)
        else:
            raise ValueError(f"Unsupported export format: {format}")
