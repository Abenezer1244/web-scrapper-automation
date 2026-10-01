"""TaxSifter owner-name address lookup (Douglas WA): src/scrapers/enrichment/taxsifter.py.

The HTML below is SYNTHETIC (invented names, streets and numbers) in the exact markup
the live portal served on 2026-10-01, so no real person's data is committed. The
client tests run the real HTTP flow against a local server that serves those pages.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from src.scrapers.enrichment import taxsifter
from src.scrapers.enrichment.taxsifter import (
    TaxSifterClient,
    parse_taxsifter_assessor,
    parse_taxsifter_results,
    to_taxsifter_name,
)


def _card(owner: str, key: str, parcel: str, role: str = "Parcel Owner",
          href: str | None = None) -> str:
    href = href or f"/Assessor.aspx?keyId={key}&parcelNumber={parcel}&typeID=1"
    return f"""
            <div id="cphContent_Repeater1_pnlResult_0" class="result">
                <div class="property-photo"><img class="propertyImage" src="x.jpg" /></div>
                <div class="details">
                    <div class="match">
                        {owner}
                        ({role})
                        <font color="#CC0000"></font>
                    </div>
                    <div>{parcel}</div>
                    <div>1 TEST ST</div>
                    <div>{owner}</div>
                    <div>11 - Residential - Single Family</div>
                </div>
                <div class="nav"><ul>
                    <li><a href="{href}">Assessor</a></li>
                    <li><a href="/Treasurer.aspx?keyId={key}&parcelNumber={parcel}&typeID=1">Treasurer</a></li>
                </ul></div>
</div>"""


def _results(*cards: str) -> str:
    return f"""<html><body><form method="post" action="./Results.aspx" id="form1">
    <div class="bar"><div id="search"><input id="q" name="q" type="text" value='' /></div>
    <div class="resultCount">{len(cards)} records found.</div></div>
    <div id="result-area">{''.join(cards)}</div></form></body></html>"""


_ASSESSOR = """<html><body>
<span id="cphContent_ParcelOwnerInfo1_lbParcelNumber">90000000001</span>
<span id="cphContent_ParcelOwnerInfo1_lbOwnerName">DOE, JANE Q </span>
<span id="cphContent_ParcelOwnerInfo1_lbAddress">{a1}</span>
<span id="cphContent_ParcelOwnerInfo1_lbSitus">{situs}</span>
<span id="cphContent_ParcelOwnerInfo1_lbAddress2">{a2}</span>
<span id="cphContent_ParcelOwnerInfo1_lbCity">{city}</span>
<span id="cphContent_ParcelOwnerInfo1_lbState">WA</span>
<span id="cphContent_ParcelOwnerInfo1_lbZip">{zip}</span>
<table class="dataGrid" id="cphContent_ctl00_dvMarketValues"><caption>2027 Market Value</caption>
<tr><td>Land:</td><td align="right">$10,000</td></tr>
<tr><td>
                Total
            </td><td align="right">
                $250,500
            </td></tr></table>
