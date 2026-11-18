"""P0-08: the connector canary no longer certifies a source that returns nothing.

Real county_connectors rows and the real registry resolution; only the portal
fetch (`_canary_scrape`, the external county site) is replaced, by a table of
what each window returns. A window whose answer is an exception raises it.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.db.models import CountyConnector
from src.db.session import SyncSessionLocal
from src.workers.scheduler_helpers import health

_TODAY = datetime.now(UTC).date()
_THIS_WEEK = (_TODAY - timedelta(days=7)).strftime("%m/%d/%Y")


@pytest.fixture
def connector():
    """make(record_types, status) -> a real active connector row, loaded."""
    made: list[str] = []
    session = SyncSessionLocal()

    def make(record_types=("probate",), status="healthy"):
        county = f"canary-{uuid.uuid4().hex[:8]}"
        c = CountyConnector(
            id=str(uuid.uuid4()), county=county, state="WA", record_types=list(record_types),
            scraper_class="src.scrapers.base_scraper.BridgeScraper", scraper_mode="manual",
            base_url=f"https://{county}.example.gov", health_status=status, active=True,
        )
        session.add(c)
        session.commit()
        made.append(c.id)
        return c

    yield make
    session.close()
    with SyncSessionLocal() as s:
        s.execute(text("DELETE FROM county_connectors WHERE id = ANY(CAST(:c AS uuid[]))"),
                  {"c": made})
        s.commit()


def _portal(monkeypatch, *, now, history):
    """The current week returns ``now``; every historical window returns ``history``."""
    calls: list[str] = []

    async def fetch(_scraper_class, date_from, _date_to, record_type=None):
        calls.append(date_from)
        answer = now if date_from == _THIS_WEEK else history
        if callable(answer):
            answer = answer(record_type)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(health, "_canary_scrape", fetch)
    return calls


def _probe(c, record_type="probate"):
    """One unit as the run does it: probe, record that type's own verdict, and a DOWN
    page delivered."""
    alerts: list = []
    mark = health._probe_unit(c, record_type, alerts)
    alerts += health._apply_mark(c, record_type, mark)
    if any(kind == "canary" for kind, *_ in alerts):
        health._mark_down_paged(health._bad_key(c, record_type))
    return c.health_status, [(kind, key) for kind, key, _s, _b in alerts]


def test_a_source_that_yields_stays_healthy(connector, monkeypatch):
    _portal(monkeypatch, now=["r"], history=[])
    assert _probe(connector()) == ("healthy", [])


def test_a_quiet_week_with_history_stays_healthy(connector, monkeypatch):
    _portal(monkeypatch, now=[], history=["r"])
    assert _probe(connector()) == ("healthy", [])


def test_a_healthy_source_that_returns_nothing_anywhere_is_downgraded_and_paged(
        connector, monkeypatch):
    """It used to stay 'healthy' whatever the probe found (sticky health)."""
    _portal(monkeypatch, now=[], history=[])
    c = connector()
    assert _probe(c) == ("degraded", [("canary_zero_output", f"{c.county}/WA/probate")])


def test_a_history_probe_that_errored_proves_nothing(connector, monkeypatch):
    _portal(monkeypatch, now=[], history=TimeoutError("portal slow"))
    assert _probe(connector()) == ("healthy", [])


def test_an_empty_trustee_sale_probe_is_not_zero_output(connector, monkeypatch):
    """Zero upcoming auctions is legitimate, and its windows are not history."""
    calls = _portal(monkeypatch, now=[], history=[])
    c = connector(record_types=("trustee_sale",))
    assert _probe(c, "trustee_sale") == ("healthy", [])
    assert calls == [_THIS_WEEK]  # no history probe


def test_a_broken_probe_is_down_and_paged_once(connector, monkeypatch):
    _portal(monkeypatch, now=RuntimeError("Jane Q Canary, 12 Elm St"), history=[])
    c = connector()
    status, alerts = _probe(c)
    assert (status, alerts) == ("down", [("canary", f"{c.county}/WA/probate")])
    assert _probe(c) == ("down", [])  # already down: no second page


def test_a_record_type_with_no_scraper_is_down_and_paged(connector, monkeypatch):
    """It used to go 'down' with no alert (Codex P2)."""
    _portal(monkeypatch, now=["r"], history=[])
    c = connector(record_types=("probate",))
    assert _probe(c, "divorce") == ("down", [("canary", f"{c.county}/WA/divorce")])


def test_a_connector_that_never_yielded_is_paged_too(connector, monkeypatch):
    """Listed to customers and returns nothing anywhere: worth a page."""
    _portal(monkeypatch, now=[], history=[])
    c = connector(status="unknown")
    assert _probe(c) == ("degraded", [("canary_zero_output", f"{c.county}/WA/probate")])


def test_unproven_with_an_unreadable_history_is_degraded_quietly(connector, monkeypatch):
    _portal(monkeypatch, now=[], history=TimeoutError("portal slow"))
    assert _probe(connector(status="unknown")) == ("degraded", [])


def test_a_second_broken_type_on_a_degraded_connector_is_paged(connector, monkeypatch):
    """The connector's one status flag must not gate another type's page (Codex P1)."""
    _portal(monkeypatch, now=[], history=[])
    c = connector(record_types=("probate", "pre_foreclosure"), status="degraded")
    assert _probe(c, "pre_foreclosure") == (
        "degraded", [("canary_zero_output", f"{c.county}/WA/pre_foreclosure")])


def test_the_page_never_carries_the_scrapers_error_text(connector, monkeypatch):
    _portal(monkeypatch, now=RuntimeError("Jane Q Canary, 12 Elm St"), history=[])
    alerts: list = []
    health._probe_unit(connector(), "probate", alerts)
    assert "jane q" not in repr(alerts).lower()


def test_every_record_type_is_in_the_rotation(connector):
    """It used to probe only record_types[0]."""
    a = connector(record_types=("probate", "pre_foreclosure", "tax_delinquent"))
    b = connector(record_types=("probate",))
    units = health._canary_units([a, b], k=100)
    assert sorted((c.county, rt) for c, rt in units) == sorted(
        [(a.county, "probate"), (a.county, "pre_foreclosure"), (a.county, "tax_delinquent"),
         (b.county, "probate")])
    assert len(health._canary_units([a, b], k=2)) == 2


def test_each_record_type_is_scraped_as_itself(connector, monkeypatch):
    """A multi-type connector probed for one type must scrape THAT type (Codex P1):
    here pre_foreclosure returns nothing anywhere while probate yields."""
    _portal(monkeypatch,
            now=lambda rt: ["r"] if rt == "probate" else [],
            history=lambda rt: ["r"] if rt == "probate" else [])
    c = connector(record_types=("probate", "pre_foreclosure"))
    assert _probe(c, "pre_foreclosure") == (
        "degraded", [("canary_zero_output", f"{c.county}/WA/pre_foreclosure")])
    # The same tick's other type still yields, and its alert key is its own.
    assert _probe(c, "probate") == ("healthy", [])


def test_the_probe_constructs_the_scraper_for_the_record_type():
    import asyncio

    built: list[dict] = []

    class Scraper:
        def __init__(self, record_type=None):
            built.append({"record_type": record_type})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def scrape(self, date_from, date_to):
            return []

    class Untyped(Scraper):
        def __init__(self):
            built.append({})

    asyncio.run(health._canary_scrape(Scraper, "10/01/2026", "10/07/2026", "pre_foreclosure"))
    asyncio.run(health._canary_scrape(Untyped, "10/01/2026", "10/07/2026", "pre_foreclosure"))
    assert built == [{"record_type": "pre_foreclosure"}, {}]


def test_an_empty_trustee_sale_probe_never_pages_whatever_the_status(connector, monkeypatch):
    """Codex P2: the exemption held only while the connector was healthy."""
    _portal(monkeypatch, now=[], history=[])
    c = connector(record_types=("trustee_sale",), status="degraded")
    assert _probe(c, "trustee_sale") == ("degraded", [])


# ── The run: no transaction held across probes; each unit's verdict is its own write ──

def _run_only(monkeypatch, wanted):
    """Run the real canary over just ``wanted`` [(connector, record_type)], in order.
    Returns the alerts it sent, as (kind, key)."""
    order = [(c.id, rt) for c, rt in wanted]
    monkeypatch.setattr(health, "_canary_units", lambda rows: [
        (r, rt) for cid, rt in order for r in rows if r.id == cid])
    sent: list = []
    import src.workers.ops_alerts as ops
    monkeypatch.setattr(ops, "send_ops_alert", lambda kind, key, _s, _b, **_kw: sent.append((kind, key)))
    return sent


def _status(c) -> str | None:
    with SyncSessionLocal() as s:
        return s.execute(text("SELECT health_status FROM county_connectors WHERE id = :i"),
                         {"i": c.id}).scalar()


def test_no_db_connection_is_held_while_a_portal_is_probed(connector, monkeypatch):
    from src.db.session import sync_engine

    held: list[int] = []

    async def fetch(_cls, _f, _t, record_type=None):
        held.append(sync_engine.pool.checkedout())
        return ["r"]

    a, b = connector(status="unknown"), connector(status="unknown")
    _run_only(monkeypatch, [(a, "probate"), (b, "probate")])
    monkeypatch.setattr(health, "_canary_scrape", fetch)

    health._canary_check_impl()

    assert held == [0, 0]
    assert (_status(a), _status(b)) == ("healthy", "healthy")


def test_a_run_stopped_by_its_time_limit_keeps_and_pages_what_it_finished(connector, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    a, b = connector(), connector()

    async def fetch(_cls, _f, _t, record_type=None):
        raise RuntimeError("portal broke") if not stopped else SoftTimeLimitExceeded()

    stopped = False
    real_record = health._record_unit

    def record(c):
        nonlocal stopped
        ok = real_record(c)
        stopped = True                     # the next unit runs out of time
        return ok

    sent = _run_only(monkeypatch, [(a, "probate"), (b, "probate")])
    monkeypatch.setattr(health, "_canary_scrape", fetch)
    monkeypatch.setattr(health, "_record_unit", record)

    with pytest.raises(SoftTimeLimitExceeded):
        health._canary_check_impl()

    assert (_status(a), _status(b)) == ("down", "healthy")
    assert sent == [("canary", f"{a.county}/WA/probate")]


def test_a_failed_write_loses_only_its_own_unit(connector, monkeypatch):
    a, b = connector(), connector()
    _portal(monkeypatch, now=RuntimeError("portal broke"), history=[])
    sent = _run_only(monkeypatch, [(a, "probate"), (b, "probate")])
    real_record = health._record_unit

    def record(c):
        if c.id == b.id:
            raise ConnectionError("pooler dropped the connection")
        return real_record(c)

    monkeypatch.setattr(health, "_record_unit", record)

    with pytest.raises(ConnectionError):
        health._canary_check_impl()

    assert (_status(a), _status(b)) == ("down", "healthy")
    assert sent == [("canary", f"{a.county}/WA/probate")]   # b's verdict never landed: no page


def test_a_connector_deleted_mid_run_is_not_paged(connector, monkeypatch):
    a, b = connector(), connector(status="unknown")

    async def fetch(_cls, _f, _t, record_type=None):
        if fetch.calls == 0:
            with SyncSessionLocal() as s:
                s.execute(text("DELETE FROM county_connectors WHERE id = :i"), {"i": a.id})
                s.commit()
            fetch.calls += 1
            raise RuntimeError("portal broke")
        return ["r"]

    fetch.calls = 0
    sent = _run_only(monkeypatch, [(a, "probate"), (b, "probate")])
    monkeypatch.setattr(health, "_canary_scrape", fetch)

    health._canary_check_impl()

    assert sent == []
    assert _status(a) is None and _status(b) == "healthy"


def test_a_second_type_breaking_on_a_down_connector_is_paged_too(connector, monkeypatch):
    """One status flag per connector, but each type pages for itself: the second type
    used to go unpaged because the first had already set the connector down (Codex)."""
    c = connector(record_types=("probate", "pre_foreclosure"))
    _portal(monkeypatch, now=lambda rt: RuntimeError("x") if rt == "probate" else RuntimeError("y"),
            history=[])
    sent = _run_only(monkeypatch, [(c, "probate"), (c, "pre_foreclosure")])

    health._canary_check_impl()

    assert _status(c) == "down"
    assert sent == [("canary", f"{c.county}/WA/probate"),
                    ("canary", f"{c.county}/WA/pre_foreclosure")]


def _row(n, record_types=("probate",), status="healthy", checked=True):
    from types import SimpleNamespace

    return SimpleNamespace(id=f"c{n}", county=f"c{n}", record_types=list(record_types), health_status=status,
                           last_checked=datetime.now(UTC) if checked else None)


def test_a_connector_never_checked_is_probed_on_the_next_tick():
    """Benton tax_delinquent waited ~22h unchecked, hidden from customers, for the rotation."""
    routine = [_row(i) for i in range(40)]
    new = _row("new", ("tax_delinquent",), status="unknown", checked=False)
    for _ in range(20):
        assert (new, "tax_delinquent") in health._canary_units([*routine, new])


def test_an_unknown_status_with_a_timestamp_still_goes_first():
    reset = _row("reset", status="unknown", checked=True)
    assert health._canary_units([*(_row(i) for i in range(40)), reset])[0][0] is reset


def test_a_priority_backlog_never_takes_the_routine_slots():
    """Codex P1: connectors whose probe never records a verdict must not starve the rest."""
    stuck = [_row(f"s{i}", ("probate", "pre_foreclosure"), status="unknown", checked=False) for i in range(10)]
    routine = [_row(i) for i in range(40)]
    units = health._canary_units([*stuck, *routine])
    assert len(units) == health.CANARY_UNITS_PER_TICK
    assert sum(c in stuck for c, _ in units) == health.CANARY_UNITS_PER_TICK - health.CANARY_ROUTINE_SLOTS
    assert len({c.id for c, _ in units if c in stuck}) == health.CANARY_UNITS_PER_TICK - health.CANARY_ROUTINE_SLOTS


def _down(n, record_types=("probate",), hours_ago=2):
    row = _row(n, record_types, status="down")
    row.id = f"down-{uuid.uuid4().hex}"  # its own Redis marker keys
    row.last_checked = datetime.now(UTC) - timedelta(hours=hours_ago)
    return row


def test_a_down_connector_is_probed_again_on_the_next_tick():
    """Clallam tax_delinquent: one unreadable page took it down, hidden 30+ h waiting for the rotation."""
    routine = [_row(i) for i in range(60)]
    down = _down("d")
    for _ in range(20):
        assert (down, "probate") in health._canary_units([*routine, down])


def test_the_type_paged_down_is_the_one_probed_again(redis_client):
    down = _down("d", ("probate", "tax_delinquent"))
    redis_client.set(health._bad_key(down, "tax_delinquent"), "canary")
    try:
        for _ in range(20):
            units = health._canary_units([*(_row(i) for i in range(60)), down])
            assert (down, "tax_delinquent") in units
    finally:
        redis_client.delete(health._bad_key(down, "tax_delinquent"))


def test_unreadable_markers_still_probe_the_down_connector(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/15")  # nothing listens
    down = _down("d")
    assert (down, "probate") in health._canary_units([*(_row(i) for i in range(60)), down])


def test_new_and_down_connectors_share_the_priority_slots_longest_unchecked_first():
    """Codex: a down backlog must not starve a new connector, nor the other way round,
    and neither takes the routine slots."""
    new = [_row(f"n{i}", status="unknown", checked=False) for i in range(10)]
    down = [_down(f"d{i}", hours_ago=i + 1) for i in range(10)]
    units = health._canary_units([*new, *down, *(_row(i) for i in range(60))])
    assert len(units) == health.CANARY_UNITS_PER_TICK
    priority = health.CANARY_UNITS_PER_TICK - health.CANARY_ROUTINE_SLOTS
    assert sum(c in new for c, _ in units) == priority - 1
    assert [c for c, _ in units if c in down][:1] == [down[-1]]   # the longest unchecked
    only_down = health._canary_units([*down, *(_row(i) for i in range(60))])
    assert [c for c, _ in only_down[:priority]] == down[::-1][:priority]


def test_a_down_connectors_other_types_stay_in_the_rotation():
    waiting_turn = _down("w", ("probate", "pre_foreclosure", "divorce"), hours_ago=1)
    older = [_down(f"o{i}", hours_ago=10 + i) for i in range(3)]   # take the recovery slots
    units = health._canary_units([waiting_turn, *older])
    assert [c for c, _ in units[:3]] == older[::-1]
    mine = [rt for c, rt in units if c is waiting_turn]   # the rotation slots are all it
    assert len(mine) == len(set(mine)) == health.CANARY_ROUTINE_SLOTS


def test_a_down_connector_just_probed_waits_and_its_types_rotate():
    """Codex P2: a source that stays down must not take a priority slot every hour,
    leaving its other types unwatched."""
    recent = _down("r", ("probate", "pre_foreclosure", "divorce"), hours_ago=0.5)
    units = health._canary_units([recent], k=100)   # recovering would probe one type alone
    assert sorted(rt for _, rt in units) == ["divorce", "pre_foreclosure", "probate"]
    due = _down("d", ("probate", "pre_foreclosure", "divorce"))
    assert len(health._canary_units([due], k=100)) == 1


def test_a_recovering_connector_is_probed_once_in_its_tick():
    """Codex P2: a sibling probed after it would overwrite the shared status."""
    down = _down("d", ("probate", "pre_foreclosure", "divorce"))
    for _ in range(20):
        units = health._canary_units([down, *(_row(i) for i in range(3))])
        assert sum(c is down for c, _ in units) == 1


def test_no_pair_is_probed_twice_in_a_tick():
    rows = [_row(i, ("probate", "divorce"), status="unknown", checked=False) for i in range(2)]
    units = health._canary_units(rows, k=100)
    assert len(units) == len({(c.id, rt) for c, rt in units}) == 4


# --- raise site: where a canary failure started, without its (PII-bearing) text ---

def _caught(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        return exc
    raise AssertionError("expected a raise")


def test_the_raise_site_names_our_file_line_and_function_not_the_page():
    from bs4 import BeautifulSoup

    from src.scrapers.clallam_wa_tax_delinquent import _parse_tax_due

    exc = _caught(lambda: _parse_tax_due(BeautifulSoup("<p>Jane Q Doe, 12 Elm St</p>", "html.parser")))
    site = health._raise_site(exc)
    assert site.startswith("src/scrapers/clallam_wa_tax_delinquent.py:"), site
    assert site.endswith(" in _parse_tax_due"), site
    assert "jane" not in site.lower() and "elm" not in site.lower()


def test_a_library_frame_shows_only_its_file_name():
    import json

    site = health._raise_site(_caught(lambda: json.loads("{")))
    assert "/" not in site.split(":")[0] and "\\" not in site, site  # no directory, no user name
    assert " in " in site


def test_a_library_raise_under_our_code_shows_both_sites():
    from src.scrapers.clallam_wa_tax_delinquent import parse_portal_status

    site = health._raise_site(_caught(lambda: parse_portal_status(123)))  # bs4 rejects it
    ours, lib = site.split(" -> ")
    assert ours.startswith("src/scrapers/clallam_wa_tax_delinquent.py:"), site
    assert ours.endswith(" in parse_portal_status"), site
    assert not lib.startswith("src/") and "/" not in lib.split(":")[0], site


def test_an_explicit_cause_is_followed():
    def inner():
        raise KeyError("Jane Q Doe")

    def outer():
        try:
            inner()
        except KeyError as e:
            raise ValueError("wrapped Jane Q Doe") from e

    site = health._raise_site(_caught(outer))
    assert " in outer; cause KeyError at " in site and site.endswith(" in inner"), site
    assert "jane" not in site.lower()


def test_an_exception_never_raised_has_no_site():
    assert health._raise_site(ValueError("x")) == "unknown"


def test_the_page_and_the_log_carry_the_raise_site_but_not_the_text(connector, monkeypatch, caplog):
    import logging

    _portal(monkeypatch, now=RuntimeError("Jane Q Canary, 12 Elm St"), history=[])
    alerts: list = []
    with caplog.at_level(logging.ERROR, logger="worker.scheduler"):
        health._probe_unit(connector(), "probate", alerts)
    body = alerts[0][3]
    assert "error_class=RuntimeError\nraise_site=" in body
    assert body.rstrip().endswith(" in fetch"), body  # the scraper call that raised
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError at " in logged and logged.rstrip().endswith(" in fetch"), logged
    for out in (body, logged):
        assert "jane" not in out.lower() and "elm st" not in out.lower()


# ─── Per-type down state and the all-clear ─────────────────────────────────────

def test_a_type_that_works_again_sends_one_all_clear(connector, monkeypatch):
    c = connector()
    _portal(monkeypatch, now=RuntimeError("portal broke"), history=[])
    assert _probe(c) == ("down", [("canary", f"{c.county}/WA/probate")])
    _portal(monkeypatch, now=["r"], history=[])
    assert _probe(c) == ("healthy", [("canary_recovered", f"{c.county}/WA/probate")])
    assert _probe(c) == ("healthy", [])  # recovered once, not every tick


def test_another_type_working_is_not_a_recovery(connector, monkeypatch):
    """Lewis, 2026-11-15: pre_foreclosure broke; probate probing fine sets the
    connector healthy, which must not announce that pre_foreclosure came back."""
    c = connector(record_types=("probate", "pre_foreclosure"))
    _portal(monkeypatch, now=lambda rt: RuntimeError("x") if rt == "pre_foreclosure" else ["r"],
            history=[])
    assert _probe(c, "pre_foreclosure") == ("down", [("canary", f"{c.county}/WA/pre_foreclosure")])
    assert _probe(c, "probate") == ("healthy", [])
    # Still broken, and the healthy connector flag does not make it page as new.
    assert _probe(c, "pre_foreclosure") == ("down", [])


def test_zero_output_that_yields_again_is_an_all_clear(connector, monkeypatch):
    c = connector()
    _portal(monkeypatch, now=[], history=[])
    assert _probe(c) == ("degraded", [("canary_zero_output", f"{c.county}/WA/probate")])
    _portal(monkeypatch, now=[], history=["r"])  # older records: extraction works
    assert _probe(c) == ("healthy", [("canary_recovered", f"{c.county}/WA/probate")])


def test_an_inconclusive_probe_is_not_a_recovery(connector, monkeypatch):
    c = connector()
    _portal(monkeypatch, now=RuntimeError("portal broke"), history=[])
    _probe(c)
    _portal(monkeypatch, now=[], history=TimeoutError("portal slow"))
    assert _probe(c) == ("degraded", [])  # proves nothing: keeps the marker
    _portal(monkeypatch, now=["r"], history=[])
    assert _probe(c) == ("healthy", [("canary_recovered", f"{c.county}/WA/probate")])


def test_an_unreadable_marker_pages_rather_than_stays_quiet(connector, monkeypatch):
    from src.config import settings

    c = connector()
    _portal(monkeypatch, now=RuntimeError("portal broke"), history=[])
    _probe(c)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/15")  # nothing listens
    assert _probe(c) == ("down", [("canary", f"{c.county}/WA/probate")])


def test_the_all_clear_skips_better_stack_and_carries_no_page_text(connector, monkeypatch):
    """Better Stack's webhook only opens incidents: an all-clear there pages anew."""
    c = connector()
    _portal(monkeypatch, now=RuntimeError("Jane Q Canary, 12 Elm St"), history=[])
    _probe(c)
    _portal(monkeypatch, now=["r", "r"], history=[])
    monkeypatch.setattr(health, "_canary_units", lambda rows: [(r, "probate") for r in rows
                                                               if r.id == c.id])
    sent: list = []
    import src.workers.ops_alerts as ops
    monkeypatch.setattr(ops, "send_ops_alert",
                        lambda kind, key, subject, body, webhook=True:
                        sent.append((kind, subject, body, webhook)))

    health._canary_check_impl()

    assert len(sent) == 1
    kind, subject, body, webhook = sent[0]
    assert (kind, webhook) == ("canary_recovered", False)
    assert subject == f"Canary RECOVERED: {c.county}/WA probate (was DOWN)"
    assert "evidence=2 records in the last 7 days" in body
    assert "jane" not in (subject + body).lower()


