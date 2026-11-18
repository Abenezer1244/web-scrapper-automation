"""Columbia PACS: "-OUT" Geo IDs and parcels listed under several tax accounts.

Live 2026-11-16 the Columbia tax crawl left ~4% of parcels unsettled, which would keep
its 95% coverage gate from ever passing:
* "-OUT" (outside city limits): the portal prints 1080090180000-OUT where the statewide
  list has 1080090180000; the search grid and the detail page were unreadable.
* two accounts on one parcel: 2008390053000 is prop_ids 265283 and 276080, each with
  its own tax page; the search was ambiguous.
Real pages (owner names redacted); only the county's HTTP boundary is substituted, with
the real Redis lease and source_health rows, as in test_pacs_parcel.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from src.scrapers.enrichment import pacs_parcel as pp
from src.scrapers.enrichment import pacs_tax
from src.scrapers.enrichment.pacs import normalize_pacs_parcel

_FIX = Path(__file__).parent / "fixtures"
_SITE = pp.PACS_SITES["columbia"]
_TWO, _OUT = "2008390053000", "1080090180000"
_ACCOUNTS = ("265283", "276080")


def _read(name: str) -> str:
    return (_FIX / name).read_text(encoding="utf-8")


_PAGES = {
    "265283": _read(f"columbia_pacs_detail_{_TWO}_265283.html"),
    "276080": _read(f"columbia_pacs_detail_{_TWO}_276080.html"),
    "264205": _read(f"columbia_pacs_detail_out_{_OUT}.html"),
}
_SEARCH = {
    _TWO: _read(f"columbia_pacs_search_2accounts_{_TWO}.html"),
    _OUT: _read(f"columbia_pacs_search_out_{_OUT}.html"),
}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", True)
    monkeypatch.setattr(pp, "_PACE_S", 0.0)
    monkeypatch.setattr(pp, "_JITTER_S", 0.0)
    monkeypatch.setattr(pp, "_RETRY_BACKOFF_S", 0.0)


@pytest.fixture(autouse=True)
def _clean_source_state(redis_client):
    from src.db.session import system_sync_session

    def _reset():
        redis_client.delete(f"bl:source_admission:{_SITE.source_key}")
        with system_sync_session() as s:
            s.execute(text("DELETE FROM external_source_health WHERE source_key = :k"),
                      {"k": _SITE.source_key})
            s.commit()

    _reset()
    yield
    _reset()


class _Resp:
    def __init__(self, status: int, body: str = ""):
        self.status_code, self.text = status, body


def _serve(monkeypatch, pages=None):
    """The portal: the real Columbia form, the real search grids, a detail page per prop_id."""
    pages = dict(_PAGES, **(pages or {}))
    log: list = []

    def _get(session, url):
        if url == _SITE.search_url:
            return _Resp(200, _read("columbia_pacs_search_form.html"))
        pid = url.split("prop_id=", 1)[1].split("&", 1)[0]
        log.append(pid)
        answer = pages[pid]
        return _Resp(answer, "") if isinstance(answer, int) else _Resp(200, answer)

    def _post(session, url, data):
        return _Resp(200, _SEARCH[data["propertySearchOptions$geoid"]])

    monkeypatch.setattr(pp, "_http_get", _get)
    monkeypatch.setattr(pp, "_http_post", _post)
    return log


# ─── "-OUT" ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "key"), [
    ("1080090180000-OUT", "1080090180000"), ("1080090180000-out", "1080090180000"),
    ("1080090180000", "1080090180000"), ("1080090180000-X", None), ("OUT", None)])
def test_out_suffix_is_the_same_parcel_and_nothing_else_is(raw, key):
    assert normalize_pacs_parcel(raw) == key


def test_out_suffix_is_never_sent_to_the_portal():
    assert pp.query_digits("1080090180000-OUT") == "1080090180000"


def test_out_parcel_resolves_to_its_tax_page(monkeypatch):
    _serve(monkeypatch)
    answer = pp.resolve_tax("columbia", [_OUT])[_OUT]
    assert answer.outcome == "found" and len(answer.statements) == 2


# ─── Several accounts ────────────────────────────────────────────────────────

def test_the_two_account_grid_is_ambiguous_for_parse_results_but_lists_its_accounts():
    assert pp.parse_results(_SEARCH[_TWO], _TWO)[0] == pp.AMBIGUOUS
    assert pp.parse_accounts(_SEARCH[_TWO], _TWO) == list(_ACCOUNTS)


def test_a_grid_that_does_not_say_it_is_complete_lists_no_accounts():
    headless = _SEARCH[_TWO].replace("1 - 2 of 2", "")
    assert pp.parse_accounts(headless, _TWO) is None


def test_tax_reads_every_account(monkeypatch):
    log = _serve(monkeypatch)
    answer = pp.resolve_tax("columbia", [_TWO])[_TWO]
    assert answer.outcome == "found"
    assert sorted(log) == sorted(_ACCOUNTS)
    assert len(answer.statements) == 4          # 2 statements on each account
    assert answer.owner_name == "OWNER, REDACTED"  # identical on both pages: kept


def test_one_page_served_for_both_accounts_is_never_counted_twice(monkeypatch):
    _serve(monkeypatch, {"276080": _PAGES["265283"]})
    assert pp.resolve_tax("columbia", [_TWO])[_TWO].outcome == "parcel_mismatch"


def test_a_block_on_the_second_account_stops_the_pass_and_settles_nothing(monkeypatch):
    _serve(monkeypatch, {"276080": 429})
    assert pp.resolve_tax("columbia", [_TWO])[_TWO].outcome == pp.SOURCE_UNAVAILABLE


def test_mailing_stays_ambiguous_for_a_two_account_parcel(monkeypatch):
    _serve(monkeypatch)
    assert pp.resolve_mailing("columbia", [_TWO])[_TWO].outcome == pp.AMBIGUOUS


# ─── Combining accounts ──────────────────────────────────────────────────────

def _answer(year_due: dict[int, str], *, owner="A", situs="1 MAIN ST, DAYTON, WA 99328",
            mail="PO BOX 1, DAYTON, WA 99328", as_of=date(2026, 11, 16)):
    return pacs_tax.TaxAnswer(
        "found", as_of=as_of,
        statements=[pacs_tax.Statement(y, f"S{y}", Decimal(v), Decimal("0"), Decimal("0"))
                    for y, v in year_due.items()],
        owner_name=owner, property_address=situs, mailing_address=mail)


def test_accounts_owing_the_same_year_add_up():
    combined = pacs_tax.combine_accounts([_answer({2024: "100.00"}), _answer({2024: "50.00"})])
    assert combined.delinquent_amount == Decimal("150.00")
    from src.workers.pacs_tax_crawler import _owed_by_year

    assert _owed_by_year(combined.past_due) == {"2024": "150.00"}


def test_accounts_that_disagree_on_who_or_where_hold_the_lead():
    combined = pacs_tax.combine_accounts([_answer({2024: "1.00"}, owner="A"),
                                          _answer({2024: "1.00"}, owner="B")])
    assert combined.outcome == "found" and combined.owner_name is None


@pytest.mark.parametrize("answers", [
    [],
    [_answer({2024: "1.00"}), pacs_tax.TaxAnswer("unparsed")],
    [_answer({2024: "1.00"}), _answer({2024: "1.00"}, as_of=date(2026, 11, 1))],
])
def test_an_unknown_account_is_never_a_zero(answers):
    assert pacs_tax.combine_accounts(answers).outcome != "found"


# ─── The county's "no statement" row (live 2026-11-17) ──────────────────────
_NO_STMT = _read("columbia_pacs_detail_no_statement_2009380172100.html")


def test_an_account_with_no_tax_billed_is_an_answer_not_unknown():
    answer = pacs_tax.parse_tax(_NO_STMT, "2009380172100")
    assert answer.outcome == "found" and answer.statements == []
    assert answer.delinquent_amount == Decimal("0")


def test_a_look_alike_row_with_money_on_it_stays_unparsed():
    # The marker is blank year AND blank id AND every amount $0.00; anything else is drift.
    owing = _NO_STMT.replace('<td class="statementTotalAmt">$0.00</td>',
                             '<td class="statementTotalAmt">$12.00</td>', 1)
    assert owing != _NO_STMT
    assert pacs_tax.parse_tax(owing, "2009380172100").outcome == "unparsed"


def test_a_no_statement_account_combines_with_a_billed_one():
    empty = pacs_tax.parse_tax(_NO_STMT, "2009380172100")
    billed = _answer({2024: "40.00"}, owner=empty.owner_name, situs=empty.property_address,
                     mail=empty.mailing_address, as_of=empty.as_of)  # same day, same parcel
    combined = pacs_tax.combine_accounts([billed, empty])
    assert combined.outcome == "found" and combined.delinquent_amount == Decimal("40.00")


def test_five_accounts_are_read_eight_is_the_bound():
    assert pp._MAX_ACCOUNTS >= 5


def test_a_statement_row_whose_class_drifted_is_never_hidden_by_a_zero_marker():
    # Zero marker + an owing statement row under an unexpected class: unknown, not "paid".
    drifted = _NO_STMT.replace(
        '</tr><tr class="rowEnd" id="DetailTable0_totalsRow">',
        '</tr><tr class="rowEndX"><td>2024</td><td>1</td><td>$5.00</td><td>$5.00</td>'
        '<td>$0.00</td><td>$0.00</td><td>$0.00</td><td>$10.00</td></tr>'
        '<tr class="rowEnd" id="DetailTable0_totalsRow">', 1)
    assert drifted != _NO_STMT
    assert pacs_tax.parse_tax(drifted, "2009380172100").outcome == "unparsed"


def test_the_pass_deadline_holds_between_accounts(monkeypatch):
    import time as _time

    log = _serve(monkeypatch)
    answer, _ = pp._resolve_one(None, _SITE, {}, _TWO, lambda: True, combine=pacs_tax.combine_accounts,
                                parse=pacs_tax.parse_tax, make=pacs_tax.TaxAnswer,
                                deadline=_time.monotonic() - 1)
    assert answer.outcome == pp.SOURCE_UNAVAILABLE and log == []  # no account page fetched


@pytest.mark.parametrize("cells", [7, 9, 1])
def test_a_zero_marker_never_hides_a_row_of_any_other_shape(cells):
    # Codex round 2: any unexpected row (here an owing one with 7, 9 or 1 cells, under
    # an unknown class) makes the page unknown, never "nothing owed".
    tds = "<td>2024</td>" + "".join("<td>$5.00</td>" for _ in range(cells - 1))
    drifted = _NO_STMT.replace(
        '</tr><tr class="rowEnd" id="DetailTable0_totalsRow">',
        f'</tr><tr class="rowEndX">{tds}</tr><tr class="rowEnd" id="DetailTable0_totalsRow">', 1)
    assert drifted != _NO_STMT
    assert pacs_tax.parse_tax(drifted, "2009380172100").outcome == "unparsed"
