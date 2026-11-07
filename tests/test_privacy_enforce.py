"""enforce_rows: the privacy flag is written from the rules (Phase 6 P5c).

Real database. One seam: the active rule snapshot is built in memory with
build_snapshot (the database refuses any rule state but 'pending' until P7, as in
tests/test_privacy_dialer_outbox.py), with its active row id pointed at a REAL pending
rule row so the results foreign key holds. Synthetic people only.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.db.session import system_sync_session
from src.privacy.authorize import RuleSnapshotError, build_snapshot
from src.privacy.enforce import enforce_rows
from tests.test_privacy_authorize import _history, _keyring  # noqa: F401 -- autouse keyring

_PARCEL = "1234567890"  # King: the anchor P|WA|king|1234567890 of _history's rule


async def _rule_row(db) -> str:
    request_id = (await db.execute(text(
        "INSERT INTO privacy_requests (kind, channel, deadline_at) "
        "VALUES ('opt_out', 'web', now() + interval '45 days') RETURNING id"))).scalar()
    rule_row = (await db.execute(text(
        "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, anchor_hmac, "
        "subject_kind, subject_hmac, scope, key_version, normalization_version, state) "
        "VALUES (gen_random_uuid(), :r, 'property', :h, 'person', :h, 'subject', 'v1', "
        "'n3', 'pending') RETURNING id"), {"r": request_id, "h": "c" * 64})).scalar()
    await db.commit()
    return str(rule_row)


def active_snapshot(rule_row: str, **over):
    """SMITH JOHN on King parcel 1234567890, active; the active row id is rule_row."""
    rows = _history(["pending", "active"], **over)
    rows[1]["id"] = rule_row
    return build_snapshot(rows)


async def _job(db, user: User, *, config_owner: User | None = None) -> str:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=(config_owner or user).id, name="Enforce",
        county="king", state="WA", record_type="probate", fields=[], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"emails": []})
    db.add(cfg)
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status="done",
               trigger="manual", record_count=0, billed_count=0, retry_count=0,
               created_at=datetime.now(UTC)))
    await db.commit()
    return job_id


async def _rows(db, user: User, job_id: str, *people: tuple[str, str | None]) -> list[str]:
    ids = []
    for name, parcel in people:
        rid = str(uuid.uuid4())
        db.add(Result(id=rid, job_id=job_id, user_id=user.id, party_name=name,
                      parcel_id=parcel, property_address="1 TEST ST",
                      dedup_hash=uuid.uuid4().hex, is_duplicate=False, enrichment_data={}))
        ids.append(rid)
    await db.commit()
    return ids


async def _state(db, rid: str) -> tuple:
    row = (await db.execute(text(
        "SELECT privacy_suppressed_at IS NOT NULL, privacy_suppression_id::text, "
        "privacy_checked_gen FROM results WHERE id = :i"), {"i": rid})).one()
    return tuple(row)


def _enforce(snapshot, ids):
    with system_sync_session() as s:
        stats = enforce_rows(s, snapshot, ids)
        s.commit()
    return stats


async def test_withheld_and_review_rows_are_flagged_and_others_stamped(db, business_user):
    rule = await _rule_row(db)
    job = await _job(db, business_user)
    smith, review, doe = await _rows(db, business_user, job, ("SMITH JOHN", _PARCEL),
                                     ("SMITH JOHN", "5555555555"), ("DOE JANE", _PARCEL))
    snapshot = active_snapshot(rule)
    stats = _enforce(snapshot, [smith, review, doe])
    assert (stats.checked, stats.flagged) == (3, 2)
    gen = snapshot.generation
    assert await _state(db, smith) == (True, rule, gen)
    assert await _state(db, review) == (True, rule, gen)   # REVIEW is withheld too
    assert await _state(db, doe) == (False, None, gen)     # someone else: allowed, stamped


async def test_no_rule_ever_active_touches_nothing(db, business_user):
    job = await _job(db, business_user)
    (smith,) = await _rows(db, business_user, job, ("SMITH JOHN", _PARCEL))
    stats = _enforce(build_snapshot(_history(["pending"])), [smith])
    assert stats.checked == 0
    assert await _state(db, smith) == (False, None, 0)


async def test_a_row_already_stamped_is_rechecked_for_its_writer(db, business_user):
    # A writer just gave a checked row the rule's name: the stamp must not hide it.
    rule = await _rule_row(db)
    job = await _job(db, business_user)
    (rid,) = await _rows(db, business_user, job, ("DOE JANE", _PARCEL))
    snapshot = active_snapshot(rule)
    _enforce(snapshot, [rid])
    row = await db.get(Result, rid)
    row.party_name = "SMITH JOHN"
    await db.commit()
    assert _enforce(snapshot, [rid]).flagged == 1
    assert (await _state(db, rid))[0] is True


async def test_a_flagged_row_is_never_reflagged_or_cleared(db, business_user):
    rule = await _rule_row(db)
    job = await _job(db, business_user)
    (rid,) = await _rows(db, business_user, job, ("SMITH JOHN", _PARCEL))
    _enforce(active_snapshot(rule), [rid])
    first = await _state(db, rid)
    # All rules lifted: nothing matches any more, and still nothing is cleared here.
    lifted = build_snapshot(_history(["pending", "active", "lifted"]))
    assert _enforce(lifted, [rid]).checked == 0
    assert await _state(db, rid) == first


@pytest.mark.parametrize("state", ["purging", "deleted"])
async def test_a_purging_or_deleted_account_is_left_to_the_purge(db, business_user, state):
    rule = await _rule_row(db)
    job = await _job(db, business_user)
    (rid,) = await _rows(db, business_user, job, ("SMITH JOHN", _PARCEL))
    # Only the purge role may move deletion_state (tests/test_account_deletion_lifecycle.py).
    await db.execute(text("SET LOCAL ROLE bridgeleads_purge"))
    await db.execute(text("UPDATE users SET deletion_state = :s WHERE id = :u"),
                     {"s": state, "u": business_user.id})
    await db.commit()
    assert _enforce(active_snapshot(rule), [rid]).checked == 0
    assert await _state(db, rid) == (False, None, 0)


async def test_a_row_of_a_corrupt_cross_tenant_job_is_still_decided(
    db, business_user, starter_user,
):
    # Never left undecided (Codex P5c): its job's config, whoever owns it, says how to
    # read it, and the decision can only withhold.
    rule = await _rule_row(db)
    job = await _job(db, business_user, config_owner=starter_user)
    (rid,) = await _rows(db, business_user, job, ("SMITH JOHN", _PARCEL))
    snapshot = active_snapshot(rule)
    assert _enforce(snapshot, [rid]).flagged == 1
    assert await _state(db, rid) == (True, rule, snapshot.generation)


async def test_batches_larger_than_the_sweeps_are_decided_row_by_row(db, business_user):
    rule = await _rule_row(db)
    job = await _job(db, business_user)
    people = [("SMITH JOHN" if i % 3 == 0 else f"DOE JANE{chr(65 + i % 26)}", _PARCEL)
              for i in range(600)]
    ids = await _rows(db, business_user, job, *people)
    snapshot = active_snapshot(rule)
    stats = _enforce(snapshot, ids)
    assert (stats.checked, stats.flagged) == (600, 200)
    flagged = (await db.execute(text(
        "SELECT count(*) FILTER (WHERE privacy_suppressed_at IS NOT NULL), "
        "count(*) FILTER (WHERE privacy_checked_gen = :g) FROM results WHERE job_id = :j"),
        {"g": snapshot.generation, "j": job})).one()
    assert tuple(flagged) == (200, 600)


async def test_an_undecidable_row_rolls_the_whole_batch_back(db, business_user, monkeypatch):
    rule = await _rule_row(db)
    job = await _job(db, business_user)
    smith, doe = await _rows(db, business_user, job, ("SMITH JOHN", _PARCEL), ("DOE JANE", "1"))
    snapshot = active_snapshot(rule)

    def _boom(*a, **k):
        raise RuleSnapshotError("privacy key v1 unavailable")

    monkeypatch.setattr("src.privacy.enforce.authorize", _boom)
    with pytest.raises(RuleSnapshotError), system_sync_session() as s:
        enforce_rows(s, snapshot, [smith, doe])
        s.commit()
    assert await _state(db, smith) == (False, None, 0)
    assert await _state(db, doe) == (False, None, 0)


# ── the generation ────────────────────────────────────────────────────────────

def test_the_generation_counts_active_rule_rows_ever_written():
    assert build_snapshot(_history(["pending"])).generation == 0
    assert build_snapshot(_history(["pending", "active"])).generation == 1
    # Lifting adds a row but no activation: the generation never falls back.
    assert build_snapshot(_history(["pending", "active", "lifted"])).generation == 1
    two = _history(["pending", "active"]) + _history(["pending", "active", "lifted"],
                                                     subject=("person", "doe|jane"))
    assert build_snapshot(two).generation == 2
    # A key rotation re-keys the rule under v2: a new activation, so a full re-check.
    rule_id = str(uuid.uuid4())
    rotated = (_history(["pending", "active"], rule_id=rule_id)
               + _history(["pending", "active"], rule_id=rule_id, version="v2"))
    assert build_snapshot(rotated).generation == 2
