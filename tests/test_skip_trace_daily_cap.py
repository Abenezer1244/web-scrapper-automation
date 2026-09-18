"""Rolling-24h spend circuit breaker for Tracerfy.

Every lookup costs the operator real money and, before this, the only ceiling
was the prepaid balance returning 402 — i.e. "until the money runs out".
"""
from src.config import settings
from src.workers import skip_trace_dispatcher


def test_cap_defaults_to_disabled_so_deploying_it_changes_nothing():
    assert settings.SKIP_TRACE_DAILY_ROW_CAP == 0


def test_tick_is_held_once_the_rolling_window_reaches_the_cap(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "t")
    monkeypatch.setattr(settings, "SKIP_TRACE_DAILY_ROW_CAP", 10)

    alerts: list[str] = []
    import src.workers.ops_alerts as ops
    monkeypatch.setattr(ops, "send_ops_alert", lambda *a, **k: alerts.append(a[0]))

    # Pretend 25 rows were already submitted inside the window.
    class _Scalar:
        def scalar_one(self):
            return 25

    class _Sess:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, *a, **k):
            return _Scalar()

    monkeypatch.setattr("src.db.session.system_sync_session", lambda *a, **k: _Sess())

    out = skip_trace_dispatcher.dispatch_pending_skip_trace.run()

    assert out["skipped"] == "daily_cap"
    assert out["spent_today"] == 25 and out["cap"] == 10
    assert "skip_trace_daily_cap" in alerts, "ops must be paged when spend is halted"


def test_the_cap_branch_does_not_shadow_module_scope(monkeypatch):
    """Regression: the cap block once did a function-local
    `from datetime import datetime`, which rebound `datetime` as LOCAL for the
    whole function — so every later `datetime.now(UTC)` raised UnboundLocalError
    on the path where the cap branch did NOT run. That broke dispatch outright.
    """
    import inspect

    src = inspect.getsource(skip_trace_dispatcher.dispatch_pending_skip_trace.run)
    # Strip comments first — the block carries an explanatory note that mentions
    # the very pattern it forbids, and matching that would be a false positive.
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "from datetime import" not in code, (
        "a function-local datetime import shadows the module-level name and "
        "breaks every later datetime.now(UTC) in this function"
    )