def test_a_down_page_that_never_went_out_is_retried(connector, monkeypatch):
    """Marked down but delivered on no route: the next probe pages again (Codex High)."""
    c = connector()
    _portal(monkeypatch, now=RuntimeError("portal broke"), history=[])
    monkeypatch.setattr(health, "_canary_units", lambda rows: [(r, "probate") for r in rows
                                                               if r.id == c.id])
    attempts: list = []
    import src.workers.ops_alerts as ops
    monkeypatch.setattr(ops, "send_ops_alert",
                        lambda kind, _k, _s, _b, **_kw: attempts.append(kind) or len(attempts) > 1)

    for _ in range(3):
        health._canary_check_impl()

    assert attempts == ["canary", "canary"]  # undelivered, retried and delivered, then quiet


def test_a_down_marker_is_unsent_until_its_page_is_delivered(connector, monkeypatch, redis_client):
    """Saved as unsent before the send and promoted only after delivery, so a failure
    between the two can only page again, never go silent (Codex High, second pass)."""
    c = connector()
    _portal(monkeypatch, now=RuntimeError("portal broke"), history=[])
    monkeypatch.setattr(health, "_canary_units", lambda rows: [(r, "probate") for r in rows
                                                               if r.id == c.id])
    delivered = {"ok": False}
    import src.workers.ops_alerts as ops
    monkeypatch.setattr(ops, "send_ops_alert", lambda *_a, **_kw: delivered["ok"])

    def marker():
        return _text(redis_client.get(health._bad_key(c, "probate")))

    health._canary_check_impl()
    assert marker() == "canary_unsent"
    delivered["ok"] = True
    health._canary_check_impl()
    assert marker() == "canary"


