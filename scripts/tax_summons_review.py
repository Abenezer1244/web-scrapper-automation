"""Operator review of a county Treasurer's tax-foreclosure Notice and Summons (Douglas WA).

Run under `railway run --service worker python scripts/tax_summons_review.py --operator-email <you> <command>`.
The operator must be an admin with MFA (the privacy operators' check, run as the
migration role and rolled back); every read and write then goes through the system role.

The county re-uploads the summons PDF as parcels redeem, stamping those rows REDEEMED.
Each distinct copy is a version; nothing from a version reaches customers until it is
published here, and a newer copy holds the county's tax leads until it is published too.

Steps for a new copy: download the PDF (show prints its URL and digest) and work only
from that file: manifest and publish hash it and refuse a copy whose bytes differ (the
county can replace the file at the same URL). Then
  manifest --version V --pdf FILE --case 26-2-00337-09 --pages 6:3,7:3,8:4,...
           the case number and how many rows each Exhibit A page holds, read off the PDF.
           A re-uploaded summons keeps its case; a later year's summons needs --new-case.
           Starts a new, empty revision: rows loaded before it no longer count.
  draft    --version V --out FILE
           a JSON file to edit, pre-filled with the last published rows, each
           "confirmed": false. Check every row against the PDF, fix it, set "confirmed":
           true. The file holds names: delete it once loaded.
           Row keys: page, row, parcel (11 digits), vested_owner, taxpayer, property_address,
           mailing_address, amount ("Taxes and Interest Due"), tax_years [list], eligibility
           (eligible | redeemed | unreadable | excluded), reason (excluded rows only:
           duplicate | not_a_parcel | privacy_request | entered_in_error), reinstate_reason
           (an eligible parcel the county stamped redeemed earlier in the case:
           redemption_reversed | entered_in_error), confirmed.
  load     --version V --file FILE
           stores the rows as a new revision: every manifest row exactly once. Prints the
           revision and counts only.
  show     --version V
           the current revision's rows, names included (for your eyes only).
  publish  --version V --revision N --pdf FILE
           approves revision N (the one you checked with show). Refused if the rows have
           changed since, the file is not this copy, or a newer copy has been seen.
  list     every version: id, county, status, case, revision, digest, rows by eligibility.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.db.models import TaxSummonsRow as Row  # noqa: E402
from src.db.models import TaxSummonsVersion as Version  # noqa: E402
from src.utils.crypto import blind_index  # noqa: E402
from src.workers.tax_summons import (  # noqa: E402
    TaxReviewError,
    counts_by_eligibility,
    draft_rows,
    load_rows,
    publish,
    set_manifest,
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


def _counts(counts: dict[str, int]) -> str:
    return " ".join(f"{k} {n}" for k, n in counts.items())


def _version(db, version_id: str) -> Version:
    try:
        v = db.get(Version, str(uuid.UUID(version_id)))
    except ValueError:
        v = None
    if v is None:
        raise SystemExit("no summons version with that id")
    return v


def cmd_list(db, op, args) -> None:
    versions = db.execute(select(Version).order_by(Version.county, Version.seen_order)).scalars().all()
    for v in versions:
        print(f"{v.id}  {_shown(v.county):<8} {v.status:<10} case {v.case_number or '-'}  rev {v.revision}  "
              f"sha {v.sha256[:12]}  seen {v.first_seen_at:%Y-%m-%d}  {_counts(counts_by_eligibility(db, v.id))}")
    print(f"{len(versions)} version(s)")


def _shown(value) -> str:
    """Text as the operator should read it: anything non-printable (a terminal escape, a
    bidi override) shown as an escape, never sent to the terminal."""
    text_value = "-" if value is None or value == "" else str(value)
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in text_value)


def cmd_show(db, op, args) -> None:
    v = _version(db, args.version)
    print(f"{_shown(v.county)}, {_shown(v.state)}  case {v.case_number or '-'}  {v.status}  revision {v.revision}"
          + (f"  published revision {v.published_revision}" if v.published_revision else ""))
    print(f"pdf: {_shown(v.source_url)}  sha {v.sha256}")
    print(f"manifest (page: rows): {v.manifest or '-'}")
    for r in db.execute(select(Row).where(Row.version_id == v.id, Row.revision == v.revision)
                        .order_by(Row.page, Row.row_no)).scalars():
        print(f"p{r.page} r{r.row_no}  {r.eligibility:<10} {r.parcel_id or '-'}  {r.amount_due or '-'}  "
              f"{r.tax_years or '-'}  {_shown(r.vested_owner)} | {_shown(r.taxpayer)} | "
              f"{_shown(r.property_address)} | {_shown(r.mailing_address)}"
              + (f"  ({r.exclusion_reason})" if r.exclusion_reason else "")
              + (f"  (reinstated: {r.reinstate_reason})" if r.reinstate_reason else ""))


def _pages(raw: str) -> dict[int, int]:
    pages: dict[int, int] = {}
    for part in raw.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", part)
        if not m or int(m.group(1)) in pages:
            raise SystemExit("--pages must be page:rows pairs, each page once, e.g. 6:3,7:3")
        pages[int(m.group(1))] = int(m.group(2))
    return pages


def _pdf_sha256(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except OSError as exc:
        raise SystemExit(f"cannot read the --pdf file ({type(exc).__name__})") from None


def cmd_manifest(db, op, args) -> str:
    rev = set_manifest(db, args.version, case_number=args.case, rows_per_page=_pages(args.pages), actor=op,
                       pdf_sha256=_pdf_sha256(args.pdf), new_case=args.new_case)
    return f"manifest recorded; the version is at revision {rev} with no rows: load them next"


def _create_windows_owner_only(path: str) -> int:
    """Create the file new, with a protected DACL granting only its owner, in one call:
    no moment exists in which the folder's inherited access applies, and sharing is off
    while it is open. Returns a C runtime file descriptor."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    advapi, kernel = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                    ("bInheritHandle", wintypes.BOOL)]

    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(_SecurityAttributes), wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.GetVolumeInformationByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeInformationByHandleW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.DeleteFileW.argtypes = [wintypes.LPCWSTR]

    sd = wintypes.LPVOID()
    # D:P = protected DACL (nothing inherited); one ACE: full access for OWNER RIGHTS.
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:P(A;;FA;;;OW)", 1, ctypes.byref(sd), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), sd, False)
        generic_write, create_new, normal = 0x40000000, 1, 0x80
        handle = kernel.CreateFileW(path, generic_write, 0, ctypes.byref(attrs), create_new, normal, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(sd)
    # A volume without persistent ACLs (FAT, exFAT) would drop the owner-only DACL on close.
    flags, file_persistent_acls = wintypes.DWORD(), 0x00000008
    if (not kernel.GetVolumeInformationByHandleW(handle, None, 0, None, None, ctypes.byref(flags), None, 0)
            or not flags.value & file_persistent_acls):
        kernel.CloseHandle(handle)
        kernel.DeleteFileW(path)
        raise OSError("the --out volume cannot keep the file private (no persistent ACLs)")
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY)
    except OSError:
        kernel.CloseHandle(handle)
        raise


