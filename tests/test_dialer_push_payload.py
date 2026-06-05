"""Phase 5 — build_dialer_push_payload (generic "push to any dialer").

Pure unit tests (no DB / no network). Locks the payload contract dialers/Zapier
consumers depend on: stable per-lead external_id, batch id, truncation metadata,
flattened scraper fields, and HMAC signing.
"""
import hashlib
import hmac
import json

from src.workers.webhook_delivery import (
    DIALER_PUSH_CAP,
    build_dialer_push_payload,
)


def _leads(n: int, dnc=None) -> list[dict]:
    return [
        {
            "id": f"id-{i}",
            "party_name": f"Owner {i}",
            "phone": f"206555{i:04d}",
            "phone_type": "Mobile",
            "phone_dnc_flag": dnc,
            "email": f"o{i}@example.com",
            "property_address": f"{i} Main St",
            "mailing_address": f"{i} PO Box",
        }
        for i in range(n)
    ]


def _build(leads, total, secret=None):
    return build_dialer_push_payload(
        job_id="job-1",
        scraper_config_id="cfg-1",
        scraper_name="King Tax Weekly",
        county="king",
        state="WA",
        record_type="tax_delinquent",
        leads=leads,
        total_dialer_ready_count=total,
        webhook_secret=secret,
    )


class TestPayloadShape:
    def test_event_and_schema_version(self):
        p = _build(_leads(2), 2)
        assert p["event"] == "leads.dialer_ready"
        assert p["schema_version"].endswith("dialer_ready.v1")

    def test_batch_id_stable_per_job(self):
        p = _build(_leads(1), 1)
        assert p["batch"]["id"] == "job:job-1:dialer-ready:v1"
        assert p["batch"]["job_id"] == "job-1"

    def test_lead_has_stable_external_id_and_flattened_scraper_fields(self):
        p = _build(_leads(1), 1)
        lead = p["leads"][0]
        assert lead["external_id"] == "bridgeleads:result:id-0"
        assert lead["id"] == "id-0"
        assert lead["phone"] == "2065550000"
        # scraper fields flattened onto each lead for Zapier "create contact"
        assert lead["county"] == "king"
        assert lead["state"] == "WA"
        assert lead["record_type"] == "tax_delinquent"

    def test_dnc_honesty_unknown_and_clear(self):
        # Honest labeling: NULL DNC -> "unknown" + payload says dnc_scrubbed=False
        # so the consumer/dialer knows BridgeLeads did not scrub the registry.
        p_unknown = _build(_leads(1, dnc=None), 1)
        assert p_unknown["dnc_scrubbed"] is False
        assert p_unknown["leads"][0]["dnc_status"] == "unknown"
        p_clear = _build(_leads(1, dnc=False), 1)
        assert p_clear["leads"][0]["dnc_status"] == "clear"

    def test_counts_and_not_truncated(self):
        p = _build(_leads(3), 3)
        assert p["lead_count"] == 3
        assert p["total_dialer_ready_count"] == 3
        assert p["truncated"] is False

    def test_truncated_when_total_exceeds_capped_leads(self):
        # Caller caps the list; total reflects the full dialer-ready set.
        p = _build(_leads(DIALER_PUSH_CAP), DIALER_PUSH_CAP + 250)
        assert p["lead_count"] == DIALER_PUSH_CAP
        assert p["total_dialer_ready_count"] == DIALER_PUSH_CAP + 250
        assert p["truncated"] is True


class TestSigning:
    def test_unsigned_when_no_secret(self):
        p = _build(_leads(1), 1, secret=None)
        assert p["signature"] == ""

    def test_hmac_matches_canonical_payload(self):
        secret = "x" * 24
        p = _build(_leads(1), 1, secret=secret)
        sig = p["signature"]
        assert sig
        # Recompute over the canonical payload sans signature (mirrors _sign_payload).
        unsigned = {k: v for k, v in p.items() if k != "signature"}
        expected = hmac.new(
            secret.encode(),
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        assert sig == expected
