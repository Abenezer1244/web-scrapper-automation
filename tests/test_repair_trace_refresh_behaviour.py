"""BEHAVIOURAL cover for _refresh_pending_name in
scripts/repair_probate_party_and_bad_parcel.py — real Postgres, no mocks.

The sibling test module asserts the SHAPE of each statement. That is not enough
here: Codex round 7 found two defects that a text assertion cannot see, because
both are about what the statements DO across more than one row.

  [P2] the UPDATE was keyed on result_id while the caller loops per pending row,
       so with two pending rows on one result the first iteration wrote both and
       the rest journalled phantom writes;
  [P2] the enqueue SUPPRESSES a non-personal party entirely (no pending row at
       all), while the refresh downgraded it to a paid address-only trace.

Seeding follows tests/test_skip_trace_dispatcher_claim.py — system_sync_session,
the same session type the script itself runs under.
"""
import uuid

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session

from .test_repair_probate_party_and_bad_parcel import _mod

# Address-shaped, no entity token -> looks_like_non_personal_party_name is True,
# which is exactly when build_pending_row_payload returns None and the enqueue
# creates no pending row at all.
_NON_PERSONAL_PARTY = "1819 HARVARD AVE"
# "LAST FIRST INITIAL" — the shape select_traceable_owner resolves to a person.
# NOT the 3-full-token "REINKE NORMAN LEONARD": that is ambiguous BY DESIGN
# (_owner_confidence returns -1) and routes to an address-only ADVANCED trace, so
# using it here would have asserted the opposite of what the enqueue does.
_PERSON_PARTY = "REINKE NORMAN L"


def _seed(user_id: str, pendings: list[dict], *, party: str = _PERSON_PARTY) -> tuple[str, list[str]]:
    """scraper_config -> job -> result ('queued') -> N pending rows. Returns ids."""
    sc_id, job_id, result_id = (str(uuid.uuid4()) for _ in range(3))
    pending_ids = []
    with system_sync_session() as db:
        db.execute(
            text("""
                INSERT INTO scraper_configs
                    (id, user_id, name, county, state, record_type, fields, enrichment,
                     schedule, deliver, skip_trace_enabled, active)
                VALUES (:sc_id, :user_id, 'trace refresh test', 'king', 'WA', 'probate',
                        '[]'::json, '[]'::json, '{"frequency":"manual"}'::json,
                        '{"format":"csv","emails":[]}'::json, true, true)
            """),
            {"sc_id": sc_id, "user_id": user_id},
        )
        db.execute(
            text("""
                INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger,
                                  page_current, page_total, record_count, retry_count)
                VALUES (:job_id, :user_id, :sc_id, 'done', 'manual', 0, 0, 0, 0)
            """),
            {"job_id": job_id, "user_id": user_id, "sc_id": sc_id},
        )
        db.execute(
            text("""
                INSERT INTO results (id, job_id, user_id, is_duplicate, skip_trace_status,
                                     party_name, property_address, created_at)
                VALUES (:rid, :job_id, :user_id, false, 'queued',
                        :party, '11547 CORLISS AVE N 98133', now())
            """),
            {"rid": result_id, "job_id": job_id, "user_id": user_id, "party": party},
        )
        for spec in pendings:
            pid = str(uuid.uuid4())
            pending_ids.append(pid)
            db.execute(
                text("""
                    INSERT INTO pending_skip_trace_rows
                        (id, job_id, result_id, user_id, property_address, city, state,
                         first_name, last_name, trace_type, status, enqueued_at,
                         tracerfy_queue_id, submitted_at)
                    VALUES (:pid, :job_id, :rid, :user_id, '11547 CORLISS AVE N 98133',
                            'SEATTLE', 'WA', :first, :last, :trace_type, :status, now(),
                            :queue_id, :submitted_at)
                """),
                {"pid": pid, "job_id": job_id, "rid": result_id, "user_id": user_id,
                 "first": spec.get("first"), "last": spec.get("last"),
                 "trace_type": spec.get("trace_type", "normal"),
                 "status": spec.get("status", "queued"),
                 "queue_id": spec.get("queue_id"),
                 "submitted_at": spec.get("submitted_at")},
            )
        db.commit()
    return result_id, pending_ids


def _pending(pending_id: str) -> tuple:
    with system_sync_session() as db:
        return db.execute(
            text("""SELECT first_name, last_name, trace_type, status
                    FROM pending_skip_trace_rows WHERE id = :id"""),
            {"id": pending_id},
        ).one()


def _result_trace_status(result_id: str) -> str:
    with system_sync_session() as db:
        return db.execute(
            text("SELECT skip_trace_status FROM results WHERE id = :id"), {"id": result_id}
        ).scalar_one()


def _run(result_id: str, party: str, *, apply: bool, tmp_path) -> dict:
    journal = str(tmp_path / "journal.jsonl")
    with system_sync_session() as db:
        counts = _mod._refresh_pending_name(
            db, result_id, party, journal=journal, apply=apply
        )
        if apply:
            db.commit()
    return counts


def _journal_lines(tmp_path) -> list[dict]:
    import json
    path = tmp_path / "journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.mark.asyncio
