"""erase_step(): discovery finds the subject's rows in every store and erases them
(Phase 6 P6b, src/privacy/erase.py, over migration 135's privacy_erase_batch).

Real database, one session rolled back at the end. The database refuses any rule state
but 'pending' until P7 (125's guard), so the session drops that CHECK in its own
transaction to record an ACTIVE deletion rule; the rollback restores it. The rule rows
carry real HMACs from the test keyring (tests/test_privacy_authorize.py). Synthetic
people only: SMITH JOHN on King parcel 1234567890.

Codex P6a contract, both required here:
  1. rows NOT yet flagged and rows flagged by ANOTHER rule (an opt-out for the same
     person) are both discovered and erased;
  2. every lookup key of a matched lead (several per lead) is collected and its cache
     entry deleted.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.db.session import system_sync_session
from src.privacy.authorize import RuleSnapshotError
from src.privacy.erase import STORES, erase_step, load_erasure_rule
from src.scrapers.enrichment.skip_trace import lookup_subject_key
from tests.test_privacy_authorize import _history, _keyring  # noqa: F401 -- autouse keyring

_PARCEL = "1234567890"
_INSERT_RULE = text(
    "INSERT INTO privacy_suppressions (id, rule_id, request_id, anchor_kind, anchor_hmac, "
    "subject_kind, subject_hmac, scope, key_version, normalization_version, state, "
    "created_at) VALUES (:id, :rule_id, :req, :anchor_kind, :anchor_hmac, :subject_kind, "
    ":subject_hmac, :scope, :key_version, :normalization_version, :state, :created_at)")


def _request(s, kind: str) -> str:
    return str(s.execute(text(
        "INSERT INTO privacy_requests (kind, channel, deadline_at) "
        "VALUES (:k, 'web', now() + interval '45 days') RETURNING id"), {"k": kind}).scalar())


def _active_rule(s, kind: str, **history) -> tuple[str, str]:
    """SMITH JOHN on the King parcel (or ``anchor=``), pending then active; returns
    (rule_id, active row)."""
    req = _request(s, kind)
    rows = _history(["pending", "active"], **history)
    for r in rows:
        s.execute(_INSERT_RULE, {**r, "req": req})
    return rows[1]["rule_id"], rows[1]["id"]


@pytest.fixture
def s():
    with system_sync_session() as session:
        session.execute(text("ALTER TABLE privacy_suppressions "
                             "DROP CONSTRAINT ck_privacy_suppressions_activation_guard"))
        try:
            yield session
        finally:
            session.rollback()


def _owner(s) -> tuple[str, str]:
    uid = str(uuid.uuid4())
    s.add(User(id=uid, email=f"{uid}@example.test", password_hash="x",
               email_hmac=uuid.uuid4().hex))
    s.flush()
    cfg = str(uuid.uuid4())
    s.add(ScraperConfig(id=cfg, user_id=uid, name="Erase", county="king", state="WA",
                        record_type="probate", fields=[], enrichment=[],
                        schedule={"frequency": "manual"}, deliver={"emails": []}))
    s.flush()
    job = str(uuid.uuid4())
    s.add(Job(id=job, user_id=uid, scraper_config_id=cfg, status="done", trigger="manual",
              record_count=0, billed_count=0, retry_count=0))
    s.flush()
    return uid, job


def _result(s, uid: str, job: str, name: str, parcel: str | None, **extra) -> str:
    rid = str(uuid.uuid4())
    s.add(Result(id=rid, job_id=job, user_id=uid, party_name=name, parcel_id=parcel,
                 property_address="1 TEST ST", dedup_hash=uuid.uuid4().hex,
                 is_duplicate=False, enrichment_data={}, phone="2065550100", **extra))
    s.flush()
    return rid


def _run_pass(s, rule) -> dict[str, int]:
    totals: dict[str, int] = {}
    cursor: dict = {}
    seen = []
    while True:
        step = erase_step(s, rule, cursor)
        seen.append(cursor.get("store", STORES[0]))
        for k, v in step.counts.items():
            totals[k] = totals.get(k, 0) + v
        if step.done:
            break
        cursor = step.cursor
    assert set(seen) == set(STORES)
    return totals


def test_a_pass_erases_the_subject_everywhere_and_nothing_else(s):
    rule_id, row = _active_rule(s, "delete")
    _, opt_out_row = _active_rule(s, "opt_out")
    rule = load_erasure_rule(s, rule_id, row)
    uid, job = _owner(s)

    unflagged = _result(s, uid, job, "SMITH JOHN", _PARCEL, skip_trace_subject_hash="e" * 64)
    # Flagged first by the person's opt-out rule: the flag names THAT rule.
    opted_out = _result(s, uid, job, "SMITH JOHN", _PARCEL)
    s.execute(text("UPDATE results SET privacy_suppressed_at = now(), "
                   "privacy_suppression_id = :r WHERE id = :i"),
              {"r": opt_out_row, "i": opted_out})
    review = _result(s, uid, job, "SMITH JOHN", None)       # name without the parcel
    neighbour = _result(s, uid, job, "DOE JANE", _PARCEL)

    # Two lookups for one lead (normal + advanced): both cache entries must go.
    lookup_keys = []
    for trace_type, status in (("normal", "completed"), ("advanced", "completed")):
        s.execute(text(
            "INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id, "
            "property_address, city, state, first_name, last_name, trace_type, status) "
            "VALUES (gen_random_uuid(), :j, :r, :u, '1 TEST ST', 'SEATTLE', 'WA', 'JOHN', "
            "'SMITH', :t, :st)"), {"j": job, "r": unflagged, "u": uid, "t": trace_type,
                                   "st": status})
        lookup_keys.append(lookup_subject_key(uid, "1 TEST ST", "SEATTLE", "WA", trace_type,
                                              "JOHN", "SMITH"))
    for key in (*lookup_keys, "e" * 64, "f" * 64):
        s.execute(text("INSERT INTO skip_trace_cache (address_hash) VALUES (:k)"), {"k": key})

    s.execute(text(
        "INSERT INTO king_parcel_owners (parcel_id, outcome, owner_name, observed_at, source) "
        "VALUES (:p, 'found', 'SMITH JOHN', now(), 'erealproperty'), "
        "('0000000001', 'found', 'SMITH JOHN', now(), 'erealproperty')"), {"p": _PARCEL})
    nts = str(uuid.uuid4())
    s.execute(text(
        "INSERT INTO nts_notices (id, source, ts_number, grantor, parcel, county, state) "
        "VALUES (:i, 'test', :t, 'JOHN SMITH', :p, 'king', 'WA')"),
        {"i": nts, "t": nts[:12], "p": _PARCEL})
    county = str(uuid.uuid4())
    s.execute(text(
        "INSERT INTO county_records (id, county, state, record_hash, party_name, parcel_id) "
        "VALUES (:i, 'king', 'WA', :h, 'SMITH JOHN', :p)"),
        {"i": county, "h": uuid.uuid4().hex, "p": _PARCEL})

    totals = _run_pass(s, rule)
    assert totals["results"] == 2
    assert (totals["king"], totals["nts"], totals["county"]) == (1, 1, 1)

    erased = dict(s.execute(text(
        "SELECT id::text, party_name IS NULL AND phone IS NULL AND privacy_erased_at "
        "IS NOT NULL FROM results WHERE id IN (:a, :b, :c, :d)"),
        {"a": unflagged, "b": opted_out, "c": review, "d": neighbour}).all())
    assert erased == {unflagged: True, opted_out: True, review: False, neighbour: False}
    # The opt-out flag is kept: erasure adds, it never rewrites another rule's flag.
    assert str(s.execute(text("SELECT privacy_suppression_id FROM results WHERE id = :i"),
                         {"i": opted_out}).scalar()) == opt_out_row
    left = {k for (k,) in s.execute(text(
        "SELECT address_hash FROM skip_trace_cache WHERE address_hash = ANY(:k)"),
        {"k": [*lookup_keys, "e" * 64, "f" * 64]}).all()}
    assert left == {"f" * 64}
    assert s.execute(text("SELECT outcome FROM king_parcel_owners WHERE parcel_id = :p"),
                     {"p": _PARCEL}).scalar() == "privacy_withheld"
    assert s.execute(text("SELECT outcome FROM king_parcel_owners "
                          "WHERE parcel_id = '0000000001'")).scalar() == "found"
    assert s.execute(text("SELECT grantor FROM nts_notices WHERE id = :i"),
                     {"i": nts}).scalar() is None
    assert s.execute(text("SELECT count(*) FROM county_records WHERE id = :i"),
                     {"i": county}).scalar() == 0

    # A second pass over clean stores erases nothing.
    assert set(_run_pass(s, rule).values()) <= {0}


def test_an_opt_out_rule_cannot_be_loaded_for_erasure_by_the_database(s):
    rule_id, row = _active_rule(s, "opt_out")
    rule = load_erasure_rule(s, rule_id, row)      # the snapshot cannot tell the kind...
    uid, job = _owner(s)
    _result(s, uid, job, "SMITH JOHN", _PARCEL)
    with pytest.raises(Exception, match="not an active deletion rule"):  # ...the DB can
        _run_pass(s, rule)


def test_a_rule_row_that_is_not_active_is_refused(s):
    rule_id, _ = _active_rule(s, "delete")
    with pytest.raises(RuleSnapshotError):
        load_erasure_rule(s, rule_id, str(uuid.uuid4()))


def test_a_cursor_resumes_where_the_last_step_stopped(s, monkeypatch):
    import src.privacy.erase as erase
    rule_id, row = _active_rule(s, "delete")
    rule = load_erasure_rule(s, rule_id, row)
    uid, job = _owner(s)
    ids = sorted(_result(s, uid, job, "SMITH JOHN", _PARCEL) for _ in range(3))
    monkeypatch.setattr(erase, "BATCH", 1)
    # Start just before the second of our rows: the first must be left alone.
    before_second = s.execute(text("SELECT id::text FROM results WHERE id < :i "
                              "ORDER BY id DESC LIMIT 1"),
                              {"i": ids[1]}).scalar()
    step = erase_step(s, rule, {"store": "results", "after": before_second})
    assert step.cursor == {"store": "results", "after": ids[1]}
    assert step.counts.get("results") == 1
    names = dict(s.execute(text("SELECT id::text, party_name FROM results "
                                "WHERE id = ANY(CAST(:i AS uuid[]))"), {"i": ids}).all())
    assert names[ids[0]] == "SMITH JOHN" and names[ids[1]] is None
    json.dumps(step.cursor)  # storable as jsonb


def _sweep(s, rule, store: str) -> dict[str, int]:
    """Walk one store from its start to its end, one step at a time."""
    totals: dict[str, int] = {}
    cursor: dict = {"store": store}
    while cursor.get("store") == store:
        step = erase_step(s, rule, cursor)
        for k, v in step.counts.items():
            totals[k] = totals.get(k, 0) + v
        if step.done:
            break
        cursor = step.cursor
    return totals


@pytest.mark.parametrize("batch", [1, 2])
def test_every_shared_store_resumes_across_pages(s, monkeypatch, batch):
    """BATCH 1 and 2 over three matches each: exact-full pages, a short last page, and
    the hand-over to the next store all happen inside each store."""
    import src.privacy.erase as erase
    rule_id, row = _active_rule(s, "delete")
    rule = load_erasure_rule(s, rule_id, row)
    monkeypatch.setattr(erase, "BATCH", batch)
    # Three of each, all on the subject's parcel, so every page boundary is crossed.
    for i in range(3):
        s.execute(text(
            "INSERT INTO king_parcel_owners (parcel_id, outcome, owner_name, observed_at, "
            "source) VALUES (:p, 'found', 'SMITH JOHN', now(), 'erealproperty')"),
            {"p": _PARCEL if i == 0 else f"000000000{i}"})
        nts = str(uuid.uuid4())
        s.execute(text(
            "INSERT INTO nts_notices (id, source, ts_number, grantor, parcel, county, state) "
            "VALUES (:i, 'test', :t, 'JOHN SMITH', :p, 'king', 'WA')"),
            {"i": nts, "t": nts[:12], "p": _PARCEL})
        s.execute(text(
            "INSERT INTO county_records (id, county, state, record_hash, party_name, "
            "parcel_id) VALUES (gen_random_uuid(), 'king', 'WA', :h, 'SMITH JOHN', :p)"),
            {"h": uuid.uuid4().hex, "p": _PARCEL})
    # King keys on the normalised parcel: only the exact parcel is the subject's.
    assert _sweep(s, rule, "king").get("king") == 1
    assert _sweep(s, rule, "nts").get("nts") == 3
    assert _sweep(s, rule, "county").get("county") == 3
    assert s.execute(text("SELECT count(*) FROM nts_notices WHERE grantor = 'JOHN SMITH'")
                     ).scalar() == 0


def test_a_phone_anchor_erases_only_the_named_subject(s):
    rule_id, row = _active_rule(s, "delete", anchor=("phone", "2065550100"))
    rule = load_erasure_rule(s, rule_id, row)
    uid, job = _owner(s)
    # No parcel at all: the phone (stored encrypted, read through the ORM) is the anchor.
    subject = _result(s, uid, job, "SMITH JOHN", None)
    stranger = _result(s, uid, job, "DOE JANE", None)       # same phone, other person
    assert _run_pass(s, rule).get("results") == 1
    names = dict(s.execute(text("SELECT id::text, party_name FROM results "
                                "WHERE id = ANY(CAST(:i AS uuid[]))"),
                           {"i": [subject, stranger]}).all())
    assert names == {subject: None, stranger: "DOE JANE"}


def test_an_account_being_purged_is_left_to_its_own_purge(s):
    rule_id, row = _active_rule(s, "delete")
    rule = load_erasure_rule(s, rule_id, row)
    uid, job = _owner(s)
    rid = _result(s, uid, job, "SMITH JOHN", _PARCEL)
    s.execute(text("SET LOCAL ROLE bridgeleads_purge"))
    for state in ("pending", "purging"):
        s.execute(text("UPDATE users SET deletion_state = :s WHERE id = :u"),
                  {"s": state, "u": uid})
    s.execute(text("RESET ROLE"))
    # Found, but not erased or counted: 113's purge blanks it and owns its lifecycle.
    assert _run_pass(s, rule).get("results", 0) == 0
    assert s.execute(text("SELECT privacy_erased_at FROM results WHERE id = :i"),
                     {"i": rid}).scalar() is None


def test_a_withheld_notice_can_never_keep_a_grantor(s):
    """Why the NTS scan only reads notices with a grantor (132's CHECK)."""
    _, row = _active_rule(s, "opt_out")
    nts = str(uuid.uuid4())
    with pytest.raises(Exception, match="ck_nts_notices_privacy_withheld"):
        with s.begin_nested():
            s.execute(text(
                "INSERT INTO nts_notices (id, source, ts_number, grantor, is_active, "
                "privacy_suppression_id) VALUES (:i, 'test', :t, 'JOHN SMITH', false, :r)"),
                {"i": nts, "t": nts[:12], "r": row})
