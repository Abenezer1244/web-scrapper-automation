"""Operator review of OCR'd recorded Notices of Trustee's Sale (Chelan, phase 4a-3a).

Run under `railway run --service worker python scripts/nts_review.py --operator-email <you> <command>`.
The operator must be an admin with MFA (the privacy operators' check, run as the
migration role and rolled back); every read and write then goes through the system role.

Commands
  list      notices awaiting review, and unreadable ones (retried daily until rejected):
            id, status, instrument, code, recorded, pages, flags (no names)
  show      one notice's OCR text, read fields, evidence, flags and the recorded image's URL.
            The text names the borrower: for the operator's eyes only.
  approve   write the notice confirmed against the image. Required: --doc, --sale-date
            (YYYY-MM-DD), --parcel (one of the recording's APNs), --grantor. Optional:
            --sale-time, --address, --amount with --amount-basis (principal|sum_owing),
            --trustee, --beneficiary. --supersedes <instrument> when the image says it amends
            an approved notice; --independent when it shares a deed of trust but is a new sale.
  reject    --doc and --reason (a fixed code, never free text that could name someone):
            nothing is written, the OCR text is dropped.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.db.models import NtsRecorderDocument as Doc  # noqa: E402
from src.utils.crypto import blind_index  # noqa: E402
from src.workers.nts_crawler import (  # noqa: E402
    _CHELAN_RECORDER,
    REJECT_REASONS,
    ReviewError,
    approve_recorder_document,
    reject_recorder_document,
)


@contextlib.contextmanager
def operator_session():
    """The migration role: the only role that may call privacy_operator_check."""
    raw = os.environ.get("DATABASE_URL_MIGRATE")
    if not raw:
        raise SystemExit("DATABASE_URL_MIGRATE not set: run under `railway run`.")
    engine = create_engine(re.sub(r"^postgresql\+\w+://", "postgresql://", raw), pool_pre_ping=True)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _operator(db, email: str) -> str:
    uid = db.execute(text("SELECT id FROM users WHERE email_hmac = :h"), {"h": blind_index(email)}).scalar()
    if uid is None:
        raise SystemExit("no account for --operator-email")
    db.execute(text("SELECT privacy_operator_check(CAST(:a AS uuid))"), {"a": str(uid)})
    db.rollback()
    return str(uid)


def _image_url(doc) -> str:
    return f"{_CHELAN_RECORDER}/Image/DocumentPdfAllPages/{doc.transaction_item_id}"


def cmd_list(db, op, args) -> None:
    rows = db.execute(select(Doc).where(Doc.status.in_(("pending_review", "failed")))
                      .order_by(Doc.record_date, Doc.instrument_number)).scalars().all()
    for d in rows:
        f = d.fields or {}
        print(f"{d.id}  {d.status:<14} {d.instrument_number}  {d.doc_code:<6} recorded {d.record_date}  "
              f"pages {d.page_count}  flags {len(f.get('flags') or [])}  "
              f"corroborated {bool(f.get('corroborated'))}")
    print(f"{len(rows)} awaiting review or unreadable (an unreadable one is retried daily "
          f"until rejected: reject --reason unreadable)")


def cmd_show(db, op, args) -> None:
    d = db.get(Doc, args.doc)
    if d is None:
        raise SystemExit("no staged document with that id")
    f = d.fields or {}
    print(f"{d.instrument_number} {d.doc_code} recorded {d.record_date} status {d.status}")
    print(f"image: {_image_url(d)}")
    print(f"recording APNs: {f.get('recording_apns')}  deed of trust: {f.get('related_deeds_of_trust')}")
    print("read fields: " + json.dumps(f.get("fields"), indent=1, default=str))
    print("flags: " + json.dumps(f.get("flags"), indent=1))
    print("evidence: " + json.dumps(f.get("evidence"), indent=1, default=str))
    print("----- OCR text -----")
    print(d.ocr_text or "(none kept)")


def _amount(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(raw.replace(",", "").lstrip("$"))
    except InvalidOperation as exc:
        raise SystemExit(f"--amount is not a number: {raw!r}") from exc


def cmd_approve(db, op, args) -> None:
    values = {
        "auction_date": date.fromisoformat(args.sale_date), "parcel": args.parcel, "grantor": args.grantor,
        "auction_time": args.sale_time, "property_address": args.address,
        "principal_owing": _amount(args.amount), "principal_owing_basis": args.amount_basis,
        "trustee": args.trustee, "beneficiary": args.beneficiary,
    }
    notice = approve_recorder_document(db, args.doc, reviewer=op, values=values,
                                       supersedes=args.supersedes, independent=args.independent)
    db.commit()
    print(f"approved: notice {notice}")


def cmd_reject(db, op, args) -> None:
    reject_recorder_document(db, args.doc, reviewer=op, reason=args.reason)
    db.commit()
    print("rejected")


_COMMANDS = {"list": cmd_list, "show": cmd_show, "approve": cmd_approve, "reject": cmd_reject}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--operator-email", required=True)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("show").add_argument("--doc", required=True)
    p = sub.add_parser("approve")
    p.add_argument("--doc", required=True)
    p.add_argument("--sale-date", required=True)
    p.add_argument("--parcel", required=True)
    p.add_argument("--grantor", required=True)
    for opt in ("--sale-time", "--address", "--amount", "--trustee", "--beneficiary", "--supersedes"):
        p.add_argument(opt)
    p.add_argument("--amount-basis", choices=("principal", "sum_owing"))
    p.add_argument("--independent", action="store_true")
    p = sub.add_parser("reject")
    p.add_argument("--doc", required=True)
    p.add_argument("--reason", required=True, choices=REJECT_REASONS)
    return ap


def main(argv=None, auth_factory=operator_session, session_factory=None) -> None:
    args = _parser().parse_args(argv)
    with auth_factory() as auth:
        op = _operator(auth, args.operator_email)
    if session_factory is None:
        from src.db.session import system_sync_session as session_factory
    with session_factory() as db:
        try:
            _COMMANDS[args.command](db, op, args)
        except ReviewError as exc:
            db.rollback()
            raise SystemExit(f"refused: {exc}") from exc


if __name__ == "__main__":
    main()
