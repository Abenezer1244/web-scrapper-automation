"""Backfill historical pre_foreclosure leads so party_name shows the HOMEOWNER.

The scraper fixes (PR #85 + the templates) only correct NEW scrapes. The rows the
user already SEES in the UI are historical: for the counties that were unwired
(Pierce, Whatcom) the OLD code stored the trustee/lender COMPANY in party_name and
the borrower PERSON in `heirs`. The shared orient helper re-derives the homeowner
from the stored (party_name, heirs) pair — no re-scrape needed, lossless.

ORIENTATION-ONLY correction (Codex-reviewed): we ONLY touch a row when its current
party_name is a COMPANY and orient recovers a real PERSON from the pair. Rows that
are already a person are left untouched (no cosmetic "(+)"/vesting churn). We do NOT
recompute dedup_hash / source_fingerprint / property_key — this is a display/data
correction on already-delivered rows; their identity reflects original ingest.

Safety:
- Dry-run by DEFAULT. Prints per-county counts + samples and writes an audit CSV.
- --commit needs an OWNER/admin DSN (ADMIN_DATABASE_URL_SYNC) — the system role
  can't UPDATE results. UPDATE is scoped by immutable results.id AND guarded with
  the expected old values (optimistic concurrency) so a concurrent change is never
  clobbered. Old party_name + heirs are stashed into enrichment_data for reversal.
- Idempotent: a re-run skips rows whose party_name is already a person.

Run (dry-run, READ-ONLY, all tenants):
    railway run --service worker python scripts/backfill_preforeclosure_party_names.py
    railway run --service worker python scripts/backfill_preforeclosure_party_names.py --county pierce

Apply (after reviewing the dry-run):
    ADMIN_DATABASE_URL_SYNC=... python scripts/backfill_preforeclosure_party_names.py --commit
"""
import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.db.session import system_sync_session
from src.scrapers.preforeclosure import (
    is_person_name,
    orient_pre_foreclosure_party,
)

# Mirrors pierce_wa_probate._strip_arms_plus (ARMS "(+)" more-parties marker). Kept
# inline so this DB script doesn't import the Playwright scraper chain. No-op when
# absent (whatcom/templates), so it is safe to apply to every county's stored value.
_PLUS = re.compile(r"\s*\(\+\)\s*")
BACKUP_PARTY_KEY = "party_name_pre_backfill_2026_06_21"
BACKUP_HEIRS_KEY = "heirs_pre_backfill_2026_06_21"
BATCH = 200


def _strip_plus(v):
    if not v:
        return v
    return _PLUS.sub(" ", v).strip() or None


# Backfill is the salvage layer (Codex): demand a CLEAN person before overwriting a
# stored value. is_person_name intentionally ALLOWS family trusts ("DOE FAMILY TRUST")
# for the live scrapers, but historical data also carries securitization trusts
# ("SPLITERO MASTER TRUST", "WILMINGTON TRUST N A"), "(...)Remarks:" / digit
# concatenation artifacts, and "PUBLIC" placeholders that slip past is_person_name.
# Rejecting these here leaves the row UNCHANGED (never a wrong swap) — we accept
# losing a rare genuine family-trust owner over introducing a wrong institutional name.
_DIRTY_NEW = re.compile(r"\d|TRUST|LLC|INC|CORP|REMARKS|PUBLIC|\bN\s+A\b|P\s*L\s*L\s*C", re.I)


def _is_clean_person(name):
    return bool(name) and is_person_name(name) and not _DIRTY_NEW.search(name)


def _classify(party_name, heirs):
    """Return ('change', new_party, new_heirs) | ('already_person', ..) | ('unrecoverable', ..)."""
    if is_person_name(party_name):
        return ("already_person", party_name, heirs)
    oriented = orient_pre_foreclosure_party(_strip_plus(party_name), _strip_plus(heirs))
    if oriented is None:
        return ("unrecoverable", party_name, heirs)
    new_party, new_heirs = oriented
    # Orientation-only guard: only overwrite a company with a CLEAN person, and only
    # when it is an actual change (idempotent).
    if not _is_clean_person(new_party) or new_party == party_name:
        return ("unrecoverable", party_name, heirs)
    return ("change", new_party, new_heirs)


