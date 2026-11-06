"""The Tracerfy dispatcher authorizes each lead before it claims it (Phase 6 P4c).

The real dispatcher tick against the test database, with no network: the Tracerfy base
URL is non-HTTPS, so submit_batch refuses before any socket opens and a CLAIMED row
ends 'errored' (tests/test_skip_trace_already_delivered.py). A row the privacy check
withdraws ends 'cancelled' instead and never reaches the claim.

One seam: the rule snapshot. The database refuses any rule state but 'pending' until
P7, so an active rule is built in memory with build_snapshot, keyed on the lead's own
parcel and name exactly as authorize() keys them. Synthetic people only.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

import src.privacy.authorize as authorize_mod
from src.privacy import keys
from src.privacy.authorize import RuleSnapshotError, build_snapshot
from src.privacy.subjects import NORMALIZATION_VERSION, subject_key_values
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring
from tests.test_skip_trace_already_delivered import (
    _PARTY,
    _dispatch,
    _dispatcher_on,  # noqa: F401 -- fixture
    _lead,
    _parcel,
    _pending_status,
    _queue,
    _row,
    _run,
)

_T0 = datetime(2026, 11, 6, tzinfo=UTC)

# Skip trace on, Tracerfy unreachable by design (non-HTTPS base URL), alerts off.
pytestmark = pytest.mark.usefixtures("_dispatcher_on")


def _active_rule(*, parcel: str | None, name: str = _PARTY) -> list[dict]:
    """pending -> active rule for (parcel in Clark, the person named ``name``)."""
    [subject] = authorize_mod._subjects({"party_name": name}, "probate", "clark")
    s_kind, s_value = subject_key_values(subject)[0]
    anchor = keys.property_anchor_value(parcel or "9999999999", "clark", "WA")
    rule_id = str(uuid.uuid4())
    return [{
        "id": str(uuid.uuid4()), "rule_id": rule_id, "anchor_kind": "property",
        "anchor_hmac": keys.privacy_hmac("property", anchor, "v1"),
        "subject_kind": s_kind, "subject_hmac": keys.privacy_hmac(s_kind, s_value, "v1"),
        "scope": "subject", "key_version": "v1", "normalization_version": NORMALIZATION_VERSION,
        "state": state, "created_at": _T0 + i * timedelta(minutes=1),
    } for i, state in enumerate(("pending", "active"))]


def _rules(monkeypatch, rows) -> None:
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", lambda db: build_snapshot(rows))


def _lead_queued(user_id: str, n: int) -> tuple[str, str]:
    job = _run(user_id, skip_on=True, status="done")
    rid = _lead(user_id, job, n)
    return rid, _queue(user_id, job, rid, n)


async def test_a_withheld_lead_is_withdrawn_unpaid_and_never_claimed(
    business_user, monkeypatch,
):
    rid, pid = _lead_queued(business_user.id, 1)
    _rules(monkeypatch, _active_rule(parcel=_parcel(1)))
    out = _dispatch()
    assert out["submitted_rows"] == 0 and not out["errors"]
    assert _pending_status(pid) == "cancelled"
    assert _row(rid).skip_trace_status == "not_attempted"


async def test_a_lead_under_review_is_withdrawn_too(business_user, monkeypatch):
    # The rule's person at another parcel: the subject without its anchor -> review.
    rid, pid = _lead_queued(business_user.id, 2)
    _rules(monkeypatch, _active_rule(parcel=None))
    _dispatch()
    assert _pending_status(pid) == "cancelled"


async def test_someone_else_at_the_same_parcel_is_still_bought(
    business_user, monkeypatch,
):
    """A shared parcel alone never suppresses another person (design D2 matrix)."""
    rid, pid = _lead_queued(business_user.id, 3)
    _rules(monkeypatch, _active_rule(parcel=_parcel(3), name="DOE JANE Q"))
    out = _dispatch()
    assert any("HTTPS" in e for e in out["errors"]), "the row never reached the claim"
    assert _pending_status(pid) == "errored"


async def test_rules_that_cannot_load_claim_nothing(business_user, monkeypatch):
    rid, pid = _lead_queued(business_user.id, 4)

    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    out = _dispatch()
    assert out["submitted_rows"] == 0 and "privacy rules unavailable" in out["errors"]
    assert _pending_status(pid) == "queued"
    assert _row(rid).skip_trace_status == "queued"


async def test_the_real_loader_runs_in_the_tick(business_user):
    """No seam: the tick loads the rules through privacy_rule_rows() and buys the lead."""
    _, pid = _lead_queued(business_user.id, 5)
    out = _dispatch()
    assert any("HTTPS" in e for e in out["errors"])
    assert _pending_status(pid) == "errored"


def test_a_row_mapping_is_read_by_key():
    """The dispatcher hands authorize() a SQLAlchemy RowMapping. Read by attribute it
    would see no name and no parcel, and allow everyone."""
    from sqlalchemy import literal, select

    from src.db.session import sync_engine

    with sync_engine.connect() as conn:
        m = conn.execute(select(literal("SMITH JOHN").label("party_name"))).mappings().one()
    assert authorize_mod._get(m, "party_name") == "SMITH JOHN"


async def test_a_lead_without_its_owners_config_is_withdrawn_while_rules_are_active(
    business_user, starter_user, monkeypatch,
):
    """Its county is unknown, so its parcel cannot be checked: never bought unchecked."""
    from sqlalchemy import text

    from src.db.session import system_sync_session

    rid, pid = _lead_queued(business_user.id, 6)
    from tests.test_privacy_tracerfy_ingest import _point_job_at_strangers_config

    with system_sync_session() as db:
        job = db.execute(text("SELECT job_id::text FROM results WHERE id = :r"),
                         {"r": rid}).scalar_one()
    _point_job_at_strangers_config(job, starter_user.id)
    _rules(monkeypatch, _active_rule(parcel="9999999999", name="ROE RICK Q"))
    _dispatch()
    assert _pending_status(pid) == "cancelled"
