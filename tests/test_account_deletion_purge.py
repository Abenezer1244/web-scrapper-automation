"""Migration 113: account-deletion purge (write fence + claim/progress/purge/complete).

Real database. Most tests run in one transaction that is rolled back; expected failures
run inside a SAVEPOINT so the outer transaction survives. The concurrency test and the
sign-in test need committed rows (two connections / the API's own session) and delete
their users afterwards (users CASCADE to everything they created).

The acceptance criterion is docs/product/account-deletion-retention-matrix.md §4.
Role-boundary tests need the provisioned bridgeleads_app/system roles and SKIP where
they are absent (CI).
"""

from __future__ import annotations

import random
import threading
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.api.auth import hash_password
from src.db.models import User
from src.db.session import sync_engine
from src.utils.crypto import blind_index

_LEASE = "interval '5 minutes'"
# skip_trace_queues is deliberately absent: a queue row is shared by several tenants.
_FENCED = (
    "results", "jobs", "scraper_configs", "scraper_batches", "batch_runs",
    "notifications", "user_record_views", "property_list_membership",
    "dialer_deliveries", "delivered_records", "pending_skip_trace_rows",
    "user_sessions", "user_avatars", "pending_email_changes",
    "password_history", "mfa_backup_codes", "mfa_break_glass_codes", "job_logs",
)
# Matrix §2 DELETE tables with their own user_id (job_logs, skip_trace_cache and
# pending_registrations are checked separately).
_DELETED = (
    "user_sessions", "user_avatars", "pending_email_changes", "password_history",
    "mfa_backup_codes", "mfa_break_glass_codes", "notifications", "user_record_views",
    "property_list_membership", "dialer_deliveries", "batch_runs",
)
# Rows that must survive with an unchanged count: the billing ledgers, delivered_records,
# audit_events, and the scrubbed skeletons they hang off.
_KEPT = (
    "contact_lookup_actions", "contact_lookup_action_results",
    "contact_lookup_action_events", "skip_trace_meter_events", "delivered_records",
    "audit_events", "jobs", "results", "scraper_configs", "scraper_batches",
    "pending_skip_trace_rows", "skip_trace_queues",
)
_SCRUBBED = {
    "results": ("party_name", "heirs", "legal_description", "mailing_address",
                "enrichment_data", "phone", "phone_type", "phone_dnc_flag", "email", "phones",
                "emails", "owner_state", "absentee_owner", "out_of_state_owner",
                "last_trace_outcome", "skip_trace_subject_hash"),
    "pending_skip_trace_rows": ("first_name", "last_name", "mail_address", "mail_city",
                                "mail_state", "mail_zip"),
    "skip_trace_queues": ("download_url", "error_message"),
    "jobs": ("export_key", "error_message"),
    "scraper_configs": ("doc_types", "include_living_owner_tod"),
    "audit_events": ("detail",),
}


@pytest.fixture
def conn():
    with sync_engine.connect() as c:
        trans = c.begin()
        try:
            yield c
        finally:
            trans.rollback()


def _sqlstate(conn, sql: str, params: dict | None = None) -> str | None:
    """Run `sql` in a savepoint; return the SQLSTATE it raised, or None."""
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql), params or {})
    except DBAPIError as exc:
        sp.rollback()
        return getattr(exc.orig, "pgcode", None)
    sp.rollback()
    return None


def _user(conn, *, trial: bool = False) -> str:
    uid = str(uuid.uuid4())
    email = f"purge_{uid[:8]}@bl.test"
    conn.execute(
        text("""
            INSERT INTO users (id, email, email_hmac, password_hash, plan, records_used,
                records_limit, is_active, is_admin, referral_credit_cents, trial_consumed_at)
            VALUES (:id, :e, :h, :pw, 'starter', 0, 50, true, false, 0,
                    CASE WHEN :trial THEN now() END)
        """),
        {"id": uid, "e": email, "h": blind_index(email), "pw": hash_password("Pw-123456789"),
         "trial": trial},
    )
    return uid


