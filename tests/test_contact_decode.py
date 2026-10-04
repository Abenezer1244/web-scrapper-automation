"""UX 3.8s1: a lead's contact never leaves as ciphertext.

``decrypt_field`` returns an undecryptable value AS-IS in tolerant mode and raises in
strict mode (``src/utils/crypto.py``). For contact PII that meant a corrupt or
wrong-key token reached the Results page, the run CSV, scheduled exports and the
dialer as raw ``fe1:...`` text, or (strict) failed a whole page for one bad row.
``src/utils/contact_decode.py`` is the one decoder that turns such a value into
nothing, logged by lead and field, never by value; ``Result``'s contact columns run
it on every ORM read (``src/db/encrypted_types.py``).

Every case runs in BOTH ``PII_ENCRYPTION_STRICT`` modes with the mode's own
expectation. Real DB (conftest, guarded test database). Malformed values are seeded
with raw SQL (the ORM would encrypt them), bound to the row's id AND its user, with
the update count asserted. The only stubs are the two external providers the
existing suites already stub: Tracerfy's CSV/queue fetch and its balance read.
"""
import csv
import io
import json
import logging
import re
import sys
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import InvalidToken
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

import src.utils.crypto as crypto
from src.config import settings
from src.db.encrypted_types import ContactLabel, EncryptedContactJSON, EncryptedContactString
from src.db.models import PendingSkipTraceRow, Result, SkipTraceCache
from src.db.session import system_sync_session
from src.utils import contact_decode as cd
from src.utils.crypto import encrypt_field, is_encrypted
from tests.test_skip_trace_already_delivered import (
    _address,
    _dispatcher_on,  # noqa: F401  (fixture)
    _enqueue,
    _lead,
    _queue,
    _run,
    _skip_trace_on,  # noqa: F401  (fixture)
)
from tests.test_tracerfy_ingest import (
    DOWNLOAD_URL,
    _csv,
    _next_queue_id,
    _seed,
    _stub_csv,  # noqa: F401  (fixture)
)

# A bare Fernet-shaped token no key opens, and the same behind the fe1: sentinel.
FERNET_SHAPED = "gAAAAAB" + "Q" * 80
CORRUPT_FE1 = "fe1:" + FERNET_SHAPED
RESIDUE = re.compile(r"fe1:|gAAAAA[A-Za-z0-9_=-]{40,}")
CONTACT_COLUMNS = ("phone", "email", "phones", "emails", "phone_type", "phone_dnc_flag")


@pytest.fixture(params=["tolerant", "strict"])
def mode(request, monkeypatch):
    """Both read modes. The key and the blind index are built first, in tolerant
    mode, because strict refuses the test environment's SECRET_KEY-derived key."""
    crypto._instance()
    crypto._blind_index_secret()
    monkeypatch.setattr(settings, "PII_ENCRYPTION_STRICT", request.param == "strict")
    return request.param


def _enc_json(value) -> str:
    return encrypt_field(json.dumps(value))


def _phone(number, label="Mobile") -> dict:
    return {"number": number, "type": label}


def _no_residue(body: str) -> None:
    found = RESIDUE.search(body)
    assert found is None, f"ciphertext residue reached the output: {body[max(0, found.start() - 40):found.end() + 10]!r}"


def _raw_set(result_id: str, user_id: str, **cols) -> None:
    """Write stored column text straight into a Result, bypassing the column types."""
    assignments = ", ".join(f"{c} = :{c}" for c in cols)
    with system_sync_session() as db:
        n = db.execute(
            text(f"UPDATE results SET {assignments} WHERE id = :id AND user_id = :uid"),  # noqa: S608 (fixed test columns)
            {**cols, "id": result_id, "uid": user_id},
        ).rowcount
        db.commit()
    assert n == 1


def _stored(result_id: str, user_id: str) -> dict:
    """The six contact-related columns exactly as stored (raw, no types)."""
    with system_sync_session() as db:
        row = db.execute(
            text(f"SELECT {', '.join(CONTACT_COLUMNS)}, skip_trace_status, skip_trace_source "  # noqa: S608
                 "FROM results WHERE id = :id AND user_id = :uid"),
            {"id": result_id, "uid": user_id},
        ).mappings().one()
    return dict(row)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─── 1. The decoder, pure ─────────────────────────────────────────────────────


