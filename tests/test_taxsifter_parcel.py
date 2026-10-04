"""Parcel-keyed taxpayer mailing on a TaxSifter portal (Whitman): taxsifter.resolve_mailing.

Identity closes twice: the parcel search must return exactly one assessor link for OUR
parcel, and that page must echo our parcel. Only "0 records found" proves
parcel_not_found; a page without the record count is unparsed, never a settled answer.

Real Redis lease, real source_health rows, real HTTP against a local portal served under
/Taxsifter (Whitman's base path). The page markup is the live portal's (verified
2026-10-03); every name, street and number in it is SYNTHETIC.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import text

from src.config import settings
from src.scrapers.enrichment import taxsifter
from tests.test_taxsifter import _client

PARCEL = "100500003050000"
BASE = "/Taxsifter"


def _results(count: int, *links: tuple[str, str]) -> str:
    cards = "".join(
        f'<div class="result"><div class="nav"><a href="Assessor.aspx?keyId={k}&amp;parcelNumber={p}'
        f'&amp;typeID=1">Assessor</a></div></div>' for k, p in links)
    return f"<html><body><span>{count} records found</span>{cards}</body></html>"


def _detail(echo: str, street: str = "PO BOX 77", city: str = "SAMPLETON", state: str = "WA",
            zipcode: str = "99111") -> str:
    def span(sfx: str, val: str) -> str:
        return f'<span id="cphContent_ParcelOwnerInfo1_{sfx}">{val}</span>'

    return ("<html><body>" + span("lbParcelNumber", echo) + span("lbSitus", "1 SAMPLE RD")
            + span("lbAddress", street) + span("lbAddress2", "") + span("lbCity", city)
            + span("lbState", state) + span("lbZip", zipcode) + "</body></html>")


# q -> results page; keyId -> detail page
SEARCH = {
    PARCEL: _results(1, ("7", PARCEL)),
    "200000000000001": _results(0),
    "200000000000002": _results(2, ("8", "200000000000002"), ("9", "200000000000002")),
    "200000000000003": "<html><body>Search is unavailable</body></html>",
    "200000000000004": _results(1, ("10", "200000000000004")),
    "200000000000005": _results(1, ("11", "200000000000005")),
    "200000000000006": "BLOCK",
    # Claims a record but its card carries no readable link: layout drift, not an answer.
    "200000000000007": '<html><body>1 records found<div class="result">x</div></body></html>',
    # Says 0 but carries our link: contradictory, never settled.
    "200000000000008": _results(0, ("12", "200000000000008")),
    # Says 0 but carries a card whose link cannot be read: still contradictory.
    "200000000000009": '<html><body>0 records found<div class="result"><a href="https://x/y">z</a></div></body></html>',
}
DETAIL = {
    "7": _detail(PARCEL),
    "10": _detail("999999999999999"),                    # echoes another parcel
    "11": _detail("200000000000005", street="", city="", state="", zipcode=""),  # no address
}


class _Portal(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *args):
        pass

    def _send(self, body: str, status: int = 200, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        type(self).hits.append(url.path)
        if url.path == BASE + "/Disclaimer.aspx":
            self._send('<form method="post" action="./Disclaimer.aspx">'
                       '<input type="hidden" name="__VIEWSTATE" value="vs" /></form>')
        elif url.path == BASE + "/Search/Results.aspx":
            body = SEARCH[q["q"][0]]
            self._send("busy", status=429) if body == "BLOCK" else self._send(body)
        elif url.path == BASE + "/Assessor.aspx":
            self._send(DETAIL[q["keyId"][0]])
        else:
            self._send("nope", status=404)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self._send("", status=302, headers={"Location": BASE + "/default.aspx",
                                            "Set-Cookie": "agreed=1; path=/"})


@pytest.fixture
def portal(monkeypatch):
    _Portal.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Portal)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}{BASE}"
    monkeypatch.setattr(taxsifter, "TaxSifterClient", lambda county, **kw: _client(origin))
    yield origin
    server.shutdown()


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch, redis_client):
    from src.db.session import system_sync_session

    monkeypatch.setattr(taxsifter, "_SPACING_S", 0.0)
    monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", True)

    def _reset():
        redis_client.delete("bl:source_admission:taxsifter_whitman")
        with system_sync_session() as s:
            s.execute(text("DELETE FROM external_source_health WHERE source_key = 'taxsifter_whitman'"))
            s.commit()

    _reset()
    yield
    _reset()


def _resolve(*pids: str) -> dict:
    return {pid: (a.outcome, a.mailing_address) for pid, a in taxsifter.resolve_mailing("whitman", list(pids)).items()}


def test_a_parcel_whose_page_echoes_it_gets_the_taxpayer_mailing(portal):
    assert _resolve(PARCEL) == {PARCEL: ("found", "PO BOX 77, SAMPLETON, WA 99111")}
    # Disclaimer accepted under the base path, then search, then the assessor page.
    assert _Portal.hits == [BASE + "/Disclaimer.aspx", BASE + "/Search/Results.aspx",
                            BASE + "/Assessor.aspx"]


def test_only_a_zero_record_count_settles_parcel_not_found(portal):
    out = _resolve("200000000000001", "200000000000003")
    assert out["200000000000001"] == ("parcel_not_found", None)
    assert out["200000000000003"] == ("unparsed", None)   # no count: we do not know


def test_a_count_and_links_that_disagree_are_never_settled(portal):
    out = _resolve("200000000000007", "200000000000008", "200000000000009")
    assert set(out.values()) == {("unparsed", None)}


def test_two_links_for_our_parcel_are_ambiguous_and_a_foreign_echo_is_a_mismatch(portal):
    out = _resolve("200000000000002", "200000000000004")
    assert out["200000000000002"] == ("ambiguous", None)
    assert out["200000000000004"] == ("parcel_mismatch", None)


def test_an_owner_block_with_no_address_is_none(portal):
    assert _resolve("200000000000005") == {"200000000000005": ("none", None)}


def test_a_block_cools_the_source_and_stops_the_pass(portal):
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.source_health import is_source_available

    out = _resolve("200000000000006", PARCEL)
    assert out["200000000000006"][0] == "source_unavailable"
    assert out[PARCEL][0] == "source_unavailable"       # never asked after the block
    with system_sync_session() as s:
        assert is_source_available(s, "taxsifter_whitman") is False
    assert _resolve(PARCEL)[PARCEL][0] == "source_unavailable"   # cooling: no request


def test_the_license_switch_defers_everything_before_any_request(portal, monkeypatch):
    monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", False)
    assert _resolve(PARCEL) == {PARCEL: ("source_unavailable", None)}
    assert _Portal.hits == []


def test_a_parcel_spelled_with_separators_is_one_lookup_for_every_caller(portal):
    out = _resolve(PARCEL, "1005-00003-050000")
    assert out == {PARCEL: ("found", "PO BOX 77, SAMPLETON, WA 99111"),
                   "1005-00003-050000": ("found", "PO BOX 77, SAMPLETON, WA 99111")}
    assert _Portal.hits.count(BASE + "/Search/Results.aspx") == 1


def test_county_gis_dispatches_whitman_to_the_taxsifter_source(portal):
    from src.scrapers.enrichment import county_gis as cg

    assert cg.has_mailing_source("whitman", "WA") is True
    got = cg._resolve_bulk_mailing("whitman_WA", [PARCEL])
    assert got[PARCEL].outcome == "found"


def test_a_parcel_only_portal_is_never_an_owner_name_source():
    with pytest.raises(ValueError):
        taxsifter.TaxSifterClient("whitman")            # the owner-name registry
    with pytest.raises(ValueError):
        taxsifter.TaxSifterClient("douglas", parcel_site=True)
