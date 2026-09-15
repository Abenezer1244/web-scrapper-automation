"""Tacoma (Pierce) code violations: the owner is the Pierce taxpayer of record, proven.

Owner decision 2026-09-14: legal review cleared storing Pierce ATIP taxpayer names FOR
CODE-VIOLATION OWNER NAMING ONLY. Before it, party_name held the case label ("Nuisance -
2117 AVE S") and skip trace could never run for Tacoma. Only external answers are
substituted here: Tacoma layer rows and the ATIP property page's own pcAtipSummary
response, copied verbatim from real responses on 2026-09-14 (a mobile-home taxpayer
name is replaced by REDACTED because that test only needs the account type). Matching,
gating, the lease (real Redis), source health, SQL writes and the sweep run for real.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.config import settings
from src.db.models import Job, Result, ScraperConfig
from src.scrapers import pierce_wa_code_violation as pcv
from src.scrapers.enrichment import pierce_atip_owner as pao
from src.scrapers.enrichment.skip_trace import build_pending_row_payload
from src.workers import pierce_cv_owner_recovery as rec
from src.workers.property_identity import legacy_strong_signature

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_pierce_code_violation_owner.py"
_spec = importlib.util.spec_from_file_location("backfill_pierce_cv_owner", _SCRIPT)
bpo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpo)

# ── Real Tacoma Code Violations layer rows (services3.arcgis.com, 2026-09-14) ───
TACOMA_2117 = {"objectid": 20491, "casenumber": "60000303996", "opendate": 1787904000000,
               "description": "COM2026 2117 TACOMA AVE S", "parcelnumber": "2021110133",
               "address": "2117 AVE S", "casetype": "Nuisance", "currentstatus": "Open",
               "casestatus": "Notice Sent ", "inspector": "Felix Vazquez_Cruz",
               "daysopentoclosed": None, "customernumber": "0401457469",
               "latitude": 47.232033479195614, "longitude": -122.46445862961257,
               "parcelinfo": "https://atip.piercecountywa.gov/app/v2/propertyDetail/2021110133/summary"}
TACOMA_602 = {"objectid": 9882, "casenumber": "60000301838", "opendate": 1784188800000,
              "description": "COM2026 602 TACOMA AVE S", "parcelnumber": "2006120010",
              "address": "602 AVE S", "casetype": "Derelict Building  - 2.01.060 (D)",
              "currentstatus": "Closed", "casestatus": "Closed Resolved",
              "inspector": "Christina Freel", "daysopentoclosed": 4, "customernumber": "0400020227",
              "latitude": 47.23451232819977, "longitude": -122.44032795874936,
              "parcelinfo": "https://atip.piercecountywa.gov/app/v2/propertyDetail/2006120010/summary"}

# ── Real ATIP /api/pcAtipSummary bodies, as the property page received them ─────
ATIP_2117 = ('[{"parcel_number":"2021110133","acct_type":"Real Property","situs":"2117 TACOMA AVE S",'
             '"mail":"1550 140TH AVE NE STE 201","mail2":null,"mail3":null,"city":"BELLEVUE",'
             '"zip":"98005-4500","care_of":null,"state":"WA","country":null,'
             '"use_cd":"9170-COMM VAC LAND","name":"TACOMA TOWN CENTER PARCELS LLC","tax_year":"2027",'
             '"category":"Land and Improvements","sa":null,"fclr_status":null,"status":"Active  ",'
             '"rtsq":"03-20-09-22","can_pay":"Y"}]')
ATIP_602 = ('[{"parcel_number":"2006120010","acct_type":"Real Property","situs":"602 TO 610 TACOMA AVE S",'
            '"mail":"6304 6TH AVE","mail2":null,"mail3":null,"city":"TACOMA","zip":"98406","care_of":null,'
            '"state":"WA","country":"UNITED STATES","use_cd":"9178-COMM LND WITH IMPROV  LAND VAL ONLY",'
            '"name":"602 LLC","tax_year":"2027","category":"Land and Improvements","sa":null,'
            '"fclr_status":null,"status":"Active  ","rtsq":"03-20-05-11","can_pay":"Y"}]')
ATIP_DIVISION = ('[{"parcel_number":"2030120032","acct_type":"Real Property","situs":"633 TO 649 DIVISION AVE",'
                 '"mail":"233 S WACKER DR STE 4700","mail2":null,"mail3":null,"city":"CHICAGO","zip":"60606",'
                 '"care_of":"C/O CONNIE L ELLIS","state":"IL","country":null,'
                 '"use_cd":"5300-GEN MERCHANDISE RETAIL TRADE","name":"B10 MOUNTAIN A WA LLC",'
                 '"tax_year":"2027","category":"Land and Improvements","sa":null,"fclr_status":null,'
                 '"status":"Active  ","rtsq":"03-21-32-42","can_pay":"Y"}]')
ATIP_PORT = ('[{"parcel_number":"0320011115","acct_type":"Real Property","situs":"5015 E 8TH ST UNIT A & B",'
             '"mail":"PO BOX 1837","mail2":null,"mail3":null,"city":"TACOMA","zip":"98401-1837",'
             '"care_of":"REAL ESTATE DEPT","state":"WA","country":"UNITED STATES",'
             '"use_cd":"9180-VAC INDUSTRIAL LAND","name":"PORT OF TACOMA","tax_year":"2027",'
             '"category":"Land and Improvements","sa":null,"fclr_status":null,"status":"Active  ",'
             '"rtsq":"03-20-01-14","can_pay":"Y"}]')
ATIP_CONDO_REFERENCE = ('[{"parcel_number":"2000050082","acct_type":"Real Property","situs":"REFERENCE",'
                        '"mail":"REFERENCE","mail2":null,"mail3":null,"city":"PUYALLUP","zip":"98375",'
                        '"care_of":null,"state":"WA","country":"UNITED STATES","use_cd":"0000-UNKNOWN",'
                        '"name":"REFERENCE","tax_year":"2027","category":"Land and Improvements","sa":null,'
                        '"fclr_status":null,"status":"Active  ","rtsq":"03-21-32-41","can_pay":"Y"}]')
ATIP_MOBILE_HOME = ('[{"parcel_number":"5000050810","acct_type":"Mobile Home","situs":"7612 159TH ST E #151",'
                    '"mail":"7612 159TH ST E SPC 151","mail2":null,"mail3":null,"city":"PUYALLUP",'
                    '"zip":"98375-7130","care_of":null,"state":"WA","country":null,'
                    '"use_cd":"1152-MOBILE/MFG HOME","name":"REDACTED","tax_year":"2027",'
                    '"category":"Mobile Home","sa":null,"fclr_status":"DSRT","status":"Active  ",'
                    '"rtsq":null,"can_pay":"Y"}]')
ATIP_UNKNOWN_PARCEL = "[]"          # 9999999999
ATIP_VERIFICATION_REJECTED = ""    # the portal's answer to an unverified session


def _rows(body: str) -> list[dict]:
    return json.loads(body)


class _Resp:
    def __init__(self, payload):
        self._payload, self.status_code = payload, 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


# ── Scraper semantics ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scraper_stores_no_label_as_party_and_keeps_the_case_fields(monkeypatch):
    pages = [{"features": [{"attributes": TACOMA_2117}, {"attributes": TACOMA_602}]}]
    monkeypatch.setattr(pcv, "safe_get", lambda *a, **kw: _Resp(pages.pop(0)))
    recs = {r.legal_description: r for r in
            await pcv.PierceWACodeViolationScraper().scrape("07/01/2026", "09/14/2026")}
    rec_602 = recs["60000301838"]
    assert rec_602.party_name is None
    assert rec_602.parcel_id == "2006120010"          # the real source parcel, unchanged
    assert rec_602.property_address == "602 AVE S"
    ed = rec_602.enrichment_data
    assert ed["violation_category"] == "Derelict Building  - 2.01.060 (D)"
    assert ed["case_type"] == "Derelict Building  - 2.01.060 (D)"
    assert ed["status"] == "Closed" and ed["source"] == "tacoma_code_violations"
    assert "description" not in ed                     # complaint free text is never stored


@pytest.mark.asyncio
async def test_scraper_idempotency_key_is_the_case_not_the_party(monkeypatch):
    async def _scrape():
        pages = [{"features": [{"attributes": TACOMA_2117}, {"attributes": TACOMA_602}]}]
        monkeypatch.setattr(pcv, "safe_get", lambda *a, **kw: _Resp(pages.pop(0)))
        return await pcv.PierceWACodeViolationScraper().scrape("07/01/2026", "09/14/2026")

    first, second = await _scrape(), await _scrape()
    keys = [r.raw_html_hash for r in first]
    assert keys == [r.raw_html_hash for r in second]
    assert len(set(keys)) == 2 and all(len(k) == 32 for k in keys)
    import hashlib
    assert first[0].raw_html_hash == hashlib.sha256(b"tacoma_cv|60000303996").hexdigest()[:32]


# ── Tacoma address normalization ───────────────────────────────────────────────

@pytest.mark.parametrize(("lead", "situs"), [
    ("2117 AVE S", "2117 TACOMA AVE S"),                # the layer drops TACOMA
    ("1717 SOUTH WAY", "1717 SOUTH TACOMA WAY"),
    ("602 AVE S", "602 TO 610 TACOMA AVE S"),           # range + dropped TACOMA
    ("606 AVE S", "602 TO 610 TACOMA AVE S"),
    ("641 DIVISION AVE", "633 TO 649 DIVISION AVE"),
    ("5015 E 8TH ST", "5015 E 8TH ST UNIT A & B"),      # unit lists
    ("3612 S MASON AVE", "3612 S MASON AVE UNIT A - D"),
    ("2810 MARSHALL AVE", "2810 MARSHALL AVE UNIT ABCDEF"),
    ("1603 N ALDER STREET", "1603 N ALDER ST"),
    ("2117 TACOMA AVE S", "2117 TACOMA AVE S"),          # after the GIS pass rewrote it
])
def test_tacoma_addresses_that_are_the_same_place(lead, situs):
    assert pao.situs_agrees(lead, situs)


@pytest.mark.parametrize(("lead", "situs"), [
    ("1605 N ALDER ST", "1603 N ALDER ST"),              # another house
    ("1603 N ALDER ST", "1603 S ALDER ST"),              # another street
    ("605 AVE S", "602 TO 610 TACOMA AVE S"),            # wrong side of the block
    ("612 AVE S", "602 TO 610 TACOMA AVE S"),            # outside the range
    ("2117 TACOMA AVE S", "2117 AVE S"),                 # only the LEAD may lack TACOMA
    ("2117 AVE", "2117 TACOMA AVE S"),
    ("2346A AVE S", "2346 TACOMA AVE S"),                # a lettered number is not proven
    ("", "1603 N ALDER ST"),
    ("1603 N ALDER ST", "REFERENCE"),
])
def test_tacoma_addresses_that_are_not(lead, situs):
    assert not pao.situs_agrees(lead, situs)


# ── Acceptance rules ───────────────────────────────────────────────────────────

def test_a_real_property_taxpayer_on_the_same_situs_is_accepted():
    d = pao.decide("2021110133", _rows(ATIP_2117), "2117 AVE S")
    assert (d.status, d.name) == ("matched", "TACOMA TOWN CENTER PARCELS LLC")
    d = pao.decide("2006120010", _rows(ATIP_602), "602 AVE S")
    assert (d.status, d.name) == ("matched", "602 LLC")


def test_care_of_is_never_the_owner():
    d = pao.decide("2030120032", _rows(ATIP_DIVISION), "641 DIVISION AVE")
    assert d.name == "B10 MOUNTAIN A WA LLC"


@pytest.mark.parametrize(("parcel", "body", "address", "status"), [
    ("2021110134", ATIP_2117, "2117 AVE S", "parcel_mismatch"),     # a page for another parcel
    ("5000050810", ATIP_MOBILE_HOME, "7612 159TH ST E", "not_real_property"),
    ("2000050082", ATIP_CONDO_REFERENCE, "35 BROADWAY", "reference_parcel"),
    ("2021110133", ATIP_2117, "2119 AVE S", "address_mismatch"),
    ("9999999999", ATIP_UNKNOWN_PARCEL, "1 A ST", "not_on_record"),
], ids=["other_parcel", "mobile_home", "condo_reference", "other_house", "unknown_parcel"])
def test_everything_else_names_nobody(parcel, body, address, status):
    d = pao.decide(parcel, _rows(body), address)
    assert (d.status, d.name) == (status, None)


def test_a_blank_taxpayer_name_is_not_an_owner():
    row = dict(_rows(ATIP_2117)[0], name="   ")
    assert pao.decide("2021110133", [row], "2117 AVE S").status == "no_name"


def test_two_rows_for_the_asked_parcel_are_ambiguous():
    row = _rows(ATIP_2117)[0]
    assert pao.decide("2021110133", [row, dict(row, name="OTHER LLC")], "2117 AVE S").status == \
        "parcel_mismatch"


# ── The lookup: one page view per parcel, paced, leased, gated ────────────────

class _Portal:
    """Stands in for the browser: answers each parcel's summary call with a real body."""

    def __init__(self, answers: dict[str, list[tuple[int, str]]]):
        self.answers = answers
        self.sessions = 0
        self.requests: list[str] = []

    def install(self, monkeypatch):
        portal = self

        class _Session:
            async def __aenter__(self):
                portal.sessions += 1
                return self

            async def __aexit__(self, *exc):
                return None

        async def _fetch(session, parcel):
            portal.requests.append(parcel)
            return portal.answers[parcel].pop(0)

        monkeypatch.setattr(pao, "_new_session", _Session)
        monkeypatch.setattr(pao, "_fetch_summary", _fetch)
        return self