def test_scalar_decision_table(mode):
    tolerant = mode == "tolerant"
    bare_ok = crypto._instance().encrypt(b"2065550100").decode("ascii")
    cases = [
        (None, (None, False)),
        ("", (None, False)),
        (" \t", (None, False)),
        (encrypt_field("2065550100"), ("2065550100", False)),
        (encrypt_field("  owner@example.com "), ("owner@example.com", False)),
        (encrypt_field("   "), (None, False)),
        (bare_ok, ("2065550100", False)),
        (CORRUPT_FE1, (None, True)),
        (FERNET_SHAPED, (None, True)),
        ("2065550100", ("2065550100", False) if tolerant else (None, True)),
        # Valid ciphertext whose plaintext is itself residue, and padded residue.
        (encrypt_field(CORRUPT_FE1), (None, True)),
        (encrypt_field("  " + FERNET_SHAPED + " "), (None, True)),
    ]
    for stored, want in cases:
        assert cd.decode_scalar(stored, field="phone", lead_id="lead-1") == want, stored


def test_phones_decision_table(mode):
    tolerant = mode == "tolerant"
    good = _phone("2065550100")
    cases = [
        (None, (None, False)),
        ("", (None, True)),
        ("  ", (None, True)),
        (_enc_json([]), ([], False)),
        (_enc_json([good]), ([good], False)),
        (_enc_json([_phone(" 2065550100 ", " Mobile ")]), ([good], False)),
        (_enc_json({"number": "2065550100"}), (None, True)),
        (_enc_json("2065550100"), (None, True)),
        (encrypt_field("not json"), (None, True)),
        (CORRUPT_FE1, (None, True)),
        (FERNET_SHAPED, (None, True)),
        (_enc_json([_phone(f"20655501{i:02d}") for i in range(5)]),
         ([_phone(f"20655501{i:02d}") for i in range(3)], False)),
        # An entry without a number is dropped; that alone is not a failure.
        (_enc_json([{"type": "Mobile"}, "2065550100", good]), ([good], False)),
        (_enc_json([_phone(CORRUPT_FE1), good]), ([good], True)),
        (_enc_json([_phone("  " + FERNET_SHAPED)]), (None, True)),
        (_enc_json([_phone("2065550100", "fe1:zz")]), ([_phone("2065550100", None)], True)),
        (_enc_json([_phone("2065550100", " " + FERNET_SHAPED)]), ([_phone("2065550100", None)], True)),
        (_enc_json([_phone("2065550100", 7)]), ([_phone("2065550100", None)], False)),
        # Residue in the label of an entry that is dropped anyway is still reported.
        (_enc_json([{"type": "fe1:zz"}, good]), ([good], True)),
        (_enc_json([_phone("2065550100", "  ")]), ([_phone("2065550100", None)], False)),
        (json.dumps([good]), ([good], False) if tolerant else (None, True)),
    ]
    for stored, want in cases:
        assert cd.decode_array(stored, kind="phones", lead_id="lead-1") == want, stored


def test_emails_decision_table(mode):
    tolerant = mode == "tolerant"
    cases = [
        (None, (None, False)),
        ("", (None, True)),
        (_enc_json([]), ([], False)),
        (_enc_json([" a@example.com ", 3, "", "b@example.com"]), (["a@example.com", "b@example.com"], False)),
        (_enc_json([f"{i}@example.com" for i in range(5)]), ([f"{i}@example.com" for i in range(3)], False)),
        (_enc_json([CORRUPT_FE1, "a@example.com"]), (["a@example.com"], True)),
        (_enc_json([FERNET_SHAPED]), (None, True)),
        (_enc_json({"a": 1}), (None, True)),
        (CORRUPT_FE1, (None, True)),
        (json.dumps(["a@example.com"]), (["a@example.com"], False) if tolerant else (None, True)),
    ]
    for stored, want in cases:
        assert cd.decode_array(stored, kind="emails", lead_id="lead-1") == want, stored


def test_phone_type_rule():
    assert cd.clean_phone_type(None) == (None, False)
    assert cd.clean_phone_type(7) == (None, True)  # the plain column: never a non-string
    assert cd.clean_phone_type("  ") == (None, False)
    assert cd.clean_phone_type(" Mobile ") == ("Mobile", False)
    assert cd.clean_phone_type("fe1:abc") == (None, True)
    assert cd.clean_phone_type(" " + FERNET_SHAPED) == (None, True)


