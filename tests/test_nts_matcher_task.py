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


# ── King PIN/account bridge, end to end ───────────────────────────────────────
#
# The scorer is unit-tested in test_nts_matcher.py. These assert the VALUES actually
# land on the Result (Codex: "assert the attached auction_date and principal_owing,
# not merely the match score"), and that the whole pipeline — candidate pool indexing,
# scoring, grouping, write guard — carries a bridged pair through.
#
# Real shape from prod 2026-09-19: the recorder indexes the 10-digit PIN while the
# trustee printed the 12-digit tax account number on TS WA07000188-22-3.

def _bridge_fixture(db, *, county, notice_parcel, result_parcel, tag):
    """Build user/config/job/result + a notice for one bridge scenario. Returns ids."""
    import uuid
    from datetime import UTC, date, datetime, timedelta
    from decimal import Decimal

    from src.api.auth import hash_password
    from src.db.models import Job, NtsNotice, Result, ScraperConfig, User

    auction = date.today() + timedelta(days=19)
    user = User(
        id=str(uuid.uuid4()), email=f"bridge_{tag}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"), plan="pro",
        records_used=0, records_limit=1000,
    )
    db.add(user)
    db.flush()
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"bridge {tag}",
        county=county, state="WA", record_type="pre_foreclosure",
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
        date_recorded="06/02/2026", party_name="SIMON JOANN", parcel_id=result_parcel,
        property_address="11120 NE 68TH ST #B-206 98033",
        doc_type="NOTICE OF TRUSTEE SALE",
        created_at=datetime.now(UTC) - timedelta(days=95),
    )
    db.add(res)
    notice = NtsNotice(
        id=str(uuid.uuid4()), source="queen_anne_news", ts_number=f"TEST-{tag}",
        county=county, state="WA", parcel=notice_parcel,
        property_address="11120 NE 68TH STREET, UNIT B-206, KIRKLAND, WA 98033",
        property_address_normalized="11120 NE 68TH ST|98033",
        grantor="JOANN SIMON, AN UNMARRIED INDIVIDUAL",
        auction_date=auction, principal_owing=Decimal("190752.06"),
        is_active=True, fetched_at=datetime.now(UTC),
    )
    db.add(notice)
    db.commit()
    return {"result": res.id, "notice": notice.id, "job": job.id,
            "config": cfg.id, "user": user.id, "auction": auction}


def _cleanup(ids):
    from sqlalchemy import delete

    from src.db.models import Job, NtsNotice, Result, ScraperConfig, User
    from src.db.session import SyncSessionLocal
    with SyncSessionLocal() as db:
        db.execute(delete(NtsNotice).where(NtsNotice.id == ids["notice"]))
        db.execute(delete(Result).where(Result.id == ids["result"]))
        db.execute(delete(Job).where(Job.id == ids["job"]))
        db.execute(delete(ScraperConfig).where(ScraperConfig.id == ids["config"]))
        db.execute(delete(User).where(User.id == ids["user"]))
        db.commit()


def test_king_account_notice_enriches_a_pin_lead_end_to_end():
    """12-digit account on the notice, 10-digit PIN on the lead: the auction date and
    the amount owing must actually be written."""
    import uuid
    from decimal import Decimal

    from src.db.models import Result
    from src.db.session import SyncSessionLocal
    from src.workers.nts_matcher_task import match_nts_notices

    tag = uuid.uuid4().hex[:8]
    pin = "".join(c for c in tag if c.isdigit()).ljust(10, "7")[:10]
    with SyncSessionLocal() as db:
        ids = _bridge_fixture(
            db, county="king", notice_parcel=f"{pin[:6]}-{pin[6:]}-08",
            result_parcel=pin, tag=tag,
        )
    try:
        assert match_nts_notices()["matched"] >= 1
        with SyncSessionLocal() as db:
            r = db.get(Result, ids["result"])
            assert r.auction_date == ids["auction"]
            assert r.default_amount == Decimal("190752.06")
            assert r.nts_notice_id == ids["notice"]
            assert r.enrichment_data["nts"]["ts_number"] == f"TEST-{tag}"
            assert float(r.nts_match_confidence) == 0.95
    finally:
        _cleanup(ids)