def _seed(conn, uid: str) -> dict[str, str]:
    """One row (two where batching is exercised) in every table the matrix names."""
    ids = {k: str(uuid.uuid4()) for k in ("batch", "config", "job", "r1", "r2", "action")}
    ids.update(u=uid, subject=f"subj-{uid}", cachekey=f"key-{uid}",
               qn=random.randint(1, 2**31 - 1))
    ids["hmac"] = conn.execute(text("SELECT email_hmac FROM users WHERE id = :u"),
                               {"u": uid}).scalar()
    for sql in (
        """INSERT INTO scraper_batches (id, user_id, name, state, fields, enrichment, schedule,
               deliver) VALUES (:batch, :u, 'Spring list', 'WA', '["a"]', '["b"]',
               '{"cron": "x"}', '{"email": "me@x.test"}')""",
        """INSERT INTO scraper_configs (id, user_id, name, county, state, record_type, fields,
               enrichment, schedule, deliver, doc_types, include_living_owner_tod, batch_id)
           VALUES (:config, :u, 'My Pierce', 'pierce', 'WA', 'probate', '["a"]', '["b"]',
               '{"cron": "x"}', '{"webhook": "https://hook.test"}', '["will"]', true, :batch)""",
        """INSERT INTO jobs (id, user_id, scraper_config_id, export_key, error_message,
               billed_count) VALUES (:job, :u, :config, 'exports/x.csv', 'vendor: 1 Main', 3)""",
        """INSERT INTO results (id, job_id, user_id, party_name, heirs, legal_description,
               parcel_id, property_address, mailing_address, enrichment_data, phone,
               phone_type, phone_dnc_flag, email, phones, emails, owner_state,
               absentee_owner, out_of_state_owner, last_trace_outcome,
               skip_trace_subject_hash)
           SELECT r, :job, :u, 'Jane Doe', 'Kid Doe', 'Lot 1', 'P-1', '1 Main St',
               'PO Box 1', '{"a": 1}', '555', 'mobile', false, 'j@x.test', '["555"]',
               '["j@x.test"]', 'OR', true, true, 'provider_rejected',
               CASE WHEN r = CAST(:r1 AS uuid) THEN :subject END
             FROM unnest(ARRAY[CAST(:r1 AS uuid), CAST(:r2 AS uuid)]) r""",
        """INSERT INTO contact_lookup_actions (id, user_id, job_id, category, quote_id, status,
               unit_price_cents, currency, pricing_version)
           VALUES (:action, :u, :job, 'skip_trace', 'q-' || :action, 'settled', 10, 'usd', 'v1')""",
        """INSERT INTO contact_lookup_action_results (id, action_id, user_id, result_id,
               disposition) VALUES (gen_random_uuid(), :action, :u, :r1, 'answered_hit')""",
        """INSERT INTO contact_lookup_action_events (id, action_id, user_id, to_status)
           VALUES (gen_random_uuid(), :action, :u, 'settled')""",
        """INSERT INTO skip_trace_queues (id, tracerfy_queue_id, job_id, user_id, status,
               download_url, error_message) VALUES (gen_random_uuid(), :qn, :job, :u,
               'completed', 'https://vendor.test/x.csv', 'row: 1 Main St')""",
        """INSERT INTO skip_trace_meter_events (id, tracerfy_queue_id, user_id, billable_units)
           VALUES (gen_random_uuid(), :qn, :u, 1)""",
        """INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id, property_address,
               first_name, last_name, mail_address, mail_city, mail_state, mail_zip, action_id)
           VALUES (gen_random_uuid(), :job, :r1, :u, '1 Main St', 'Jane', 'Doe', 'PO Box 1',
               'Tacoma', 'WA', '98401', :action)""",
        """INSERT INTO batch_runs (id, batch_id, user_id, child_job_ids, combined_export_key)
           VALUES (gen_random_uuid(), :batch, :u, '[]', 'exports/b.csv')""",
        """INSERT INTO notifications (id, user_id, type, job_id, detail)
           VALUES (gen_random_uuid(), :u, 'job_done', :job, '{"n": 1}')""",
        "INSERT INTO user_record_views (user_id, scraper_config_id) VALUES (:u, :config)",
        """INSERT INTO property_list_membership (user_id, record_type, property_key)
           VALUES (:u, 'probate', 'k1'), (:u, 'probate', 'k2')""",
        """INSERT INTO dialer_deliveries (id, job_id, result_id, user_id, scraper_config_id,
               vendor_id) VALUES (gen_random_uuid(), :job, :r1, :u, :config, 'v')""",
        """INSERT INTO delivered_records (id, user_id, dedup_hash, parcel_id, property_address)
           VALUES (gen_random_uuid(), :u, 'd-' || :u, 'P-1', '1 Main St')""",
        "INSERT INTO user_sessions (id, user_id) VALUES (left(replace(:u, '-', ''), 32), :u)",
        "INSERT INTO user_avatars (user_id, version) VALUES (:u, 'v1')",
        """INSERT INTO pending_email_changes (id, user_id, new_email, new_email_hmac, expires_at)
           VALUES (gen_random_uuid(), :u, 'enc', 'h-' || :u, now() + interval '1 day')""",
        """INSERT INTO password_history (id, user_id, password_hash)
           VALUES (gen_random_uuid(), :u, 'x')""",
        """INSERT INTO mfa_backup_codes (id, user_id, code_hash)
           VALUES (gen_random_uuid(), :u, 'x')""",
        """INSERT INTO mfa_break_glass_codes (id, user_id, code_hash, batch_id, created_by)
           VALUES (gen_random_uuid(), :u, 'x', gen_random_uuid(), 'ops')""",
        """INSERT INTO job_logs (id, job_id, message)
           VALUES (gen_random_uuid(), :job, 'traced 1 Main St')""",
        """INSERT INTO skip_trace_cache (address_hash, phone)
           VALUES (:subject, '555'), (:cachekey, '555')""",
        """INSERT INTO pending_registrations (id, email_hmac, email, expires_at)
           VALUES (gen_random_uuid(), :hmac, 'enc', now() + interval '1 day')""",
        """INSERT INTO audit_events (id, event, user_id, detail)
           VALUES (gen_random_uuid(), 'login_success', :u, 'free text')""",
    ):
        conn.execute(text(sql), ids)
    return ids


def _open_due(conn, uid: str, *, stripe: str = "cancel_set", due: bool = True) -> str:
    """A pending deletion whose 30 days are up. purge_after is immutable once written,
    so the row is created already due, as the purge role (the only writer)."""
    conn.execute(text("SET LOCAL ROLE bridgeleads_purge"))
    conn.execute(text("UPDATE users SET deletion_state = 'pending' WHERE id = :u"), {"u": uid})
    did = conn.execute(
        text("INSERT INTO account_deletions (user_id, status, purge_after, stripe_state) "
             "VALUES (:u, 'pending', CASE WHEN :due THEN timestamptz '1990-01-01' "
             "ELSE now() + interval '30 days' END, :s) RETURNING id"),
        {"u": uid, "due": due, "s": stripe},
    ).scalar()
    conn.execute(text("RESET ROLE"))
    return str(did)


def _as_purge(conn, sql: str, params: dict) -> None:
    """Move a marker/lease clock directly (stands in for the passage of time)."""
    conn.execute(text("SET LOCAL ROLE bridgeleads_purge"))
    conn.execute(text(sql), params)
    conn.execute(text("RESET ROLE"))


def _claim(conn):
    return conn.execute(text(f"SELECT * FROM claim_account_deletion({_LEASE})")).one_or_none()


def _progress(conn, did, token, phase, frm=None, to=None, err=None):
    return conn.execute(
        text("SELECT record_deletion_progress(:d, :t, :p, :f, :to, :e)"),
        {"d": did, "t": token, "p": phase, "f": frm, "to": to, "e": err},
    ).scalar()


