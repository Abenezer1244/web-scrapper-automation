"""Account data export worker (P4b): build, publish, email, retry, expire.

Real database: the task opens its own sessions, so every test commits its rows and
deletes its users afterwards (users CASCADE to their configs, jobs, results and export
rows). R2 and Resend are external services: the task takes a store stand-in and a send
callable (precedent: account_deletion_beat), which record what they were given.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import timedelta

import jwt
import pytest
from sqlalchemy import text

from src.api.auth import hash_password
from src.config import settings
from src.db.session import sync_engine
from src.utils.crypto import blind_index, encrypt_field
from src.utils.lead_export import CRM_V1_LABELS
from src.workers import account_export as worker

_SECRETS = ("whsec_config_0123456789abcdef", "dialsec_config_0123456789ab",
            "pb_token_0123456789abcdef", "whsec_batch_0123456789abcdef")


class FakeStore:
    def __init__(self, fail_upload: bool = False, on_upload=None):
        self.objects: dict[str, bytes] = {}
        self.fail_upload = fail_upload
        self.on_upload = on_upload

    def upload(self, local_path, key):
        if self.fail_upload:
            raise ConnectionError("r2 down")
        self.objects[key] = local_path.read_bytes()
        if self.on_upload:
            self.on_upload()

    def delete(self, key):
        self.objects.pop(key, None)
        return True


class Sent:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    def __call__(self, to, link, expires_at):
        if self.fail:
            raise ConnectionError("resend down")
        self.calls.append((to, link, expires_at))


@pytest.fixture
def made():
    users: list[str] = []
    yield users
    with sync_engine.begin() as c:
        for uid in users:
            c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})


@pytest.fixture
def mail_on(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(settings, "API_BASE_URL", "https://api.bl.test")


def _account(made, *, party: str = "=HYPERLINK(\"http://evil\")") -> dict:
    """An account with a CRM-layout config holding every deliver secret, a batch, a
    finished run (one deliverable lead, one duplicate), a running run, and one export
    request. Returns its ids."""
    a = {k: str(uuid.uuid4()) for k in ("user", "config", "batch", "job", "running", "export")}
    a["email"] = f"exp_{a['user'][:8]}@bl.test"
    deliver = json.dumps({"emails": ["me@x.test"], "csv_layout": "crm_v1",
                          "webhook_url": "https://hook.test", "webhook_secret": _SECRETS[0],
                          "dialer_webhook_secret": _SECRETS[1],
                          "phoneburner_access_token": _SECRETS[2]})
    with sync_engine.begin() as c:
        c.execute(text("INSERT INTO users (id, email, email_hmac, password_hash, first_name) "
                       "VALUES (:user, :e, :h, :pw, 'Ada')"),
                  a | {"e": encrypt_field(a["email"]), "h": blind_index(a["email"]),
                       "pw": hash_password("Pw-123456789")})
        made.append(a["user"])
        c.execute(text(
            "INSERT INTO scraper_batches (id, user_id, name, state, fields, enrichment, "
            "schedule, deliver) VALUES (:batch, :user, 'Spring list', 'WA', '{}', '{}', "
            "'{}', CAST(:d AS json))"), a | {"d": json.dumps({"webhook_secret": _SECRETS[3],
                                                             "emails": ["me@x.test"]})})
        c.execute(text(
            "INSERT INTO scraper_configs (id, user_id, name, county, state, record_type, "
            "fields, enrichment, schedule, deliver) VALUES (:config, :user, 'My Pierce', "
            "'pierce', 'WA', 'probate', '{}', '{}', '{}', CAST(:d AS json))"),
            a | {"d": deliver})
        c.execute(text("INSERT INTO jobs (id, user_id, scraper_config_id, status, export_key) "
                       "VALUES (:job, :user, :config, 'done', 'exports/x/leads.csv'), "
                       "(:running, :user, :config, 'running', NULL)"), a)
        c.execute(text(
            "INSERT INTO results (id, job_id, user_id, party_name, property_address, "
            "parcel_id, is_duplicate) VALUES "
            "(gen_random_uuid(), :job, :user, :p, '1 Main St', 'P-1', false), "
            "(gen_random_uuid(), :job, :user, 'Dup Person', '2 Main St', 'P-2', true), "
            "(gen_random_uuid(), :running, :user, 'Running Person', '3 Main St', 'P-3', false)"),
            a | {"p": party})
        c.execute(text("INSERT INTO account_exports (id, user_id) VALUES (:export, :user)"), a)
    return a


def _row(export_id: str):
    with sync_engine.connect() as c:
        return c.execute(text("SELECT * FROM account_exports WHERE id = :i"),
                         {"i": export_id}).one()


def _run(store=None, send=None):
    return worker._build_account_exports_impl(store=store or FakeStore(),
                                              send=send or Sent())


def _due(export_id: str) -> None:
    with sync_engine.begin() as c:
        c.execute(text("UPDATE account_exports SET next_attempt_at = now() - interval '1 s' "
                       "WHERE id = :i"), {"i": export_id})


def _set_pending_deletion(uid: str) -> None:
    with sync_engine.begin() as c:
        c.execute(text("SET LOCAL ROLE bridgeleads_purge"))
        c.execute(text("UPDATE users SET deletion_state = 'pending' WHERE id = :u"), {"u": uid})


def _key(a) -> str:
    return f"exports/{a['user']}/account/{a['export']}.zip"


def _leftover_files() -> set[str]:
    return {p.name for p in settings.EXPORTS_DIR.glob("acc*")}


# ── The ZIP ──────────────────────────────────────────────────────────────────

def test_the_zip_holds_every_downloadable_lead_and_no_secret(made) -> None:
    before = _leftover_files()
    a, other = _account(made), _account(made, party="Other Tenant Person")
    store = FakeStore()
    assert _run(store)["built"] == "ready"

    row = _row(a["export"])
    assert row.status == "ready" and row.attempts == 1 and row.claim_id is None
    assert row.size_bytes == len(store.objects[_key(a)])
    assert row.expires_at - row.ready_at == timedelta(days=7)
    assert list(store.objects) == [_key(a)]  # one export built per run
    assert _row(other["export"]).status == "pending"

    zf = zipfile.ZipFile(io.BytesIO(store.objects[_key(a)]))
    lead_file = f"leads/{a['job']}.csv"
    assert set(zf.namelist()) == {"README.txt", "profile.json", "scrapers.json",
                                  "batches.json", "runs.json", lead_file}
    everything = b"".join(zf.read(n) for n in zf.namelist()).decode()
    for secret in _SECRETS:
        assert secret not in everything
    assert "Other Tenant Person" not in everything

    profile = json.loads(zf.read("profile.json"))
    assert (profile["email"], profile["first_name"]) == (a["email"], "Ada")
    assert "password_hash" not in profile and "api_key_hash" not in profile
    scrapers = json.loads(zf.read("scrapers.json"))
    assert [s["name"] for s in scrapers] == ["My Pierce"]
    assert scrapers[0]["deliver"]["webhook_secret_set"] is True
    assert json.loads(zf.read("batches.json"))[0]["deliver"] == {"emails": ["me@x.test"]}
    runs = {r["id"]: r for r in json.loads(zf.read("runs.json"))["runs"]}
    assert runs[a["job"]]["lead_file"] == lead_file
    assert runs[a["running"]]["lead_file"] is None  # not finished: no file to download

    csv_text = zf.read(lead_file).decode()
    lines = csv_text.strip().splitlines()
    assert len(lines) == 2  # header + the one deliverable lead (the duplicate is not)
    assert list(CRM_V1_LABELS.values())[0] in lines[0]  # the config's CRM layout
    assert "'=HYPERLINK" in csv_text  # formula neutralised
    assert "Dup Person" not in csv_text and "Running Person" not in csv_text
    assert _leftover_files() == before  # temp CSVs and the ZIP removed


def test_over_the_row_or_byte_cap_fails_and_uploads_nothing(made, monkeypatch) -> None:
    a = _account(made)
    monkeypatch.setattr(settings, "ACCOUNT_EXPORT_MAX_ROWS", 0)
    store = FakeStore()
    assert _run(store)["built"] == "failed"
    assert (_row(a["export"]).status, _row(a["export"]).last_error) == ("failed", "too_large")
    assert store.objects == {}

    b = _account(made)
    monkeypatch.setattr(settings, "ACCOUNT_EXPORT_MAX_ROWS", 250_000)
    monkeypatch.setattr(settings, "ACCOUNT_EXPORT_MAX_BYTES", 10)
    assert _run(store)["built"] == "failed"
    assert _row(b["export"]).last_error == "too_large" and store.objects == {}


# ── Deletion ─────────────────────────────────────────────────────────────────

def test_an_account_scheduled_for_deletion_gets_no_export(made) -> None:
    a = _account(made)
    _set_pending_deletion(a["user"])
    store = FakeStore()
    assert _run(store)["built"] == "failed"
    assert _row(a["export"]).last_error == "deletion_requested"
    assert store.objects == {}


def test_a_deletion_request_during_the_build_wins(made) -> None:
    """The request lands after the upload, before publishing: the export is never
    downloadable and its object is deleted (decision A)."""
    a = _account(made)
    store = FakeStore(on_upload=lambda: _set_pending_deletion(a["user"]))
    assert _run(store)["built"] == "failed"
    row = _row(a["export"])
    assert (row.status, row.last_error, row.ready_at) == ("failed", "deletion_requested", None)
    assert store.objects == {}


# ── Retries, leases ──────────────────────────────────────────────────────────

def test_upload_failures_back_off_then_give_up(made) -> None:
    a = _account(made)
    store = FakeStore(fail_upload=True)
    assert _run(store)["built"] == "retry"
    row = _row(a["export"])
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "upload_failed")
    assert _run(store)["built"] is None  # backing off: nothing claimed
    for _ in range(2):
        _due(a["export"])
        _run(store)
    row = _row(a["export"])
    assert (row.status, row.attempts, row.last_error) == ("failed", 3, "upload_failed")


def test_a_crashed_build_is_claimed_again_until_it_gives_up(made) -> None:
    a = _account(made)
    with sync_engine.begin() as c:  # a build whose worker died mid-way
        c.execute(text("UPDATE account_exports SET status = 'building', attempts = 1, "
                       "claim_id = gen_random_uuid(), "
                       "claimed_until = now() - interval '1 minute' WHERE id = :i"),
                  {"i": a["export"]})
    assert _run()["built"] == "ready"
    assert _row(a["export"]).attempts == 2

    b = _account(made)
    with sync_engine.begin() as c:  # one that died three times
        c.execute(text("UPDATE account_exports SET status = 'building', attempts = 3, "
                       "claim_id = gen_random_uuid(), "
                       "claimed_until = now() - interval '1 minute' WHERE id = :i"),
                  {"i": b["export"]})
    assert _run()["built"] == "failed"
    assert _row(b["export"]).last_error == "build_failed"


def test_a_live_lease_is_never_taken(made) -> None:
    a = _account(made)
    with sync_engine.begin() as c:
        c.execute(text("UPDATE account_exports SET status = 'building', attempts = 1, "
                       "claim_id = gen_random_uuid(), "
                       "claimed_until = now() + interval '10 minutes' WHERE id = :i"),
                  {"i": a["export"]})
    assert _run()["built"] is None
    assert _row(a["export"]).status == "building"


def test_one_run_at_a_time(made) -> None:
    _account(made)
    with sync_engine.connect() as c:
        c.execute(text("SELECT pg_advisory_lock(:k)"), {"k": worker._LOCK_KEY})
        try:
            assert _run() == {"skipped": True}
        finally:
            c.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": worker._LOCK_KEY})


# ── Email ────────────────────────────────────────────────────────────────────

def test_the_link_is_emailed_once_to_the_current_address(made, mail_on) -> None:
    a = _account(made)
    sent = Sent()
    _run(send=sent)
    assert len(sent.calls) == 1
    to, link, expires_at = sent.calls[0]
    row = _row(a["export"])
    assert to == a["email"] and expires_at == row.expires_at
    assert row.email_sent_at is not None
    prefix = f"https://api.bl.test/auth/export/{a['export']}/download?token="
    assert link.startswith(prefix)
    claims = jwt.decode(link[len(prefix):], settings.SECRET_KEY, algorithms=["HS256"],
                        audience="bridgeleads-download", issuer="bridgeleads")
    assert (claims["purpose"], claims["export_id"], claims["sub"]) == (
        "account_export", a["export"], a["user"])
    assert abs(claims["exp"] - row.expires_at.timestamp()) < 5  # the link dies with it
    _run(send=sent)
    assert len(sent.calls) == 1


def test_a_failed_email_never_unreadies_the_export(made, mail_on) -> None:
    a = _account(made)
    _run(send=Sent(fail=True))
    row = _row(a["export"])
    assert (row.status, row.email_attempts, row.email_sent_at) == ("ready", 1, None)
    sent = Sent()
    _run(send=sent)
    assert sent.calls == []  # backing off
    _due(a["export"])
    _run(send=sent)
    assert len(sent.calls) == 1 and _row(a["export"]).email_sent_at is not None


def test_no_email_for_an_account_scheduled_for_deletion(made, mail_on) -> None:
    a = _account(made)
    _run(send=Sent(fail=True))  # ready, email owed
    _set_pending_deletion(a["user"])
    _due(a["export"])
    sent = Sent()
    _run(send=sent)
    assert sent.calls == []


# ── Expiry ───────────────────────────────────────────────────────────────────

def test_after_seven_days_the_object_is_deleted(made) -> None:
    a = _account(made)
    store = FakeStore()
    _run(store)
    with sync_engine.begin() as c:
        c.execute(text("UPDATE account_exports SET ready_at = now() - interval '8 days', "
                       "expires_at = now() - interval '1 day' WHERE id = :i"),
                  {"i": a["export"]})
    assert _run(store)["expired"] == 1
    assert _row(a["export"]).status == "expired" and store.objects == {}
