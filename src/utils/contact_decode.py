"""The one contact decoder: stored contact PII in, plaintext or nothing out.

Every path that hands a lead's contact (phone, email, phones, emails, phone type) to
a client goes through here: the ORM column types in ``src.db.encrypted_types`` call
the ``clean_*`` functions on values ``decrypt_field`` already produced, and raw
``text()`` SQL paths call the ``decode_*`` functions on the stored column text.

Why it exists. ``decrypt_field`` (``src.utils.crypto``) returns an undecryptable
value AS-IS in tolerant mode, so a corrupt or wrong-key token used to reach the
Results page, the CSV exports and the dialer as raw ciphertext; in strict mode it
raises, which used to fail a whole page for one bad row. Here, for contact fields
only, a value that cannot be read becomes ``None``, is logged, and never leaks.

Decision table (``decrypt_field`` is the only decryption primitive and is unchanged):

    stored value                      tolerant           strict             returns
    NULL                              -                  -                  None, ok
    scalar blank                      -                  -                  None, ok
    array column blank                -                  -                  None, FAILED
    fe1: + decryptable                plaintext          plaintext          cleaned plaintext
    fe1: + NOT decryptable            returned as-is     InvalidToken       None, FAILED
    bare Fernet token, decryptable    plaintext          plaintext          cleaned plaintext
    bare token, NOT decryptable       returned as-is     InvalidToken       None, FAILED
    legacy plaintext                  returned as-is     InvalidToken       tolerant kept, strict None FAILED

Cleaning (scalars and every array entry): trimmed; blank is no value; a value that
still starts with ``fe1:`` or has the Fernet token shape is ciphertext residue and is
dropped as a failure. A phone number or an email address can have neither shape.

Only ``InvalidToken`` is caught. A key or config error (raised by
``crypto._instance()`` outside the token catch) propagates and fails the request.

Every failure logs ONE warning per field with the lead id (``unknown`` when the
caller cannot see the row, as the column types cannot) and the field name. Never
the value.
"""

import json
import logging
import re
from typing import Any

from cryptography.fernet import InvalidToken

from src.utils.crypto import _ENC_PREFIX, decrypt_field

_log = logging.getLogger(__name__)

# A Fernet token: version byte 0x80 + 8-byte timestamp, base64url, so it always
# starts "gAAAAA" and is far longer than 46 characters.
_FERNET_SHAPE = re.compile(r"^gAAAAA[A-Za-z0-9_=-]{40,}$")

# The same cap ResultRow applies and the workers write ("up to 3").
MAX_CONTACTS = 3


def _is_residue(value: str) -> bool:
    return value.startswith(_ENC_PREFIX) or bool(_FERNET_SHAPE.match(value))


def _warn(field: str, lead_id: str | None) -> None:
    _log.warning(
        "contact decode dropped an unreadable value: lead=%s field=%s",
        lead_id or "unknown",
        field,
    )


# ─── cleaning already-decrypted values ────────────────────────────────────────


def clean_scalar(value: Any, *, field: str, lead_id: str | None = None) -> tuple[str | None, bool]:
    """A decrypted phone or email: trimmed, blank -> None, residue -> None + failed."""
    if value is None:
        return None, False
    if not isinstance(value, str):
        _warn(field, lead_id)
        return None, True
    text = value.strip()
    if not text:
        return None, False
    if _is_residue(text):
        _warn(field, lead_id)
        return None, True
    return text, False


def _clean_label(value: Any) -> tuple[str | None, bool]:
    """The phone type rule without the log line (callers warn once per field)."""
    if not isinstance(value, str):
        return None, False
    text = value.strip()
    if not text:
        return None, False
    if _is_residue(text):
        return None, True
    return text, False


def clean_phone_type(
    value: Any, *, field: str = "phone_type", lead_id: str | None = None
) -> tuple[str | None, bool]:
    """The plain ``phone_type`` column (Mobile | Landline | VoIP, as the provider
    gave it): trimmed; blank is absent (no failure); residue or a non-string value is
    dropped as a failure. ``phones[].type`` uses the quieter ``_clean_label`` through
    ``clean_phones``, where a non-string type is simply absent.
    """
    if value is not None and not isinstance(value, str):
        _warn(field, lead_id)
        return None, True
    label, failed = _clean_label(value)
    if failed:
        _warn(field, lead_id)
    return label, failed


