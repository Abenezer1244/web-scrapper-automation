"""The canary that `source_health` was designed around and shipped without.

Its own docstring promised a source stays blocked "until a canary clears it", but
`sources_due_for_probe`, `mark_probe_failed` and the recovery transition had no
production caller at all. Production on 2026-09-07 showed the consequence exactly:
`king_erealproperty` throttled since 2026-09-04 with `last_probe_at = NULL` and
`consecutive_probe_failures = 0`, while the source itself answered 60/60 with 200.

Real DB. The probe function is substituted, because the whole point of a probe is
that it makes a real request and these tests must not make sixty of them.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.scrapers.enrichment.source_health import (
    get_source_state,
    is_source_available,
    mark_source_unhealthy,
)
from src.workers.scheduler_helpers.health import _enrichment_source_canary_impl

_KEY = "test_canary_source"


@pytest.fixture
def sync_db():
    from src.db.session import SyncSessionLocal

    with SyncSessionLocal() as s:
        yield s


@pytest.fixture(autouse=True)
def _clean(sync_db):
    def _wipe():
        sync_db.execute(
            text("DELETE FROM external_source_health WHERE source_key = :k"), {"k": _KEY}
        )
        sync_db.commit()

    _wipe()
    yield
    _wipe()


@pytest.fixture(autouse=True)
def _quiet_alerts(monkeypatch):
    monkeypatch.setattr(
        "src.workers.ops_alerts.send_ops_alert", lambda *_a, **_k: False
    )


def _blocked_and_due(sync_db):
    mark_source_unhealthy(sync_db, _KEY, "blocked in test")
    sync_db.execute(
        text("UPDATE external_source_health SET cooldown_until = :t WHERE source_key = :k"),
        {"t": datetime.now(UTC) - timedelta(minutes=1), "k": _KEY},
    )
    sync_db.commit()


def _with_probe(monkeypatch, result):
    calls: list[int] = []

    def _probe(_db):
        calls.append(1)
        return result

    monkeypatch.setattr(
        "src.scrapers.enrichment.source_probe.PROBES", {_KEY: _probe}, raising=False
    )
    return calls


class TestCanaryRecovery:
    def test_a_passing_probe_clears_the_source(self, sync_db, monkeypatch):
        _blocked_and_due(sync_db)
        calls = _with_probe(monkeypatch, (True, "200 + parseable"))

        _enrichment_source_canary_impl()

        assert len(calls) == 1
        st = get_source_state(sync_db, _KEY)
        assert st["status"] == "healthy"
        assert is_source_available(sync_db, _KEY) is True

    def test_a_failing_probe_escalates_instead_of_clearing(self, sync_db, monkeypatch):
        _blocked_and_due(sync_db)
        _with_probe(monkeypatch, (False, "HTTP503"))

        _enrichment_source_canary_impl()

        st = get_source_state(sync_db, _KEY)
        assert st["status"] == "throttled"
        assert st["consecutive_probe_failures"] == 1
        assert "HTTP503" in st["reason"]
        # Next rung of the ladder, so a source that stays angry is asked less often.
        delta = st["cooldown_until"] - datetime.now(UTC)
        assert timedelta(hours=5) < delta <= timedelta(hours=6)

    def test_a_probe_that_raises_counts_as_a_failed_probe(self, sync_db, monkeypatch):
        _blocked_and_due(sync_db)

        def _boom(_db):
            raise OSError("connection reset")

        monkeypatch.setattr(
            "src.scrapers.enrichment.source_probe.PROBES", {_KEY: _boom}, raising=False
        )
        _enrichment_source_canary_impl()

        st = get_source_state(sync_db, _KEY)
        assert st["status"] == "throttled"
        assert "OSError" in st["reason"]


class TestCanaryRestraint:
    def test_a_source_still_in_cooldown_is_never_probed(self, sync_db, monkeypatch):
        mark_source_unhealthy(sync_db, _KEY, "blocked in test")  # cooldown in the future
        calls = _with_probe(monkeypatch, (True, "ok"))

        _enrichment_source_canary_impl()

        assert calls == []
        assert get_source_state(sync_db, _KEY)["status"] == "throttled"

    def test_a_healthy_source_is_never_probed(self, sync_db, monkeypatch):
        calls = _with_probe(monkeypatch, (True, "ok"))
        _enrichment_source_canary_impl()
        assert calls == []

    def test_a_second_tick_does_not_probe_the_same_outage_again(self, sync_db, monkeypatch):
        _blocked_and_due(sync_db)
        calls = _with_probe(monkeypatch, (False, "HTTP503"))

        _enrichment_source_canary_impl()
        _enrichment_source_canary_impl()

        # One outage, one probe. Probing twice would escalate two rungs off a
        # single refusal, turning a 1-hour backoff into a 24-hour one.
        assert len(calls) == 1

    def test_a_source_with_no_registered_probe_is_left_alone(self, sync_db, monkeypatch):
        _blocked_and_due(sync_db)
        monkeypatch.setattr(
            "src.scrapers.enrichment.source_probe.PROBES", {}, raising=False
        )
        _enrichment_source_canary_impl()
        # Clearing a source we cannot verify would put real traffic back on it blind.
        st = get_source_state(sync_db, _KEY)
        assert st["status"] == "throttled"
        assert st["consecutive_probe_failures"] == 0


class TestEndToEndRecovery:
    def test_block_then_probe_then_traffic_is_allowed_again(self, sync_db, monkeypatch):
        """The whole loop the system was missing."""
        mark_source_unhealthy(sync_db, _KEY, "circuit breaker tripped")
        assert is_source_available(sync_db, _KEY) is False

        # Cooldown expires. Traffic is STILL held: expiry means "due for a probe".
        sync_db.execute(
            text("UPDATE external_source_health SET cooldown_until = :t WHERE source_key = :k"),
            {"t": datetime.now(UTC) - timedelta(minutes=1), "k": _KEY},
        )
        sync_db.commit()
        assert is_source_available(sync_db, _KEY) is False

        _with_probe(monkeypatch, (True, "200 + parseable"))
        _enrichment_source_canary_impl()

        # Only now, on evidence, does traffic resume.
        assert is_source_available(sync_db, _KEY) is True


class TestProbeTargetsAreKingOnly:
    def test_the_king_probe_query_is_filtered_to_king(self):
        """A Pierce parcel would make the King probe fail forever.

        `results` holds Pierce, Snohomish and Clark rows, and several counties
        also use 10-digit parcel ids. Verified in production: the unfiltered
        version of this query returned 9900000021, a PIERCE parcel, in its top 5.
        Feeding that to eRealProperty yields a page that is not about it,
        `parcel_page_is_for` fails, and the canary reports King as still
        refusing us — so King stays blocked forever WITH a canary running, which
        is worse than the outage this change exists to fix (Codex).
        """
        import inspect

        from src.scrapers.enrichment import source_probe

        src = inspect.getsource(source_probe._king_probe_parcels)
        assert "lower(sc.county) = 'king'" in src
        assert "upper(sc.state) = 'WA'" in src
        assert "JOIN jobs j" in src and "JOIN scraper_configs sc" in src


class TestAbandonedProbeDoesNotBlockForever:
    def test_a_stale_probe_claim_stops_holding_traffic(self, sync_db):
        """`claim_probe` stamps last_probe_at BEFORE the request.

        A worker killed between the claim and the verdict leaves that stamp
        behind. Treating it as proof a canary is running held traffic permanently
        on one abandoned claim, which is the same indefinite silent block this
        change exists to remove (Codex).
        """
        mark_source_unhealthy(sync_db, _KEY, "blocked in test")
        sync_db.execute(
            text(
                "UPDATE external_source_health SET cooldown_until = :c, "
                "last_probe_at = :p WHERE source_key = :k"
            ),
            {
                "c": datetime.now(UTC) - timedelta(hours=8),
                "p": datetime.now(UTC) - timedelta(hours=7),
                "k": _KEY,
            },
        )
        sync_db.commit()
        assert is_source_available(sync_db, _KEY) is True

    def test_a_fresh_probe_claim_still_holds_traffic(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "blocked in test")
        sync_db.execute(
            text(
                "UPDATE external_source_health SET cooldown_until = :c, "
                "last_probe_at = :p WHERE source_key = :k"
            ),
            {
                "c": datetime.now(UTC) - timedelta(hours=8),
                "p": datetime.now(UTC) - timedelta(minutes=2),
                "k": _KEY,
            },
        )
        sync_db.commit()
        assert is_source_available(sync_db, _KEY) is False
