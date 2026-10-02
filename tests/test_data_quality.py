"""Per-run data-quality coverage against the county x record-type baseline.

The shape this guards against is Clark job 62404bd0 (2026-10-02): 1,335 parcels,
1,292 property addresses, 0 mailing addresses, "Enrichment complete". Real DB rows,
real Redis marker, real ops-alert persistence (audit_events); no PII is read.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.workers import data_quality as dq


async def _job(db, user: User, *, county: str = "benton", record_type: str = "probate",
               finished_hours_ago: float = 1.0) -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"{county} {record_type} dq",
        county=county, state="WA", record_type=record_type,
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status="done",
               trigger="manual", record_count=0, billed_count=0,
               finished_at=datetime.now(UTC) - timedelta(hours=finished_hours_ago)))
    await db.commit()
    return job_id


async def _rows(db, user: User, job_id: str, *, n: int, with_mailing: int,
                echo: bool = False, duplicates: int = 0, with_property: int | None = None) -> None:
    with_property = n if with_property is None else with_property
    for i in range(n + duplicates):
        prop = f"{100 + i} MAIN ST" if i < with_property else None
        mail = None
        if i < with_mailing:
            mail = prop if echo else f"PO BOX {i}, PHOENIX, AZ 85001"
        db.add(Result(
            id=str(uuid.uuid4()), job_id=job_id, user_id=user.id, party_name=f"P{i}",
            parcel_id=f"13107301112{i:04d}", property_address=prop, mailing_address=mail,
            is_duplicate=i >= n, enrichment_data={"mailing_source": "pacs_benton"} if mail else {},
        ))
    await db.commit()


def _sync():
    from src.db.session import system_sync_session

    return system_sync_session()


class TestEvaluate:
    """Pure rules over measured shares."""

    @staticmethod
    def _m(rows: int, mailing: float, *, echo: float = 0.0, **pct) -> dict:
        p = dict.fromkeys(dq.FIELDS, 90.0)
        p.update(mailing_address=mailing, phone=0.0, email=0.0, auction_date=0.0, default_amount=0.0)
        p.update(pct)
        return {"rows": rows, "pct": p, "echo_pct": echo, "deferred": 0, "mailing_sources": {}}

    def test_a_mailing_collapse_against_a_real_baseline_is_a_warning(self):
        warnings = dq.evaluate(self._m(20, 0.0), self._m(200, 82.0))
        assert [(w["field"], w["kind"]) for w in warnings] == [("mailing_address", "coverage_collapse")]
        assert warnings[0]["run_pct"] == 0.0 and warnings[0]["baseline_pct"] == 82.0

    def test_a_small_dip_is_not_a_warning(self):
        assert dq.evaluate(self._m(20, 60.0), self._m(200, 82.0)) == []

    def test_no_baseline_means_no_warning(self):
        """The first runs of a new county are the baseline being born."""
        assert dq.evaluate(self._m(500, 0.0), self._m(0, 0.0)) == []
        assert dq.evaluate(self._m(500, 0.0), self._m(dq.MIN_BASELINE_ROWS - 1, 90.0)) == []

    def test_a_tiny_run_is_not_judged(self):
        assert dq.evaluate(self._m(dq.MIN_RUN_ROWS - 1, 0.0), self._m(500, 90.0)) == []

    def test_a_field_the_baseline_rarely_has_is_not_compared(self):
        """Auction date on probate is normally absent; its absence is not a collapse."""
        base = self._m(500, 80.0, auction_date=3.0)
        assert dq.evaluate(self._m(50, 80.0, auction_date=0.0), base) == []

    def test_every_mailing_equal_to_property_is_flagged_as_an_echo(self):
        warnings = dq.evaluate(self._m(50, 100.0, echo=100.0), self._m(500, 80.0, echo=40.0))
        assert [w["kind"] for w in warnings] == ["mailing_echoes_property"]

    def test_an_owner_occupied_county_is_not_an_echo_warning(self):
        assert dq.evaluate(self._m(50, 100.0, echo=100.0), self._m(500, 80.0, echo=90.0)) == []

    def test_phone_and_email_are_judged_only_with_skip_tracing_on(self):
        """A config without skip tracing has no phone/email by design (prod: Pierce
        probate 4474edb2 read as 0% phone vs a 34% baseline). Not a collapse."""
        run, base = self._m(50, 90.0, phone=0.0, email=0.0), self._m(500, 90.0, phone=35.0, email=40.0)
        assert dq.evaluate(run, base, skip_trace=False) == []
        assert {w["field"] for w in dq.evaluate(run, base, skip_trace=True)} == {"phone", "email"}

    def test_a_property_collapse_is_reported_too(self):
        warnings = dq.evaluate(self._m(50, 80.0, property_address=20.0), self._m(500, 80.0))
        assert [w["field"] for w in warnings] == ["property_address"]


class TestMeasure:
    async def test_run_coverage_counts_new_rows_only(self, db, starter_user):
        job = await _job(db, starter_user)
        await _rows(db, starter_user, job, n=8, with_mailing=6, duplicates=4)
        with _sync() as s:
            run = dq.run_coverage(s, job)
        assert run["rows"] == 8
        assert run["pct"]["mailing_address"] == 75.0
        assert run["pct"]["parcel_id"] == 100.0 and run["pct"]["phone"] == 0.0
        assert run["echo_pct"] == 0.0
        assert run["mailing_sources"] == {"pacs_benton": 6}

    async def test_echo_share_is_over_mailing_rows(self, db, starter_user):
        job = await _job(db, starter_user)
        await _rows(db, starter_user, job, n=10, with_mailing=4, echo=True)
        with _sync() as s:
            run = dq.run_coverage(s, job)
        assert run["pct"]["mailing_address"] == 40.0 and run["echo_pct"] == 100.0

    async def test_baseline_excludes_the_job_itself_and_other_counties(self, db, starter_user):
        this = await _job(db, starter_user)
        await _rows(db, starter_user, this, n=10, with_mailing=0)
        other = await _job(db, starter_user, finished_hours_ago=48)
        await _rows(db, starter_user, other, n=20, with_mailing=16)
        clark = await _job(db, starter_user, county="clark")
        await _rows(db, starter_user, clark, n=30, with_mailing=0)
        with _sync() as s:
            base = dq.baseline_coverage(s, job_id=this, county="BENTON", state="wa", record_type="probate")
        assert base["rows"] == 20 and base["pct"]["mailing_address"] == 80.0


class TestCheckJob:
    async def test_the_clark_shape_raises_a_durable_alert(self, db, starter_user, monkeypatch):
        """Parcels and property addresses present, 0 mailing, while the county's
        baseline carries mailing on most rows."""
        from src.config import settings

        monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")  # no e-mail; the row still lands
        base = await _job(db, starter_user, finished_hours_ago=30)
        await _rows(db, starter_user, base, n=60, with_mailing=50)
        bad = await _job(db, starter_user)
        await _rows(db, starter_user, bad, n=15, with_mailing=0)
        with _sync() as s:
            report = dq.check_job(s, bad)
        # The durable row is written off the running loop (fire-and-forget executor).
        rows: list = []
        for _ in range(30):
            with _sync() as s:
                rows = s.execute(text(
                    "SELECT detail FROM audit_events WHERE event = 'ops_alert' AND path = :p ORDER BY created_at DESC"
                ), {"p": "data_quality:benton/probate"}).scalars().all()
            if rows:
                break
            await asyncio.sleep(0.1)
        assert [w["field"] for w in report["warnings"]] == ["mailing_address"]
        assert report["run"]["pct"]["mailing_address"] == 0.0
        assert report["baseline"]["pct"]["mailing_address"] == pytest.approx(83.3, abs=0.1)
        assert rows and "fell below its baseline" in rows[0]

    async def test_a_normal_run_raises_nothing(self, db, starter_user):
        base = await _job(db, starter_user, finished_hours_ago=30)
        await _rows(db, starter_user, base, n=60, with_mailing=50)
        good = await _job(db, starter_user)
        await _rows(db, starter_user, good, n=15, with_mailing=12)
        with _sync() as s:
            report = dq.check_job(s, good)
        assert report["warnings"] == []

    async def test_report_mode_never_alerts(self, db, starter_user, monkeypatch):
        base = await _job(db, starter_user, finished_hours_ago=30)
        await _rows(db, starter_user, base, n=60, with_mailing=50)
        bad = await _job(db, starter_user)
        await _rows(db, starter_user, bad, n=15, with_mailing=0)
        called = []
        import src.workers.ops_alerts as oa

        monkeypatch.setattr(oa, "send_ops_alert", lambda *a, **k: called.append(a))
        with _sync() as s:
            report = dq.check_job(s, bad, alert=False)
        assert report["warnings"] and called == []


class TestSweep:
    async def test_each_finished_job_is_judged_once(self, db, starter_user, redis_client):
        job = await _job(db, starter_user)
        await _rows(db, starter_user, job, n=12, with_mailing=10)
        redis_client.delete(dq._checked_key(job))
        first = dq.run_data_quality_sweep(lookback_hours=2, limit=500)
        second = dq.run_data_quality_sweep(lookback_hours=2, limit=500)
        assert first["checked"] >= 1
        assert redis_client.get(dq._checked_key(job)) in (b"1", "1")
        assert second["skipped"] >= 1 and second["checked"] < first["checked"]

    async def test_the_sweep_is_registered_hourly_off_the_king_minutes(self):
        from src.workers.scheduler import app

        entry = app.conf.beat_schedule["data-quality-sweep"]
        assert entry["task"] == "src.workers.data_quality.data_quality_sweep"
        assert set(entry["schedule"].minute) == {33}
