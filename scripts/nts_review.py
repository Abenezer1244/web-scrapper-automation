"""Operator review of OCR'd recorded Notices of Trustee's Sale (every county in
nts_crawler._RECORDER_PROFILES: Chelan, Cowlitz).

Run under `railway run --service worker python scripts/nts_review.py --operator-email <you> <command>`.
The operator must be an admin with MFA (the privacy operators' check, run as the
migration role and rolled back); every read and write then goes through the system role.

Commands
  list      notices awaiting review, and unreadable ones (retried daily until rejected):
            id, county, status, instrument, code, recorded, pages, flags (no names)
  show      one notice's OCR text, read fields, evidence, flags and the recorded image's URL.
            The text names the borrower: for the operator's eyes only.
  approve   write the notice confirmed against the image. Required: --doc, --sale-date
            (YYYY-MM-DD), --parcel (one of the recording's APNs), --grantor. Optional:
            --sale-time, --address, --amount with --amount-basis (principal|sum_owing),
            --trustee, --beneficiary. --supersedes <instrument> when the image says it amends
            an approved notice; --independent when it shares a deed of trust but is a new sale.
            A discontinuance on file that cites only this notice's loan must be answered:
            --discontinued-by <its instrument> or --not-discontinued.
            The parcel's current site address (in the notice's own county) is looked up
            and becomes the property
            address; --address (what the notice printed) is kept beside it. A printed
            address can be years out of date. --keep-notice-address uses the printed one.
            When the crawl stored no APN (Chelan indexes it some time after recording),
            the recorder page is read again and the parcel must be one it lists now.
  refresh-address  --doc of an approved notice: give it the parcel's current site
            address, keeping the printed one. Refused once it has gone out as a
            trustee-sale lead (its address is part of that lead's identity).
  resolve-nod  a discontinuance the crawl could not apply on its own (it cites a loan, or
            links nothing): --cancels <instrument> for each approved notice it ends
            (repeatable), or --no-effect.
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
    _profile,
    ReviewError,
    approve_recorder_document,
    chelan_live_recording_apns,
    lookup_parcel_situs,
    refresh_notice_address,
    reject_recorder_document,
    resolve_discontinuance,
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
    if (doc.county, doc.state) == ("chelan", "WA"):
        return f"{_CHELAN_RECORDER}/Image/DocumentPdfAllPages/{doc.transaction_item_id}"
    # Laserfiche (Cowlitz) has no stable all-pages PDF URL: its viewer page shows the image.
    return _profile(doc.county, doc.state)["url"](doc.transaction_item_id)


def _county_of(db, doc_id: str) -> tuple[str, str, dict, str]:
    """The staged document's county, state, fields and recorder item id, read and
    released before any network lookup (approval and refresh re-read the row under their
    own lock). Plain values, not an ORM object: a cached object could carry a stale
    'pending_review' into approval after another reviewer decided the row (Codex)."""
    row = db.execute(select(Doc.county, Doc.state, Doc.fields, Doc.transaction_item_id)
                     .where(Doc.id == doc_id)).one_or_none()
    db.rollback()
    if row is None:
        raise SystemExit("no staged document with that id")
    return row.county, row.state, dict(row.fields or {}), row.transaction_item_id


def cmd_list(db, op, args) -> None:
    rows = db.execute(select(Doc).where(Doc.status.in_(("pending_review", "failed")))
                      .order_by(Doc.record_date, Doc.instrument_number)).scalars().all()
    for d in rows:
        f = d.fields or {}
        print(f"{d.id}  {d.county:<8} {d.status:<14} {d.instrument_number}  {d.doc_code:<6} recorded {d.record_date}  "
              f"pages {d.page_count}  flags {len(f.get('flags') or [])}  "
              f"corroborated {bool(f.get('corroborated'))}")
    print(f"{len(rows)} awaiting review or unreadable (an unreadable one is retried daily "
          f"until rejected: reject --reason unreadable)")


def cmd_show(db, op, args) -> None:
    d = db.get(Doc, args.doc)
    if d is None:
        raise SystemExit("no staged document with that id")
    f = d.fields or {}
    print(f"{d.county}, {d.state}  {d.instrument_number} {d.doc_code} recorded {d.record_date} "
          f"status {d.status}")
    print(f"image: {_image_url(d)}")
    print(f"recording APNs: {f.get('recording_apns')}  deed of trust: {f.get('related_deeds_of_trust')}")
    if d.doc_code == "NOD":
        print(f"discontinuance: related {f.get('related')}  ended {f.get('cancelled')}  note: {d.decision_note}")
        return
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
    # Before approval takes its lock: network reads must not hold it (Codex).
    county, state, fields, item = _county_of(db, args.doc)
    live_apns = None
    if (county, state) == ("chelan", "WA") and fields.get("recording_apns") == []:
        live_apns = chelan_live_recording_apns(item)
        print(f"the crawl stored no APN; the recorder page lists now: {live_apns}")
    situs = lookup_parcel_situs(county, state, args.parcel) if not args.keep_notice_address else None
    notice = approve_recorder_document(db, args.doc, reviewer=op, values=values,
                                       supersedes=args.supersedes, independent=args.independent,
                                       discontinued_by=args.discontinued_by,
                                       not_discontinued=args.not_discontinued, situs=situs,
                                       live_apns=live_apns)
    db.commit()
    print(f"approved: notice {notice}")
    _print_address(args.address, situs)


def _print_address(printed: str | None, situs: str | None) -> None:
    if situs:
        print(f"property address: {situs} (the parcel's current site address)")
        if printed and " ".join(printed.split()).upper() != situs.upper():
            print(f"the notice printed: {printed} (kept as the address on the notice)")
    else:
        print(f"property address: {printed or '(none)'} (as printed; no site address found "
              f"for the parcel, or --keep-notice-address)")


def cmd_refresh_address(db, op, args) -> None:
    county, state, fields, _ = _county_of(db, args.doc)  # refresh takes the lock itself
    parcel = (fields.get("approved") or {}).get("parcel")
    if not parcel:
        raise SystemExit("this document has no approved parcel")
    situs = lookup_parcel_situs(county, state, parcel)
    old, new = refresh_notice_address(db, args.doc, reviewer=op, situs=situs)
    db.commit()
    print(f"address: {old} -> {new}")


def cmd_reject(db, op, args) -> None:
    reject_recorder_document(db, args.doc, reviewer=op, reason=args.reason)
    db.commit()
    print("rejected")


def cmd_resolve_nod(db, op, args) -> None:
    if bool(args.cancels) == bool(args.no_effect):
        raise SystemExit("give --cancels <instrument> (repeatable) or --no-effect")
    resolve_discontinuance(db, args.doc, reviewer=op, cancels=args.cancels or [])
    db.commit()
    print("resolved")


_COMMANDS = {"list": cmd_list, "show": cmd_show, "approve": cmd_approve, "reject": cmd_reject,
             "resolve-nod": cmd_resolve_nod, "refresh-address": cmd_refresh_address}


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
    for opt in ("--sale-time", "--address", "--amount", "--trustee", "--beneficiary", "--supersedes",
                "--discontinued-by"):
        p.add_argument(opt)
    p.add_argument("--not-discontinued", action="store_true")
    p.add_argument("--amount-basis", choices=("principal", "sum_owing"))
    p.add_argument("--independent", action="store_true")
    p.add_argument("--keep-notice-address", action="store_true",
                   help="use the printed address even when the parcel has a current site address")
    sub.add_parser("refresh-address").add_argument("--doc", required=True)
    p = sub.add_parser("resolve-nod")
    p.add_argument("--doc", required=True)
    p.add_argument("--cancels", action="append")
    p.add_argument("--no-effect", action="store_true")
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