def _text(v):
    return v.decode() if isinstance(v, bytes) else v


@pytest.mark.parametrize("step", ["read", "write"])
def test_a_time_limit_inside_the_marker_step_stops_the_run(connector, monkeypatch, step):
    """Out of time is not Redis being down: it must reach the task, not be swallowed."""
    from celery.exceptions import SoftTimeLimitExceeded

    c = connector()
    _portal(monkeypatch, now=["r"], history=[])

    def out_of_time():
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(health, "_redis", out_of_time)
    with pytest.raises(SoftTimeLimitExceeded):
        if step == "read":
            health._probe_unit(c, "probate", [])
        else:
            health._apply_mark(c, "probate", ("ok", "1 records in the last 7 days"))


# --- a source that proves its empty answers (Asotin's recorder) ------------------------

_PROVES_EMPTY = "src.scrapers.asotin_wa_recorder.AsotinWARecorderScraper"


@pytest.mark.parametrize("status", ["healthy", "unknown", "degraded"])
def test_a_validated_empty_week_is_healthy_without_a_history_probe(connector, monkeypatch, status):
    calls = _portal(monkeypatch, now=[], history=[])
    c = connector(record_types=("pre_foreclosure",), status=status)
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE county_connectors SET scraper_class = :k WHERE id = CAST(:i AS uuid)"),
                  {"k": _PROVES_EMPTY, "i": c.id})
        s.commit()
    assert _probe(c, "pre_foreclosure") == ("healthy", [])
    assert calls == [_THIS_WEEK]                         # no historical probe, no extra solves


