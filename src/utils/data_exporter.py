"""Data exporter: CSV / Excel / JSON with CSV injection sanitization and R2 upload."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
from botocore.config import Config

from src.api.middleware.security import sanitize_for_csv
from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("exporter")

# Column display order for lead exports
_COLUMN_ORDER = [
    "date_recorded",
    "party_name",
    "heirs",
    "legal_description",
    "parcel_id",
    "property_address",
    "mailing_address",
]

# Amber header colour for Excel (matches BridgeLeads design system)
_AMBER_HEX = "F5A623"


def _get_r2_client():
    """Return a boto3 S3 client configured for Cloudflare R2."""
    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT_URL,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _build_dataframe(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Build a sanitized DataFrame from a list of result dicts.

    Applies CSV injection sanitization to all string fields.
    Columns are ordered per _COLUMN_ORDER; extra columns appended at end.
    """
    if not records:
        return pd.DataFrame(columns=_COLUMN_ORDER)

    df = pd.DataFrame(records)

    # Apply CSV injection sanitization to every string-valued cell
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].apply(
                lambda v: sanitize_for_csv(str(v)) if v is not None else ""
            )

    # Re-order columns: known columns first, then any extras
    ordered = [c for c in _COLUMN_ORDER if c in df.columns]
    extras = [c for c in df.columns if c not in _COLUMN_ORDER]
    df = df[ordered + extras]

    return df


class DataExporter:
    """Export lead records to CSV / Excel / JSON and upload to Cloudflare R2."""

    def __init__(self, export_dir: str | None = None) -> None:
        self.export_dir = Path(export_dir) if export_dir else settings.EXPORTS_DIR
        self.export_dir.mkdir(parents=True, exist_ok=True)

    # ─── Local file export ────────────────────────────────────────────────────

    def to_csv(self, records: list[dict[str, Any]], filename: str = "export") -> Path:
        """Export records to a sanitized CSV file."""
        filepath = self._timestamped_path(filename, "csv")
        df = _build_dataframe(records)
        df.to_csv(filepath, index=False, encoding="utf-8")
        _logger.info("CSV exported: %s (%d rows)", filepath.name, len(df))
        return filepath

    def to_excel(self, records: list[dict[str, Any]], filename: str = "export") -> Path:
        """Export records to an Excel file with amber header row."""
        filepath = self._timestamped_path(filename, "xlsx")
        df = _build_dataframe(records)

        with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Leads")
            ws = writer.sheets["Leads"]

            # Style header row: amber background, bold white text
            from openpyxl.styles import Alignment, Font, PatternFill
            header_fill = PatternFill(fill_type="solid", fgColor=_AMBER_HEX)
            header_font = Font(bold=True, color="FFFFFF")
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")

            # Auto-fit column widths
            for col in ws.columns:
                max_len = max((len(str(cell.value or "")) for cell in col), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)

        _logger.info("Excel exported: %s (%d rows)", filepath.name, len(df))
        return filepath

    def to_json(self, records: list[dict[str, Any]], filename: str = "export") -> Path:
        """Export records to JSON (orient=records)."""
        filepath = self._timestamped_path(filename, "json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2, default=str)
        _logger.info("JSON exported: %s (%d rows)", filepath.name, len(records))
        return filepath

    def export(
        self,
        records: list[dict[str, Any]],
        filename: str = "export",
        fmt: str | None = None,
    ) -> Path:
        """Export to the given format. Single entry point for all callers."""
        fmt = (fmt or settings.EXPORT_FORMAT).lower()
        if fmt == "csv":
            return self.to_csv(records, filename)
        if fmt == "json":
            return self.to_json(records, filename)
        if fmt in ("excel", "xlsx"):
            return self.to_excel(records, filename)
        raise ValueError(f"Unsupported export format: {fmt}")

    # ─── R2 upload ────────────────────────────────────────────────────────────

    def upload_to_r2(self, local_path: Path, object_key: str) -> str:
        """Upload a local file to Cloudflare R2 and return the object key.

        Args:
            local_path: Path to the local file.
            object_key: S3-style key (e.g. 'exports/job_id/leads.csv').

        Returns:
            The object key stored in R2.
        """
        client = _get_r2_client()
        content_types = {
            ".csv": "text/csv",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".json": "application/json",
        }
        content_type = content_types.get(local_path.suffix, "application/octet-stream")

        with open(local_path, "rb") as f:
            client.put_object(
                Bucket=settings.R2_BUCKET_NAME,
                Key=object_key,
                Body=f,
                ContentType=content_type,
                ContentDisposition=f'attachment; filename="{local_path.name}"',
            )
        _logger.info("Uploaded to R2: %s", object_key)
        return object_key

    def get_download_url(self, object_key: str, expires_in: int = 3600) -> str:
        """Generate a pre-signed download URL for an R2 object.

        Args:
            object_key: The R2 object key.
            expires_in: URL expiry in seconds (default: 1hr for in-app, use 172800 for email).

        Returns:
            Pre-signed HTTPS URL.
        """
        client = _get_r2_client()
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.R2_BUCKET_NAME, "Key": object_key},
            ExpiresIn=expires_in,
        )
        return url

    # ─── Helper ───────────────────────────────────────────────────────────────

    def _timestamped_path(self, base_name: str, extension: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.export_dir / f"{base_name}_{timestamp}.{extension}"
