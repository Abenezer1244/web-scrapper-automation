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

from sqlalchemy.types import Text, TypeDecorator

from src.utils.crypto import decrypt_field, encrypt_field


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
        return encrypt_field(text)

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
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
        return encrypt_field(json.dumps(value, separators=(",", ":")))

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        return json.loads(decrypt_field(value))
