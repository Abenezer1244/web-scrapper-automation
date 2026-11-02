"""Inline enrichment never holds a transaction open across a network call.

The canary fix (#579) found the pattern live: one transaction left open across minutes of
county requests was dropped by the pooler, and its commit then failed with "SSL SYSCALL
error: EOF detected", losing every write in it. A Codex audit of enrich.py found the same
shape in three places that run on the normal path:

- the Pierce legal repair kept every earlier repair uncommitted while it called GIS for the
  next target, and counted repairs a failed commit had rolled back;
- the King account-number resolution downloaded the assessor extract (300 s timeout) inside
  the transaction the job's results SELECT opened;
- a refused skip-trace claim sent its ops alert (email + webhook) while the job lock, the
  jobs-row lock and the config FOR SHARE were held, and its log line committed mid-lock.

Real DB and the real enrichment steps. Only the network calls are replaced, and each one
records whether the session was inside a transaction when it was made.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from src.config import settings
from src.db.models import Job, JobLog, Result, ScraperConfig, User
from src.workers.tasks_helpers import enrich
from tests.test_skip_trace_enqueue_after_delivery import _PARTY, _SITUS, _job, _row


class _Redis:
    """The job log's pub/sub side; the stored log line is what these tests read."""

    def publish(self, *_a, **_k):
        return 0


async def _cfg(db, user: User, county: str, **kw) -> ScraperConfig:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"{county} network transactions",
        county=county, state="WA", record_type=kw.pop("record_type", "probate"),
        fields=[], enrichment=[], schedule={"frequency": "manual"},
        deliver={"formats": ["csv"], "emails": []}, **kw,
    )
    db.add(cfg)
    await db.commit()
    return cfg


async def _result(db, rid: str) -> Result:
    db.expire_all()
    return (await db.execute(select(Result).where(Result.id == rid))).scalar_one()


# ── Pierce legal repair ──────────────────────────────────────────────────────────

_LEGAL = "PARKWOOD LT 11 (+)"


async def test_each_pierce_repair_commits_before_the_next_targets_lookups(
        db, business_user, monkeypatch):
    from src.scrapers.enrichment import pierce_atip, pierce_legal_repair

    cfg = await _cfg(db, business_user, "pierce")
    job_id = await _job(db, business_user, cfg)
    first = await _row(db, job_id, business_user.id, parcel_id="1234000110", legal_description=_LEGAL)
    second = await _row(db, job_id, business_user.id, parcel_id="1234000220", legal_description=_LEGAL)
    open_at_call: list[tuple[str, bool]] = []
    committed_before: list[str | None] = []

    def _probe(sync_db):
        def _hard_negative(parcel):
            open_at_call.append((parcel, sync_db.in_transaction()))
            if parcel == "1234000220":
                # By the second target's lookup the first repair is already durable.
                with sync_db.get_bind().connect() as other:
                    committed_before.append(other.execute(
                        text("SELECT property_address FROM results WHERE id = :i"), {"i": first},
                    ).scalar())
            return True

        def _by_legal(legal):
            open_at_call.append(("legal", sync_db.in_transaction()))
            # Two survivors share the legal; the lot-suffix guard picks each target's own.
            return [{"parcel_id": "9876000110", "property_address": "11 PARKWOOD DR"},
                    {"parcel_id": "9876000220", "property_address": "22 PARKWOOD DR"}]

        monkeypatch.setattr(pierce_legal_repair, "parcel_hard_negative", _hard_negative)
        monkeypatch.setattr(pierce_legal_repair, "find_pierce_parcels_by_legal", _by_legal)
        monkeypatch.setattr(pierce_atip, "lookup_atip_addresses", _atip_none)
        sync_cfg = sync_db.get(ScraperConfig, cfg.id)
        rows = sorted(sync_db.execute(select(Result).where(Result.job_id == job_id)).scalars().all(),
                      key=lambda res: res.parcel_id)
        assert sync_db.in_transaction()        # the job's SELECT left one open, as in the task
        enrich.pierce_address_recovery(sync_db, _Redis(), job_id, sync_cfg, rows)

    await db.run_sync(_probe)

    assert open_at_call and not any(is_open for _, is_open in open_at_call), open_at_call
    assert committed_before == ["11 PARKWOOD DR"]
    assert (await _result(db, first)).parcel_id == "9876000110"
    assert (await _result(db, second)).parcel_id == "9876000220"
    lines = (await db.execute(select(JobLog.message).where(JobLog.job_id == job_id))).scalars().all()
    assert any(m.startswith("Pierce legal repair: recovered 2/2") for m in lines), lines


