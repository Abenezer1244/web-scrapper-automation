"""The shared eRealProperty lease: ownership, renewal, and release.

The lease is a RATE bound, not a correctness lock, but the three properties below
each caused a concrete failure when they were missing:

  * permission-to-work and ownership-requiring-cleanup are different facts;
  * a renewal we cannot CONFIRM is not a renewal;
  * once ownership is known lost, it stays lost.
"""
from __future__ import annotations

import pytest

from src.scrapers.enrichment.source_admission import SourceAdmission


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.eval_raises = False
        self.eval_result = 1

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def eval(self, script, _n, key, token, *args):
        """Real compare-and-delete / compare-and-expire semantics.

        Both the release and the renewal go through EVAL, so the fake has to tell
        them apart the way Redis would: by what the script actually does.
        """
        if self.eval_raises:
            raise ConnectionError("redis unreachable")
        if self.eval_result == 0:
            return 0                       # simulate "someone else owns it"
        if self.store.get(key) != token:
            return 0
        if "del" in script:
            del self.store[key]
        return 1

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def fake(monkeypatch):
    client = _FakeRedis()
    monkeypatch.setattr(
        "src.scrapers.enrichment.source_admission._client", lambda: client
    )
    return client


class TestOwnershipVsPermission:
    def test_a_lease_acquired_then_budget_exhausted_is_still_released(self, fake):
        """The wrapper sets admitted=False when the wait ate the budget.

        Releasing on `admitted` meant that lease was never handed back and the
        source stayed locked for the full 900s TTL while nobody used it (Codex).
        """
        with SourceAdmission("k", max_wait_s=0.0) as adm:
            assert adm.admitted is True
            assert adm.holds_lease is True
            adm.admitted = False          # caller found no budget left
        assert fake.store == {}, "the lease must be released even so"

    def test_a_refused_caller_releases_nothing(self, fake):
        fake.store["bl:source_admission:k"] = "someone-elses-token"
        with SourceAdmission("k", max_wait_s=0.0) as adm:
            assert adm.admitted is False
            assert adm.holds_lease is False
        # The other holder's lease is untouched.
        assert fake.store["bl:source_admission:k"] == "someone-elses-token"


class TestRenewal:
    def test_losing_the_lease_stops_requests_and_stays_stopped(self, fake):
        with SourceAdmission("k", max_wait_s=0.0) as adm:
            fake.eval_result = 0          # someone else owns it now
            assert adm.still_held() is False
            assert adm.admitted is False
            # A second call must not report True through a "nothing to lose" path.
            assert adm.still_held() is False

    def test_a_brief_renewal_error_is_tolerated(self, fake):
        with SourceAdmission("k", max_wait_s=0.0) as adm:
            fake.eval_raises = True
            assert adm.still_held() is True   # inside the grace window

    def test_an_unconfirmable_lease_eventually_stops_requests(self, fake, monkeypatch):
        import src.scrapers.enrichment.source_admission as sa

        with SourceAdmission("k", max_wait_s=0.0) as adm:
            fake.eval_raises = True
            # Pretend the last confirmed ownership was long ago.
            adm._confirmed_at -= (sa._RENEWAL_GRACE_S + 1)
            # Blanket fail-open here would keep this worker running past the TTL
            # alongside whoever acquired the lease next (Codex).
            assert adm.still_held() is False
            assert adm.admitted is False
