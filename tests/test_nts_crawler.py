"""Tests for the NTS crawler health signal (Phase 3 observability).

_barren_alert_reason is the pure decision behind the OPS alert that catches a silently
broken crawler — the failure mode that let the Pierce NTS cache go stale for a week.
"""
from src.workers.nts_crawler import _barren_alert_reason


class TestBarrenAlertReason:
    def test_zero_discovered_alerts(self):
        # discovery broke (the Bug A page-1-break symptom, or a layout change)
        assert _barren_alert_reason(0, 0) is not None
        assert "discovered" in _barren_alert_reason(0, 0)

    def test_discovered_but_none_upserted_alerts(self):
        # everything failed to parse/validate — parser/format drift
        reason = _barren_alert_reason(12, 0)
        assert reason is not None
        assert "0 upserted" in reason

    def test_healthy_run_does_not_alert(self):
        assert _barren_alert_reason(12, 7) is None
        assert _barren_alert_reason(1, 1) is None

    def test_clark_zero_upserts_is_not_an_alert(self):
        # The Columbian crawler counts EVERY legal-notice ad as discovered (not just
        # trustee sales), so 0 upserts is the normal no-sale-today case — suppress it.
        assert _barren_alert_reason(32, 0, alert_on_zero_upserts=False) is None

    def test_clark_zero_discovered_still_alerts(self):
        # A listing that yields 0 ads means discovery/extraction broke — always alert.
        assert _barren_alert_reason(0, 0, alert_on_zero_upserts=False) is not None
