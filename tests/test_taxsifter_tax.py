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
from bs4 import BeautifulSoup
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


COMP = "44006510019005"


def _comp() -> str:
    return _page(f"treasurer_{COMP}_compensation.html")


def test_a_compensating_tax_statement_is_read_as_land_tax():
    """Real page (redacted): a 2025 'Compensation' row (2025-<parcel>-C1) beside the
    real-property rows. It used to make the whole page unparsed (141 Ferry parcels)."""
    ans = taxsifter.parse_parcel_treasurer(_comp(), normalize_pacs_parcel(COMP))
    assert ans.outcome == "found"
    ids = {s.statement_id for s in ans.statements}
    assert f"2025-{COMP}" in ids and f"2026-{COMP}" in ids
    assert f"2025-{COMP}-C1" not in ids  # verified, but never a counted statement
    assert ans.delinquent_amount == Decimal("0")  # this parcel has paid everything


def test_owed_compensating_tax_is_never_called_past_due():
    """DERIVED: the real page with its Compensation row's Balance Due set to $201.26.
    Its due date (30 days after notice) is not on the page, so it is not counted."""
    soup = BeautifulSoup(_comp(), "html.parser")
    link = soup.find("a", string=re.compile(rf"^\s*2025-{COMP}-C1\s*$"))
    cell = link.find_parent("tr").find_all("td", recursive=False)[5]
    cell.clear()
    cell.append("$201.26")
    ans = taxsifter.parse_parcel_treasurer(str(soup), normalize_pacs_parcel(COMP))
    assert ans.outcome == "found"
    assert ans.delinquent_amount == Decimal("0") and ans.oldest_delinquent_year is None


@pytest.mark.parametrize("kind, statement", [
    ("Compensation", f"2025-{COMP}"),          # compensation without its -C suffix
    ("Real Property", f"2025-{COMP}-C1"),      # real property with a -C suffix
    ("Timber Excise", f"2025-{COMP}"),         # a kind this reader does not know
    ("Real Property", f"2025-{COMP}-0"),       # a zero statement number
    ("Compensation", f"2025-{COMP}-C0"),
    ("Real Property", f"0000-{COMP}"),         # no real year
], ids=["comp no suffix", "real with suffix", "unknown kind", "zero suffix", "zero comp",
        "year 0000"])
def test_a_statement_whose_kind_and_number_disagree_is_unparsed(kind, statement):
    soup = BeautifulSoup(_comp(), "html.parser")
    link = soup.find("a", string=re.compile(rf"^\s*2025-{COMP}-C1\s*$"))
    cells = link.find_parent("tr").find_all("td", recursive=False)
    cells[0].clear()
    cells[0].append(kind)
    link.clear()
    link.append(statement)
    assert taxsifter.parse_parcel_treasurer(str(soup), normalize_pacs_parcel(COMP)).outcome == "unparsed"


def test_numbered_real_property_statements_are_all_read():
    """Real page (redacted): a parcel billed on two statements a year (-1, -2)."""
    pid = "23608240001000"
    html = _page(f"treasurer_{pid}_split_statements.html")
    ans = taxsifter.parse_parcel_treasurer(html, normalize_pacs_parcel(pid))
    assert ans.outcome == "found"
    assert {f"2026-{pid}-1", f"2026-{pid}-2", f"2021-{pid}-2"} <= {s.statement_id for s in ans.statements}
    assert ans.delinquent_amount == Decimal("185.77") and ans.oldest_delinquent_year == 2026


def test_a_statement_listed_twice_is_unparsed_not_summed_twice():
    pid = "23608240001000"
    soup = BeautifulSoup(_page(f"treasurer_{pid}_split_statements.html"), "html.parser")
    history = soup.find("h1", string=re.compile("5 Year Tax History")).find_next("table")
    link = history.find("a", string=re.compile(rf"^\s*2026-{pid}-1\s*$"))
    link.clear()
    link.append(f"2026-{pid}-2")                 # now 2026-...-2 appears twice
    assert taxsifter.parse_parcel_treasurer(str(soup), normalize_pacs_parcel(pid)).outcome == "unparsed"