async def test_every_sibling_pending_row_is_journalled_against_its_own_write(
    starter_user, tmp_path
):
    """Round-7 [P2]: two pending rows on ONE result carrying the SAME stale tuple.

    Measured honestly, the pre-fix statement reached the same END STATE — keyed on
    result_id, its first iteration updated BOTH siblings (rowcount 2) and the
    second matched nothing. What it got wrong was the AUDIT RECORD: the journal
    was written before the statement ran, so it claimed a second, separate write
    that never happened, and the per-row rowcount was meaningless. For a repair
    whose whole justification is an auditable trail of what it spent money on,
    that is the defect. So this asserts on the journal, not just the rows —
    exactly one entry per pending row, each accounting for its OWN single write.
    """
    stale = {"first": "STATE", "last": "WASHINGTON", "trace_type": "normal"}
    result_id, pids = _seed(starter_user.id, [dict(stale), dict(stale)])

    counts = _run(result_id, _PERSON_PARTY, apply=True, tmp_path=tmp_path)

    assert counts == {"refreshed": 2, "suppressed": 0}
    for pid in pids:
        assert _pending(pid) == ("NORMAN", "REINKE", "normal", "queued")

    entries = _journal_lines(tmp_path)
    assert len(entries) == 2
    # One entry per row, each attributing exactly one write to its own id.
    assert {e["pending_id"] for e in entries} == set(pids)
    assert [e["written"] for e in entries] == [1, 1]


@pytest.mark.asyncio
async def test_a_row_already_at_the_provider_is_never_rewritten(starter_user, tmp_path):
    # Correlated to a Tracerfy queue id, or already submitted: possibly paid for.
    result_id, pids = _seed(starter_user.id, [
        {"first": "STATE", "last": "WASHINGTON", "queue_id": 4242},
        {"first": "STATE", "last": "WASHINGTON", "status": "completed"},
    ])

    counts = _run(result_id, _PERSON_PARTY, apply=True, tmp_path=tmp_path)

    assert counts["refreshed"] == 0
    assert _pending(pids[0])[:3] == ("STATE", "WASHINGTON", "normal")
    assert _pending(pids[1])[:3] == ("STATE", "WASHINGTON", "normal")


@pytest.mark.asyncio
async def test_a_non_personal_party_is_suppressed_not_downgraded(starter_user, tmp_path):
    # The enqueue would have created NO pending row for this party. Downgrading it
    # to 'advanced' keeps paying for an address-only trace the enqueue refuses.
    result_id, pids = _seed(
        starter_user.id,
        [{"first": "STATE", "last": "WASHINGTON", "trace_type": "normal"}],
        party=_NON_PERSONAL_PARTY,
    )

    counts = _run(result_id, _NON_PERSONAL_PARTY, apply=True, tmp_path=tmp_path)

    assert counts == {"refreshed": 0, "suppressed": 1}
    assert _pending(pids[0])[3] == "errored"
    # ...and the lead does not sit in 'queued' behind a row that will never go out.
    assert _result_trace_status(result_id) == "not_attempted"


@pytest.mark.asyncio
async def test_a_row_at_the_provider_is_not_suppressed_either(starter_user, tmp_path):
    result_id, pids = _seed(
        starter_user.id,
        [{"first": "STATE", "last": "WASHINGTON", "status": "submitted"}],
        party=_NON_PERSONAL_PARTY,
    )

    counts = _run(result_id, _NON_PERSONAL_PARTY, apply=True, tmp_path=tmp_path)

    assert counts == {"refreshed": 0, "suppressed": 0}
    assert _pending(pids[0])[3] == "submitted"
    assert _result_trace_status(result_id) == "queued"


@pytest.mark.asyncio
async def test_dry_run_counts_exactly_what_apply_would_touch(starter_user, tmp_path):
    """A preview that over-reports is not a preview.

    The per-statement WHERE guard alone is not enough: without the same
    not-yet-at-the-provider test in the loop, a dry run counts and journals rows
    that --apply then declines to write, so the operator authorises one thing and
    a different thing runs. One refreshable row beside three untouchable ones must
    read as 1 in BOTH modes.
    """
    result_id, _ = _seed(starter_user.id, [
        {"first": "STATE", "last": "WASHINGTON", "trace_type": "normal"},
        {"first": "STATE", "last": "WASHINGTON", "queue_id": 99},
        {"first": "STATE", "last": "WASHINGTON", "status": "submitted"},
        {"first": "STATE", "last": "WASHINGTON", "status": "errored"},
    ])

    dry = _run(result_id, _PERSON_PARTY, apply=False, tmp_path=tmp_path)
    applied = _run(result_id, _PERSON_PARTY, apply=True, tmp_path=tmp_path)

    assert dry == applied == {"refreshed": 1, "suppressed": 0}


@pytest.mark.asyncio
async def test_dry_run_reports_the_cost_impact_and_writes_nothing(starter_user, tmp_path):
    # Round-7 [P3]: trace_type decides what we SPEND, so it has to be visible
    # before --apply is authorised.
    result_id, pids = _seed(
        starter_user.id, [{"first": "STATE", "last": "WASHINGTON", "trace_type": "normal"}]
    )

    counts = _run(result_id, _PERSON_PARTY, apply=False, tmp_path=tmp_path)

    assert counts == {"refreshed": 1, "suppressed": 0}
    assert _pending(pids[0])[:3] == ("STATE", "WASHINGTON", "normal")
    assert (tmp_path / "journal.jsonl").read_text(encoding="utf-8").count("refresh_dry_run") == 1


@pytest.mark.asyncio
async def test_second_apply_is_a_no_op(starter_user, tmp_path):
    result_id, pids = _seed(
        starter_user.id, [{"first": "STATE", "last": "WASHINGTON", "trace_type": "normal"}]
    )

    first = _run(result_id, _PERSON_PARTY, apply=True, tmp_path=tmp_path)
    second = _run(result_id, _PERSON_PARTY, apply=True, tmp_path=tmp_path)

    assert first == {"refreshed": 1, "suppressed": 0}
    assert second == {"refreshed": 0, "suppressed": 0}
    assert _pending(pids[0]) == ("NORMAN", "REINKE", "normal", "queued")
