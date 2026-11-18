"""Past-due property tax from a PACS PropertyAccess detail page (Asotin first).

Asotin publishes no delinquent list, but every PACS detail page carries the parcel's
"Taxes and Assessment Details": one row per tax statement (year, statement id, first-
and second-half base, penalty, interest, base paid, amount due) "as of" a date.

Only the statement rows of ``#ctl00_details_detailsTable`` are read (``tr.rowEnd``,
eight cells). The per-jurisdiction rows nested under each statement repeat the same
money and are never summed, so nothing is counted twice.

Past due follows RCW 84.56.020: the first half is due April 30, the second half
October 31 of the tax year. A statement that still owes after its first due date is
past due, the current year included; measured 2026-11-12, 6 of 60 random Asotin
parcels owed only 2026 tax after both halves were due, so skipping the current year
(as the Snohomish bulk file does) would drop nearly every Asotin delinquency.

Unknown is never zero: a page without the statement table, without a readable "as
of" date, with a table but no statement row, or naming another parcel is ``unparsed`` /
``parcel_mismatch``. Every real parcel has rows, county-owned exempt land included (2
each on Columbia, checked live 2026-11-14), so an empty table is layout drift, never
"nothing owed". An account with no tax billed shows ONE row of its own (blank year and
statement id, every amount $0.00; Columbia 2026-11-17): only that zero TOTALS row is an
answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

from src.scrapers.enrichment.pacs import (
    normalize_pacs_parcel,
    pacs_owner_names,
    parse_pacs_detail_html,
)

FOUND = "found"
UNPARSED = "unparsed"
PARCEL_MISMATCH = "parcel_mismatch"

_AS_OF = re.compile(r"Property\s+Tax\s+Information\s+as\s+of\s*(\d{1,2}/\d{1,2}/\d{4})", re.I)
_MONEY = re.compile(r"^(-)?\$?\s*([\d,]+\.\d{2})$")


@dataclass(frozen=True)
class Statement:
    year: int
    statement_id: str
    amount_due: Decimal
    penalty: Decimal
    interest: Decimal


@dataclass
class TaxAnswer:
    outcome: str
    as_of: date | None = None
    statements: list[Statement] = field(default_factory=list)
    # The lead's identity, read off the same page that proved the parcel.
    owner_name: str | None = None
    property_address: str | None = None
    mailing_address: str | None = None

    @property
    def past_due(self) -> list[Statement]:
        """Statements still owing after their first-half due date (April 30)."""
        if self.as_of is None:
            return []
        return [s for s in self.statements
                if s.amount_due > 0 and self.as_of > date(s.year, 4, 30)]

    @property
    def delinquent_amount(self) -> Decimal:
        return sum((s.amount_due for s in self.past_due), Decimal("0"))

    @property
    def oldest_delinquent_year(self) -> int | None:
        return min((s.year for s in self.past_due), default=None)


def _known_row(tr) -> bool:
    """A statement-table row of a shape this parser understands (PACS, Asotin and
    Columbia alike): header (th only), detail row (rowMiddle, one cell), or a
    statement/totals row (rowEnd, eight cells)."""
    classes = tr.get("class") or []
    tds = len(tr.find_all("td", recursive=False))
    ths = len(tr.find_all("th", recursive=False))
    if ths and not tds:
        return True
    if "rowMiddle" in classes:
        return tds == 1
    if "rowEnd" in classes:
        return tds == 8
    return False


def _is_no_statement_row(tr, cells: list[str]) -> bool:
    """The page's TOTALS row standing alone: an account with no tax billed shows only
    its totals row (id "...totalsRow"), with a blank year and statement id and every
    amount exactly $0.00 (Columbia, live 2026-11-17). That is an answer, nothing owed;
    a page with NO rows, or a totals row carrying any money, is not."""
    return (str(tr.get("id", "")).endswith("totalsRow") and len(cells) == 8
            and not cells[0] and not cells[1]
            and all(_money(c) == Decimal("0") for c in cells[2:]))


def combine_accounts(answers: list[TaxAnswer]) -> TaxAnswer:
    """One parcel's answer from each of its tax accounts (Columbia lists some parcels
    under two property ids, each with its own tax page).

    Unknown is never zero: an account that is not ``found`` decides the whole parcel.
    The pages must agree on their "as of" date. The parcel owes what its accounts owe
    together; owner, situs and mailing are kept only where every account agrees, so a
    disagreement holds the lead instead of picking one.
    """
    for answer in answers:
        if answer.outcome != FOUND:
            return answer
    if not answers or len({a.as_of for a in answers}) != 1:
        return TaxAnswer(UNPARSED)

    def same(values):
        values = set(values)
        return values.pop() if len(values) == 1 else None

    return TaxAnswer(
        FOUND, as_of=answers[0].as_of,
        statements=[s for a in answers for s in a.statements],
        owner_name=same(a.owner_name for a in answers),
        property_address=same(a.property_address for a in answers),
        mailing_address=same(a.mailing_address for a in answers),
    )


def _money(text: str) -> Decimal | None:
    m = _MONEY.match(text.strip())
    if not m:
        return None
    try:
        value = Decimal(m.group(2).replace(",", ""))
    except InvalidOperation:
        return None
    return -value if m.group(1) else value


def parse_tax(html: str, parcel_key: str) -> TaxAnswer:
    """One PACS detail page -> its tax statements for ``parcel_key`` (normalised)."""
    from src.scrapers.enrichment.pacs_parcel import FOUND as MAIL_FOUND
    from src.scrapers.enrichment.pacs_parcel import parse_detail

    page = parse_pacs_detail_html(html)
    geo_ids = page["geo_ids"]
    if not geo_ids:
        return TaxAnswer(UNPARSED)
    if len(geo_ids) != 1 or normalize_pacs_parcel(geo_ids[0]) != parcel_key:
        return TaxAnswer(PARCEL_MISMATCH)
    soup = BeautifulSoup(html, "html.parser")
    owner_name = pacs_owner_names(html)
    situs = ", ".join(line for line in (page["situs"] or []) if line) or None
    mail = parse_detail(html, parcel_key)  # same co-owner / ambiguity rules as mailing
    as_of_m = _AS_OF.search(soup.get_text(" "))
    table = soup.find("table", id="ctl00_details_detailsTable")
    if as_of_m is None or table is None:
        return TaxAnswer(UNPARSED)
    as_of = datetime.strptime(as_of_m.group(1), "%m/%d/%Y").date()
    statements: list[Statement] = []
    no_statement_row = False
    own_rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
    # Every row of the table must have a shape we know: the header, a one-cell detail
    # row, or an eight-cell statement/totals row. Anything else is a changed layout,
    # never skipped: a skipped owing row beside a zero marker would read as "nothing
    # owed" (Codex P1, two rounds).
    if not all(_known_row(tr) for tr in own_rows):
        return TaxAnswer(UNPARSED)
    for tr in own_rows:
        if "rowEnd" not in (tr.get("class") or []):
            continue  # headers, and the one-cell detail rows nesting per-district tables
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
        if _is_no_statement_row(tr, cells):
            no_statement_row = True
            continue
        if len(cells) != 8 or not cells[0].isdigit():
            return TaxAnswer(UNPARSED)
        due, penalty, interest = _money(cells[7]), _money(cells[4]), _money(cells[5])
        if due is None or penalty is None or interest is None:
            return TaxAnswer(UNPARSED)
        statements.append(Statement(int(cells[0]), cells[1], due, penalty, interest))
    if not statements and not no_statement_row:
        return TaxAnswer(UNPARSED)  # a renamed row class must not read as "paid"
    return TaxAnswer(
        FOUND, as_of=as_of, statements=statements,
        owner_name=owner_name, property_address=situs,
        mailing_address=mail.mailing_address if mail.outcome == MAIL_FOUND else None,
    )