def test_each_failure_logs_the_lead_and_field_never_the_value(mode, caplog):
    caplog.set_level(logging.WARNING, logger="src.utils.contact_decode")
    stored = _enc_json([_phone("2065550100", "fe1:zz"), _phone(CORRUPT_FE1), _phone(FERNET_SHAPED)])

    assert cd.decode_array(stored, kind="phones", lead_id="lead-9") == (
        [_phone("2065550100", None)], True)

    lines = [r.getMessage() for r in caplog.records if r.name == "src.utils.contact_decode"]
    assert lines == [
        "contact decode dropped an unreadable value: lead=lead-9 field=phones",
        "contact decode dropped an unreadable value: lead=lead-9 field=phones.type",
    ]
    assert all(not RESIDUE.search(line) and "2065550100" not in line for line in lines)


def test_a_key_error_is_never_swallowed(monkeypatch):
    """Only InvalidToken (strict refusing one value) is caught. A broken key fails the
    read, as decrypt_field always has."""
    monkeypatch.setattr(settings, "PII_ENCRYPTION_STRICT", False)
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(settings, "FIELD_ENCRYPTION_KEY", ",")
    with pytest.raises(ValueError):
        cd.decode_scalar(CORRUPT_FE1, field="phone")
    with pytest.raises(ValueError):
        cd.decode_array(CORRUPT_FE1, kind="phones")


def test_strict_refusal_is_the_only_exception_turned_into_none(mode):
    if mode == "tolerant":
        assert crypto.decrypt_field(CORRUPT_FE1) == CORRUPT_FE1
    else:
        with pytest.raises(InvalidToken):
            crypto.decrypt_field(CORRUPT_FE1)
    assert cd.decode_scalar(CORRUPT_FE1, field="email") == (None, True)


# ─── 2. The column types ──────────────────────────────────────────────────────


def test_contact_types_store_exactly_what_the_parent_types_store(mode):
    stored = EncryptedContactString("phone").process_bind_param("2065550100", None)
    assert stored.startswith("fe1:") and crypto.decrypt_field(stored) == "2065550100"
    assert EncryptedContactString("phone").process_bind_param("  ", None) is None
    stored_json = EncryptedContactJSON("phones").process_bind_param([_phone("2065550100")], None)
    assert json.loads(crypto.decrypt_field(stored_json)) == [_phone("2065550100")]
    # ContactLabel has no bind step at all: the value is bound exactly as given.
    assert ContactLabel().bind_processor(postgresql.dialect()) is None
    assert ContactLabel().impl.length == 16  # the column's existing storage


def test_contact_types_read_through_the_decoder(mode):
    phone, phones = EncryptedContactString("phone"), EncryptedContactJSON("phones")
    assert phone.process_result_value(encrypt_field(" 2065550100 "), None) == "2065550100"
    assert phone.process_result_value(CORRUPT_FE1, None) is None
    assert phone.process_result_value("  ", None) is None  # blank scalar: no value
    assert phones.process_result_value("", None) is None  # blank array: unreadable
    assert phones.process_result_value(CORRUPT_FE1, None) is None
    assert phones.process_result_value(_enc_json([_phone("2065550100")]), None) == [_phone("2065550100")]
    assert ContactLabel().process_result_value("fe1:zz", None) is None
    assert ContactLabel().process_result_value(" Mobile ", None) == "Mobile"


# ─── 3. The Results page and the run CSV (real endpoint) ──────────────────────


