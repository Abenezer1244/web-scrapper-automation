"""Give existing King code-violation leads their real semantics: category, parcel tier, owner.

Before 2026-09-14 the King (Seattle SDCI) scraper wrote the case label into party_name
("Complaint - 7011 ROOSEVELT WAY NE") and kept SDCI's violation category nowhere else.
For every stored lead in a DONE job this script:

  1. Re-reads the case from the SDCI source by its record number (authoritative; the
     label is never parsed) and stores `violation_category`.
  2. For a parcel located before `kc_pin_match` existed, re-locates it and records the
     tier. Only a re-located PIN equal to the stored one can be "exact"; anything else
     is "unconfirmed". Mailing addresses are never touched.
  3. For an EXACT located PIN, reads the owner (King Assessor eRealProperty, lease-guarded
     and paced) with --owners.
  4. Replaces party_name ONLY when it still equals the label the old scraper built from
     that same source row: with the owner when found, otherwise with NULL (the label
     is not a party). Any other party_name is left alone and reported.

Guarded single-row UPDATEs (same user, same party_name as read, job still done). No
parcel_id, dedup, billing, quota, skip trace or delivery change. Idempotent: a repaired
row carries `cv_semantics_repaired_at`; --retry-owners revisits exact rows still unnamed.

    railway run --service worker python scripts/backfill_king_code_violation_owner.py              # dry-run
    railway run --service worker python scripts/backfill_king_code_violation_owner.py --owners --apply

Owner lookups need the King source lease in the SAME Redis as the workers. Off Railway,
`railway run` injects the private Redis host, where the lease fails OPEN; point
REDIS_URL at the Redis service's public URL first (the script refuses otherwise).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

_SOURCE = "seattle_sdci_code_violations"
_SOCRATA_URL = "https://data.seattle.gov/resource/ez4a-iug7.json"
_SOCRATA_BATCH = 100
_LABEL_MAX = 120  # the old scraper's cap, needed to rebuild its label byte for byte
_RECORDNUM_RE = re.compile(r"^[0-9A-Za-z-]{1,32}$")
UNCONFIRMED = "unconfirmed"

_CANDIDATES_SQL = """
    SELECT r.id, r.user_id, r.party_name, r.property_address, r.legal_description,
           r.enrichment_data::jsonb AS ed
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'code_violation'
      AND j.status = 'done'
      AND jsonb_typeof(r.enrichment_data::jsonb) = 'object'
      AND r.enrichment_data::jsonb->>'source' = :source
      AND (NOT (r.enrichment_data::jsonb ? 'cv_semantics_repaired_at')
           OR (:retry_owners AND coalesce(btrim(r.party_name), '') = ''
               AND r.enrichment_data::jsonb->>'kc_pin_match' = 'exact'
               AND NOT (r.enrichment_data::jsonb ? 'owner_source')))
    ORDER BY r.id
"""

_UPDATE_SQL = """
    UPDATE results SET
      party_name = CAST(:new_party AS varchar),
      enrichment_data = (enrichment_data::jsonb || CAST(:payload AS jsonb))::json
    WHERE id = :rid AND user_id = :uid
      AND party_name IS NOT DISTINCT FROM CAST(:old_party AS varchar)
      AND jsonb_typeof(enrichment_data::jsonb) = 'object'
      AND enrichment_data::jsonb->>'source' = :source
      AND enrichment_data::jsonb->>'kc_pin' IS NOT DISTINCT FROM CAST(:old_pin AS text)
      AND enrichment_data::jsonb->>'kc_pin_status' IS NOT DISTINCT FROM CAST(:old_pin_status AS text)
      AND enrichment_data::jsonb->>'kc_pin_match' IS NOT DISTINCT FROM CAST(:old_pin_match AS text)
      AND enrichment_data::jsonb->>'kc_pin_source' IS NOT DISTINCT FROM CAST(:old_pin_source AS text)
      AND NOT (enrichment_data::jsonb ? 'owner_source')
      AND EXISTS (SELECT 1 FROM jobs j WHERE j.id = results.job_id AND j.status = 'done')
