"""The PACS search-mode repair touches exactly the two cohorts, once, and only safely.

Cohort A (a PACS county the bug broke, outcome `error`, out of attempts) goes back into
the GIS sweep's queue; cohort B (Island, a lettered id "not found" by `pacs_island`) loses
the false outcome but is not queued. Real DB, real rows, the real script functions and
entry point, the real sweep and requeue selection SQL.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User

pytestmark = pytest.mark.asyncio

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = _load("repair_pacs_mailing_recovery")
rq = _load("requeue_gis_mailing_recovery")

_LAST_AT = "2026-10-20T03:00:00+00:00"
_GAVE_UP = {"mailing_recovery_outcome": "error", "mailing_recovery_attempts": 5,
            "mailing_recovery_last_at": _LAST_AT, "mailing_lookup_deferred": False,
            "source": "eagleweb", "delivery_note": "kept"}
_ISLAND_ID = "R03213-049-3470"
_FALSE_NOT_FOUND = {"mailing_recovery_outcome": "parcel_not_found", "mailing_source": "pacs_island",
                    "mailing_recovery_attempts": 5, "mailing_recovery_last_at": _LAST_AT,
                    "mailing_lookup_deferred": False, "source": "eagleweb"}


@pytest.fixture(autouse=True)
def _restricted_mailing_on(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", True)


async def _job(db, user: User, county: str, *, status: str = "done", state: str = "WA") -> str:
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name=f"{county} repair",
                           county=county, state=state, record_type="probate", fields=["party_name"],
                           enrichment=[], schedule={"frequency": "manual"},
                           deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    return job_id


async def _row(db, user, county: str, ed: dict, *, parcel: str = "0630115504300000",
               mailing: str | None = None, status: str = "done", state: str = "WA") -> str:
    job_id = await _job(db, user, county, status=status, state=state)
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name="DOE JANE", parcel_id=parcel,
                  property_address="1335 E EIGHTH ST", mailing_address=mailing,
                  enrichment_data=dict(ed), skip_trace_status="not_attempted", is_duplicate=False))
    await db.commit()
    return rid


async def _ed(db, rid: str) -> dict:
    return (await db.execute(text("SELECT enrichment_data FROM results WHERE id = :i"), {"i": rid})).scalar()


def _repair(tmp_path, *, apply: bool, counties: tuple[str, ...] = rp.RETRY_COUNTIES) -> dict:
    from src.db.session import system_sync_session

    with system_sync_session() as sdb:
        return rp.repair(sdb, apply=apply, report=tmp_path / "evidence.jsonl", counties=counties)


def _sweep_selects(county: str, parcel: str) -> bool:
    """The GIS sweep's own candidate query: would it look this parcel up next?"""
    from src.db.session import system_sync_session
    from src.workers import mailing_recovery as mr

    with system_sync_session() as sdb:
        rows = sdb.execute(text(mr._GIS_CANDIDATE_PARCELS_SQL),
                           {"max_attempts": mr._MAX_ATTEMPTS, "counties": [county]}).all()
    return any(r.parcel_id == parcel for r in rows)


def test_the_attempt_ceiling_is_the_sweeps():
    from src.workers import mailing_recovery as mr

    assert rp._MAX_ATTEMPTS == mr._MAX_ATTEMPTS


async def test_cohort_a_goes_back_into_the_sweeps_queue_with_its_history_kept(db, business_user, tmp_path):
    rid = await _row(db, business_user, "clallam", _GAVE_UP)
    assert not await asyncio.to_thread(_sweep_selects, "clallam", "0630115504300000")

    stats = await asyncio.to_thread(_repair, tmp_path, apply=True)

    assert stats["repaired"] == 1 and stats["by_cohort"] == {"A": 1}
    ed = await _ed(db, rid)
    assert ed["mailing_lookup_deferred"] is True
    assert "mailing_recovery_attempts" not in ed and "mailing_recovery_outcome" not in ed
    assert ed["mailing_recovery_last_at"] == _LAST_AT
    assert ed["source"] == "eagleweb" and ed["delivery_note"] == "kept"
    mark = ed[rp.MARKER]
    assert mark["cohort"] == "A" and mark["previous_outcome"] == "error"
    assert mark["previous_attempts"] == 5 and mark["previous_last_at"] == _LAST_AT
    assert mark["previous_deferred"] is False
    assert await asyncio.to_thread(_sweep_selects, "clallam", "0630115504300000")


