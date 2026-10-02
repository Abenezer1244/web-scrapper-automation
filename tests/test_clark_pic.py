"""Clark County owner mailing addresses from the Property Information Center.

Every Clark lead had a NULL mailing address because Clark had no mailing source at
all (admin job 62404bd0: 1,335 rows, 0 mailing). These tests pin the fix end to end:
the page parser, the paced/guarded resolver, the county_gis hook, the live job pass,
the background recovery sweep, and what the API and CSV hand back.

Real DB, real rows, real Redis lease. Only the county's HTTP boundary is substituted,
with pages built from the exact markup the live site served on 2026-10-02 (names and
addresses sanitized).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
import requests
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig
from src.scrapers.enrichment import clark_pic as cp
from src.scrapers.enrichment import county_gis as cg
from src.workers import mailing_recovery as mr


@pytest.fixture(autouse=True)
def _clark_enabled_and_fast(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", True)
    monkeypatch.setattr(cp, "_PACE_S", 0.0)
    monkeypatch.setattr(cp, "_JITTER_S", 0.0)
    monkeypatch.setattr(cp, "_RETRY_BACKOFF_S", 0.0)
    # The statewide situs layer is a different source; keep it off the network.
    monkeypatch.setattr(cg, "_batch_query_wa_statewide", lambda pids, county: {
        p: {"property_address": "13114 NE 144TH ST", "mailing_address": None,
            "property_city": "BRUSH PRAIRIE", "property_state": "WA", "property_zip": "98606"}
        for p in pids})


@pytest.fixture(autouse=True)
def _clean_source_state(redis_client):
    """Clark's lease and cooldown are shared state; no test may inherit another's."""
    from src.db.session import system_sync_session

    def _reset():
        redis_client.delete("bl:source_admission:clark_pic")
        with system_sync_session() as s:
            s.execute(text("DELETE FROM external_source_health WHERE source_key = 'clark_pic'"))
            s.commit()

    _reset()
    yield
    _reset()


def _page(pid: str, mailing_lines: list[str] | None, *, owner: str = "DOE JANE A",
          site: str = "13114 NE 144TH ST, BRUSH PRAIRIE, WA  98606",
          extra_echo: str | None = None) -> str:
    """A PIC page in the live markup. mailing_lines=None drops the mailing cell."""
    echo = (f'<td class="picBasicInfo1"> Property Identification Number: '
            f'<span class="picBasicInfo2">{pid}</span></td>')
    if extra_echo:
        echo += (f'<td class="picBasicInfo1"> Property Identification Number: '
                 f'<span class="picBasicInfo2">{extra_echo}</span></td>')
    mail = ""
    if mailing_lines is not None:
        body = "<br />\n\t\t\t\t".join(mailing_lines)
        mail = (f'<td class="picBasicInfo1" valign="top" width="34%">Owner Mailing Address<br />'
                f'\n\t\t<span class="picBasicInfo2">\n\t\t\t{body}\n      </span>\t\n    </td>')
    return f"""<html><body>
<table><tr>{echo}</tr></table>
<table class="picContainer"><tr>
  <td class="picBasicInfo1" valign="top" width="33%"><div id="gis-wrapper"><div style="float:left">
    Property Owner<br /><span class="picBasicInfo2">{owner}</span></div></div></td>
  {mail}
  <td class="picBasicInfo1" valign="top" width="33%">Property Site Address<br />
    <span class="picBasicInfo2">{site}</span></td>
