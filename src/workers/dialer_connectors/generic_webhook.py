"""Generic webhook/Zapier dialer connector — the shipped P5 push, made pluggable.

Wraps `build_dialer_push_payload` byte-for-byte so routing the existing generic
push through the connector seam is a behavior-preserving refactor (locked by the
regression test in tests/test_dialer_connector_base.py). One request per job batch.
"""
from src.workers.dialer_connectors.base import DialerConnector
from src.workers.webhook_delivery import build_dialer_push_payload


class GenericWebhookConnector(DialerConnector):
    VENDOR_ID = "generic_webhook"

    def validate_config(self, deliver: dict) -> None:
        url = (deliver or {}).get("dialer_webhook_url")
        if not url:
            raise ValueError("generic_webhook requires deliver.dialer_webhook_url")

    def build_requests(self, leads: list[dict], job_meta: dict) -> list[dict]:
        """Build the single generic-webhook delivery request.

        Returns one request: the existing dialer-ready payload + its destination
        URL. The HMAC secret is used only to SIGN the payload (never sent), so it
        is consumed here and not surfaced as a request field. The URL itself is a
        user-provided catch-hook secret → the transport re-reads it from the DB at
        send time rather than from a Celery task arg.
        """
        payload = build_dialer_push_payload(
            job_id=job_meta["job_id"],
            scraper_config_id=job_meta["scraper_config_id"],
            scraper_name=job_meta["scraper_name"],
            county=job_meta["county"],
            state=job_meta["state"],
            record_type=job_meta["record_type"],
            leads=leads,
            total_dialer_ready_count=job_meta["total_dialer_ready_count"],
            webhook_secret=job_meta.get("dialer_webhook_secret"),
        )
        return [{
            "vendor_id": self.VENDOR_ID,
            "url": job_meta["dialer_webhook_url"],
            "payload": payload,
            "carries_pii": self.carries_pii,
        }]
