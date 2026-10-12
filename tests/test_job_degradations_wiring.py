"""P0-07 6a-2: run_scrape_job marks a run whose phase failed, before DONE.

The whole production task runs against the real test DB and the real test Redis.
Only the county portal (`_run_scraper`) and R2 (`upload_to_r2`) are replaced,
because they are external, plus, per test, the one callee inside the `try` under
test, made to raise. Dedup, export, billing, the DONE CAS, the marker, the job
log and the ops-alert audit row are all production code. A marker that cannot be
written is forced with a real trigger on job_degradations, not by stubbing it.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text

from src.api.auth import hash_password
from src.db.models import CountyConnector, Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.scrapers.base_scraper import ScrapedRecord
from src.utils.data_exporter import DataExporter
from src.workers import nts_matcher_task, tasks
from src.workers.tasks_helpers import dedup
from src.workers.tasks_helpers.degradation import REASONS

_SECRET = "Jane Q Wiring, 1234 Wiring Elm St"
_RECORDS = 3


class _PhaseCrashError(RuntimeError):
    pass


def _crash(*_a, **_k):
    raise _PhaseCrashError(_SECRET)


@pytest.fixture
def make_job(monkeypatch):
    """make_job(record_type) -> job_id: a pending job for a fresh business account."""
    connectors: list[str] = []

    def make(record_type: str = "probate", skip_trace: bool = False) -> str:
        county = f"p007-{uuid.uuid4().hex[:8]}"
        start = datetime.now(UTC) - timedelta(days=10)
        with SyncSessionLocal() as s:
            user = User(
                id=str(uuid.uuid4()),
                email=f"p007_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
                password_hash=hash_password("TestPass123!"), plan="business",
                records_limit=5000, records_used=0, subscription_status="active",
                quota_anchor_at=start, quota_period_start=start,
                quota_period_end=start + timedelta(days=30), records_period_start=start,
            )
            s.add(user)
            connector = CountyConnector(
                id=str(uuid.uuid4()), county=county, state="WA", record_types=[record_type],
                scraper_class="src.scrapers.base_scraper.BridgeScraper",
                scraper_mode="manual", base_url=f"https://{county}.example.gov",
                health_status="healthy", active=True,
            )
            s.add(connector)
            s.flush()
            config = ScraperConfig(
                id=str(uuid.uuid4()), user_id=user.id, name="p007 wiring",
                county=county, state="WA", record_type=record_type,
                fields=["party_name", "property_address"], enrichment=[],
                skip_trace_enabled=skip_trace,
                schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
                active=True,
            )
            s.add(config)
            s.flush()
            job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=config.id,
                      status="pending", trigger="manual")
            s.add(job)
            s.commit()
            connectors.append(connector.id)
        if record_type == "pre_foreclosure":
            monkeypatch.setattr(nts_matcher_task, "NTS_MATCH_COUNTIES",
                                (*nts_matcher_task.NTS_MATCH_COUNTIES, county))
        return job.id

    def again(job_id: str) -> str:
        """A second pending job of the same scraper: it re-finds the same leads."""
        with SyncSessionLocal() as s:
            first = s.get(Job, job_id)
            job = Job(id=str(uuid.uuid4()), user_id=first.user_id,
                      scraper_config_id=first.scraper_config_id,
                      status="pending", trigger="manual")
            s.add(job)
            s.commit()
            return job.id

    make.again = again
    # The same leads on every run of this test, so a second run sees prior claims.
    seed = uuid.uuid4().hex

    async def _portal(*_a, **_k):
        return [
            ScrapedRecord(
                date_recorded="10/01/2026", party_name=f"Estate Of Wiring {i}",
                doc_type="Probate", parcel_id=f"P007{i}{seed[:8]}",
                property_address=f"{100 + i} Wiring Ave, Seattle, WA 98101",
                mailing_address=f"{100 + i} Wiring Ave, Seattle, WA 98101",
                raw_html_hash=f"{seed[:24]}{i:08d}",
            )
            for i in range(_RECORDS)
        ]

    monkeypatch.setattr(tasks, "_run_scraper", _portal)
    monkeypatch.setattr(DataExporter, "upload_to_r2", lambda self, path, key: key)
    yield make
    with SyncSessionLocal() as s:
        s.execute(text("DELETE FROM county_connectors WHERE id = ANY(CAST(:c AS uuid[]))"),
                  {"c": connectors})
        s.commit()


@pytest.fixture
def unwritable_markers():
    """A real trigger that makes every job_degradations INSERT fail."""
    with SyncSessionLocal() as s:
        s.execute(text(
            "CREATE OR REPLACE FUNCTION zz_test_p007_refuse() RETURNS trigger AS $f$ "
            "BEGIN RAISE EXCEPTION 'p007 test: marker refused'; END; $f$ LANGUAGE plpgsql"))
        s.execute(text("DROP TRIGGER IF EXISTS zz_test_p007_refuse ON job_degradations"))
        s.execute(text(
            "CREATE TRIGGER zz_test_p007_refuse BEFORE INSERT ON job_degradations "
            "FOR EACH ROW EXECUTE FUNCTION zz_test_p007_refuse()"))
        s.commit()
    yield
    with SyncSessionLocal() as s:
        s.execute(text("DROP TRIGGER IF EXISTS zz_test_p007_refuse ON job_degradations"))
        s.execute(text("DROP FUNCTION IF EXISTS zz_test_p007_refuse()"))
        s.commit()


def _apply(job_id: str):
    """Run the task the way a worker does, so on_failure fires on an escape."""
    return tasks.run_scrape_job.apply(args=[job_id])


def _outcome(job_id: str):
    with SyncSessionLocal() as s:
        job = s.execute(text(
            "SELECT status, billing_applied_at, billed_count FROM jobs WHERE id = :j"),
            {"j": job_id}).one()
        markers = s.execute(text(
            "SELECT reason, exc_type, repair_status FROM job_degradations WHERE job_id = :j"),
            {"j": job_id}).all()
        logs = [m for (m,) in s.execute(text(
            "SELECT message FROM job_logs WHERE job_id = :j ORDER BY created_at"),
            {"j": job_id}).all()]
        alerts = s.execute(text(
            "SELECT path, detail FROM audit_events WHERE event = 'ops_alert' AND path LIKE :p"),
            {"p": f"job_degraded:{job_id}:%"}).all()
        claims = s.execute(text(
            "SELECT count(*) FROM delivered_records WHERE first_job_id = :j"),
            {"j": job_id}).scalar_one()
    return job, markers, logs, alerts, claims


def test_a_clean_run_is_done_and_unmarked(make_job, redis_client):
    job_id = make_job()
    _apply(job_id)
    job, markers, _logs, alerts, _claims = _outcome(job_id)
    assert job.status == "done"
    assert markers == [] and alerts == []


def _owner_flags_after_insert():
    """compute_owner_flags also runs once per record at insert; crash only after."""
    real = tasks.compute_owner_flags
    calls = {"n": 0}

    def flags(*a, **k):
        calls["n"] += 1
        if calls["n"] > _RECORDS:
            _crash()
        return real(*a, **k)

    return flags


# (reason, record_type, module, attribute, replacement factory, repair_status)
_PHASES = [
    ("enrichment", "probate", tasks, "_run_inline_enrichment", lambda: _crash, "none"),
    ("nts_match", "pre_foreclosure", nts_matcher_task, "match_job_inline", lambda: _crash, "none"),
    ("survivor_reelection", "probate", dedup, "reconcile_same_run_survivors",
     lambda: _crash, "none"),
    ("claim_transfer", "probate", dedup, "transfer_undelivered_claims", lambda: _crash, "none"),
    ("owner_flags", "probate", tasks, "compute_owner_flags", _owner_flags_after_insert, "none"),
    ("skip_trace_enqueue", "probate", tasks, "_enqueue_skip_trace_rows",
     lambda: _crash, "pending"),
    ("property_key", "probate", tasks, "_write_result_property_keys", lambda: _crash, "none"),
    ("property_membership", "probate", tasks, "_upsert_property_membership",
     lambda: _crash, "none"),
    ("claim_release", "probate", dedup, "release_parcelless_no_address_claims",
     lambda: _crash, "none"),
]


@pytest.mark.parametrize(("reason", "record_type", "module", "attr", "factory", "repair"),
                         _PHASES, ids=[p[0] for p in _PHASES])
def test_a_failed_phase_finishes_done_and_marked(
        make_job, redis_client, monkeypatch, reason, record_type, module, attr, factory, repair):
    job_id = make_job(record_type)
    monkeypatch.setattr(module, attr, factory())
    _apply(job_id)

    job, markers, logs, alerts, _claims = _outcome(job_id)
    assert job.status == "done"
    assert job.billing_applied_at is not None  # delivered and billed as before
    assert [(m.reason, m.exc_type, m.repair_status) for m in markers] == [
        (reason, "_PhaseCrashError", repair)]
    assert logs.count(REASONS[reason]) == 1
    assert [a.path for a in alerts] == [f"job_degraded:{job_id}:{reason}"]
    # Never the exception's text (P0-04).
    assert "wiring elm" not in repr((markers, logs, alerts)).lower()


def test_enrichment_failure_no_longer_writes_the_old_line_or_queues_contacts(
        make_job, redis_client, monkeypatch):
    job_id = make_job()
    monkeypatch.setattr(tasks, "_run_inline_enrichment", _crash)
    queued = []
    monkeypatch.setattr(tasks, "_enqueue_skip_trace_rows", lambda *a, **k: queued.append(1))
    _apply(job_id)
    _job, _markers, logs, _alerts, _claims = _outcome(job_id)
    assert not any("Address enrichment failed" in m for m in logs)
    assert queued == []  # contact lookup is still gated on a completed enrichment


def test_a_failed_completion_line_is_not_an_enrichment_failure(
        make_job, redis_client, monkeypatch):
    """Codex P1: the completion log ran inside the enrichment try. Here the line
    is stored and its live push then fails, as a Redis outage would."""
    job_id = make_job()
    real = tasks._publish_log

    def publish(r, jid, level, message, **k):
        real(r, jid, level, message, **k)
        if message.startswith("Enrichment complete"):
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(tasks, "_publish_log", publish)
    _apply(job_id)
    job, markers, _logs, _alerts, _claims = _outcome(job_id)
    assert [m.reason for m in markers] == []
    # ...and it does not cost the customer the delivery either.
    assert job.status == "done"
    assert job.billing_applied_at is not None


def test_one_failed_claim_transfer_inside_the_loop_marks_the_run(
        make_job, redis_client, monkeypatch):
    """The per-hash catch inside transfer_undelivered_claims used to swallow it
    before the caller's handler could see it (Codex)."""
    first = make_job()
    _apply(first)
    second = make_job.again(first)
    real = dedup._transfer_one_claim
    tried: list[str] = []

    def one_fails(db, job_id, uid, user_id, dedup_hash, *a, **k):
        tried.append(dedup_hash)
        if len(tried) == 1:
            _crash()
        return real(db, job_id, uid, user_id, dedup_hash, *a, **k)

    monkeypatch.setattr(dedup, "_transfer_one_claim", one_fails)
    _apply(second)
    job, markers, _logs, _alerts, _claims = _outcome(second)
    assert len(set(tried)) == _RECORDS  # the failure did not stop the other hashes
    assert job.status == "done"
    assert [(m.reason, m.exc_type) for m in markers] == [
        ("claim_transfer", "_PhaseCrashError")]


