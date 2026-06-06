"""Security regression — webhook HMAC secrets must be WRITE-ONLY in responses.

ScraperConfigResponse must never echo webhook_secret / dialer_webhook_secret
back to the client (a stolen frontend token / XSS could otherwise read them and
forge signed webhook/dialer payloads). Only presence flags are exposed. (Codex
security review, Phase 3-5 hardening.)
"""
from datetime import UTC, datetime

from src.api.schemas import ScraperConfigResponse


def _resp(deliver: dict) -> ScraperConfigResponse:
    now = datetime.now(UTC)
    return ScraperConfigResponse(
        id="i", user_id="u", name="n", county="c", state="WA",
        record_type="tax_delinquent", fields={}, enrichment={}, schedule={},
        deliver=deliver, active=True, created_at=now, updated_at=now,
    )


def test_secrets_stripped_from_response():
    r = _resp({
        "webhook_url": "https://hook.example/x",
        "webhook_secret": "sek_" + "a" * 24,
        "dialer_webhook_url": "https://dialer.example/y",
        "dialer_webhook_secret": "dsk_" + "b" * 24,
    })
    assert "webhook_secret" not in r.deliver
    assert "dialer_webhook_secret" not in r.deliver
    # Presence flags reflect that secrets WERE set, without leaking the value.
    assert r.deliver["webhook_secret_set"] is True
    assert r.deliver["dialer_webhook_secret_set"] is True
    # Non-secret fields (URLs) are preserved.
    assert r.deliver["webhook_url"] == "https://hook.example/x"
    assert r.deliver["dialer_webhook_url"] == "https://dialer.example/y"


def test_presence_flags_false_when_unset():
    r = _resp({"webhook_url": "https://hook.example/x"})
    assert r.deliver["webhook_secret_set"] is False
    assert r.deliver["dialer_webhook_secret_set"] is False


def test_no_secret_value_anywhere_in_serialized_response():
    secret = "sek_" + "z" * 24
    r = _resp({"webhook_url": "https://x", "webhook_secret": secret})
    # Full model dump must not contain the secret string anywhere.
    assert secret not in str(r.model_dump())


def test_phoneburner_token_is_write_only():
    # Thread 3: the PhoneBurner OAuth token is a credential — same treatment.
    token = "pbtok_" + "q" * 30
    r = _resp({
        "dialer_type": "phoneburner",
        "phoneburner_owner_id": "4242",
        "phoneburner_access_token": token,
    })
    assert "phoneburner_access_token" not in r.deliver
    assert r.deliver["phoneburner_access_token_set"] is True
    # owner_id is not a secret — preserved so the UI can show it.
    assert r.deliver["phoneburner_owner_id"] == "4242"
    assert token not in str(r.model_dump())


def test_phoneburner_token_flag_false_when_unset():
    r = _resp({"dialer_type": "phoneburner", "phoneburner_owner_id": "1"})
    assert r.deliver["phoneburner_access_token_set"] is False
