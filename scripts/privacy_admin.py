"""Operator CLI for privacy requests and suppression rules (Phase 6 P7, design section 9).

Run under `railway run --service worker python scripts/privacy_admin.py <command> ...`.
It writes through migration 136's operator functions as the migration role
(DATABASE_URL_MIGRATE); every function checks that --operator-email is an admin with
MFA enabled and records that user. Only `show` prints personal data (the requester's own
contact and request, which the operator needs to act); everything else prints request,
rule and result ids and counts.

Commands
  record      a request received by email or post (kind, channel, deadline, contact)
  event       an audit event: verification_sent, verified, extended, in_review,
              decided, closed, withdrawn
  add-rule    a PENDING rule: an anchor (parcel, phone or email) plus the subject's
              name, keyed under every live key version. Nothing is enforced yet.
  candidates  dry run before activation: how many stored leads the rule would withhold
              or send to review (counts only)
  activate    the rule goes live. Refuses unless both activation keys are turned
              (src/privacy/activation.py). Commits only if the rule snapshot still loads,
              then flags the matching leads at once (the beat sweep is the backstop) and
              withdraws their queued work: unsent lookups and dialer sends (design
              section 6). A deletion request also queues the P6 erasure.
  held        the leads an active rule holds, each with WITHHELD or REVIEW
  release     an operator decides a REVIEW lead is not the rule's subject
  lift        an opt-out ends: the rule stops and its leads are re-decided. A deletion
              rule is never lifted (erased data cannot come back).
  status      a request's events and rules
  inbox       open requests, soonest deadline first (ids, kinds, dates, last event)
  show        one request's decrypted contact and details, for the operator only

The database is the authority for every lifecycle rule (migration 136); this script only
builds keys from plain input and keeps the derived state (the results flag) consistent
in the same transaction.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.db.models import Job, Result, ScraperConfig  # noqa: E402
from src.privacy import keys  # noqa: E402
from src.privacy.activation import assert_activation_allowed  # noqa: E402
from src.privacy.authorize import (  # noqa: E402
    ALLOWED,
    REVIEW,
    ROW_COLUMNS,
    WITHHELD,
    authorize,
    build_snapshot,
    load_review_releases,
    load_rule_rows,
    load_rule_snapshot,
)
from src.privacy.enforce import enforce_rows  # noqa: E402
from src.privacy.subjects import (  # noqa: E402
    NORMALIZATION_VERSION,
    email_key_value,
    entity_key_value,
    person_key_value,
    phone_key_value,
)
from src.utils.crypto import blind_index, decrypt_field, encrypt_field  # noqa: E402

_SCAN_BATCH = 500


@contextlib.contextmanager
def operator_session():
    """A session as the migration role, the only role that may call 136's functions."""
    raw = os.environ.get("DATABASE_URL_MIGRATE")
    if not raw:
        raise SystemExit("DATABASE_URL_MIGRATE not set: run under `railway run`.")
    engine = create_engine(re.sub(r"^postgresql\+\w+://", "postgresql://", raw),
                           pool_pre_ping=True)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _operator(db, email: str) -> str:
    uid = db.execute(text("SELECT id FROM users WHERE email_hmac = :h"),
                     {"h": blind_index(email)}).scalar()
    if uid is None:
        raise SystemExit("no account for --operator-email")
    return str(uid)  # the database checks admin + MFA on every call


def _anchor(args) -> tuple[str, str]:
    if args.parcel:
        value = keys.property_anchor_value(args.parcel, args.county, args.state)
        kind = "property"
    elif args.phone:
        value, kind = phone_key_value(args.phone), "phone"
    else:
        value, kind = email_key_value(args.email), "email"
    if not value:
        raise SystemExit(f"the {kind} anchor could not be read (parcel needs --county and "
                         "--state; a phone needs 10 US digits)")
    return kind, value


def _subject(args) -> tuple[str, str]:
    if args.entity:
        value, kind = entity_key_value(args.entity), "entity"
    else:
        value, kind = person_key_value(args.surname, args.given), "person"
    if not value:
        raise SystemExit("the subject needs --surname and --given, or --entity")
    return kind, value


def _rule_rows(db, rule: str) -> list:
    return [r for r in load_rule_rows(db) if str(r["rule_id"]) == rule]


