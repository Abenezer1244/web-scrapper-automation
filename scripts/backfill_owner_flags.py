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
    SELECT id, property_address, mailing_address, property_city, property_state, property_zip
    FROM results
    WHERE owner_state IS NULL
      AND property_state IS NULL
      AND absentee_owner IS NULL
      AND out_of_state_owner IS NULL
      AND id > :after
    ORDER BY id
    LIMIT :lim
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
      AND property_address IS NOT DISTINCT FROM :pa
      AND mailing_address IS NOT DISTINCT FROM :ma
      AND property_city IS NOT DISTINCT FROM :pc
      AND property_zip IS NOT DISTINCT FROM :pz
      AND absentee_owner IS NOT DISTINCT FROM :cur_absentee
      AND owner_state IS NOT DISTINCT FROM :cur_owner_state
      AND property_state IS NOT DISTINCT FROM :cur_property_state
      AND out_of_state_owner IS NOT DISTINCT FROM :cur_out_of_state
    """
)  # guarded: a row a worker changed after it was read is skipped, never clobbered (Codex)
_NULL_FLAGS = {"cur_absentee": None, "cur_owner_state": None, "cur_property_state": None,
               "cur_out_of_state": None}


# Recompute (2026-11-15) of every absentee_owner=TRUE row after the same-place fixes
# (glued city on Chelan situs, unit spellings on King). Writes ONLY absentee_owner, so
# a repair can never overwrite the state / out-of-state flags (Codex). Keyset paging,
# because a corrected row leaves the TRUE set and OFFSET would skip rows; the UPDATE
# re-checks the inputs it was computed from, so a concurrent edit is skipped, not clobbered.
_SELECT_ABSENTEE = text(
    """
    SELECT id, property_address, mailing_address, property_city, property_state, property_zip
    FROM results
    WHERE absentee_owner = :cur AND id > :after
      AND property_address IS NOT NULL AND mailing_address IS NOT NULL
    ORDER BY id
    LIMIT :lim
    """
)
_UPDATE_ABSENTEE = text(
    """
    UPDATE results SET absentee_owner = :absentee
    WHERE id = :id AND absentee_owner = :cur
      AND property_address IS NOT DISTINCT FROM :pa AND mailing_address IS NOT DISTINCT FROM :ma
      AND property_city IS NOT DISTINCT FROM :pc AND property_state IS NOT DISTINCT FROM :ps
      AND property_zip IS NOT DISTINCT FROM :pz
    """
)


def recompute_absentee(commit: bool, batch: int, cur: bool = True) -> None:
    """Re-evaluate every row whose absentee_owner is `cur`; write only rows that change.

    cur=False (2026-11-17) re-checks rows an earlier, looser rule cleared: Codex judged
    the spaced house letter ("2651 B" vs "2651B") ambiguous, so those go back to True.
    """
    after = "00000000-0000-0000-0000-000000000000"
    scanned = written = skipped = 0
    changed: dict[object, int] = {}
    shown = 0
    with system_sync_session() as db:
        while True:
            rows = db.execute(_SELECT_ABSENTEE, {"after": after, "lim": batch, "cur": cur}).fetchall()
            if not rows:
                break
            for rid, pa, ma, pc, ps, pz in rows:
                scanned += 1
                new = compute_owner_flags(pa, ma, property_city=pc, property_state=ps,
                                          property_zip=pz)["absentee_owner"]
                if new is cur:
                    continue
                changed[new] = changed.get(new, 0) + 1
                if shown < 40:  # sample for operator review in the run log
                    print(f"  {rid}: {cur} -> {new}  [{pa!r} vs {ma!r}]", flush=True)
                    shown += 1
                if commit:
                    n = db.execute(_UPDATE_ABSENTEE, {"id": rid, "absentee": new, "cur": cur,
                                                      "pa": pa, "ma": ma, "pc": pc, "ps": ps,
                                                      "pz": pz}).rowcount
                    written += n
                    skipped += 1 - n
            after = str(rows[-1][0])
            if commit:
                db.commit()
            print(f"  scanned={scanned} changed={changed}", flush=True)
    print("\n=== absentee recompute ===")
    print(f"scanned rows with absentee={cur}: {scanned}")
    for new, n in changed.items():
        print(f"{cur} -> {new}: {n}")
    if commit:
        print(f"written: {written}  skipped (row changed meanwhile): {skipped}")
    else:
        print("DRY RUN - re-run with --commit")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="apply updates (default: dry run)")
    ap.add_argument("--batch", type=int, default=1000, help="rows per chunk (Codex: 1k-5k)")
    ap.add_argument(
        "--recompute-absentee", action="store_true",
        help="re-evaluate every absentee=TRUE row with the structured situs parts; writes absentee_owner only",
    )
    ap.add_argument(
        "--recompute-not-absentee", action="store_true",
        help="re-evaluate every absentee=FALSE row the same way (rows a looser rule cleared)",
    )
    args = ap.parse_args()
    if args.recompute_absentee or args.recompute_not_absentee:
        recompute_absentee(args.commit, max(100, min(args.batch, 5000)),
                           cur=not args.recompute_not_absentee)
        return
    batch = max(100, min(args.batch, 5000))

    scanned = 0
    would_set = {"property_state": 0, "owner_state": 0, "absentee": 0, "out_of_state": 0}
    committed = 0

    # Keyset paging by id: a row leaving the all-NULL window (written here or by a
    # worker) can never shift the next page, so nothing is skipped (Codex). `after`
    # persists across reconnects, so a dropped pooler connection resumes in place.
    after = "00000000-0000-0000-0000-000000000000"
    done = False
    while not done:
        try:
            with system_sync_session() as db:
                while True:
                    rows = db.execute(_SELECT, {"lim": batch, "after": after}).fetchall()
                    if not rows:
                        done = True
                        break
                    for rid, prop, mail, pc, ps, pz in rows:
                        # With the structured situs parts, exactly as the worker does, so
                        # a row is never settled on a weaker verdict than live rows get.
                        flags = compute_owner_flags(prop, mail, property_city=pc,
                                                    property_state=ps, property_zip=pz)
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
                        if has_any and args.commit:
                            # Selected from the all-NULL window, so all four were NULL.
                            db.execute(_UPDATE, {"id": rid, **flags, "pa": prop, "ma": mail,
                                                 "pc": pc, "pz": pz, **_NULL_FLAGS})
                            committed += 1
                    if args.commit:
                        db.commit()
                    after = str(rows[-1][0])
                    print(f"  scanned={scanned} committed={committed} after={after}", flush=True)
        except OperationalError as exc:
            # Supavisor drops long-lived sessions; commits are per-chunk so progress
            # is durable. Reconnect and resume after the last id read.
            print(f"  [reconnect] pooler dropped the session ({str(exc).splitlines()[0][:60]}); "
                  f"resuming after {after} in 5s", flush=True)
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
