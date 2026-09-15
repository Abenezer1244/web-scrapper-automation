"""Background owner names for King code-violation leads (Seattle, Bellevue, Burien, Accela).

A King code-violation job names owners inside a 240 s budget; rows it did not reach
stayed unnamed forever. This sweep is the second look.

Contract pinned here: delivered leads only, on a parcel we can prove (SDCI located at a
SHOWN tier, the same rule as src/utils/located_parcel.py; Bellevue, Burien and King County
Accela by the parcel_id they printed, never a kc_pin), one lookup per parcel;
a name is filled only onto a row that is still blank, still on the same parcel and
still delivered; parcel_id, dedup_hash, property_key, mailing, skip trace, billing
and quota never move; an attempt is charged only when King was actually asked; the
kill switch and the Redis lock stop it before any request.

Real DB, real Redis, real sweep. Only King's HTTP response and the shared source
lease are substituted.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment import king_county_assessor as kca
from src.scrapers.enrichment.source_health import KING_EREALPROPERTY
from src.workers import cv_owner_recovery as cvr

pytestmark = pytest.mark.asyncio


def _page(pid: str, owner: str | None, echo: str | None = None) -> str:
    shown = echo or pid
    return (
        f'<tr><td style="font-weight:bold;">Parcel Number</td><td>{shown[:6]}-{shown[6:]}</td></tr>'
        f'<tr><td style="font-weight:bold;">Name</td><td>{owner if owner else "&nbsp;"}</td></tr>'
    )


class _Resp:
    def __init__(self, status: int, body: str = ""):
        self.status_code = status
        self.text = body
        self.url = "https://blue.kingcounty.com/x"
        self.headers: dict = {}


def _lease(monkeypatch, *, admitted: bool = True) -> None:
    class _Lease:
        def __init__(self, *a, **k):
            self.admitted = admitted

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def still_held(self):
            return admitted

    monkeypatch.setattr("src.scrapers.enrichment.source_admission.SourceAdmission", _Lease)


def _county(monkeypatch, answers: dict[str, _Resp]) -> list[str]:
    asked: list[str] = []

    def _get(url, **_k):
        pid = url.rsplit("=", 1)[-1]
        asked.append(pid)
        return answers[pid]

    monkeypatch.setattr(kca, "safe_get", _get)
    return asked


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    from src.config import settings
    from src.db.session import SyncSessionLocal

    monkeypatch.setattr(cvr, "_PACE_S", 0.0)
    monkeypatch.setattr(settings, "OWNER_RECOVERY_ENABLED", True, raising=False)

    def _wipe():
        with SyncSessionLocal() as sdb:
            sdb.execute(text("DELETE FROM external_source_health WHERE source_key = :k"),
                        {"k": KING_EREALPROPERTY})
            sdb.commit()

    _wipe()
    yield
    _wipe()


async def _job(db, user: User, *, county: str = "king", record_type: str = "code_violation",
               status: str = "done") -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="cv owner recovery",
        county=county, state="WA", record_type=record_type,
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    return job_id


def _located(pin: str, match: str = "exact", **extra) -> dict:
    return {"source": "seattle_sdci_code_violations", "record_number": f"CP-{pin[-4:]}",
            "kc_pin": pin, "kc_pin_status": "matched", "kc_pin_source": "king_gis_point_in_parcel",
            "kc_pin_match": match, "latitude": "47.67934", "longitude": "-122.31749", **extra}


async def _row(db, user: User, job_id: str, *, pin: str, match: str = "exact",
               party: str | None = None, duplicate: bool = False, date: str = "08/23/2026",
               ed: dict | None = None, **extra) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(
        id=rid, user_id=user.id, job_id=job_id, parcel_id=None, party_name=party,
        date_recorded=date, property_address="7011 ROOSEVELT WAY NE, SEATTLE, WA 98115",
        mailing_address=None, is_duplicate=duplicate,
        duplicate_reason="prior_run" if duplicate else None,
        dedup_hash=uuid.uuid4().hex, property_key=uuid.uuid4().hex,
        skip_trace_status="not_attempted",
        enrichment_data=ed if ed is not None else _located(pin, match, **extra),
    ))
    await db.commit()
    return rid


async def _get(db, rid: str):
    return (await db.execute(text(
        "SELECT party_name, enrichment_data, skip_trace_status, phone, parcel_id, dedup_hash, "
        "property_key, mailing_address FROM results WHERE id = :i"), {"i": rid})).first()


def _tick() -> dict:
    return cvr.recover_code_violation_owners()


async def test_exact_and_street_level_leads_get_their_owner_and_nothing_else_moves(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    exact = await _row(db, business_user, job_id, pin="9138100481")
    street = await _row(db, business_user, job_id, pin="5249802770", match="street_only")
    _county(monkeypatch, {
        "9138100481": _Resp(200, _page("9138100481", "7011 ROOSEVELT WAY NE LLC")),
        "5249802770": _Resp(200, _page("5249802770", "NGUYEN THANH")),
    })
    before = {rid: await _get(db, rid) for rid in (exact, street)}
    job_before = (await db.execute(text(
        "SELECT billed_count, billing_applied_at, reserved_count, status FROM jobs WHERE id = :j"),
        {"j": job_id})).first()
    used_before = (await db.execute(text("SELECT records_used FROM users WHERE id = :u"),
                                    {"u": business_user.id})).scalar()

    stats = await asyncio.to_thread(_tick)

    assert stats["found"] == 2 and stats["parcels"] == 2
    for rid, pin, name in ((exact, "9138100481", "7011 ROOSEVELT WAY NE LLC"),
                           (street, "5249802770", "NGUYEN THANH")):
        row = await _get(db, rid)
        assert row.party_name == name
        ed = row.enrichment_data
        assert (ed["owner_source"], ed["owner_pin"]) == ("king_erealproperty", pin)
        assert ed["owner_checked_at"] and ed["cv_owner_recovery_last_at"]
        assert ed["cv_owner_recovery_outcome"] == "found" and ed["cv_owner_recovery_attempts"] == 1
        old = before[rid]
        assert (row.parcel_id, row.dedup_hash, row.property_key, row.mailing_address,
                row.skip_trace_status, row.phone) == (
            old.parcel_id, old.dedup_hash, old.property_key, old.mailing_address,
            old.skip_trace_status, old.phone)
        assert row.parcel_id is None
        assert ed["kc_pin"] == pin
    assert tuple((await db.execute(text(
        "SELECT billed_count, billing_applied_at, reserved_count, status FROM jobs WHERE id = :j"),
        {"j": job_id})).first()) == tuple(job_before)
    assert (await db.execute(text("SELECT records_used FROM users WHERE id = :u"),
                             {"u": business_user.id})).scalar() == used_before


async def test_a_county_address_match_is_named_and_an_address_only_candidate_never_is(
    db, business_user, monkeypatch,
):
    """address_point is a shown tier (src/utils/located_parcel.py); address_only, a condo
    complex and a tier stamped by the wrong rule are not, so they are never asked."""
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    ap = {"kc_pin_source": "king_gis_address_point"}
    shown = await _row(db, business_user, job_id, pin="7899800716", match="address_point", **ap)
    hidden = await _row(db, business_user, job_id, pin="5318100580", match="address_only", **ap)
    condo = await _row(db, business_user, job_id, pin="8562990000", match="condo_complex", **ap)
    wrong_rule = await _row(db, business_user, job_id, pin="2770602445", match="address_point")
    asked = _county(monkeypatch, {
        "7899800716": _Resp(200, _page("7899800716", "NAMUE KATA & ISABELLA MONGI")),
    })
    before = {rid: await _get(db, rid) for rid in (hidden, condo, wrong_rule)}

    stats = await asyncio.to_thread(_tick)

    assert asked == ["7899800716"] and stats["found"] == 1
    row = await _get(db, shown)
    assert row.party_name == "NAMUE KATA & ISABELLA MONGI"
    assert row.enrichment_data["owner_pin"] == "7899800716"
    for rid, old in before.items():
        assert tuple(await _get(db, rid)) == tuple(old)


async def test_leads_that_are_not_eligible_are_never_looked_up_or_written(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    king = await _job(db, business_user)
    ids = [
        await _row(db, business_user, king, pin="1000000001", match="condo_complex"),
        await _row(db, business_user, king, pin="1000000002", match="unconfirmed"),
        await _row(db, business_user, king, pin="1000000003",
                   ed={**_located("1000000003"), "kc_pin_status": "address_mismatch"}),
        await _row(db, business_user, king, pin="1000000004", duplicate=True),
        await _row(db, business_user, king, pin="1000000005",
                   delivery_excluded_reason="over_quota"),
        await _row(db, business_user, king, pin="1000000006", party="ALREADY NAMED"),
        await _row(db, business_user, king, pin="1000000007", owner_source="king_erealproperty"),
        await _row(db, business_user, king, pin="1000000008",
                   cv_owner_recovery_attempts=cvr._MAX_ATTEMPTS),
        await _row(db, business_user, king, pin="1000000009",
                   cv_owner_recovery_outcome="not_on_record"),
        await _row(db, business_user, king, pin="1000000010",
                   ed={**_located("1000000010"), "kc_pin_source": "something_else"}),
        await _row(db, business_user, king, pin="100000011",   # 9 digits
                   ed=_located("100000011")),
        await _row(db, business_user, king, pin="1000000012",
                   ed={**_located("1000000012"), "source": "tacoma_code_violations"}),
    ]
    live = await _job(db, business_user, status="enriching")
    ids.append(await _row(db, business_user, live, pin="1000000013"))
    tax = await _job(db, business_user, record_type="tax_delinquent")
    ids.append(await _row(db, business_user, tax, pin="1000000014"))
    pierce = await _job(db, business_user, county="pierce")
    ids.append(await _row(db, business_user, pierce, pin="1000000015"))
    before = {rid: (await _get(db, rid)).enrichment_data for rid in ids}

    stats = await asyncio.to_thread(_tick)

    assert asked == [] and stats["parcels"] == 0
    for rid in ids:
        assert (await _get(db, rid)).enrichment_data == before[rid]


def _printed(source: str, case: str, **extra) -> dict:
    return {"source": source, "case_number": case, "status": "Open", **extra}


async def _printed_row(db, user: User, job_id: str, *, source: str, parcel_id: str | None,
                       ed_extra: dict | None = None, party: str | None = None) -> str:
    rid = await _row(db, user, job_id, pin=parcel_id or "0000000000", party=party,
                     ed=_printed(source, f"CASE-{uuid.uuid4().hex[:6]}", **(ed_extra or {})))
    await db.execute(text("UPDATE results SET parcel_id = :p WHERE id = :i"),
                     {"p": parcel_id, "i": rid})
    await db.commit()
    return rid


async def test_printed_pin_leads_are_named_for_their_parcel_id_and_nothing_else_moves(
    db, business_user, monkeypatch,
):
    # Bellevue, Burien and King County Accela print the King PIN into parcel_id at scrape.
    # Their owner is looked up for that parcel_id, and owner_pin is that parcel_id: the
    # proof skip trace requires. A located kc_pin block on such a row is never read.
    from src.scrapers.king_cv_sources import PARCEL_AT_SCRAPE_SOURCES

    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rows = {}
    answers = {}
    for n, source in enumerate(sorted(PARCEL_AT_SCRAPE_SOURCES)):
        pin, decoy = f"30000000{n:02d}", f"39000000{n:02d}"
        rows[source] = (pin, await _printed_row(
            db, business_user, job_id, source=source, parcel_id=pin,
            ed_extra={"kc_pin": decoy, "kc_pin_status": "matched",
                      "kc_pin_source": "king_gis_point_in_parcel", "kc_pin_match": "exact"}))
        answers[pin] = _Resp(200, _page(pin, f"OWNER {n} LLC"))
    asked = _county(monkeypatch, answers)
    before = {rid: await _get(db, rid) for _, rid in rows.values()}

    stats = await asyncio.to_thread(_tick)

    assert sorted(asked) == sorted(answers) and stats["found"] == len(rows)
    for n, source in enumerate(sorted(PARCEL_AT_SCRAPE_SOURCES)):
        pin, rid = rows[source]
        row = await _get(db, rid)
        assert row.party_name == f"OWNER {n} LLC"
        ed = row.enrichment_data
        assert (ed["owner_source"], ed["owner_pin"]) == ("king_erealproperty", pin)
        assert ed["cv_owner_recovery_outcome"] == "found"
        old = before[rid]
        assert (row.parcel_id, row.dedup_hash, row.property_key, row.mailing_address,
                row.skip_trace_status, row.phone) == (
            old.parcel_id, old.dedup_hash, old.property_key, old.mailing_address,
            old.skip_trace_status, old.phone)


async def test_the_named_printed_pin_lead_passes_the_skip_trace_owner_proof(db, business_user, monkeypatch):
    from types import SimpleNamespace

    from src.scrapers.enrichment.skip_trace import code_violation_owner_is_known

    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rid = await _printed_row(db, business_user, job_id, source="burien_code_enforcement",
                             parcel_id="7835800148")
    _county(monkeypatch, {"7835800148": _Resp(200, _page("7835800148", "OVERLOOK AT BURIEN LLC"))})

    await asyncio.to_thread(_tick)

    row = await _get(db, rid)
    assert code_violation_owner_is_known(SimpleNamespace(
        party_name=row.party_name, parcel_id=row.parcel_id, enrichment_data=row.enrichment_data))


async def test_printed_pin_leads_without_a_provable_parcel_are_never_looked_up(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    job_id = await _job(db, business_user)
    bellevue = "bellevue_code_enforcement"
    ids = [
        await _printed_row(db, business_user, job_id, source=bellevue, parcel_id=None,
                           ed_extra={"kc_pin": "4000000001", "kc_pin_status": "matched",
                                     "kc_pin_source": "king_gis_point_in_parcel",
                                     "kc_pin_match": "exact"}),
        await _printed_row(db, business_user, job_id, source=bellevue, parcel_id="400000002"),
        await _printed_row(db, business_user, job_id, source=bellevue, parcel_id="40000-00003"),
        await _printed_row(db, business_user, job_id, source=bellevue, parcel_id="4000000004",
                           ed_extra={"owner_pin": "4000000099"}),
        await _printed_row(db, business_user, job_id, source=bellevue, parcel_id="4000000005",
                           party="ALREADY NAMED"),
        await _printed_row(db, business_user, job_id, source=bellevue, parcel_id="4000000006",
                           ed_extra={"owner_source": "king_erealproperty"}),
    ]
    before = {rid: tuple(await _get(db, rid)) for rid in ids}

    stats = await asyncio.to_thread(_tick)

    assert asked == [] and stats["parcels"] == 0
    for rid in ids:
        assert tuple(await _get(db, rid)) == before[rid]


async def test_a_printed_pin_lead_whose_parcel_changed_during_the_lookup_is_left_alone(
    db, business_user, monkeypatch,
):
    from types import SimpleNamespace

    from src.db.session import system_sync_session

    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rid = await _printed_row(db, business_user, job_id, source="kingco_accela_code_enforcement",
                             parcel_id="5000000001")

    def _get_and_reparcel(url, **_k):
        with system_sync_session() as sdb:
            sdb.execute(text("UPDATE results SET parcel_id = '5000000099' WHERE id = :i"), {"i": rid})
            sdb.commit()
        return _Resp(200, _page(url.rsplit("=", 1)[-1], "STALE ANSWER"))

    monkeypatch.setattr(kca, "safe_get", _get_and_reparcel)
    stats = await asyncio.to_thread(_tick)

    row = await _get(db, rid)
    assert row.party_name is None and "owner_source" not in row.enrichment_data
    assert stats["stale"] == 1 and stats["found"] == 0

    # The Python rule refuses the same write on its own: the row's parcel_id is not the PIN.
    stale = SimpleNamespace(id=rid, user_id=business_user.id, pin="5000000001",
                            parcel_id="5000000099", enrichment_data=row.enrichment_data)

    def _go():
        with system_sync_session() as sdb:
            return cvr._write(sdb, stale, "found", "SOMEONE")

    assert await asyncio.to_thread(_go) == "stale"


async def test_a_blank_party_name_is_treated_as_unnamed(db, business_user, monkeypatch):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, pin="9138100481", party="   ")
    _county(monkeypatch, {"9138100481": _Resp(200, _page("9138100481", "OWNER LLC"))})

    await asyncio.to_thread(_tick)

    assert (await _get(db, rid)).party_name == "OWNER LLC"


async def test_attempts_increment_on_failure_and_give_up_at_the_cap(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    down = await _row(db, business_user, job_id, pin="5379801941")
    last_try = await _row(db, business_user, job_id, pin="0040000055",
                          cv_owner_recovery_attempts=cvr._MAX_ATTEMPTS - 1)
    asked = _county(monkeypatch, {"5379801941": _Resp(503), "0040000055": _Resp(503)})

    stats = await asyncio.to_thread(_tick)

    row = await _get(db, down)
    assert row.party_name is None and "owner_source" not in row.enrichment_data
    assert row.enrichment_data["cv_owner_recovery_attempts"] == 1
    assert row.enrichment_data["cv_owner_recovery_outcome"] == "transient_failure"
    row = await _get(db, last_try)
    assert row.party_name is None
    assert row.enrichment_data["cv_owner_recovery_attempts"] == cvr._MAX_ATTEMPTS
    assert row.enrichment_data["cv_owner_recovery_outcome"] == "gave_up"
    assert stats["transient"] == 1 and stats["gave_up"] == 1

    # A given-up lead is never asked about again.
    asked.clear()
    await asyncio.to_thread(_tick)
    assert "0040000055" not in asked and "5379801941" in asked


async def test_a_page_that_names_nobody_or_another_parcel_settles_without_a_name(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    nobody = await _row(db, business_user, job_id, pin="2624069024")
    other = await _row(db, business_user, job_id, pin="6411600027")
    asked = _county(monkeypatch, {
        "2624069024": _Resp(200, _page("2624069024", None)),
        "6411600027": _Resp(200, _page("6411600027", "SNYDER JACOB", echo="6411600002")),
    })

    stats = await asyncio.to_thread(_tick)

    row = await _get(db, nobody)
    assert row.party_name is None and "owner_source" not in row.enrichment_data
    assert row.enrichment_data["cv_owner_recovery_outcome"] == "not_on_record"
    row = await _get(db, other)
    assert row.party_name is None and "owner_source" not in row.enrichment_data
    assert row.enrichment_data["cv_owner_recovery_outcome"] == "parcel_mismatch"
    assert stats["not_on_record"] == 1 and stats["parcel_mismatch"] == 1

    asked.clear()
    again = await asyncio.to_thread(_tick)
    assert asked == [] and again["parcels"] == 0


async def test_a_tripped_breaker_names_nobody_and_a_busy_source_charges_nothing(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    monkeypatch.setattr(kca.asyncio, "sleep", _no_wait)
    job_id = await _job(db, business_user)
    pins = [f"70000000{i:02d}" for i in range(25)]
    ids = [await _row(db, business_user, job_id, pin=p) for p in pins]
    _county(monkeypatch, {p: _Resp(503) for p in pins})

    stats = await asyncio.to_thread(_tick)

    assert stats["skipped"].startswith("stopped:")
    for rid in ids:
        row = await _get(db, rid)
        assert row.party_name is None and "owner_source" not in row.enrichment_data
    assert stats["found"] == 0

    # Lease denied: nobody asked, nobody charged, rotated to the back of the queue.
    from src.db.session import SyncSessionLocal

    with SyncSessionLocal() as sdb:
        sdb.execute(text("DELETE FROM external_source_health WHERE source_key = :k"),
                    {"k": KING_EREALPROPERTY})
        sdb.commit()
    _lease(monkeypatch, admitted=False)
    before = {rid: (await _get(db, rid)).enrichment_data.get("cv_owner_recovery_attempts")
              for rid in ids}
    asked = _county(monkeypatch, {})
    busy = await asyncio.to_thread(_tick)
    assert asked == [] and busy["unreached"] == len(ids)
    for rid in ids:
        ed = (await _get(db, rid)).enrichment_data
        assert ed.get("cv_owner_recovery_attempts") == before[rid]
        assert "cv_owner_recovery_last_at" in ed


async def _no_wait(_s):
    return None


@pytest.mark.parametrize("change", ["party_name", "kc_pin", "kc_pin_match", "job_status"])
async def test_a_lead_that_changed_during_the_lookup_is_left_alone(
    db, business_user, monkeypatch, change,
):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, pin="1000000031")
    statements = {
        "party_name": ("UPDATE results SET party_name = 'FOUND BY A RE-RUN' WHERE id = :i", rid),
        "kc_pin": ("UPDATE results SET enrichment_data = (enrichment_data::jsonb || "
                   "'{\"kc_pin\": \"1000000099\"}'::jsonb)::json WHERE id = :i", rid),
        "kc_pin_match": ("UPDATE results SET enrichment_data = (enrichment_data::jsonb || "
                         "'{\"kc_pin_match\": \"condo_complex\"}'::jsonb)::json WHERE id = :i", rid),
        "job_status": ("UPDATE jobs SET status = 'enriching' WHERE id = :i", job_id),
    }

    def _get_and_change(url, **_k):
        from src.db.session import system_sync_session

        sql, target = statements[change]
        with system_sync_session() as sdb:
            sdb.execute(text(sql), {"i": target})
            sdb.commit()
        return _Resp(200, _page(url.rsplit("=", 1)[-1], "STALE ANSWER"))

    monkeypatch.setattr(kca, "safe_get", _get_and_change)
    stats = await asyncio.to_thread(_tick)

    row = await _get(db, rid)
    assert row.party_name != "STALE ANSWER"
    assert "owner_source" not in row.enrichment_data
    assert "cv_owner_recovery_outcome" not in row.enrichment_data
    assert stats["stale"] == 1 and stats["found"] == 0


async def test_python_located_parcel_rule_is_checked_before_a_lookup(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    job_id = await _job(db, business_user)
    # SQL-eligible row; the read-side rule is made to disagree, so only the Python
    # guard can stop the lookup.
    rid = await _row(db, business_user, job_id, pin="1000000041")
    monkeypatch.setattr(cvr, "located_parcel_id", lambda _ed, **_k: None)

    stats = await asyncio.to_thread(_tick)

    assert asked == [] and stats["parcels"] == 0
    assert "cv_owner_recovery_last_at" not in (await _get(db, rid)).enrichment_data


async def test_python_located_parcel_rule_is_checked_again_at_write(db, business_user, monkeypatch):
    from types import SimpleNamespace

    from src.db.session import system_sync_session

    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, pin="1000000042")
    row = SimpleNamespace(id=rid, user_id=business_user.id, pin="1000000042",
                          enrichment_data=_located("1000000042"))
    monkeypatch.setattr(cvr, "located_parcel_id", lambda _ed, **_k: "1000000099")

    def _go():
        with system_sync_session() as sdb:
            return cvr._write(sdb, row, "found", "SOMEONE")

    assert await asyncio.to_thread(_go) == "stale"
    assert (await _get(db, rid)).party_name is None


async def test_the_write_phase_stops_at_its_budget(db, business_user, monkeypatch):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, pin="1000000043")
    _county(monkeypatch, {"1000000043": _Resp(200, _page("1000000043", "LATE OWNER"))})
    monkeypatch.setattr(cvr, "_WRITE_BUDGET_S", 0.0)

    stats = await asyncio.to_thread(_tick)

    row = await _get(db, rid)
    assert row.party_name is None and "cv_owner_recovery_last_at" not in row.enrichment_data
    assert stats["skipped"] == "write budget exhausted" and stats["found"] == 0


@pytest.mark.parametrize("limit", ["soft", "hard"])
async def test_a_celery_time_limit_is_not_swallowed(db, business_user, monkeypatch, limit):
    from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded

    exc_type = SoftTimeLimitExceeded if limit == "soft" else TimeLimitExceeded
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, pin="1000000044")

    def _limit_hits_during_http(*_a, **_k):   # where the worker's signal lands
        raise exc_type()

    monkeypatch.setattr(kca, "safe_get", _limit_hits_during_http)
    with pytest.raises(exc_type):
        await asyncio.to_thread(_tick)

    assert "cv_owner_recovery_last_at" not in (await _get(db, rid)).enrichment_data
    assert isinstance(cvr._acquire_lock(), tuple)   # the lock was still released
    import redis as sync_redis

    from src.config import settings

    sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs()).delete(cvr._LOCK_KEY)


async def test_two_cases_on_one_parcel_share_one_lookup_newest_first(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    monkeypatch.setattr(cvr, "_BATCH_PARCELS", 1)
    job_id = await _job(db, business_user)
    old = await _row(db, business_user, job_id, pin="1000000051", date="01/05/2025")
    new_a = await _row(db, business_user, job_id, pin="1000000052", date="08/30/2026")
    new_b = await _row(db, business_user, job_id, pin="1000000052", date="08/01/2026")
    asked = _county(monkeypatch, {"1000000052": _Resp(200, _page("1000000052", "SHARED OWNER"))})

    stats = await asyncio.to_thread(_tick)

    assert asked == ["1000000052"] and stats["rows"] == 2
    assert (await _get(db, new_a)).party_name == "SHARED OWNER"
    assert (await _get(db, new_b)).party_name == "SHARED OWNER"
    assert (await _get(db, old)).party_name is None


async def test_an_sdci_lead_and_a_printed_pin_lead_on_one_parcel_share_one_lookup(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    sdci = await _row(db, business_user, job_id, pin="6000000001")
    bellevue = await _printed_row(db, business_user, job_id, source="bellevue_code_enforcement",
                                  parcel_id="6000000001")
    asked = _county(monkeypatch, {"6000000001": _Resp(200, _page("6000000001", "ONE OWNER LLC"))})

    stats = await asyncio.to_thread(_tick)

    assert asked == ["6000000001"] and stats["parcels"] == 1 and stats["rows"] == 2
    assert stats["found"] == 2
    for rid in (sdci, bellevue):
        row = await _get(db, rid)
        assert row.party_name == "ONE OWNER LLC" and row.enrichment_data["owner_pin"] == "6000000001"
    assert (await _get(db, sdci)).parcel_id is None
    assert (await _get(db, bellevue)).parcel_id == "6000000001"


async def test_a_padded_printed_parcel_is_not_provable_and_never_looked_up(
    db, business_user, monkeypatch,
):
    # Adapters store the normalized PIN; a padded value (a hand edit, a future adapter bug)
    # fails closed here exactly as skip trace's owner proof would.
    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    job_id = await _job(db, business_user)
    rid = await _printed_row(db, business_user, job_id, source="burien_code_enforcement",
                             parcel_id=" 7000000001")
    before = tuple(await _get(db, rid))

    stats = await asyncio.to_thread(_tick)

    assert asked == [] and stats["parcels"] == 0 and tuple(await _get(db, rid)) == before


@pytest.mark.parametrize("step", ["acquire", "renew", "release"])
async def test_a_celery_time_limit_in_the_lock_is_not_swallowed(monkeypatch, step):
    from celery.exceptions import SoftTimeLimitExceeded

    class _Client:
        def set(self, *a, **k):
            raise SoftTimeLimitExceeded()

        def eval(self, *a, **k):
            raise SoftTimeLimitExceeded()

    if step == "acquire":
        import redis as sync_redis

        monkeypatch.setattr(sync_redis, "from_url", lambda *a, **k: _Client())
        with pytest.raises(SoftTimeLimitExceeded):
            cvr._acquire_lock()
    elif step == "renew":
        with pytest.raises(SoftTimeLimitExceeded):
            cvr._renew_lock((_Client(), "token"))
    else:
        with pytest.raises(SoftTimeLimitExceeded):
            cvr._release_lock((_Client(), "token"))


async def test_the_kill_switch_the_lock_and_a_cooling_source_stop_it_before_any_request(
    db, business_user, monkeypatch,
):
    from src.config import settings
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.source_health import mark_source_unhealthy

    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, pin="9138100481")

    monkeypatch.setattr(settings, "OWNER_RECOVERY_ENABLED", False)
    off = await asyncio.to_thread(_tick)
    assert off["skipped"] == "OWNER_RECOVERY_ENABLED is off"
    monkeypatch.setattr(settings, "OWNER_RECOVERY_ENABLED", True)

    held = cvr._acquire_lock()
    assert isinstance(held, tuple)
    try:
        concurrent = await asyncio.to_thread(_tick)
        assert concurrent["skipped"] == "another tick is running"
    finally:
        cvr._release_lock(held)

    def _block():
        with system_sync_session() as sdb:
            mark_source_unhealthy(sdb, KING_EREALPROPERTY, "test cooldown")

    await asyncio.to_thread(_block)
    cooling = await asyncio.to_thread(_tick)
    assert cooling["skipped"] == "king_erealproperty is in cooldown"

    assert asked == []
    ed = (await _get(db, rid)).enrichment_data
    assert "cv_owner_recovery_last_at" not in ed and "owner_source" not in ed


async def test_without_redis_the_sweep_does_not_run(db, business_user, monkeypatch):
    import redis as sync_redis

    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    job_id = await _job(db, business_user)
    await _row(db, business_user, job_id, pin="1000000061")

    def _no_redis(*_a, **_k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(sync_redis, "from_url", _no_redis)
    stats = await asyncio.to_thread(_tick)

    assert asked == [] and stats["skipped"].startswith("lock unavailable")


async def test_a_tick_never_releases_a_lock_it_does_not_own():
    lock = cvr._acquire_lock()
    assert isinstance(lock, tuple)
    lock[0].set(cvr._LOCK_KEY, "someone-else")
    cvr._release_lock(lock)
    assert lock[0].get(cvr._LOCK_KEY) in (b"someone-else", "someone-else")
    lock[0].delete(cvr._LOCK_KEY)


def test_the_sweep_is_registered_and_scheduled():
    from celery.schedules import crontab

    from src.workers import app
    from src.workers.scheduler import app as _beat_app  # noqa: F401  (loads the schedule)

    assert "src.workers.cv_owner_recovery" in app.conf.include
    entry = app.conf.beat_schedule["recover-code-violation-owners"]
    assert entry["task"] == "src.workers.cv_owner_recovery.recover_code_violation_owners_task"
    assert entry["task"] in app.tasks
    assert entry["schedule"] == crontab(minute="18-59/20")


async def test_a_lookup_that_fails_before_any_request_charges_nobody(
    db, business_user, monkeypatch,
):
    class _BrokenLease:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise ConnectionError("lease store unreachable")

        def __exit__(self, *a):
            return None

    monkeypatch.setattr("src.scrapers.enrichment.source_admission.SourceAdmission", _BrokenLease)
    asked = _county(monkeypatch, {})
    job_id = await _job(db, business_user)
    ids = [await _row(db, business_user, job_id, pin=p, cv_owner_recovery_attempts=2)
           for p in ("1000000071", "1000000072")]

    stats = await asyncio.to_thread(_tick)

    assert asked == [] and stats["skipped"].startswith("ConnectionError")
    assert stats["unreached"] == 2 and stats["transient"] == 0
    for rid in ids:
        ed = (await _get(db, rid)).enrichment_data
        assert ed["cv_owner_recovery_attempts"] == 2
        assert "cv_owner_recovery_outcome" not in ed and "cv_owner_recovery_last_at" in ed


async def test_the_parcel_in_flight_when_the_lookup_crashed_is_not_charged(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    first = await _row(db, business_user, job_id, pin="1000000081")
    crashed = await _row(db, business_user, job_id, pin="1000000082")
    never = await _row(db, business_user, job_id, pin="1000000083")

    async def _fetch(pid, *, max_attempts=1, **_kw):
        if pid == "1000000082":
            raise RuntimeError("parser blew up")
        return "FIRST OWNER", False

    monkeypatch.setattr(kca, "_fetch_king_owner", _fetch)
    stats = await asyncio.to_thread(_tick)

    assert (await _get(db, first)).party_name == "FIRST OWNER"
    for rid in (crashed, never):
        row = await _get(db, rid)
        assert row.party_name is None
        assert "cv_owner_recovery_attempts" not in row.enrichment_data
        assert "cv_owner_recovery_last_at" in row.enrichment_data
    assert stats["found"] == 1 and stats["unreached"] == 2 and stats["transient"] == 0


async def test_a_blank_owner_is_never_written_as_found(db, business_user):
    from types import SimpleNamespace

    from src.db.session import system_sync_session

    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, pin="1000000091")
    row = SimpleNamespace(id=rid, user_id=business_user.id, pin="1000000091",
                          enrichment_data=_located("1000000091"))

    def _go():
        with system_sync_session() as sdb:
            return cvr._write(sdb, row, "found", "   ")

    label = await asyncio.to_thread(_go)

    stored = await _get(db, rid)
    assert label != "found" and stored.party_name is None
    ed = stored.enrichment_data
    assert "owner_source" not in ed and "owner_pin" not in ed
    assert ed.get("cv_owner_recovery_outcome") != "found"


async def test_a_row_whose_tenant_differs_from_its_job_is_never_touched(
    db, business_user, starter_user, monkeypatch,
):
    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    foreign_job = await _job(db, starter_user)
    rid = await _row(db, business_user, foreign_job, pin="1000000111")

    stats = await asyncio.to_thread(_tick)

    assert asked == [] and stats["parcels"] == 0
    assert "cv_owner_recovery_last_at" not in (await _get(db, rid)).enrichment_data


async def test_a_lead_with_no_usable_address_is_not_delivered_and_never_looked_up(
    db, business_user, monkeypatch,
):
    _lease(monkeypatch)
    asked = _county(monkeypatch, {})
    job_id = await _job(db, business_user)
    ids = []
    for pin, prop in (("1000000141", None), ("1000000142", "  "),
                      ("1000000143", "(enrichment unavailable)")):
        rid = await _row(db, business_user, job_id, pin=pin)
        await db.execute(text("UPDATE results SET property_address = :p, mailing_address = :p "
                              "WHERE id = :i"), {"p": prop, "i": rid})
        await db.commit()
        ids.append(rid)

    stats = await asyncio.to_thread(_tick)

    assert asked == [] and stats["parcels"] == 0
    for rid in ids:
        assert "cv_owner_recovery_last_at" not in (await _get(db, rid)).enrichment_data


async def test_a_whitespace_only_party_name_is_unnamed(db, business_user, monkeypatch):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, pin="1000000121", party="\t\n")
    _county(monkeypatch, {"1000000121": _Resp(200, _page("1000000121", "TAB OWNER"))})

    await asyncio.to_thread(_tick)

    assert (await _get(db, rid)).party_name == "TAB OWNER"


async def test_a_tick_that_lost_its_lock_writes_nothing(db, business_user, monkeypatch):
    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    rid = await _row(db, business_user, job_id, pin="1000000131")

    def _get_and_lose_lock(url, **_k):
        import redis as sync_redis

        from src.config import settings

        client = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
        client.set(cvr._LOCK_KEY, "a-newer-tick")   # our TTL expired, another tick took over
        return _Resp(200, _page(url.rsplit("=", 1)[-1], "LATE ANSWER"))

    monkeypatch.setattr(kca, "safe_get", _get_and_lose_lock)
    try:
        stats = await asyncio.to_thread(_tick)
        holder = cvr._acquire_lock()
        assert holder == "another tick is running"   # the newer tick's lock survives
    finally:
        import redis as sync_redis

        from src.config import settings

        sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs()).delete(cvr._LOCK_KEY)

    row = await _get(db, rid)
    assert row.party_name is None and "cv_owner_recovery_last_at" not in row.enrichment_data
    assert stats["skipped"] == "lock lost before writing" and stats["found"] == 0


async def test_a_lock_lost_between_two_rows_of_one_parcel_stops_the_second_write(
    db, business_user, monkeypatch,
):
    import redis as sync_redis

    from src.config import settings

    _lease(monkeypatch)
    job_id = await _job(db, business_user)
    first = await _row(db, business_user, job_id, pin="1000000151")
    second = await _row(db, business_user, job_id, pin="1000000151")
    _county(monkeypatch, {"1000000151": _Resp(200, _page("1000000151", "SHARED OWNER"))})
    real_write = cvr._write
    client = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())

    def _write_then_lose_lock(*a, **k):
        label = real_write(*a, **k)
        client.set(cvr._LOCK_KEY, "a-newer-tick")
        return label

    monkeypatch.setattr(cvr, "_write", _write_then_lose_lock)
    try:
        stats = await asyncio.to_thread(_tick)
    finally:
        client.delete(cvr._LOCK_KEY)

    named = [(await _get(db, rid)).party_name for rid in sorted((first, second))]
    assert named == ["SHARED OWNER", None]
    assert stats["found"] == 1 and stats["skipped"] == "lock lost before writing"


def test_the_blank_owner_page_classifies_as_not_found():
    out = cvr._classify(["1000000101"], {"1000000101": "  "},
                        {"outcome": "complete", "attempted": ["1000000101"], "transient": [],
                         "no_owner_on_record": [], "parcel_mismatch": []})
    assert out == {"1000000101": "transient"}


def test_a_registration_failure_is_logged_at_error_with_the_exception(monkeypatch):
    import importlib
    import logging

    from src.workers import app

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture(level=logging.DEBUG)
    logger = logging.getLogger("worker.cv_owner_recovery")
    logger.addHandler(handler)

    def _broken_task(*_a, **_k):
        raise RuntimeError("broker config rejected")

    monkeypatch.setattr(app, "task", _broken_task)
    try:
        importlib.reload(cvr)
    finally:
        monkeypatch.undo()
        logger.removeHandler(handler)
        importlib.reload(cvr)

    errors = [r for r in records if r.levelno == logging.ERROR and "NOT registered" in r.getMessage()]
    assert errors and errors[0].exc_info is not None
    assert "src.workers.cv_owner_recovery.recover_code_violation_owners_task" in app.tasks
