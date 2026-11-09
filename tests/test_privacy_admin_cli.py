"""The operator CLI end to end (scripts/privacy_admin.py, Phase 6 P7).

Real database. The CLI's session, and the enforcer sweep it runs after an activation, are
bound to ONE outer transaction (SQLAlchemy's create_savepoint mode: the CLI's commits
release savepoints), rolled back at the end. Synthetic people only, under a surname no
other test uses, because the dry run and the sweep read every stored lead.
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import src.workers.privacy_enforcer as enforcer
from src.db.session import sync_engine
from src.privacy import activation
from src.privacy.authorize import RuleSnapshotError
from src.utils.crypto import blind_index
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring

_SPEC = importlib.util.spec_from_file_location(
    "privacy_admin", Path(__file__).resolve().parents[1] / "scripts" / "privacy_admin.py")
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)

_PARCEL = "1234567890"
_SURNAME, _GIVEN = "PRIVACYCLITEST", "ALEX"


@pytest.fixture
def c(monkeypatch):
    with sync_engine.connect() as conn:
        outer = conn.begin()

        @contextmanager
        def _session():
            s = Session(bind=conn, join_transaction_mode="create_savepoint")
            try:
                yield s
            finally:
                s.close()

        monkeypatch.setattr(enforcer, "system_sync_session", _session)
        conn.session_factory = _session
        try:
            yield conn
        finally:
            outer.rollback()


@pytest.fixture
def keys_turned(monkeypatch):
    monkeypatch.setattr(activation, "IMPLEMENTED_ENFORCEMENT_VERSION",
                        activation.REQUIRED_ENFORCEMENT_VERSION)
    monkeypatch.setattr(activation.settings, "PRIVACY_ENFORCEMENT_VERSION",
                        activation.REQUIRED_ENFORCEMENT_VERSION)


def _q(conn, sql: str, /, **params):
    return conn.execute(text(sql), params)


def _admin(c) -> str:
    email = f"op-{uuid.uuid4().hex[:8]}@example.test"
    _q(c, "INSERT INTO users (id, email, password_hash, email_hmac, is_admin, mfa_enabled) "
          "VALUES (gen_random_uuid(), :e, 'x', :h, true, true)", e=email, h=blind_index(email))
    return email


def _run(c, email: str, *argv: str) -> None:
    cli.main(["--operator-email", email, *argv], session_factory=c.session_factory)


def _lead(c, *, parcel: str | None) -> str:
    owner, cfg, job, res = (str(uuid.uuid4()) for _ in range(4))
    _q(c, "INSERT INTO users (id, email, password_hash, email_hmac) VALUES (:i, :e, 'x', :h)",
       i=owner, e=f"{owner}@example.test", h=uuid.uuid4().hex)
    _q(c, "INSERT INTO scraper_configs (id, user_id, name, county, state, record_type) "
          "VALUES (:c, :u, 'P7 CLI', 'king', 'WA', 'probate')", c=cfg, u=owner)
    _q(c, "INSERT INTO jobs (id, user_id, scraper_config_id, status) "
          "VALUES (:j, :u, :c, 'done')", j=job, u=owner, c=cfg)
    _q(c, "INSERT INTO results (id, job_id, user_id, party_name, parcel_id, enrichment_data, "
          "dedup_hash) VALUES (:i, :j, :u, :n, :p, CAST(:ed AS json), :d)",
       i=res, j=job, u=owner, n=f"{_SURNAME} {_GIVEN}", p=parcel, ed=json.dumps({}),
       d=uuid.uuid4().hex)
    return res


def _flag(c, res: str):
    return _q(c, "SELECT privacy_suppressed_at IS NOT NULL FROM results WHERE id = :i",
              i=res).scalar()


def _verified_request(c, email: str, kind: str) -> str:
    _run(c, email, "record", "--kind", kind, "--channel", "email", "--contact", "by email")
    rid = str(_q(c, "SELECT request_id FROM privacy_request_events e JOIN users u "
                    "ON u.id = e.actor_user_id WHERE u.email_hmac = :h AND seq = 1 "
                    "ORDER BY occurred_at DESC LIMIT 1", h=blind_index(email)).scalar())
    _run(c, email, "event", "--request", rid, "--type", "verified",
         "--verification-level", "email_link")
    return rid


def _add_rule(c, email: str, rid: str) -> str:
    _run(c, email, "add-rule", "--request", rid, "--parcel", _PARCEL, "--county", "king",
         "--state", "WA", "--surname", _SURNAME, "--given", _GIVEN)
    return str(_q(c, "SELECT rule_id FROM privacy_suppressions WHERE request_id = :r",
                  r=rid).scalar())


def _queue_work(c, res: str) -> tuple[str, str]:
    """A queued lookup and an unsent dialer send for the lead; returns their ids."""
    job, owner, cfg = _q(c, "SELECT r.job_id, r.user_id, j.scraper_config_id FROM results r "
                            "JOIN jobs j ON j.id = r.job_id WHERE r.id = :i", i=res).one()
    _q(c, "UPDATE results SET skip_trace_status = 'queued' WHERE id = :i", i=res)
    pending, dialer = str(uuid.uuid4()), str(uuid.uuid4())
    _q(c, "INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id, "
          "property_address, first_name, last_name, mail_address, status) VALUES (:i, :j, "
          ":r, :u, '1 TEST ST', 'ALEX', 'PRIVACYCLITEST', '2 MAIL ST', 'queued')",
       i=pending, j=job, r=res, u=owner)
    _q(c, "INSERT INTO dialer_deliveries (id, job_id, result_id, user_id, scraper_config_id, "
          "vendor_id, status) VALUES (:i, :j, :r, :u, :cfg, 'phoneburner', 'pending')",
       i=dialer, j=job, r=res, u=owner, cfg=cfg)
    return pending, dialer


def test_an_opt_out_from_intake_to_lift(c, keys_turned, capsys):
    email = _admin(c)
    exact, review = _lead(c, parcel=_PARCEL), _lead(c, parcel=None)
    pending, dialer = _queue_work(c, exact)
    rid = _verified_request(c, email, "opt_out")
    rule = _add_rule(c, email, rid)

    _run(c, email, "candidates", "--rule", rule)
    assert "would withhold 1 lead(s) and send 1 to review" in capsys.readouterr().out
    assert not _flag(c, exact) and not _flag(c, review)  # a dry run changes nothing

    _run(c, email, "activate", "--rule", rule)
    assert _flag(c, exact) and _flag(c, review)           # flagged at once, not at the tick
    # Its queued work is withdrawn by the activation itself (design section 6).
    assert _q(c, "SELECT status FROM pending_skip_trace_rows WHERE id = :i",
              i=pending).scalar() == "cancelled"
    assert _q(c, "SELECT skip_trace_status FROM results WHERE id = :i",
              i=exact).scalar() == "not_attempted"
    assert _q(c, "SELECT status, last_error FROM dialer_deliveries WHERE id = :i",
              i=dialer).one() == ("skipped", "privacy withheld")

    _run(c, email, "held", "--rule", rule)
    out = capsys.readouterr().out
    assert f"{exact}  withheld" in out and f"{review}  review" in out
    assert _SURNAME not in out                            # ids and outcomes only

    with pytest.raises(SystemExit):                       # an exact match is never released
        _run(c, email, "release", "--rule", rule, "--result", exact)
    _run(c, email, "release", "--rule", rule, "--result", review)
    assert _flag(c, exact) and not _flag(c, review)

    _run(c, email, "lift", "--rule", rule)
    assert not _flag(c, exact)
    states = dict(_q(c, "SELECT state, count(*) FROM privacy_suppressions WHERE rule_id = :r "
                        "GROUP BY state", r=rule).all())
    # One row per live key version, each lifted.
    assert states["lifted"] == states["active"] == states["pending"] >= 1


def test_a_deletion_queues_its_erasure_and_cannot_be_lifted(c, keys_turned):
    email = _admin(c)
    _lead(c, parcel=_PARCEL)
    rule = _add_rule(c, email, _verified_request(c, email, "delete"))
    _run(c, email, "activate", "--rule", rule)
    assert _q(c, "SELECT count(*) FROM privacy_erasures WHERE rule_id = :r",
              r=rule).scalar() == 1
    with pytest.raises(DBAPIError):
        _run(c, email, "lift", "--rule", rule)


def test_activation_is_refused_until_both_keys_are_turned(c):
    email = _admin(c)
    rule = _add_rule(c, email, _verified_request(c, email, "opt_out"))
    with pytest.raises(activation.ActivationNotAllowedError):
        _run(c, email, "activate", "--rule", rule)
    assert _q(c, "SELECT count(*) FROM privacy_suppressions WHERE rule_id = :r "
                 "AND state <> 'pending'", r=rule).scalar() == 0


def test_a_rule_the_loader_would_reject_never_commits(c, keys_turned):
    """The database cannot see the normalisation version: a stale one must not go live,
    or every send path would stop loading rules."""
    email = _admin(c)
    rid = _verified_request(c, email, "opt_out")
    op = str(_q(c, "SELECT id FROM users WHERE email_hmac = :h", h=blind_index(email)).scalar())
    rule = str(_q(c, "SELECT privacy_add_rule(CAST(:r AS uuid), 'property', :h, 'person', :h, "
                     "'v1', 'n1', CAST(:a AS uuid))", r=rid, h="c" * 64, a=op).scalar())
    with pytest.raises(RuleSnapshotError):
        _run(c, email, "activate", "--rule", rule)
    assert _q(c, "SELECT count(*) FROM privacy_suppressions WHERE rule_id = :r "
                 "AND state = 'active'", r=rule).scalar() == 0


def test_an_operator_without_mfa_is_refused(c):
    email = _admin(c)
    _q(c, "UPDATE users SET mfa_enabled = false WHERE email_hmac = :h", h=blind_index(email))
    with pytest.raises(DBAPIError):
        _run(c, email, "record", "--kind", "opt_out", "--channel", "email", "--contact", "x")


def test_inbox_lists_open_requests_and_show_decrypts_one_for_the_operator(c, capsys):
    email = _admin(c)
    rid = _verified_request(c, email, "opt_out")
    capsys.readouterr()
    _run(c, email, "inbox")
    out = capsys.readouterr().out
    assert rid in out and "last: verified" in out
    _run(c, email, "show", "--request", rid)
    assert "contact: by email" in capsys.readouterr().out
    _run(c, email, "event", "--request", rid, "--type", "decided", "--decision-code",
         "suppressed")
    capsys.readouterr()
    _run(c, email, "inbox")
    assert rid not in capsys.readouterr().out                 # decided: no longer open


def test_a_reader_command_also_needs_an_admin_with_mfa(c):
    email = _admin(c)
    _q(c, "UPDATE users SET mfa_enabled = false WHERE email_hmac = :h", h=blind_index(email))
    for argv in (("inbox",), ("show", "--request", str(uuid.uuid4()))):
        with pytest.raises(DBAPIError):
            _run(c, email, *argv)