</tr></table>
<div id="footerGISDisclaimer">RCW 42.56 prohibits releasing and/or using lists of individuals
gathered from this site for commercial purposes</div></body></html>"""


_NOT_FOUND = ("<html><body><h1>Property Information Center</h1>Street Address: Tax Account: "
              "Search Clear Form No Records Found. Searching Options (choose one)</body></html>")


class _Resp:
    def __init__(self, status: int, body: str = ""):
        self.status_code = status
        self.text = body


def _serve(monkeypatch, pages: dict):
    """Clark answers from `pages` (pid -> body | status | exception). Records asks."""
    asked: list[str] = []

    def _get(url, *, params=None, **kw):
        assert url == cp._URL
        pid = params["pid"]
        assert params["account"] == pid
        asked.append(pid)
        answer = pages[pid]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return _Resp(answer)
        return _Resp(200, answer)

    monkeypatch.setattr(cp, "safe_get", _get)
    return asked


# ─── Parser: the shapes the live site serves ─────────────────────────────────

class TestParsePage:
    def test_mailing_different_from_the_property(self):
        a = cp.parse_page(_page("105614454", ["800 NE TENNEY RD STE 110-310",
                                              "VANCOUVER WA \n , 98685"]), "105614454")
        assert a.outcome == "found"
        assert a.mailing_address == "800 NE TENNEY RD STE 110-310, VANCOUVER, WA 98685"

    def test_mailing_the_same_as_the_property_comes_from_the_mailing_cell(self):
        # The county printed the property's street as the mailing address, so it is
        # stored; the SITE cell is never read as mailing.
        a = cp.parse_page(_page("196948000", ["13114 NE 144TH ST",
                                              "BRUSH PRAIRIE WA \n , 98606", "US"],
                                site="1 SOMEWHERE ELSE, VANCOUVER, WA 98660"), "196948000")
        assert a.mailing_address == "13114 NE 144TH ST, BRUSH PRAIRIE, WA 98606"

    def test_out_of_state_mailing_is_kept(self):
        a = cp.parse_page(_page("37915015", ["4410 E CACTUS RD", "PHOENIX AZ , 85032-1234",
                                             "US"]), "37915015")
        assert a.mailing_address == "4410 E CACTUS RD, PHOENIX, AZ 85032-1234"

    def test_po_box(self):
        a = cp.parse_page(_page("37915015", ["PO BOX 1872", "BATTLE GROUND WA , 98604"]),
                          "37915015")
        assert a.mailing_address == "PO BOX 1872, BATTLE GROUND, WA 98604"

    def test_unit_is_preserved_as_the_county_printed_it(self):
        a = cp.parse_page(_page("97976264", ["311-T NE 85TH ST", "VANCOUVER WA , 98665",
                                             "US"]), "97976264")
        assert a.mailing_address == "311-T NE 85TH ST, VANCOUVER, WA 98665"
        b = cp.parse_page(_page("97976264", ["900 MAIN ST", "APT 4B", "VANCOUVER WA , 98660"]),
                          "97976264")
        assert b.mailing_address == "900 MAIN ST, APT 4B, VANCOUVER, WA 98660"

    def test_addressee_line_before_the_street_is_dropped(self):
        a = cp.parse_page(_page("110089668", ["JANE A DOE REVOCABLE LIVING TRUST",
                                              "14506 NE 31ST ST", "VANCOUVER WA , 98682"]),
                          "110089668")
        assert a.mailing_address == "14506 NE 31ST ST, VANCOUVER, WA 98682"

    def test_foreign_mailing_keeps_its_country(self):
        a = cp.parse_page(_page("37915015", ["12 HIGH ST", "LONDON SW1A 1AA",
                                             "UNITED KINGDOM"]), "37915015")
        assert a.mailing_address == "12 HIGH ST, LONDON SW1A 1AA, UNITED KINGDOM"

    def test_empty_mailing_cell_is_a_settled_none(self):
        a = cp.parse_page(_page("37915015", []), "37915015")
        assert a.outcome == "none" and a.mailing_address is None

    def test_parcel_not_found(self):
        assert cp.parse_page(_NOT_FOUND, "999999999").outcome == "parcel_not_found"

    def test_a_page_for_another_parcel_is_never_taken(self):
        a = cp.parse_page(_page("37915016", ["105 NE 89TH AVE", "VANCOUVER WA , 98664"]),
                          "37915015")
        assert a.outcome == "parcel_mismatch" and a.mailing_address is None

    def test_two_identification_blocks_are_ambiguous(self):
        a = cp.parse_page(_page("37915015", ["105 NE 89TH AVE", "VANCOUVER WA , 98664"],
                                extra_echo="12345678"), "37915015")
        assert a.outcome == "parcel_mismatch"

    def test_leading_zeros_do_not_break_identity(self):
        a = cp.parse_page(_page("037915015", ["105 NE 89TH AVE", "VANCOUVER WA , 98664"]),
                          cp.normalize_parcel("37915015"))
        assert a.outcome == "found"

    @pytest.mark.parametrize("body", [
        "<html><body>Service temporarily unavailable</body></html>",
        _page("37915015", None),                                    # mailing cell gone
        _page("37915015", ["SOMEONE", "VANCOUVER WA , 98664"]),     # no street line
        _page("37915015", ["105 NE 89TH AVE", "garbled locality"]),  # unreadable locality
    ])
    def test_unreadable_pages_are_unparsed_not_none(self, body):
        assert cp.parse_page(body, "37915015").outcome == "unparsed"


# ─── Resolver: pacing, lease, breaker, failure handling ──────────────────────

class TestResolveMailing:
    def test_answers_every_caller_spelling_with_one_request(self, monkeypatch):
        asked = _serve(monkeypatch, {"37915015": _page("37915015", [
            "105 NE 89TH AVE", "VANCOUVER WA , 98664"])})
        out = cp.resolve_mailing(["37915015", "037915015", "37915015"])
        assert asked == ["37915015"]
        assert {a.mailing_address for a in out.values()} == {
            "105 NE 89TH AVE, VANCOUVER, WA 98664"}

    def test_a_transient_timeout_is_retried_once(self, monkeypatch):
        asked = _serve(monkeypatch, {"37915015": [
            requests.Timeout(), _page("37915015", ["105 NE 89TH AVE", "VANCOUVER WA , 98664"])]})
        assert cp.resolve_mailing(["37915015"])["37915015"].outcome == "found"
        assert asked == ["37915015", "37915015"]

    def test_a_5xx_that_recovers_on_retry_is_an_answer(self, monkeypatch):
        asked = _serve(monkeypatch, {"37915015": [
            502, _page("37915015", ["105 NE 89TH AVE", "VANCOUVER WA , 98664"])]})
        assert cp.resolve_mailing(["37915015"])["37915015"].outcome == "found"
        assert asked == ["37915015", "37915015"]

    @pytest.mark.parametrize("failure", [requests.ConnectionError("reset"), 503,
                                         requests.exceptions.ChunkedEncodingError()])
    def test_a_failure_that_survives_its_retry_stops_everyone(self, monkeypatch, failure):
        """A real outage costs Clark at most two requests before every process stops."""
        from src.db.session import system_sync_session
        from src.scrapers.enrichment.source_health import is_source_available

        asked = _serve(monkeypatch, {"1111111": failure, "2222222": _page("2222222", [])})
        out = cp.resolve_mailing(["1111111", "2222222"])
        assert asked == ["1111111", "1111111"]
        # It cost a request, so it is not "never asked"; the next parcel was.
        assert out["1111111"].outcome == "request_failed"
        assert out["2222222"].outcome == "source_unavailable"
        with system_sync_session() as s:
            assert is_source_available(s, "clark_pic") is False

    def test_losing_the_lease_mid_retry_still_cools_the_source(self, monkeypatch):
        from src.db.session import system_sync_session
        from src.scrapers.enrichment.source_admission import SourceAdmission
        from src.scrapers.enrichment.source_health import is_source_available

        held = iter([True, False])  # held before the request, lost before the retry
        monkeypatch.setattr(SourceAdmission, "still_held", lambda self: next(held))
        asked = _serve(monkeypatch, {"1111111": requests.ConnectionError("reset")})
        assert cp.resolve_mailing(["1111111"])["1111111"].outcome == "request_failed"
        assert asked == ["1111111"]
        with system_sync_session() as s:
            assert is_source_available(s, "clark_pic") is False

    def test_a_client_error_is_not_retried(self, monkeypatch):
        asked = _serve(monkeypatch, {"1111111": 404})
        assert cp.resolve_mailing(["1111111"])["1111111"].outcome == "request_failed"
        assert asked == ["1111111"]

    @pytest.mark.parametrize("status", [429, 403])
    def test_a_block_stops_the_pass_and_cools_the_source_for_everyone(self, monkeypatch, status):
        from src.db.session import system_sync_session
        from src.scrapers.enrichment.source_health import is_source_available

        asked = _serve(monkeypatch, {"1111111": status, "2222222": _page("2222222", [])})
        out = cp.resolve_mailing(["1111111", "2222222"])
        assert asked == ["1111111"]  # stopped at the block, never asked again
        # The refused parcel spent its request; the one after it was never asked.
        assert out["1111111"].outcome == "request_failed"
        assert out["2222222"].outcome == "source_unavailable"
        with system_sync_session() as s:
            assert is_source_available(s, "clark_pic") is False
        # A later call (any process) makes no request while the cooldown holds.
        asked.clear()
        assert cp.resolve_mailing(["2222222"])["2222222"].outcome == "source_unavailable"
        assert asked == []

    def test_layout_drift_trips_the_breaker_instead_of_burning_every_parcel(self, monkeypatch):
        broken = "<html><body>new layout</body></html>"
        pids = [str(1000000 + i) for i in range(6)]
        asked = _serve(monkeypatch, dict.fromkeys(pids, broken))
        out = cp.resolve_mailing(pids)
        assert len(asked) == cp._UNPARSED_STREAK_LIMIT
        assert all(a.outcome in ("unparsed", "source_unavailable") for a in out.values())

    def test_no_lease_no_request(self, monkeypatch, redis_client):
        monkeypatch.setattr(cp, "_LEASE_WAIT_S", 0.0)
        redis_client.set("bl:source_admission:clark_pic", "someone-else", ex=60)
        asked = _serve(monkeypatch, {"37915015": _page("37915015", [])})
        assert cp.resolve_mailing(["37915015"])["37915015"].outcome == "source_unavailable"
        assert asked == []

    def test_the_budget_defers_what_it_cannot_reach(self, monkeypatch):
        asked = _serve(monkeypatch, {p: _page(p, []) for p in ("1111111", "2222222")})
        # The worst case per parcel must fit, the first parcel included (Codex P2).
        monkeypatch.setattr(cp, "_WORST_CASE_FETCH_S", 10.0)
        out = cp.resolve_mailing(["1111111", "2222222"], time_budget_s=5.0)
        assert asked == []
        assert {a.outcome for a in out.values()} == {"source_unavailable"}

    def test_every_request_is_paced_including_the_first(self, monkeypatch):
        """The pause before the FIRST request is what spaces this pass from the
        previous lease holder's last one (Codex P1)."""
        events: list[str] = []
        monkeypatch.setattr(cp, "_PACE_S", 6.0)
        monkeypatch.setattr(cp.time, "sleep", lambda s: events.append(f"sleep {s:g}"))
        pages = {p: _page(p, []) for p in ("1111111", "2222222")}
        _serve(monkeypatch, pages)
        real_get = cp.safe_get
        monkeypatch.setattr(cp, "safe_get", lambda url, **kw: (
            events.append("GET"), real_get(url, **kw))[1])
        cp.resolve_mailing(list(pages))
        assert events == ["sleep 6", "GET", "sleep 6", "GET"]

    def test_a_malformed_parcel_is_never_requested(self, monkeypatch):
        asked = _serve(monkeypatch, {})
        assert cp.resolve_mailing(["ABC-12"])["ABC-12"].outcome == "parcel_not_found"
        assert asked == []