@pytest.fixture
def paces(monkeypatch):
    waited: list[float] = []

    async def _sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(pao.asyncio, "sleep", _sleep)
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_ENABLED", True)
    return waited


def _clear_health():
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        s.execute(text("DELETE FROM external_source_health WHERE source_key = :k"),
                  {"k": "pierce_atip_owner"})
        s.commit()


@pytest.fixture
def clean_health():
    _clear_health()
    yield
    _clear_health()


def test_flag_off_makes_zero_requests(monkeypatch):
    portal = _Portal({"2021110133": [(200, ATIP_2117)]}).install(monkeypatch)
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_ENABLED", False)
    stats: dict = {}
    assert pao.lookup_parcels(["2021110133"], stats=stats) == {}
    assert stats["outcome"] == "disabled"
    assert portal.sessions == 0 and portal.requests == []


def test_lookup_is_paced_and_audited_without_names(monkeypatch, paces, clean_health, caplog):
    portal = _Portal({"2021110133": [(200, ATIP_2117)], "2006120010": [(200, ATIP_602)],
                      "9999999999": [(200, ATIP_UNKNOWN_PARCEL)]}).install(monkeypatch)
    stats: dict = {}
    with caplog.at_level(logging.INFO, logger="scraper.enrichment.pierce_atip_owner"):
        got = pao.lookup_parcels(["2021110133", " 2006120010 ", "2021110133", "12345", "9999999999"],
                                 pace_s=0.1, stats=stats)
    assert portal.requests == ["2021110133", "2006120010", "9999999999"]   # invalid/dupes dropped
    assert portal.sessions == 1
    assert paces == [pao.MIN_PACE_S, pao.MIN_PACE_S]                       # never below 2 s
    assert got["2021110133"].kind == "found" and got["9999999999"].kind == "not_found"
    assert stats["outcome"] == "complete"
    log = caplog.text
    assert "parcel=2021110133 outcome=found" in log and "TACOMA TOWN CENTER" not in log


