"""Tracerfy answers for a withheld person are quarantined at ingest (Phase 6 P4d).

The real ingest against the test database; only the CSV download and the provider's
queue record are stubbed (tests/test_tracerfy_ingest.py), and the rule snapshot (the
database refuses any rule state but 'pending' until P7, so an active rule is built in
memory). Synthetic people only.
"""
from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

import src.privacy.authorize as authorize_mod
from src.db.session import system_sync_session
from src.privacy import keys
from src.privacy.authorize import RuleSnapshotError, build_snapshot
from src.privacy.subjects import NORMALIZATION_VERSION, subject_key_values
from src.workers.tracerfy_ingest import ingest_tracerfy_batch
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring
from tests.test_tracerfy_ingest import (
    DOWNLOAD_URL,
    _csv,
    _next_queue_id,
    _pending_status,
    _result_row,
    _seed,
    _stub_csv,  # noqa: F401 -- fixture
    _usage,
)

_PARCEL = "0123456789"
_ANSWER = "{addr},TACOMA,WA,JANE,DOE,2065550100,Mobile,2065550100,,,jane@example.com,"


@pytest.fixture
def answers(request):
    """Serve this canned result CSV (tests/test_tracerfy_ingest._stub_csv)."""
    return request.getfixturevalue("_stub_csv")


def _rule(parcel: str, name: str = "DOE JANE") -> list[dict]:
    """pending -> active: the person ``name`` at ``parcel`` in Pierce."""
    [subject] = authorize_mod._subjects({"party_name": name}, "probate", "pierce")
    s_kind, s_value = subject_key_values(subject)[0]
    anchor = keys.property_anchor_value(parcel, "pierce", "WA")
    t0, rule_id = datetime(2026, 11, 6, tzinfo=UTC), str(uuid.uuid4())
    return [{
        "id": str(uuid.uuid4()), "rule_id": rule_id, "anchor_kind": "property",
        "anchor_hmac": keys.privacy_hmac("property", anchor, "v1"),
        "subject_kind": s_kind, "subject_hmac": keys.privacy_hmac(s_kind, s_value, "v1"),
        "scope": "subject", "key_version": "v1", "normalization_version": NORMALIZATION_VERSION,
        "state": state, "created_at": t0 + timedelta(minutes=i),
    } for i, state in enumerate(("pending", "active"))]


def _set_parcel(result_id: str, parcel: str) -> None:
    with system_sync_session() as db:
        db.execute(text("UPDATE results SET parcel_id = :p WHERE id = :r"),
                   {"p": parcel, "r": result_id})
        db.commit()


def _cache_count(pending_id: str) -> int:
    from src.db.models import PendingSkipTraceRow
    from src.scrapers.enrichment.skip_trace import pending_row_subject_key

    with system_sync_session() as db:
        key = pending_row_subject_key(db.get(PendingSkipTraceRow, pending_id))
        return db.execute(text("SELECT count(*) FROM skip_trace_cache WHERE address_hash = :k"),
                          {"k": key}).scalar_one()


def _ingest(qid: int) -> dict:
    return ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL,
                                 rows_uploaded=2, credits_deducted=2)


async def test_a_withheld_answer_is_quarantined_and_the_other_is_stored(
    starter_user, answers, monkeypatch,
):
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("1 FIR ST", "TACOMA", "WA"), ("2 FIR ST", "TACOMA", "WA")])
    held, kept = seed["rows"]
    _set_parcel(held["result_id"], _PARCEL)
    _set_parcel(kept["result_id"], "0987654321")
    with system_sync_session() as db:     # another person: DOE JANE elsewhere is review
        db.execute(text("UPDATE results SET party_name = 'ROE RICK' WHERE id = :r"),
                   {"r": kept["result_id"]})
        db.commit()
    answers(_csv(_ANSWER.format(addr="1 FIR ST"), _ANSWER.format(addr="2 FIR ST")))
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot",
                        lambda db: build_snapshot(_rule(_PARCEL)))
    before = _usage(starter_user.id)

    out = _ingest(qid)
    assert out["hits"] == 1                                # the quarantined answer is not a hit

    row = _result_row(held["result_id"])
    assert (row.skip_trace_status, row.phone, row.email, row.phones, row.emails) == (
        "errored", None, None, None, None)
    assert _pending_status(held["pending_id"]) == "quarantined"
    assert _cache_count(held["pending_id"]) == 0
    # The other person at the other parcel is answered and billed as always.
    assert _result_row(kept["result_id"]).phone == "2065550100"
    assert _pending_status(kept["pending_id"]) == "completed"
    assert _usage(starter_user.id) == before + 1          # the quarantined row is not billed


async def test_quarantine_drops_an_earlier_cached_answer_for_that_subject(
    starter_user, answers, monkeypatch,
):
    from src.db.models import PendingSkipTraceRow, SkipTraceCache
    from src.scrapers.enrichment.skip_trace import pending_row_subject_key

    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("3 FIR ST", "TACOMA", "WA")])
    [held] = seed["rows"]
    _set_parcel(held["result_id"], _PARCEL)
    with system_sync_session() as db:
        key = pending_row_subject_key(db.get(PendingSkipTraceRow, held["pending_id"]))
        db.add(SkipTraceCache(address_hash=key, phone="2065550199", phones=[], emails=[],
                              fetched_at=datetime.now(UTC)))
        db.commit()
    answers(_csv(_ANSWER.format(addr="3 FIR ST")))
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot",
                        lambda db: build_snapshot(_rule(_PARCEL)))
    _ingest(qid)
    assert _cache_count(held["pending_id"]) == 0