# ─── county_gis hook ─────────────────────────────────────────────────────────

class TestCountyGisHook:
    def test_kill_switch_off_means_no_clark_request(self, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", False)
        asked = _serve(monkeypatch, {})
        out = cg.batch_enrich_parcels_gis(["196948000"], "clark", "WA", stats={})
        assert asked == []
        assert out["196948000"]["mailing_address"] is None
        assert cg.has_mailing_source("clark", "WA") is False

    def test_found_settled_and_deferred_are_three_different_things(self, monkeypatch):
        _serve(monkeypatch, {
            "196948000": _page("196948000", ["PO BOX 77", "BOISE ID , 83701"]),
            "37915015": _page("37915015", []),
            "1111111": requests.ConnectionError("reset"),
        })
        stats: dict = {}
        out = cg.batch_enrich_parcels_gis(["196948000", "37915015", "1111111"],
                                          "clark", "WA", stats=stats)
        found = out["196948000"]
        assert found["mailing_address"] == "PO BOX 77, BOISE, ID 83701"
        assert found["mailing_source"] == "clark_pic"
        # The situs from the statewide layer survives; mailing never copies it.
        assert found["property_address"] == "13114 NE 144TH ST"
        assert out["37915015"]["mailing_address"] is None
        assert out["37915015"]["mailing_lookup"] == "none"
        assert stats["county_unreached"] == ["1111111"]


# ─── Live job pass + recovery sweep, against the real DB ─────────────────────

async def _job(db, user, *, status: str) -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Clark probate",
        county="clark", state="WA", record_type="probate",
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


async def _row(db, user, job_id: str, *, parcel: str, mailing: str | None = None,
               duplicate: bool = False, enrichment: dict | None = None) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(
        id=rid, user_id=user.id, job_id=job_id, party_name="DOE JANE A",
        doc_type="LACK OF PROBATE AFFIDAVIT", parcel_id=parcel,
        property_address="13114 NE 144TH ST", property_city="BRUSH PRAIRIE",
        property_state="WA", property_zip="98606", mailing_address=mailing,
        enrichment_data=enrichment or {"source": "clark_county_recorder"},
        skip_trace_status="not_attempted", is_duplicate=duplicate,
    ))
    await db.commit()
    return rid


