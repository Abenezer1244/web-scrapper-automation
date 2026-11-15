"""Per-parcel property tax off a TaxSifter Treasurer page (Ferry): taxsifter.resolve_tax.

The fixtures are the live Ferry portal's pages (2026-11-15) with every owner name,
mailing line, situs and legal description REDACTED. One case below derives a prior-
year balance from a real page by editing that statement's Balance Due cell; it is
marked where it happens.
"""
from __future__ import annotations

import re
import threading
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import text

from src.config import settings
from src.scrapers.enrichment import taxsifter
from src.scrapers.enrichment.pacs import normalize_pacs_parcel
from tests.test_taxsifter import _client

FIX = Path(__file__).parent / "fixtures" / "ferry_taxsifter"
OWES = "33715440001000"
PAID = "32916500002000"


def _page(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _owes() -> str:
    return _page(f"treasurer_{OWES}_owes_current.html")


def _parse(html: str, parcel: str = OWES):
    return taxsifter.parse_parcel_treasurer(html, normalize_pacs_parcel(parcel))


def _with_2025_balance(html: str, amount: str) -> str:
    """DERIVED: the real page with its 2025 statement's Balance Due set to ``amount``."""
    row = re.search(r"2025-" + OWES + r"</a></td>(.*?)</tr>", html, re.S)
    assert row is not None
    cells = row.group(1)
    last = cells.rfind("$0.00")
    assert last != -1
    patched = cells[:last] + amount + cells[last + len("$0.00"):]
    return html[:row.start(1)] + patched + html[row.end(1):]


def test_a_paid_parcel_is_found_with_nothing_past_due():
    ans = _parse(_page(f"treasurer_{PAID}_paid.html"), PAID)
    assert ans.outcome == "found"
    assert ans.as_of == date(2026, 11, 12)
    assert {s.year for s in ans.statements} >= {2022, 2023, 2024, 2025, 2026}
    assert ans.past_due == [] and ans.delinquent_amount == Decimal("0")


def test_a_current_year_balance_is_read_from_the_history_table():
    ans = _parse(_owes())
    assert ans.outcome == "found"
    by_year = {s.year: s.amount_due for s in ans.statements}
    assert by_year[2026] == Decimal("679.84") and by_year[2025] == Decimal("0.00")
    assert ans.delinquent_amount == Decimal("679.84")
    assert ans.oldest_delinquent_year == 2026
    assert ans.owner_name == "REDACTED OWNER"
    assert ans.property_address == "100 REDACTED RD, REPUBLIC 99166"
    assert ans.mailing_address == "PO BOX 100, REPUBLIC, WA 99166"


def test_a_prior_year_balance_makes_the_oldest_year_prior():
    ans = _parse(_with_2025_balance(_owes(), "$383.30"))
    assert ans.oldest_delinquent_year == 2025
    assert ans.delinquent_amount == Decimal("1063.14")


def test_interest_and_penalty_are_never_invented():
    ans = _parse(_owes())
    assert all(s.penalty == 0 and s.interest == 0 for s in ans.statements)


def test_another_parcels_page_is_a_mismatch():
    assert _parse(_owes(), PAID).outcome == "parcel_mismatch"


@pytest.mark.parametrize("break_it", [
    lambda h: h.replace('id="lblDataDate"', 'id="gone"'),          # no as-of date
    lambda h: h.replace("5 Year Tax History", "History"),          # no history table
    lambda h: re.sub(r"\d{4}-" + OWES, "9999-1", h),               # no statement of ours
])
def test_unknown_is_never_zero(break_it):
    html = break_it(_owes())
    assert html != _owes()
    assert _parse(html).outcome == "unparsed"


def test_unreadable_balance_on_our_statement_is_unparsed():
    html = _owes()
    row = re.search(r"2026-" + OWES + r"</a></td>(.*?)</tr>", html, re.S)
    cells = row.group(1)
    i = cells.rfind("$679.84")
    broken = html[:row.start(1)] + cells[:i] + "n/a" + cells[i + 7:] + html[row.end(1):]
    assert _parse(broken).outcome == "unparsed"


def test_a_broken_prior_year_row_is_unparsed_not_a_zero():
    """A paid 2026 row parses; a 2025 row with a mangled statement number must not be skipped."""
    html = _with_2025_balance(_owes(), "$383.30").replace(f"2025-{OWES}</a>", "2025-??</a>")
    assert _parse(html).outcome == "unparsed"


def test_an_unknown_row_shape_is_unparsed():
    html = _owes().replace("<th>Statement Number</th>", "<th>Statement Number</th>", 1)
    i = html.find('<table class="dataGridPrimary"')
    j = html.find("<tr>", html.find("</tr>", i))
    assert _parse(html[:j] + "<tr><td>x</td><td>y</td><td>z</td></tr>" + html[j:]).outcome == "unparsed"


def test_personal_property_debt_is_never_counted_as_the_parcels():
    """DERIVED: the 2026 statement relabelled Personal Property must drop out of the total."""
    html = _owes().replace("<td>Real Property</td>", "<td>Personal Property</td>", 1)
    ans = _parse(html)
    assert ans.outcome == "found"
    assert 2026 not in {s.year for s in ans.statements}
    assert ans.delinquent_amount == Decimal("0")


def test_fixtures_carry_no_hidden_state():
    for page in FIX.glob("*.html"):
        assert not re.search(r'type="hidden" value="[^"]', page.read_text(encoding="utf-8")), page.name


# ── the paced pass, against a local portal serving the real pages ──────────────

SEARCH = {OWES: _page(f"results_{OWES}.html"), "100": "BLOCK"}


class _Portal(BaseHTTPRequestHandler):
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
        if url.path == "/Disclaimer.aspx":
            self._send('<form method="post" action="./Disclaimer.aspx">'
                       '<input type="hidden" name="__VIEWSTATE" value="vs" /></form>')
        elif url.path == "/Search/Results.aspx":
            body = SEARCH[q["q"][0]]
            self._send("busy", status=429) if body == "BLOCK" else self._send(body)
        elif url.path == "/Treasurer.aspx" and q.get("parcelNumber") == [OWES]:
            self._send(_owes())
        else:
            self._send("nope", status=404)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self._send("", status=302, headers={"Location": "/default.aspx",
                                            "Set-Cookie": "agreed=1; path=/"})


@pytest.fixture
def portal(monkeypatch, redis_client):
    from src.db.session import system_sync_session

    def _reset():
        redis_client.delete("bl:source_admission:taxsifter_ferry")
        with system_sync_session() as s:
            s.execute(text("DELETE FROM external_source_health WHERE source_key = 'taxsifter_ferry'"))
            s.commit()

    _reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Portal)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(taxsifter, "TaxSifterClient",
                        lambda county, **kw: _client(f"http://127.0.0.1:{server.server_port}"))
    monkeypatch.setattr(taxsifter, "_SPACING_S", 0.0)
    yield
    server.shutdown()
    _reset()


def test_the_pass_reads_a_real_page_end_to_end(portal):
    ans = taxsifter.resolve_tax("ferry", [OWES])[OWES]
    assert ans.outcome == "found" and ans.delinquent_amount == Decimal("679.84")


def test_ferry_tax_does_not_wait_on_the_restricted_mailing_switch(portal, monkeypatch):
    monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", False)
    assert taxsifter.resolve_tax("ferry", [OWES])[OWES].outcome == "found"


def test_whitman_still_waits_on_the_restricted_mailing_switch(monkeypatch):
    monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", False)
    assert taxsifter.resolve_tax("whitman", ["1"])["1"].outcome == "source_unavailable"


def test_a_block_defers_the_parcel_and_cools_the_source(portal):
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.source_health import get_source_state

    # Never asked to a settled answer: deferred, so the crawler keeps it at the front.
    assert taxsifter.resolve_tax("ferry", ["100"])["100"].outcome == "source_unavailable"
    with system_sync_session() as db:
        assert get_source_state(db, "taxsifter_ferry") is not None


def test_an_unknown_county_is_never_asked():
    assert taxsifter.resolve_tax("king", ["1"])["1"].outcome == "source_unavailable"
