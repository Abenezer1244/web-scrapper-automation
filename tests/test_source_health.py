"""Shared external-source health state.

Real DB, no mocks. This is the flag that stops every worker from hammering a
source that has already blocked us, so its edges are worth pinning exactly.
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.scrapers.enrichment.source_health import (
    KING_EREALPROPERTY,
    SourceUnavailableError,
    assert_source_available,
    cooldown_for,
    get_source_state,
    is_source_available,
    mark_probe_failed,
    mark_source_healthy,
    mark_source_unhealthy,
    sources_due_for_probe,
)

_KEY = "test_source_health_probe"


@pytest.fixture
def sync_db():
    """Real sync session — source_health is called from Celery workers, which are
    sync. Defined locally rather than in conftest so this doesn't collide with
    other branches editing the shared fixtures."""
    from src.db.session import SyncSessionLocal

    with SyncSessionLocal() as s:
        yield s


@pytest.fixture(autouse=True)
def _clean(sync_db):
    def _wipe():
        sync_db.execute(
            text("DELETE FROM external_source_health WHERE source_key IN (:a, :b)"),
            {"a": _KEY, "b": KING_EREALPROPERTY},
        )
        sync_db.commit()

    _wipe()
    yield
    _wipe()


class TestDefaultIsHealthy:
    def test_unknown_source_is_available(self, sync_db):
        # No row == healthy. Keeps the happy path a single read, never a write.
        assert is_source_available(sync_db, _KEY) is True
        assert get_source_state(sync_db, _KEY) is None

    def test_assert_does_not_raise_for_unknown_source(self, sync_db):
        assert_source_available(sync_db, _KEY)  # no raise


class TestMarkingUnhealthy:
    def test_marking_blocks_subsequent_calls(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "rate-blocked in test")
        assert is_source_available(sync_db, _KEY) is False
        with pytest.raises(SourceUnavailableError) as e:
            assert_source_available(sync_db, _KEY)
        assert e.value.source_key == _KEY

    def test_first_block_starts_at_the_bottom_of_the_ladder(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "first")
        st = get_source_state(sync_db, _KEY)
        assert st["consecutive_probe_failures"] == 0
        # 1h cooldown, allowing a little slack for execution time. The first rung
        # is short ON PURPOSE: it fires before any probe has confirmed anything,
        # so it must not cost a day of enrichment for what may be a blip. The
        # canary escalates from here once it has actual evidence.
        delta = st["cooldown_until"] - datetime.now(UTC)
        assert timedelta(minutes=59) < delta <= timedelta(hours=1)

    def test_reason_and_first_seen_are_recorded(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "429 from upstream")
        st = get_source_state(sync_db, _KEY)
        assert "429" in st["reason"]
        assert st["first_seen_at"] is not None

    def test_remark_preserves_the_original_first_seen(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "one")
        first = get_source_state(sync_db, _KEY)["first_seen_at"]
        mark_source_unhealthy(sync_db, _KEY, "two")
        # The outage started once; re-marking must not reset how long it has run.
        assert get_source_state(sync_db, _KEY)["first_seen_at"] == first
        assert get_source_state(sync_db, _KEY)["reason"] == "two"


class TestCooldownLadder:
    def test_escalates_1_6_24_48_72_then_caps(self):
        assert cooldown_for(0) == timedelta(hours=1)
        assert cooldown_for(1) == timedelta(hours=6)
        assert cooldown_for(2) == timedelta(hours=24)
        assert cooldown_for(3) == timedelta(hours=48)
        assert cooldown_for(4) == timedelta(hours=72)
        assert cooldown_for(9) == timedelta(hours=72)   # capped, never unbounded

    def test_failed_probe_escalates_the_cooldown(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "blocked")
        mark_probe_failed(sync_db, _KEY, "still blocked")
        st = get_source_state(sync_db, _KEY)
        assert st["consecutive_probe_failures"] == 1
        delta = st["cooldown_until"] - datetime.now(UTC)
        assert timedelta(hours=5) < delta <= timedelta(hours=6)
        assert st["last_probe_at"] is not None


class TestRecovery:
    def test_marking_healthy_clears_everything(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "blocked")
        recovered = mark_source_healthy(sync_db, _KEY)
        assert recovered is True   # signals a transition, for ops alerting
        st = get_source_state(sync_db, _KEY)
        assert st["status"] == "healthy"
        assert st["cooldown_until"] is None
        assert st["consecutive_probe_failures"] == 0
        assert st["last_success_at"] is not None
        assert is_source_available(sync_db, _KEY) is True

    def test_healthy_on_already_healthy_is_not_a_recovery(self, sync_db):
        # Only a real transition should alert ops; a no-op must stay silent.
        assert mark_source_healthy(sync_db, _KEY) is False

    def test_block_after_recovery_restarts_the_ladder(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "blocked")
        mark_probe_failed(sync_db, _KEY, "still blocked")
        mark_source_healthy(sync_db, _KEY)
        mark_source_unhealthy(sync_db, _KEY, "blocked again")
        # A fresh outage must not inherit the old streak and start at 72h.
        st = get_source_state(sync_db, _KEY)
        assert st["consecutive_probe_failures"] == 0
        delta = st["cooldown_until"] - datetime.now(UTC)
        assert timedelta(minutes=59) < delta <= timedelta(hours=1)


class TestCanaryWorkList:
    def test_source_in_cooldown_is_not_due(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "blocked")
        assert _KEY not in sources_due_for_probe(sync_db)

    def test_source_past_cooldown_is_due_and_available_again(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "blocked")
        sync_db.execute(
            text("UPDATE external_source_health SET cooldown_until = :t WHERE source_key = :k"),
            {"t": datetime.now(UTC) - timedelta(minutes=1), "k": _KEY},
        )
        sync_db.commit()
        assert _KEY in sources_due_for_probe(sync_db)
        # Past cooldown means "due for a CLAIMED PROBE", not "open to traffic".
        # Releasing ordinary traffic here is exactly what let the next real job
        # walk into a still-refusing source, spend a full breaker window
        # rediscovering the block and re-arm the cooldown.
        assert is_source_available(sync_db, _KEY) is False

    def test_healthy_source_is_never_due(self, sync_db):
        mark_source_healthy(sync_db, _KEY)
        assert _KEY not in sources_due_for_probe(sync_db)


class TestProbeClaim:
    """The claim is what stops two ticks probing (and escalating) one outage twice."""

    def _expire(self, sync_db, key=_KEY):
        sync_db.execute(
            text("UPDATE external_source_health SET cooldown_until = :t WHERE source_key = :k"),
            {"t": datetime.now(UTC) - timedelta(minutes=1), "k": key},
        )
        sync_db.commit()

    def test_claim_succeeds_once_then_blocks_a_second_claim(self, sync_db):
        from src.scrapers.enrichment.source_health import claim_probe

        mark_source_unhealthy(sync_db, _KEY, "blocked")
        self._expire(sync_db)
        first = claim_probe(sync_db, _KEY, timedelta(minutes=10))
        assert first is not None
        # A second tick arriving right behind must NOT also probe: two probes of
        # one outage would escalate the ladder two rungs off a single refusal.
        assert claim_probe(sync_db, _KEY, timedelta(minutes=10)) is None

    def test_claim_refused_while_still_in_cooldown(self, sync_db):
        from src.scrapers.enrichment.source_health import claim_probe

        mark_source_unhealthy(sync_db, _KEY, "blocked")
        assert claim_probe(sync_db, _KEY, timedelta(minutes=10)) is None

    def test_claim_refused_for_a_healthy_source(self, sync_db):
        from src.scrapers.enrichment.source_health import claim_probe

        mark_source_healthy(sync_db, _KEY)
        assert claim_probe(sync_db, _KEY, timedelta(minutes=10)) is None


class TestProbeResolution:
    def _blocked_and_claimed(self, sync_db):
        from src.scrapers.enrichment.source_health import claim_probe

        mark_source_unhealthy(sync_db, _KEY, "blocked")
        sync_db.execute(
            text("UPDATE external_source_health SET cooldown_until = :t WHERE source_key = :k"),
            {"t": datetime.now(UTC) - timedelta(minutes=1), "k": _KEY},
        )
        sync_db.commit()
        token = claim_probe(sync_db, _KEY, timedelta(minutes=10))
        assert token is not None
        return token

    def test_successful_probe_recovers_the_source(self, sync_db):
        from src.scrapers.enrichment.source_health import resolve_probe

        token = self._blocked_and_claimed(sync_db)
        assert resolve_probe(sync_db, _KEY, token, healthy=True, reason="200 ok") is True
        st = get_source_state(sync_db, _KEY)
        assert st["status"] == "healthy"
        assert st["cooldown_until"] is None
        assert st["consecutive_probe_failures"] == 0
        assert is_source_available(sync_db, _KEY) is True

    def test_recovery_keeps_the_diagnosis(self, sync_db):
        from src.scrapers.enrichment.source_health import resolve_probe

        token = self._blocked_and_claimed(sync_db)
        resolve_probe(sync_db, _KEY, token, healthy=True, reason="200 + parseable")
        # Nulling `reason` on recovery threw away the only durable record of the
        # outage; worker log retention does not reach back far enough to replace it.
        assert "200 + parseable" in get_source_state(sync_db, _KEY)["reason"]

    def test_failed_probe_escalates_one_rung_only(self, sync_db):
        from src.scrapers.enrichment.source_health import resolve_probe

        token = self._blocked_and_claimed(sync_db)
        assert resolve_probe(sync_db, _KEY, token, healthy=False, reason="HTTP503") is True
        st = get_source_state(sync_db, _KEY)
        assert st["consecutive_probe_failures"] == 1
        assert st["status"] == "throttled"
        delta = st["cooldown_until"] - datetime.now(UTC)
        assert timedelta(hours=5) < delta <= timedelta(hours=6)

    def test_a_stale_success_cannot_erase_a_fresher_block(self, sync_db):
        """The race that would have made recovery unsafe.

        Our probe succeeds; while it was in flight another worker records a NEW
        outage. Clearing the source now would delete a block that is newer than
        our evidence, putting real traffic straight back onto a refusing source.
        """
        from src.scrapers.enrichment.source_health import resolve_probe

        token = self._blocked_and_claimed(sync_db)
        mark_source_unhealthy(sync_db, _KEY, "fresh block while probe in flight")
        assert resolve_probe(sync_db, _KEY, token, healthy=True, reason="200 ok") is False
        st = get_source_state(sync_db, _KEY)
        assert st["status"] == "throttled"
        assert st["reason"] == "fresh block while probe in flight"

    def test_a_stale_failure_cannot_re_block_a_recovered_source(self, sync_db):
        from src.scrapers.enrichment.source_health import resolve_probe

        token = self._blocked_and_claimed(sync_db)
        mark_source_healthy(sync_db, _KEY)
        assert resolve_probe(sync_db, _KEY, token, healthy=False, reason="late 503") is False
        assert get_source_state(sync_db, _KEY)["status"] == "healthy"


class TestCanaryBackstop:
    """Recovery must not become a new single point of failure."""

    def test_expired_cooldown_holds_traffic_while_a_canary_could_still_probe(self, sync_db):
        mark_source_unhealthy(sync_db, _KEY, "blocked")
        sync_db.execute(
            text("UPDATE external_source_health SET cooldown_until = :t WHERE source_key = :k"),
            {"t": datetime.now(UTC) - timedelta(minutes=5), "k": _KEY},
        )
        sync_db.commit()
        assert is_source_available(sync_db, _KEY) is False

    def test_traffic_is_released_when_nothing_has_probed_for_a_long_time(self, sync_db):
        # If the canary is not running at all, a source must not stay blocked
        # forever — that would be a worse failure than the one being fixed.
        mark_source_unhealthy(sync_db, _KEY, "blocked")
        sync_db.execute(
            text("UPDATE external_source_health SET cooldown_until = :t WHERE source_key = :k"),
            {"t": datetime.now(UTC) - timedelta(hours=7), "k": _KEY},
        )
        sync_db.commit()
        assert is_source_available(sync_db, _KEY) is True
