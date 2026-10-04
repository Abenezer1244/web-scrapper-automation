"""PACS owner-name lookup: follow the search redirect, and tell a failure from a miss.

Island probate job 6b1f3445 (2026-10-03) looked up 71 owner names and found 0 in
23 s. The portal answers the search POST with a 302 to SearchResults.aspx; the name
path posted with allow_redirects=False and read that 302 as "no match", so every
lookup failed silently and the run closed on "Enrichment complete: addresses added".

The county portal is the external boundary, so it is played by a scripted session
(the same seam test_pacs_parcel uses). Everything above it is the real code: the
form scrape, ``post_search``, the results parser, the owner-name guard, the detail
fetch and the outcome mapping. The SSRF validator is recorded, not skipped: every
test asserts it ran on the portal URL before any request.
"""
import pytest

from src.scrapers.enrichment import pacs
from src.scrapers.enrichment.pacs import (
    LOOKUP_FAILED,
    LOOKUP_FOUND,
    LOOKUP_NO_MATCH,
    LOOKUP_SKIPPED,
    batch_lookup_pacs_by_name,
    lookup_pacs_by_name,
    post_search,
)

URL = "https://assessor.islandcountywa.gov/propertyaccess/PropertySearch.aspx?cid=0"
RESULTS = "https://assessor.islandcountywa.gov/propertyaccess/SearchResults.aspx?cid=0"
FORM = '<form><input name="__VIEWSTATE" value="VS1" /><input name="__EVENTVALIDATION" value="EV1" /></form>'
ERROR_PAGE = "/propertyaccess/customdisplay.htm?aspxerrorpath=/propertyaccess/PropertySearch.aspx"


def _grid(*owners: str) -> str:
    rows = "".join(
        f"<tr><td><input type=checkbox></td><td>R1234{i}5</td><td>R1234{i}6789</td><td>Real</td>"
        f"<td>0010</td><td>{100 + i} PINE RD, LANGLEY WA 98260</td><td>LOT 1</td><td>{owner}</td>"
        f"<td>$250,000</td><td><a href='Property.aspx?cid=0&prop_id={58808 + i}'>View</a></td></tr>"
        for i, owner in enumerate(owners)
    )
    return f"<html><table id='propertySearchResults_resultsTable'><tr><th>Owner</th></tr>{rows}</table></html>"


DETAIL = (
    "<html><body><table><tr><td>Property ID:</td><td>58808</td></tr>"
    "<tr><td>Parcel # / Geo ID:</td><td>R12340</td></tr>"
    "<tr><td>Address:</td><td>100 PINE RD <BR> LANGLEY, WA 98260</td></tr>"
    "<tr><td>Name:</td><td>DOE JANE</td></tr>"
    "<tr><td>Mailing Address:</td><td>PO BOX 9 <BR> CLINTON, WA 98236</td></tr>"
    "</table></body></html>"
)


class _Resp:
    def __init__(self, status: int, text: str = "", location: str | None = None):
        self.status_code, self.text = status, text
        self.headers = {"Location": location} if location else {}


class _Portal:
    """One scripted portal session. ``post`` is what the search POST answers;
    ``pages`` maps a GET URL to its response."""

    def __init__(self, post: _Resp, pages: dict[str, _Resp] | None = None, form: _Resp | None = None):
        self.post_resp = post
        self.pages = {URL: form or _Resp(200, FORM), **(pages or {})}
        self.headers: dict = {}
        self.calls: list[tuple] = []

    def get(self, url, **kw):
        assert kw.get("allow_redirects") is False, "a redirect must never be followed blindly"
        self.calls.append(("GET", url))
        return self.pages.get(url, _Resp(404))

    def post(self, url, data=None, **kw):
        assert kw.get("allow_redirects") is False
        self.calls.append(("POST", url, data))
        return self.post_resp


