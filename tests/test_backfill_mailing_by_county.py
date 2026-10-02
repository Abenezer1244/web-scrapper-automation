"""The county-scoped mailing backfill: fill-only, settled rows never re-asked, dry run
writes nothing, never touches quota or skip trace. Real DB rows; the county adapter
is substituted by a dict of answers (no HTTP)."""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment.snohomish_assessor_roll import FOUND, SOURCE_UNAVAILABLE, MailingAnswer

_spec = importlib.util.spec_from_file_location(
    "backfill_mailing_by_county", Path(__file__).parent.parent / "scripts" / "backfill_mailing_by_county.py")
bf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bf
_spec.loader.exec_module(bf)


async def _job(db, user: User, county: str = "benton") -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"{county} backfill", county=county, state="WA",
        record_type="probate", fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status="done",
               trigger="manual", record_count=0, billed_count=0))
    await db.commit()
    return job_id


async def _row(db, user: User, job_id: str, parcel: str, *, mailing: str | None = None,
               enrichment: dict | None = None) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, job_id=job_id, user_id=user.id, party_name="P", parcel_id=parcel,
                  property_address="65003 N SR 225", mailing_address=mailing,
                  enrichment_data=enrichment if enrichment is not None else {}))
    await db.commit()
    return rid


def _state(rid: str) -> tuple:
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        return s.execute(text(
            "SELECT mailing_address, absentee_owner, enrichment_data::jsonb ->> 'mailing_recovery_outcome', "
            "enrichment_data::jsonb ->> 'mailing_source', enrichment_data::jsonb ->> 'mailing_lookup_deferred' "
            "FROM results WHERE id = :id"), {"id": rid}).one()


def _answers(monkeypatch, answers: dict, calls: list):
    def _resolver_for(county):
        def _resolve(ids):
            calls.append(list(ids))
            return {pid: answers.get(pid, MailingAnswer(SOURCE_UNAVAILABLE)) for pid in ids}
        return _resolve, "pacs_benton"
    monkeypatch.setattr(bf, "resolver_for", _resolver_for)


async def test_dry_run_reads_and_writes_nothing(db, starter_user, monkeypatch):
    job = await _job(db, starter_user)
    rid = await _row(db, starter_user, job, "131073011125003")
    calls: list = []
    _answers(monkeypatch, {"131073011125003": MailingAnswer(FOUND, "PO BOX 800, PHOENIX, AZ 85001")}, calls)
    stats = bf.run("benton", apply=False, batch=10, max_parcels=10)
    assert stats["found"] == 1 and stats["written"] == 0
    assert _state(rid)[0] is None


async def test_apply_fills_only_null_rows_and_recomputes_flags(db, starter_user, monkeypatch):
    job = await _job(db, starter_user)
    empty = await _row(db, starter_user, job, "131073011125003")
    kept = await _row(db, starter_user, job, "131073011125004", mailing="1 EXISTING ST, TWISP, WA 98856")
    calls: list = []
    _answers(monkeypatch, {
        "131073011125003": MailingAnswer(FOUND, "PO BOX 800, PHOENIX, AZ 85001"),
        "131073011125004": MailingAnswer(FOUND, "9 WRONG RD, LANGLEY, WA 98260"),
    }, calls)
    stats = bf.run("benton", apply=True, batch=10, max_parcels=10)
    assert calls == [["131073011125003"]]  # the row with a mailing address is never a candidate
    assert stats["written"] == 1
    mailing, absentee, outcome, source, deferred = _state(empty)
    assert mailing == "PO BOX 800, PHOENIX, AZ 85001"
    assert absentee is True and outcome == "found" and source == "pacs_benton" and deferred == "false"
    assert _state(kept)[0] == "1 EXISTING ST, TWISP, WA 98856"


async def test_settled_none_is_recorded_and_never_asked_again(db, starter_user, monkeypatch):
    job = await _job(db, starter_user)
    rid = await _row(db, starter_user, job, "131073011125003")
    calls: list = []
    _answers(monkeypatch, {"131073011125003": MailingAnswer("none")}, calls)
    bf.run("benton", apply=True, batch=10, max_parcels=10)
    mailing, _, outcome, _, deferred = _state(rid)
    assert mailing is None and outcome == "none" and deferred == "false"
    bf.run("benton", apply=True, batch=10, max_parcels=10)
    assert calls == [["131073011125003"]]


async def test_unreached_parcels_spend_nothing_and_stay_eligible(db, starter_user, monkeypatch):
    job = await _job(db, starter_user)
    rid = await _row(db, starter_user, job, "131073011125003")
    calls: list = []
    _answers(monkeypatch, {}, calls)  # the adapter answers source_unavailable
    stats = bf.run("benton", apply=True, batch=10, max_parcels=10)
    assert stats["deferred"] == 1 and stats["written"] == 0
    assert _state(rid)[2] is None  # no outcome recorded, no attempt spent
    bf.run("benton", apply=True, batch=10, max_parcels=10)
    assert len(calls) == 2


async def test_a_malformed_attempts_value_does_not_fail_the_run(db, starter_user, monkeypatch):
    """Historical JSON can hold junk; the row is treated as 0 attempts, not as a crash (Codex P2)."""
    job = await _job(db, starter_user)
    rid = await _row(db, starter_user, job, "131073011125003",
                     enrichment={"mailing_recovery_attempts": "lots"})
    calls: list = []
    _answers(monkeypatch, {"131073011125003": MailingAnswer(FOUND, "PO BOX 1, X, WA 98001")}, calls)
    stats = bf.run("benton", apply=True, batch=10, max_parcels=10)
    assert stats["written"] == 1 and _state(rid)[0] == "PO BOX 1, X, WA 98001"


async def test_rows_past_the_attempt_cap_are_left_alone(db, starter_user, monkeypatch):
    job = await _job(db, starter_user)
    await _row(db, starter_user, job, "131073011125003",
               enrichment={"mailing_recovery_attempts": bf._MAX_ATTEMPTS, "mailing_recovery_outcome": "error"})
    calls: list = []
    _answers(monkeypatch, {"131073011125003": MailingAnswer(FOUND, "PO BOX 1, X, WA 98001")}, calls)
    stats = bf.run("benton", apply=True, batch=10, max_parcels=10)
    assert stats["parcels"] == 0 and calls == []


async def test_other_counties_and_undelivered_jobs_are_out_of_scope(db, starter_user, monkeypatch):
    clark = await _job(db, starter_user, county="clark")
    await _row(db, starter_user, clark, "196948000")
    calls: list = []
    _answers(monkeypatch, {"196948000": MailingAnswer(FOUND, "1 A ST, VANCOUVER, WA 98660")}, calls)
    stats = bf.run("benton", apply=True, batch=10, max_parcels=10)
    assert stats["parcels"] == 0 and calls == []


def test_resolver_dispatch_covers_pacs_thurston_and_refuses_unknown():
    assert bf.resolver_for("benton")[1] == "pacs_benton"
    assert bf.resolver_for("WHATCOM")[1] == "pacs_whatcom"
    assert bf.resolver_for("thurston")[1] == "thurston_assessor"
    assert bf.resolver_for("pierce") is None