def _scan(db, decide) -> None:
    """Every stored lead with its config context, in id batches; decide(mapping)."""
    after = "00000000-0000-0000-0000-000000000000"
    cols = (Result.id, *(getattr(Result, c) for c in ROW_COLUMNS),
            ScraperConfig.record_type, ScraperConfig.county, ScraperConfig.state)
    while True:
        page = db.execute(
            select(*cols).join(Job, Job.id == Result.job_id)
            .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
            .where(Result.id > after).order_by(Result.id).limit(_SCAN_BATCH)
        ).mappings().all()
        if not page:
            return
        for m in page:
            decide(m)
        after = str(page[-1]["id"])


def _decide(snapshot, m):
    return authorize(snapshot, m, record_type=m["record_type"], county=m["county"],
                     state=m["state"])


def _sweep_now() -> None:
    """Flag the new rule's leads now rather than at the next beat tick."""
    from src.workers.privacy_enforcer import enforce_privacy_rules

    while True:
        stats = enforce_privacy_rules()
        print(f"  sweep: checked {stats['checked']}, flagged {stats['flagged']}")
        if not stats["batches"]:
            return


def _withdraw_queued(db, rule: str) -> tuple[int, int]:
    """Cancel the queued work of the leads the rule now holds, as design section 6 asks
    of an activation. Send-time checks (P4) stay the final guard for anything in flight.
      lookups  the dispatcher's own cancel sweep: every 'queued' lookup of a withheld
               lead (and the reasons it always applied), never one already at Tracerfy.
      dialer   unsent rows ('pending'/'failed') of the rule's leads, skipped with the
               reason the outbox itself writes at send time."""
    from src.workers.skip_trace_dispatcher import _cancel_undeliverable_queued

    lookups = _cancel_undeliverable_queued(db)
    dialer = db.execute(text(
        "UPDATE dialer_deliveries d SET status = 'skipped', last_error = 'privacy withheld' "
        "FROM results r JOIN privacy_suppressions s ON s.id = r.privacy_suppression_id "
        "WHERE d.result_id = r.id AND d.user_id = r.user_id AND s.rule_id = :rule "
        "  AND d.status IN ('pending', 'failed')"), {"rule": rule}).rowcount
    db.commit()
    return lookups, dialer


def _commit_if_rules_load(db) -> None:
    """The database cannot see key versions or the normalisation version: a change the
    rule loader would reject must never commit, or every send path would stop."""
    load_rule_snapshot(db)
    db.commit()


def cmd_record(db, op, args) -> None:
    deadline = datetime.now(UTC) + timedelta(days=args.deadline_days)
    rid = db.execute(text(
        "SELECT privacy_record_request(:k, :c, :d, :h, :contact, :details, CAST(:a AS uuid))"),
        {"k": args.kind, "c": args.channel, "d": deadline,
         "h": blind_index(args.requester_email) if args.requester_email else None,
         "contact": encrypt_field(args.contact),
         "details": encrypt_field(args.details) if args.details else None, "a": op}).scalar()
    db.commit()
    print(f"request {rid} recorded, deadline {deadline:%Y-%m-%d}")


def cmd_event(db, op, args) -> None:
    deadline = (datetime.now(UTC) + timedelta(days=args.extend_days)
                if args.extend_days else None)
    seq = db.execute(text(
        "SELECT privacy_record_event(CAST(:r AS uuid), :t, :v, :d, :dl, CAST(:a AS uuid))"),
        {"r": args.request, "t": args.type, "v": args.verification_level,
         "d": args.decision_code, "dl": deadline, "a": op}).scalar()
    db.commit()
    print(f"event {seq} ({args.type}) recorded on request {args.request}")


def cmd_add_rule(db, op, args) -> None:
    anchor_kind, anchor_value = _anchor(args)
    subject_kind, subject_value = _subject(args)
    anchors = keys.privacy_hmacs(anchor_kind, anchor_value)
    subjects = keys.privacy_hmacs(subject_kind, subject_value)
    rule = None
    for version in sorted(anchors):  # one row per live key version, one rule id
        rule = db.execute(text(
            "SELECT privacy_add_rule(CAST(:r AS uuid), :ak, :ah, :sk, :sh, :kv, :nv, "
            "CAST(:a AS uuid), CAST(:rule AS uuid))"),
            {"r": args.request, "ak": anchor_kind, "ah": anchors[version], "sk": subject_kind,
             "sh": subjects[version], "kv": version, "nv": NORMALIZATION_VERSION, "a": op,
             "rule": rule}).scalar()
    db.commit()
    print(f"rule {rule} added as pending ({anchor_kind} anchor, {subject_kind}, "
          f"{len(anchors)} key version(s))")