def test_a_time_limit_inside_the_claim_transfer_loop_escapes_unmarked(
        make_job, redis_client, monkeypatch):
    first = make_job()
    _apply(first)
    second = make_job.again(first)

    def out_of_time(*_a, **_k):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(dedup, "_transfer_one_claim", out_of_time)
    result = _apply(second)
    assert isinstance(result.result, SoftTimeLimitExceeded)
    job, markers, _logs, _alerts, _claims = _outcome(second)
    assert job.status not in ("done", "failed")
    assert markers == []


def _out_of_time(*_a, **_k):
    raise SoftTimeLimitExceeded()


def _out_of_time_on_upload(nth: int):
    """R2 runs out of time on upload number ``nth`` (1 = first export, 2 = re-export)."""
    def factory():
        calls = {"n": 0}

        def upload(self, path, key):
            calls["n"] += 1
            if calls["n"] == nth:
                _out_of_time()
            return key

        return upload

    return factory


def _out_of_time_on_reexport():
    """The first export calls it too; run out of time only on the re-export."""
    real = tasks._result_rows_to_export_dicts
    calls = {"n": 0}

    def rows(*a, **k):
        calls["n"] += 1
        if calls["n"] > 1:
            _out_of_time()
        return real(*a, **k)

    return rows


@pytest.mark.parametrize(("module", "attr", "factory"), [
    (tasks, "_run_inline_enrichment", lambda: _out_of_time),
    (dedup, "transfer_undelivered_claims", lambda: _out_of_time),
    (tasks, "reserve_job_quota", lambda: _out_of_time),  # the plan cap
    (tasks, "_result_rows_to_export_dicts", _out_of_time_on_reexport),
    (DataExporter, "upload_to_r2", _out_of_time_on_upload(1)),
    (DataExporter, "upload_to_r2", _out_of_time_on_upload(2)),
    (tasks, "_upsert_property_membership", lambda: _out_of_time),
], ids=["enrichment", "claim_transfer", "plan_cap", "reexport", "first_upload",
        "reexport_upload", "membership"])
