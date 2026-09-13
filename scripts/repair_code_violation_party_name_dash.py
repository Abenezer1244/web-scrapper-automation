"""Replace the em dash in existing code-violation party names with a plain hyphen.

The King and Pierce code-violation scrapers built party_name as "<label> — <address>"
until #289; customers see party_name, and product copy never uses an em dash. New rows
already use " - ". This rewrites the stored rows the same way.

Safe for identity: every affected row has a strong parcel|address dedup key (checked in
prod on 2026-09-13: 1,836 rows, all strong), so no dedup, billing or overlap key reads the
changed text. Rows on jobs that are still running are skipped (their within-job
idempotency fingerprint includes party_name). Guarded single-row UPDATEs pinned to id,
user_id and the exact old value.

    railway run --service worker python scripts/repair_code_violation_party_name_dash.py           # dry-run
    railway run --service worker python scripts/repair_code_violation_party_name_dash.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

EM_DASH = "—"
TERMINAL_JOB_STATUSES = ("done", "failed", "cancelled")

_CANDIDATES_SQL = """
    SELECT r.id, r.user_id, r.parcel_id, r.property_address, r.party_name, lower(sc.county) AS county
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE sc.record_type = 'code_violation'
      AND lower(sc.county) IN ('king', 'pierce') AND upper(sc.state) = 'WA'
      AND j.status IN :terminal
      AND strpos(r.party_name, :dash) > 0
    ORDER BY r.id
"""

_UPDATE_SQL = """
    UPDATE results SET party_name = :new_name
    WHERE id = :rid AND user_id = :uid AND party_name = :old_name
      AND parcel_id IS NOT DISTINCT FROM :parcel AND property_address IS NOT DISTINCT FROM :prop
      AND EXISTS (SELECT 1 FROM jobs j WHERE j.id = results.job_id AND j.status IN :terminal)
"""


def new_name(old: str) -> str:
    """" — " becomes " - "; a bare em dash (no spaces) becomes "-"."""
    return old.replace(f" {EM_DASH} ", " - ").replace(EM_DASH, "-")


def run(db, *, apply_writes: bool) -> dict:
    from sqlalchemy import bindparam

    from src.workers.property_identity import legacy_strong_signature

    terminal = bindparam("terminal", expanding=True)
    rows = db.execute(text(_CANDIDATES_SQL).bindparams(terminal),
                      {"terminal": list(TERMINAL_JOB_STATUSES), "dash": EM_DASH}).all()
    db.rollback()
    weak = [r for r in rows if legacy_strong_signature(r.parcel_id, r.property_address) is None]
    stats: dict = {"candidates": dict(Counter(r.county for r in rows)),
                   "weak_identity_skipped": len(weak),
                   "sample": [(r.party_name, new_name(r.party_name)) for r in rows[:3]]}
    if not apply_writes:
        return stats
    # A weak-identity row's dedup key is NAME|DATE: changing the name would split it from
    # its own history, so those are never rewritten.
    weak_ids = {r.id for r in weak}
    counts: Counter = Counter()
    for i, r in enumerate((x for x in rows if x.id not in weak_ids), 1):
        res = db.execute(text(_UPDATE_SQL).bindparams(terminal), {
            "new_name": new_name(r.party_name), "rid": r.id, "uid": r.user_id,
            "old_name": r.party_name, "parcel": r.parcel_id, "prop": r.property_address,
            "terminal": list(TERMINAL_JOB_STATUSES)})
        counts["written" if res.rowcount else "skipped_by_write_guard"] += 1
        if i % 500 == 0:
            db.commit()
    db.commit()
    stats["writes"] = dict(counts)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args(argv)
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        stats = run(db, apply_writes=args.apply)
    print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
    if not args.apply:
        print("dry-run: nothing written (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