async def test_results_page_and_run_csv_never_carry_residue(
    mode, client, business_user, business_token, starter_token,
):
    tolerant = mode == "tolerant"
    uid = business_user.id
    job = _run(uid, skip_on=False, status="done")
    valid = _lead(uid, job, 1, status="hit", traced_days_ago=1,
                  phone="2065550100", email="owner@example.com")
    corrupt = _lead(uid, job, 2, status="hit", traced_days_ago=1,
                    phone="2065550111", email="gone@example.com")
    _raw_set(corrupt, uid, phone=CORRUPT_FE1, email=FERNET_SHAPED, phones=CORRUPT_FE1,
             emails=encrypt_field(CORRUPT_FE1), phone_type="fe1:zz")
    partial = _lead(uid, job, 3, status="hit", traced_days_ago=1,
                    phone="2065550133", email="b@example.com")
    _raw_set(partial, uid,
             phones=_enc_json([_phone(CORRUPT_FE1), _phone("2065550133", "fe1:zz")]),
             emails=_enc_json([" " + FERNET_SHAPED, "b@example.com"]),
             phone_type=" Mobile ")
    legacy = _lead(uid, job, 4)
    _raw_set(legacy, uid, phone="2065550144")  # stored before encryption

    resp = await client.get(f"/jobs/{job}/results", headers=_auth(business_token))

    assert resp.status_code == 200, resp.text
    _no_residue(resp.text)
    rows = {r["id"]: r for r in resp.json()["items"]}
    assert rows[valid]["phone"] == "2065550100"
    assert rows[valid]["phones"] == [_phone("2065550100")]
    assert rows[valid]["emails"] == ["owner@example.com"]
    # Every value dropped: absent, and the status is never rewritten (accepted, 5).
    assert (rows[corrupt]["phone"], rows[corrupt]["email"], rows[corrupt]["phones"],
            rows[corrupt]["emails"], rows[corrupt]["phone_type"]) == (None,) * 5
    assert rows[corrupt]["skip_trace_status"] == "hit"
    # Some values dropped: the readable ones survive.
    assert rows[partial]["phones"] == [_phone("2065550133", None)]
    assert rows[partial]["emails"] == ["b@example.com"]
    assert rows[partial]["phone_type"] == "Mobile"
    assert rows[legacy]["phone"] == ("2065550144" if tolerant else None)

    # The other account cannot read this run at all.
    other = await client.get(f"/jobs/{job}/results", headers=_auth(starter_token))
    assert other.status_code == 404

    # The run CSV is built live from the same rows.
    url = await client.get(f"/jobs/{job}/export-url", headers=_auth(business_token))
    assert url.status_code == 200, url.text
    download = await client.get(url.json()["url"])
    assert download.status_code == 200, download.text
    _no_residue(download.text)
    assert "2065550100" in download.text and "2065550133" in download.text
    assert ("2065550144" in download.text) is tolerant


async def test_reading_a_residue_label_logs_the_field(mode, business_user, caplog):
    caplog.set_level(logging.WARNING, logger="src.utils.contact_decode")
    uid = business_user.id
    rid = _lead(uid, _run(uid, skip_on=False, status="done"), 1)
    _raw_set(rid, uid, phone_type="fe1:zz")
    with system_sync_session() as db:
        assert db.get(Result, rid).phone_type is None
    lines = [r.getMessage() for r in caplog.records if r.name == "src.utils.contact_decode"]
    assert lines == ["contact decode dropped an unreadable value: lead=unknown field=phone_type"]


# ─── 4. Writers never turn a dropped value into a stored one ──────────────────


async def test_saving_a_lead_never_rewrites_its_unreadable_contacts(mode, business_user):
    uid = business_user.id
    rid = _lead(uid, _run(uid, skip_on=False, status="done"), 1)
    _raw_set(rid, uid, phone=CORRUPT_FE1, email=FERNET_SHAPED, phones=CORRUPT_FE1,
             emails=encrypt_field(CORRUPT_FE1), phone_type="fe1:zz", phone_dnc_flag=True)
    before = _stored(rid, uid)

    with system_sync_session() as db:
        row = db.get(Result, rid)
        assert (row.phone, row.email, row.phones, row.emails, row.phone_type) == (None,) * 5
        row.party_name = "RENAMED OWNER"
        db.commit()

    assert _stored(rid, uid) == before


# Bad SkipTraceCache entries. The cache keeps the PLAIN types on purpose, so every
# outcome below is what happened before this change; the new part is that nothing
# the copy carries can be read back as residue.
#   valid_residue: well-formed ciphertext of residue text (an earlier tolerant copy)
#   bad_scalars:   undecryptable phone/email (arrays readable)
#   bad_arrays:    undecryptable phones/emails (scalars readable)
CACHE_CASES = {
    "valid_residue": {
        "phone": encrypt_field(CORRUPT_FE1), "email": encrypt_field(FERNET_SHAPED),
        "phones": _enc_json([_phone(CORRUPT_FE1)]), "emails": _enc_json([FERNET_SHAPED]),
    },
    "bad_scalars": {"phone": CORRUPT_FE1, "email": FERNET_SHAPED},
    "bad_arrays": {"phones": CORRUPT_FE1, "emails": FERNET_SHAPED},
}


