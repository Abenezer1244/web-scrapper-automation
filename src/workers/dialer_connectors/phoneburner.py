"""PhoneBurner native dialer connector (Thread 3).

Creates CONTACTS in PhoneBurner via its public REST API — one POST per lead
(PhoneBurner has no bulk-import endpoint), so this connector is outbox-based
(`uses_outbox = True`): the sweep materializes a dialer_deliveries row per lead
and process_dialer_outbox drains them with durable per-row replay.

SECURITY / COMPLIANCE invariants (docs/dialer_connectors_spike.md):
- CONTACT CREATION ONLY. This connector NEVER targets a dial-session / auto-dial
  endpoint — the destination is hardcoded to /rest/1/contacts. BridgeLeads has no
  DNC feed (phone_dnc_flag is unknown for most leads), so it must not initiate or
  facilitate dialing; the customer is the TCPA-responsible caller. dnc_status is
  labeled on every contact and dnc_scrubbed stays implicitly False.
- Host pinned: ALLOWED_HOSTS = {www.phoneburner.com}. The transport asserts the
  built URL's host before POSTing, so PII + a bearer token can never reach another
  host. The URL is never sourced from user config.
- The OAuth access token is supplied at SEND TIME by the transport (re-read from
  the DB), never embedded in a Celery task arg. build_requests receives it via
  job_meta only because the transport calls it immediately before the POST.

NOTE: PhoneBurner's contact field names are taken from public docs
(phoneburner.com/developer); the exact names + duplicate-handling params must be
confirmed against the live API during the first smoke (the research flagged the
spec as not fully verifiable without an account). The outbox/security architecture
is independent of the exact body shape.
"""
from src.utils.address_intel import compose_situs
from src.workers.dialer_connectors.base import DialerConnector

_CONTACTS_URL = "https://www.phoneburner.com/rest/1/contacts"
_MAX_TOKEN_LEN = 4096


def _split_name(party_name: str | None) -> tuple[str, str]:
    """Best-effort first/last from a county-record owner name.

    County records are typically "LAST FIRST MIDDLE" (e.g. "CISSNA RICHARD C").
    PhoneBurner requires both names, so first token → last_name, remainder →
    first_name; a single token fills last_name with "-" as first_name. Exact
    splitting is not load-bearing for a dialer contact (the full name is preserved
    in a custom field).
    """
    name = (party_name or "").strip()
    if not name:
        return ("-", "Unknown")
    parts = name.split()
    if len(parts) == 1:
        return ("-", parts[0])
    return (" ".join(parts[1:]), parts[0])


class PhoneBurnerConnector(DialerConnector):
    VENDOR_ID = "phoneburner"
    uses_outbox = True
    carries_pii = True
    ALLOWED_HOSTS = frozenset({"www.phoneburner.com"})

    def validate_config(self, deliver: dict) -> None:
        deliver = deliver or {}
        token = deliver.get("phoneburner_access_token")
        owner = deliver.get("phoneburner_owner_id")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("phoneburner requires deliver.phoneburner_access_token")
        if len(token) > _MAX_TOKEN_LEN or any(c in token for c in "\r\n\t"):
            raise ValueError("phoneburner_access_token is malformed")
        if owner in (None, "") or not str(owner).strip():
            raise ValueError("phoneburner requires deliver.phoneburner_owner_id")

    def build_requests(self, leads: list[dict], job_meta: dict) -> list[dict]:
        """Build one contact-create request per lead.

        ``job_meta`` carries ``phoneburner_access_token`` + ``phoneburner_owner_id``
        (the transport populated them from the DB at send time). One lead in →
        one request out (the outbox drains a single row per call).
        """
        token = job_meta["phoneburner_access_token"]
        owner_id = job_meta["phoneburner_owner_id"]
        requests_out: list[dict] = []
        for ld in leads:
            first_name, last_name = _split_name(ld.get("party_name"))
            dnc_status = self.map_dnc_status(ld.get("phone_dnc_flag"))
            body = {
                "owner_id": owner_id,
                "first_name": first_name,
                "last_name": last_name,
                "phone_number": ld.get("phone"),
                "email_address": ld.get("email"),
                # Rebuild the full situs line rather than pushing the
                # street-only value: migration 085 froze property_address to the
                # street and moved city/ZIP into their own columns, so a bare
                # "9226 175TH STREET CT E" reached the dialer with no town.
                # compose_situs is canonical-not-append-only, so a source that
                # already carries its own tail is not given a second copy.
                "address": compose_situs(
                    ld.get("property_address"),
                    ld.get("property_city"),
                    ld.get("property_state"),
                    ld.get("property_zip"),
                ),
                # PhoneBurner-side dedup backstop (NOT our replay model). Confirm
                # exact param names against the live API on first smoke.
                "duplicate_checks": "phone",
                "on_duplicate": "skip",
                "custom_fields": {
                    "bridgeleads_external_id": f"bridgeleads:result:{ld.get('id')}",
                    "owner_name": ld.get("party_name"),
                    "dnc_status": dnc_status,
                    "mailing_address": ld.get("mailing_address"),
                    "record_type": job_meta.get("record_type"),
                    "county": job_meta.get("county"),
                    "state": job_meta.get("state"),
                },
            }
            requests_out.append({
                "vendor_id": self.VENDOR_ID,
                "url": _CONTACTS_URL,
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                "body": body,
                "result_id": str(ld.get("id")),
                "carries_pii": self.carries_pii,
            })
        return requests_out