async def test_cohort_b_loses_the_false_answer_and_is_queued_only_by_the_standard_requeue(
        db, business_user, tmp_path):
    rid = await _row(db, business_user, "island", _FALSE_NOT_FOUND, parcel=_ISLAND_ID)

    stats = await asyncio.to_thread(_repair, tmp_path, apply=True)

    assert stats["repaired"] == 1 and stats["by_cohort"] == {"B": 1}
    ed = await _ed(db, rid)
    for gone in ("mailing_recovery_outcome", "mailing_source", "mailing_recovery_attempts"):
        assert gone not in ed, gone
    assert ed["mailing_lookup_deferred"] is False                  # not queued by the repair
    assert ed[rp.MARKER]["previous_source"] == "pacs_island"
    assert not await asyncio.to_thread(_sweep_selects, "island", _ISLAND_ID)

    def _requeue():
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            return rq.requeue(sdb, ["island"], apply=True, report=tmp_path / "rq.jsonl")

    assert (await asyncio.to_thread(_requeue))["marked"] == 1      # eligible once Island is searchable
    assert await asyncio.to_thread(_sweep_selects, "island", _ISLAND_ID)


@pytest.mark.parametrize("county,ed,kw", [
    ("benton", _GAVE_UP, {}),                                                        # its lookups worked
    ("clallam", _GAVE_UP, {"mailing": "PO BOX 1, PORT ANGELES, WA 98362"}),          # already has mail
    ("clallam", {**_GAVE_UP, "mailing_recovery_attempts": 3, "mailing_lookup_deferred": True}, {}),
    ("clallam", _GAVE_UP, {"status": "running"}),                                    # job not done
    ("clallam", _GAVE_UP, {"state": "OR"}),                                          # not WA
    ("island", _FALSE_NOT_FOUND, {"parcel": "0321304934700000"}),                    # a numeric id: a real answer
    ("island", {**_FALSE_NOT_FOUND, "mailing_source": "county_gis"}, {"parcel": _ISLAND_ID}),
    ("island", {**_FALSE_NOT_FOUND, "mailing_lookup_deferred": True}, {"parcel": _ISLAND_ID}),
    ("clallam", {**_GAVE_UP, rp.MARKER: {"cohort": "A"}}, {}),                       # repaired before
], ids=["benton", "has-mailing", "still-queued", "job-running", "not-wa", "island-numeric",
        "island-other-source", "island-deferred", "already-repaired"])
async def test_rows_outside_the_cohorts_are_never_touched(db, business_user, tmp_path, county, ed, kw):
    rid = await _row(db, business_user, county, ed, **kw)
    before = await _ed(db, rid)

    stats = await asyncio.to_thread(_repair, tmp_path, apply=True)

    assert stats["candidates"] == 0
    assert await _ed(db, rid) == before


