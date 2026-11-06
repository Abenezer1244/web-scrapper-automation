"""What the stored Section IV amount is (migration 128): 'principal' when the notice
labels it so, 'sum_owing' when it is the unlabelled total (a matured loan), unknown
(NULL) for amounts parsed before the column existed. Real fixtures and a real test DB.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select

from src.db.models import NtsNotice
from src.db.session import SyncSessionLocal
from src.scrapers.sources.nts_tacoma_index import (
    _section_iv_amount,
    notice_to_row,
    parse_nts_notice,
)
from src.workers.nts_crawler import _upsert_notice

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return parse_nts_notice((_FIXTURES / name).read_text(encoding="utf-8"))


def test_a_labelled_principal_is_principal():
    text = ("IV. The sum owing on the obligation secured by the Deed of Trust is: "
            "accrued interest of $1,234.56 plus the principal sum of $200,000.00. V. The")
    assert _section_iv_amount(text) == (Decimal("200000.00"), "principal")


def test_an_unlabelled_total_is_sum_owing_and_no_amount_has_no_basis():
    text = ("IV. The sum owing on the matured obligation secured by the Deed of Trust "
            "is: $575,150.38. V. The above-described real property will be sold")
    assert _section_iv_amount(text) == (Decimal("575150.38"), "sum_owing")
    assert _section_iv_amount("IV. The sum owing on the obligation is: as stated. V. x") == (None, None)
    assert _section_iv_amount("no statutory sentence here") == (None, None)


def test_real_notices_carry_their_basis_into_the_cache_row():
    matured = _fixture("nts_tacoma_matured_obligation.txt")
    assert matured["principal_owing_basis"] == "sum_owing"
    row = notice_to_row(matured, "https://example.test/x", date(2026, 9, 2))
    assert row["principal_owing"] == Decimal("575150.38")
    assert row["principal_owing_basis"] == "sum_owing"

    labelled = _fixture("nts_tacoma_paren_grantor.txt")
    assert labelled["principal_owing"] is not None
    assert labelled["principal_owing_basis"] == "principal"


def _upsert(row: dict) -> NtsNotice:
    with SyncSessionLocal() as db:
        _upsert_notice(db, NtsNotice, row)
        db.commit()
        return db.execute(
            select(NtsNotice).where(NtsNotice.source == row["source"],
                                    NtsNotice.ts_number == row["ts_number"])
        ).scalar_one()


def test_a_reread_that_cannot_find_the_amount_keeps_its_basis_and_an_older_issue_cannot_change_it():
    ts = f"TEST-BASIS-{uuid.uuid4().hex[:8]}"
    base = {"source": "queen_anne_news", "ts_number": ts, "county": "king", "state": "WA",
            "is_active": True, "auction_date": date.today() + timedelta(days=20)}
    url = "https://h/static/QA%20Legals%20{}.pdf"
    try:
        _upsert({**base, "source_url": url.format("10-21-26"), "fetched_at": datetime.now(UTC),
                 "principal_owing": Decimal("491002.57"), "principal_owing_basis": "principal"})
        blank = _upsert({**base, "source_url": url.format("10-28-26"), "fetched_at": datetime.now(UTC),
                         "principal_owing": None, "principal_owing_basis": None})
        assert (blank.principal_owing, blank.principal_owing_basis) == (Decimal("491002.57"), "principal")
        older = _upsert({**base, "source_url": url.format("9-30-26"), "fetched_at": datetime.now(UTC),
                         "principal_owing": Decimal("456249.92"), "principal_owing_basis": "sum_owing"})
        assert (older.principal_owing, older.principal_owing_basis) == (Decimal("491002.57"), "principal")
    finally:
        with SyncSessionLocal() as db:
            db.execute(delete(NtsNotice).where(NtsNotice.ts_number == ts))
            db.commit()


def test_matched_leads_carry_and_follow_their_notice_basis():
    """_write_match stamps the basis; the refresh gives an unchanged amount its basis,
    rebuilds a missing nts object without losing other keys, and leaves a lead alone
    when the notice's basis is still unknown."""
    from src.api.auth import hash_password
    from src.db.models import Job, Result, ScraperConfig, User
    from src.workers.nts_matcher_task import _write_match, refresh_matched_from_notices

    tag = uuid.uuid4().hex[:8]
    sale = date.today() + timedelta(days=30)
    amount = Decimal("575150.38")
    with SyncSessionLocal() as db:
        user = User(id=str(uuid.uuid4()), email=f"ntsbasis_{tag}@test.bridgeleads.io",
                    password_hash=hash_password("TestPass123!"), plan="pro",
                    records_used=0, records_limit=1000)
        db.add(user)
        db.flush()
        cfg = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name=f"basis {tag}",
                            county="pierce", state="WA", record_type="pre_foreclosure",
                            fields=["party_name"], enrichment=[],
                            schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []})
        db.add(cfg)
        db.flush()
        job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id, status="done")
        db.add(job)
        db.flush()

        def notice(ts, basis):
            n = NtsNotice(id=str(uuid.uuid4()), source="tacoma_daily_index",
                          ts_number=f"TEST-{tag}-{ts}", county="pierce", state="WA",
                          auction_date=sale, principal_owing=amount,
                          principal_owing_basis=basis, is_active=True,
                          fetched_at=datetime.now(UTC))
            db.add(n)
            db.flush()
            return n.id

        def lead(nid, enrichment):
            r = Result(id=str(uuid.uuid4()), job_id=job.id, user_id=user.id,
                       party_name="TEST OWNER", auction_date=sale, default_amount=amount,
                       nts_notice_id=nid, enrichment_data=enrichment)
            db.add(r)
            db.flush()
            return r.id

        known = notice("known", "sum_owing")
        unknown = notice("unknown", None)
        ids = {
            "basis_only": lead(known, {"nts": {"trustee": "T"}, "source": "arms"}),
            "no_nts": lead(known, None),  # stored as JSON null, not SQL NULL
            "nts_not_object": lead(known, {"nts": "x", "source": "arms"}),
            "still_unknown": lead(unknown, {"nts": {"trustee": "T"}}),
            "write_match": lead(None, {"source": "arms"}),
        }
        notice_dict = {"id": known, "auction_date": sale, "principal_owing": amount,
                       "principal_owing_basis": "sum_owing", "ts_number": f"TEST-{tag}-known"}
        db.commit()
        uid, jid, cid = user.id, job.id, cfg.id

    try:
        with SyncSessionLocal() as db:
            db.execute(Result.__table__.update().where(Result.id == ids["write_match"])
                       .values(auction_date=None))
            assert _write_match(db, ids["write_match"], notice_dict, 0.9) == 1
            db.commit()
        with SyncSessionLocal() as db:
            assert refresh_matched_from_notices(db) == 3
            assert refresh_matched_from_notices(db) == 0  # idempotent, no churn on unknown
        with SyncSessionLocal() as db:
            def enr(key):
                return db.get(Result, ids[key]).enrichment_data

            assert enr("write_match")["nts"]["amount_basis"] == "sum_owing"
            assert enr("write_match")["source"] == "arms"
            assert enr("basis_only") == {"nts": {"trustee": "T", "amount_basis": "sum_owing"},
                                         "source": "arms"}
            assert enr("no_nts") == {"nts": {"amount_basis": "sum_owing"}}
            assert enr("nts_not_object") == {"nts": {"amount_basis": "sum_owing"}, "source": "arms"}
            assert enr("still_unknown") == {"nts": {"trustee": "T"}}
            assert db.get(Result, ids["basis_only"]).default_amount == amount
    finally:
        with SyncSessionLocal() as db:
            db.execute(delete(Result).where(Result.job_id == jid))
            db.execute(delete(NtsNotice).where(NtsNotice.ts_number.like(f"TEST-{tag}-%")))
            db.execute(delete(Job).where(Job.id == jid))
            db.execute(delete(ScraperConfig).where(ScraperConfig.id == cid))
            db.execute(delete(User).where(User.id == uid))
            db.commit()
