"""Read-only DROP match report: how many DROP identifiers our stored leads would match.

Run under `railway run --service worker python scripts/drop_match_report.py
[--download <uuid>]`. It changes nothing and prints COUNTS ONLY: per list (email, phone),
the identifiers pending (recorded by the DROP pull and not withdrawn), how many distinct
identifiers hit at least one stored lead, how many did not, and how many leads were hit.
Never a name, a contact, a DROP Id or a hash.

How: the pending hashes come from migration 139's drop_pending_hashes() (keyset pages);
the leads are read through the ORM in id-ordered batches, so results.email/emails and
phone/phones decrypt, and each value is standardized and hashed exactly as DROP does
(src/privacy/drop.py). Leads a privacy rule already withholds are left out
(not_privacy_suppressed_condition): this sizes what DROP would still have to act on.
It is a sizing tool for counsel's D8 decision, not the matching step (not built yet).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_, select, text  # noqa: E402

from src.api.lead_actionability import not_privacy_suppressed_condition  # noqa: E402
from src.db.models import Result  # noqa: E402
from src.db.session import system_sync_session  # noqa: E402
from src.privacy.drop import EMAIL, LIST_TYPES, PHONE, email_digest, phone_digest  # noqa: E402

_PAGE = 10000
_LEAD_BATCH = 1000


def _pending(db, download_id: str | None) -> dict[str, dict[str, set[str]]]:
    """list type -> hash -> identifier ids (one hash can arrive under several Ids)."""
    out: dict[str, dict[str, set[str]]] = {t: {} for t in LIST_TYPES}
    after = None
    while True:
        rows = db.execute(text(
            "SELECT * FROM public.drop_pending_hashes(CAST(:d AS uuid), NULL, "
            "CAST(:a AS uuid), :n)"), {"d": download_id, "a": after, "n": _PAGE}).all()
        for r in rows:
            out[r.list_type].setdefault(r.hash, set()).add(str(r.id))
        if len(rows) < _PAGE:
            return out
        after = rows[-1].id


def _lead_digests(row) -> dict[str, set[str]]:
    emails = [row.email] + [e for e in (row.emails or []) if isinstance(e, str)]
    phones = [row.phone] + [p.get("number") for p in (row.phones or [])
                            if isinstance(p, dict) and isinstance(p.get("number"), str)]
    return {EMAIL: {d for d in map(email_digest, emails) if d},
            PHONE: {d for d in map(phone_digest, phones) if d}}


def match_counts(db, download_id: str | None = None) -> dict[str, dict[str, int]]:
    pending = _pending(db, download_id)
    matched: dict[str, set[str]] = {t: set() for t in LIST_TYPES}
    leads_hit: dict[str, int] = dict.fromkeys(LIST_TYPES, 0)
    has_contact = or_(Result.email.isnot(None), Result.emails.isnot(None),
                      Result.phone.isnot(None), Result.phones.isnot(None))
    after = None
    while True:
        stmt = (select(Result.id, Result.email, Result.emails, Result.phone, Result.phones)
                .where(has_contact, not_privacy_suppressed_condition())
                .order_by(Result.id).limit(_LEAD_BATCH))
        if after is not None:
            stmt = stmt.where(Result.id > after)
        rows = db.execute(stmt).all()
        for row in rows:
            for list_type, digests in _lead_digests(row).items():
                hit = False
                for d in digests:
                    ids = pending[list_type].get(d)
                    if ids:
                        matched[list_type] |= ids
                        hit = True
                leads_hit[list_type] += hit
        if len(rows) < _LEAD_BATCH:
            break
        after = rows[-1].id
    report = {}
    for t in LIST_TYPES:
        total = sum(len(ids) for ids in pending[t].values())
        report[t] = {"pending": total, "matched": len(matched[t]),
                     "unmatched": total - len(matched[t]), "leads_hit": leads_hit[t]}
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--download", help="only this drop_downloads id")
    args = ap.parse_args(argv)
    with system_sync_session() as db:
        try:
            report = match_counts(db, args.download)
        finally:
            db.rollback()  # read-only: nothing to keep
    for t, c in report.items():
        print(f"{t}: pending={c['pending']} matched={c['matched']} "
              f"unmatched={c['unmatched']} leads_hit={c['leads_hit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
