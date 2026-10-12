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
               finished_hours_ago: float = 1.0, records_found: int | None = None,
               window: tuple[str, str] | None = None, doc_types: list | None = None) -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"{county} {record_type} dq",
        county=county, state="WA", record_type=record_type,
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []}, doc_types=doc_types,
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status="done",
               trigger="manual", record_count=0, billed_count=0,
               records_found=records_found,
               date_from=window[0] if window else None, date_to=window[1] if window else None,
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

    async def test_a_failed_check_releases_the_claim_so_the_job_is_judged_next_tick(
            self, db, starter_user, redis_client, monkeypatch):
        """A transient error must not silence a job for 14 days (Codex P1)."""
        job = await _job(db, starter_user)
        await _rows(db, starter_user, job, n=12, with_mailing=10)
        redis_client.delete(dq._checked_key(job))
        calls = {"n": 0}

        def _boom(db_, job_id, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db hiccup")
            return {"job_id": job_id, "warnings": []}

        monkeypatch.setattr(dq, "check_job", _boom)
        first = dq.run_data_quality_sweep(lookback_hours=2, limit=500)
        assert redis_client.get(dq._checked_key(job)) is None  # claim released
        second = dq.run_data_quality_sweep(lookback_hours=2, limit=500)
        assert first["checked"] < second["checked"] + 1 and calls["n"] >= 2
        assert redis_client.get(dq._checked_key(job)) in (b"1", "1")

    def test_the_judged_mark_never_overwrites_another_workers_live_claim(self, redis_client):
        key = "bl:dq:checked:test-mark"
        redis_client.set(key, "their-token", ex=60)
        dq._mark_judged(redis_client, key, "my-token")
        assert redis_client.get(key) in (b"their-token", "their-token")
        redis_client.set(key, "my-token", ex=60)
        dq._mark_judged(redis_client, key, "my-token")
        assert redis_client.get(key) in (b"1", "1")
        redis_client.delete(key)
        dq._mark_judged(redis_client, key, "my-token")  # our claim expired mid-check
        assert redis_client.get(key) in (b"1", "1")
        redis_client.delete(key)

    async def test_the_sweep_is_registered_hourly_off_the_king_minutes(self):
        from src.workers.scheduler import app

        entry = app.conf.beat_schedule["data-quality-sweep"]
        assert entry["task"] == "src.workers.data_quality.data_quality_sweep"
        assert set(entry["schedule"].minute) == {33}


# ── Volume (P0-08): a source that silently returns nothing ─────────────────────

_WEEK = ("10/01/2026", "10/07/2026")
_QUARTER = ("07/10/2026", "10/07/2026")


class TestEvaluateVolume:
    @staticmethod
    def _v(found, *, rate=4.0, days=7, jobs=6, record_type="probate"):
        return {"records_found": found, "median_rate": rate, "days": days,
                "baseline_jobs": jobs, "record_type": record_type}

    def test_zero_against_a_steady_source_is_a_collapse(self):
        w = dq.evaluate_volume(self._v(0))
        assert w["kind"] == "volume_collapse" and w["expected"] == 28.0 and w["found"] == 0

    def test_a_normal_or_slow_week_is_not(self):
        assert dq.evaluate_volume(self._v(20)) is None
        assert dq.evaluate_volume(self._v(6)) is None  # 21% of expected

    def test_a_quiet_source_is_never_judged_on_chance(self):
        """Expected under 10: a zero is what a rural county does."""
        assert dq.evaluate_volume(self._v(0, rate=1.0)) is None

    def test_too_few_comparable_runs_or_no_window_is_no_judgement(self):
        assert dq.evaluate_volume(self._v(0, jobs=dq.VOLUME_MIN_BASELINE_JOBS - 1)) is None
        assert dq.evaluate_volume(self._v(0, days=None)) is None
        assert dq.evaluate_volume(None) is None

    def test_trustee_sale_is_not_judged(self):
        assert dq.evaluate_volume(self._v(0, record_type="trustee_sale")) is None


async def _baseline(db, user, county, n=6, found=30, window=_WEEK, doc_types=None,
                    record_type="probate"):
    for i in range(n):
        await _job(db, user, county=county, record_type=record_type, records_found=found,
                   window=window, doc_types=doc_types, finished_hours_ago=24 + i)


async def _audit_paths(prefix: str) -> list[str]:
    rows: list = []
    for _ in range(30):
        with _sync() as s:
            rows = s.execute(text(
                "SELECT path FROM audit_events WHERE event = 'ops_alert' AND path LIKE :p"),
                {"p": prefix + "%"}).scalars().all()
        if rows:
            break
        await asyncio.sleep(0.1)
    return rows


class TestVolume:
    async def test_a_silent_source_raises_a_durable_alert(self, db, starter_user, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
        county = f"dqv{uuid.uuid4().hex[:6]}"
        await _baseline(db, starter_user, county)
        silent = await _job(db, starter_user, county=county, records_found=0, window=_WEEK)
        with _sync() as s:
            report = dq.check_job(s, silent)
        vol = [w for w in report["warnings"] if w["kind"] == "volume_collapse"]
        assert vol == [{"kind": "volume_collapse", "found": 0, "expected": 30.0,
                        "window_days": 7, "baseline_jobs": 6, "doc_types": None}]
        assert await _audit_paths(f"data_quality_volume:{county}/") == [
            f"data_quality_volume:{county}/WA/probate/all"]

    async def test_a_normal_run_raises_no_volume_warning(self, db, starter_user):
        county = f"dqv{uuid.uuid4().hex[:6]}"
        await _baseline(db, starter_user, county)
        ok = await _job(db, starter_user, county=county, records_found=27, window=_WEEK)
        with _sync() as s:
            assert dq.check_job(s, ok, alert=False)["warnings"] == []

    async def test_runs_with_a_far_wider_window_are_not_its_baseline(self, db, starter_user):
        """A 7-day run on a lagged county is not judged against 90-day runs."""
        county = f"dqv{uuid.uuid4().hex[:6]}"
        await _baseline(db, starter_user, county, found=900, window=_QUARTER)
        week = await _job(db, starter_user, county=county, records_found=0, window=_WEEK)
        with _sync() as s:
            v = dq.volume(s, week)
        assert v["baseline_jobs"] == 0 and dq.evaluate_volume(v) is None

    async def test_another_document_selection_is_not_its_baseline(self, db, starter_user):
        county = f"dqv{uuid.uuid4().hex[:6]}"
        await _baseline(db, starter_user, county, doc_types=["NOTICE OF TRUSTEE SALE"])
        narrow = await _job(db, starter_user, county=county, records_found=0, window=_WEEK,
                            doc_types=["LIS PENDENS"])
        with _sync() as s:
            assert dq.volume(s, narrow)["baseline_jobs"] == 0

    async def test_a_zero_run_is_judged_although_it_is_below_the_coverage_minimum(
            self, db, starter_user):
        """The coverage check skips runs under MIN_RUN_ROWS; volume must not."""
        county = f"dqv{uuid.uuid4().hex[:6]}"
        await _baseline(db, starter_user, county)
        silent = await _job(db, starter_user, county=county, records_found=0, window=_WEEK)
        with _sync() as s:
            report = dq.check_job(s, silent, alert=False)
        assert report["run"]["rows"] == 0
        assert [w["kind"] for w in report["warnings"]] == ["volume_collapse"]


    async def test_the_same_selection_in_another_order_is_its_baseline(self, db, starter_user):
        county = f"dqv{uuid.uuid4().hex[:6]}"
        await _baseline(db, starter_user, county, doc_types=["NOTICE OF DEFAULT", "LIS PENDENS"])
        job = await _job(db, starter_user, county=county, records_found=0, window=_WEEK,
                         doc_types=["NOTICE OF DEFAULT", "LIS PENDENS", "LIS PENDENS"])
        with _sync() as s:
            assert dq.volume(s, job)["baseline_jobs"] == 6

    async def test_a_coverage_and_a_volume_collapse_together_both_alert(
            self, db, starter_user, monkeypatch):
        """The volume warning never reaches the coverage formatter (Codex asked)."""
        from src.config import settings

        monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
        county = f"dqv{uuid.uuid4().hex[:6]}"
        await _baseline(db, starter_user, county, found=300)
        base = await _job(db, starter_user, county=county, finished_hours_ago=30)
        await _rows(db, starter_user, base, n=60, with_mailing=50)
        bad = await _job(db, starter_user, county=county, records_found=15, window=_WEEK)
        await _rows(db, starter_user, bad, n=15, with_mailing=0)
        with _sync() as s:
            report = dq.check_job(s, bad)
        assert sorted(w["kind"] for w in report["warnings"]) == [
            "coverage_collapse", "volume_collapse"]
        assert len(await _audit_paths(f"data_quality_volume:{county}/")) == 1
        assert len(await _audit_paths(f"data_quality:{county}/")) == 1