def cmd_candidates(db, op, args) -> None:
    rows = _rule_rows(db, args.rule)
    if not rows or any(r["state"] != "pending" for r in rows):
        raise SystemExit("candidates is a dry run for a pending rule")
    # The rule as if active, next to every rule already live.
    later = max(r["created_at"] for r in rows) + timedelta(seconds=1)
    as_active = [{**r, "id": f"{r['id']}", "state": "active", "created_at": later}
                 for r in rows]
    snapshot = build_snapshot(load_rule_rows(db) + as_active, load_review_releases(db))
    mine = {r["id"] for r in as_active}
    counts = {WITHHELD: 0, REVIEW: 0}

    def decide(m):
        d = _decide(snapshot, m)
        if d.outcome != ALLOWED and mine & set(d.suppression_ids):
            counts[d.outcome] += 1

    _scan(db, decide)
    db.rollback()
    print(f"rule {args.rule} would withhold {counts[WITHHELD]} lead(s) and send "
          f"{counts[REVIEW]} to review")


def cmd_activate(db, op, args) -> None:
    assert_activation_allowed()
    n = db.execute(text("SELECT privacy_activate_rule(CAST(:r AS uuid), CAST(:a AS uuid))"),
                   {"r": args.rule, "a": op}).scalar()
    _commit_if_rules_load(db)
    print(f"rule {args.rule} active ({n} key version row(s)); flagging its leads")
    _sweep_now()
    lookups, dialer = _withdraw_queued(db, args.rule)
    print(f"  withdrawn: {lookups} queued lookup(s), {dialer} dialer send(s)")


def cmd_held(db, op, args) -> None:
    active = {r["id"] for r in _rule_rows(db, args.rule) if r["state"] == "active"}
    if not active:
        raise SystemExit("the rule is not active")
    snapshot = load_rule_snapshot(db)
    ids = list(db.execute(select(Result.id).where(
        Result.privacy_suppression_id.in_(active))).scalars())
    for chunk in (ids[i:i + _SCAN_BATCH] for i in range(0, len(ids), _SCAN_BATCH)):
        for m in db.execute(
            select(Result.id, *(getattr(Result, c) for c in ROW_COLUMNS),
                   ScraperConfig.record_type, ScraperConfig.county, ScraperConfig.state)
            .join(Job, Job.id == Result.job_id)
            .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
            .where(Result.id.in_(chunk)).order_by(Result.id)
        ).mappings():
            print(f"  {m['id']}  {_decide(snapshot, m).outcome}")
    db.rollback()
    print(f"{len(ids)} lead(s) held by rule {args.rule}")


def cmd_release(db, op, args) -> None:
    snapshot = load_rule_snapshot(db)
    m = db.execute(
        select(Result.id, *(getattr(Result, c) for c in ROW_COLUMNS),
               ScraperConfig.record_type, ScraperConfig.county, ScraperConfig.state)
        .join(Job, Job.id == Result.job_id)
        .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
        .where(Result.id == args.result)).mappings().first()
    if m is None or _decide(snapshot, m).outcome != REVIEW:
        raise SystemExit("only a lead under REVIEW can be released (an exact match is the "
                         "rule's subject; lift the rule instead)")
    db.execute(text("SELECT privacy_release_review(CAST(:res AS uuid), CAST(:r AS uuid), "
                    "CAST(:a AS uuid))"), {"res": args.result, "r": args.rule, "a": op})
    stats = enforce_rows(db, load_rule_snapshot(db), [args.result])
    _commit_if_rules_load(db)
    print(f"lead {args.result} released from rule {args.rule}"
          + (" but still withheld by another rule" if stats.flagged else ""))


def cmd_lift(db, op, args) -> None:
    ids = [str(i) for i in db.execute(text(
        "SELECT * FROM privacy_lift_rule(CAST(:r AS uuid), CAST(:a AS uuid))"),
        {"r": args.rule, "a": op}).scalars()]
    stats = enforce_rows(db, load_rule_snapshot(db), ids)
    _commit_if_rules_load(db)
    print(f"rule {args.rule} lifted: {len(ids)} lead(s) re-decided, {stats.flagged} still "
          "withheld by another rule")