<table class="dataGrid" id="cphContent_ctl00_dvTaxableValues"><caption>2027 Taxable Value</caption>
<tr><td>Total</td><td align="right">$1</td></tr></table>
</body></html>"""


def _assessor(situs="12 SAMPLE LN", a1="", a2="PO BOX 77", city="SAMPLETON", zip_="98800-1234"):
    return _ASSESSOR.format(situs=situs, a1=a1, a2=a2, city=city, zip=zip_)


# ─── name conversion ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "expected"), [
    ("DOE JANE Q", "DOE, JANE Q"),
    ("doe  jane", "DOE, JANE"),
    ("DOE, JANE Q", "DOE, JANE Q"),
    ("ESTATE OF DOE JANE", "DOE, JANE"),
    ("DOE JANE / ROE RICHARD", "DOE, JANE"),
    ("O'NEIL-SMITH ANN", "O'NEIL-SMITH, ANN"),
    ("DOE", None),
    ("DOE, JANE, Q", None),
    ("DOE JANE 3RD", None),
    ("", None),
    (None, None),
])
def test_to_taxsifter_name(raw, expected):
    assert to_taxsifter_name(raw) == expected


# ─── results page ─────────────────────────────────────────────────────────────

def test_a_unique_exact_owner_gives_its_parcel():
    html = _results(_card("DOE, JANE Q", "101", "90000000001"))
    assert parse_taxsifter_results(html, "DOE, JANE Q") == ("101", "90000000001")


def test_a_co_owner_listed_first_counts():
    html = _results(_card("DOE, JANE Q & ROE, RICHARD", "101", "90000000001"))
    assert parse_taxsifter_results(html, "DOE, JANE Q") == ("101", "90000000001")


def test_two_parcels_for_the_owner_is_ambiguous():
    """The live portal's own example: one exact-owner parcel plus co-owned ones."""
    html = _results(_card("DOE, JANE Q", "101", "90000000001"),
                    _card("DOE, JANE Q & ROE, RICHARD", "102", "90000000002"))
    assert parse_taxsifter_results(html, "DOE, JANE Q") is None


def test_the_same_parcel_twice_is_still_one_parcel():
    html = _results(_card("DOE, JANE Q", "101", "90000000001"),
                    _card("DOE, JANE Q", "101", "90000000001"))
    assert parse_taxsifter_results(html, "DOE, JANE Q") == ("101", "90000000001")


@pytest.mark.parametrize("card", [
    _card("DOE, JANE QUINN", "101", "90000000001"),         # a longer name is someone else
    _card("DOE, JANE Q", "101", "90000000001", role="Taxpayer"),
    _card("DOE, JANE Q", "101", "90000000001", href="/Assessor.aspx?keyId=1x&parcelNumber=2"),
    _card("DOE, JANE Q", "101", "90000000001", href="https://evil.example/Assessor.aspx?x=1"),
    _card("DOE, JANE Q", "101", "90000000001",
          href="/Assessor.aspx?keyId=101&keyId=999&parcelNumber=90000000001"),
    _card("DOE, JANE Q", "101", "90000000001",
          href="/AssessorX.aspx?keyId=101&parcelNumber=90000000001"),
    _card("DOE, JANE Q", "١٠١", "90000000001"),  # Arabic-Indic digits
])
def test_cards_that_do_not_qualify_are_ignored(card):
    assert parse_taxsifter_results(_results(card), "DOE, JANE Q") is None


def test_no_results():
    assert parse_taxsifter_results(_results(), "DOE, JANE Q") is None


# ─── assessor page ────────────────────────────────────────────────────────────

def test_the_assessor_page_gives_address_mailing_and_market_value():
    assert parse_taxsifter_assessor(_assessor()) == {
        "address": "12 SAMPLE LN",
        "mailing": "PO BOX 77, SAMPLETON, WA 98800",
        "value": "$250,500",
    }


def test_no_situs_means_no_enrichment():
    assert parse_taxsifter_assessor(_assessor(situs="")) is None


def test_a_missing_mailing_city_leaves_mailing_out():
    assert parse_taxsifter_assessor(_assessor(city="")) == {
        "address": "12 SAMPLE LN", "value": "$250,500",
    }


# ─── the client, over real HTTP ───────────────────────────────────────────────

