"""The Lists CSV, end to end, against the real database and the real exporter.

The sibling test (`test_segments_auction_columns.py`) asserts the SQL TEXT projects
each promised column. That is a fast guard but it trusts one human judgement: whether a
new entry in `OVERLAP_LEAD_COLUMNS` was classified row-backed or derived. Classify a
row-backed column as derived and the guard passes while the column ships blank - which
is exactly the shape of the original bug (one exporter fixed, its sibling missed).

This one cannot be fooled by a classification, because it does not read one. It seeds a
Result with every promised field populated, runs the REAL segment query, feeds the
returned row through the REAL overlap CSV writer, and asserts the rendered cells carry
the seeded values. A column dropped from any SELECT arrives as `getattr(row, name,
None)` -> None -> `""`, and this fails.
"""
import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes import segments
from src.api.tax_filters import tax_cap_condition  # noqa: F401  (bind-name parity)
from src.db.models import Job, Result, ScraperConfig, User
from src.utils.lead_export import OVERLAP_LEAD_COLUMNS, write_lead_csv_with_overlap

NOW = datetime.now(UTC)

# Distinctive seeded values, keyed by the CSV column they must reach. Every one is a
# value a dropped projection would silently replace with "".
SEEDED = {
    "doc_type": "NOTICE OF TRUSTEE SALE",
    "heirs": "GRANTEE PARTY NAME",
    "legal_description": "LOT 7 BLK 2 SUB: TEST ADDITION",
    "parcel_id": "9876543210",
    "party_name": "TESTOWNER JANE",
    "mailing_address": "500 MAIL ST, SEATTLE, WA 98101",
    "property_address": "100 SUBJECT ST, SEATTLE, WA 98101",
    "phone": "2065551234",
    "phone_type": "Mobile",
    "email": "seeded@example.com",
}
AUCTION_DATE = (NOW + timedelta(days=21)).date()
DEFAULT_AMOUNT = Decimal("190752.06")
DELINQUENT_AMOUNT = Decimal("4321.00")
DELINQUENT_BILL_YEAR = NOW.year - 1


async def _seed(db: AsyncSession, user: User) -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"lists csv {uuid.uuid4().hex[:6]}",
        county="king", state="WA", record_type="pre_foreclosure",
        fields=["party_name", "parcel_id"], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status="done", created_at=NOW))
    await db.commit()
    rid = str(uuid.uuid4())
    db.add(Result(
        id=rid, job_id=job_id, user_id=user.id,
        date_recorded="06/02/2026",
        party_name=SEEDED["party_name"], heirs=SEEDED["heirs"],
        legal_description=SEEDED["legal_description"],
        doc_type=SEEDED["doc_type"], parcel_id=SEEDED["parcel_id"],
        property_address=SEEDED["property_address"],
        mailing_address=SEEDED["mailing_address"],
        property_city="SEATTLE", property_state="WA", property_zip="98101",
        phone=SEEDED["phone"], phone_type=SEEDED["phone_type"], email=SEEDED["email"],
        auction_date=AUCTION_DATE, default_amount=DEFAULT_AMOUNT,
        delinquent_amount=DELINQUENT_AMOUNT, delinquent_bill_year=DELINQUENT_BILL_YEAR,
        created_at=NOW,
    ))
    await db.commit()
    return rid


async def _export_row(db: AsyncSession, user: User, rid: str) -> dict:
    """Run the real union query, render the real overlap CSV, return the seeded row."""
    sql = text(segments._UNION_SQL.format(county_clause=""))
    res = await db.execute(sql, {
        "uid": user.id,
        "types": ["pre_foreclosure"],
        "limit": 1000,
        "filing_from": None,
        "filing_to": None,
        "require_date": False,
        "tax_cap_min_year": 1900,
    })
    # The real export path decrypts the EncryptedString/EncryptedJSON columns before
    # rendering: a raw SQL row carries ciphertext, because the ORM decryptor is
    # bypassed. Skipping this step would test a pipeline the product does not use (and
    # the first run of this test duly emitted a Fernet blob into the email column).
    rows = [r for r in segments._decrypt_pii_rows(res.fetchall())
            if str(r.id) == rid]
    assert rows, "the seeded lead did not come back from the segment query"
    pairs = [(r, {"overlap": "Pre-Foreclosure", "lists_count": 1,
                  "lists": "Pre-Foreclosure", "counties": "king"})
             for r in rows]
    out = io.StringIO()
    write_lead_csv_with_overlap(pairs, out)
    parsed = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert len(parsed) == 1
    return parsed[0]


@pytest.mark.parametrize("column", sorted(SEEDED))
async def test_every_seeded_column_survives_to_the_csv(
    db: AsyncSession, starter_user: User, column: str
):
    rid = await _seed(db, starter_user)
    row = await _export_row(db, starter_user, rid)
    assert column in row, f"{column} is not even a header in the Lists CSV"
    assert row[column] == SEEDED[column], (
        f"{column} reached the CSV as {row[column]!r}; a dropped projection renders "
        f"as an empty string and is indistinguishable from a NULL value"
    )


async def test_the_auction_and_tax_columns_carry_their_values(
    db: AsyncSession, starter_user: User
):
    """The columns this branch exists for, plus the tax pair that was blanked the
    same way. `days_to_auction` is derived from auction_date at render time, so its
    presence proves auction_date arrived as a real date and not as text."""
    rid = await _seed(db, starter_user)
    row = await _export_row(db, starter_user, rid)

    assert row["auction_date"] == AUCTION_DATE.isoformat()
    assert Decimal(row["default_amount"]) == DEFAULT_AMOUNT
    assert row["days_to_auction"].strip() != ""
    assert int(row["days_to_auction"]) > 0
    assert Decimal(row["delinquent_amount"]) == DELINQUENT_AMOUNT
    assert int(row["delinquent_bill_year"]) == DELINQUENT_BILL_YEAR


async def test_the_dialer_name_columns_are_populated(
    db: AsyncSession, starter_user: User
):
    """First Name / Last Name are among the most-used columns in a dialer import and
    were permanently blank: `record_type` was SELECTed into the CTE but never
    projected, so `name_order_for()` got None and `split_first_person` returns blanks
    by contract when the source's word order is unknown."""
    rid = await _seed(db, starter_user)
    row = await _export_row(db, starter_user, rid)
    # Seeded party_name is "TESTOWNER JANE" (King pre_foreclosure = surname first).
    assert row["first_name"] == "JANE"
    assert row["last_name"] == "TESTOWNER"


async def test_no_promised_column_is_silently_blank(
    db: AsyncSession, starter_user: User
):
    """The whole-contract sweep: with every row-backed field populated, the only
    blank cells left must be ones that genuinely have no source on this lead.

    This is the check a wrong row-backed/derived classification cannot defeat, because
    it reads the rendered CSV rather than a mapping.
    """
    rid = await _seed(db, starter_user)
    row = await _export_row(db, starter_user, rid)

    # Legitimately blank for THIS fixture, each for a reason in the exporter's own
    # contract - not because a column was dropped from a SELECT.
    allowed_blank = {
        "phone_2", "phone_3", "email_2", "email_3",  # only one contact seeded
        "lead_subtype",                               # no subtype on this lead
        # "Overlap" is the WORD, written only at lists_count >= 2
        # (build_overlap_export_row). This fixture is on one list.
        "overlap",
    }
    blank = {c for c in OVERLAP_LEAD_COLUMNS if not (row.get(c) or "").strip()}
    unexpected = blank - allowed_blank
    assert not unexpected, (
        f"promised columns rendered blank despite being populated: {sorted(unexpected)}"
    )