def test_a_rejected_verification_restarts_once_then_stops_and_cools_down(monkeypatch, paces,
                                                                          clean_health):
    portal = _Portal({"2021110133": [(200, ATIP_VERIFICATION_REJECTED), (200, ATIP_2117)],
                      "2006120010": [(200, ATIP_VERIFICATION_REJECTED),
                                     (200, ATIP_VERIFICATION_REJECTED)],
                      "9999999999": [(200, ATIP_UNKNOWN_PARCEL)]}).install(monkeypatch)
    stats: dict = {}
    got = pao.lookup_parcels(["2021110133", "2006120010", "9999999999"], stats=stats)
    assert got["2021110133"].kind == "found"            # the fresh session answered
    assert stats["outcome"] == "verification_rejected"
    assert portal.sessions == 2 and "9999999999" not in portal.requests
    assert stats["transient"] == ["2006120010"]
    # The whole portal now cools down: the next pass makes no request at all.
    again: dict = {}
    assert pao.lookup_parcels(["9999999999"], stats=again) == {}
    assert again["outcome"] == "source_unavailable" and portal.requests.count("9999999999") == 0


def test_three_hard_failures_stop_the_batch(monkeypatch, paces, clean_health):
    portal = _Portal({p: [(503, "Service Unavailable")] for p in
                      ("2021110133", "2006120010", "2030120032", "0320011115")}).install(monkeypatch)
    stats: dict = {}
    assert pao.lookup_parcels(["2021110133", "2006120010", "2030120032", "0320011115"],
                              stats=stats) == {}
    assert stats["outcome"] == "source_failing"
    assert portal.requests == ["2021110133", "2006120010", "2030120032"]