@pytest.fixture
def portal(monkeypatch):
    validated: list[str] = []
    holder: dict = {}

    def install(p: _Portal) -> _Portal:
        holder["p"] = p
        return p

    monkeypatch.setattr(pacs, "validate_scraping_target", lambda url, **kw: validated.append(url))
    monkeypatch.setattr(pacs, "pinned_session", lambda: holder["p"])
    install.validated = validated
    return install


def _redirect_to(page: _Resp) -> _Portal:
    return _Portal(_Resp(302, location="SearchResults.aspx?cid=0"), {RESULTS: page,
                   "https://assessor.islandcountywa.gov/propertyaccess/Property.aspx?cid=0&prop_id=58808":
                   _Resp(200, DETAIL)})


def test_a_redirected_search_is_followed_and_answers(portal):
    """The Island regression: the POST's 302 used to be read as no match."""
    p = portal(_redirect_to(_Resp(200, _grid("DOE JANE"))))
    outcome, result = lookup_pacs_by_name(URL, "DOE JANE")
    assert outcome == LOOKUP_FOUND
    assert result == {"address": "100 PINE RD, LANGLEY WA 98260", "value": "$250,000",
                      "mailing": "PO BOX 9, CLINTON, WA 98236"}
    assert "parcel_id" not in result  # an owner-name match never sets identity
    assert ("GET", RESULTS) in p.calls
    assert portal.validated == [URL]


def test_none_found_is_a_miss_not_a_failure(portal):
    portal(_redirect_to(_Resp(200, "<html><body>None found</body></html>")))
    assert lookup_pacs_by_name(URL, "DOE JANE") == (LOOKUP_NO_MATCH, None)


def test_several_properties_under_the_name_is_a_miss(portal):
    portal(_redirect_to(_Resp(200, _grid("DOE JANE", "DOE JANE"))))
    assert lookup_pacs_by_name(URL, "DOE JANE") == (LOOKUP_NO_MATCH, None)


def test_a_row_for_another_owner_is_a_miss(portal):
    portal(_redirect_to(_Resp(200, _grid("DOE JANET"))))
    assert lookup_pacs_by_name(URL, "DOE JANE") == (LOOKUP_NO_MATCH, None)


@pytest.mark.parametrize("portal_state", [
    # The results GET bounced to the ASP.NET error page (seen live 2026-10-03).
    _Portal(_Resp(302, location="SearchResults.aspx?cid=0"), {RESULTS: _Resp(302, location=ERROR_PAGE)}),
    # The search form itself bounced to the error page.
    _Portal(_Resp(200), form=_Resp(302, location=ERROR_PAGE)),
    # A form with no view state: not the search form.
    _Portal(_Resp(200), form=_Resp(200, "<html>maintenance</html>")),
    # A 200 that is neither a grid nor "None found".
    _Portal(_Resp(200, "<html>Session expired</html>")),
    # A 307 asks for the POST to be replayed: never turned into a GET.
    _Portal(_Resp(307, location="SearchResults.aspx?cid=0")),
    # A server error.
    _Portal(_Resp(500)),
], ids=["results-error-page", "form-error-page", "no-viewstate", "unknown-200", "307", "500"])
def test_a_portal_that_did_not_answer_is_a_failure(portal, portal_state):
    portal(portal_state)
    assert lookup_pacs_by_name(URL, "DOE JANE") == (LOOKUP_FAILED, None)


def test_an_off_origin_redirect_is_refused_and_never_fetched(portal):
    p = portal(_Portal(_Resp(302, location="https://169.254.169.254/latest/meta-data")))
    assert lookup_pacs_by_name(URL, "DOE JANE") == (LOOKUP_FAILED, None)
    assert not any(c[0] == "GET" and "169.254" in c[1] for c in p.calls)


