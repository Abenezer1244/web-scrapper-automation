"""Backfill owner-location flags (migration 057) on existing results rows.

Computes property_state / owner_state / absentee_owner / out_of_state_owner for
the 310k pre-057 rows via src/utils/address_intel.compute_owner_flags (the SAME
helper the worker uses at insert + end-of-job, so backfilled and live rows can't
disagree). property/mailing addresses are NOT encrypted, so no key is needed.

Runs as the worker role (bridgeleads_system) which has a FOR ALL RLS policy on
results — sufficient to UPDATE every tenant's rows under FORCE'd RLS (no
DATABASE_URL_MIGRATE / BYPASSRLS bypass needed for a data backfill, Codex).

Idempotent + resumable: only touches rows where all four flags are still NULL
(WHERE owner_state IS NULL AND property_state IS NULL AND absentee_owner IS NULL
AND out_of_state_owner IS NULL). Re-running after a partial run continues where it
stopped. A row whose addresses genuinely yield all-NULL flags (no parseable state,
one address missing) is re-evaluated each run — harmless (same result) and rare
relative to the bulk.

  Dry run (default, no writes):  railway run --service worker python scripts/backfill_owner_flags.py
  Commit:                        railway run --service worker python scripts/backfill_owner_flags.py --commit
  Tune batch:                    ... --commit --batch 2000
"""
import argparse
import logging
import sys
import time

sys.path.insert(0, ".")  # railway-run cwd shim (see other scripts)

# Silence the prod worker's SQLAlchemy echo (DEBUG): it floods the log AND slows
# the per-row UPDATE loop. We only want our own progress lines.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from src.db.session import system_sync_session
from src.utils.address_intel import compute_owner_flags

_SELECT = text(
    """
    SELECT id, property_address, mailing_address
    FROM results
    WHERE owner_state IS NULL
      AND property_state IS NULL
      AND absentee_owner IS NULL
      AND out_of_state_owner IS NULL
    ORDER BY id
    LIMIT :lim
    OFFSET :off
    """
)

_UPDATE = text(
    """
    UPDATE results
    SET property_state = :property_state,
        owner_state = :owner_state,
        absentee_owner = :absentee_owner,
        out_of_state_owner = :out_of_state_owner
    WHERE id = :id
    """
)


# Targeted recompute (2026-09-02): rows already flagged absentee_owner=TRUE whose
# mailing street merely EXTENDS the property street (county situs dropped the
# suffix / post-directional: "20508 ISLAND PKWY" vs "20508 ISLAND PKWY E …").
# compute_owner_flags now reads those as unknown (NULL), not absentee. The NULL
# window above never revisits them, so this mode re-evaluates exactly that set.
_SELECT_SUFFIXLESS = text(
    """
    SELECT id, property_address, mailing_address
    FROM results
    WHERE absentee_owner IS TRUE
      AND property_address IS NOT NULL
      AND mailing_address IS NOT NULL
      AND UPPER(mailing_address) LIKE UPPER(property_address) || ' %'
    ORDER BY id
    """
)


def recompute_suffixless(commit: bool) -> None:
    changed = 0
    scanned = 0
    with system_sync_session() as db:
        rows = db.execute(_SELECT_SUFFIXLESS).fetchall()
        for rid, prop, mail in rows:
            scanned += 1
            flags = compute_owner_flags(prop, mail)
            if flags["absentee_owner"] is True:
                continue  # still a different place (e.g. different ZIP) — keep
            print(f"  {rid}: absentee True -> {flags['absentee_owner']}  [{prop!r} vs {mail!r}]", flush=True)
            changed += 1
            if commit:
                db.execute(_UPDATE, {"id": rid, **flags})
        if commit:
            db.commit()
    print("\n=== suffixless absentee recompute ===")
    print(f"scanned:   {scanned}")
    print(f"corrected: {changed}" + ("" if commit else "  (DRY RUN — re-run with --commit)"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="apply updates (default: dry run)")
    ap.add_argument("--batch", type=int, default=1000, help="rows per chunk (Codex: 1k-5k)")
    ap.add_argument(
        "--recompute-suffixless", action="store_true",
        help="only re-evaluate absentee=TRUE rows whose mailing street extends the property street",
    )
    args = ap.parse_args()
    if args.recompute_suffixless:
        recompute_suffixless(args.commit)
        return
    batch = max(100, min(args.batch, 5000))

    scanned = 0
    would_set = {"property_state": 0, "owner_state": 0, "absentee": 0, "out_of_state": 0}
    committed = 0

    # The SELECT window is "all four flags still NULL". On --commit, a row that gets
    # any non-NULL flag leaves the window; rows that stay all-NULL (no parseable
    # data at all) remain, so OFFSET must skip PAST them or the next read re-fetches
    # them forever. `offset` counts intentionally-left-NULL rows and PERSISTS across
    # reconnects (below), so a dropped pooler connection just resumes where it was.
    offset = 0
    done = False
    while not done:
        try:
            with system_sync_session() as db:
                while True:
                    rows = db.execute(_SELECT, {"lim": batch, "off": offset}).fetchall()
                    if not rows:
                        done = True
                        break
                    chunk_left_null = 0
                    for rid, prop, mail in rows:
                        flags = compute_owner_flags(prop, mail)
                        scanned += 1
                        if flags["property_state"]:
                            would_set["property_state"] += 1
                        if flags["owner_state"]:
                            would_set["owner_state"] += 1
                        if flags["absentee_owner"] is not None:
                            would_set["absentee"] += 1
                        if flags["out_of_state_owner"] is not None:
                            would_set["out_of_state"] += 1
                        has_any = any(v is not None for v in flags.values())
                        if not has_any:
                            chunk_left_null += 1  # stays in the NULL window — skip past it
                        elif args.commit:
                            db.execute(_UPDATE, {"id": rid, **flags})
                            committed += 1
                    if args.commit:
                        db.commit()
                        offset += chunk_left_null  # committed rows left the window already
                    else:
                        offset += len(rows)  # dry run changes nothing; walk the whole table
                    print(f"  scanned={scanned} committed={committed} offset={offset}", flush=True)
        except OperationalError as exc:
            # Supavisor drops long-lived sessions; commits are per-chunk so progress
            # is durable. Reconnect and resume from the preserved offset.
            print(f"  [reconnect] pooler dropped the session ({str(exc).splitlines()[0][:60]}); "
                  f"resuming at offset={offset} in 5s", flush=True)
            time.sleep(5)

    print("\n=== owner-flag backfill ===")
    print(f"scanned:        {scanned}")
    print(f"property_state: {would_set['property_state']}")
    print(f"owner_state:    {would_set['owner_state']}")
    print(f"absentee known: {would_set['absentee']}")
    print(f"out_of_state known: {would_set['out_of_state']}")
    if args.commit:
        print(f"COMMITTED rows: {committed}")
    else:
        print("DRY RUN — no writes. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