def _purge(conn, did, token, keys=None, batch=1000) -> bool:
    return conn.execute(
        text("SELECT purge_account_data(:d, :t, :k, :b)"),
        {"d": did, "t": token, "k": keys, "b": batch},
    ).scalar()


def _deletion(conn, did):
    return conn.execute(text("SELECT * FROM account_deletions WHERE id = :d"), {"d": did}).one()


def _count(conn, table: str, uid: str) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {table} WHERE user_id = :u"),
                        {"u": uid}).scalar()


def _snapshot(conn, ids: dict) -> dict:
    """Every row a tenant owns, as text, for the byte-identical check."""
    snap = {}
    tables = sorted({*_DELETED, *_KEPT})
    for t in tables:
        snap[t] = conn.execute(text(
            f"SELECT row_to_json(x)::text FROM {t} x WHERE user_id = :u ORDER BY 1"),
            {"u": ids["u"]}).scalars().all()
    snap["job_logs"] = conn.execute(text(
        "SELECT row_to_json(l)::text FROM job_logs l JOIN jobs j ON j.id = l.job_id "
        "WHERE j.user_id = :u ORDER BY 1"), {"u": ids["u"]}).scalars().all()
    snap["skip_trace_cache"] = conn.execute(text(
        "SELECT row_to_json(c)::text FROM skip_trace_cache c "
        "WHERE address_hash IN (:a, :b) ORDER BY 1"),
        {"a": ids["subject"], "b": ids["cachekey"]}).scalars().all()
    snap["pending_registrations"] = conn.execute(text(
        "SELECT row_to_json(p)::text FROM pending_registrations p WHERE email_hmac = :h"),
        {"h": ids["hmac"]}).scalars().all()
    snap["users"] = conn.execute(text(
        "SELECT row_to_json(u)::text FROM users u WHERE id = :u"), {"u": ids["u"]}).scalars().all()
    return snap


# ── Claim ────────────────────────────────────────────────────────────────────────

def test_claim_takes_only_a_due_deletion_whose_billing_is_stopped(conn) -> None:
    uid, other = _user(conn), _user(conn)
    ids, other_ids = _seed(conn, uid), _seed(conn, other)
    # A lookup not yet sent (and another tenant's), plus one already with the vendor.
    conn.execute(text("UPDATE results SET skip_trace_status = 'queued' WHERE id IN (:a, :b)"),
                 {"a": ids["r1"], "b": other_ids["r1"]})
    conn.execute(text(
        "INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id, property_address, "
        "status) VALUES (gen_random_uuid(), :job, :r2, :u, '1 Main St', 'submitted')"), ids)
    did = _open_due(conn, uid, stripe="pending_cancel")
    assert _claim(conn) is None  # still being billed

    assert _progress(conn, did, None, "stripe", "pending_cancel", "cancel_set") is True
    claim = _claim(conn)
    assert (str(claim.deletion_id), str(claim.user_id), claim.reclaimed) == (did, uid, False)
    state, active = conn.execute(text(
        "SELECT deletion_state, is_active FROM users WHERE id = :u"), {"u": uid}).one()
    assert (state, active) == ("purging", False)
    row = _deletion(conn, did)
    assert (row.status, str(row.claim_token), row.attempts) == ("purging", str(claim.claim_token), 1)
    assert _claim(conn) is None  # the live lease is not up for grabs
    # The unsent lookup is withdrawn exactly as the dispatcher withdraws one; the one
    # already with the vendor is left for the precondition; the other tenant untouched.
    pending = dict(conn.execute(text(
        "SELECT result_id::text, status FROM pending_skip_trace_rows WHERE user_id = :u"),
        {"u": uid}).all())
    assert pending == {ids["r1"]: "cancelled", ids["r2"]: "submitted"}
    statuses = dict(conn.execute(text(
        "SELECT id::text, skip_trace_status FROM results WHERE id IN (:a, :b)"),
        {"a": ids["r1"], "b": other_ids["r1"]}).all())
    assert statuses == {ids["r1"]: "not_attempted", other_ids["r1"]: "queued"}
    assert conn.execute(text("SELECT status FROM pending_skip_trace_rows WHERE user_id = :u"),
                        {"u": other}).scalar() == "queued"


def test_claim_waits_for_the_deadline_and_the_backoff(conn) -> None:
    uid = _user(conn)
    _open_due(conn, uid, due=False)
    assert _claim(conn) is None

    other = _user(conn)
    did = _open_due(conn, other)
    assert _progress(conn, did, None, "error", err="email bounced") is True
    assert _claim(conn) is None  # inside its backoff
    _as_purge(conn, "UPDATE account_deletions SET next_attempt_at = now() - interval '1 second' "
                    "WHERE id = :d", {"d": did})
    assert str(_claim(conn).deletion_id) == did


def test_reclaim_after_the_lease_expires_rotates_the_token(conn) -> None:
    uid = _user(conn)
    did = _open_due(conn, uid)
    first = _claim(conn)
    _as_purge(conn, "UPDATE account_deletions SET claimed_until = clock_timestamp() "
                    "- interval '1 second' WHERE id = :d", {"d": did})
    second = _claim(conn)
    assert (str(second.deletion_id), second.reclaimed) == (did, True)
    assert second.claim_token != first.claim_token
    assert _sqlstate(conn, "SELECT record_deletion_progress(:d, :t, 'r2_first_sweep')",
                     {"d": did, "t": first.claim_token}) == "BLD33"
    assert _sqlstate(conn, "SELECT purge_account_data(:d, :t, NULL, 10)",
                     {"d": did, "t": first.claim_token}) == "BLD33"
    assert _progress(conn, did, second.claim_token, "r2_first_sweep") is True