async def _get(db, rid: str):
    return (await db.execute(text(
        "SELECT mailing_address, enrichment_data, owner_state, absentee_owner, "
        "out_of_state_owner, skip_trace_status FROM results WHERE id = :i"), {"i": rid})).first()


async def _run_job_enrichment(db, job_id, redis_client):
    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _run_inline_enrichment

        with system_sync_session() as sdb:
            job = sdb.get(Job, job_id)
            config = sdb.get(ScraperConfig, job.scraper_config_id)
            summary: dict = {}
            _run_inline_enrichment(sdb, job, redis_client, job_id, config, summary=summary)
            return summary

    return await asyncio.to_thread(_go)


@pytest.mark.asyncio
class TestJobAndRecovery:
    async def test_job_pass_fills_settles_and_defers(self, db, business_user, redis_client,
                                                      monkeypatch):
        job_id = await _job(db, business_user, status="enriching")
        found = await _row(db, business_user, job_id, parcel="196948000")
        none = await _row(db, business_user, job_id, parcel="37915015")
        down = await _row(db, business_user, job_id, parcel="1111111")
        _serve(monkeypatch, {
            "196948000": _page("196948000", ["4410 E CACTUS RD", "PHOENIX AZ , 85032"]),
            "37915015": _page("37915015", []),
            # Unreadable, not failing: a failure would end the pass and make which
            # parcels were asked depend on row order.
            "1111111": "<html><body>Service temporarily unavailable</body></html>",
        })
        summary = await _run_job_enrichment(db, job_id, redis_client)

        f = await _get(db, found)
        assert f.mailing_address == "4410 E CACTUS RD, PHOENIX, AZ 85032"
        assert f.enrichment_data["mailing_source"] == "clark_pic"
        n = await _get(db, none)
        assert n.mailing_address is None
        assert n.enrichment_data["mailing_recovery_outcome"] == "none"
        assert "mailing_lookup_deferred" not in n.enrichment_data
        d = await _get(db, down)
        assert d.mailing_address is None
        assert d.enrichment_data["mailing_lookup_deferred"] is True
        assert summary["mailing_deferred"] == 1
        # Enrichment never buys a skip trace on its own.
        assert {f.skip_trace_status, n.skip_trace_status, d.skip_trace_status} == {"not_attempted"}

    async def test_an_existing_mailing_address_is_never_overwritten(
        self, db, business_user, redis_client, monkeypatch,
    ):
        job_id = await _job(db, business_user, status="enriching")
        rid = await _row(db, business_user, job_id, parcel="196948000",
                         mailing="PO BOX 5, YACOLT, WA 98675")
        # The row still needs a property address, so the sweep reaches it.
        await db.execute(text("UPDATE results SET property_address = NULL WHERE id = :i"),
                         {"i": rid})
        await db.commit()
        _serve(monkeypatch, {"196948000": _page("196948000", [])})
        await _run_job_enrichment(db, job_id, redis_client)
        assert (await _get(db, rid)).mailing_address == "PO BOX 5, YACOLT, WA 98675"

    async def test_a_duplicate_missing_mailing_is_enriched_without_new_billing(
        self, db, business_user, redis_client, monkeypatch,
    ):
        """Already delivered once, no mailing then. A later run's duplicate row must
        still get the address, and the run's billing must not move."""
        old_job = await _job(db, business_user, status="done")
        await _row(db, business_user, old_job, parcel="196948000")
        job_id = await _job(db, business_user, status="enriching")
        dup = await _row(db, business_user, job_id, parcel="196948000", duplicate=True)
        _serve(monkeypatch, {"196948000": _page("196948000", ["PO BOX 77", "BOISE ID , 83701"])})
        await _run_job_enrichment(db, job_id, redis_client)

        assert (await _get(db, dup)).mailing_address == "PO BOX 77, BOISE, ID 83701"
        billed = (await db.execute(text(
            "SELECT billed_count, record_count FROM jobs WHERE id = :j"), {"j": job_id})).first()
        assert (billed.billed_count, billed.record_count) == (1, 1)

    async def test_recovery_fills_a_historical_row_and_records_why_others_stopped(
        self, db, business_user, monkeypatch,
    ):
        job_id = await _job(db, business_user, status="done")
        deferred = {"source": "clark_county_recorder", "mailing_lookup_deferred": True}
        found = await _row(db, business_user, job_id, parcel="196948000", enrichment=deferred)
        missing = await _row(db, business_user, job_id, parcel="999999999", enrichment=deferred)
        moved = await _row(db, business_user, job_id, parcel="37915015", enrichment=deferred)
        garbled = await _row(db, business_user, job_id, parcel="2222222", enrichment=deferred)
        _serve(monkeypatch, {
            "196948000": _page("196948000", ["PO BOX 77", "BOISE ID , 83701"]),
            "999999999": _NOT_FOUND,
            "37915015": _page("37915016", ["1 ELSEWHERE", "VANCOUVER WA , 98660"]),
            "2222222": _page("2222222", ["SOMEONE", "VANCOUVER WA , 98664"]),
        })
        await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        f = await _get(db, found)
        assert f.mailing_address == "PO BOX 77, BOISE, ID 83701"
        assert f.enrichment_data["mailing_recovery_outcome"] == "found"
        assert f.enrichment_data["mailing_source"] == "clark_pic"
        assert f.owner_state == "ID" and f.out_of_state_owner is True and f.absentee_owner is True
        m = await _get(db, missing)
        assert m.mailing_address is None
        assert m.enrichment_data["mailing_recovery_outcome"] == "parcel_not_found"
        assert m.enrichment_data["mailing_lookup_deferred"] is False
        # A settled "no address" still says which source settled it.
        assert m.enrichment_data["mailing_source"] == "clark_pic"
        x = await _get(db, moved)
        assert x.mailing_address is None
        assert x.enrichment_data["mailing_recovery_outcome"] == "parcel_mismatch"
        # Fetched but unreadable: retried, but it spends an attempt (ends at the ceiling).
        g = await _get(db, garbled)
        assert g.mailing_address is None
        assert g.enrichment_data["mailing_recovery_outcome"] == "error"
        assert g.enrichment_data["mailing_recovery_attempts"] == 1
        assert g.enrichment_data["mailing_lookup_deferred"] is True

    async def test_recovery_charges_a_failed_request_and_stops(self, db, business_user,
                                                                monkeypatch):
        job_id = await _job(db, business_user, status="done")
        rid = await _row(db, business_user, job_id, parcel="1111111",
                         enrichment={"mailing_lookup_deferred": True})
        _serve(monkeypatch, {"1111111": requests.ConnectionError("reset")})
        await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        d = await _get(db, rid)
        assert d.mailing_address is None
        assert d.enrichment_data["mailing_lookup_deferred"] is True
        assert d.enrichment_data["mailing_recovery_outcome"] == "error"
        assert d.enrichment_data["mailing_recovery_attempts"] == 1

    async def test_recovery_during_a_cooldown_asks_nothing_and_charges_nothing(
        self, db, business_user, monkeypatch,
    ):
        from src.scrapers.enrichment.source_health import record_source_blocked

        job_id = await _job(db, business_user, status="done")
        rid = await _row(db, business_user, job_id, parcel="196948000",
                         enrichment={"mailing_lookup_deferred": True})
        await asyncio.to_thread(record_source_blocked, "clark_pic", "HTTP 429")
        asked = _serve(monkeypatch, {"196948000": _page("196948000", ["PO BOX 77",
                                                                      "BOISE ID , 83701"])})
        await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        assert asked == []
        row = await _get(db, rid)
        assert row.mailing_address is None
        assert row.enrichment_data["mailing_lookup_deferred"] is True
        assert "mailing_recovery_attempts" not in row.enrichment_data

    async def test_requeue_accepts_clark_and_marks_only_unconcluded_rows(
        self, db, business_user,
    ):
        from scripts.requeue_gis_mailing_recovery import requeue

        from src.db.session import system_sync_session

        job_id = await _job(db, business_user, status="done")
        fresh = await _row(db, business_user, job_id, parcel="196948000")
        settled = await _row(db, business_user, job_id, parcel="37915015", enrichment={
            "mailing_recovery_outcome": "none"})
        has_mail = await _row(db, business_user, job_id, parcel="97976264",
                              mailing="PO BOX 5, YACOLT, WA 98675")

        def _go():
            with system_sync_session() as s:
                return requeue(s, ["clark"], apply=True)

        stats = await asyncio.to_thread(_go)
        assert stats["marked"] == 1
        assert (await _get(db, fresh)).enrichment_data["mailing_lookup_deferred"] is True
        assert "mailing_lookup_deferred" not in (await _get(db, settled)).enrichment_data
        assert "mailing_lookup_deferred" not in (await _get(db, has_mail)).enrichment_data