def test_a_validated_empty_source_that_breaks_is_still_down(connector, monkeypatch):
    _portal(monkeypatch, now=RuntimeError("portal changed"), history=[])
    c = connector(record_types=("pre_foreclosure",))
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE county_connectors SET scraper_class = :k WHERE id = CAST(:i AS uuid)"),
                  {"k": _PROVES_EMPTY, "i": c.id})
        s.commit()
    status, alerts = _probe(c, "pre_foreclosure")
    assert status == "down" and [k for k, _ in alerts] == ["canary"]


def test_other_sources_empty_everywhere_are_still_zero_output(connector, monkeypatch):
    _portal(monkeypatch, now=[], history=[])
    c = connector(record_types=("pre_foreclosure",))
    assert _probe(c, "pre_foreclosure") == (
        "degraded", [("canary_zero_output", f"{c.county}/WA/pre_foreclosure")])


def test_a_down_source_that_proves_an_empty_week_recovers_once(connector, monkeypatch):
    c = connector(record_types=("pre_foreclosure",))
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE county_connectors SET scraper_class = :k WHERE id = CAST(:i AS uuid)"),
                  {"k": _PROVES_EMPTY, "i": c.id})
        s.commit()
    _portal(monkeypatch, now=RuntimeError("portal broke"), history=[])
    assert _probe(c, "pre_foreclosure") == ("down", [("canary", f"{c.county}/WA/pre_foreclosure")])
    _portal(monkeypatch, now=[], history=[])
    assert _probe(c, "pre_foreclosure") == (
        "healthy", [("canary_recovered", f"{c.county}/WA/pre_foreclosure")])
    assert _probe(c, "pre_foreclosure") == ("healthy", [])     # once, not every tick
