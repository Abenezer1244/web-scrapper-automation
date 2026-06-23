# tests/test_jobs_entitlement_guard.py
from src.api.entitlements import ConfigRow, config_run_violation


def test_guard_blocks_starter_preforeclosure(monkeypatch):
    # Pure-path assertion: the route delegates to config_run_violation, so verify
    # the decision the route will make for a starter user with a pre_foreclosure config.
    rows = [ConfigRow(id="1", state="WA", county="King", record_type="pre_foreclosure",
                      created_at=__import__("datetime").datetime(2026,1,1,tzinfo=__import__("datetime").UTC))]
    assert config_run_violation("starter", "WA", "King", "pre_foreclosure", rows) is not None