def test_a_raised_error_is_a_failure(portal, monkeypatch):
    def boom(url, **kw):
        raise ValueError("blocked address")
    monkeypatch.setattr(pacs, "validate_scraping_target", boom)
    portal(_redirect_to(_Resp(200, _grid("DOE JANE"))))
    assert lookup_pacs_by_name(URL, "DOE JANE") == (LOOKUP_FAILED, None)


def test_plaintext_and_empty_inputs_are_failures_without_a_request(portal):
    p = portal(_redirect_to(_Resp(200, _grid("DOE JANE"))))
    assert lookup_pacs_by_name(URL.replace("https", "http"), "DOE JANE") == (LOOKUP_FAILED, None)
    assert lookup_pacs_by_name(URL, "") == (LOOKUP_FAILED, None)
    assert p.calls == []


def test_batch_keeps_order_and_outcomes(portal, monkeypatch):
    answers = {"A": (LOOKUP_FOUND, {"address": "1 A ST"}), "B": (LOOKUP_NO_MATCH, None)}

    def fake(url, name):
        if name == "C":
            raise RuntimeError("worker died")
        return answers[name]

    monkeypatch.setattr(pacs, "lookup_pacs_by_name", fake)
    monkeypatch.setattr(pacs, "NAME_PACE_S", 0.0)
    monkeypatch.setattr(pacs, "NAME_JITTER_S", 0.0)
    assert batch_lookup_pacs_by_name(URL, ["A", "B", "C"]) == [
        (LOOKUP_FOUND, {"address": "1 A ST"}), (LOOKUP_NO_MATCH, None), (LOOKUP_FAILED, None)]
    assert batch_lookup_pacs_by_name("", ["A"]) == [(LOOKUP_SKIPPED, None)]


def test_post_search_follows_one_same_origin_hop():
    s = _Portal(_Resp(303, location="/propertyaccess/SearchResults.aspx?cid=0"), {RESULTS: _Resp(200, "grid")})
    assert post_search(s, URL, {}, 5).text == "grid"
    off = _Portal(_Resp(302, location="https://evil.example/x"))
    assert post_search(off, URL, {}, 5).status_code == 400
    assert [c for c in off.calls if c[0] == "GET"] == []


def test_the_completion_line_no_longer_says_addresses_added_over_none():
    """Job 6b1f3445 closed on "Enrichment complete: addresses added" with 71 of 71
    records holding no address and every lookup failed."""
    from src.workers.tasks_helpers.enrich import enrichment_completion_log

    level, msg = enrichment_completion_log({"no_address": 71, "name_lookup_failed": 71})
    assert level == "info"
    assert "addresses added" not in msg
    assert "71 records have no property or mailing address." in msg
    assert "71 address lookups failed." in msg
    _, one = enrichment_completion_log({"no_address": 1, "name_lookup_failed": 1})
    assert "1 record has no property" in one and "1 address lookup failed" in one
    # A miss is not a failure: no-address rows alone do not claim the site was down.
    _, miss = enrichment_completion_log({"no_address": 3})
    assert "failed" not in miss
    assert enrichment_completion_log({}) == ("success", "Enrichment complete: addresses added")


# ─── The live job pass, against the real DB ──────────────────────────────────

