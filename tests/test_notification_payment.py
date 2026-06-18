"""Tests for emit_payment_notification Celery task and Stripe webhook enqueue.

Task-body test: patches create_notification directly (no DB).
Webhook test: uses AsyncMock for the DB session — the one sanctioned mock for
this project (external-API-shaped webhook boundary; driving a real async
Stripe+DB session in a unit test is disproportionate per the testing rules).
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.workers.tasks import emit_payment_notification


def test_emit_payment_notification_calls_helper():
    with patch("src.workers.notification_emit.create_notification") as m:
        # call the task body directly (bind=True → first arg is self; pass None)
        emit_payment_notification.run("user-123", 2)
    m.assert_called_once()
    kwargs = m.call_args.kwargs
    assert kwargs["user_id"] == "user-123"
    assert kwargs["type"] == "payment_failed"
    assert kwargs["job_id"] is None
    assert kwargs["detail"] == {"attempt_count": 2}


@pytest.mark.asyncio
async def test_webhook_enqueues_payment_notification():
    from src.api.routes.billing import _handle_payment_failed

    class _Result:
        def scalar_one_or_none(self):
            class _U:
                id = "user-xyz"
                email = "p@bl.test"
            return _U()

    # AsyncMock is the sanctioned exception to the no-mock rule here:
    # driving a real Stripe webhook + async SQLAlchemy session in a unit test
    # is disproportionate for this external-API-shaped boundary.
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())
    data = {"customer": "cus_1", "attempt_count": 3}

    with patch("src.workers.delivery._send_payment_failed_email"), \
         patch("src.workers.tasks.emit_payment_notification.delay") as m:
        await _handle_payment_failed(data, db)
    m.assert_called_once_with("user-xyz", 3)