def cmd_draft(db, op, args) -> str:
    payload = draft_rows(db, args.version)
    db.rollback()
    try:  # owner-only from creation: the file holds names. The path can carry a name: never echoed.
        if os.name == "nt":
            fd = _create_windows_owner_only(args.out)
        else:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("the --out file exists; give a new file") from None
    except OSError as exc:
        if getattr(exc, "winerror", None) == 80:  # ERROR_FILE_EXISTS
            raise SystemExit("the --out file exists; give a new file") from None
        raise SystemExit(f"cannot write the --out file ({type(exc).__name__})") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    return f"wrote {len(payload['rows'])} row(s) to the --out file, all unconfirmed"


class _DuplicateKeyError(ValueError):
    """A repeated key: json.loads would keep only its last value, silently."""


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _DuplicateKeyError  # neither the key nor its values: they can name someone
    return dict(pairs)


def cmd_load(db, op, args) -> str:
    try:
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKeyError:
        raise SystemExit("the --file repeats a key inside one object (json keeps only the last); "
                         "fix the file") from None
    except (OSError, ValueError) as exc:  # the path can carry a name: never echoed
        raise SystemExit(f"cannot read the --file ({type(exc).__name__})") from None
    rev, counts = load_rows(db, args.version, payload, actor=op)
    return f"loaded as revision {rev}: {_counts(counts)}"