def _atip_none(pids):
    """The ATIP fallback's answer for parcels it could not find (its real stats shape)."""
    return {}, {"not_found": len(pids), "hard_failure": 0}


async def test_a_repair_lost_at_commit_is_not_counted_and_the_next_lookups_hold_nothing(
        db, business_user, monkeypatch):
    from src.scrapers.enrichment import pierce_atip, pierce_legal_repair

    cfg = await _cfg(db, business_user, "pierce")
    job_id = await _job(db, business_user, cfg)
    lost = await _row(db, job_id, business_user.id, parcel_id="1234000110", legal_description=_LEGAL)
    kept = await _row(db, job_id, business_user.id, parcel_id="1234000220", legal_description=_LEGAL)
    after_failure: list[bool] = []
    failed = []

    def _run(sync_db):
        def _hard_negative(parcel):
            if failed:
                after_failure.append(sync_db.in_transaction())
            return True

        def _by_legal(legal):
            if failed:
                after_failure.append(sync_db.in_transaction())
            return [{"parcel_id": "9876000110", "property_address": "11 PARKWOOD DR"},
                    {"parcel_id": "9876000220", "property_address": "22 PARKWOOD DR"}]

        monkeypatch.setattr(pierce_legal_repair, "parcel_hard_negative", _hard_negative)
        monkeypatch.setattr(pierce_legal_repair, "find_pierce_parcels_by_legal", _by_legal)
        monkeypatch.setattr(pierce_atip, "lookup_atip_addresses", _atip_none)
        sync_cfg = sync_db.get(ScraperConfig, cfg.id)
        rows = sorted(sync_db.execute(select(Result).where(Result.job_id == job_id)).scalars().all(),
                      key=lambda res: res.parcel_id)
        real_commit, calls = sync_db.commit, []

        def _commit():
            calls.append(1)
            if len(calls) == 2:     # 1: the read before the first lookup; 2: the first repair
                # What a dropped connection does at commit: the work is gone, and the
                # session's rows are expired, so reading one would open a new transaction.
                sync_db.rollback()
                failed.append(True)
                raise RuntimeError("SSL SYSCALL error: EOF detected")
            real_commit()

        sync_db.commit = _commit
        try:
            enrich.pierce_address_recovery(sync_db, _Redis(), job_id, sync_cfg, rows)
        finally:
            sync_db.commit = real_commit

    await db.run_sync(_run)

    assert after_failure == [False, False]          # the second target's two lookups
    lost_row, kept_row = await _result(db, lost), await _result(db, kept)
    assert (lost_row.parcel_id, lost_row.property_address) == ("1234000110", None)
    assert (kept_row.parcel_id, kept_row.property_address) == ("9876000220", "22 PARKWOOD DR")
    lines = (await db.execute(select(JobLog.message).where(JobLog.job_id == job_id))).scalars().all()
    assert any(m.startswith("Pierce legal repair: recovered 1/2") for m in lines), lines


# ── King account-number resolution ───────────────────────────────────────────────