def _load_rows(county_filter):
    sql = (
        "SELECT r.id AS id, r.user_id AS user_id, r.party_name AS party_name, "
        "       r.heirs AS heirs, sc.county AS county, sc.state AS state "
        "FROM results r "
        "JOIN jobs j ON j.id = r.job_id "
        "JOIN scraper_configs sc ON sc.id = j.scraper_config_id "
        "WHERE sc.record_type = 'pre_foreclosure'"
    )
    if county_filter:
        sql += " AND lower(sc.county) = :county"
    with system_sync_session() as db:
        params = {"county": county_filter.lower()} if county_filter else {}
        return db.execute(text(sql), params).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="apply (needs ADMIN_DATABASE_URL_SYNC)")
    ap.add_argument("--county", default="", help="restrict to one county slug")
    ap.add_argument("--limit", type=int, default=0, help="cap rows CHANGED (0 = no cap)")
    ap.add_argument("--csv", default="scripts/audit_out/preforeclosure_backfill_audit.csv")
    args = ap.parse_args()

    print("=" * 72)
    print(f"pre_foreclosure party-name BACKFILL  (mode={'COMMIT' if args.commit else 'DRY-RUN'})")
    print("=" * 72)

    rows = _load_rows(args.county)
    print(f"Scanned {len(rows)} pre_foreclosure rows"
          + (f" in {args.county}" if args.county else " (all counties)"))

    changes, by_county = [], {}
    for r in rows:
        verdict, np_, nh = _classify(r.party_name, r.heirs)
        c = (r.county or "?").lower()
        bucket = by_county.setdefault(c, {"change": 0, "already_person": 0, "unrecoverable": 0})
        bucket[verdict] += 1
        if verdict == "change":
            changes.append({
                "result_id": r.id, "user_id": r.user_id, "county": c, "state": r.state,
                "old_party": r.party_name, "old_heirs": r.heirs,
                "new_party": np_, "new_heirs": nh,
            })

    print("\nPer-county (change / already-person / unrecoverable):")
    for c in sorted(by_county):
        b = by_county[c]
        print(f"  {c:<12} change={b['change']:<5} ok={b['already_person']:<5} unrecoverable={b['unrecoverable']}")
    print(f"\nTOTAL rows to change: {len(changes)}")

    # Samples: first 6 + 6 from the largest-change county.
    if changes:
        print("\nsample changes:")
        for ch in changes[:6]:
            print(f"  [{ch['county']}] {ch['old_party']!r}  ->  {ch['new_party']!r}   (heirs<-{ch['old_party']!r})")

    # Always write the audit CSV (dry-run proof + commit record).
    os.makedirs(os.path.dirname(args.csv), exist_ok=True)
    with open(args.csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["result_id", "user_id", "county", "state",
                                           "old_party", "old_heirs", "new_party", "new_heirs"])
        w.writeheader()
        w.writerows(changes)
    print(f"\nAudit CSV: {args.csv} ({len(changes)} rows)")

    if not args.commit:
        print("\nDRY-RUN — no DB writes. Re-run with --commit + ADMIN_DATABASE_URL_SYNC to apply.")
        return

    dsn = os.getenv("ADMIN_DATABASE_URL_SYNC")
    if not dsn:
        sys.exit("--commit requires ADMIN_DATABASE_URL_SYNC (owner/admin DSN) — refusing.")
    to_apply = changes[: args.limit] if args.limit else changes
    print(f"\nAPPLYING {len(to_apply)} updates in batches of {BATCH}...")
    engine = create_engine(dsn, pool_pre_ping=True)
    Session = sessionmaker(engine)
    # Optimistic-concurrency UPDATE: only touch the row if it still holds the old
    # values; stash old party_name + heirs into enrichment_data (preserving keys).
    # enrichment_data is a `json` column (not jsonb): cast to jsonb to use `||`,
    # then cast the merged result back to json for assignment. Old party_name +
    # heirs stashed for reversibility (jsonb_build_object coerces the text binds —
    # no `::text` adjacency to a bind param, which can mis-parse; Codex P2). WHERE
    # guards (Codex P2): optimistic concurrency on old values; only merge when
    # enrichment_data is NULL or a JSON object (never corrupt array/scalar shape);
    # skip rows already carrying the backup key (double idempotency on re-run).
    upd = text(
        "UPDATE results SET "
        "  party_name = :new_party, "
        "  heirs = :new_heirs, "
        "  enrichment_data = (COALESCE(enrichment_data::jsonb, '{}'::jsonb) "
        "    || jsonb_build_object(:bk_party, :old_party, :bk_heirs, :old_heirs))::json "
        "WHERE id = :id "
        "  AND party_name = :old_party "
        "  AND heirs IS NOT DISTINCT FROM :old_heirs "
        "  AND (enrichment_data IS NULL "
        "       OR jsonb_typeof(enrichment_data::jsonb) = 'object') "
        "  AND NOT (COALESCE(enrichment_data::jsonb, '{}'::jsonb) ? :bk_party)"
    )
    applied, skipped = 0, []
    with Session() as s:
        for i in range(0, len(to_apply), BATCH):
            batch = to_apply[i:i + BATCH]
            for ch in batch:
                res = s.execute(upd, {
                    "id": ch["result_id"], "new_party": ch["new_party"],
                    "new_heirs": ch["new_heirs"], "old_party": ch["old_party"],
                    "old_heirs": ch["old_heirs"], "bk_party": BACKUP_PARTY_KEY,
                    "bk_heirs": BACKUP_HEIRS_KEY,
                })
                if res.rowcount:
                    applied += res.rowcount
                else:
                    skipped.append(ch["result_id"])
            s.commit()
            print(f"  committed batch {i // BATCH + 1}: {min(i + BATCH, len(to_apply))}/{len(to_apply)} (updated so far: {applied})")
    print(f"\nDONE. Rows updated: {applied} / {len(to_apply)} attempted.")
    if skipped:
        print(f"SKIPPED {len(skipped)} rows (concurrent change / already-applied / "
              "non-object enrichment_data). First 20 ids:")
        for sid in skipped[:20]:
            print(f"  {sid}")
        sys.exit(2)  # loud: a clean first run should skip nothing


if __name__ == "__main__":
    main()