def cmd_publish(db, op, args) -> str:
    superseded = publish(db, args.version, actor=op, expected_revision=args.revision,
                         pdf_sha256=_pdf_sha256(args.pdf))
    return f"published revision {args.revision}" + (f"; superseded {superseded}" if superseded else "")


# list/show print as they go; the others return what to say once their work is kept.
_COMMANDS = {"list": cmd_list, "show": cmd_show, "manifest": cmd_manifest, "draft": cmd_draft,
             "load": cmd_load, "publish": cmd_publish}
_DATABASE_WRITES = {"manifest", "load", "publish"}


class _QuietParser(argparse.ArgumentParser):
    """argparse quotes a rejected value back, and a value here can be a path naming
    someone: report only that the arguments were invalid."""

    def error(self, message):  # noqa: ARG002 -- the message is what must not be printed
        self.exit(2, f"{self.prog}: invalid or missing arguments; see --help\n")


def _parser() -> argparse.ArgumentParser:
    ap = _QuietParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--operator-email", required=True)
    sub = ap.add_subparsers(dest="command", required=True)  # subparsers are _QuietParser too
    sub.add_parser("list")
    sub.add_parser("show").add_argument("--version", required=True)
    p = sub.add_parser("manifest")
    p.add_argument("--version", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--pages", required=True)
    p.add_argument("--pdf", required=True, help="your downloaded copy of this version's PDF")
    p.add_argument("--new-case", action="store_true", help="a later year's summons, filed under a new case")
    p = sub.add_parser("draft")
    p.add_argument("--version", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("load")
    p.add_argument("--version", required=True)
    p.add_argument("--file", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--version", required=True)
    p.add_argument("--revision", required=True, type=int)
    p.add_argument("--pdf", required=True, help="your downloaded copy of this version's PDF")
    return ap


def _database_error(exc: SQLAlchemyError, outcome: str) -> SystemExit:
    """The driver's text can quote a row's values; report a reference instead."""
    ref = uuid.uuid4().hex[:12]
    print(f"tax_summons_review: database error {type(exc).__name__} ref {ref}", file=sys.stderr)
    return SystemExit(f"database error (ref {ref}); {outcome}")


_NOTHING = "nothing was written"
_UNKNOWN = "the save may or may not have happened: run show before trying again"


def main(argv=None, auth_factory=operator_session, session_factory=None) -> None:
    args = _parser().parse_args(argv)
    # A character the terminal's encoding cannot show is printed as an escape, never a crash
    # halfway through show.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        with auth_factory() as auth:
            op = _operator(auth, args.operator_email)
    except SQLAlchemyError as exc:
        # The operator check's own refusal ("must be an admin with MFA") is safe to show.
        if "admin with MFA" in str(getattr(exc, "orig", "")):
            raise SystemExit("refused: the operator must be an admin with MFA") from None
        raise _database_error(exc, _NOTHING) from None
    if session_factory is None:
        from src.db.session import system_sync_session as session_factory
    stage, done = _NOTHING, None
    try:  # session entry, the command, commit, rollback and cleanup alike
        with session_factory() as db:
            try:
                done = _COMMANDS[args.command](db, op, args)
            except (TaxReviewError, SQLAlchemyError):
                db.rollback()
                raise
            if args.command in _DATABASE_WRITES:
                stage = _UNKNOWN  # a commit that fails mid-way may still have landed
                db.commit()
                stage = "the change was saved; closing the session failed"
            elif done is not None:  # draft: the file is written, the database untouched
                stage = "the draft file was written; closing the session failed"
            if done is not None:
                print(done)
    except TaxReviewError as exc:
        raise SystemExit(f"refused: {exc}") from None
    except SQLAlchemyError as exc:
        raise _database_error(exc, stage) from None


if __name__ == "__main__":
    main()
