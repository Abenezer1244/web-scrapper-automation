"""SQLAlchemy column types that transparently encrypt PII at rest (H3).

``EncryptedString`` and ``EncryptedJSON`` wrap the Fernet primitive in
``src.utils.crypto`` so ORM reads/writes of in-scope contact PII
(phone/email/phones/emails/raw_response) are encrypted on the way to the DB and
decrypted on the way back, with no change at the call sites.

Important boundaries:

* These types only run for ORM-mapped and SQLAlchemy Core reads/writes against
  the mapped column. Raw ``text()`` SQL bypasses them — those paths decrypt
  explicitly (see the spec's raw-SQL section).
* ``None`` passes through untouched both directions, and blank/whitespace-only
  strings bind to ``None``. This keeps ``IS NOT NULL`` / ``trim(col) != ''``
  contactability predicates correct: there is never an empty-string ciphertext
  that would falsely read as "contactable".
* Storage is ``Text`` (Fernet tokens far exceed the old ``String(32)``/``(255)``
  widths). Migration 046 widens the columns.
"""

import json
from typing import Any

from sqlalchemy.types import String, Text, TypeDecorator

# NOTE: src.utils.crypto (and src.utils.contact_decode, which imports it) are imported
# LAZILY inside the bind/result methods, not at module top level. alembic/env.py
# imports Base (-> models -> this module) for every Alembic command; a top-level
# crypto import would instantiate Settings (SECRET_KEY, REDIS_URL, ...) and break
# migration-only environments that only provide the DB URL.


class EncryptedString(TypeDecorator):
    """Text column whose value is Fernet-encrypted at rest.

    Blank/whitespace-only inputs normalize to NULL so encrypted columns never
    hold an empty-string ciphertext.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        if text.strip() == "":
            return None
        from src.utils.crypto import encrypt_field
        return encrypt_field(text)

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        from src.utils.crypto import decrypt_field
        return decrypt_field(value)


class EncryptedJSON(TypeDecorator):
    """Text column holding a JSON value, Fernet-encrypted at rest.

    Serializes to JSON, encrypts the JSON text, and stores the token. ``None``
    passes through as SQL NULL (distinct from a stored JSON ``null``).
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        from src.utils.crypto import encrypt_field
        return encrypt_field(json.dumps(value, separators=(",", ":")))

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        from src.utils.crypto import decrypt_field
        return json.loads(decrypt_field(value))


# ─── Contact columns (UX 3.8s1) ───────────────────────────────────────────────
# A lead's contact PII reaches clients (Results, run CSVs, scheduled R2 exports,
# deliveries, the dialer) through ORM reads of these columns, so the read side runs
# the one contact decoder (src.utils.contact_decode): a value that cannot be read
# becomes None and is logged, instead of leaking ciphertext (tolerant mode) or
# failing the whole read (strict mode). The bind side, and the stored bytes, are
# exactly the parent types'. A type cannot see its row, so its log line names the
# column only.
#
# Only Result uses these. SkipTraceCache deliberately keeps the plain types: its
# cache-hit copy derives hit/miss from the values it reads, so a scrub there would
# turn a corrupt entry into a fabricated miss; the residue it may copy is scrubbed
# when the Result is read.


class EncryptedContactString(EncryptedString):
    """``EncryptedString`` for a contact scalar (``phone`` / ``email``)."""

    cache_ok = True

    def __init__(self, field: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.field = field

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        from src.utils.contact_decode import decode_scalar
        return decode_scalar(value, field=self.field)[0]


class EncryptedContactJSON(EncryptedJSON):
    """``EncryptedJSON`` for a contact array (``phones`` / ``emails``)."""

    cache_ok = True

    def __init__(self, kind: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.kind = kind

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        from src.utils.contact_decode import decode_array
        return decode_array(value, kind=self.kind)[0]


class ContactLabel(TypeDecorator):
    """A plain-text contact label column (``phone_type``), cleaned on read.

    Not encrypted (the label is not PII). Storage and bind are the column's existing
    ``String(16)``; the read side drops residue and blanks with the decoder's phone
    type rule. No constructor arguments, so the statement cache key is the class.
    """

    impl = String(16)
    cache_ok = True

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        from src.utils.contact_decode import clean_phone_type
        return clean_phone_type(value)[0]