def test_a_held_lease_means_no_second_browser(monkeypatch, paces, clean_health, redis_client):
    portal = _Portal({"2021110133": [(200, ATIP_2117)]}).install(monkeypatch)
    redis_client.set("bl:source_admission:pierce_atip_owner", "another-pass", ex=60)
    monkeypatch.setattr(pao, "_LEASE_WAIT_S", 0.0)
    stats: dict = {}
    assert pao.lookup_parcels(["2021110133"], stats=stats) == {}
    assert stats["outcome"] == "not_admitted" and portal.sessions == 0


def test_an_unconfirmable_lease_fails_closed(monkeypatch, paces, clean_health):
    portal = _Portal({"2021110133": [(200, ATIP_2117)]}).install(monkeypatch)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")   # nothing listens
    stats: dict = {}
    assert pao.lookup_parcels(["2021110133"], stats=stats) == {}
    assert stats["outcome"] == "not_admitted" and portal.sessions == 0


# ── Rows: only Tacoma code violations are ever named from ATIP ─────────────────

def _cv_row(**over):
    base = {"id": "r1", "job_id": "j1", "user_id": "u1", "party_name": None,
            "parcel_id": "2021110133", "property_address": "2117 AVE S",
            "mailing_address": "1550 140TH AVE NE STE 201, BELLEVUE, WA, 98005-4500",
            "property_city": "TACOMA", "property_state": "WA", "property_zip": "98402",
            "enrichment_data": {"source": "tacoma_code_violations", "case_number": "60000303996"}}
    base.update(over)
    return SimpleNamespace(**base)


