"""Bellevue code enforcement cases from the city's public permit layer (ArcGIS Online).

Source: Bellevue Permits hosted FeatureServer, PERMITTYPE 'EA' (Enforcement Action),
refreshed daily. Each case prints the King County PIN (PARCELNUMBER), so parcel_id is set
at scrape. The layer's OWNER column is not requested: the owner is read from King
eRealProperty for that PIN, never from the permit system (data minimization, and the
permit owner is not necessarily the current taxpayer).

TWO SERVICE COPIES (verified 2026-09-14). The city publishes the same schema as
`Bellevue_Permits` (item "Bellevue Permits (Archive 20260908)", yet still edited daily)
and `Bellevue_Permit` (item "Bellevue Permits", last edited 2026-09-08). It looks
mid-migration, and either copy could be the one that stops updating. Every fetch reads
both layers' editingInfo.dataLastEditDate and queries the more recently edited copy; the
choice is recorded on each record (enrichment_data.source_service). If one copy's
metadata cannot be read, the other is used; if neither can, the source fails.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import ScrapedRecord
from src.scrapers.king_cv_sources import BELLEVUE
from src.scrapers.king_cv_sources.base import (
    LABEL_MAX,
    CodeViolationSource,
    arcgis_query_all,
    get_json_with_retries,
    normalize_king_pin,
)
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.king_cv_sources.bellevue")

_HOST = "services1.arcgis.com"
_SERVICE_URL = "https://" + _HOST + "/EYzEZbDhXZjURPbP/arcgis/rest/services/{service}/FeatureServer/0"
SERVICES = ("Bellevue_Permits", "Bellevue_Permit")
add_scrape_domain(_HOST)

_OUT_FIELDS = ("ObjectId,PERMITNUMBER,PERMITTYPE,SUBTYPE,SITEADDRESS,CITY,STATE,ZIPCODE,"
               "PERMITSTATUS,PARCELNUMBER,APPLIEDDATE,FINALEDDATE,ENFORCEMENTACTIONS")
# The layer's maxRecordCount.
_PAGE_SIZE = 2000
# APPLIEDDATE is stored as Pacific local midnight (07:00Z / 08:00Z).
_PACIFIC = ZoneInfo("America/Los_Angeles")


def _epoch_ms_to_local(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(_PACIFIC)
    except (OverflowError, OSError, ValueError):
        return None


def _address(attrs: dict) -> str | None:
    street = " ".join(str(attrs.get("SITEADDRESS") or "").split())
    if not street:
        return None
    city = " ".join(str(attrs.get("CITY") or "").split())
    state = " ".join(str(attrs.get("STATE") or "").split())
    zipcode = "".join(str(attrs.get("ZIPCODE") or "").split())[:5]
    out = street
    if city:
        out += f", {city}"
    if state:
        out += f" {state}"
    if len(zipcode) == 5 and zipcode.isdigit():
        out += f" {zipcode}"
    return out


class BellevueSource(CodeViolationSource):
    key = BELLEVUE
    jurisdiction = "Bellevue"

    def pick_service(self) -> str:
        """The service copy whose data was edited most recently."""
        edited: dict[str, int] = {}
        for service in SERVICES:
            try:
                meta = get_json_with_retries(_SERVICE_URL.format(service=service), {"f": "json"},
                                             what=f"{self.key} {service} metadata",
                                             require_features=False)
            except RuntimeError as exc:
                _logger.warning("Bellevue %s metadata unreadable: %s", service, str(exc)[:160])
                continue
            info = meta.get("editingInfo") if isinstance(meta.get("editingInfo"), dict) else {}
            stamp = info.get("dataLastEditDate")
            edited[service] = stamp if isinstance(stamp, int) and not isinstance(stamp, bool) else 0
        if not edited:
            raise RuntimeError(f"{self.key}: neither Bellevue permit service answered its metadata")
        # max() keeps the first of equal stamps, so SERVICES order breaks a tie.
        return max(edited, key=lambda s: edited[s])

    async def fetch(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start = datetime.strptime(date_from, "%m/%d/%Y").date()
        end = datetime.strptime(date_to, "%m/%d/%Y").date()
        service = self.pick_service()
        # The server compares timestamps in UTC while cases are dated in Pacific time.
        # Query a window that surely contains every Pacific day in range (a Pacific day
        # ends by 08:00Z the next UTC day), then keep the exact days below.
        where = (f"PERMITTYPE='EA' AND APPLIEDDATE >= TIMESTAMP '{start:%Y-%m-%d} 00:00:00' "
                 f"AND APPLIEDDATE < TIMESTAMP '{end + timedelta(days=2):%Y-%m-%d} 00:00:00'")
        _logger.info("Bellevue code enforcement %s to %s via %s", date_from, date_to, service)
        rows = arcgis_query_all(
            _SERVICE_URL.format(service=service) + "/query",
            {"where": where, "outFields": _OUT_FIELDS, "returnGeometry": "false",
             # ObjectId is the layer's unique object id: a total order for offset paging.
             "orderByFields": "ObjectId ASC"},
            page_size=_PAGE_SIZE, what=f"{self.key} query", on_page=self._progress)

        records: list[ScrapedRecord] = []
        seen: set[str] = set()
        parsed = 0
        for attrs in rows:
            case = " ".join(str(attrs.get("PERMITNUMBER") or "").split())
            applied = _epoch_ms_to_local(attrs.get("APPLIEDDATE"))
            if not case or applied is None:
                continue
            parsed += 1
            if case in seen or not (start <= applied.date() <= end):
                continue
            seen.add(case)
            finaled = _epoch_ms_to_local(attrs.get("FINALEDDATE"))
            category = " ".join(str(attrs.get("ENFORCEMENTACTIONS") or "").split()).rstrip(".")
            raw_pin = attrs.get("PARCELNUMBER")
            record = ScrapedRecord(
                date_recorded=applied.strftime("%m/%d/%Y"),
                # The owner comes from King eRealProperty for the PIN, never from here.
                party_name=None,
                legal_description=case,
                parcel_id=normalize_king_pin(raw_pin),
                property_address=_address(attrs),
                raw_html_hash=hashlib.sha256(f"{self.key}|{case}".encode()).hexdigest()[:32],
            )
            record.enrichment_data = {
                "source": self.key,
                "case_number": case,
                "status": attrs.get("PERMITSTATUS"),
                "violation_category": category[:LABEL_MAX] or None,
                "case_type": attrs.get("SUBTYPE"),
                "applied_at": applied.isoformat(),
                "closed_at": finaled.isoformat() if finaled else None,
                "source_parcel_number": raw_pin,
                "source_service": service,
            }
            records.append(record)
        self.check_canary(len(rows), parsed)
        _logger.info("Bellevue code enforcement: %d cases from %d rows", len(records), len(rows))
        return records