def _copies(case: str, mode: str) -> bool:
    """Whether the cache read succeeds, so the copy happens. Tolerant passes an
    unreadable SCALAR through as text; an unreadable ARRAY hits json.loads in both
    modes; strict refuses any unreadable value."""
    if case == "valid_residue":
        return True
    if case == "bad_scalars":
        return mode == "tolerant"
    return False


def _cache_with(user_id: str, n: int, case: str) -> str:
    """A fresh cache entry for lead n's subject, then its columns overwritten raw."""
    from src.scrapers.enrichment.skip_trace import lookup_subject_key

    key = lookup_subject_key(user_id, _address(n), "VANCOUVER", "WA", "normal", "AVELINO", "SAARENAS")
    with system_sync_session() as db:
        db.add(SkipTraceCache(
            address_hash=key, phone="2065550155", phone_type="Mobile", email="cache@example.com",
            phones=[_phone("2065550155")], emails=["cache@example.com"],
            fetched_at=datetime.now(UTC) - timedelta(days=2),
        ))
        db.commit()
        cols = CACHE_CASES[case]
        assignments = ", ".join(f"{c} = :{c}" for c in cols)
        assert db.execute(
            text(f"UPDATE skip_trace_cache SET {assignments} WHERE address_hash = :k"),  # noqa: S608
            {**cols, "k": key},
        ).rowcount == 1
        db.commit()
    return key


def _cache_stored(key: str) -> dict:
    with system_sync_session() as db:
        return dict(db.execute(
            text("SELECT phone, email, phones, emails, phone_type FROM skip_trace_cache "
                 "WHERE address_hash = :k"), {"k": key},
        ).mappings().one())


async def _assert_page_clean(client, job_id: str, token: str, category: str = "already_delivered"):
    resp = await client.get(f"/jobs/{job_id}/results", params={"category": category},
                            headers=_auth(token))
    assert resp.status_code == 200, resp.text
    _no_residue(resp.text)
    return resp.json()


@pytest.mark.parametrize("case", list(CACHE_CASES))
async def test_enrich_cache_copy(
    case, mode, client, business_user, business_token, redis_client, _skip_trace_on,  # noqa: F811
):
    uid = business_user.id
    _lead(uid, _run(uid, skip_on=False), 1)
    key = _cache_with(uid, 1, case)
    cache_before = _cache_stored(key)
    again = _run(uid, skip_on=True, status="done")
    dup = _lead(uid, again, 1, dup=True)
    before = _stored(dup, uid)

    if _copies(case, mode):
        _enqueue(again, redis_client)
        after = _stored(dup, uid)
        assert (after["skip_trace_status"], after["skip_trace_source"]) == ("hit", "reused")
    else:
        # Fail closed, as before: the copy raises on the cache read and writes nothing.
        with pytest.raises((InvalidToken, ValueError)):
            _enqueue(again, redis_client)
        assert _stored(dup, uid) == before
    assert _cache_stored(key) == cache_before
    await _assert_page_clean(client, again, business_token)


@pytest.mark.parametrize("case", list(CACHE_CASES))
async def test_dispatcher_settle_copy(
    case, mode, client, business_user, business_token, _dispatcher_on,  # noqa: F811
):
    from src.workers.skip_trace_dispatcher import _settle_queued_from_known_answers

    uid = business_user.id
    _lead(uid, _run(uid, skip_on=False, status="done"), 1)
    again = _run(uid, skip_on=True, status="done")
    dup = _lead(uid, again, 1, dup=True)
    held = _queue(uid, again, dup, 1)
    key = _cache_with(uid, 1, case)
    cache_before = _cache_stored(key)
    before = _stored(dup, uid)

    with system_sync_session() as db:
        settled = _settle_queued_from_known_answers(db)

    with system_sync_session() as db:
        pending = db.get(PendingSkipTraceRow, held).status
    if _copies(case, mode):
        assert settled == 1 and pending == "reused"
        assert _stored(dup, uid)["skip_trace_status"] == "hit"
    else:
        # The sweep is best-effort: it rolls back, and the rows stay queued.
        assert settled is None and pending == "queued"
        assert _stored(dup, uid) == before
    assert _cache_stored(key) == cache_before
    await _assert_page_clean(client, again, business_token)


