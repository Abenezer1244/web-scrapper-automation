"""Segments multi-contact: the raw-SQL decrypt path + the overlap CSV columns.

The segment queries now SELECT results.phones/emails (EncryptedJSON — raw
text() bypasses the ORM type, so _decrypt_pii_rows must decrypt+parse them).
Locks: ciphertext arrays decrypt to lists, legacy plaintext-JSON rows still
parse, garbage degrades to None (blank CSV cells, never a 500), and the
overlap CSV writer emits phone_2/3 + email_2/3 from the arrays.
"""
import csv
import io
import json

from src.api.routes.segments import _decrypt_pii_rows
from src.utils.crypto import encrypt_field
from src.utils.lead_export import write_lead_csv_with_overlap


class _Row:
    """Mimics a SQLAlchemy raw-result row: only ._mapping is read."""

    def __init__(self, mapping: dict):
        self._mapping = mapping


_BASE = {
    "id": "r1",
    "date_recorded": "01/05/2026",
    "party_name": "DOE JOHN",
    "parcel_id": "123",
    "property_address": "1 MAIN ST, EVERETT WA",
    "mailing_address": "1 MAIN ST, EVERETT WA",
    "county": "snohomish",
    "state": "WA",
    "phone_type": "mobile",
    "matched_record_types": ["probate"],
    "overlap_count": 1,
}

_PHONES = [
    {"number": "4255550100", "type": "mobile"},
    {"number": "4255550101", "type": "landline"},
    {"number": "4255550102", "type": None},
]
_EMAILS = ["a@x.com", "b@x.com", "c@x.com"]


def _encrypted_row(**over) -> _Row:
    m = dict(
        _BASE,
        phone=encrypt_field("4255550100"),
        email=encrypt_field("a@x.com"),
        phones=encrypt_field(json.dumps(_PHONES)),
        emails=encrypt_field(json.dumps(_EMAILS)),
    )
    m.update(over)
    return _Row(m)


def test_decrypt_parses_encrypted_multi_contact_arrays():
    [r] = _decrypt_pii_rows([_encrypted_row()])
    assert r.phone == "4255550100"
    assert r.phones == _PHONES
    assert r.emails == _EMAILS


def test_decrypt_null_arrays_pass_through():
    [r] = _decrypt_pii_rows([_encrypted_row(phones=None, emails=None)])
    assert r.phones is None
    assert r.emails is None


def test_decrypt_legacy_plaintext_json_still_parses():
    # Pre-encryption rows held plain JSON; decrypt_field (non-strict) passes
    # non-Fernet text through unchanged, and json.loads parses it.
    [r] = _decrypt_pii_rows([_encrypted_row(phones=json.dumps(_PHONES))])
    assert r.phones == _PHONES


def test_decrypt_garbage_degrades_to_none_not_500():
    [r] = _decrypt_pii_rows([_encrypted_row(phones="not json at all{{")])
    assert r.phones is None  # blank CSV cells, never an exception


def test_overlap_csv_emits_secondary_contacts_from_arrays():
    [r] = _decrypt_pii_rows([_encrypted_row()])
    out = io.StringIO()
    write_lead_csv_with_overlap(
        [(r, {"lists_count": 2, "lists": "Probate; Tax Delinquent", "counties": "snohomish"})],
        out,
    )
    # Parse with the csv module — addresses contain commas, so DictWriter
    # quotes them and a naive split() breaks (Codex P2).
    [cols] = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert cols["phone_2"] == "4255550101"
    assert cols["phone_3"] == "4255550102"
    assert cols["email_2"] == "b@x.com"
    assert cols["email_3"] == "c@x.com"
    assert cols["overlap"] == "Overlap"