def test_same_shape_in_another_county_writes_nothing():
    """The bridge is county-gated: an identical 10/12 pair in Pierce must stay a
    conflict and leave the lead's auction fields NULL."""
    import uuid

    from src.db.models import Result
    from src.db.session import SyncSessionLocal
    from src.workers.nts_matcher_task import match_nts_notices

    tag = uuid.uuid4().hex[:8]
    pin = "".join(c for c in tag if c.isdigit()).ljust(10, "5")[:10]
    with SyncSessionLocal() as db:
        ids = _bridge_fixture(
            db, county="pierce", notice_parcel=f"{pin}08", result_parcel=pin, tag=tag,
        )
    try:
        match_nts_notices()
        with SyncSessionLocal() as db:
            r = db.get(Result, ids["result"])
            assert r.auction_date is None
            assert r.default_amount is None
            assert r.nts_notice_id is None
    finally:
        _cleanup(ids)


# ── Why a lead has no auction data ────────────────────────────────────────────

class TestAuctionMissingReason:
    """A blank Auction Date has several very different causes. Stamping which one
    turns "King shows N/A on everything" from a session's work into one query."""

    def _reason(self, *, county="king", age_days=200, has_source=True):
        from datetime import date, timedelta

        from src.workers.nts_matcher_task import auction_missing_reason
        today = date(2026, 9, 20)
        return auction_missing_reason(
            county=county,
            date_recorded=None if age_days is None else today - timedelta(days=age_days),
            today=today, has_source=has_source,
        )

    def test_county_without_a_crawler(self):
        from src.workers.nts_matcher_task import NTS_MISSING_NO_SOURCE
        assert self._reason(county="spokane", has_source=False) == NTS_MISSING_NO_SOURCE

    def test_recorded_too_recently_to_have_been_published(self):
        # RCW 61.24.040: the notice cannot legally have been published yet, so a blank
        # here is not evidence the source lacks it.
        from src.workers.nts_matcher_task import NTS_MISSING_NOT_PUBLISHED
        assert self._reason(age_days=10) == NTS_MISSING_NOT_PUBLISHED
        assert self._reason(age_days=54) == NTS_MISSING_NOT_PUBLISHED

    def test_past_the_publication_window_means_the_source_has_nothing(self):
        from src.workers.nts_matcher_task import NTS_MISSING_NO_NOTICE
        assert self._reason(age_days=55) == NTS_MISSING_NO_NOTICE
        assert self._reason(age_days=400) == NTS_MISSING_NO_NOTICE

    def test_no_recording_date_is_unknown_not_a_guess(self):
        from src.workers.nts_matcher_task import NTS_MISSING_UNKNOWN
        assert self._reason(age_days=None) == NTS_MISSING_UNKNOWN

    def test_no_source_outranks_every_other_reason(self):
        from src.workers.nts_matcher_task import NTS_MISSING_NO_SOURCE
        assert self._reason(age_days=None, has_source=False) == NTS_MISSING_NO_SOURCE
        assert self._reason(age_days=5, has_source=False) == NTS_MISSING_NO_SOURCE

    def test_the_window_matches_the_statutory_minimum(self):
        from src.workers.nts_matcher_task import _PUBLICATION_LAG_MIN_DAYS
        # >= 90 days recording-to-sale, published no later than 7 days before the sale.
        assert 40 <= _PUBLICATION_LAG_MIN_DAYS <= 90


