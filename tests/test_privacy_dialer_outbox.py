"""The native dialer outbox authorizes every row right before its POST (Phase 6 P4b-1).

process_dialer_outbox is driven for real against the test database. Two seams only:
the rule snapshot (the database refuses any rule state but 'pending' until P7, so an
active rule is built in memory with build_snapshot, as tests/test_privacy_authorize.py
does), and _deliver_one, recorded instead of POSTing to the vendor. Synthetic people
and a fake vendor token only.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import src.workers.dialer_outbox as outbox
from src.db.models import DialerDelivery, Job, Result, ScraperConfig, User
from src.privacy.authorize import RuleSnapshotError, build_snapshot
from tests.test_privacy_authorize import _history, _keyring  # noqa: F401 -- autouse keyring
from tests.test_privacy_read_paths import _withhold

_PARCEL = "1234567890"  # King: scoped anchor P|WA|king|1234567890 in _history


async def _outbox(db, user: User, *people: tuple[str, str]) -> tuple[str, dict[str, str]]:
    """A done PhoneBurner job with one pending outbox row per (party_name, parcel)."""
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Dialer", county="king", state="WA",
        record_type="probate", fields=[], enrichment=[], schedule={"frequency": "manual"},
        deliver={"emails": [], "dialer_type": "phoneburner",
                 "phoneburner_access_token": "test-token", "phoneburner_owner_id": "1"})
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
    for rid in ids.values():
        db.add(DialerDelivery(id=str(uuid.uuid4()), job_id=job_id, result_id=rid,
                              user_id=user.id, scraper_config_id=cfg.id,
                              vendor_id="phoneburner", status="pending"))
    await db.commit()
    return job_id, ids


async def _states(db, job_id: str) -> dict[str, tuple[str, str | None, int]]:
    db.expire_all()
    rows = (await db.execute(select(DialerDelivery).where(DialerDelivery.job_id == job_id)))
    return {r.result_id: (r.status, r.last_error, r.attempts) for r in rows.scalars()}


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    """Record each POST instead of sending it; mark the row delivered as a 2xx would."""
    posted: list[str] = []

    def _record(connector, req, row):
        posted.append(row.result_id)
        row.status = "delivered"

    monkeypatch.setattr(outbox, "_deliver_one", _record)
    return posted


def _rules(monkeypatch, rows) -> None:
    monkeypatch.setattr(outbox, "load_rule_snapshot", lambda db: build_snapshot(rows))


async def test_a_withheld_row_is_skipped_and_never_posted(db, business_user, sent, monkeypatch):
    job, ids = await _outbox(db, business_user, ("SMITH JOHN", _PARCEL), ("DOE JANE", "9876543210"))
    _rules(monkeypatch, _history(["pending", "active"]))  # SMITH JOHN on that parcel
    outbox.process_dialer_outbox(job)
    states = await _states(db, job)
    assert states[ids["SMITH JOHN"]][:2] == ("skipped", "privacy withheld")
    assert sent == [ids["DOE JANE"]]          # the other person is still delivered
    assert states[ids["DOE JANE"]][0] == "delivered"


async def test_a_row_under_review_is_skipped_too(db, business_user, sent, monkeypatch):
    # The rule's person on a different parcel: the subject without its anchor -> review.
    job, ids = await _outbox(db, business_user, ("SMITH JOHN", "5555555555"))
    _rules(monkeypatch, _history(["pending", "active"]))
    outbox.process_dialer_outbox(job)
    assert (await _states(db, job))[ids["SMITH JOHN"]][:2] == ("skipped", "privacy review")
    assert sent == []


async def test_rules_that_cannot_load_send_nothing_and_leave_every_row_pending(
    db, business_user, sent, monkeypatch,
):
    job, ids = await _outbox(db, business_user, ("DOE JANE", "9876543210"))

    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(outbox, "load_rule_snapshot", _broken)
    with pytest.raises(RuleSnapshotError):
        outbox.process_dialer_outbox(job)
    assert sent == []
    assert (await _states(db, job))[ids["DOE JANE"]] == ("pending", None, 0)


async def test_a_flagged_row_is_skipped_with_the_privacy_reason(db, business_user, sent, monkeypatch):
    """The P3 flag hides the row from the send-time read; the reason says privacy,
    not the tax cap it used to claim."""
    job, ids = await _outbox(db, business_user, ("DOE JANE", "9876543210"))
    await _withhold(db, ids["DOE JANE"])
    _rules(monkeypatch, [])
    outbox.process_dialer_outbox(job)
    assert (await _states(db, job))[ids["DOE JANE"]][:2] == ("skipped", "privacy withheld")
    assert sent == []


async def test_with_no_rules_every_row_is_sent(db, business_user, sent, monkeypatch):
    job, ids = await _outbox(db, business_user, ("SMITH JOHN", _PARCEL))
    _rules(monkeypatch, _history(["pending"]))  # recorded, not enforced
    outbox.process_dialer_outbox(job)
    assert sent == [ids["SMITH JOHN"]]


async def test_the_real_loader_runs_in_the_drain(db, business_user, sent):
    """No seam on the rules: the drain loads them through privacy_rule_rows()."""
    job, ids = await _outbox(db, business_user, ("DOE JANE", "9876543210"))
    outbox.process_dialer_outbox(job)
    assert sent == [ids["DOE JANE"]]