def test_owner_is_written_with_its_proof_and_parcel_id_is_untouched():
    row, label = _cv_row(), _cv_row(id="r2", party_name="Nuisance - 2117 AVE S")
    mapped = pao.owner_lookup_parcels([row, label])
    assert mapped == {"2021110133": [row]}                 # a party (even a label) is not replaced
    counts = pao.apply_owner_decisions(
        mapped, {"2021110133": pao.Fetched("found", _rows(ATIP_2117))}, checked_at="t")
    assert counts["matched"] == 1
    assert row.party_name == "TACOMA TOWN CENTER PARCELS LLC"
    assert row.parcel_id == "2021110133"
    assert {k: row.enrichment_data[k] for k in ("owner_source", "owner_pin", "owner_status")} == {
        "owner_source": "pierce_atip", "owner_pin": "2021110133", "owner_status": "matched"}


def test_a_rejected_row_gets_a_terminal_status_and_no_name():
    row = _cv_row(property_address="2119 AVE S")
    counts = pao.apply_owner_decisions(pao.owner_lookup_parcels([row]),
                                       {"2021110133": pao.Fetched("found", _rows(ATIP_2117))},
                                       checked_at="t")
    assert counts["address_mismatch"] == 1 and row.party_name is None
    assert row.enrichment_data["owner_status"] == "address_mismatch"
    assert "owner_source" not in row.enrichment_data
    assert pao.owner_lookup_parcels([row]) == {}           # never looked up again


def test_a_row_changed_since_selection_is_left_alone():
    row = _cv_row()
    mapped = pao.owner_lookup_parcels([row])
    row.parcel_id = "2021110134"
    counts = pao.apply_owner_decisions(
        mapped, {"2021110133": pao.Fetched("found", _rows(ATIP_2117))}, checked_at="t")
    assert counts["stale"] == 1 and row.party_name is None


@pytest.mark.parametrize("source", ["pierce_recorder", "pierce_arms", None, "seattle_sdci_code_violations"])
def test_no_other_record_type_is_ever_offered_for_atip_naming(source):
    ed = {"source": source} if source else {}
    assert pao.owner_lookup_parcels([_cv_row(enrichment_data=ed)]) == {}


# ── Skip trace: a Tacoma owner is paid for only with its own proof ─────────────

def _proven(**over):
    ed = {"source": "tacoma_code_violations", "owner_source": "pierce_atip",
          "owner_pin": "2021110133", "owner_status": "matched"}
    ed.update(over)
    return ed