def test_claim_rejects_a_silly_lease(conn) -> None:
    for lease in ("interval '0'", "interval '2 hours'", "NULL"):
        assert _sqlstate(conn, f"SELECT * FROM claim_account_deletion({lease})") == "BLD30"


# ── Progress ─────────────────────────────────────────────────────────────────────

def test_progress_enforces_the_phase_order(conn) -> None:
    uid = _user(conn)
    did = _open_due(conn, uid, stripe="pending_cancel")
    # Pre-claim phases need no token.
    assert _progress(conn, did, None, "scheduled_email_sent") is True
    sent = _deletion(conn, did).scheduled_email_sent_at
    assert _progress(conn, did, None, "scheduled_email_sent") is True
    assert _deletion(conn, did).scheduled_email_sent_at == sent  # first send wins
    # Stripe is a compare-and-set along the allowed edges only.
    assert _progress(conn, did, None, "stripe", "cancel_set", "customer_deleted") is False
    assert _progress(conn, did, None, "stripe", "pending_uncancel", "uncancel_set") is False
    assert _progress(conn, did, None, "stripe", "pending_cancel", "cancel_set") is True
    assert _progress(conn, did, None, "stripe", "pending_cancel", "cancel_set") is False

    token = _claim(conn).claim_token
    assert _progress(conn, did, None, "scheduled_email_sent") is False  # no longer pending
    assert _sqlstate(conn, "SELECT record_deletion_progress(:d, NULL, 'error', NULL, NULL, 'x')",
                     {"d": did}) == "BLD32"
    for phase in ("final_email_sent", "tombstoned", "r2_final_sweep"):
        assert _sqlstate(conn, "SELECT record_deletion_progress(:d, :t, :p)",
                         {"d": did, "t": token, "p": phase}) == "BLD34", phase
    assert _sqlstate(conn, "SELECT record_deletion_progress(:d, :t, 'bogus')",
                     {"d": did, "t": token}) == "BLD35"
    assert _sqlstate(conn, "SELECT record_deletion_progress(:d, :t, 'r2_first_sweep')",
                     {"d": str(uuid.uuid4()), "t": token}) == "BLD31"

    # A purge error releases the lease and backs off.
    assert _progress(conn, did, token, "error", err="R2 503") is True
    row = _deletion(conn, did)
    assert row.last_error == "R2 503" and row.next_attempt_at > row.claimed_until
    assert _sqlstate(conn, "SELECT record_deletion_progress(:d, :t, 'r2_first_sweep')",
                     {"d": did, "t": token}) == "BLD33"


# ── Purge ────────────────────────────────────────────────────────────────────────

def test_purge_needs_the_first_r2_sweep(conn) -> None:
    uid = _user(conn)
    did = _open_due(conn, uid)
    token = _claim(conn).claim_token
    assert _sqlstate(conn, "SELECT purge_account_data(:d, :t, NULL, 10)",
                     {"d": did, "t": token}) == "BLD34"
    for batch in (0, 20001):
        assert _sqlstate(conn, "SELECT purge_account_data(:d, :t, NULL, :b)",
                         {"d": did, "t": token, "b": batch}) == "BLD30"


def test_purge_leaves_exactly_the_matrix_end_state(conn) -> None:
    victim, other = _user(conn, trial=True), _user(conn, trial=True)
    v, o = _seed(conn, victim), _seed(conn, other)
    # A previous deletion of the same address left a LONGER trial hold: it must survive.
    conn.execute(text("INSERT INTO consumed_trial_emails (email_hmac, expires_at) "
                      "VALUES (:h, now() + interval '3 years')"), {"h": v["hmac"]})
    # A Tracerfy batch still in flight, shared with co-tenants: its link must survive.
    in_flight = conn.execute(text(
        "INSERT INTO skip_trace_queues (id, tracerfy_queue_id, user_id, download_url) "
        "VALUES (gen_random_uuid(), :n, :u, 'https://vendor.test/live.csv') RETURNING id"),
        {"n": random.randint(1, 2**31 - 1), "u": victim}).scalar()
    # A finished batch "owned" by a co-tenant (user_id = its first tenant) that also
    # carried the victim's row: its link holds the victim's data too.
    co_tenant = _user(conn)
    shared_n = random.randint(1, 2**31 - 1)
    conn.execute(text(
        "INSERT INTO skip_trace_queues (id, tracerfy_queue_id, user_id, status, download_url) "
        "VALUES (gen_random_uuid(), :n, :u, 'completed', 'https://vendor.test/shared.csv')"),
        {"n": shared_n, "u": co_tenant})
    conn.execute(text("UPDATE pending_skip_trace_rows SET tracerfy_queue_id = :n "
                      "WHERE user_id = :u"), {"n": shared_n, "u": victim})
    kept = {t: _count(conn, t, victim) for t in _KEPT}
    before_other = _snapshot(conn, o)

    did = _open_due(conn, victim)
    token = _claim(conn).claim_token
    assert _progress(conn, did, token, "r2_first_sweep") is True
    # Batches of 1 over 2 results + 2 list rows: two partial calls, then done.
    calls = [_purge(conn, did, token, [v["cachekey"]], batch=1) for _ in range(3)]
    assert calls == [False, False, True]
    assert _deletion(conn, did).db_purged_at is not None

    # §4.3 DELETE tables hold nothing for the user.
    for t in _DELETED:
        assert _count(conn, t, victim) == 0, t
    assert conn.execute(text("SELECT count(*) FROM job_logs WHERE job_id = :j"),
                        {"j": v["job"]}).scalar() == 0
    # §4.0 the sign-up in progress is gone; §4.4 the user's cache keys are gone.
    assert conn.execute(text("SELECT count(*) FROM pending_registrations WHERE email_hmac = :h"),
                        {"h": v["hmac"]}).scalar() == 0
    assert conn.execute(text("SELECT count(*) FROM skip_trace_cache "
                             "WHERE address_hash IN (:a, :b)"),
                        {"a": v["subject"], "b": v["cachekey"]}).scalar() == 0
    # §4.2 every SCRUB column is blank, every KEEP count unchanged.
    for t, cols in _SCRUBBED.items():
        filled = " OR ".join(f"{c} IS NOT NULL" for c in cols)
        assert conn.execute(text(f"SELECT count(*) FROM {t} WHERE user_id = :u AND ({filled}) "
                                 "AND id <> :q"),
                            {"u": victim, "q": str(in_flight)}).scalar() == 0, t
    assert conn.execute(text("SELECT download_url FROM skip_trace_queues WHERE id = :q"),
                        {"q": str(in_flight)}).scalar() == "https://vendor.test/live.csv"
    assert conn.execute(text("SELECT download_url FROM skip_trace_queues "
                             "WHERE tracerfy_queue_id = :n"), {"n": shared_n}).scalar() is None
    assert {t: _count(conn, t, victim) for t in _KEPT} == kept
    cfg = conn.execute(text("SELECT name, fields::text, enrichment::text, schedule::text, "
                            "deliver::text, active FROM scraper_configs WHERE user_id = :u"),
                       {"u": victim}).one()
    assert tuple(cfg) == ("deleted", "[]", "[]", "{}", "{}", False)
    batch = conn.execute(text("SELECT name, fields::text, schedule::text, deliver::text, "
                              "delivery_mode, status FROM scraper_batches WHERE user_id = :u"),
                         {"u": victim}).one()
    assert tuple(batch) == ("deleted", "[]", "{}", "{}", "everything", "archived")
    # Public record kept on the lead shell (owner decision 1).
    assert conn.execute(text("SELECT count(*) FROM results WHERE user_id = :u "
                             "AND parcel_id = 'P-1' AND property_address = '1 Main St'"),
                        {"u": victim}).scalar() == 2
    # Trial-fraud hold: present, and the longer expiry kept.
    assert conn.execute(text("SELECT expires_at > now() + interval '2 years 6 months' "
                             "FROM consumed_trial_emails WHERE email_hmac = :h"),
                        {"h": v["hmac"]}).scalar() is True
    # §4.1 the other tenant is byte-identical.
    assert _snapshot(conn, o) == before_other
    # §4.6 a re-run is a no-op.
    after = _snapshot(conn, v)
    assert _purge(conn, did, token, [v["cachekey"]], batch=1) is True
    assert _snapshot(conn, v) == after