async def test_the_king_extract_download_runs_outside_any_transaction(db, business_user, monkeypatch):
    from src.scrapers.enrichment import king_rpacct

    cfg = await _cfg(db, business_user, "king")
    job_id = await _job(db, business_user, cfg)
    landmark = {"source": "king_landmark_json"}
    # Two leads on one account number: both must be resolved, not just the last one read.
    a = await _row(db, job_id, business_user.id, parcel_id="123456789012", enrichment_data=landmark)
    b = await _row(db, job_id, business_user.id, parcel_id="123456789012", enrichment_data=landmark)
    seen: list[bool] = []

    def _run(sync_db):
        def _resolve(accounts):
            seen.append(sync_db.in_transaction())
            return dict.fromkeys(accounts, "1234567890"), "snapshot-2026-11-02"

        monkeypatch.setattr(king_rpacct, "resolve_account_pins", _resolve)
        rows = sync_db.execute(select(Result).where(Result.job_id == job_id)).scalars().all()
        assert sync_db.in_transaction()
        assert enrich._resolve_king_account_parcels(sync_db, rows, job_id) == 2
        sync_db.commit()

    await db.run_sync(_run)

    assert seen == [False]
    for rid in (a, b):
        assert (await _result(db, rid)).enrichment_data["resolved_parcel_id"] == "1234567890"


async def test_an_unavailable_king_extract_leaves_no_transaction_open(db, business_user, monkeypatch):
    from src.scrapers.enrichment import king_rpacct

    cfg = await _cfg(db, business_user, "king")
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id, parcel_id="123456789012",
                     enrichment_data={"source": "king_landmark_json"})
    seen: list[bool] = []

    def _run(sync_db):
        def _unavailable(accounts):
            seen.append(sync_db.in_transaction())
            return None

        monkeypatch.setattr(king_rpacct, "resolve_account_pins", _unavailable)
        rows = sync_db.execute(select(Result).where(Result.job_id == job_id)).scalars().all()
        assert enrich._resolve_king_account_parcels(sync_db, rows, job_id) == 0
        return sync_db.in_transaction()

    assert await db.run_sync(_run) is False
    assert seen == [False]
    assert "resolved_parcel_id" not in (await _result(db, rid)).enrichment_data


# ── skip-trace claim refused: the alert waits for the commit ─────────────────────

@pytest.fixture
def _skip_trace_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


async def test_a_refused_claim_alerts_and_logs_only_after_the_locks_are_released(
        db, business_user, _skip_trace_on, monkeypatch, redis_client):
    from src.workers import ops_alerts
    from src.workers.skip_trace_claim import INDEX_NAME

    cfg = await _cfg(db, business_user, "clark", skip_trace_enabled=True)
    job_id = await _job(db, business_user, cfg)
    await _row(db, job_id, business_user.id, party_name=_PARTY, property_address="1400 MAIN ST",
               mailing_address="1400 MAIN ST, VANCOUVER, WA 98661", **_SITUS)
    alerts: list[tuple[str, bool]] = []
    # Renamed, not dropped: the claim refuses when the index it expects is missing, and
    # renaming it back restores the exact definition for every later test.
    parked = f"{INDEX_NAME}_parked_by_test"
    await db.execute(text(f"ALTER INDEX {INDEX_NAME} RENAME TO {parked}"))
    await db.commit()
    try:
        def _run(sync_db):
            monkeypatch.setattr(ops_alerts, "send_ops_alert",
                                lambda kind, *_a: alerts.append((kind, sync_db.in_transaction())) or True)
            job = sync_db.get(Job, job_id)
            sync_cfg = sync_db.get(ScraperConfig, cfg.id)
            return enrich._enqueue_skip_trace_rows(sync_db, job, redis_client, job_id, sync_cfg)

        outcome = await db.run_sync(_run)
    finally:
        await db.rollback()
        await db.execute(text(f"ALTER INDEX {parked} RENAME TO {INDEX_NAME}"))
        await db.commit()

    assert outcome == "unenforced"
    assert alerts == [("skip_trace_claim_unenforced", False)]
    lines = (await db.execute(select(JobLog.message).where(JobLog.job_id == job_id))).scalars().all()
    assert any(m.startswith("Contact lookups are paused for 1 lead(s)") for m in lines), lines
