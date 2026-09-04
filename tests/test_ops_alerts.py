"""M6 ops alerts: configuration gates. Never sends a real email.

The send path is exercised in prod by construction (best-effort + cooldown);
these lock the SAFE defaults: e-mail DELIVERY is a no-op unless explicitly
configured, and a missing Resend key can never raise out of a worker task.

NOTE (2026-09-04): "no-op" now means *no e-mail*, not *no side effect*. Every
alert-worthy call also queues a durable audit_events row on a background worker
thread — because OPS_ALERT_EMAIL was blank in production and the old silent
return meant a four-week crawl outage left no trace anywhere. These tests do not
assert on that; see tests/test_nts_upsert_and_ops_alert_durability.py, which
injects a fake session via ops_alerts._session_for_persist.
"""
from src.config import settings
from src.workers.ops_alerts import send_ops_alert


def test_disabled_by_default_is_silent_noop():
    # Dev/CI default: OPS_ALERT_EMAIL empty -> returns False, sends nothing,
    # raises nothing (the watchdog/canary callers depend on this contract).
    assert settings.OPS_ALERT_EMAIL == ""
    assert send_ops_alert("canary", "x/WA", "subject", "body") is False


def test_never_raises_even_with_weird_input():
    # Best-effort contract: garbage in -> False out, no exception.
    assert send_ops_alert("", "", "", "") is False
    assert send_ops_alert("k" * 500, "y" * 500, "s" * 500, "<b>&</b>" * 100) is False


def test_configured_send_failure_never_raises(monkeypatch):
    """Resend (external API — mock justified) blowing up must return False,
    never raise into the watchdog/canary caller (Codex P2 coverage gap)."""
    import sys
    import types

    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "ops@test.bridgeleads.io")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_not_real")
    # Cooldown: force-acquired so the send path is reached without Redis.
    import src.workers.ops_alerts as oa
    monkeypatch.setattr(oa, "_cooldown_acquired", lambda kind, key: True)

    fake = types.ModuleType("resend")
    class _Emails:
        @staticmethod
        def send(_payload):
            raise RuntimeError("resend exploded")
    fake.Emails = _Emails
    fake.api_key = None
    monkeypatch.setitem(sys.modules, "resend", fake)

    assert send_ops_alert("canary", "x/WA", "subject", "body") is False  # not raised


def test_configured_send_success_and_cooldown_suppression(monkeypatch):
    """Happy path returns True and the payload is well-formed; a cooldown-held
    bucket suppresses the send entirely."""
    import sys
    import types

    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "ops@test.bridgeleads.io")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_not_real")
    import src.workers.ops_alerts as oa

    sent: list[dict] = []
    fake = types.ModuleType("resend")
    class _Emails:
        @staticmethod
        def send(payload):
            sent.append(payload)
    fake.Emails = _Emails
    fake.api_key = None
    monkeypatch.setitem(sys.modules, "resend", fake)

    monkeypatch.setattr(oa, "_cooldown_acquired", lambda kind, key: True)
    assert send_ops_alert("watchdog", "job-1", "Job died", "job_id=job-1") is True
    assert sent and "[BridgeLeads OPS] Job died" == sent[0]["subject"]
    assert "job_id=job-1" in sent[0]["html"]

    sent.clear()
    monkeypatch.setattr(oa, "_cooldown_acquired", lambda kind, key: False)
    assert send_ops_alert("watchdog", "job-1", "Job died", "job_id=job-1") is False
    assert sent == []  # suppressed — nothing sent