async def test_rules_that_cannot_load_write_nothing(starter_user, answers, monkeypatch):
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("4 FIR ST", "TACOMA", "WA")])
    [row] = seed["rows"]
    answers(_csv(_ANSWER.format(addr="4 FIR ST")))

    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    with pytest.raises(RuleSnapshotError):
        _ingest(qid)
    assert _pending_status(row["pending_id"]) == "submitted"
    assert _result_row(row["result_id"]).phone is None
    with system_sync_session() as db:
        assert db.execute(text("SELECT status FROM skip_trace_queues WHERE tracerfy_queue_id = :q"),
                          {"q": qid}).scalar_one() == "pending"


async def test_the_real_loader_runs_in_ingest(starter_user, answers):
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("5 FIR ST", "TACOMA", "WA")])
    answers(_csv(_ANSWER.format(addr="5 FIR ST")))
    _ingest(qid)
    assert _pending_status(seed["rows"][0]["pending_id"]) == "completed"


def test_a_contact_lookup_action_settles_a_quarantined_lead_as_released():
    """Otherwise an action holding a quarantined lead would never finish."""
    from src.workers.scheduler_helpers import contact_lookups

    assert "'quarantined'" in contact_lookups._MAPPABLE
    src = inspect.getsource(contact_lookups)
    assert 'status in ("cancelled", "quarantined")' in src


def _point_job_at_strangers_config(job_id: str, stranger_id: str) -> None:
    """A corrupt pairing: the job names a config another tenant owns."""
    import uuid as _uuid

    from src.db.models import ScraperConfig

    cfg = str(_uuid.uuid4())
    with system_sync_session() as db:
        db.add(ScraperConfig(id=cfg, user_id=stranger_id, name="stranger", county="king",
                             state="WA", record_type="probate", fields=[], enrichment=[],
                             schedule={"frequency": "manual"}, deliver={"emails": []}))
        db.flush()
        db.execute(text("UPDATE jobs SET scraper_config_id = :c WHERE id = :j"),
                   {"c": cfg, "j": job_id})
        db.commit()


async def test_a_lead_whose_config_is_another_tenants_is_quarantined(
    starter_user, business_user, answers, monkeypatch,
):
    """Corrupt pairing: the job's config belongs to someone else, so the lead's county is
    unknown and its parcel cannot be checked. With a rule active it is never stored."""
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("6 FIR ST", "TACOMA", "WA")])
    [row] = seed["rows"]
    _point_job_at_strangers_config(seed["job_id"], business_user.id)
    answers(_csv(_ANSWER.format(addr="6 FIR ST")))
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot",
                        lambda db: build_snapshot(_rule("5555555555", name="ROE RICK")))
    _ingest(qid)
    assert _pending_status(row["pending_id"]) == "quarantined"
    assert _result_row(row["result_id"]).phone is None


def _phone_rule(phone_digits: str) -> list[dict]:
    """pending -> active: a rule anchored on a PHONE (subject DOE JANE)."""
    [subject] = authorize_mod._subjects({"party_name": "DOE JANE"}, "probate", "pierce")
    s_kind, s_value = subject_key_values(subject)[0]
    t0, rule_id = datetime(2026, 11, 7, tzinfo=UTC), str(uuid.uuid4())
    return [{
        "id": str(uuid.uuid4()), "rule_id": rule_id, "anchor_kind": "phone",
        "anchor_hmac": keys.privacy_hmac("phone", phone_digits, "v1"),
        "subject_kind": s_kind, "subject_hmac": keys.privacy_hmac(s_kind, s_value, "v1"),
        "scope": "subject", "key_version": "v1", "normalization_version": NORMALIZATION_VERSION,
        "state": state, "created_at": t0 + timedelta(minutes=i),
    } for i, state in enumerate(("pending", "active"))]


async def test_an_answer_whose_own_phone_a_rule_anchors_is_quarantined(
    starter_user, answers, monkeypatch,
):
    """P5h: the incoming phone is an anchor too. The lead's name cannot be read in its
    source's order, so it is allowed until the answer's phone (the rule's anchor) is on
    it: anchor + unresolved subject -> review -> quarantined. Its control, a readable
    other person, gets the same phone and is stored and billed as always."""
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("3 FIR ST", "TACOMA", "WA"), ("4 FIR ST", "TACOMA", "WA")])
    held, kept = seed["rows"]
    with system_sync_session() as db:
        db.execute(text("UPDATE results SET party_name = 'ИВАНОВ ИВАН' WHERE id = :r"),
                   {"r": held["result_id"]})
        db.execute(text("UPDATE results SET party_name = 'ROE RICK' WHERE id = :r"),
                   {"r": kept["result_id"]})
        db.commit()
    answers(_csv(_ANSWER.format(addr="3 FIR ST"), _ANSWER.format(addr="4 FIR ST")))
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot",
                        lambda db: build_snapshot(_phone_rule("2065550100")))
    before = _usage(starter_user.id)

    _ingest(qid)
    assert _pending_status(held["pending_id"]) == "quarantined"
    assert _result_row(held["result_id"]).phone is None
    assert _cache_count(held["pending_id"]) == 0
    assert _pending_status(kept["pending_id"]) == "completed"
    assert _result_row(kept["result_id"]).phone == "2065550100"
    assert _usage(starter_user.id) == before + 1