async def test_the_job_pass_records_each_rows_outcome_and_counts_failures(
        db, business_user, redis_client, monkeypatch):
    """Parcel-less Island probate rows go through the owner-name pass. Each row gets
    its outcome; a failure is counted apart from a miss; the found row is filled."""
    import asyncio
    import uuid

    from sqlalchemy import text

    from src.db.models import Job, Result, ScraperConfig

    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=business_user.id, name="island probate name pass",
        county="island", state="WA", record_type="probate", fields=["party_name"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=business_user.id, scraper_config_id=config.id,
               status="enriching", trigger="manual", record_count=0, billed_count=0))
    await db.commit()
    ids = {}
    for name in ("FOUND ANN", "MISS BOB", "FAIL CAL"):
        ids[name] = str(uuid.uuid4())
        db.add(Result(id=ids[name], user_id=business_user.id, job_id=job_id, party_name=name,
                      doc_type="CERTIFICATE OF DEATH", parcel_id=None, property_address=None,
                      mailing_address=None, enrichment_data={"instrument_number": "1"},
                      skip_trace_status="not_attempted"))
    await db.commit()

    asked: list = []

    def scripted(url, names, max_workers=5):
        asked.append((url, list(names)))
        by_name = {"FOUND ANN": (LOOKUP_FOUND, {"address": "1 FIR LN", "mailing": "PO BOX 1, CLINTON, WA 98236"}),
                   "MISS BOB": (LOOKUP_NO_MATCH, None), "FAIL CAL": (LOOKUP_FAILED, None)}
        return [by_name[n] for n in names]

    monkeypatch.setattr(pacs, "batch_lookup_pacs_by_name", scripted)

    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _run_inline_enrichment

        with system_sync_session() as sdb:
            job = sdb.get(Job, job_id)
            cfg = sdb.get(ScraperConfig, job.scraper_config_id)
            summary: dict = {}
            _run_inline_enrichment(sdb, job, redis_client, job_id, cfg, summary=summary)
            return summary

    summary = await asyncio.to_thread(_go)

    assert len(asked) == 1 and sorted(asked[0][1]) == ["FAIL CAL", "FOUND ANN", "MISS BOB"]
    rows = {}
    for name, rid in ids.items():
        rows[name] = (await db.execute(text(
            "SELECT property_address, mailing_address, parcel_id, enrichment_data "
            "FROM results WHERE id = :i"), {"i": rid})).first()
    assert rows["FOUND ANN"].property_address == "1 FIR LN"
    assert rows["FOUND ANN"].mailing_address == "PO BOX 1, CLINTON, WA 98236"
    assert rows["FOUND ANN"].parcel_id is None  # never a parcel from a name
    assert {n: r.enrichment_data.get(pacs.PACS_NAME_LOOKUP_KEY) for n, r in rows.items()} == {
        "FOUND ANN": LOOKUP_FOUND, "MISS BOB": LOOKUP_NO_MATCH, "FAIL CAL": LOOKUP_FAILED}
    # Existing markers survive the write.
    assert all(r.enrichment_data.get("instrument_number") == "1" for r in rows.values())
    assert summary["name_lookup_failed"] == 1
    assert summary["no_address"] == 2

    logs = [m for (m,) in (await db.execute(text(
        "SELECT message FROM job_logs WHERE job_id = :j"), {"j": job_id})).all()]
    assert any("Found 1/3 addresses via PACS (1 lookup failed: the county site did not answer)" in m
               for m in logs), logs


def test_the_no_address_count_ignores_a_quota_marker():
    """A retried job can carry an earlier attempt's over-quota marker. That is not a
    missing address, so the completion count must use the address half only."""
    from src.api.lead_actionability import has_address, is_actionable

    capped = {"property_address": "1 FIR LN", "mailing_address": None,
              "enrichment_data": {"delivery_excluded_reason": "over_quota"}}
    assert has_address(capped) and not is_actionable(capped)
    for empty in (None, "", "   ", "(enrichment unavailable)"):
        assert not has_address({"property_address": empty, "mailing_address": empty})
    assert has_address({"property_address": None, "mailing_address": "PO BOX 1"})


# ─── Pacing: one request at a time, a budget, and a breaker ─────────────────

def test_the_pass_is_sequential_and_paced(monkeypatch):
    """It used to fire 5 unpaced workers at a county portal. Now one at a time, with
    the pace between names (never before the first)."""
    events: list = []
    monkeypatch.setattr(pacs, "lookup_pacs_by_name", lambda url, n: events.append(('ask', n)) or (LOOKUP_NO_MATCH, None))
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: events.append(('sleep', round(s, 1))))
    monkeypatch.setattr(pacs, "NAME_PACE_S", 3.0)
    monkeypatch.setattr(pacs, "NAME_JITTER_S", 0.0)
    batch_lookup_pacs_by_name(URL, ["A", "B", "C"])
    assert events == [('ask', 'A'), ('sleep', 3.0), ('ask', 'B'), ('sleep', 3.0), ('ask', 'C')]


