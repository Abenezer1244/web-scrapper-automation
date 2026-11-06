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


def test_an_older_issue_never_labels_an_amount_it_did_not_supply():
    """Legacy row: amount from a newer issue, basis unknown. An older issue with a
    different, labelled figure must not lend its basis to the retained amount; when
    the stored amount is missing, the older issue supplies amount and basis together."""
    url = "https://h/static/QA%20Legals%20{}.pdf"
    tag = uuid.uuid4().hex[:8]
    base = {"source": "queen_anne_news", "county": "king", "state": "WA", "is_active": True,
            "auction_date": date.today() + timedelta(days=20)}
    kept, filled = f"TEST-PAIR-{tag}-a", f"TEST-PAIR-{tag}-b"
    try:
        _upsert({**base, "ts_number": kept, "source_url": url.format("10-21-26"),
                 "fetched_at": datetime.now(UTC), "principal_owing": Decimal("575150.38")})
        older = _upsert({**base, "ts_number": kept, "source_url": url.format("9-30-26"),
                         "fetched_at": datetime.now(UTC), "principal_owing": Decimal("500000.00"),
                         "principal_owing_basis": "principal"})
        assert (older.principal_owing, older.principal_owing_basis) == (Decimal("575150.38"), None)

        _upsert({**base, "ts_number": filled, "source_url": url.format("10-21-26"),
                 "fetched_at": datetime.now(UTC), "principal_owing": None,
                 "principal_owing_basis": None})
        older = _upsert({**base, "ts_number": filled, "source_url": url.format("9-30-26"),
                         "fetched_at": datetime.now(UTC), "principal_owing": Decimal("500000.00"),
                         "principal_owing_basis": "principal"})
        assert (older.principal_owing, older.principal_owing_basis) == (Decimal("500000.00"), "principal")
    finally:
        with SyncSessionLocal() as db:
            db.execute(delete(NtsNotice).where(NtsNotice.ts_number.like(f"TEST-PAIR-{tag}-%")))
            db.commit()


def test_the_resweep_reaches_a_missing_amount_before_any_unknown_basis():
    from src.workers.nts_crawler import _RESWEEP_LIMIT, _RESWEEP_SELECT

    tag = uuid.uuid4().hex[:8]
    source = f"test_resweep_{tag}"
    soon = date.today() + timedelta(days=5)
    with SyncSessionLocal() as db:
        for i in range(_RESWEEP_LIMIT + 1):  # more legacy rows than one run can take, all sooner
            db.add(NtsNotice(id=str(uuid.uuid4()), source=source, ts_number=f"TEST-RS-{tag}-{i}",
                             county="pierce", state="WA", auction_date=soon,
                             principal_owing=Decimal("1.00"), source_url="https://h/x",
                             is_active=True))
        db.add(NtsNotice(id=str(uuid.uuid4()), source=source, ts_number=f"TEST-RS-{tag}-amount",
                         county="pierce", state="WA", auction_date=soon + timedelta(days=30),
                         principal_owing=None, source_url="https://h/y", is_active=True))
        db.commit()
    try:
        with SyncSessionLocal() as db:
            picked = [r.ts_number for r in db.execute(_RESWEEP_SELECT, {
                "source": source, "today": date.today(),
                "stale_before": datetime.now(UTC), "lim": _RESWEEP_LIMIT}).fetchall()]
        assert len(picked) == _RESWEEP_LIMIT
        assert picked[0] == f"TEST-RS-{tag}-amount"
    finally:
        with SyncSessionLocal() as db:
            db.execute(delete(NtsNotice).where(NtsNotice.source == source))
            db.commit()


def test_the_csv_says_principal_only_when_the_notice_does():
    from types import SimpleNamespace

    from src.utils.lead_export import build_lead_export_row

    def label(record):
        return build_lead_export_row(record)["default_amount_type"]

    def lead(amount, basis=None, nts=True):
        enr = {"nts": {"trustee": "T", "amount_basis": basis}} if nts else {}
        return {"default_amount": amount, "enrichment_data": enr}

    assert label(lead("296494.82", "principal")) == "Principal"
    assert label(lead("575150.38", "sum_owing")) == "Amount stated in notice"
    assert label(lead("254015.94")) == "Amount stated in notice"  # read before migration 128
    assert label(lead("254015.94", nts=False)) == "Amount stated in notice"
    assert label(lead(None, "principal")) == ""
    # A Lists (segment) row has no enrichment_data: its query projects the basis.
    seg = SimpleNamespace(default_amount=Decimal("1.00"), default_amount_basis="principal",
                          enrichment_data=None)
    assert label(seg) == "Principal"


def test_the_new_column_lands_after_every_existing_one():
    from src.utils.lead_export import (
        BATCH_LEAD_COLUMNS,
        LEAD_CSV_COLUMNS,
        OVERLAP_LEAD_COLUMNS,
        resolve_crm_export_columns,
        resolve_lead_export_columns,
    )

    for cols in (LEAD_CSV_COLUMNS, OVERLAP_LEAD_COLUMNS, BATCH_LEAD_COLUMNS,
                 resolve_lead_export_columns("pre_foreclosure"),
                 resolve_lead_export_columns("trustee_sale"),
                 resolve_crm_export_columns("pre_foreclosure"),
                 resolve_crm_export_columns(None)):
        assert cols[-1] == "default_amount_type"
        assert cols.count("default_amount_type") == 1
    for record_type in ("probate", "tax_delinquent", "code_violation"):
        assert "default_amount_type" not in resolve_lead_export_columns(record_type)
        assert "default_amount_type" not in resolve_crm_export_columns(record_type)


def test_an_auction_lead_freezes_the_basis_with_its_amount():
    import json

    from src.scrapers.trustee_sale import _record_from_notice
    from src.workers.trustee_sale_finalize import _nts_update_params

    notice = NtsNotice(id="n-1", source="tacoma_daily_index", ts_number="WA-26-1",
                       county="pierce", state="WA", auction_date=date(2026, 12, 1),
                       principal_owing=Decimal("575150.38"), principal_owing_basis="sum_owing",
                       property_address="1 MAIN ST, TACOMA, WA 98402", grantor="TEST OWNER")
    src = _record_from_notice(notice).enrichment_data["nts_source"]
    assert (src["default_amount"], src["amount_basis"]) == ("575150.38", "sum_owing")
    assert json.loads(_nts_update_params("r", src)["nts"])["amount_basis"] == "sum_owing"
    # A snapshot frozen before migration 128 has no basis: unknown, never principal.
    legacy = {k: v for k, v in src.items() if k != "amount_basis"}
    assert json.loads(_nts_update_params("r", legacy)["nts"])["amount_basis"] is None