def test_purge_records_a_trial_hold_only_for_a_used_trial(conn) -> None:
    uid = _user(conn, trial=False)
    hmac = _seed(conn, uid)["hmac"]
    did = _open_due(conn, uid)
    token = _claim(conn).claim_token
    _progress(conn, did, token, "r2_first_sweep")
    assert _purge(conn, did, token) is True
    assert conn.execute(text("SELECT count(*) FROM consumed_trial_emails WHERE email_hmac = :h"),
                        {"h": hmac}).scalar() == 0


# ── Complete ─────────────────────────────────────────────────────────────────────

def test_complete_needs_every_phase_and_the_24_hour_gap(conn) -> None:
    uid = _user(conn)
    ids = _seed(conn, uid)
    did = _open_due(conn, uid)
    token = _claim(conn).claim_token

    def refused(tok) -> bool:
        return _sqlstate(conn, "SELECT complete_account_deletion(:d, :t)",
                         {"d": did, "t": tok}) == "BLD34"

    assert refused(token)
    _progress(conn, did, token, "r2_first_sweep")
    assert _purge(conn, did, token) is True
    assert refused(token)
    _progress(conn, did, token, "final_email_sent")
    assert refused(token)
    # The marker needs the users row really tombstoned (what SQL can see of it).
    conn.execute(text("UPDATE users SET first_name = 'Jane' WHERE id = :u"), {"u": uid})
    assert _sqlstate(conn, "SELECT record_deletion_progress(:d, :t, 'tombstoned')",
                     {"d": did, "t": token}) == "BLD34"
    conn.execute(text("UPDATE users SET first_name = NULL WHERE id = :u"), {"u": uid})
    # Tombstone parks the row until 24 h after the first sweep, lease released.
    _progress(conn, did, token, "tombstoned")
    row = _deletion(conn, did)
    assert row.next_attempt_at == row.r2_first_sweep_at + __import__("datetime").timedelta(hours=24)
    assert _sqlstate(conn, "SELECT complete_account_deletion(:d, :t)",
                     {"d": did, "t": token}) == "BLD33"
    assert _claim(conn) is None  # not yet 24 h

    _as_purge(conn, "UPDATE account_deletions SET r2_first_sweep_at = r2_first_sweep_at "
                    "- interval '25 hours', next_attempt_at = next_attempt_at "
                    "- interval '25 hours' WHERE id = :d", {"d": did})
    token = _claim(conn).claim_token
    assert refused(token)
    _progress(conn, did, token, "r2_final_sweep")
    # An UPDATE that was in flight at the claim lands after the first pass (written here
    # through the purge role, the only writer the fence lets through): complete refuses
    # until the purge has run again.
    for late in ("UPDATE results SET party_name = 'Late Write' WHERE id = :r",
                 "UPDATE scraper_configs SET deliver = '{\"email\": \"x@y.test\"}' "
                 "WHERE user_id = :u"):
        _as_purge(conn, late, {"r": ids["r1"], "u": uid})
        assert _sqlstate(conn, "SELECT complete_account_deletion(:d, :t)",
                         {"d": did, "t": token}) == "BLD36", late
        assert _purge(conn, did, token) is True
    # A Tracerfy batch carrying the user's row still in flight: completion waits for it
    # to finish, and the link it finishes with is never stored (shared batches are
    # pinned, not refused, once any tenant is purging).
    queue_n = random.randint(1, 2**31 - 1)
    conn.execute(text("INSERT INTO skip_trace_queues (id, tracerfy_queue_id, user_id) "
                      "VALUES (gen_random_uuid(), :n, :u)"), {"n": queue_n, "u": uid})
    assert _sqlstate(conn, "SELECT complete_account_deletion(:d, :t)",
                     {"d": did, "t": token}) == "BLD36"
    conn.execute(text("UPDATE skip_trace_queues SET status = 'completed', "
                      "download_url = 'https://vendor.test/late.csv' "
                      "WHERE tracerfy_queue_id = :n"), {"n": queue_n})
    assert conn.execute(text("SELECT download_url FROM skip_trace_queues "
                             "WHERE tracerfy_queue_id = :n"), {"n": queue_n}).scalar() is None
    # An audit row written while purging (a refused sign-in) loses its detail too.
    conn.execute(text("INSERT INTO audit_events (id, event, user_id, detail) "
                      "VALUES (gen_random_uuid(), 'login_failure', :u, 'late')"), {"u": uid})
    conn.execute(text("SELECT complete_account_deletion(:d, :t)"), {"d": did, "t": token})

    row = _deletion(conn, did)
    assert (row.status, row.claim_token, row.completed_at is not None) == ("completed", None, True)
    assert conn.execute(text("SELECT deletion_state FROM users WHERE id = :u"),
                        {"u": uid}).scalar() == "deleted"
    assert conn.execute(text("SELECT count(*) FROM audit_events WHERE user_id = :u "
                             "AND detail IS NOT NULL"), {"u": uid}).scalar() == 0
    # Stripe cleanup continues after completion, once the subscription has ended.
    assert _progress(conn, did, None, "stripe", "cancel_set", "customer_deleted") is True