@pytest.mark.parametrize("case", list(CACHE_CASES))
async def test_backfill_script_cache_copy(
    case, mode, client, business_user, business_token, monkeypatch, _skip_trace_on,  # noqa: F811
):
    import scripts.backfill_skip_trace_jobs as backfill

    monkeypatch.setattr(backfill, "_tracerfy_balance", lambda: None)  # Tracerfy, not us
    uid = business_user.id
    _lead(uid, _run(uid, skip_on=False, status="done"), 1)
    key = _cache_with(uid, 1, case)
    cache_before = _cache_stored(key)
    again = _run(uid, skip_on=True, status="done")
    dup = _lead(uid, again, 1, dup=True)
    before = _stored(dup, uid)
    monkeypatch.setattr(sys, "argv", ["backfill_skip_trace_jobs.py", "--jobs", again, "--commit"])

    if _copies(case, mode):
        assert backfill.main() == 0
        assert _stored(dup, uid)["skip_trace_status"] == "hit"
    else:
        with pytest.raises((InvalidToken, ValueError)):
            backfill.main()
        assert _stored(dup, uid) == before
    assert _cache_stored(key) == cache_before
    await _assert_page_clean(client, again, business_token)


async def test_duplicate_reuse_copies_ciphertext_untouched(
    mode, client, business_user, business_token, redis_client, _skip_trace_on,  # noqa: F811
):
    """Both raw-SQL reuse statements move the stored bytes column to column (no type
    runs), so residue arrives exactly as it was and is dropped when it is read."""
    uid = business_user.id
    residue = {"phone": CORRUPT_FE1, "email": encrypt_field(FERNET_SHAPED),
               "phones": _enc_json([_phone(CORRUPT_FE1, "fe1:zz")]), "emails": CORRUPT_FE1,
               "phone_type": "fe1:zz", "phone_dnc_flag": False}

    # Statement 1: the already-delivered first result is the source.
    first = _run(uid, skip_on=True)
    source1 = _lead(uid, first, 1, status="hit", traced_days_ago=3, phone="2065550100")
    _raw_set(source1, uid, **residue)
    # Statement 2: a later run's traced duplicate is the source.
    _lead(uid, _run(uid, skip_on=False), 2)
    later = _run(uid, skip_on=True, status="done")
    source2 = _lead(uid, later, 2, dup=True, status="hit", traced_days_ago=1, phone="2065550111")
    _raw_set(source2, uid, **residue)

    again = _run(uid, skip_on=True, status="done")
    target1 = _lead(uid, again, 1, dup=True)
    target2 = _lead(uid, again, 2, dup=True)

    _enqueue(again, redis_client)

    for source, target in ((source1, target1), (source2, target2)):
        got, want = _stored(target, uid), _stored(source, uid)
        assert got["skip_trace_source"] == "reused"
        assert {c: got[c] for c in CONTACT_COLUMNS} == {c: want[c] for c in CONTACT_COLUMNS}
    page = await _assert_page_clean(client, again, business_token)
    by_id = {r["id"]: r for r in page["items"]}
    assert all(by_id[t]["phone"] is None and by_id[t]["phones"] is None for t in (target1, target2))


# ─── 5. Outbound: the dialer, the Tracerfy ingest, scheduled exports ──────────


async def test_dialer_payload_from_a_residue_row(mode, business_user):
    """Both dialer paths build each lead from the ORM row's attributes
    (scheduler_helpers/dialer.py, dialer_outbox.py), so that read is the boundary."""
    from src.workers.webhook_delivery import build_dialer_push_payload

    uid = business_user.id
    job = _run(uid, skip_on=False, status="done")
    good = _lead(uid, job, 1, status="hit", traced_days_ago=1, phone="2065550100", email="a@example.com")
    bad = _lead(uid, job, 2, status="hit", traced_days_ago=1, phone="2065550111")
    _raw_set(bad, uid, phone=CORRUPT_FE1, email=FERNET_SHAPED, phone_type="fe1:zz")

    with system_sync_session() as db:
        rows = [db.get(Result, good), db.get(Result, bad)]
        leads = [{"id": r.id, "party_name": r.party_name, "phone": r.phone,
                  "phone_type": r.phone_type, "phone_dnc_flag": r.phone_dnc_flag,
                  "email": r.email, "property_address": r.property_address,
                  "mailing_address": r.mailing_address} for r in rows]
    payload = build_dialer_push_payload(job, "cfg", "name", "clark", "WA", "probate",
                                        leads, len(leads), None)

    body = json.dumps(payload)
    _no_residue(body)
    assert "2065550100" in body and "a@example.com" in body


