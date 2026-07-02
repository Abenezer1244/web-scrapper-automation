"""Data exporter: CSV / Excel / JSON with CSV injection sanitization and R2 upload.

Lead CSV/Excel content (columns, phone normalization, name/address split,
sanitization) is owned by src/utils/lead_export.py so the in-app download and the
scheduled/R2 export produce the IDENTICAL dialer-ready file. This module owns only
file writing + R2 I/O. JSON stays raw-typed for API consumers (NOT canonicalized).
The DNC/TCPA disclaimer is NOT written into the CSV/Excel (a disclaimer row breaks
dialer import) — it's surfaced in the delivery email body + download UI instead.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests as _requests

from src.api.middleware.security import sanitize_for_csv
from src.config import settings
from src.utils.lead_export import (
    HIDEABLE_OUTPUT_FIELDS,
    LEAD_CSV_COLUMNS,
    _apply_visibility,
    build_lead_export_row,
    write_lead_csv,
)
from src.utils.logger import setup_logger

_logger = setup_logger("exporter")

# Amber header colour for Excel (matches BridgeLeads design system)
_AMBER_HEX = "F5A623"


def _r2_api_base() -> str:
    """Return the Cloudflare R2 API base URL for the configured account + bucket."""
    account_id = settings.R2_ACCOUNT_ID
    bucket = settings.R2_BUCKET_NAME
    return f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket}"


def _r2_headers() -> dict[str, str]:
    """Return auth headers for the Cloudflare R2 API."""
    return {"Authorization": f"Bearer {settings.R2_API_TOKEN}"}


def _sanitize_json_value(value: Any) -> Any:
    """Recursively CSV-sanitize every string in a JSON-serializable value.

    JSON exports are a common hand-off into spreadsheet tools, so a formula
    trigger anywhere in the tree (top-level column OR a string nested inside
    enrichment_data / the phones-emails arrays) must be neutralized, not just
    the top-level columns. dict keys are left untouched (export keys are
    code-defined, never scraped); only values are walked. Non-str scalars keep
    their native JSON type — they cannot carry a formula trigger.
    """
    if isinstance(value, str):
        return sanitize_for_csv(value)
    if isinstance(value, dict):
        return {k: _sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_value(v) for v in value]
    return value


def _canonical_dataframe(
    records: list[Any], hidden_fields: set[str] | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Build a DataFrame of canonical lead rows — the SAME columns + formatting
    (dialer split cols, normalized phones, sanitized values) as the CSV, so the
    Excel export matches the CSV exactly. No DNC footer; that lives in the email
    body + download UI (a disclaimer row breaks spreadsheet/dialer import).

    One `today` for the whole frame so a large export crossing UTC midnight can't
    give two rows different freshness_days/months_delinquent (Codex review).

    `hidden_fields` blanks the user-deselected hideable columns (header order is
    unchanged), keeping Excel byte-identical to the CSV for the same config.

    `columns` (from `resolve_lead_export_columns`) restricts the frame to a lean
    per-record-type subset, keeping Excel in lockstep with the CSV. None => full
    `LEAD_CSV_COLUMNS`. Selecting a subset of the full-width rows can't drift from
    the CSV because both project the SAME built rows.
    """
    today = datetime.now(UTC).date()
    rows = [_apply_visibility(build_lead_export_row(r, today), hidden_fields) for r in records]
    return pd.DataFrame(rows, columns=columns or LEAD_CSV_COLUMNS)