# ── Fence ────────────────────────────────────────────────────────────────────────

def test_fence_refuses_inserts_and_pins_scrubbed_columns_once_purging(conn) -> None:
    uid, bystander = _user(conn), _user(conn)
    ids = _seed(conn, uid)
    bys = _seed(conn, bystander)
    did = _open_due(conn, uid)
    # Pending accounts are still writable (the user may restore).
    conn.execute(text("UPDATE notifications SET read_at = now() WHERE user_id = :u"), {"u": uid})
    conn.execute(text("UPDATE scraper_configs SET active = false WHERE user_id = :u"), {"u": uid})
    _claim(conn)

    note = ("INSERT INTO notifications (id, user_id, type) "
            "VALUES (gen_random_uuid(), :u, 'job_done')")
    assert _sqlstate(conn, note, {"u": uid}) == "BLD20"
    # A cross-tenant sweep is never aborted: the status write lands, the personal data
    # does not (pinned to its old value until the purge blanks it, then to NULL).
    conn.execute(text("UPDATE results SET phone = '999', party_name = 'X', "
                      "skip_trace_status = 'errored' WHERE user_id IN (:u, :b)"),
                 {"u": uid, "b": bystander})
    rows = conn.execute(text("SELECT user_id::text, phone, party_name, skip_trace_status "
                             "FROM results WHERE user_id IN (:u, :b)"),
                        {"u": uid, "b": bystander}).all()
    assert {(r.user_id, r.phone, r.party_name, r.skip_trace_status) for r in rows} == {
        (uid, "555", "Jane Doe", "errored"), (bystander, "999", "X", "errored")}
    # A row cannot join a Tracerfy batch after the claim (the purge finds batches by it).
    conn.execute(text("UPDATE pending_skip_trace_rows SET tracerfy_queue_id = 42, "
                      "status = 'submitted' WHERE user_id = :u"), {"u": uid})
    assert tuple(conn.execute(text(
        "SELECT tracerfy_queue_id, status FROM pending_skip_trace_rows WHERE user_id = :u"),
        {"u": uid}).one()) == (None, "submitted")
    conn.execute(text("UPDATE scraper_configs SET active = true, paused_reason = NULL "
                      "WHERE user_id = :u"), {"u": uid})
    assert conn.execute(text("SELECT active FROM scraper_configs WHERE user_id = :u"),
                        {"u": uid}).scalar() is False  # a paused schedule never revives
    conn.execute(text("UPDATE notifications SET read_at = now() WHERE user_id = :u"),
                 {"u": uid})  # rows the purge deletes: updates are harmless
    # Audit events are kept, but a purging owner's free text is never stored.
    conn.execute(text("INSERT INTO audit_events (id, event, user_id, detail) VALUES "
                      "(gen_random_uuid(), 'late', :u, 'free text'), "
                      "(gen_random_uuid(), 'late', :b, 'free text')"),
                 {"u": uid, "b": bystander})
    conn.execute(text("UPDATE audit_events SET detail = 'again' WHERE user_id = :u"), {"u": uid})
    late = dict(conn.execute(text(
        "SELECT user_id::text, detail FROM audit_events WHERE event = 'late'")).all())
    assert late == {uid: None, bystander: "free text"}
    assert conn.execute(text("SELECT count(*) FROM audit_events WHERE user_id = :u "
                             "AND detail IS NOT NULL"), {"u": uid}).scalar() == 0
    assert _sqlstate(conn, "INSERT INTO delivered_records (id, user_id, dedup_hash) "
                           "VALUES (gen_random_uuid(), :u, 'late')", {"u": uid}) == "BLD20"
    assert _sqlstate(conn, "INSERT INTO user_sessions (id, user_id) VALUES ('late', :u)",
                     {"u": uid}) == "BLD20"
    # job_logs has no user_id: the owner is resolved through jobs.
    assert _sqlstate(conn, "INSERT INTO job_logs (id, job_id, message) "
                           "VALUES (gen_random_uuid(), :j, 'late')", {"j": ids["job"]}) == "BLD20"
    # One fenced row fails the whole multi-row statement.
    assert _sqlstate(conn, "INSERT INTO notifications (id, user_id, type) VALUES "
                           "(gen_random_uuid(), :b, 'x'), (gen_random_uuid(), :u, 'x')",
                     {"u": uid, "b": bystander}) == "BLD20"
    # Everyone else is untouched, and the purge's own role writes through.
    conn.execute(text(note), {"u": bystander})
    conn.execute(text("INSERT INTO job_logs (id, job_id, message) "
                      "VALUES (gen_random_uuid(), :j, 'ok')"), {"j": bys["job"]})
    _as_purge(conn, "UPDATE results SET phone = NULL WHERE user_id = :u", {"u": uid})
    assert did