async def test_tracerfy_ingest_stores_ciphertext_and_reads_back_clean(
    mode, client, starter_user, business_user, starter_token, business_token, _stub_csv,  # noqa: F811
):
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("123 MAIN ST", "TACOMA", "WA"), ("456 OAK ST", "TACOMA", "WA")])
    theirs = _seed(business_user.id, _next_queue_id(), [("123 MAIN ST", "TACOMA", "WA")])
    _stub_csv(_csv(
        "123 MAIN ST,TACOMA,WA,JANE,DOE,2065550100,Mobile,2065550100,,,jane@example.com,",
        # No Mobile-1, so the provider's own primary_phone_type is what gets stored.
        f"456 OAK ST,TACOMA,WA,JOHN,ROE,2065550111,fe1:zz,,,,{FERNET_SHAPED},",
    ))
    from src.workers.tracerfy_ingest import ingest_tracerfy_batch

    started = datetime.now(UTC) - timedelta(seconds=5)
    out = ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=2,
                                credits_deducted=2)

    assert out["hits"] == 2
    jane = seed["rows"][0]["result_id"]
    stored = _stored(jane, starter_user.id)
    assert stored["phone"].startswith("fe1:") and is_encrypted(stored["phone"])
    assert is_encrypted(stored["phones"]) and is_encrypted(stored["emails"])
    with system_sync_session() as db:
        cached = db.execute(  # only what this ingest wrote; other cases leave rows behind
            text("SELECT phone, phones FROM skip_trace_cache WHERE fetched_at >= :t"),
            {"t": started},
        ).all()
    assert cached and all(is_encrypted(p) and is_encrypted(ps) for p, ps in cached)

    page = await _assert_page_clean(client, seed["job_id"], starter_token, category="new")
    rows = {r["id"]: r for r in page["items"]}
    assert rows[jane]["phone"] == "2065550100"
    assert rows[jane]["phones"][0]["number"] == "2065550100"
    assert rows[jane]["emails"] == ["jane@example.com"]
    john = rows[seed["rows"][1]["result_id"]]
    assert john["phone"] == "2065550111" and john["phone_type"] is None
    assert john["phones"] == [_phone("2065550111", None)]
    assert _stored(seed["rows"][1]["result_id"], starter_user.id)["phone_type"] == "fe1:zz"

    # The other account's lead at the same address was neither written nor shown.
    other = _stored(theirs["rows"][0]["result_id"], business_user.id)
    assert other["phone"] is None and other["skip_trace_status"] == "submitted"
    theirs_page = await _assert_page_clean(client, theirs["job_id"], business_token, category="new")
    assert all(r["phone"] is None for r in theirs_page["items"])


async def test_scheduled_export_file_never_carries_residue(mode, business_user, tmp_path):
    from sqlalchemy import select

    from src.utils.data_exporter import DataExporter
    from src.workers.tasks import _result_rows_to_export_dicts

    uid = business_user.id
    job = _run(uid, skip_on=False, status="done")
    _lead(uid, job, 1, status="hit", traced_days_ago=1, phone="2065550100", email="a@example.com")
    bad = _lead(uid, job, 2, status="hit", traced_days_ago=1, phone="2065550111")
    _raw_set(bad, uid, phone=CORRUPT_FE1, email=FERNET_SHAPED, phones=CORRUPT_FE1,
             emails=_enc_json([CORRUPT_FE1]), phone_type="fe1:zz")
    partial = _lead(uid, job, 3, status="hit", traced_days_ago=1, phone="2065550133")
    _raw_set(partial, uid, phones=_enc_json([_phone(FERNET_SHAPED), _phone("2065550133", " Mobile ")]))

    # The worker's own projection over the same tenant-scoped read (tasks.py).
    with system_sync_session() as db:
        rows = db.execute(
            select(Result).where(Result.job_id == job, Result.user_id == uid).order_by(Result.id)
        ).scalars().all()
        records = _result_rows_to_export_dicts(rows)
    path = DataExporter(export_dir=str(tmp_path)).export(records, filename="scheduled", fmt="csv")

    body = path.read_text(encoding="utf-8")
    _no_residue(body)
    assert "2065550100" in body and "a@example.com" in body and "2065550133" in body
    lines = list(csv.reader(io.StringIO(body)))
    assert len(lines) == 4  # header + three leads, none dropped