def test_a_time_limit_in_a_phase_escapes_unmarked(make_job, redis_client, monkeypatch,
                                                   module, attr, factory):
    """Out of time goes to the watchdog's retry path, never to a marker or DONE."""
    job_id = make_job()
    monkeypatch.setattr(module, attr, factory())
    result = _apply(job_id)
    assert isinstance(result.result, SoftTimeLimitExceeded)
    job, markers, _logs, alerts, claims = _outcome(job_id)
    assert job.status not in ("done", "failed")  # on_failure declines a timeout
    assert job.billing_applied_at is None
    assert markers == [] and alerts == []
    assert claims == _RECORDS  # held for the retry, which reuses its own claims


def test_an_unwritable_marker_fails_the_run_instead_of_finishing_it(
        make_job, redis_client, monkeypatch, unwritable_markers):
    job_id = make_job()
    monkeypatch.setattr(dedup, "transfer_undelivered_claims", _crash)
    _apply(job_id)
    job, markers, _logs, _alerts, claims = _outcome(job_id)
    assert job.status == "failed"
    assert job.billing_applied_at is None  # not charged
    assert markers == []
    assert claims == 0  # released: nothing was delivered


def test_an_unwritable_marker_after_done_keeps_the_delivered_run(
        make_job, redis_client, monkeypatch, unwritable_markers):
    job_id = make_job()
    monkeypatch.setattr(dedup, "release_parcelless_no_address_claims", _crash)
    _apply(job_id)
    job, markers, logs, alerts, _claims = _outcome(job_id)
    assert job.status == "done"
    assert job.billing_applied_at is not None
    assert markers == []
    assert any(m.startswith("Job complete") for m in logs)  # delivery carried on
    assert [a.path for a in alerts] == [f"job_degraded:{job_id}:claim_release"]