"""


def old_scraper_label(raw: dict) -> str:
    """The party_name the pre-2026-09-14 scraper built from this SDCI row."""
    label = (raw.get("recordtypedesc") or raw.get("recordtype") or "Code Violation").strip()[:_LABEL_MAX]
    addr = (raw.get("originaladdress1") or "").strip()
    return f"{label} - {addr}" if addr else label


def fetch_source_rows(recordnums: list[str], *, pace_s: float = 1.0) -> dict[str, dict]:
    """{recordnum: raw SDCI row} for the given case numbers, in paced batches."""
    from src.api.middleware.security import add_scrape_domain
    from src.config import settings
    from src.utils.safe_http import safe_get

    add_scrape_domain("data.seattle.gov")
    wanted = sorted({n for n in recordnums if n and _RECORDNUM_RE.match(n)})
    out: dict[str, dict] = {}
    for i in range(0, len(wanted), _SOCRATA_BATCH):
        chunk = wanted[i:i + _SOCRATA_BATCH]
        quoted = ",".join(f"'{n}'" for n in chunk)  # validated by _RECORDNUM_RE above
        resp = safe_get(_SOCRATA_URL, params={"$where": f"recordnum in ({quoted})",
                                              "$limit": len(chunk) * 2},
                        headers={"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"},
                        timeout=settings.DEFAULT_TIMEOUT)
        resp.raise_for_status()  # a failed batch aborts the run; nothing half-applied
        data = resp.json()
        if not isinstance(data, list):
            raise RuntimeError(f"SDCI returned {type(data).__name__}, expected a list")
        for row in data:
            if isinstance(row, dict) and row.get("recordnum") in chunk:
                out[row["recordnum"]] = row
        time.sleep(pace_s)
    return out


def resolve_owners(pins: list[str], *, delay: float) -> dict[str, str]:
    """{pin: owner} from eRealProperty via the shared lease-guarded owner-only path."""
    from src.scrapers.enrichment.king_county_assessor import (
        KingOwnerLookupBlockedError,
        batch_extract_king_owners,
    )
    from src.scrapers.enrichment.source_health import SourceUnavailableError

    owners: dict[str, str] = {}
    try:
        asyncio.run(batch_extract_king_owners(
            pins, delay=delay, circuit_window=20, max_transient_rate=0.10,
            max_unresolved_rate=0.50, fetch_attempts=2, out=owners))
    except (KingOwnerLookupBlockedError, SourceUnavailableError) as exc:
        print(f"owner lookup stopped early, keeping {len(owners)} names: {str(exc)[:160]}")
    return owners


def _recordnum(row) -> str:
    return (row.ed.get("record_number") or row.legal_description or "").strip()


def run(db, *, apply_writes: bool, owners: bool, retry_owners: bool = False,
        limit: int | None = None, report: Path | None = None, socrata_pace_s: float = 1.0,
        gis_pace_s: float = 0.35, owner_delay: float = 2.0) -> dict:
    from src.scrapers.enrichment.king_parcel_locate import OWNER_SOURCE, locate
    from src.utils.located_parcel import located_parcel_id

    rows = db.execute(text(_CANDIDATES_SQL),
                      {"source": _SOURCE, "retry_owners": retry_owners}).all()
    db.rollback()
    if limit:
        rows = rows[:limit]
    raw_by_num = fetch_source_rows([_recordnum(r) for r in rows], pace_s=socrata_pace_s)

    now = datetime.now(UTC).isoformat()
    plans: dict[str, dict] = {}
    counts: Counter = Counter()
    for r in rows:
        raw = raw_by_num.get(_recordnum(r))
        if raw is None:
            counts["not_in_source"] += 1  # left untouched, never guessed from the label
            continue
        ed = dict(r.ed)
        payload: dict = {"violation_category": (raw.get("recordtypedesc") or "").strip()[:_LABEL_MAX] or None}
        if ed.get("kc_pin_status") == "matched" and not ed.get("kc_pin_match"):
            loc = locate(ed.get("latitude"), ed.get("longitude"), r.property_address)
            same = loc.status == "matched" and loc.pin == ed.get("kc_pin")
            payload["kc_pin_match"] = loc.match if same else UNCONFIRMED
            time.sleep(gis_pace_s)
        counts[f"tier_{payload.get('kc_pin_match') or ed.get('kc_pin_match') or 'none'}"] += 1
        unnamed = not (r.party_name or "").strip()  # NULL or blank: nobody is named
        is_label = not unnamed and r.party_name == old_scraper_label(raw)
        if not is_label and not unnamed:
            counts["party_name_not_the_label_left_alone"] += 1
        ed.update(payload)
        plans[str(r.id)] = {"row": r, "payload": payload, "is_label": is_label,
                            "unnamed": unnamed, "pin": located_parcel_id(ed)}

    owner_names: dict[str, str] = {}
    pins = sorted({p["pin"] for p in plans.values()
                   if p["pin"] and (p["is_label"] or p["unnamed"])})
    counts["exact_pins_for_owner"] = len(pins)
    if owners and pins:
        owner_names = resolve_owners(pins, delay=owner_delay)
    counts["owners_found"] = len(owner_names)

    written = skipped = 0
    fh = report.open("w", encoding="utf-8") if report is not None else None
    try:
        for i, (rid, p) in enumerate(plans.items(), 1):
            r, payload = p["row"], dict(p["payload"])
            new_party = r.party_name
            owner = owner_names.get(p["pin"]) if p["pin"] else None
            if owner and (p["is_label"] or p["unnamed"]):
                new_party = owner.strip()[:512]
                payload.update({"owner_source": OWNER_SOURCE, "owner_pin": p["pin"],
                                "owner_checked_at": now})
                counts["named"] += 1
            elif p["is_label"]:
                new_party = None
                counts["label_cleared_no_owner"] += 1
            payload["cv_semantics_repaired_at"] = now
            if fh is not None:
                fh.write(json.dumps({"result_id": rid, "old_party_name": r.party_name,
                                     "new_party_name": new_party, **payload}) + "\n")
            if not apply_writes:
                continue
            res = db.execute(text(_UPDATE_SQL), {
                "new_party": new_party, "old_party": r.party_name, "rid": r.id,
                "uid": r.user_id, "payload": json.dumps(payload), "source": _SOURCE,
                # The owner and tier were decided for THIS location; a row re-located
                # since it was read is skipped, never given another parcel's owner.
                "old_pin": r.ed.get("kc_pin"), "old_pin_status": r.ed.get("kc_pin_status"),
                "old_pin_match": r.ed.get("kc_pin_match"),
                "old_pin_source": r.ed.get("kc_pin_source")})
            written += bool(res.rowcount)
            skipped += not res.rowcount
            if i % 200 == 0:
                db.commit()
        if apply_writes:
            db.commit()
    finally:
        if fh is not None:
            fh.close()

    stats = {"candidates": len(rows), "planned": len(plans), **dict(counts)}
    if apply_writes:
        stats["writes"] = {"written": written, "skipped_by_write_guard": skipped}
    return stats


def _refuse_private_redis() -> None:
    host = urlparse(os.environ.get("REDIS_URL", "")).hostname or ""
    if host.endswith(".railway.internal"):
        raise SystemExit("REDIS_URL is Railway's private host; off Railway the King source "
                         "lease fails open. Set REDIS_URL to the Redis public URL and retry.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--owners", action="store_true", help="look up owners on eRealProperty")
    ap.add_argument("--retry-owners", action="store_true",
                    help="also revisit repaired exact rows that still have no owner")
    ap.add_argument("--limit", type=int, help="only the first N candidates")
    ap.add_argument("--owner-delay", type=float, default=2.0, help="seconds between owner pages")
    ap.add_argument("--report", type=Path,
                    default=Path(f"king_cv_owner_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"))
    args = ap.parse_args(argv)
    if args.owners:
        _refuse_private_redis()
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        stats = run(db, apply_writes=args.apply, owners=args.owners,
                    retry_owners=args.retry_owners, limit=args.limit, report=args.report,
                    owner_delay=args.owner_delay)
    print(json.dumps(stats, indent=2))
    print(f"evidence -> {args.report}")
    if not args.apply:
        print("dry-run: nothing written (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
