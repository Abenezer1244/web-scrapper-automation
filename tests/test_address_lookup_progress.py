"""Address lookup progress: the post-scrape lookup's stage, and its measured GIS sweep.

UX audit item 3, phase 3.10a-BE. The whole post-scrape address lookup used to run
under ONE stage, `enriching`, with no counts, so the results page could only show an
indeterminate bar for what is often the longest part of a run. Now run_scrape_job
writes the umbrella stage `address_lookup`, and the generic county GIS sweep (the one
pass that runs for every county with parcel rows, King included) reports
`address_lookup_gis` with "parcels checked of N" after every committed batch, then
returns to the umbrella when it ends, however it ends.

Real test database and real Redis throughout. The county GIS adapter
(`batch_enrich_parcels_gis`) and King's own county lookup are the external county
APIs and the only things substituted; `_GIS_COMMIT_BATCH` is lowered so a few rows
make several batches. A pass-through recorder around the REAL `_set_progress` notes
which progress writes landed; every row assertion reads through a FRESH session.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import socket
import textwrap
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis as sync_redis
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select, text

from src.api.schemas import _STAGE_LABELS, JobResponse, _stage_label
from src.config.constants import HEARTBEAT_STALE_MINUTES, JOB_STAGES
from src.db.models import Job, JobLog, Result, ScraperConfig
from src.scrapers.enrichment import county_gis as cg
from src.workers.tasks_helpers import enrich
from src.workers.tasks_helpers.status import AttemptToken

GIS = "address_lookup_gis"
UMBRELLA = "address_lookup"


# ─── Fixtures and helpers ────────────────────────────────────────────────────

async def _job(db, user, *, county: str = "benton", parcels: int = 5,
               record_type: str = "probate") -> tuple[str, AttemptToken, list[str]]:
    """A job in the post-scrape lookup (as run_scrape_job leaves it), with one parcel
    row per parcel. Returns (job id, its attempt token, the parcel ids in order)."""
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"{county} lookup progress",
        county=county, state="WA", record_type=record_type,
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    started = datetime.now(UTC) - timedelta(minutes=3)
    db.add(Job(
        id=job_id, user_id=user.id, scraper_config_id=config.id, status="enriching",
        trigger="manual", record_count=parcels, billed_count=parcels,
        started_at=started, retry_count=0, last_heartbeat_at=datetime.now(UTC),
        stage=UMBRELLA, stage_started_at=datetime.now(UTC),
    ))
    await db.commit()
    pids = [f"{9100000000 + n}" for n in range(1, parcels + 1)]
    for pid in pids:
        db.add(Result(
            id=str(uuid.uuid4()), user_id=user.id, job_id=job_id, party_name="DOE JANE A",
            doc_type="DEATH CERTIFICATE", parcel_id=pid, property_address=None,
            mailing_address=None, enrichment_data={"instrument_number": pid},
            skip_trace_status="not_attempted",
        ))
    await db.commit()
    return job_id, AttemptToken(started, 0), pids


def _answer(pid: str, *, mailing: bool = True) -> dict:
    return {
        "parcel_id": pid,
        "property_address": f"{pid[-3:]} MAIN ST",
        "mailing_address": f"PO BOX {pid[-3:]}, KENNEWICK, WA 99336" if mailing else None,
        "property_city": "KENNEWICK", "property_state": "WA", "property_zip": "99336",
    }


def _county(monkeypatch, per_call) -> list[list[str]]:
    """Substitute the county GIS adapter. ``per_call(call_index, pids, stats)`` returns
    that batch's answers (or raises). Records each call's parcel ids."""
    calls: list[list[str]] = []

    def _fake(pids, county, state, stats=None):
        calls.append(list(pids))
        return per_call(len(calls) - 1, list(pids), stats if stats is not None else {})

    monkeypatch.setattr(cg, "batch_enrich_parcels_gis", _fake)
    return calls


def _all_answered(_i, pids, _stats) -> dict:
    return {p: _answer(p) for p in pids}


def _recorder(monkeypatch) -> list[dict]:
    """Pass-through around the REAL _set_progress: notes each write that LANDED."""
    real = enrich._set_progress
    landed: list[dict] = []

    def _rec(db, job, *, expected_started_at, commit=True, **values):
        ok = real(db, job, expected_started_at=expected_started_at, commit=commit, **values)
        if ok:
            landed.append(dict(values))
        return ok

    monkeypatch.setattr(enrich, "_set_progress", _rec)
    return landed


def _batch(monkeypatch, size: int = 2) -> None:
    monkeypatch.setattr(enrich, "_GIS_COMMIT_BATCH", size)


def _row(job_id: str) -> Job:
    """The job row as a FRESH session sees it."""
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        job = s.get(Job, job_id)
        s.expunge(job)
        return job


def _logs(job_id: str) -> list[str]:
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        return list(s.execute(
            select(JobLog.message).where(JobLog.job_id == job_id).order_by(JobLog.created_at)
        ).scalars())


def _progress_logs(job_id: str) -> list[str]:
    return [m for m in _logs(job_id) if m.startswith("Property lookup progress:")]


def _results(job_id: str) -> dict[str, tuple]:
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        return {
            r.parcel_id: (r.property_address, r.mailing_address, r.enrichment_data)
            for r in s.execute(select(Result).where(Result.job_id == job_id)).scalars()
        }


def _run(job_id: str, r, token) -> None:
    from src.db.session import system_sync_session

    with system_sync_session() as sdb:
        job = sdb.get(Job, job_id)
        config = sdb.get(ScraperConfig, job.scraper_config_id)
        enrich._run_inline_enrichment(sdb, job, r, job_id, config, summary={},
                                      attempt_token=token)


async def _enrich(job_id: str, r, token) -> None:
    await asyncio.to_thread(_run, job_id, r, token)


def _counts(writes: list[dict]) -> list[tuple]:
    return [(w.get("stage"), w["units_done"], w["units_total"], w["progress_unit"])
            for w in writes]


def _response(**over) -> JobResponse:
    """A JobResponse for a live run in the post-scrape lookup, with ``over`` applied."""
    now = datetime.now(UTC)
    base = {
        "id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "scraper_config_id": str(uuid.uuid4()), "status": "enriching", "trigger": "manual",
        "page_current": 0, "page_total": 0, "record_count": 7, "export_key": None,
        "error_message": None, "retry_count": 0, "started_at": now - timedelta(minutes=2),
        "finished_at": None, "created_at": now - timedelta(minutes=3),
        "last_heartbeat_at": now,
    }
    base.update(over)
    return JobResponse.model_validate(base)


# ─── Stages and labels ───────────────────────────────────────────────────────

class TestStages:
    def test_both_stages_are_written_by_the_app_and_have_customer_copy(self):
        for stage in (UMBRELLA, GIS):
            assert stage in JOB_STAGES
            assert stage in _STAGE_LABELS
            assert len(stage) <= 32  # jobs.stage is String(32)
            assert "—" not in _STAGE_LABELS[stage]
        assert _STAGE_LABELS[UMBRELLA] == "Looking up property and mailing addresses"
        assert _STAGE_LABELS[GIS] == "Checking county parcel records"

    def test_measured_label(self):
        assert _stage_label(stage=GIS, status="enriching", units_done=2, units_total=5,
                            progress_unit="parcel", records_found=None) == (
            "Checking county parcel records: Property 2 of 5")

    def test_umbrella_label_is_composed_with_the_record_count(self):
        j = _response(stage=UMBRELLA, records_found=7)
        expected = _stage_label(stage=UMBRELLA, status="enriching", units_done=None,
                                units_total=None, progress_unit=None, records_found=7)
        assert expected == "Looking up property and mailing addresses: 7 records found"
        assert j.stage_label == expected
        assert j.progress_pct is None

    def test_rollout_overlap_old_stage_and_unknown_stage(self):
        """An old worker still writes `enriching` during a rolling deploy."""
        assert _stage_label(stage="enriching", status="enriching", units_done=None,
                            units_total=None, progress_unit=None,
                            records_found=None) == "Adding property and mailing details"
        assert _stage_label(stage="address_lookup_other", status="enriching",
                            units_done=None, units_total=None, progress_unit=None,
                            records_found=None) == "Adding property and mailing details"

    def test_a_stale_heartbeat_is_still_stalled_whatever_the_progress(self):
        """Stall detection reads the heartbeat; a fresh progress write does not hide it."""
        now = datetime.now(UTC)
        j = _response(
            started_at=now - timedelta(minutes=30), created_at=now - timedelta(minutes=31),
            last_heartbeat_at=now - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1),
            last_progress_at=now, stage=GIS, units_done=2, units_total=5,
            progress_unit="parcel",
        )
        assert j.progress_stalled is True


# ─── The measured sweep ──────────────────────────────────────────────────────

class TestGisSweep:
    async def test_multi_batch_sequence_then_back_to_the_umbrella(
            self, db, business_user, redis_client, monkeypatch):
        job_id, token, _pids = await _job(db, business_user, parcels=5)
        _batch(monkeypatch)
        writes = _recorder(monkeypatch)
        seen: list[Job] = []

        def _per_call(i, pids, stats):
            if i > 0:
                seen.append(_row(job_id))  # what the API serves between batches
            return _all_answered(i, pids, stats)

        _county(monkeypatch, _per_call)
        await _enrich(job_id, redis_client, token)

        assert _counts(writes) == [
            (GIS, 2, 5, "parcel"), (None, 4, 5, "parcel"), (None, 5, 5, "parcel"),
            (UMBRELLA, None, None, None),
        ]
        # The stage clock is stamped on the stage change only, not per batch.
        assert "stage_started_at" in writes[0]
        assert all("stage_started_at" not in w for w in writes[1:3])
        assert seen[0].stage_started_at == seen[1].stage_started_at
        # Mid-sweep, the API answers with the pass and its counts.
        mid = JobResponse.model_validate(seen[0])
        assert (mid.stage, mid.units_done, mid.units_total) == (GIS, 2, 5)
        assert mid.progress_pct == 40
        assert mid.stage_label == "Checking county parcel records: Property 2 of 5"
        assert JobResponse.model_validate(seen[1]).progress_pct == 80
        # The same count drives the log line.
        assert _progress_logs(job_id) == [
            "Property lookup progress: 2/5 parcels (2 rows updated)",
            "Property lookup progress: 4/5 parcels (4 rows updated)",
            "Property lookup progress: 5/5 parcels (5 rows updated)",
        ]
        end = _row(job_id)
        assert (end.stage, end.units_done, end.units_total, end.progress_unit) == (
            UMBRELLA, None, None, None)
        assert end.status == "enriching"

    async def test_a_failed_batch_adds_nothing_and_log_and_progress_agree(
            self, db, business_user, redis_client, monkeypatch):
        """The middle batch is rejected AT ITS COMMIT by the real driver (a NUL byte in
        an address; Session.commit is not touched). Its fills roll back, its deferral
        markers persist, and the count it would have added never appears."""
        job_id, token, pids = await _job(db, business_user, parcels=6)
        _batch(monkeypatch)
        writes = _recorder(monkeypatch)

        def _per_call(i, batch, stats):
            out = _all_answered(i, batch, stats)
            if i == 0:
                # One parcel the county could not be reached for: answered with a
                # property address, mailing deferred to recovery, so not CHECKED.
                out[batch[0]] = _answer(batch[0], mailing=False)
                stats["county_unreached"] = [batch[0]]
            if i == 1:
                out[batch[1]]["property_address"] = "12 MAIN\x00 ST"
            return out

        _county(monkeypatch, _per_call)
        await _enrich(job_id, redis_client, token)

        assert _counts(writes) == [
            (GIS, 1, 6, "parcel"), (None, 3, 6, "parcel"), (UMBRELLA, None, None, None),
        ]
        assert [int(m.split(":")[1].split("/")[0]) for m in _progress_logs(job_id)] == [
            w["units_done"] for w in writes[:2]]
        rows = _results(job_id)
        for pid in pids[2:4]:
            prop, mail, ed = rows[pid]
            assert (prop, mail) == (None, None)            # the fills rolled back
            assert ed.get("mailing_lookup_deferred") is True  # the markers persisted
        assert rows[pids[0]][2].get("mailing_lookup_deferred") is True
        assert rows[pids[4]][0] is not None and rows[pids[5]][0] is not None

    async def test_a_failed_first_batch_does_not_enter_the_stage(
            self, db, business_user, redis_client, monkeypatch):
        job_id, token, _pids = await _job(db, business_user, parcels=4)
        _batch(monkeypatch)
        writes = _recorder(monkeypatch)
        before_second: list[Job] = []

        def _per_call(i, batch, stats):
            out = _all_answered(i, batch, stats)
            if i == 0:
                out[batch[0]]["property_address"] = "12 MAIN\x00 ST"
            else:
                before_second.append(_row(job_id))
            return out

        _county(monkeypatch, _per_call)
        await _enrich(job_id, redis_client, token)

        assert before_second[0].stage == UMBRELLA
        assert before_second[0].units_total is None
        assert _counts(writes) == [(GIS, 2, 4, "parcel"), (UMBRELLA, None, None, None)]

    async def test_parcels_with_no_answer_are_checked_deferred_ones_are_not(
            self, db, business_user, redis_client, monkeypatch):
        job_id, token, _pids = await _job(db, business_user, parcels=4)
        _batch(monkeypatch)
        writes = _recorder(monkeypatch)
        # Batch 1: one answered, one absent from the answer (and not deferred).
        _county(monkeypatch, lambda i, batch, stats: (
            {batch[0]: _answer(batch[0])} if i == 0 else _all_answered(i, batch, stats)))
        await _enrich(job_id, redis_client, token)
        assert _counts(writes)[0] == (GIS, 2, 4, "parcel")
        assert _progress_logs(job_id)[0] == "Property lookup progress: 2/4 parcels (1 rows updated)"

    async def test_a_one_batch_sweep_is_not_measured(
            self, db, business_user, redis_client, monkeypatch):
        job_id, token, _pids = await _job(db, business_user, parcels=2)
        _batch(monkeypatch)
        writes = _recorder(monkeypatch)
        _county(monkeypatch, _all_answered)
        await _enrich(job_id, redis_client, token)
        assert writes == []
        end = _row(job_id)
        assert (end.stage, end.units_total) == (UMBRELLA, None)
        assert _progress_logs(job_id) == ["Property lookup progress: 2/2 parcels (2 rows updated)"]

    async def test_king_runs_the_same_measured_sweep_and_its_own_passes_stay_unmeasured(
            self, db, business_user, redis_client, monkeypatch):
        from src.scrapers.enrichment import king_county_assessor as kca

        king_calls: list[list[str]] = []

        async def _king_answers_nothing(parcel_ids, **kw):
            king_calls.append(list(parcel_ids))
            if kw.get("stats") is not None:
                kw["stats"]["deferred"] = []
            return {}

        monkeypatch.setattr(kca, "batch_enrich_king_county", _king_answers_nothing)
        job_id, token, _pids = await _job(db, business_user, county="king", parcels=5)
        _batch(monkeypatch)
        writes = _recorder(monkeypatch)
        # Property only: the King mailing pass then has work to do (and answers nothing).
        _county(monkeypatch, lambda i, batch, stats: {
            p: _answer(p, mailing=False) for p in batch})
        await _enrich(job_id, redis_client, token)

        assert king_calls, "King's own lookup pass did not run"
        assert _counts(writes) == [
            (GIS, 2, 5, "parcel"), (None, 4, 5, "parcel"), (None, 5, 5, "parcel"),
            (UMBRELLA, None, None, None),
        ]
        assert {w.get("stage") for w in writes} <= {GIS, UMBRELLA, None}
        assert _row(job_id).stage == UMBRELLA


# ─── The sweep ends abnormally ───────────────────────────────────────────────

def _closed_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestAbnormalEnd:
    async def test_a_redis_publish_failure_propagates_after_the_reset(
            self, db, business_user, monkeypatch):
        """The per-batch log line commits, then its Redis publish raises: the error
        escapes as it always has, and the stage is back on the umbrella first."""
        from src.config import settings

        r = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)
        job_id, token, _pids = await _job(db, business_user, parcels=5)
        _batch(monkeypatch)
        real = enrich._set_progress
        events: list[tuple] = []

        def _rec(sdb, job, *, expected_started_at, commit=True, **values):
            ok = real(sdb, job, expected_started_at=expected_started_at, commit=commit,
                      **values)
            events.append((values.get("stage"), values.get("units_done"), ok,
                           list(_logs(job_id))))
            return ok

        monkeypatch.setattr(enrich, "_set_progress", _rec)

        def _per_call(i, batch, stats):
            if i == 0:
                # The real client, repointed at a port nothing listens on.
                r.connection_pool.connection_kwargs["port"] = _closed_port()
                r.connection_pool.disconnect()
                # Drop the pooled connection objects too: each keeps its own port.
                r.connection_pool.reset()
            return _all_answered(i, batch, stats)

        _county(monkeypatch, _per_call)
        with pytest.raises(sync_redis.exceptions.ConnectionError):
            await _enrich(job_id, r, token)

        # Order: the opening log line, then the progress write, then the batch's log
        # row (committed), then the publish error, then the reset.
        first, reset = events
        assert first[:3] == (GIS, 2, True)
        assert first[3] == ["Looking up 5 property addresses..."]
        assert reset[:3] == (UMBRELLA, None, True)
        assert reset[3] == ["Looking up 5 property addresses...",
                            "Property lookup progress: 2/5 parcels (2 rows updated)"]
        end = _row(job_id)
        assert (end.stage, end.units_done, end.units_total, end.progress_unit) == (
            UMBRELLA, None, None, None)

    async def test_a_soft_time_limit_resets_and_escapes_unchanged(
            self, db, business_user, redis_client, monkeypatch):
        job_id, token, _pids = await _job(db, business_user, parcels=5)
        _batch(monkeypatch)
        writes = _recorder(monkeypatch)
        limit = SoftTimeLimitExceeded()

        def _per_call(i, batch, stats):
            if i == 1:
                raise limit
            return _all_answered(i, batch, stats)

        _county(monkeypatch, _per_call)
        with pytest.raises(SoftTimeLimitExceeded) as caught:
            await _enrich(job_id, redis_client, token)
        assert caught.value is limit
        assert _counts(writes) == [(GIS, 2, 5, "parcel"), (UMBRELLA, None, None, None)]
        end = _row(job_id)
        assert (end.stage, end.units_done, end.units_total) == (UMBRELLA, None, None)


# ─── Whose run it is ─────────────────────────────────────────────────────────

class TestAttempt:
    @pytest.mark.parametrize("which", ["started_at", "retry_count"])
    async def test_a_superseded_attempt_writes_nothing_and_enrichment_completes(
            self, db, business_user, redis_client, monkeypatch, which):
        job_id, token, pids = await _job(db, business_user, parcels=5)
        stale = (AttemptToken(token.started_at - timedelta(seconds=30), token.retry_count)
                 if which == "started_at" else AttemptToken(token.started_at, 1))
        _batch(monkeypatch)
        writes = _recorder(monkeypatch)
        _county(monkeypatch, _all_answered)
        await _enrich(job_id, redis_client, stale)
        assert writes == []
        end = _row(job_id)
        assert (end.stage, end.units_total) == (UMBRELLA, None)
        assert all(prop is not None for prop, _m, _e in _results(job_id).values())
        assert len(_results(job_id)) == len(pids)

    async def test_no_token_writes_nothing_and_fills_exactly_the_same(
            self, db, business_user, redis_client, monkeypatch):
        with_token, token, _p = await _job(db, business_user, parcels=5)
        without, _t, _q = await _job(db, business_user, parcels=5)
        _batch(monkeypatch)
        writes = _recorder(monkeypatch)
        _county(monkeypatch, _all_answered)
        await _enrich(without, redis_client, None)
        assert writes == []
        await _enrich(with_token, redis_client, token)
        assert writes  # the control run did measure

        # Same parcel ids in both jobs: every row's addresses and enrichment data match.
        assert _results(without) == _results(with_token)


# ─── Telemetry never commits enrichment's work ───────────────────────────────

class TestCleanSession:
    @pytest.mark.parametrize("pending", ["open_transaction", "new_object", "dirty_object"])
    async def test_report_waits_for_a_clean_session(
            self, db, business_user, caplog, pending):
        from src.db.session import system_sync_session

        job_id, token, pids = await _job(db, business_user, parcels=2)
        marker = f"pending-{uuid.uuid4()}"

        def _go():
            with system_sync_session() as sdb:
                job = sdb.get(Job, job_id)
                res = sdb.execute(select(Result).where(Result.job_id == job_id)).scalars().first()
                sdb.commit()
                if pending == "open_transaction":
                    sdb.add(JobLog(id=str(uuid.uuid4()), job_id=job_id, level="info",
                                   message=marker))
                    sdb.flush()
                    assert sdb.in_transaction() and not sdb.new
                elif pending == "new_object":
                    sdb.add(JobLog(id=str(uuid.uuid4()), job_id=job_id, level="info",
                                   message=marker))
                    assert sdb.new
                else:
                    res.property_address = marker
                    assert sdb.dirty
                progress = enrich._LookupProgress(sdb, job, token)
                with caplog.at_level(logging.WARNING, logger="worker.task"):
                    refused = progress.report(GIS, done=1, total=2)
                # Nothing of it is visible to anyone else.
                outside = _row(job_id)
                leaked = marker in _logs(job_id) or marker in {
                    a for a, _m, _e in _results(job_id).values()}
                sdb.rollback()
                landed = progress.report(GIS, done=1, total=2)
                return refused, outside, leaked, landed, progress.last_stage

        refused, outside, leaked, landed, last = await asyncio.to_thread(_go)
        assert refused is False
        assert "progress not recorded" in caplog.text
        assert (outside.stage, outside.units_done) == (UMBRELLA, None)
        assert leaked is False
        assert landed is True and last == GIS
        after = _row(job_id)
        assert (after.stage, after.units_done, after.units_total) == (GIS, 1, 2)
        assert pids[0] not in caplog.text  # the warning names the stage, not lead data

    async def test_a_landed_report_holds_no_lock_on_the_job_row(self, db, business_user):
        """The cancel endpoint writes this row; a report must never leave it locked."""
        from src.db.session import system_sync_session

        job_id, token, _pids = await _job(db, business_user, parcels=2)

        def _go():
            with system_sync_session() as sdb:
                job = sdb.get(Job, job_id)
                sdb.commit()
                assert enrich._LookupProgress(sdb, job, token).report(GIS, done=1, total=2)
                with system_sync_session() as other:
                    other.execute(text("SET LOCAL lock_timeout = '2s'"))
                    n = other.execute(text(
                        "UPDATE jobs SET last_progress_at = now() "
                        "WHERE id = :j AND user_id = :u"),
                        {"j": job_id, "u": str(job.user_id)}).rowcount
                    other.rollback()
                return n

        assert await asyncio.to_thread(_go) == 1


# ─── Production wiring (source inspection, as the repo's other wiring tests) ─

class TestWiring:
    def test_run_scrape_job_writes_the_umbrella_and_passes_the_attempt(self):
        from src.workers.tasks import run_scrape_job

        tree = ast.parse(textwrap.dedent(inspect.getsource(run_scrape_job.__wrapped__)))

        def _name(call: ast.Call) -> str | None:
            f = call.func
            return f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)

        calls = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call)),
                       key=lambda n: (n.lineno, n.col_offset))

        def _kw(call: ast.Call, name: str):
            return next((k.value for k in call.keywords if k.arg == name), None)

        def _stage_literal(call: ast.Call):
            arg = call.args[2] if len(call.args) > 2 else None
            return arg.value if isinstance(arg, ast.Constant) else None

        set_stages = [c for c in calls if _name(c) == "_set_stage"]
        assert not [c for c in set_stages if _stage_literal(c) == "enriching"]
        umbrella = [c for c in set_stages if _stage_literal(c) == UMBRELLA]
        assert len(umbrella) == 1
        stage_call = umbrella[0]
        assert isinstance(_kw(stage_call, "expected_started_at"), ast.Name)
        assert _kw(stage_call, "expected_started_at").id == "attempt_token"
        commit = _kw(stage_call, "commit")
        assert isinstance(commit, ast.Constant) and commit.value is False

        after = [c for c in calls if c.lineno > stage_call.lineno]
        first_log = next(c for c in after if _name(c) == "_publish_log")
        enrich_calls = [c for c in calls if _name(c) == "_run_inline_enrichment"]
        assert len(enrich_calls) == 1
        enrich_call = enrich_calls[0]
        # The stage's commit=False write is committed by the very next log line,
        # before the lookup starts.
        assert stage_call.lineno < first_log.lineno < enrich_call.lineno
        token = _kw(enrich_call, "attempt_token")
        assert isinstance(token, ast.Name) and token.id == "attempt_token"