def cmd_status(db, op, args) -> None:
    for e in db.execute(text(
            "SELECT seq, event_type, verification_level, decision_code, occurred_at "
            "FROM privacy_request_events WHERE request_id = :r ORDER BY seq"),
            {"r": args.request}):
        print(f"  event {e.seq}: {e.event_type} {e.verification_level or ''}"
              f"{e.decision_code or ''} at {e.occurred_at:%Y-%m-%d %H:%M}")
    for r in db.execute(text(
            "SELECT rule_id, anchor_kind, subject_kind, key_version, state, created_at "
            "FROM privacy_suppressions WHERE request_id = :r ORDER BY rule_id, created_at"),
            {"r": args.request}):
        print(f"  rule {r.rule_id}: {r.anchor_kind}/{r.subject_kind} {r.key_version} "
              f"{r.state} at {r.created_at:%Y-%m-%d %H:%M}")
    db.rollback()


def cmd_inbox(db, op, args) -> None:
    rows = db.execute(text(
        "SELECT r.id, r.kind, r.channel, r.received_at, coalesce("
        "  (SELECT max(e.deadline_at) FROM privacy_request_events e"
        "    WHERE e.request_id = r.id AND e.event_type = 'extended'), r.deadline_at) AS due,"
        "  (SELECT e.event_type FROM privacy_request_events e WHERE e.request_id = r.id"
        "    ORDER BY e.seq DESC LIMIT 1) AS last "
        "FROM privacy_requests r WHERE NOT EXISTS (SELECT 1 FROM privacy_request_events e"
        "  WHERE e.request_id = r.id AND e.event_type IN ('decided', 'closed', 'withdrawn')) "
        "ORDER BY due, r.id")).all()
    for r in rows:
        print(f"  {r.id}  {r.kind:8} {r.channel:6} received {r.received_at:%Y-%m-%d}  "
              f"due {r.due:%Y-%m-%d}  last: {r.last}")
    db.rollback()
    print(f"{len(rows)} open request(s)")


def cmd_show(db, op, args) -> None:
    row = db.execute(text(
        "SELECT contact_encrypted, details_encrypted FROM privacy_request_payloads "
        "WHERE request_id = :r"), {"r": args.request}).first()
    db.rollback()
    if row is None:
        raise SystemExit("no such request, or its payload was deleted (retention)")
    print("contact: " + decrypt_field(row.contact_encrypted))
    if row.details_encrypted:
        print("details: " + decrypt_field(row.details_encrypted))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--operator-email", required=True)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record")
    p.add_argument("--kind", required=True, choices=("opt_out", "delete", "access", "correct"))
    p.add_argument("--channel", required=True, choices=("email", "postal", "web"))
    p.add_argument("--deadline-days", type=int, default=45)
    p.add_argument("--contact", required=True, help="how to reach the requester (encrypted)")
    p.add_argument("--requester-email")
    p.add_argument("--details", help="free text from the request (encrypted)")

    p = sub.add_parser("event")
    p.add_argument("--request", required=True)
    p.add_argument("--type", required=True, choices=(
        "verification_sent", "verified", "extended", "in_review", "decided", "closed",
        "withdrawn"))
    p.add_argument("--verification-level")
    p.add_argument("--decision-code")
    p.add_argument("--extend-days", type=int)

    p = sub.add_parser("add-rule")
    p.add_argument("--request", required=True)
    anchor = p.add_mutually_exclusive_group(required=True)
    anchor.add_argument("--parcel")
    anchor.add_argument("--phone")
    anchor.add_argument("--email")
    p.add_argument("--county", help="with --parcel, e.g. king")
    p.add_argument("--state", help="with --parcel, e.g. WA")
    p.add_argument("--surname")
    p.add_argument("--given")
    p.add_argument("--entity", help="an estate or trust named as the subject (D4)")

    for name in ("candidates", "activate", "held", "lift"):
        sub.add_parser(name).add_argument("--rule", required=True)
    p = sub.add_parser("release")
    p.add_argument("--rule", required=True)
    p.add_argument("--result", required=True)
    sub.add_parser("status").add_argument("--request", required=True)
    sub.add_parser("inbox")
    sub.add_parser("show").add_argument("--request", required=True)
    return ap


_COMMANDS = {"record": cmd_record, "event": cmd_event, "add-rule": cmd_add_rule,
             "candidates": cmd_candidates, "activate": cmd_activate, "held": cmd_held,
             "release": cmd_release, "lift": cmd_lift, "status": cmd_status,
             "inbox": cmd_inbox, "show": cmd_show}


def main(argv=None, session_factory=operator_session) -> None:
    args = _parser().parse_args(argv)
    with session_factory() as db:
        op = _operator(db, args.operator_email)
        _COMMANDS[args.command](db, op, args)


if __name__ == "__main__":
    main()
