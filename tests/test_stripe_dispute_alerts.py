"""Stripe chargebacks page ops and change no entitlement (compliance audit 2026-11, 5b).

Real signed webhook deliveries through POST /billing/webhook, real Postgres ledger.
send_ops_alert is observed with pytest's monkeypatch (its routes are covered in
tests/test_ops_alerts.py); the alert runs as a post-commit background task.
"""

import hashlib
import hmac
import json
import time
import uuid

import pytest
from sqlalchemy import text

from src.config import settings

_WEBHOOK_SECRET = "whsec_dispute_alert_signature_secret_0123456789"


def _signed(event: dict) -> tuple[bytes, str]:
    payload = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(_WEBHOOK_SECRET.encode(), f"{ts}.".encode() + payload,
                   hashlib.sha256).hexdigest()
    return payload, f"t={ts},v1={sig}"


def _event(event_type: str, charge: object = "ch_123") -> dict:
    return {
        "id": f"evt_{uuid.uuid4().hex}",
        "object": "event",
        "type": event_type,
        "data": {"object": {
            "id": "dp_456", "object": "dispute", "charge": charge, "amount": 1234,
            "currency": "usd", "reason": "fraudulent", "status": "needs_response",
            # Present on real disputes; must never reach the alert.
            "evidence": {"customer_email_address": "owner@example.com"},
            "metadata": {"note": "private"},
        }},
    }


@pytest.fixture
def alerts(monkeypatch) -> list[tuple]:
    from src.workers import ops_alerts

    sent: list[tuple] = []
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    monkeypatch.setattr(ops_alerts, "send_ops_alert",
                        lambda kind, key, subject, body: sent.append((kind, key, subject, body)))
    return sent


async def _cleanup(db, event_id: str) -> None:
    await db.execute(text("DELETE FROM stripe_webhook_events WHERE event_id = :e"),
                     {"e": event_id})
    await db.commit()


@pytest.mark.parametrize("event_type", [
    "charge.dispute.created", "charge.dispute.closed",
    "charge.dispute.funds_withdrawn", "charge.dispute.funds_reinstated",
])
async def test_each_dispute_event_pages_ops_with_ids_only(client, db, alerts, event_type):
    event = _event(event_type)
    payload, sig = _signed(event)
    try:
        r = await client.post("/billing/webhook", content=payload,
                              headers={"stripe-signature": sig})
        assert r.status_code == 200, r.text
        assert len(alerts) == 1
        kind, key, subject, body = alerts[0]
        assert kind == "stripe_dispute"
        assert key == f"{event_type}:dp_456"  # per event, so 'closed' is never swallowed
        assert "12.34 USD" in subject
        for fact in ("dp_456", "ch_123", "fraudulent", "needs_response",
                     "No account access was changed"):
            assert fact in body, fact
        assert "owner@example.com" not in subject + body and "private" not in body
    finally:
        await _cleanup(db, event["id"])


async def test_a_replayed_dispute_event_pages_once(client, db, alerts):
    event = _event("charge.dispute.created")
    payload, sig = _signed(event)
    try:
        for _ in range(2):
            r = await client.post("/billing/webhook", content=payload,
                                  headers={"stripe-signature": sig})
            assert r.status_code == 200
        assert len(alerts) == 1
        row = (await db.execute(text(
            "SELECT event_type FROM stripe_webhook_events WHERE event_id = :e"),
            {"e": event["id"]})).first()
        assert row is not None and row.event_type == "charge.dispute.created"
    finally:
        await _cleanup(db, event["id"])


async def test_an_expanded_charge_object_still_reports_its_id(client, db, alerts):
    event = _event("charge.dispute.created", charge={"id": "ch_expanded", "object": "charge"})
    payload, sig = _signed(event)
    try:
        r = await client.post("/billing/webhook", content=payload,
                              headers={"stripe-signature": sig})
        assert r.status_code == 200
        assert "Charge: ch_expanded" in alerts[0][3]
    finally:
        await _cleanup(db, event["id"])


async def test_an_alerting_failure_never_fails_the_webhook(client, db, monkeypatch):
    from src.workers import ops_alerts

    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET)

    def _boom(*a, **k):
        raise RuntimeError("alert route down")

    monkeypatch.setattr(ops_alerts, "send_ops_alert", _boom)
    event = _event("charge.dispute.created")
    payload, sig = _signed(event)
    try:
        r = await client.post("/billing/webhook", content=payload,
                              headers={"stripe-signature": sig})
        assert r.status_code == 200  # the ledger committed; Stripe must not retry
    finally:
        await _cleanup(db, event["id"])


async def test_a_zero_decimal_currency_is_not_divided_by_100(client, db, alerts):
    event = _event("charge.dispute.created")
    event["data"]["object"].update(amount=1234, currency="jpy")
    payload, sig = _signed(event)
    try:
        r = await client.post("/billing/webhook", content=payload,
                              headers={"stripe-signature": sig})
        assert r.status_code == 200
        assert "1,234 JPY" in alerts[0][2] and "12.34" not in alerts[0][2]
    finally:
        await _cleanup(db, event["id"])