async def test_a_dry_run_reports_every_candidate_and_writes_nothing(db, business_user, tmp_path):
    a = await _row(db, business_user, "grant", _GAVE_UP)
    b = await _row(db, business_user, "island", _FALSE_NOT_FOUND, parcel=_ISLAND_ID)
    before = {a: await _ed(db, a), b: await _ed(db, b)}

    stats = await asyncio.to_thread(_repair, tmp_path, apply=False)

    assert stats["candidates"] == 2 and stats["repaired"] == 0
    lines = [json.loads(x) for x in (tmp_path / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {(x["result_id"], x["cohort"], x["action"]) for x in lines} == {(a, "A", "dry-run"), (b, "B", "dry-run")}
    allowed = {"result_id", "job_id", "county", "cohort", "parcel_id",
               "previous_outcome", "previous_attempts", "action"}
    assert all(set(x) == allowed for x in lines)                        # identifiers only: no names, addresses
    assert {a: await _ed(db, a), b: await _ed(db, b)} == before


async def test_a_report_that_cannot_be_written_stops_everything(db, business_user, tmp_path):
    rid = await _row(db, business_user, "jefferson", _GAVE_UP)
    before = await _ed(db, rid)

    def _bad_report():
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            rp.repair(sdb, apply=True, report=tmp_path / "missing_dir" / "evidence.jsonl")

    with pytest.raises(OSError):
        await asyncio.to_thread(_bad_report)
    assert await _ed(db, rid) == before


async def test_it_repairs_a_row_once_even_after_five_fresh_failures(db, business_user, tmp_path):
    rid = await _row(db, business_user, "chelan", _GAVE_UP)
    await asyncio.to_thread(_repair, tmp_path, apply=True)
    first = (await _ed(db, rid))[rp.MARKER]

    assert (await asyncio.to_thread(_repair, tmp_path, apply=True))["candidates"] == 0   # rerun
    await db.execute(text(                      # the sweep gives up on it again
        "UPDATE results SET enrichment_data = (enrichment_data::jsonb || CAST(:p AS jsonb))::json "
        "WHERE id = :i"), {"i": rid, "p": json.dumps({
            "mailing_recovery_outcome": "error", "mailing_recovery_attempts": 5,
            "mailing_lookup_deferred": False})})
    await db.commit()

    assert (await asyncio.to_thread(_repair, tmp_path, apply=True))["candidates"] == 0
    assert (await _ed(db, rid))[rp.MARKER] == first                   # evidence never overwritten


@pytest.mark.parametrize("change", [
    "UPDATE results SET mailing_address = 'PO BOX 9, SEQUIM, WA 98382' WHERE id = :i",
    "UPDATE results SET parcel_id = '0630115504310000' WHERE id = :i",
    "UPDATE results SET enrichment_data = (enrichment_data::jsonb "
    "|| '{\"mailing_recovery_attempts\": 6}'::jsonb)::json WHERE id = :i",
    "UPDATE results SET enrichment_data = (enrichment_data::jsonb "
    "|| '{\"mailing_lookup_deferred\": true}'::jsonb)::json WHERE id = :i",
], ids=["mailing-filled", "parcel-changed", "attempts-changed", "re-deferred"])
async def test_a_row_changed_between_read_and_write_is_left_alone(db, business_user, tmp_path, change):
    rid = await _row(db, business_user, "clallam", _GAVE_UP)

    def _read_change_write():
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            rows = rp.select_candidates(sdb)
            sdb.execute(text(change), {"i": rid})
            sdb.commit()
            stats = {"repaired": 0, "skipped_changed": 0}
            rp.apply_rows(sdb, rows, stats)
            return stats

    assert await asyncio.to_thread(_read_change_write) == {"repaired": 0, "skipped_changed": 1}
    assert rp.MARKER not in await _ed(db, rid)


async def test_apply_refuses_without_the_fix_or_while_a_sweep_holds_the_lock(
        db, business_user, tmp_path, redis_client, monkeypatch):
    from src.workers import mailing_recovery as mr

    rid = await _row(db, business_user, "clallam", _GAVE_UP)
    before = await _ed(db, rid)
    report = str(tmp_path / "evidence.jsonl")

    assert await asyncio.to_thread(rp.main, ["--apply", "--report", report]) == 2
    # An option-shaped value reaches the script this way; the hex check must refuse it.
    assert await asyncio.to_thread(rp.main, ["--apply", "--deployed-commit=--output=x",
                                             "--report", report]) == 2
    monkeypatch.setattr(rp, "commit_has_fix", lambda sha, counties=(): True)
    redis_client.set(mr._LOCK_KEY, "a sweep tick", ex=60)
    try:
        assert await asyncio.to_thread(rp.main, ["--apply", "--deployed-commit", "abc1234",
                                                 "--report", report]) == 3
    finally:
        redis_client.delete(mr._LOCK_KEY)
    assert await _ed(db, rid) == before

    assert await asyncio.to_thread(rp.main, ["--apply", "--deployed-commit", "abc1234",
                                             "--report", report]) == 0
    assert (await _ed(db, rid))[rp.MARKER]["cohort"] == "A"
    assert redis_client.get(mr._LOCK_KEY) is None                     # released


def test_the_deployed_commit_check_reads_the_real_history():
    import shutil
    import subprocess

    head = subprocess.run([shutil.which("git"), "-C", str(rp.REPO), "rev-parse", "HEAD"],  # noqa: S603
                          capture_output=True, text=True, check=True).stdout.strip()
    assert rp.commit_has_fix(head) is True                             # this branch carries #575
    assert rp.commit_has_fix("d3d0d08e") is False                      # main before #575
    assert rp.commit_has_fix("HEAD") is False                          # refs are refused: hex only
    assert rp.commit_has_fix("not-a-sha") is False
    assert rp.commit_has_fix(head, ("clallam",)) is True


@pytest.mark.parametrize("shape", range(len(rp._CLALLAM_FIX_SHAPES)))
def test_code_without_the_clallam_fix_is_refused_only_for_runs_that_include_clallam(shape):
    # CI checks out shallow, so "main before the fix" is built from this tree's own source
    # with one piece of the Clallam fix taken out, not read from an older commit.
    src = (rp.REPO / "src/scrapers/enrichment/pacs_parcel.py").read_text(encoding="utf-8")
    assert rp.source_has_fix(src) is True
    before = rp._CLALLAM_FIX_SHAPES[shape].sub("", src)
    assert rp.source_has_fix(before, ("jefferson",)) is True
    assert rp.source_has_fix(before, ("clallam",)) is False
    assert rp.source_has_fix(before, ("jefferson", "clallam")) is False
    assert rp.source_has_fix(before) is False                         # the default run
    assert rp.source_has_fix(src.replace("search_type_field", ""), ("jefferson",)) is False


@pytest.mark.parametrize(("argv", "expected"), [
    (["--county", "jefferson"], ("jefferson",)),
    (["--county", "clallam", "--county", "jefferson"], ("clallam", "jefferson")),
    ([], rp.RETRY_COUNTIES),
])
def test_apply_checks_the_fix_for_the_counties_it_would_repair(tmp_path, monkeypatch, argv, expected):
    asked: list = []
    monkeypatch.setattr(rp, "commit_has_fix", lambda sha, counties=(): asked.append(counties) or False)

    assert rp.main(["--apply", "--deployed-commit", "abc1234", "--report",
                    str(tmp_path / "e.jsonl"), *argv]) == 2
    assert asked == [expected]


async def test_attempts_stored_as_a_string_are_still_the_ceiling(db, business_user, tmp_path):
    rid = await _row(db, business_user, "grant", {**_GAVE_UP, "mailing_recovery_attempts": "5"})

    assert (await asyncio.to_thread(_repair, tmp_path, apply=True))["repaired"] == 1
    assert (await _ed(db, rid))[rp.MARKER]["previous_attempts"] == "5"   # the stored JSON value kept


# ── the lease: owned, renewed on time, fails closed ───────────────────────────

def _redis():
    import redis as sync_redis

    from src.config import settings

    return sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())


def _lease(client):
    from src.workers import mailing_recovery as mr

    return rp.Lease(client, mr._LOCK_KEY, mr._LOCK_TTL_S)


async def test_a_lease_lost_before_the_first_write_writes_nothing(db, business_user, monkeypatch):
    from src.workers import mailing_recovery as mr

    rid = await _row(db, business_user, "clallam", _GAVE_UP)
    before = await _ed(db, rid)
    monkeypatch.setattr(rp, "_RENEW_EVERY_S", 0.0)                       # every check renews

    def _run():
        from src.db.session import system_sync_session

        client = _redis()
        lease = _lease(client)
        assert lease.acquire()
        try:
            with system_sync_session() as sdb:
                rows = rp.select_candidates(sdb)
                client.set(mr._LOCK_KEY, "a sweep tick took it", ex=60)   # ours expired meanwhile
                with pytest.raises(rp.LeaseLostError):
                    rp.apply_rows(sdb, rows, {"repaired": 0, "skipped_changed": 0}, lease=lease)
            assert client.get(mr._LOCK_KEY) in (b"a sweep tick took it", "a sweep tick took it")
            lease.release()                                              # never deletes theirs
            assert client.get(mr._LOCK_KEY) in (b"a sweep tick took it", "a sweep tick took it")
        finally:
            client.delete(mr._LOCK_KEY)

    await asyncio.to_thread(_run)
    assert await _ed(db, rid) == before


def test_a_renewal_redis_cannot_answer_fails_closed(monkeypatch):
    class _Down:
        def set(self, *a, **k):
            return True

        def eval(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(rp, "_RENEW_EVERY_S", 0.0)
    lease = _lease(_Down())
    assert lease.acquire()
    with pytest.raises(rp.LeaseLostError):
        lease.check()


def test_a_held_lease_renews_and_releases_only_itself(redis_client, monkeypatch):
    from src.workers import mailing_recovery as mr

    monkeypatch.setattr(rp, "_RENEW_EVERY_S", 0.0)
    client = _redis()
    lease = _lease(client)
    try:
        assert lease.acquire()
        assert not _lease(client).acquire()                              # a second holder is refused
        lease.check()                                                    # renews: still ours
        assert client.ttl(mr._LOCK_KEY) > mr._LOCK_TTL_S - 5
        lease.release()
        assert client.get(mr._LOCK_KEY) is None
    finally:
        client.delete(mr._LOCK_KEY)


async def test_apply_refuses_when_redis_is_unreachable(db, business_user, tmp_path, monkeypatch):
    import redis as sync_redis

    rid = await _row(db, business_user, "clallam", _GAVE_UP)
    before = await _ed(db, rid)
    monkeypatch.setattr(rp, "commit_has_fix", lambda sha, counties=(): True)

    def _unreachable(*a, **k):
        raise ConnectionError("no route to redis")

    monkeypatch.setattr(sync_redis, "from_url", _unreachable)

    assert await asyncio.to_thread(rp.main, ["--apply", "--deployed-commit", "abc1234",
                                             "--report", str(tmp_path / "e.jsonl")]) == 3
    assert await _ed(db, rid) == before


async def test_ownership_is_proven_before_every_write_not_only_at_renewal(db, business_user):
    """Production interval (no renewal due): a holder replaced between rows stops the next write."""
    from src.workers import mailing_recovery as mr

    assert rp._RENEW_EVERY_S == 60.0
    first = await _row(db, business_user, "clallam", _GAVE_UP)
    second = await _row(db, business_user, "jefferson", _GAVE_UP)

    def _run():
        from src.db.session import system_sync_session

        client = _redis()
        lease = _lease(client)
        assert lease.acquire()
        try:
            with system_sync_session() as sdb:
                rows = rp.select_candidates(sdb)
                stats = {"repaired": 0, "skipped_changed": 0}
                rp.apply_rows(sdb, rows[:1], stats, lease=lease)          # one write while ours
                client.set(mr._LOCK_KEY, "a sweep tick took it", ex=60)   # Redis lost it; a tick took it
                with pytest.raises(rp.LeaseLostError):
                    rp.apply_rows(sdb, rows[1:], stats, lease=lease)
                return stats, [str(r.id) for r in rows]
        finally:
            client.delete(mr._LOCK_KEY)

    stats, order = await asyncio.to_thread(_run)
    assert stats == {"repaired": 1, "skipped_changed": 0}
    written, held_back = order[0], order[1]
    assert rp.MARKER in await _ed(db, written)
    assert rp.MARKER not in await _ed(db, held_back)
    assert {written, held_back} == {first, second}


async def test_a_county_filter_repairs_only_that_county(db, business_user, tmp_path):
    jeff = await _row(db, business_user, "jefferson", _GAVE_UP, parcel="901013015")
    clallam = await _row(db, business_user, "clallam", _GAVE_UP)
    island = await _row(db, business_user, "island", _FALSE_NOT_FOUND, parcel=_ISLAND_ID)
    before = {clallam: await _ed(db, clallam), island: await _ed(db, island)}

    stats = await asyncio.to_thread(_repair, tmp_path, apply=True, counties=("jefferson",))

    assert stats["candidates"] == 1 and stats["repaired"] == 1
    assert (await _ed(db, jeff))[rp.MARKER]["cohort"] == "A"
    assert {clallam: await _ed(db, clallam), island: await _ed(db, island)} == before

    stats = await asyncio.to_thread(_repair, tmp_path, apply=False, counties=("jefferson", "island"))
    assert stats["by_cohort"] == {"B": 1}                   # jefferson is done; clallam still excluded


async def test_the_county_option_reaches_the_selection(db, business_user, tmp_path):
    await _row(db, business_user, "jefferson", _GAVE_UP, parcel="901013015")
    await _row(db, business_user, "clallam", _GAVE_UP)
    report = tmp_path / "cli.jsonl"

    code = await asyncio.to_thread(
        rp.main, ["--county", "jefferson", "--county", "jefferson", "--report", str(report)])

    assert code == 0
    lines = [json.loads(x) for x in report.read_text(encoding="utf-8").splitlines()]
    assert [x["county"] for x in lines] == ["jefferson"]
    with pytest.raises(SystemExit):
        rp.main(["--county", "benton"])                     # its lookups worked: not a repair county
