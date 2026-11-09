"""Migration 137: the web form's request, its outbox email and its one-time link.

Real database, one rolled-back transaction per test. Expected failures run in a SAVEPOINT
and assert the function's own SQLSTATE. The ciphertext is a shape-valid placeholder: the
database only checks the `fe1:` prefix (encryption is src/utils/crypto's guarantee).
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.db.session import sync_engine

_SUBMIT = ("SELECT privacy_web_submit(:k, :h, :c, :d, :agent, now() + interval '45 days', "
           "'ccpa-2026')")
_CONFIRM = "SELECT privacy_web_confirm(:t)"
_FUNCTIONS = (
    "privacy_web_submit(text, text, text, text, boolean, timestamptz, text)",
    "privacy_web_confirm(text)",
    "privacy_outbox_claim(integer)",
    "privacy_outbox_token(uuid, text, timestamptz)",
    "privacy_outbox_sent(uuid)",
    "privacy_outbox_failed(uuid, text)",
    "privacy_tokens_purge()",
    "privacy_requests_due(interval)",
)


@pytest.fixture
def conn():
    with sync_engine.connect() as c:
        tx = c.begin()
        # This test's own rows only: earlier runs may have left due outbox rows behind.
        c.execute(text("UPDATE privacy_email_outbox SET gave_up_at = now() "
                       "WHERE sent_at IS NULL AND gave_up_at IS NULL"))
        try:
            yield c
        finally:
            tx.rollback()


def _sqlstate(conn, sql: str, **params) -> str | None:
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql), params)
    except DBAPIError as exc:
        sp.rollback()
        return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    sp.rollback()
    return None


def _submit(conn, kind: str = "opt_out", *, agent: bool = False, email: str | None = None) -> str:
    h = hashlib.sha256((email or uuid.uuid4().hex).encode()).hexdigest()
    conn.execute(text(_SUBMIT), {"k": kind, "h": h, "c": "fe1:contact", "d": "fe1:details",
                                 "agent": agent})
    return h


def _request(conn, h: str, kind: str = "opt_out"):
    return conn.execute(text(
        "SELECT id, channel, deadline_rules FROM privacy_requests "
        "WHERE requester_email_hmac = :h AND kind = :k"), {"h": h, "k": kind}).one()


def _events(conn, rid) -> list[str]:
    return list(conn.execute(text("SELECT event_type FROM privacy_request_events "
                                  "WHERE request_id = :r ORDER BY seq"), {"r": rid}).scalars())


def _claim(conn, rid):
    return conn.execute(text("SELECT o.* FROM privacy_outbox_claim(100) o "
                             "JOIN privacy_email_outbox x ON x.id = o.outbox_id "
                             "WHERE x.request_id = :r"), {"r": rid}).one()


def _token(conn, outbox) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(text("SELECT privacy_outbox_token(:o, :h, now() + interval '7 days')"),
                 {"o": outbox, "h": hashlib.sha256(token.encode()).hexdigest()})
    return token


def _send(conn, rid) -> str:
    """The worker's side: claim, store the token's hash, (send), mark sent."""
    row = _claim(conn, rid)
    assert row.contact_encrypted == "fe1:contact"
    token = _token(conn, row.outbox_id)
    conn.execute(text("SELECT privacy_outbox_sent(:o)"), {"o": row.outbox_id})
    return token


def _confirm(conn, token: str) -> None:
    conn.execute(text(_CONFIRM), {"t": hashlib.sha256(token.encode()).hexdigest()})


# ── Submission ─────────────────────────────────────────────────────────────────

def test_a_submission_is_the_received_request_with_its_email_queued(conn):
    h = _submit(conn)
    rid, channel, rules = _request(conn, h)
    assert (channel, rules) == ("web", "ccpa-2026")
    assert _events(conn, rid) == ["received", "verification_sent"]
    assert conn.execute(text("SELECT contact_encrypted FROM privacy_request_payloads "
                             "WHERE request_id = :r"), {"r": rid}).scalar() == "fe1:contact"
    assert conn.execute(text("SELECT kind FROM privacy_email_outbox WHERE request_id = :r"),
                        {"r": rid}).scalar() == "verification"


def test_an_agents_submission_goes_to_review_and_gets_a_receipt_without_a_link(conn):
    h = _submit(conn, "delete", agent=True)
    rid = _request(conn, h, "delete").id
    assert _events(conn, rid) == ["received", "in_review"]
    row = _claim(conn, rid)
    assert row.email_kind == "receipt"
    assert _sqlstate(conn, "SELECT privacy_outbox_token(:o, :h, now() + interval '1 day')",
                     o=row.outbox_id, h="a" * 64) == "BLD58"   # a receipt carries no token
    conn.execute(text("SELECT privacy_outbox_sent(:o)"), {"o": row.outbox_id})


def test_a_repeat_within_a_day_records_nothing_but_another_kind_does(conn):
    email = f"{uuid.uuid4().hex}@example.test"
    h = _submit(conn, email=email)
    _submit(conn, email=email)
    _submit(conn, "delete", email=email)
    counts = dict(conn.execute(text("SELECT kind, count(*) FROM privacy_requests "
                                    "WHERE requester_email_hmac = :h GROUP BY kind"),
                               {"h": h}).all())
    assert counts == {"opt_out": 1, "delete": 1}


@pytest.mark.parametrize("over", [
    {"k": "erase"}, {"h": "A" * 64}, {"h": "a" * 63}, {"c": "plain contact"},
    {"d": "plain details"}, {"c": "fe1:" + "x" * 5000}, {"agent": None},
])
def test_bad_input_is_refused(conn, over):
    params = {"k": "opt_out", "h": "a" * 64, "c": "fe1:c", "d": None, "agent": False, **over}
    assert _sqlstate(conn, _SUBMIT, **params) == "BLD57"


@pytest.mark.parametrize("deadline", ["now() - interval '1 day'", "now() + interval '200 days'"])
def test_an_implausible_deadline_is_refused(conn, deadline):
    assert _sqlstate(conn, (f"SELECT privacy_web_submit('opt_out', :h, 'fe1:c', NULL, false, "
                            f"{deadline}, 'ccpa-2026')"), h="b" * 64) == "BLD57"


# ── The one-time link ──────────────────────────────────────────────────────────

def test_confirming_an_opt_out_verifies_it_once(conn):
    rid = _request(conn, _submit(conn)).id
    token = _send(conn, rid)
    _confirm(conn, token)
    assert _events(conn, rid) == ["received", "verification_sent", "verified"]
    assert conn.execute(text("SELECT verification_level FROM privacy_request_events "
                             "WHERE request_id = :r AND event_type = 'verified'"),
                        {"r": rid}).scalar() == "email_link"
    _confirm(conn, token)                                       # replay: nothing new
    assert len(_events(conn, rid)) == 3


def test_confirming_a_deletion_sends_it_to_the_operator_not_to_verified(conn):
    rid = _request(conn, _submit(conn, "delete"), "delete").id
    _confirm(conn, _send(conn, rid))
    assert _events(conn, rid) == ["received", "verification_sent", "in_review"]


def test_expired_unknown_and_garbage_tokens_do_nothing(conn):
    rid = _request(conn, _submit(conn)).id
    token = _send(conn, rid)
    conn.execute(text("UPDATE privacy_request_tokens SET created_at = now() - interval '9 days', "
                      "expires_at = now() - interval '1 second' WHERE request_id = :r"), {"r": rid})
    _confirm(conn, token)
    _confirm(conn, secrets.token_urlsafe(32))
    for garbage in ("", "x" * 5000, "' OR 1=1 --"):
        conn.execute(text(_CONFIRM), {"t": garbage})
    assert _events(conn, rid) == ["received", "verification_sent"]


def test_a_verification_email_cannot_be_marked_sent_without_its_token(conn):
    rid = _request(conn, _submit(conn)).id
    outbox = _claim(conn, rid).outbox_id
    assert _sqlstate(conn, "SELECT privacy_outbox_sent(:o)", o=outbox) == "BLD58"


def test_an_unclaimed_row_can_be_neither_tokened_nor_marked_sent(conn):
    rid = _request(conn, _submit(conn)).id
    outbox = conn.execute(text("SELECT id FROM privacy_email_outbox WHERE request_id = :r"),
                          {"r": rid}).scalar()
    assert _sqlstate(conn, "SELECT privacy_outbox_token(:o, :h, now() + interval '1 day')",
                     o=outbox, h="a" * 64) == "BLD58"
    assert _sqlstate(conn, "SELECT privacy_outbox_sent(:o)", o=outbox) == "BLD58"


def test_a_crash_after_sending_resends_and_both_links_stay_valid(conn):
    rid = _request(conn, _submit(conn)).id
    first = _claim(conn, rid).outbox_id
    first_token = _token(conn, first)                          # emailed, then the worker dies
    assert conn.execute(text("SELECT count(*) FROM privacy_outbox_claim(100) o "
                             "WHERE o.outbox_id = :o"), {"o": first}).scalar() == 0  # leased
    conn.execute(text("UPDATE privacy_email_outbox SET claimed_until = now() - interval '1 s' "
                      "WHERE id = :o"), {"o": first})          # the lease lapses
    again = _claim(conn, rid).outbox_id
    assert again == first
    _token(conn, again)
    conn.execute(text("SELECT privacy_outbox_sent(:o)"), {"o": again})
    _confirm(conn, first_token)                                 # the first email's link works
    assert _events(conn, rid)[-1] == "verified"


# ── Outbox retries and token purge ─────────────────────────────────────────────

def test_a_failed_send_backs_off_and_gives_up_after_ten_attempts(conn):
    rid = _request(conn, _submit(conn)).id
    outbox = _claim(conn, rid).outbox_id
    conn.execute(text("SELECT privacy_outbox_failed(:o, 'ResendError')"), {"o": outbox})
    row = conn.execute(text("SELECT attempts, next_attempt_at > now() AS later, gave_up_at, "
                            "claimed_until, last_error FROM privacy_email_outbox WHERE id = :o"),
                       {"o": outbox}).one()
    assert (row.attempts, row.later, row.gave_up_at, row.claimed_until, row.last_error) == (
        1, True, None, None, "ResendError")
    assert conn.execute(text("SELECT count(*) FROM privacy_outbox_claim(100) "
                             "WHERE outbox_id = :o"), {"o": outbox}).scalar() == 0
    for _ in range(9):
        conn.execute(text("SELECT privacy_outbox_failed(:o, 'ResendError')"), {"o": outbox})
    assert conn.execute(text("SELECT gave_up_at IS NOT NULL FROM privacy_email_outbox "
                             "WHERE id = :o"), {"o": outbox}).scalar() is True


def test_a_provider_message_is_never_stored_only_an_error_class(conn):
    rid = _request(conn, _submit(conn)).id
    outbox = _claim(conn, rid).outbox_id
    conn.execute(text("SELECT privacy_outbox_failed(:o, :e)"),
                 {"o": outbox, "e": "550 mailbox jane@example.test unavailable"})
    assert conn.execute(text("SELECT last_error FROM privacy_email_outbox WHERE id = :o"),
                        {"o": outbox}).scalar() == "error"


def test_a_token_has_a_checked_purpose(conn):
    rid = _request(conn, _submit(conn)).id
    assert _sqlstate(conn, "INSERT INTO privacy_request_tokens (request_id, purpose, "
                           "token_hash, expires_at) VALUES (:r, 'login', :h, "
                           "now() + interval '1 day')", r=rid, h="d" * 64) == "23514"


def test_only_long_expired_tokens_are_purged(conn):
    rid = _request(conn, _submit(conn)).id
    _send(conn, rid)
    assert conn.execute(text("SELECT privacy_tokens_purge()")).scalar() >= 0
    assert conn.execute(text("SELECT count(*) FROM privacy_request_tokens WHERE request_id = :r"),
                        {"r": rid}).scalar() == 1
    conn.execute(text("UPDATE privacy_request_tokens SET created_at = now() - interval '60 days', "
                      "expires_at = now() - interval '31 days' WHERE request_id = :r"), {"r": rid})
    conn.execute(text("SELECT privacy_tokens_purge()"))
    assert conn.execute(text("SELECT count(*) FROM privacy_request_tokens WHERE request_id = :r"),
                        {"r": rid}).scalar() == 0


# ── Roles ──────────────────────────────────────────────────────────────────────

def test_a_role_with_no_grant_reaches_none_of_it(conn):
    probe = f"p8_probe_{uuid.uuid4().hex[:8]}"
    conn.execute(text(f"CREATE ROLE {probe} NOLOGIN"))  # rolled back with the test
    for fn in _FUNCTIONS:
        assert conn.execute(text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                            {"r": probe, "f": f"public.{fn}"}).scalar() is False, fn
    for table in ("privacy_request_tokens", "privacy_email_outbox"):
        assert conn.execute(text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                            {"r": probe, "t": f"public.{table}"}).scalar() is False


@pytest.mark.parametrize("role, allowed", [
    ("bridgeleads_app", {"privacy_web_submit", "privacy_web_confirm"}),
    ("bridgeleads_system", {"privacy_outbox_claim", "privacy_outbox_token",
                            "privacy_outbox_sent", "privacy_outbox_failed",
                            "privacy_tokens_purge", "privacy_requests_due"}),
    ("anon", set()), ("authenticated", set()), ("service_role", set()),
])
def test_each_runtime_role_reaches_only_its_own_functions(conn, role, allowed):
    if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first() is None:
        pytest.skip(f"role {role} absent")
    reach = {fn.split("(")[0] for fn in _FUNCTIONS if conn.execute(
        text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
        {"r": role, "f": f"public.{fn}"}).scalar()}
    assert reach == allowed


def test_the_grant_script_restores_every_grant() -> None:
    body = (Path(__file__).resolve().parent.parent
            / "scripts/provision_rls_roles.sql").read_text(encoding="utf-8")
    for fn, role in (("privacy_web_confirm(text)", "bridgeleads_app"),
                     ("privacy_outbox_claim(integer)", "bridgeleads_system"),
                     ("privacy_requests_due(interval)", "bridgeleads_system"),
                     ("privacy_tokens_purge()", "bridgeleads_system")):
        assert f"GRANT EXECUTE ON FUNCTION public.{fn} TO {role};" in body
    assert "REVOKE ALL ON privacy_request_tokens, privacy_email_outbox" in body