# ─── What the customer receives ──────────────────────────────────────────────

@pytest.mark.parametrize(("mailing", "parts"), [
    ("4410 E CACTUS RD, PHOENIX, AZ 85032-1234", ("4410 E CACTUS RD", "PHOENIX", "AZ")),
    ("PO BOX 1872, BATTLE GROUND, WA 98604", ("PO BOX 1872", "BATTLE GROUND", "WA")),
])
def test_csv_splits_the_clark_mailing_into_its_columns(mailing, parts):
    from src.utils.lead_export import build_lead_export_row

    row = build_lead_export_row({
        "party_name": "DOE JANE A", "parcel_id": "196948000",
        "property_address": "13114 NE 144TH ST", "mailing_address": mailing,
    }, context={"county": "clark", "state": "WA", "record_type": "probate"})
    assert row["mailing_address"] == mailing
    assert (row["mailing_street"], row["mailing_city"], row["mailing_state"]) == parts
    assert row["mailing_zip"] and row["mailing_zip"] in mailing


@pytest.mark.asyncio
async def test_api_returns_the_persisted_clark_mailing(client, db, business_user,
                                                       business_token):
    job_id = await _job(db, business_user, status="done")
    await _row(db, business_user, job_id, parcel="196948000",
               mailing="4410 E CACTUS RD, PHOENIX, AZ 85032",
               enrichment={"mailing_source": "clark_pic"})
    resp = await client.get(f"/jobs/{job_id}/results",
                            headers={"Authorization": f"Bearer {business_token}"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["mailing_address"] for i in items] == ["4410 E CACTUS RD, PHOENIX, AZ 85032"]