def test_fence_never_lets_a_row_change_owner(conn) -> None:
    a, b = _user(conn), _user(conn)
    ids_a, ids_b = _seed(conn, a), _seed(conn, b)
    assert _sqlstate(conn, "UPDATE notifications SET user_id = :b WHERE user_id = :a",
                     {"a": a, "b": b}) == "BLD21"
    assert _sqlstate(conn, "UPDATE job_logs SET job_id = :jb WHERE job_id = :ja",
                     {"ja": ids_a["job"], "jb": ids_b["job"]}) == "BLD21"
    for change in ("user_id = :b", "tracerfy_queue_id = tracerfy_queue_id + 1"):
        assert _sqlstate(conn, f"UPDATE skip_trace_queues SET {change} WHERE user_id = :a",
                         {"a": a, "b": b}) == "BLD21", change
    for target in (":b", "NULL"):
        assert _sqlstate(conn, f"UPDATE audit_events SET user_id = {target} WHERE user_id = :a",
                         {"a": a, "b": b}) == "BLD21"


def test_claim_skips_an_account_with_a_write_in_flight() -> None:
    """A writer passed the fence while the account was pending and has not committed.
    The claim must not take the account until it commits (else the write would land
    after the claim); the purge then removes what it wrote, and a writer arriving after
    the claim is refused."""
    with sync_engine.connect() as setup, setup.begin():
        uid = _user(setup)
        did = _open_due(setup, uid)
    writer = sync_engine.connect()
    try:
        w = writer.begin()
        writer.execute(text("INSERT INTO notifications (id, user_id, type) "
                            "VALUES (gen_random_uuid(), :u, 'in_flight')"), {"u": uid})
        with sync_engine.connect() as p:
            with p.begin():
                assert _claim(p) is None  # skipped, not blocked: the writer holds KEY SHARE
            w.commit()
            with p.begin():
                claim = _claim(p)
                assert str(claim.user_id) == uid
            with p.begin():
                _progress(p, did, claim.claim_token, "r2_first_sweep")
                assert _purge(p, did, claim.claim_token) is True
        with sync_engine.connect() as check, check.begin():
            assert _count(check, "notifications", uid) == 0
            assert _sqlstate(check, "INSERT INTO notifications (id, user_id, type) "
                                    "VALUES (gen_random_uuid(), :u, 'late')", {"u": uid}) == "BLD20"
    finally:
        writer.close()
        with sync_engine.connect() as cleanup, cleanup.begin():
            cleanup.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})


def test_a_batch_write_waiting_across_the_claim_stores_no_link() -> None:
    """A webhook's queue UPDATE that started before the claim but waited on the row lock
    must see the account as purging when its trigger finally runs (a fresh read, not
    the statement's old snapshot)."""
    n = random.randint(1, 2**31 - 1)
    with sync_engine.connect() as setup, setup.begin():
        uid = _user(setup)
        _open_due(setup, uid)
        setup.execute(text("INSERT INTO skip_trace_queues (id, tracerfy_queue_id, user_id) "
                           "VALUES (gen_random_uuid(), :n, :u)"), {"n": n, "u": uid})
    holder = sync_engine.connect()
    try:
        h = holder.begin()
        holder.execute(text("UPDATE skip_trace_queues SET rows_uploaded = 1 "
                            "WHERE tracerfy_queue_id = :n"), {"n": n})
        outcome: dict = {}

        def webhook() -> None:
            try:
                with sync_engine.connect() as c, c.begin():
                    c.execute(text("SET LOCAL lock_timeout = '30s'"))
                    c.execute(text("UPDATE skip_trace_queues SET status = 'completed', "
                                   "download_url = 'https://vendor.test/late.csv' "
                                   "WHERE tracerfy_queue_id = :n"), {"n": n})
                outcome["done"] = True
            except Exception as exc:  # surfaced by the assert below
                outcome["error"] = exc

        t = threading.Thread(target=webhook)
        t.start()
        t.join(1)
        assert t.is_alive()  # its statement snapshot predates the claim
        with sync_engine.connect() as p, p.begin():
            assert str(_claim(p).user_id) == uid
        h.commit()
        t.join(30)
        assert outcome == {"done": True}, outcome
        with sync_engine.connect() as check:
            assert tuple(check.execute(text(
                "SELECT status, download_url FROM skip_trace_queues WHERE tracerfy_queue_id = :n"),
                {"n": n}).one()) == ("completed", None)
    finally:
        holder.close()
        with sync_engine.connect() as cleanup, cleanup.begin():
            cleanup.execute(text("DELETE FROM skip_trace_queues WHERE tracerfy_queue_id = :n"),
                            {"n": n})
            cleanup.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})