def test_a_proven_tacoma_owner_is_traceable():
    payload = build_pending_row_payload(_cv_row(party_name="SMITH JOHN A", enrichment_data=_proven()))
    assert payload is not None and payload["property_address"] == "2117 AVE S"


@pytest.mark.parametrize(("party", "parcel", "ed"), [
    ("SMITH JOHN A", "2021110134", _proven()),                       # parcel changed since naming
    ("SMITH JOHN A", "2021110133", _proven(owner_source="king_erealproperty")),
    ("SMITH JOHN A", "2021110133", _proven(owner_status="address_mismatch")),
    ("SMITH JOHN A", "2021110133", _proven(owner_pin=None)),
    ("SMITH JOHN A", None, _proven()),
    ("   ", "2021110133", _proven()),
    (None, "2021110133", _proven()),
    ("Nuisance - 2117 AVE S", "2021110133", {"source": "tacoma_code_violations"}),
])
def test_anything_short_of_that_proof_is_not_traced(party, parcel, ed):
    assert build_pending_row_payload(_cv_row(party_name=party, parcel_id=parcel, enrichment_data=ed)) is None


# ── DB: live pass, sweep and repair ────────────────────────────────────────────

async def _pierce_job(db, user, *, record_type="code_violation", status="done") -> str:
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="Pierce",
                           county="pierce", state="WA", record_type=record_type,
                           fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
                           deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    return job_id


async def _stored(db, user, job_id, *, party, parcel="2021110133", address="2117 AVE S",
                  ed=None, is_duplicate=False) -> tuple[str, str]:
    rid = str(uuid.uuid4())
    dedup = legacy_strong_signature(parcel, address)
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name=party, parcel_id=parcel,
                  property_address=address, legal_description="60000303996",
                  mailing_address="1550 140TH AVE NE STE 201, BELLEVUE, WA, 98005-4500",
                  dedup_hash=dedup, skip_trace_status="not_attempted", is_duplicate=is_duplicate,
                  enrichment_data=ed if ed is not None else {
                      "source": "tacoma_code_violations", "case_number": "60000303996"}))
    await db.commit()
    return rid, dedup


async def _fetch_rows(db, ids):
    return {str(r.id): r for r in (await db.execute(text(
        "SELECT id, party_name, parcel_id, dedup_hash, mailing_address, skip_trace_status, "
        "enrichment_data FROM results WHERE id = ANY(:ids)"), {"ids": list(ids)})).all()}


def _inline(job_id, redis_client):
    from src.db.session import system_sync_session
    from src.workers.tasks_helpers.enrich import _run_inline_enrichment

    with system_sync_session() as sdb:
        job = sdb.get(Job, str(job_id))
        config = sdb.get(ScraperConfig, job.scraper_config_id)
        _run_inline_enrichment(sdb, job, redis_client, str(job_id), config, summary={})


@pytest.mark.asyncio
async def test_a_live_pierce_cv_job_names_the_owner_and_keeps_parcel_and_dedup(
    db, business_user, redis_client, monkeypatch, paces, clean_health,
):
    job_id = await _pierce_job(db, business_user, status="enriching")
    named, dedup = await _stored(db, business_user, job_id, party=None)
    other, _ = await _stored(db, business_user, job_id, party=None, parcel="2006120010",
                             address="602 AVE S")
    portal = _Portal({"2021110133": [(200, ATIP_2117)],
                      "2006120010": [(200, ATIP_602)]}).install(monkeypatch)
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis",
                        lambda *a, **kw: {})
    await asyncio.to_thread(_inline, job_id, redis_client)

    got = await _fetch_rows(db, [named, other])
    n = got[named]
    assert n.party_name == "TACOMA TOWN CENTER PARCELS LLC"
    assert (n.parcel_id, n.dedup_hash) == ("2021110133", dedup)
    assert n.mailing_address == "1550 140TH AVE NE STE 201, BELLEVUE, WA, 98005-4500"
    assert n.skip_trace_status == "not_attempted"
    assert n.enrichment_data["owner_pin"] == "2021110133"
    assert got[other].party_name == "602 LLC"
    assert sorted(portal.requests) == ["2006120010", "2021110133"]


