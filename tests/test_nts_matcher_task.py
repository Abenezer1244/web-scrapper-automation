"""Guard tests for the NTS DB matcher plumbing (scoring itself is in test_nts_matcher).

The candidate-load/write SQL is integration-level (needs a real DB); here we pin the
cheap invariants: empty input is a no-op (no DB touched) and the module imports +
registers its beat task cleanly.
"""
from src.workers.nts_matcher_task import (
    NTS_MATCH_COUNTIES,
    match_results_inline,
)


def test_inline_empty_is_noop_without_db():
    # `db` is never touched when there are no candidate rows
    assert match_results_inline(db=None, result_dicts=[], county="snohomish") == 0


def test_beat_task_registered():
    from src.workers import app
    assert "src.workers.nts_matcher_task.match_nts_notices" in app.tasks


def test_match_counties_cover_every_crawler():
    # the matcher must be wired for every county that has a crawler populating notices
    assert set(NTS_MATCH_COUNTIES) >= {"pierce", "snohomish", "king", "clark"}


def test_clark_columbian_crawler_registered():
    import src.workers.nts_crawler  # noqa: F401 — import registers the @app.task crawlers
    from src.workers import app
    assert "src.workers.nts_crawler.crawl_nts_columbian_clark" in app.tasks


def test_pdf_crawler_tasks_registered():
    import src.workers.nts_crawler  # noqa: F401 — import registers the @app.task crawlers
    from src.workers import app
    assert "src.workers.nts_crawler.crawl_nts_snoho_tribune" in app.tasks
    assert "src.workers.nts_crawler.crawl_nts_king_queenanne" in app.tasks


# ── Re-match window vs the statutory publication lag (Test 2 audit, 2026-09-02) ──
#
# RCW 61.24.040(1) records a notice of sale >= 90 days (120 with a 61.24.031 letter)
# before the sale and 61.24.040(5) publishes it 35–28 / 14–7 days before the sale,
# so a lead's notice reaches the newspaper cache 55–150 days AFTER recording. A
# 45-day re-match window silently aged leads out before publication (21 real Pierce
# leads found unmatched against an exact-parcel active notice).

def test_rematch_window_covers_statutory_publication_lag():
    from src.workers.nts_matcher_task import _RECENT_DAYS
    assert _RECENT_DAYS >= 150


def test_beat_rematches_lead_created_120_days_ago_against_active_parcel_notice():
    """A pre_foreclosure lead well past the old 45-day window, with an exact
    parcel match to an ACTIVE future-dated notice, is enriched by the beat.

    The lead carries the REAL Pierce ARMS label (doc_type="TRUSTEE SALE", see
    test_pierce_arms_doc_type.py) — the matcher selects by the config's
    record_type, never by doc_type, so the relabel cannot hide rows (Codex)."""
    import uuid
    from datetime import UTC, date, datetime, timedelta
    from decimal import Decimal

    from sqlalchemy import delete

    from src.api.auth import hash_password
    from src.db.models import Job, NtsNotice, Result, ScraperConfig, User
    from src.db.session import SyncSessionLocal
    from src.workers.nts_matcher_task import match_nts_notices

    tag = uuid.uuid4().hex[:8]
    parcel = f"05{tag[:2].encode().hex()[:8]}"  # 10 digits, unlikely to collide
    parcel = "".join(ch if ch.isdigit() else "7" for ch in parcel)[:10].ljust(10, "3")
    auction = date.today() + timedelta(days=23)

    with SyncSessionLocal() as db:
        user = User(
            id=str(uuid.uuid4()), email=f"nts_{tag}@test.bridgeleads.io",
            password_hash=hash_password("TestPass123!"), plan="pro",
            records_used=0, records_limit=1000,
        )
        db.add(user)
        db.flush()
        cfg = ScraperConfig(
            id=str(uuid.uuid4()), user_id=user.id, name=f"nts window {tag}",
            county="pierce", state="WA", record_type="pre_foreclosure",
            fields=["party_name", "parcel_id"], enrichment=[],
            schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
        )
        db.add(cfg)
        db.flush()
        job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id, status="done")
        db.add(job)
        db.flush()
        res = Result(
            id=str(uuid.uuid4()), job_id=job.id, user_id=user.id,
            date_recorded="05/19/2026", party_name="GROVER JAMES", parcel_id=parcel,
            property_address="12111 213TH AVE CT E", doc_type="TRUSTEE SALE",
            created_at=datetime.now(UTC) - timedelta(days=120),
        )
        db.add(res)
        notice = NtsNotice(
            id=str(uuid.uuid4()), source="tacoma_daily_index", ts_number=f"TEST-{tag}",
            county="pierce", state="WA", parcel=parcel,
            property_address="12111 213TH AVE CT E, BONNEY LAKE, WA 98391",
            property_address_normalized="12111 213TH AVE CT E|98391",
            grantor="JAMES GROVER AND ROBIN GROVER, HUSBAND AND WIFE",
            auction_date=auction, principal_owing=Decimal("325241.74"),
            is_active=True, fetched_at=datetime.now(UTC),
        )
        db.add(notice)
        db.commit()
        rid, nid, jid, cid, uid = res.id, notice.id, job.id, cfg.id, user.id

    try:
        summary = match_nts_notices()
        assert summary["matched"] >= 1
        with SyncSessionLocal() as db:
            r = db.get(Result, rid)
            assert r.auction_date == auction
            assert r.default_amount == Decimal("325241.74")
            assert r.nts_notice_id == nid
            assert r.enrichment_data["nts"]["confidence"] >= 0.9
    finally:
        with SyncSessionLocal() as db:
            db.execute(delete(NtsNotice).where(NtsNotice.id == nid))
            db.execute(delete(Result).where(Result.id == rid))
            db.execute(delete(Job).where(Job.id == jid))
            db.execute(delete(ScraperConfig).where(ScraperConfig.id == cid))
            db.execute(delete(User).where(User.id == uid))
            db.commit()
