"""Thread 3 — dialer connector seam.

The safety net for routing the shipped generic dialer push through the connector
abstraction: GenericWebhookConnector must produce the SAME payload + destination as
the prior direct `build_dialer_push_payload` call, so the refactor is behavior-
preserving. No network, no DB.
"""
import pytest

from src.workers.dialer_connectors import get_connector
from src.workers.dialer_connectors.base import DialerConnector
from src.workers.dialer_connectors.generic_webhook import GenericWebhookConnector
from src.workers.webhook_delivery import build_dialer_push_payload

_LEADS = [
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "party_name": "DOE JANE",
        "phone": "4255551212",
        "phone_type": "Mobile",
        "phone_dnc_flag": False,
        "email": "jane@example.com",
        "property_address": "1 MAIN ST, EVERETT WA 98201",
        "mailing_address": "PO BOX 1, EVERETT WA 98201",
    },
    {
        "id": "22222222-2222-2222-2222-222222222222",
        "party_name": "ROE JOHN",
        "phone": "4255553434",
        "phone_type": "Landline",
        "phone_dnc_flag": None,  # unknown DNC
        "email": None,
        "property_address": "2 OAK AVE, LYNNWOOD WA 98036",
        "mailing_address": None,
    },
]

_JOB_META = {
    "job_id": "99999999-9999-9999-9999-999999999999",
    "scraper_config_id": "88888888-8888-8888-8888-888888888888",
    "scraper_name": "Snohomish Tax Weekly",
    "county": "snohomish",
    "state": "WA",
    "record_type": "tax_delinquent",
    "total_dialer_ready_count": 7,
    "dialer_webhook_url": "https://hooks.zapier.com/abc/def",
    "dialer_webhook_secret": "a-long-enough-shared-secret-value-123456",
}


def _strip_volatile(payload: dict) -> dict:
    # delivered_at is a wall-clock timestamp; signature is HMAC over the payload
    # (incl. delivered_at). Both legitimately differ between two calls — compare
    # the meaningful content with them removed.
    p = dict(payload)
    p.pop("delivered_at", None)
    p.pop("signature", None)
    return p


def test_generic_connector_payload_matches_legacy_builder():
    reference = build_dialer_push_payload(
        job_id=_JOB_META["job_id"],
        scraper_config_id=_JOB_META["scraper_config_id"],
        scraper_name=_JOB_META["scraper_name"],
        county=_JOB_META["county"],
        state=_JOB_META["state"],
        record_type=_JOB_META["record_type"],
        leads=_LEADS,
        total_dialer_ready_count=_JOB_META["total_dialer_ready_count"],
        webhook_secret=_JOB_META["dialer_webhook_secret"],
    )
    reqs = GenericWebhookConnector().build_requests(_LEADS, _JOB_META)

    assert len(reqs) == 1
    req = reqs[0]
    assert req["url"] == _JOB_META["dialer_webhook_url"]
    assert req["vendor_id"] == "generic_webhook"
    assert req["carries_pii"] is True
    # Byte-identical content (minus the volatile timestamp/signature).
    assert _strip_volatile(req["payload"]) == _strip_volatile(reference)
    # And the signature is still present + non-empty (secret was provided).
    assert req["payload"]["signature"]


def test_get_connector_resolves_generic_for_none_and_explicit():
    assert isinstance(get_connector(None), GenericWebhookConnector)
    assert isinstance(get_connector("generic_webhook"), GenericWebhookConnector)


def test_get_connector_rejects_unknown_vendor():
    with pytest.raises(ValueError, match="Unknown dialer_type"):
        get_connector("totally_made_up")


def test_validate_config_requires_dialer_webhook_url():
    c = GenericWebhookConnector()
    c.validate_config({"dialer_webhook_url": "https://x.example/y"})  # ok
    with pytest.raises(ValueError, match="dialer_webhook_url"):
        c.validate_config({})


@pytest.mark.parametrize("flag,expected", [
    (False, "clear"),
    (True, "dnc"),
    (None, "unknown"),
])
def test_map_dnc_status(flag, expected):
    assert DialerConnector.map_dnc_status(flag) == expected
