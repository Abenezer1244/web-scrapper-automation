"""Background recovery of King tax owner names that a job could not look up.

Since #298 a King tax lead the job could not name carries
`owner_lookup_deferred = true` and a reason. Nothing read that marker, so the
lead stayed unnamed forever unless a human re-ran the scraper (job b2f2ecd5:
840 delivered leads, 0 owner names). This sweep is the reading half.

Contract pinned here: delivered leads only, one lookup per parcel, largest
balance first; a name is filled only onto a row that is still blank, still
deferred and still delivered; no billing, quota, skip trace or other column is
touched; an attempt is charged only when King was actually asked; a kill switch
and the source-health gate stop it before any request.

Real DB, real sweep. Only King's HTTP response and the shared source lease are
substituted.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment import king_county_assessor as kca
from src.scrapers.enrichment.source_health import KING_EREALPROPERTY
from src.workers import owner_recovery as orc

pytestmark = pytest.mark.asyncio


def _page(pid: str, owner: str | None, echo: str | None = None) -> str:
    shown = echo or pid
    return (
        f'<tr><td style="font-weight:bold;">Parcel Number</td><td>{shown[:6]}-{shown[6:]}</td></tr>'
        f'<tr><td style="font-weight:bold;">Name</td><td>{owner if owner else "&nbsp;"}</td></tr>'
    )


class _Resp:
    def __init__(self, status: int, body: str = ""):
        self.status_code = status
        self.text = body
        self.url = "https://blue.kingcounty.com/x"
        self.headers: dict = {}


def _lease(monkeypatch, *, admitted: bool = True) -> None:
    class _Lease:
        def __init__(self, *a, **k):
            self.admitted = admitted

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def still_held(self):
            return admitted

    monkeypatch.setattr("src.scrapers.enrichment.source_admission.SourceAdmission", _Lease)


def _county(monkeypatch, answers: dict[str, _Resp]) -> list[str]:
    asked: list[str] = []

    def _get(url, **_k):
        pid = url.rsplit("=", 1)[-1]
        asked.append(pid)
        return answers[pid]

    monkeypatch.setattr(kca, "safe_get", _get)
    return asked


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    from src.config import settings
    from src.db.session import SyncSessionLocal

    monkeypatch.setattr(orc, "_PACE_S", 0.0)
    monkeypatch.setattr(settings, "OWNER_RECOVERY_ENABLED", True, raising=False)

    def _wipe():
        with SyncSessionLocal() as sdb:
            sdb.execute(text("DELETE FROM external_source_health WHERE source_key = :k"),
                        {"k": KING_EREALPROPERTY})
            sdb.commit()

    _wipe()
    yield
    _wipe()


async def _job(db, user: User, *, county: str = "king", record_type: str = "tax_delinquent",
               status: str = "done") -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="owner recovery",
        county=county, state="WA", record_type=record_type,
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    return job_id


async def _row(db, user: User, job_id: str, *, parcel: str, amount: str = "100.00",
               deferred: bool = True, attempts: int | None = None, party: str | None = None,
               over_quota: bool = False, duplicate: bool = False) -> str:
    ed: dict = {"source": "king_county_delinquent_taxes"}
    if deferred:
        ed["owner_lookup_deferred"] = True
        ed["owner_lookup_deferred_reason"] = "not_admitted"
    if attempts is not None:
        ed["owner_recovery_attempts"] = attempts
    if over_quota:
        ed["delivery_excluded_reason"] = "over_quota"
    rid = str(uuid.uuid4())
    db.add(Result(
        id=rid, user_id=user.id, job_id=job_id, parcel_id=parcel, party_name=party,
        delinquent_amount=amount, mailing_address="PO BOX 1, KENT, WA 98032",
        is_duplicate=duplicate, duplicate_reason="prior_run" if duplicate else None,
        skip_trace_status="not_attempted", enrichment_data=ed,
    ))
    await db.commit()
    return rid


async def _get(db, rid: str):
    return (await db.execute(text(
        "SELECT party_name, enrichment_data, skip_trace_status, phone, parcel_id, "
        "mailing_address FROM results WHERE id = :i"), {"i": rid})).first()


def _tick() -> dict:
    return orc.recover_deferred_king_owners()


async def test_a_delivered_lead_gets_its_owner_and_nothing_else_moves(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, parcel="0007200015")
    _county(monkeypatch, {"0007200015": _Resp(200, _page("0007200015", "SD CONST & CONSULTING INC"))})
    job_before = (await db.execute(text(
        "SELECT billed_count, billing_applied_at, reserved_count FROM jobs WHERE id = :j"),
        {"j": job_id})).first()
    used_before = (await db.execute(text("SELECT records_used FROM users WHERE id = :u"),
                                    {"u": business_user.id})).scalar()

    stats = await asyncio.to_thread(_tick)

    row = await _get(db, rid)
    assert row.party_name == "SD CONST & CONSULTING INC"
    ed = row.enrichment_data
    assert ed["owner_lookup_deferred"] is False
    assert "owner_lookup_deferred_reason" not in ed
    assert ed["owner_recovery_outcome"] == "found" and ed["owner_recovery_attempts"] == 1
    assert (row.skip_trace_status, row.phone, row.parcel_id) == ("not_attempted", None, "0007200015")
    assert tuple((await db.execute(text(
        "SELECT billed_count, billing_applied_at, reserved_count FROM jobs WHERE id = :j"),
        {"j": job_id})).first()) == tuple(job_before)
    assert (await db.execute(text("SELECT records_used FROM users WHERE id = :u"),
                             {"u": business_user.id})).scalar() == used_before
    assert stats["found"] == 1


async def test_leads_that_are_not_delivered_or_not_king_tax_are_never_looked_up(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    king = await _job(db, business_user)
    await _row(db, business_user, king, parcel="1000000001", over_quota=True)
    await _row(db, business_user, king, parcel="1000000002", duplicate=True)
    await _row(db, business_user, king, parcel="1000000003", deferred=False)
    await _row(db, business_user, king, parcel="1000000004", party="ALREADY NAMED")
    await _row(db, business_user, king, parcel="1000000005", attempts=orc._MAX_ATTEMPTS)
    live = await _job(db, business_user, status="enriching")
    await _row(db, business_user, live, parcel="1000000006")
    prefc = await _job(db, business_user, record_type="pre_foreclosure")
    await _row(db, business_user, prefc, parcel="1000000007")
    snoho = await _job(db, business_user, county="snohomish")
    await _row(db, business_user, snoho, parcel="1000000008")

    stats = await asyncio.to_thread(_tick)

    assert asked == [] and stats["parcels"] == 0


async def test_each_county_answer_settles_or_retries_the_lead(db, business_user, monkeypatch):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    none = await _row(db, business_user, job_id, parcel="2624069024")
    other = await _row(db, business_user, job_id, parcel="6411600027")
    down = await _row(db, business_user, job_id, parcel="5379801941")
    last_try = await _row(db, business_user, job_id, parcel="0040000055",
                          attempts=orc._MAX_ATTEMPTS - 1)
    _county(monkeypatch, {
        "2624069024": _Resp(200, _page("2624069024", None)),
        "6411600027": _Resp(200, _page("6411600027", "SNYDER JACOB", echo="6411600002")),
        "5379801941": _Resp(503),
        "0040000055": _Resp(503),
    })

    await asyncio.to_thread(_tick)

    ed = (await _get(db, none)).enrichment_data
    assert ed["owner_lookup_outcome"] == "not_on_record" and ed["owner_lookup_deferred"] is False
    assert ed["owner_recovery_attempts"] == 1
    ed = (await _get(db, other)).enrichment_data
    assert (await _get(db, other)).party_name is None
    assert ed["owner_lookup_deferred"] is False
    assert ed["owner_lookup_deferred_reason"] == "parcel_mismatch"
    ed = (await _get(db, down)).enrichment_data
    assert ed["owner_lookup_deferred"] is True and ed["owner_recovery_attempts"] == 1
    assert ed["owner_recovery_outcome"] == "transient_failure"
    ed = (await _get(db, last_try)).enrichment_data
    assert ed["owner_lookup_deferred"] is False and ed["owner_recovery_outcome"] == "gave_up"
    assert ed["owner_recovery_attempts"] == orc._MAX_ATTEMPTS


async def test_a_busy_source_charges_nothing(db, business_user, monkeypatch):
    _lease(monkeypatch, admitted=False)
    asked = _county(monkeypatch, {})
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, parcel="0007200015", attempts=2)

    stats = await asyncio.to_thread(_tick)

    ed = (await _get(db, rid)).enrichment_data
    assert asked == []
    assert ed["owner_recovery_attempts"] == 2 and ed["owner_lookup_deferred"] is True
    assert "owner_recovery_last_at" in ed          # rotated to the back of the queue
    assert stats["unreached"] == 1 and stats["found"] == 0


async def test_a_lead_that_changed_during_the_lookup_is_left_alone(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    renamed = await _row(db, business_user, job_id, parcel="1000000011")
    capped = await _row(db, business_user, job_id, parcel="1000000012")

    def _get_and_change(url, **_k):
        from src.db.session import system_sync_session

        pid = url.rsplit("=", 1)[-1]
        with system_sync_session() as sdb:
            if pid == "1000000011":
                sdb.execute(text("UPDATE results SET party_name = 'FOUND BY A RE-RUN' WHERE id = :i"),
                            {"i": renamed})
            else:
                sdb.execute(text(
                    "UPDATE results SET enrichment_data = (enrichment_data::jsonb || "
                    "'{\"delivery_excluded_reason\": \"over_quota\"}'::jsonb)::json WHERE id = :i"),
                    {"i": capped})
            sdb.commit()
        return _Resp(200, _page(pid, "STALE ANSWER"))

    monkeypatch.setattr(kca, "safe_get", _get_and_change)
    await asyncio.to_thread(_tick)

    assert (await _get(db, renamed)).party_name == "FOUND BY A RE-RUN"
    assert (await _get(db, capped)).party_name is None


async def test_the_kill_switch_and_a_cooling_source_stop_it_before_any_request(
    db, business_user, monkeypatch,
):
    from src.config import settings
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.source_health import mark_source_unhealthy

    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, parcel="0007200015")

    monkeypatch.setattr(settings, "OWNER_RECOVERY_ENABLED", False)
    off = await asyncio.to_thread(_tick)
    assert off["skipped"] == "OWNER_RECOVERY_ENABLED is off"

    monkeypatch.setattr(settings, "OWNER_RECOVERY_ENABLED", True)

    def _block():
        with system_sync_session() as sdb:
            mark_source_unhealthy(sdb, KING_EREALPROPERTY, "test cooldown")

    await asyncio.to_thread(_block)
    cooling = await asyncio.to_thread(_tick)
    assert cooling["skipped"] == "king_erealproperty is in cooldown"

    assert asked == []
    assert "owner_recovery_attempts" not in (await _get(db, rid)).enrichment_data


async def test_the_largest_balances_are_named_first(db, business_user, monkeypatch):
    _lease(monkeypatch)
    monkeypatch.setattr(orc, "_BATCH_PARCELS", 1)
    job_id = await _job(db, business_user)
    await _row(db, business_user, job_id, parcel="1000000021", amount="14.71")
    await _row(db, business_user, job_id, parcel="1000000022", amount="31729.74")
    asked = _county(monkeypatch, {
        "1000000021": _Resp(200, _page("1000000021", "SMALL")),
        "1000000022": _Resp(200, _page("1000000022", "LARGE")),
    })

    await asyncio.to_thread(_tick)

    assert asked == ["1000000022"]


def test_the_sweep_is_registered_and_scheduled():
    from src.workers import app
    from src.workers.scheduler import app as _beat_app  # noqa: F401  (loads the schedule)

    assert "src.workers.owner_recovery" in app.conf.include
    entries = {e["task"] for e in app.conf.beat_schedule.values()}
    assert "src.workers.owner_recovery.recover_deferred_owners" in entries


async def test_a_job_that_is_no_longer_done_is_not_written(db, business_user, monkeypatch):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, parcel="1000000031")

    def _get_and_reopen(url, **_k):
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            sdb.execute(text("UPDATE jobs SET status = 'enriching' WHERE id = :j"), {"j": job_id})
            sdb.commit()
        return _Resp(200, _page(url.rsplit("=", 1)[-1], "STALE ANSWER"))

    monkeypatch.setattr(kca, "safe_get", _get_and_reopen)
    stats = await asyncio.to_thread(_tick)

    assert (await _get(db, rid)).party_name is None
    assert stats["stale"] == 1


async def test_only_a_well_formed_king_pin_is_looked_up(db, business_user, monkeypatch):
    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    job_id = await _job(db, business_user)
    await _row(db, business_user, job_id, parcel="012603938700")   # 12-digit recorder id
    await _row(db, business_user, job_id, parcel="PENDING-1")

    stats = await asyncio.to_thread(_tick)

    assert asked == [] and stats["parcels"] == 0