@pytest.mark.asyncio
async def test_boundary_a_pierce_probate_job_never_asks_atip_for_a_name(
    db, business_user, redis_client, monkeypatch, paces, clean_health,
):
    """The 2026-09-14 clearance is code violations only: probate stays address-only."""
    job_id = await _pierce_job(db, business_user, record_type="probate", status="enriching")
    rid, _ = await _stored(db, business_user, job_id, party=None,
                           ed={"source": "pierce_recorder"})
    portal = _Portal({"2021110133": [(200, ATIP_2117)]}).install(monkeypatch)
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis",
                        lambda *a, **kw: {})
    await asyncio.to_thread(_inline, job_id, redis_client)
    row = (await _fetch_rows(db, [rid]))[rid]
    assert portal.sessions == 0 and portal.requests == []
    assert row.party_name is None and "owner_source" not in row.enrichment_data


@pytest.mark.asyncio
async def test_the_sweep_names_delivered_rows_only_and_charges_transients(
    db, business_user, monkeypatch, paces, clean_health,
):
    job_id = await _pierce_job(db, business_user)
    delivered, dedup = await _stored(db, business_user, job_id, party=None)
    duplicate, _ = await _stored(db, business_user, job_id, party=None, is_duplicate=True)
    over_quota, _ = await _stored(db, business_user, job_id, party=None, ed={
        "source": "tacoma_code_violations", "delivery_excluded_reason": "plan_cap"})
    flaky, _ = await _stored(db, business_user, job_id, party=None, parcel="2006120010",
                             address="602 AVE S")
    portal = _Portal({"2021110133": [(200, ATIP_2117)],
                      "2006120010": [(503, "Service Unavailable")]}).install(monkeypatch)

    stats = await asyncio.to_thread(rec.recover_pierce_cv_owners)
    assert stats["matched"] == 1 and stats["transient"] == 1
    got = await _fetch_rows(db, [delivered, duplicate, over_quota, flaky])
    assert got[delivered].party_name == "TACOMA TOWN CENTER PARCELS LLC"
    assert (got[delivered].parcel_id, got[delivered].dedup_hash) == ("2021110133", dedup)
    assert got[duplicate].party_name is None and got[over_quota].party_name is None
    assert got[flaky].enrichment_data["owner_recovery_attempts"] == 1
    assert "owner_status" not in got[flaky].enrichment_data
    assert portal.requests.count("2021110133") == 1

    # Named rows are never asked again; the flaky parcel is retried.
    portal.answers["2006120010"] = [(200, ATIP_602)]
    again = await asyncio.to_thread(rec.recover_pierce_cv_owners)
    assert again["matched"] == 1 and portal.requests.count("2021110133") == 1
    assert (await _fetch_rows(db, [flaky]))[flaky].party_name == "602 LLC"


@pytest.mark.asyncio
async def test_the_sweep_does_nothing_with_the_flag_off(db, business_user, monkeypatch):
    job_id = await _pierce_job(db, business_user)
    await _stored(db, business_user, job_id, party=None)
    portal = _Portal({"2021110133": [(200, ATIP_2117)]}).install(monkeypatch)
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_ENABLED", False)
    stats = await asyncio.to_thread(rec.recover_pierce_cv_owners)
    assert stats["skipped"] == "PIERCE_CV_OWNER_ENABLED is off" and portal.sessions == 0


def test_the_sweep_is_a_crontab_off_the_king_lease():
    from celery.schedules import crontab

    from src.workers import app
    from src.workers.scheduler import app as beat_app

    entry = beat_app.conf.beat_schedule["recover-pierce-cv-owners"]
    assert isinstance(entry["schedule"], crontab)
    assert sorted(entry["schedule"].minute) == [10, 40]
    assert "src.workers.pierce_cv_owner_recovery" in app.conf.include


