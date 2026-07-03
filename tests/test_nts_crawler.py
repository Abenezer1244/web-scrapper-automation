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