class _Portal(BaseHTTPRequestHandler):
    """A local TaxSifter: disclaimer -> 302 -> results -> assessor."""
    redirect_to = "/default.aspx"
    fail_results = 0
    results_html = _results(_card("DOE, JANE Q", "101", "90000000001"))
    hits: list[str] = []

    def log_message(self, *args):  # keep test output quiet
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
        type(self).hits.append(url.path)
        if url.path == "/Disclaimer.aspx":
            self._send('<form method="post" action="https://evil.example/x" id="form1">'
                       '<input type="hidden" name="__VIEWSTATE" value="vs" />'
                       '<input type="submit" name="ctl00$cphContent$btnAgree" value="I Agree" /></form>')
        elif url.path == "/Search/Results.aspx" and type(self).fail_results > 0:
            type(self).fail_results -= 1
            self._send("busy", status=503)
        elif url.path == "/Search/Results.aspx":
            assert self.headers.get("Cookie") == "agreed=1"
            assert parse_qs(url.query)["q"] == ["DOE, JANE Q"]
            self._send(type(self).results_html)
        elif url.path == "/Assessor.aspx":
            assert parse_qs(url.query) == {"keyId": ["101"], "parcelNumber": ["90000000001"],
                                           "typeID": ["1"]}
            self._send(_assessor())
        else:
            self._send("nope", status=404)

    def do_POST(self):
        type(self).hits.append("POST " + self.path)
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        assert "__VIEWSTATE=vs" in body and "btnAgree=I+Agree" in body
        self._send("", status=302, headers={"Location": type(self).redirect_to,
                                            "Set-Cookie": "agreed=1; path=/"})


@pytest.fixture
def portal():
    _Portal.hits = []
    _Portal.redirect_to = "/default.aspx"
    _Portal.fail_results = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Portal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _client(origin: str) -> TaxSifterClient:
    """A client on the local portal. The constructor's SSRF validation and pinned
    session refuse loopback by design, so this one skips them and keeps the rest."""
    client = TaxSifterClient.__new__(TaxSifterClient)
    client._origin = origin
    client._session = requests.Session()
    client._accepted = False
    client._last_request = 0.0
    client._cache = {}
    return client


def test_a_lookup_returns_address_mailing_and_value_and_never_a_parcel(portal, monkeypatch):
    monkeypatch.setattr(taxsifter, "_SPACING_S", 0.0)
    client = _client(portal)
    result = client.lookup("DOE JANE Q")
    assert result == {"address": "12 SAMPLE LN", "mailing": "PO BOX 77, SAMPLETON, WA 98800",
                      "value": "$250,500"}
    assert not {"parcel", "parcel_id", "parcelNumber"} & set(result)
    # The form's own (hostile) action was ignored: the POST went to the fixed path.
    assert _Portal.hits == ["/Disclaimer.aspx", "POST /Disclaimer.aspx",
                            "/Search/Results.aspx", "/Assessor.aspx"]
    # Same name again: answered from the cache, no new request.
    assert client.lookup("DOE, JANE Q") == result
    assert len(_Portal.hits) == 4


@pytest.mark.parametrize("location", ["https://evil.example/default.aspx", "/Login.aspx"])
def test_an_unexpected_disclaimer_redirect_stops_the_lookups(portal, monkeypatch, location):
    monkeypatch.setattr(taxsifter, "_SPACING_S", 0.0)
    _Portal.redirect_to = location
    client = _client(portal)
    with pytest.raises(RuntimeError, match="disclaimer"):
        client.lookup("DOE JANE Q")
    assert "/Search/Results.aspx" not in _Portal.hits


def test_requests_are_spaced(portal, monkeypatch):
    import time
    monkeypatch.setattr(taxsifter, "_SPACING_S", 0.2)
    client = _client(portal)
    start = time.monotonic()
    client.lookup("DOE JANE Q")  # 4 requests -> at least 3 gaps
    assert time.monotonic() - start >= 0.6


def test_the_douglas_origin_is_the_only_one_and_https():
    assert taxsifter.TAXSIFTER_ORIGINS == {
        "douglas": "https://douglaswa-taxsifter.publicaccessnow.com",
    }


def test_a_failed_results_page_is_not_cached_so_the_name_is_retried(portal, monkeypatch):
    monkeypatch.setattr(taxsifter, "_SPACING_S", 0.0)
    _Portal.fail_results = 1
    client = _client(portal)
    with pytest.raises(RuntimeError, match="results page"):
        client.lookup("DOE JANE Q")
    assert client.lookup("DOE JANE Q") == {
        "address": "12 SAMPLE LN", "mailing": "PO BOX 77, SAMPLETON, WA 98800", "value": "$250,500",
    }