@pytest.mark.asyncio
async def test_repair_replaces_only_the_old_label_and_converges(
    db, business_user, tmp_path, monkeypatch, paces, clean_health,
):
    job_id = await _pierce_job(db, business_user)
    label, dedup = await _stored(db, business_user, job_id, party="Nuisance - 2117 AVE S")
    cleared, _ = await _stored(db, business_user, job_id, parcel="2006120010", address="602 AVE S",
                               party="Derelict Building  - 2.01.060 (D) - 602 AVE S",
                               ed={"source": "tacoma_code_violations", "case_number": "60000301838"})
    foreign, _ = await _stored(db, business_user, job_id, party="HAND ENTERED NAME")
    live_job = await _pierce_job(db, business_user, status="enriching")
    live, _ = await _stored(db, business_user, live_job, party="Nuisance - 2117 AVE S")

    layer_calls: list = []

    def _layer(url, params=None, **kw):
        layer_calls.append(params["where"])
        return _Resp({"features": [{"attributes": TACOMA_2117}, {"attributes": TACOMA_602}]})

    import src.utils.safe_http as safe_http
    monkeypatch.setattr(safe_http, "safe_get", _layer)
    monkeypatch.setattr(bpo.time, "sleep", lambda s: None)
    # 602's taxpayer page says the parcel is not on record: that label is cleared, no name.
    portal = _Portal({"2021110133": [(200, ATIP_2117)],
                      "2006120010": [(200, ATIP_UNKNOWN_PARCEL)]}).install(monkeypatch)

    def _run(apply_writes):
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            return bpo.run(sdb, apply_writes=apply_writes, owners=apply_writes,
                           report=tmp_path / "ev.jsonl", source_pace_s=0)

    dry = await asyncio.to_thread(_run, False)
    assert dry["candidates"] == 3 and "writes" not in dry and portal.requests == []
    assert "casenumber IN (" in layer_calls[0]
    stats = await asyncio.to_thread(_run, True)
    assert stats["writes"] == {"written": 3, "skipped_by_write_guard": 0}
    assert stats["named"] == 1 and stats["label_cleared_no_owner"] == 1
    assert stats["party_name_not_the_label_left_alone"] == 1

    got = await _fetch_rows(db, [label, cleared, foreign, live])
    assert got[label].party_name == "TACOMA TOWN CENTER PARCELS LLC"
    assert got[label].enrichment_data["violation_category"] == "Nuisance"
    assert (got[label].parcel_id, got[label].dedup_hash) == ("2021110133", dedup)
    assert got[label].skip_trace_status == "not_attempted"
    assert got[cleared].party_name is None
    assert got[cleared].enrichment_data["owner_status"] == "not_on_record"
    assert got[foreign].party_name == "HAND ENTERED NAME"
    assert got[live].party_name == "Nuisance - 2117 AVE S"
    assert "TACOMA TOWN CENTER" not in (tmp_path / "ev.jsonl").read_text()

    assert (await asyncio.to_thread(_run, True))["candidates"] == 0


@pytest.mark.asyncio
async def test_repair_write_guard_skips_a_row_whose_party_or_parcel_moved(db, business_user):
    job_id = await _pierce_job(db, business_user)
    rid, _ = await _stored(db, business_user, job_id, party="Nuisance - 2117 AVE S")

    def _write(old_party, old_parcel):
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            res = sdb.execute(text(bpo._UPDATE_SQL), {
                "new_party": "TACOMA TOWN CENTER PARCELS LLC", "old_party": old_party,
                "old_parcel": old_parcel, "old_address": "2117 AVE S", "rid": rid,
                "uid": business_user.id, "payload": "{}", "source": bpo._SOURCE,
                "writes_owner_status": True})
            sdb.commit()
            return res.rowcount

    assert await asyncio.to_thread(_write, "Nuisance - 2117 AVE S", "2021110134") == 0
    assert await asyncio.to_thread(_write, "SOMEONE ELSE", "2021110133") == 0
    assert await asyncio.to_thread(_write, "Nuisance - 2117 AVE S", "2021110133") == 1


def test_repair_owners_needs_the_flag_and_the_public_redis(monkeypatch):
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_ENABLED", False)
    with pytest.raises(SystemExit, match="PIERCE_CV_OWNER_ENABLED"):
        bpo.main(["--owners"])
    monkeypatch.setenv("REDIS_URL", "redis://default:x@redis.railway.internal:6379")
    with pytest.raises(SystemExit):
        bpo._refuse_private_redis()


def test_old_label_is_rebuilt_exactly_from_the_source_row():
    assert bpo.old_scraper_label(TACOMA_2117) == "Nuisance - 2117 AVE S"
    assert bpo.old_scraper_label(TACOMA_602) == "Derelict Building  - 2.01.060 (D) - 602 AVE S"
    assert bpo.old_scraper_label({"casetype": "Nuisance"}) == "Nuisance"