# ── Sign-in ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_purging_account_cannot_sign_in_or_refresh(
    client: AsyncClient, starter_user: User
) -> None:
    login = await client.post("/auth/login",
                              json={"email": starter_user.email, "password": "TestPass123!"})
    assert login.status_code == 200, login.text
    tokens = login.json()
    with sync_engine.connect() as c, c.begin():
        _open_due(c, str(starter_user.id))
        assert str(_claim(c).user_id) == str(starter_user.id)

    again = await client.post("/auth/login",
                              json={"email": starter_user.email, "password": "TestPass123!"})
    assert again.status_code == 401
    refreshed = await client.post("/auth/refresh",
                                  json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 401
    me = await client.get("/auth/me",
                          headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 401


# ── Lock-down ────────────────────────────────────────────────────────────────────

def test_purge_functions_and_fence_are_locked_down(conn) -> None:
    for fn in ("claim_account_deletion", "record_deletion_progress", "purge_account_data",
               "complete_account_deletion", "account_deletion_owner_state",
               "account_deletion_queue_tainted"):
        owner, definer, config, acl = conn.execute(text(
            "SELECT pg_get_userbyid(proowner), prosecdef, proconfig, proacl::text "
            "FROM pg_proc WHERE proname = :f"), {"f": fn}).one()
        assert (owner, definer) == ("bridgeleads_purge", True), fn
        assert config == ["search_path=pg_catalog, pg_temp"]
        assert acl is not None and not any(e.startswith("=") for e in acl.strip("{}").split(","))
    for fn in ("account_deletion_fence", "account_deletion_fence_job_logs",
               "account_deletion_fence_audit", "account_deletion_fence_queue"):
        definer, acl = conn.execute(text(
            "SELECT prosecdef, proacl::text FROM pg_proc WHERE proname = :f"), {"f": fn}).one()
        assert definer is False
        assert acl is not None and not any(e.startswith("=") for e in acl.strip("{}").split(","))
    enabled = dict(conn.execute(text(
        "SELECT c.relname, t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE t.tgname = 'zz_account_deletion_fence'")).all())
    assert enabled == dict.fromkeys((*_FENCED, "audit_events", "skip_trace_queues"), "A")
    assert conn.execute(text(
        "SELECT count(*) FROM pg_auth_members WHERE roleid = 'bridgeleads_purge'::regrole "
        "AND (set_option OR inherit_option OR member <> current_user::regrole)")).scalar() == 0


def test_supabase_api_roles_cannot_run_the_purge(conn) -> None:
    present = [r for (r,) in conn.execute(text(
        "SELECT rolname FROM pg_roles WHERE rolname IN "
        "('anon', 'authenticated', 'service_role')")).all()]
    if not present:
        pytest.skip("no Supabase API roles on this cluster")
    for role in present:
        for fn in ("claim_account_deletion(interval)",
                   "record_deletion_progress(uuid, uuid, text, text, text, text)",
                   "purge_account_data(uuid, uuid, text[], integer)",
                   "complete_account_deletion(uuid, uuid)",
                   "account_deletion_fence()", "account_deletion_fence_job_logs()",
                   "account_deletion_fence_audit()", "account_deletion_fence_queue()",
                   "account_deletion_queue_tainted(integer, uuid)",
                   "account_deletion_owner_state(uuid, uuid, boolean)"):
            assert conn.execute(text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                                {"r": role, "f": fn}).scalar() is False, (role, fn)
        _assert_cannot_delete_skeletons(conn, role)


def _assert_cannot_delete_skeletons(conn, role: str) -> None:
    """The billing ledgers CASCADE from these rows: deleting one would erase evidence."""
    for tbl in ("jobs", "results", "scraper_configs", "scraper_batches"):
        for priv in ("DELETE", "TRUNCATE"):
            assert conn.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                {"r": role, "t": tbl, "p": priv}).scalar() is False, (
                role, tbl, priv)


def _require_runtime_roles(conn) -> None:
    n = conn.execute(text("SELECT count(*) FROM pg_roles WHERE rolname IN "
                          "('bridgeleads_app', 'bridgeleads_system')")).scalar()
    if n != 2:
        pytest.skip("bridgeleads_app/system not provisioned")
    current = conn.execute(text("SELECT current_user")).scalar()
    sp = conn.begin_nested()
    try:
        for role in ("bridgeleads_app", "bridgeleads_system"):
            conn.execute(text(f'GRANT {role} TO "{current}"'))
        sp.commit()
    except DBAPIError:
        sp.rollback()
        pytest.skip(f"{current!r} cannot GRANT the runtime roles (needs an owner DSN)")


@pytest.mark.integration
def test_runtime_roles_hit_the_fence_and_only_the_worker_runs_the_purge(conn) -> None:
    _require_runtime_roles(conn)
    uid, bystander = _user(conn), _user(conn)
    ids = _seed(conn, uid)
    _open_due(conn, uid)
    _claim(conn)
    note = ("INSERT INTO notifications (id, user_id, type) "
            "VALUES (gen_random_uuid(), :u, 'job_done')")

    conn.execute(text("SET LOCAL ROLE bridgeleads_system"))
    assert _sqlstate(conn, note, {"u": uid}) == "BLD20"
    conn.execute(text(note), {"u": bystander})  # the invoker's own privileges suffice
    conn.execute(text("UPDATE results SET phone = '999', skip_trace_status = 'errored' "
                      "WHERE id = :r"), {"r": ids["r1"]})
    assert tuple(conn.execute(text("SELECT phone, skip_trace_status FROM results WHERE id = :r"),
                              {"r": ids["r1"]}).one()) == ("555", "errored")
    assert conn.execute(text(f"SELECT * FROM claim_account_deletion({_LEASE})")).all() == []
    conn.execute(text("RESET ROLE"))

    conn.execute(text("SET LOCAL ROLE bridgeleads_app"))
    conn.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": uid})
    assert _sqlstate(conn, "INSERT INTO user_sessions (id, user_id) VALUES ('x', :u)",
                     {"u": uid}) == "BLD20"
    assert _sqlstate(conn, f"SELECT * FROM claim_account_deletion({_LEASE})") == "42501"
    conn.execute(text("RESET ROLE"))
    for role in ("bridgeleads_app", "bridgeleads_system"):
        for priv in ("MEMBER", "USAGE", "SET"):
            assert conn.execute(text("SELECT pg_has_role(:r, 'bridgeleads_purge', :p)"),
                                {"r": role, "p": priv}).scalar() is False, (role, priv)
        _assert_cannot_delete_skeletons(conn, role)
