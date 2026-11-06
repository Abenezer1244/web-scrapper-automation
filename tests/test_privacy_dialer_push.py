"""The generic dialer push carries row ids and re-checks every row at send (Phase 6 P4b-2).

deliver_dialer_push is driven for real against the test database. Two seams only: the
rule snapshot (the database refuses any rule state but 'pending' until P7, so an active
rule is built in memory with build_snapshot) and _post, recorded instead of POSTing to
the customer's endpoint. Synthetic people only.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

import src.workers.webhook_delivery as wd
from src.db.models import Job, Result, ScraperConfig, User
from src.privacy.authorize import RuleSnapshotError, build_snapshot
from tests.test_privacy_authorize import _history, _keyring  # noqa: F401 -- autouse keyring
from tests.test_privacy_read_paths import _withhold

_PARCEL = "1234567890"  # King: the anchor P|WA|king|1234567890 in _history
_HOOK = "https://dialer.example/hook"


async def _job(db, user: User, *people: tuple[str, str]) -> tuple[str, str, dict[str, str]]:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Dialer", county="king", state="WA",
        record_type="probate", fields=[], enrichment=[], schedule={"frequency": "manual"},
        deliver={"emails": [], "dialer_webhook_url": _HOOK, "dialer_webhook_secret": "s"})
    db.add(cfg)
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status="done",
               trigger="manual", record_count=0, billed_count=0, retry_count=0,
               created_at=datetime.now(UTC)))
    await db.commit()
    ids: dict[str, str] = {}
    for name, parcel in people:
        rid = str(uuid.uuid4())
        db.add(Result(id=rid, job_id=job_id, user_id=user.id, party_name=name,
                      parcel_id=parcel, property_address="1 TEST ST", phone="2065550100",
                      dedup_hash=uuid.uuid4().hex, is_duplicate=False, enrichment_data={}))
        ids[name] = rid
    await db.commit()
    return job_id, cfg.id, ids


@pytest.fixture
def posts(monkeypatch) -> list[tuple[str, dict]]:
    sent: list[tuple[str, dict]] = []

    def _record(task, job_id, url, payload):
        sent.append((url, payload))
        return {"status": "delivered"}

    monkeypatch.setattr(wd, "_post", _record)
    return sent


def _rules(monkeypatch, rows) -> None:
    monkeypatch.setattr(wd, "load_rule_snapshot", lambda db: build_snapshot(rows))


def _push(job: str, ids: dict[str, str], total: int | None = None) -> dict:
    return wd.deliver_dialer_push(job, result_ids=list(ids.values()),
                                  total=total if total is not None else len(ids))


async def test_a_withheld_row_is_left_out_and_the_rest_is_sent(db, business_user, posts, monkeypatch):
    job, _, ids = await _job(db, business_user, ("SMITH JOHN", _PARCEL), ("DOE JANE", "9876543210"))
    _rules(monkeypatch, _history(["pending", "active"]))  # SMITH JOHN on that parcel
    _push(job, ids)
    [(url, payload)] = posts
    assert url == _HOOK
    assert [ld["id"] for ld in payload["leads"]] == [ids["DOE JANE"]]
    assert payload["lead_count"] == 1 and payload["total_dialer_ready_count"] == 2


async def test_when_every_row_is_held_back_nothing_is_sent(db, business_user, posts, monkeypatch):
    job, _, ids = await _job(db, business_user, ("SMITH JOHN", "5555555555"))  # review
    _rules(monkeypatch, _history(["pending", "active"]))
    assert _push(job, ids) == {"status": "skipped", "reason": "no_deliverable_rows"}
    assert posts == []


async def test_rules_that_cannot_load_send_nothing(db, business_user, posts, monkeypatch):
    job, _, ids = await _job(db, business_user, ("DOE JANE", "9876543210"))

    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(wd, "load_rule_snapshot", _broken)
    with pytest.raises(RuleSnapshotError):
        _push(job, ids)
    assert posts == []


async def test_a_flagged_row_is_left_out(db, business_user, posts, monkeypatch):
    job, _, ids = await _job(db, business_user, ("DOE JANE", "9876543210"), ("ROE RICK", "1111111111"))
    await _withhold(db, ids["DOE JANE"])
    _rules(monkeypatch, [])
    _push(job, ids)
    assert [ld["id"] for ld in posts[0][1]["leads"]] == [ids["ROE RICK"]]


async def test_the_destination_is_read_at_send_not_from_the_message(db, business_user, posts, monkeypatch):
    job, cfg, ids = await _job(db, business_user, ("DOE JANE", "9876543210"))
    await db.execute(text(
        "UPDATE scraper_configs SET deliver = jsonb_set(deliver::jsonb, '{dialer_webhook_url}', "
        "'\"https://dialer.example/new\"')::json WHERE id = :c"), {"c": cfg})
    await db.commit()
    _rules(monkeypatch, [])
    _push(job, ids)
    assert posts[0][0] == "https://dialer.example/new"


async def test_a_downgraded_owner_gets_no_push(db, business_user, posts, monkeypatch):
    job, _, ids = await _job(db, business_user, ("DOE JANE", "9876543210"))
    await db.execute(text("UPDATE users SET plan = 'starter' WHERE id = :u"), {"u": business_user.id})
    await db.commit()
    _rules(monkeypatch, [])
    assert _push(job, ids) == {"status": "skipped", "reason": "not_eligible"}
    assert posts == []


async def test_the_real_loader_runs_at_send(db, business_user, posts):
    job, _, ids = await _job(db, business_user, ("DOE JANE", "9876543210"))
    _push(job, ids)
    assert [ld["id"] for ld in posts[0][1]["leads"]] == [ids["DOE JANE"]]


def test_the_message_carries_no_person():
    """The sweep and late delivery enqueue ids and a count; nothing else."""
    import inspect

    from src.workers import cv_late_delivery
    from src.workers.scheduler_helpers import dialer

    for src in (inspect.getsource(dialer._dialer_push_sweep_impl),
                inspect.getsource(cv_late_delivery._send_to_dialer)):
        assert "deliver_dialer_push.delay(" in src
        assert "deliver_job_webhook" not in src and "seal_task_args" not in src
