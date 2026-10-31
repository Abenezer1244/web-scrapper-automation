"""A live King job settles a PIN King does not carry instead of deferring it to the sweep.

Before: the job marked such a lead `mailing_lookup_deferred` (and later
`property_lookup_deferred`), the mailing sweep settled it a tick later with one more
King request, and a tax job's owner-only pass asked King about it again. Now the job
settles it itself on the sweep's three proofs (king_county_assessor.confirm_not_carried),
only for a parcel nothing in the job contradicts, all or nothing per parcel, and only
after the deferred markers committed.

Real DB and real enrichment code. Only the county boundary is substituted: the
eRealProperty and King GIS HTTP responses, the shared source lease, and the Assessor
extract download (the extract itself is a real zip in a real worker cache).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment import king_county_assessor as kca
from src.scrapers.enrichment import king_rpacct as kr
from src.scrapers.enrichment.source_health import KING_EREALPROPERTY
from tests.test_king_no_record_page import NO_RECORD_PAGE, _Resp
from tests.test_king_owner_lookup_state import _enrich, _lease, _logs
from tests.test_mailing_recovery_no_record import _OTHER_ACCOUNT, PIN, SNAPSHOT, _gis, _use_extract

pytestmark = pytest.mark.asyncio

_OTHER_PIN = "9999990002"


@pytest.fixture(autouse=True)
def _clean_source_health():
    from src.db.session import SyncSessionLocal

    def _wipe():
        with SyncSessionLocal() as sdb:
            sdb.execute(text("DELETE FROM external_source_health WHERE source_key = :k"),
                        {"k": KING_EREALPROPERTY})
            sdb.commit()

    _wipe()
    yield
    _wipe()


async def _job(db, user: User, *, status: str = "enriching") -> str:
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King live not carried",
                           county="king", state="WA", record_type="tax_delinquent",
                           fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
                           deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    return job_id


async def _row(db, user: User, job_id: str, parcel: str = PIN, *, mailing: str | None = None,
               extra: dict | None = None, deferred: bool = False) -> str:
    rid = str(uuid.uuid4())
    ed = {"source": "king_county_delinquent_taxes", "delinquent_amount": "7066.18", **(extra or {})}
    if deferred:
        ed["mailing_lookup_deferred"] = True
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name=None, parcel_id=parcel,
                  legal_description=parcel, mailing_address=mailing, is_duplicate=False,
                  skip_trace_status="not_attempted", enrichment_data=ed))
    await db.commit()
    return rid


async def _ed(db, rid: str) -> dict:
    return (await db.execute(text("SELECT enrichment_data FROM results WHERE id = :i"), {"i": rid})).scalar()


def _settled(ed: dict) -> bool:
    return (ed.get("mailing_recovery_outcome") == "parcel_not_found"
            and ed.get("mailing_lookup_deferred") is False
            and ed.get("mailing_source") == kca.NOT_CARRIED_SOURCE
            and ed.get("property_lookup_outcome") == "parcel_not_found")


# ── the whole job, real page pass ────────────────────────────────────────────

def _erealproperty(monkeypatch) -> list[str]:
    asked: list[str] = []

    def _get(url, **_k):
        asked.append(url.rsplit("=", 1)[-1])
        return _Resp(200, NO_RECORD_PAGE)

    monkeypatch.setattr(kca, "safe_get", _get)
    monkeypatch.setattr(kca, "record_source_blocked", lambda *_a, **_k: None)
    return asked


async def test_a_job_settles_a_dead_pin_and_never_asks_king_about_it_again(
        db, business_user, redis_client, monkeypatch, tmp_path):
    _lease(monkeypatch, admitted=True)
    job_id = await _job(db, business_user)
    a = await _row(db, business_user, job_id)
    b = await _row(db, business_user, job_id)                 # a second lead on the same PIN
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    _gis(monkeypatch)
    asked = _erealproperty(monkeypatch)

    summary = await asyncio.to_thread(_enrich, job_id, redis_client)

    for rid in (a, b):
        ed = await _ed(db, rid)
        assert _settled(ed), ed
        assert ed["property_lookup_deferred"] is False         # the later property marker left it
        assert ed["mailing_rpacct_snapshot"] == SNAPSHOT
        assert ed["delinquent_amount"] == "7066.18"             # unrelated keys survive
        assert "owner_lookup_outcome" not in ed                 # no owner answer is invented
        assert "mailing_recovery_attempts" not in ed            # no recovery attempt was made
    assert asked.count(PIN) == 1                                # the owner-only pass did not re-ask
    logs = await _logs(db, job_id)
    assert "King County sources did not find the parcel IDs listed on 2 leads." in logs
    assert not any(m.startswith("Mailing addresses are still being looked up") for m in logs), logs
    assert not summary.get("mailing_deferred")


async def test_a_mailed_sibling_on_the_same_pin_keeps_it_deferred(
        db, business_user, redis_client, monkeypatch, tmp_path):
    _lease(monkeypatch, admitted=True)
    job_id = await _job(db, business_user)
    dead = await _row(db, business_user, job_id)
    await _row(db, business_user, job_id, mailing="PO BOX 1, KENT, WA 98032")
    _use_extract(monkeypatch, tmp_path, [_OTHER_ACCOUNT])
    _gis(monkeypatch)
    _erealproperty(monkeypatch)

    await asyncio.to_thread(_enrich, job_id, redis_client)

    ed = await _ed(db, dead)
    assert ed["mailing_lookup_deferred"] is True and "mailing_recovery_outcome" not in ed


# ── the helper's guards (rows already carrying the committed deferred marker) ─

def _settle(job_id: str, user_id: str, *, pids=frozenset({PIN}), answers=None, hidden=frozenset(),
            time_left=lambda: 1000.0, break_commit: bool = False) -> set:
    from src.db.session import SyncSessionLocal
    from src.workers.tasks_helpers.enrich import _settle_king_not_carried

    with SyncSessionLocal() as sdb:
        rows = sdb.query(Result).filter(Result.job_id == job_id).all()
        hidden_ids = {r.id for r in rows if str(r.id) in hidden}
        if break_commit:
            def _boom():
                raise RuntimeError("commit refused")
            sdb.commit = _boom
        extract = {"answers": answers if answers is not None else {p: kr.Answer("absent") for p in pids},
                   "snapshot": SNAPSHOT}
        settled = _settle_king_not_carried(sdb, rows, hidden_ids, set(pids), extract, time_left,
                                           job_id, user_id)
        if not break_commit:
            # The ORM copies were expired: they now read what the database holds.
            for r in rows:
                if str(r.id) in settled:
                    assert _settled(r.enrichment_data)
        return settled


async def test_the_helper_settles_every_row_of_an_uncontradicted_pin(db, business_user, monkeypatch):
    job_id = await _job(db, business_user)
    a = await _row(db, business_user, job_id, deferred=True)
    b = await _row(db, business_user, job_id, deferred=True)
    gis = _gis(monkeypatch)

    settled = await asyncio.to_thread(_settle, job_id, str(business_user.id))

    assert settled == {a, b} and len(gis) == 1
    assert _settled(await _ed(db, a)) and _settled(await _ed(db, b))


@pytest.mark.parametrize("sibling", [
    {"mailing": "PO BOX 1, KENT, WA 98032"},
    {"extra": {"resolved_parcel_id": "1234567890", "resolved_by": "gis_plus_owner_match"}},
    {"extra": {"source_parcel_id": "99999900012"}},
    {"extra": {"vacant_no_situs": True}},
    {"extra": {"condo_unit_status": "no_locality"}},
    {"extra": {"property_source": "king_condo_unit"}},
    {"hidden": True},
], ids=["mailed", "resolved", "source-parcel", "vacant-gis-match", "condo-unit", "property-source",
        "hidden"])
async def test_anything_on_any_sibling_that_contradicts_keeps_the_pin_deferred(
        db, business_user, monkeypatch, sibling):
    job_id = await _job(db, business_user)
    dead = await _row(db, business_user, job_id, deferred=True)
    other = await _row(db, business_user, job_id, deferred=True, mailing=sibling.get("mailing"),
                       extra=sibling.get("extra"))
    gis = _gis(monkeypatch)

    settled = await asyncio.to_thread(
        _settle, job_id, str(business_user.id), hidden=frozenset({other}) if sibling.get("hidden") else frozenset())

    assert settled == set() and gis == []
    assert (await _ed(db, dead))["mailing_lookup_deferred"] is True


@pytest.mark.parametrize("case", ["no-extract", "extract-lists-it", "gis-carries-it", "non-ascii-digits"])
async def test_a_missing_proof_keeps_the_pin_deferred(db, business_user, monkeypatch, case):
    job_id = await _job(db, business_user)
    parcel = "٩٩٩٩٩٩٠٠٠١" if case == "non-ascii-digits" else PIN    # Arabic-Indic digits
    dead = await _row(db, business_user, job_id, parcel, deferred=True)
    _gis(monkeypatch, body={"features": [{"attributes": {
        "PIN": PIN, "ADDR_FULL": None, "POSTALCTYNAME": "SEATTLE", "STATE_ABBR": "WA", "ZIP5": "98101",
    }}]} if case == "gis-carries-it" else None)
    answers = {"no-extract": None, "extract-lists-it": {PIN: kr.Answer("no_address")}}.get(
        case, {parcel: kr.Answer("absent")})

    if case == "no-extract":
        from src.db.session import SyncSessionLocal
        from src.workers.tasks_helpers.enrich import _settle_king_not_carried

        def _call():
            with SyncSessionLocal() as sdb:
                rows = sdb.query(Result).filter(Result.job_id == job_id).all()
                return _settle_king_not_carried(sdb, rows, set(), {PIN}, {}, lambda: 1000.0,
                                                job_id, str(business_user.id))
        settled = await asyncio.to_thread(_call)
    else:
        settled = await asyncio.to_thread(_settle, job_id, str(business_user.id),
                                          pids=frozenset({parcel}), answers=answers)

    assert settled == set()
    assert (await _ed(db, dead))["mailing_lookup_deferred"] is True


async def test_with_no_time_left_gis_is_never_asked(db, business_user, monkeypatch):
    job_id = await _job(db, business_user)
    await _row(db, business_user, job_id, deferred=True)
    gis = _gis(monkeypatch)

    settled = await asyncio.to_thread(_settle, job_id, str(business_user.id), time_left=lambda: 44.0)

    assert settled == set() and gis == []


async def test_a_clock_crossing_the_floor_stops_after_the_first_batch_of_50(db, business_user, monkeypatch):
    job_id = await _job(db, business_user)
    pins = [f"99999{i:05d}" for i in range(51)]
    for pin in pins:
        await _row(db, business_user, job_id, pin, deferred=True)
    gis = _gis(monkeypatch)
    clock = iter([1000.0, 10.0])

    settled = await asyncio.to_thread(_settle, job_id, str(business_user.id), pids=frozenset(pins),
                                      time_left=lambda: next(clock))

    assert len(gis) == 1 and len(settled) == 50


async def test_a_row_changed_meanwhile_rolls_the_whole_pin_back(db, business_user, monkeypatch):
    """All or nothing: one row failing its guard must not leave its sibling settled."""
    from src.db.session import SyncSessionLocal

    job_id = await _job(db, business_user)
    a = await _row(db, business_user, job_id, deferred=True)
    b = await _row(db, business_user, job_id, deferred=True)

    def _gis_answer_while_another_writer_settles_b(url, params=None, **_k):
        with SyncSessionLocal() as other:            # e.g. the mailing sweep, mid-proof
            other.execute(text("UPDATE results SET enrichment_data = (enrichment_data::jsonb "
                               "|| '{\"mailing_lookup_deferred\": false}'::jsonb)::json WHERE id = :i"),
                          {"i": b})
            other.commit()

        class _Ok:
            status_code = 200

            def json(self):
                return {"features": []}
        return _Ok()

    monkeypatch.setattr("src.scrapers.enrichment.county_gis.safe_get", _gis_answer_while_another_writer_settles_b)

    settled = await asyncio.to_thread(_settle, job_id, str(business_user.id))

    assert settled == set()
    ed_a = await _ed(db, a)
    assert ed_a["mailing_lookup_deferred"] is True and "mailing_recovery_outcome" not in ed_a


async def test_a_failed_settlement_commit_reports_nothing_and_keeps_the_markers(db, business_user, monkeypatch):
    job_id = await _job(db, business_user)
    a = await _row(db, business_user, job_id, deferred=True)
    _gis(monkeypatch)

    settled = await asyncio.to_thread(_settle, job_id, str(business_user.id), break_commit=True)

    assert settled == set()
    ed = await _ed(db, a)
    assert ed["mailing_lookup_deferred"] is True and "mailing_recovery_outcome" not in ed


# ── owner recovery leaves settled rows alone ─────────────────────────────────

def test_owner_recovery_names_the_same_source_as_the_settlement():
    from src.workers import owner_recovery as orc

    assert f"= '{kca.NOT_CARRIED_SOURCE}'" in orc._ELIGIBLE_ROW


async def test_owner_recovery_never_asks_about_a_settled_dead_pin(db, business_user, monkeypatch):
    from src.config import settings
    from src.workers import owner_recovery as orc

    monkeypatch.setattr(orc, "_PACE_S", 0.0)
    monkeypatch.setattr(settings, "OWNER_RECOVERY_ENABLED", True, raising=False)
    _lease(monkeypatch, admitted=True)
    job_id = await _job(db, business_user, status="done")
    await _row(db, business_user, job_id, extra={
        "owner_lookup_deferred": True, "mailing_recovery_outcome": "parcel_not_found",
        "mailing_source": kca.NOT_CARRIED_SOURCE})
    live = await _row(db, business_user, job_id, _OTHER_PIN, extra={"owner_lookup_deferred": True})
    asked = _erealproperty(monkeypatch)

    await asyncio.to_thread(orc.recover_deferred_king_owners)

    assert PIN not in asked and _OTHER_PIN in asked           # the control lead is still asked
    assert (await _ed(db, live)).get("owner_recovery_attempts") == 1