def test_a_portal_that_keeps_failing_stops_the_pass(monkeypatch):
    """Island answers every request with its maintenance page while offline. After
    NAME_BREAKER failures in a row the rest are skipped, not hammered."""
    asked: list = []
    monkeypatch.setattr(pacs, "lookup_pacs_by_name", lambda url, n: asked.append(n) or (LOOKUP_FAILED, None))
    monkeypatch.setattr(pacs, "NAME_PACE_S", 0.0)
    monkeypatch.setattr(pacs, "NAME_JITTER_S", 0.0)
    out = batch_lookup_pacs_by_name(URL, [f"N{i}" for i in range(10)])
    assert asked == ["N0", "N1", "N2"]
    assert [o for o, _ in out] == [LOOKUP_FAILED] * 3 + [LOOKUP_SKIPPED] * 7


def test_an_answer_resets_the_breaker(monkeypatch):
    seq = iter([LOOKUP_FAILED, LOOKUP_FAILED, LOOKUP_NO_MATCH, LOOKUP_FAILED, LOOKUP_FAILED, LOOKUP_FOUND])
    monkeypatch.setattr(pacs, "lookup_pacs_by_name", lambda url, n: (next(seq), None))
    monkeypatch.setattr(pacs, "NAME_PACE_S", 0.0)
    monkeypatch.setattr(pacs, "NAME_JITTER_S", 0.0)
    out = batch_lookup_pacs_by_name(URL, list("abcdef"))
    assert LOOKUP_SKIPPED not in [o for o, _ in out]


def test_names_past_the_budget_are_skipped_not_failed(monkeypatch):
    import time as _time
    # deadline, check A, check B, after-pace B, check C -> past the budget
    clock = iter([0.0, 0.0, 10.0, 10.0, 99999.0, 99999.0])
    monkeypatch.setattr(_time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(pacs, "lookup_pacs_by_name", lambda url, n: (LOOKUP_NO_MATCH, None))
    monkeypatch.setattr(pacs, "NAME_PACE_S", 0.0)
    monkeypatch.setattr(pacs, "NAME_JITTER_S", 0.0)
    out = batch_lookup_pacs_by_name(URL, ["A", "B", "C"])
    assert [o for o, _ in out] == [LOOKUP_NO_MATCH, LOOKUP_NO_MATCH, LOOKUP_SKIPPED]


def test_the_completion_line_names_records_not_looked_up():
    from src.workers.tasks_helpers.enrich import enrichment_completion_log

    level, msg = enrichment_completion_log({"name_lookup_skipped": 7})
    assert level == "info" and "7 records were not looked up." in msg
    assert "1 record was not looked up." in enrichment_completion_log({"name_lookup_skipped": 1})[1]


def test_a_pace_that_crosses_the_deadline_asks_no_more(monkeypatch):
    import time as _time
    clock = iter([0.0, 0.0, 10.0, 99999.0])  # deadline, check A, check B, after-pace B
    monkeypatch.setattr(_time, "monotonic", lambda: next(clock))
    asked: list = []
    monkeypatch.setattr(pacs, "lookup_pacs_by_name", lambda url, n: asked.append(n) or (LOOKUP_NO_MATCH, None))
    monkeypatch.setattr(pacs, "NAME_PACE_S", 0.0)
    monkeypatch.setattr(pacs, "NAME_JITTER_S", 0.0)
    out = batch_lookup_pacs_by_name(URL, ["A", "B"])
    assert asked == ["A"] and [o for o, _ in out] == [LOOKUP_NO_MATCH, LOOKUP_SKIPPED]
