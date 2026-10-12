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

    async def fetch(_scraper_class, date_from, _date_to):
        calls.append(date_from)
        answer = now if date_from == _THIS_WEEK else history
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(health, "_canary_scrape", fetch)
    return calls


def _probe(c, record_type="probate"):
    alerts: list = []
    health._probe_unit(c, record_type, alerts)
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
    assert (status, alerts) == ("down", [("canary", f"{c.county}/WA")])
    assert _probe(c) == ("down", [])  # already down: no second page


def test_a_record_type_with_no_scraper_is_down_and_paged(connector, monkeypatch):
    """It used to go 'down' with no alert (Codex P2)."""
    _portal(monkeypatch, now=["r"], history=[])
    c = connector(record_types=("probate",))
    assert _probe(c, "divorce") == ("down", [("canary", f"{c.county}/WA")])


def test_an_unproven_connector_with_nothing_anywhere_is_degraded_quietly(connector, monkeypatch):
    _portal(monkeypatch, now=[], history=[])
    assert _probe(connector(status="unknown")) == ("degraded", [])


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