def test_unmatched_lead_is_stamped_with_its_reason_and_matched_one_is_not():
    """The beat records why each blank lead is blank, and never stamps a lead that
    actually received auction data."""
    import uuid
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete

    from src.db.models import Result
    from src.db.session import SyncSessionLocal
    from src.workers.nts_matcher_task import (
        NTS_MISSING_NO_NOTICE,
        NTS_MISSING_NOT_PUBLISHED,
        match_nts_notices,
    )

    tag = uuid.uuid4().hex[:8]
    pin = "".join(c for c in tag if c.isdigit()).ljust(10, "4")[:10]
    with SyncSessionLocal() as db:
        # A matched King lead (bridged), plus two unmatched ones with different ages.
        ids = _bridge_fixture(
            db, county="king", notice_parcel=f"{pin}04", result_parcel=pin, tag=tag,
        )
        fresh = Result(
            id=str(uuid.uuid4()), job_id=ids["job"], user_id=ids["user"],
            date_recorded=(datetime.now(UTC) - timedelta(days=9)).strftime("%m/%d/%Y"),
            party_name="RECENT FILING", parcel_id="8" + pin[1:],
            property_address="1 RECENT ST", doc_type="NOTICE OF TRUSTEE SALE",
            created_at=datetime.now(UTC) - timedelta(days=9),
        )
        stale = Result(
            id=str(uuid.uuid4()), job_id=ids["job"], user_id=ids["user"],
            date_recorded=(datetime.now(UTC) - timedelta(days=150)).strftime("%m/%d/%Y"),
            party_name="OLD FILING", parcel_id="9" + pin[1:],
            property_address="2 OLD ST", doc_type="NOTICE OF TRUSTEE SALE",
            created_at=datetime.now(UTC) - timedelta(days=150),
        )
        db.add_all([fresh, stale])
        db.commit()
        fresh_id, stale_id = fresh.id, stale.id
    try:
        match_nts_notices()
        with SyncSessionLocal() as db:
            assert db.get(Result, fresh_id).enrichment_data["nts_missing"] == \
                NTS_MISSING_NOT_PUBLISHED
            assert db.get(Result, stale_id).enrichment_data["nts_missing"] == \
                NTS_MISSING_NO_NOTICE
            # The lead that DID get auction data carries no missing-reason.
            matched = db.get(Result, ids["result"])
            assert matched.auction_date is not None
            assert "nts_missing" not in (matched.enrichment_data or {})
    finally:
        with SyncSessionLocal() as db:
            db.execute(delete(Result).where(Result.id.in_([fresh_id, stale_id])))
            db.commit()
        _cleanup(ids)


def test_stamping_does_not_clobber_existing_enrichment_keys():
    """enrichment_data is MERGED — the scraper's instrument_number must survive."""
    import uuid
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete

    from src.db.models import Result
    from src.db.session import SyncSessionLocal
    from src.workers.nts_matcher_task import match_nts_notices

    tag = uuid.uuid4().hex[:8]
    pin = "".join(c for c in tag if c.isdigit()).ljust(10, "6")[:10]
    with SyncSessionLocal() as db:
        ids = _bridge_fixture(
            db, county="king", notice_parcel=f"{pin}04", result_parcel=pin, tag=tag,
        )
        lonely = Result(
            id=str(uuid.uuid4()), job_id=ids["job"], user_id=ids["user"],
            date_recorded=(datetime.now(UTC) - timedelta(days=170)).strftime("%m/%d/%Y"),
            party_name="NO NOTICE", parcel_id="7" + pin[1:],
            property_address="3 LONELY LN", doc_type="NOTICE OF TRUSTEE SALE",
            enrichment_data={"instrument_number": "20260918000910",
                             "source": "king_landmark_json"},
            created_at=datetime.now(UTC) - timedelta(days=170),
        )
        db.add(lonely)
        db.commit()
        lonely_id = lonely.id
    try:
        match_nts_notices()
        with SyncSessionLocal() as db:
            enr = db.get(Result, lonely_id).enrichment_data
            assert enr["instrument_number"] == "20260918000910"
            assert enr["source"] == "king_landmark_json"
            assert enr["nts_missing"]
    finally:
        with SyncSessionLocal() as db:
            db.execute(delete(Result).where(Result.id == lonely_id))
            db.commit()
        _cleanup(ids)


def test_bridged_match_is_idempotent_and_does_not_rewrite():
    """Re-running the beat claims nothing new — the backfill path is a no-op on a
    lead that already carries the notice (no duplicate work, no churn)."""
    import uuid

    from src.db.models import Result
    from src.db.session import SyncSessionLocal
    from src.workers.nts_matcher_task import match_nts_notices

    tag = uuid.uuid4().hex[:8]
    pin = "".join(c for c in tag if c.isdigit()).ljust(10, "9")[:10]
    with SyncSessionLocal() as db:
        ids = _bridge_fixture(
            db, county="king", notice_parcel=f"{pin}04", result_parcel=pin, tag=tag,
        )
    try:
        assert match_nts_notices()["matched"] >= 1
        with SyncSessionLocal() as db:
            first = db.get(Result, ids["result"]).enrichment_data["nts"]["matched_at"]
        # Second pass: the row now holds a FUTURE auction, so the write guard skips it.
        match_nts_notices()
        with SyncSessionLocal() as db:
            r = db.get(Result, ids["result"])
            assert r.enrichment_data["nts"]["matched_at"] == first
            assert r.auction_date == ids["auction"]
    finally:
        _cleanup(ids)
