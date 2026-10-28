"""Webhook destination URLs are write-only in every user-facing readback.

An unsigned webhook (e.g. a Zapier catch-hook) is authenticated by nothing but
its URL, so GET /scrapers and the account export return a deterministic mask
(scheme + host + "/<hidden>"). An edit form that echoes the mask back unchanged
keeps the stored URL; blank still removes it; a mask that cannot be matched to
the stored URL is never persisted.
"""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.routes.scrapers import _merge_deliver
from src.api.schemas import (
    DeliverConfig,
    DeliverUpdate,
    ScraperConfigResponse,
    mask_webhook_url,
    redact_deliver,
)
from src.workers.account_export import _batch

ZAP = "https://hooks.zapier.com/hooks/catch/123456/abcdef/?token=zz"
DIALER = "https://dialer.example.com:8443/push/s3cr3t"
SECRET = "sek_" + "a" * 24


@pytest.mark.parametrize(
    "url, masked",
    [
        (ZAP, "https://hooks.zapier.com/<hidden>"),
        (DIALER, "https://dialer.example.com:8443/<hidden>"),
        ("https://user:pw@host.example/p#frag", "https://host.example/<hidden>"),
        ("https://[2001:db8::1]:9000/x", "https://[2001:db8::1]:9000/<hidden>"),
        ("not a url", "https://invalid/<hidden>"),
        ("https://host.example:99999/x", "https://host.example/<hidden>"),
        # Malformed authorities raise inside urlparse/.hostname: never crash.
        ("https://[broken/path?k=s3cr3t", "https://invalid/<hidden>"),
        ("https://[::1/x", "https://invalid/<hidden>"),
        (None, None),
        ("", ""),
    ],
)
def test_mask_keeps_only_scheme_host_and_port(url, masked):
    assert mask_webhook_url(url) == masked


def test_response_masks_both_urls_and_keeps_truthiness():
    now = datetime.now(UTC)
    r = ScraperConfigResponse(
        id="i", user_id="u", name="n", county="c", state="WA",
        record_type="tax_delinquent", fields={}, enrichment={}, schedule={},
        deliver={"webhook_url": ZAP, "dialer_webhook_url": DIALER, "webhook_secret": SECRET},
        active=True, created_at=now, updated_at=now,
    )
    dumped = str(r.model_dump())
    assert "abcdef" not in dumped and "s3cr3t" not in dumped and SECRET not in dumped
    assert r.deliver["webhook_url"] == "https://hooks.zapier.com/<hidden>"
    assert r.deliver["webhook_secret_set"] is True


def test_redact_deliver_does_not_mutate_the_stored_dict():
    stored = {"webhook_url": ZAP, "webhook_secret": SECRET}
    redact_deliver(stored)
    assert stored == {"webhook_url": ZAP, "webhook_secret": SECRET}


def test_account_export_batch_masks_urls_and_drops_secrets():
    batch = SimpleNamespace(
        id="b", name="n", state="WA", status="active", delivery_mode="email",
        fields={}, enrichment={}, schedule={}, created_at=None,
        deliver={"webhook_url": ZAP, "dialer_webhook_url": DIALER, "dialer_webhook_secret": SECRET},
    )
    out = str(_batch(batch))
    assert "abcdef" not in out and "s3cr3t" not in out and SECRET not in out
    assert "https://hooks.zapier.com/<hidden>" in out


def test_malformed_legacy_url_cannot_break_a_read_or_an_export():
    bad = "https://[broken/path?k=s3cr3t"
    now = datetime.now(UTC)
    r = ScraperConfigResponse(
        id="i", user_id="u", name="n", county="c", state="WA",
        record_type="tax_delinquent", fields={}, enrichment={}, schedule={},
        deliver={"webhook_url": bad}, active=True, created_at=now, updated_at=now,
    )
    batch = SimpleNamespace(
        id="b", name="n", state="WA", status="active", delivery_mode="email",
        fields={}, enrichment={}, schedule={}, created_at=None, deliver={"dialer_webhook_url": bad},
    )
    assert r.deliver["webhook_url"] == "https://invalid/<hidden>"
    assert "s3cr3t" not in str(_batch(batch))


def test_a_real_endpoint_that_looks_like_the_old_mask_is_a_normal_url():
    # Only the RFC-illegal "/<hidden>" is reserved; "/****" is just a path.
    assert DeliverConfig(webhook_url="https://example.com/****").webhook_url == "https://example.com/****"
    stored = {"webhook_url": "https://example.com/old"}
    merged = _merge_deliver(stored, DeliverUpdate(webhook_url="https://example.com/****"))
    assert merged.webhook_url == "https://example.com/****"


def _stored():
    return {"webhook_url": ZAP, "webhook_secret": SECRET, "dialer_webhook_url": DIALER}


def test_untouched_edit_echo_keeps_both_stored_urls_and_secret():
    echo = redact_deliver(_stored())
    merged = _merge_deliver(_stored(), DeliverUpdate(**echo))
    assert merged.webhook_url == ZAP
    assert merged.dialer_webhook_url == DIALER
    assert merged.webhook_secret == SECRET


def test_blank_url_still_removes_the_destination_and_its_secret():
    echo = redact_deliver(_stored())
    echo["webhook_url"] = ""
    merged = _merge_deliver(_stored(), DeliverUpdate(**echo))
    assert merged.webhook_url is None
    assert merged.webhook_secret is None
    assert merged.dialer_webhook_url == DIALER


def test_a_new_url_replaces_the_stored_one():
    echo = redact_deliver(_stored())
    echo["webhook_url"] = "https://hooks.zapier.com/hooks/catch/999/new/"
    merged = _merge_deliver(_stored(), DeliverUpdate(**echo))
    assert merged.webhook_url == "https://hooks.zapier.com/hooks/catch/999/new/"


def test_an_unmatched_mask_is_rejected_not_persisted():
    # A mask edited to another host, or pasted where nothing is stored.
    for stored, url in [
        (_stored(), "https://other.example/<hidden>"),
        ({}, "https://hooks.zapier.com/<hidden>"),
    ]:
        with pytest.raises(HTTPException) as exc:
            _merge_deliver(stored, DeliverUpdate(webhook_url=url))
        assert exc.value.status_code == 422


def test_create_rejects_a_pasted_mask():
    with pytest.raises(ValueError):
        DeliverConfig(webhook_url="https://hooks.zapier.com/<hidden>")