class DataExporter:
    """Export lead records to CSV / Excel / JSON and upload to Cloudflare R2."""

    def __init__(self, export_dir: str | None = None) -> None:
        self.export_dir = Path(export_dir) if export_dir else settings.EXPORTS_DIR
        self.export_dir.mkdir(parents=True, exist_ok=True)

    # ─── Local file export ────────────────────────────────────────────────────

    def to_csv(
        self, records: list[Any], filename: str = "export",
        hidden_fields: set[str] | None = None,
        columns: list[str] | None = None,
    ) -> Path:
        """Export records to the canonical dialer-ready CSV (shared builder).

        `columns` restricts the header to a lean per-record-type subset (None =
        full superset). Single-type callers resolve it via
        `resolve_lead_export_columns`; combined/batch callers omit it.
        """
        filepath = self._timestamped_path(filename, "csv")
        # newline="" so the csv writer doesn't emit blank lines between rows.
        with open(filepath, "w", encoding="utf-8", newline="") as f:
            write_lead_csv(records, f, hidden_fields=hidden_fields, columns=columns)
        _logger.info("CSV exported: %s (%d rows)", filepath.name, len(records))
        return filepath

    def to_excel(
        self, records: list[Any], filename: str = "export",
        hidden_fields: set[str] | None = None,
        columns: list[str] | None = None,
    ) -> Path:
        """Export records to an Excel file (canonical columns) with amber header.

        `columns` restricts the sheet to a lean per-record-type subset (None =
        full superset), keeping Excel in lockstep with the CSV.
        """
        filepath = self._timestamped_path(filename, "xlsx")
        df = _canonical_dataframe(records, hidden_fields, columns)

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

    def to_json(
        self, records: list[dict[str, Any]], filename: str = "export",
        hidden_fields: set[str] | None = None,
    ) -> Path:
        """Export records to JSON (orient=records) with sanitization.

        `hidden_fields` blanks the user-deselected hideable keys (same set the CSV
        suppresses), keeping every export format consistent for one config.

        NOTE: the lean per-record-type column trim (`columns=`) is deliberately NOT
        applied here. Unlike CSV/Excel — which pass raw rows through
        `build_lead_export_row` to the CANONICAL column schema — this path dumps the
        raw DB dicts as-is (keys like `enrichment_data`/`phones`, a different key
        namespace). A canonical-column allowlist would wrongly drop those raw keys,
        so JSON is left at full fidelity (its schema already diverges from the CSV).
        """
        filepath = self._timestamped_path(filename, "json")
        # Only ever blank a genuinely hideable key (never an identity column).
        blank_keys = (hidden_fields or set()) & HIDEABLE_OUTPUT_FIELDS
        # Sanitize string values before export
        sanitized = []
        for row in records:
            clean_row = {}
            for k, v in row.items():
                if k in blank_keys:
                    clean_row[k] = ""
                    continue
                # E4: sanitize EVERY string value, not just truthy ones — the
                # old `and v` skipped "" and the leading-quote/embedded-tab
                # bypass passed straight through. JSON is a common hand-off
                # into spreadsheet tools, so the same neutralization applies.
                # Sanitize RECURSIVELY: top-level-only sanitization let scraped
                # free-text inside nested objects (e.g. enrichment_data, the
                # phones/emails arrays) reach the file raw, so a formula-trigger
                # string survived in the delivered JSON (Codex). Non-str scalars
                # (numbers/bools/None) keep their native JSON type — they cannot
                # carry a formula trigger.
                clean_row[k] = _sanitize_json_value(v)
            sanitized.append(clean_row)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(sanitized, f, ensure_ascii=False, indent=2, default=str)
        _logger.info("JSON exported: %s (%d rows)", filepath.name, len(sanitized))
        return filepath

    def export(
        self,
        records: list[dict[str, Any]],
        filename: str = "export",
        fmt: str | None = None,
        hidden_fields: set[str] | None = None,
        columns: list[str] | None = None,
    ) -> Path:
        """Export to the given format. Single entry point for all callers.

        `hidden_fields` (from `resolve_hidden_output_fields(config.fields)`) is
        forwarded to every format so the delivered file honors the user's output
        visibility selection identically across csv/json/excel.

        `columns` (from `resolve_lead_export_columns(record_type)`) selects the lean
        per-record-type column subset for the CANONICAL formats (csv/excel). None =
        full superset (combined/batch callers omit it). JSON is intentionally NOT
        trimmed — it dumps raw DB dicts, a different key namespace (see `to_json`).
        """
        from src.config.constants import SUPPORTED_EXPORT_FORMATS
        fmt = (fmt or settings.EXPORT_FORMAT).lower()
        # Guard against drift: the accepted set is the shared
        # SUPPORTED_EXPORT_FORMATS constant (same one the schema validator +
        # worker use), so adding a format is a one-line constant change.
        if fmt not in SUPPORTED_EXPORT_FORMATS:
            raise ValueError(f"Unsupported export format: {fmt}")
        if fmt == "csv":
            return self.to_csv(records, filename, hidden_fields=hidden_fields, columns=columns)
        if fmt == "json":
            # JSON is intentionally NOT column-trimmed (raw dict namespace) — do
            # not forward `columns`; to_json has no such parameter.
            return self.to_json(records, filename, hidden_fields=hidden_fields)
        # excel | xlsx
        return self.to_excel(records, filename, hidden_fields=hidden_fields, columns=columns)

    # ─── R2 upload ────────────────────────────────────────────────────────────

    def upload_to_r2(self, local_path: Path, object_key: str) -> str:
        """Upload a local file to Cloudflare R2 and return the object key.

        Args:
            local_path: Path to the local file.
            object_key: S3-style key (e.g. 'exports/user_id/job_id/leads.csv').

        Returns:
            The object key stored in R2.

        Raises:
            ValueError: If object_key contains path traversal.
        """
        # Prevent path traversal attacks
        if ".." in object_key or object_key.startswith("/"):
            raise ValueError(f"Invalid object key: {object_key}")
        content_types = {
            ".csv": "text/csv",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".json": "application/json",
        }
        content_type = content_types.get(local_path.suffix, "application/octet-stream")

        url = f"{_r2_api_base()}/objects/{object_key}"
        headers = _r2_headers()
        headers["Content-Type"] = content_type

        with open(local_path, "rb") as f:
            resp = _requests.put(url, headers=headers, data=f, timeout=120)

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"R2 upload failed ({resp.status_code}): {resp.text[:200]}")

        _logger.info("Uploaded to R2: %s", object_key)
        return object_key

    def download_object(self, object_key: str) -> bytes:
        """Download an object from R2 and return its bytes.

        Uses the Cloudflare REST API (same auth as upload).
        """
        url = f"{_r2_api_base()}/objects/{object_key}"
        resp = _requests.get(url, headers=_r2_headers(), timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"R2 download failed ({resp.status_code}): {resp.text[:200]}")
        _logger.info("Downloaded from R2: %s (%d bytes)", object_key, len(resp.content))
        return resp.content

    def get_download_url(self, object_key: str, expires_in: int = 3600) -> str:
        """Generate a temporary download URL for an R2 object.

        Strategy (in order):
        1. R2 public URL if configured
        2. S3-compatible presigned URL via boto3 (most reliable)
        3. Cloudflare R2 API presigned URL (requires ACCOUNT_ID)

        Args:
            object_key: The R2 object key.
            expires_in: URL expiry in seconds (default: 1hr for in-app, use 172800 for email).

        Returns:
            HTTPS download URL.
        """
        # Public-URL path requires an EXPLICIT opt-in (R2_ALLOW_PUBLIC_URLS).
        # Exports contain seller PII; a stray R2_PUBLIC_URL must not silently
        # hand out permanent unauthenticated links. Without the flag we fall
        # through to the presigned/streamed path below.
        if settings.R2_PUBLIC_URL and settings.R2_ALLOW_PUBLIC_URLS:
            return f"{settings.R2_PUBLIC_URL}/{object_key}"
        if settings.R2_PUBLIC_URL and not settings.R2_ALLOW_PUBLIC_URLS:
            _logger.warning(
                "R2_PUBLIC_URL is set but R2_ALLOW_PUBLIC_URLS is false — "
                "ignoring it and using presigned URLs (export PII safety)."
            )

        # S3-compatible presigned URL via boto3 against the R2 S3 endpoint.
        # This is the active production path on Railway (R2_ENDPOINT_URL +
        # R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY are the env vars set in
        # prod). Don't remove this branch as "legacy" without first
        # migrating prod onto either R2_PUBLIC_URL or R2_ACCOUNT_ID.
        if settings.R2_ENDPOINT_URL and settings.R2_ACCESS_KEY_ID and settings.R2_SECRET_ACCESS_KEY:
            try:
                import boto3
                from botocore.config import Config

                s3 = boto3.client(
                    "s3",
                    endpoint_url=settings.R2_ENDPOINT_URL,
                    aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                    aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                    config=Config(signature_version="s3v4"),
                    region_name="auto",
                )
                presigned = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": settings.R2_BUCKET_NAME, "Key": object_key},
                    ExpiresIn=expires_in,
                )
                _logger.info("Generated S3 presigned URL for %s", object_key)
                return presigned
            except Exception as exc:
                _logger.warning("S3 presigned URL failed: %s", str(exc)[:80])

        # Cloudflare R2 native API presigned URL (used only when the
        # boto3 S3-compatible path above is not configured — currently
        # not the production path).
        if settings.R2_ACCOUNT_ID:
            url = f"{_r2_api_base()}/objects/{object_key}?presigned=true&expiresIn={expires_in}"
            try:
                resp = _requests.get(url, headers=_r2_headers(), timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    presigned = data.get("result", {}).get("presignedUrl")
                    if presigned:
                        return presigned
            except Exception as exc:
                _logger.warning("R2 API presigned URL failed: %s", str(exc)[:80])

        _logger.error("No download URL method available for %s", object_key)
        raise RuntimeError("Export download is not configured. Contact support.")

    # ─── Helper ───────────────────────────────────────────────────────────────

    def _timestamped_path(self, base_name: str, extension: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.export_dir / f"{base_name}_{timestamp}.{extension}"
