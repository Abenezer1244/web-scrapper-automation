"""The generic dialer push carries row ids and re-checks every row at send (Phase 6 P4b-2).

deliver_dialer_push is driven for real against the test database. Two seams only: the
rule snapshot (the database refuses any rule state but 'pending' until P7, so an active
rule is built in memory with build_snapshot) and _post, recorded instead of POSTing to
the customer's endpoint. Synthetic people only.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

import src.privacy.authorize as authorize_mod
import src.workers.webhook_delivery as wd
from src.config import settings
from src.db.models import Job, Result, ScraperConfig, User
from src.privacy import keys
from src.privacy.authorize import RuleSnapshotError, build_snapshot
from src.privacy.subjects import NORMALIZATION_VERSION, subject_key_values
from src.workers import cv_late_delivery as late
from src.workers.scheduler_helpers import dialer
from tests.test_cv_late_delivery import _held, _held_row
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
    for src in (inspect.getsource(dialer._dialer_push_sweep_impl),
                inspect.getsource(cv_late_delivery._send_to_dialer)):
        assert "deliver_dialer_push.delay(" in src
        assert "deliver_job_webhook" not in src and "seal_task_args" not in src


# ── the boundaries before the send task (Codex P4b review) ─────────────────────

async def _late_run(db, user) -> str:
    from tests.test_cv_late_dialer import _GENERIC, _run

    return await _run(db, user, deliver=_GENERIC, pushed_hours_ago=48)


async def _late_dialer(db, rid: str):
    from tests.test_cv_late_dialer import _dialer

    db.expire_all()
    return await _dialer(db, rid)


def _rule_for(party: str, parcel: str, record_type: str, county: str) -> list[dict]:
    """An active rule for exactly this row's person at this row's parcel."""
    [subject] = authorize_mod._subjects({"party_name": party}, record_type, county)
    s_kind, s_value = subject_key_values(subject)[0]
    anchor = keys.property_anchor_value(parcel, county, "WA")
    t0 = datetime(2026, 11, 6, tzinfo=UTC)
    rule_id = str(uuid.uuid4())
    return [{
        "id": str(uuid.uuid4()), "rule_id": rule_id, "anchor_kind": "property",
        "anchor_hmac": keys.privacy_hmac("property", anchor, "v1"),
        "subject_kind": s_kind, "subject_hmac": keys.privacy_hmac(s_kind, s_value, "v1"),
        "scope": "subject", "key_version": "v1", "normalization_version": NORMALIZATION_VERSION,
        "state": state, "created_at": t0 + timedelta(minutes=i),
    } for i, state in enumerate(("pending", "active"))]


@pytest.fixture
def enqueued(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(wd.deliver_dialer_push, "delay",
                        lambda job_id, **kw: calls.append((job_id, kw)))
    return calls


async def test_a_legacy_sealed_dialer_message_is_rerouted_by_id_never_sent(
    db, business_user, posts, enqueued,
):
    """A push sealed by the previous release (people frozen at selection) is never
    posted as is: its ids go through the re-reading, authorizing task."""
    from src.utils.crypto import seal_task_args

    job, _, ids = await _job(db, business_user, ("DOE JANE", "9876543210"))
    legacy = {"event": "leads.dialer_ready", "total_dialer_ready_count": 1,
              "leads": [{"id": ids["DOE JANE"], "party_name": "DOE JANE", "phone": "2065550100"}]}
    out = wd.deliver_job_webhook(job, sealed=seal_task_args({"url": _HOOK, "payload": legacy}))
    assert out == {"status": "rerouted", "rows": 1}
    assert posts == []
    assert enqueued == [(job, {"result_ids": [ids["DOE JANE"]], "total": 1})]


async def _pushed_at(db, job: str):
    db.expire_all()
    return (await db.execute(text("SELECT dialer_pushed_at FROM jobs WHERE id = :j"),
                             {"j": job})).scalar()


async def test_the_sweep_claims_nothing_when_rules_cannot_load(
    db, business_user, enqueued, monkeypatch,
):
    job, _, _ = await _job(db, business_user, ("DOE JANE", "9876543210"))

    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    dialer._dialer_push_sweep_impl()
    assert await _pushed_at(db, job) is None
    assert enqueued == []


async def test_the_sweep_never_queues_a_withheld_row(db, business_user, enqueued, monkeypatch):
    job, _, ids = await _job(db, business_user, ("SMITH JOHN", _PARCEL), ("DOE JANE", "9876543210"))
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot",
                        lambda db: build_snapshot(_history(["pending", "active"])))
    dialer._dialer_push_sweep_impl()
    [(queued_job, kw)] = [c for c in enqueued if c[0] == job]
    assert kw == {"result_ids": [ids["DOE JANE"]], "total": 1}
    assert await _pushed_at(db, job) is not None


async def test_a_late_row_the_rules_withhold_is_never_sent(db, business_user, enqueued, monkeypatch):
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", True, raising=False)
    job = await _late_run(db, business_user)
    rid = await _held_row(db, business_user, job, _held(), phone="2065550100")
    party = (await db.execute(text(
        "SELECT party_name FROM results WHERE id = :i"), {"i": rid})).scalar()
    # The row's person under a rule anchored at another parcel: held for review. Since
    # P5g late delivery authorizes BEFORE delivering, so the row is flagged (a real rule
    # row: the flag's foreign key) and never delivered: it never reaches a dialer at all.
    from tests.test_privacy_nts_readers import _rule_row

    rows = _rule_for(party, "5555555555", "code_violation", "king")
    rows[-1]["id"] = _rule_row()
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", lambda db: build_snapshot(rows))
    stats = late.deliver_completed_held_rows()
    assert (stats["delivered"], stats["privacy_withheld"]) == (0, 1)
    assert await _late_dialer(db, rid) is None       # never delivered, so never pushed
    flag = (await db.execute(text("SELECT privacy_suppression_id::text FROM results "
                                  "WHERE id = :i"), {"i": rid})).scalar()
    assert flag == rows[-1]["id"]
    assert enqueued == []


async def test_late_delivery_claims_no_push_when_rules_cannot_load(
    db, business_user, enqueued, monkeypatch,
):
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", True, raising=False)
    job = await _late_run(db, business_user)
    rid = await _held_row(db, business_user, job, _held(), phone="2065550100")

    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    stats = late.deliver_completed_held_rows()
    # Since P5g nothing is delivered without readable rules; the dialer step still runs
    # and pushes nothing.
    assert stats["skipped"] == "privacy rules unavailable" and stats["delivered"] == 0
    assert stats["dialer_pushed"] == 0
    assert await _late_dialer(db, rid) is None and enqueued == []
