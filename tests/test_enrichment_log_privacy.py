"""P0-04: enrichment success lines log ids, never owner names, addresses or values.

Runs the real parsers (the real Pierce GIS config, a Regrid-shaped GeoJSON body)
and reads what their own handlers actually render. No mocks.
"""
import io
import logging

from src.scrapers.enrichment import county_gis, national

_OWNER = "Jane Q Marker"
_SITUS = "1234 Marker Elm St"
_MAILING = "PO Box 9876 Markerville"


def _rendered(logger: logging.Logger, call) -> tuple[object, str]:
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    logger.addHandler(handler)
    try:
        return call(), out.getvalue()
    finally:
        logger.removeHandler(handler)


def test_gis_success_line_names_the_parcel_only():
    attrs = {"TaxParcelNumber": "0123456789", "Site_Address": _SITUS,
             "Delivery_Address": _MAILING, "City_State": "TACOMA WA", "Zipcode": "98402",
             "Legal_Description": _OWNER}
    result, out = _rendered(county_gis._logger, lambda: county_gis._parse_gis_response(
        {"features": [{"attributes": attrs}]}, county_gis._KNOWN_GIS_ENDPOINTS["pierce_WA"]))
    assert result["property_address"] == _SITUS  # the data itself is unchanged
    assert "GIS enriched parcel 0123456789 (owner found)" in out
    for pii in (_OWNER, _SITUS, "Marker"):
        assert pii not in out


def test_regrid_success_line_names_the_parcel_only():
    props = {"parcelnumb": "0123456789", "address": _SITUS, "mailadd": _MAILING,
             "owner": _OWNER, "parval": 412345}
    result, out = _rendered(national._logger, lambda: national._parse_regrid_response(
        {"features": [{"properties": props}]}))
    assert result["mailing_address"] == _MAILING
    assert "Regrid enriched 0123456789 (address found, owner found, value found)" in out
    for pii in (_OWNER, _SITUS, "412345", "Marker"):
        assert pii not in out


def test_scraper_error_message_never_carries_page_text():
    from src.scrapers.reliability import ScraperExecutionError, TransientScrapeError

    page = f"Search results: {_OWNER}, {_SITUS}, LOT 4 BLK 2"
    for cls in (ScraperExecutionError, TransientScrapeError):
        exc = cls("King", "AcclaimWeb", "block wall detected", page=3, context=page[:120])
        assert "block wall detected" in str(exc) and "page=3" in str(exc)
        assert "Marker" not in str(exc)
        assert not hasattr(exc, "context")  # not kept anywhere on the error either
    wrapped = ScraperExecutionError("King", "AcclaimWeb", "page extraction failed",
                                    context=TimeoutError(f"waiting for {_OWNER}"))
    assert "ctx=TimeoutError" in str(wrapped) and "Marker" not in str(wrapped)