def clean_phones(value: Any, *, lead_id: str | None = None) -> tuple[list[dict] | None, bool]:
    """A parsed ``phones`` value -> at most 3 ``{"number", "type"}`` entries.

    Not a list -> None, failed. An entry is kept only when it is a dict with a
    non-blank string ``number`` that is not residue; a residue ``type`` becomes None
    (the number is kept). A genuinely empty list stays ``[]`` (traced, none found);
    a non-empty list that cleans down to nothing is None, failed (unknown).
    """
    if value is None:
        return None, False
    if not isinstance(value, list):
        _warn("phones", lead_id)
        return None, True
    out: list[dict] = []
    number_failed = type_failed = False
    for item in value:
        if not isinstance(item, dict):
            continue
        # The label is judged even when the entry is then dropped, so residue in it
        # is still reported.
        label, bad_label = _clean_label(item.get("type"))
        type_failed = type_failed or bad_label
        number = item.get("number")
        if not isinstance(number, str) or not number.strip():
            continue
        number = number.strip()
        if _is_residue(number):
            number_failed = True
            continue
        out.append({"number": number, "type": label})
    emptied = bool(value) and not out
    if number_failed or emptied:
        _warn("phones", lead_id)
    if type_failed:
        _warn("phones.type", lead_id)
    if emptied:
        return None, True
    return out[:MAX_CONTACTS], number_failed or type_failed


def clean_emails(value: Any, *, lead_id: str | None = None) -> tuple[list[str] | None, bool]:
    """A parsed ``emails`` value -> at most 3 non-blank, non-residue strings.

    Same list rules as ``clean_phones``.
    """
    if value is None:
        return None, False
    if not isinstance(value, list):
        _warn("emails", lead_id)
        return None, True
    out: list[str] = []
    failed = False
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        text = item.strip()
        if _is_residue(text):
            failed = True
            continue
        out.append(text)
    emptied = bool(value) and not out
    if failed or emptied:
        _warn("emails", lead_id)
    if emptied:
        return None, True
    return out[:MAX_CONTACTS], failed


_ARRAY_CLEANERS = {"phones": clean_phones, "emails": clean_emails}


def parse_array(text: str, *, kind: str, lead_id: str | None = None) -> tuple[list | None, bool]:
    """Decrypted array text -> the cleaned list. Blank or invalid JSON -> None, failed."""
    cleaner = _ARRAY_CLEANERS[kind]
    if not text.strip():
        _warn(kind, lead_id)
        return None, True
    try:
        parsed = json.loads(text)
    except ValueError:
        _warn(kind, lead_id)
        return None, True
    return cleaner(parsed, lead_id=lead_id)


# ─── decoding stored column text (raw SQL paths) ──────────────────────────────


def _decrypt(stored: str, *, field: str, lead_id: str | None) -> str | None:
    """decrypt_field, with ONLY InvalidToken (strict mode refusing a value) turned
    into a logged None. Key/config errors propagate."""
    try:
        return decrypt_field(stored)
    except InvalidToken:
        _warn(field, lead_id)
        return None


def decode_scalar(stored: Any, *, field: str, lead_id: str | None = None) -> tuple[str | None, bool]:
    """A stored ``phone`` / ``email`` column value -> plaintext or None."""
    if stored is None:
        return None, False
    if not isinstance(stored, str):
        _warn(field, lead_id)
        return None, True
    if not stored.strip():
        return None, False
    plain = _decrypt(stored, field=field, lead_id=lead_id)
    if plain is None:
        return None, True
    return clean_scalar(plain, field=field, lead_id=lead_id)


def decode_array(stored: Any, *, kind: str, lead_id: str | None = None) -> tuple[list | None, bool]:
    """A stored ``phones`` / ``emails`` column value -> the cleaned list or None."""
    if stored is None:
        return None, False
    if not isinstance(stored, str) or not stored.strip():
        _warn(kind, lead_id)
        return None, True
    plain = _decrypt(stored, field=kind, lead_id=lead_id)
    if plain is None:
        return None, True
    return parse_array(plain, kind=kind, lead_id=lead_id)
