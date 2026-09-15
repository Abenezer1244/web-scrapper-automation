"""Give existing Tacoma (Pierce) code-violation leads their real semantics: category, owner.

Before 2026-09-14 the Tacoma scraper wrote the case label into party_name
("Nuisance - 1603 N ALDER ST") and stored the case type only as `case_type`. For every
stored lead in a DONE job this script:

  1. Re-reads the case from the Tacoma source by its case number (authoritative; the
     label is never parsed) and stores `violation_category`.
  2. With --owners, looks up the parcel's taxpayer through the same validated Pierce
     ATIP path the live pass uses (pierce_atip_owner: echoed parcel, real property, not
     a reference parcel, same situs; paced, lease-guarded, PIERCE_CV_OWNER_ENABLED).
  3. Replaces party_name ONLY when it still equals the label the old scraper built from
     that same source row: with the owner when accepted, otherwise with NULL (the label
     is not a party). A blank party_name is filled only with an accepted owner. Any
     other party_name is left alone and reported.

Owner decision 2026-09-14: ATIP taxpayer names may be stored for code-violation owner
naming only. Guarded single-row UPDATEs (same user, same party_name and parcel as read,
no owner yet, job still done). No parcel_id, dedup, mailing, billing, quota, skip trace
or delivery change. Idempotent: a repaired row carries `cv_semantics_repaired_at`;
--retry-owners revisits repaired rows still unnamed with no owner_status.

    railway run --service worker python scripts/backfill_pierce_code_violation_owner.py              # dry-run
    railway run --service worker python scripts/backfill_pierce_code_violation_owner.py --owners --apply

Owner lookups need the Pierce owner lease in the SAME Redis as the workers; off Railway
`railway run` injects the private Redis host, so point REDIS_URL at the public URL first
(the script refuses otherwise).
"""
from __future__ import annotations

import argparse
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

_SOURCE = "tacoma_code_violations"
_FEATURE_URL = (
    "https://services3.arcgis.com/SCwJH1pD8WSn5T5y/arcgis/rest/services"
    "/Code%20Violations/FeatureServer/0/query"
)
_BATCH = 100
_MAX_SOURCE_PAGES = 50
# Parcels per owner-lookup chunk: each chunk takes and releases the Pierce owner lease
# and commits its rows, so the live pass and the sweep are never locked out for long.
_OWNER_CHUNK = 20
_CASENUMBER_RE = re.compile(r"^[0-9A-Za-z-]{1,32}$")

_CANDIDATES_SQL = """
    SELECT r.id, r.user_id, r.job_id, r.party_name, r.parcel_id, r.property_address,
           r.legal_description, r.enrichment_data::jsonb AS ed
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'pierce' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'code_violation'
      AND j.status = 'done' AND j.user_id = r.user_id AND sc.user_id = r.user_id
      AND jsonb_typeof(r.enrichment_data::jsonb) = 'object'
      AND r.enrichment_data::jsonb->>'source' = :source
      AND NOT (r.enrichment_data::jsonb ? 'owner_source')
      AND (NOT (r.enrichment_data::jsonb ? 'cv_semantics_repaired_at')
           OR (:retry_owners AND coalesce(btrim(r.party_name), '') = ''
               AND NOT (r.enrichment_data::jsonb ? 'owner_status')))
    ORDER BY r.id
"""

_UPDATE_SQL = """
    UPDATE results SET
      party_name = CAST(:new_party AS varchar),
      enrichment_data = (enrichment_data::jsonb || CAST(:payload AS jsonb))::json
    WHERE id = :rid AND user_id = :uid AND job_id = :jid
      AND party_name IS NOT DISTINCT FROM CAST(:old_party AS varchar)
      AND parcel_id IS NOT DISTINCT FROM CAST(:old_parcel AS varchar)
      AND property_address IS NOT DISTINCT FROM CAST(:old_address AS varchar)
      AND legal_description IS NOT DISTINCT FROM CAST(:old_legal AS varchar)
      AND jsonb_typeof(enrichment_data::jsonb) = 'object'
      -- The same case key the source row was fetched by (_case_number: enrichment
      -- case_number, else legal_description), so a legacy row is not silently skipped.
      AND COALESCE(NULLIF(btrim(enrichment_data::jsonb->>'case_number'), ''),
                   btrim(legal_description)) = CAST(:old_case AS text)
      AND enrichment_data::jsonb->>'source' = :source
      AND NOT (enrichment_data::jsonb ? 'owner_source')
      AND (NOT CAST(:writes_owner_status AS boolean)
           OR NOT (enrichment_data::jsonb ? 'source_parcel')
           OR enrichment_data::jsonb->>'source_parcel' = btrim(parcel_id))
      AND (NOT CAST(:writes_owner_status AS boolean)
           OR NOT (enrichment_data::jsonb ? 'owner_status'))
      AND EXISTS (
        SELECT 1 FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id
        WHERE j.id = results.job_id AND j.user_id = results.user_id
          AND sc.user_id = results.user_id AND j.status = 'done'
          AND lower(sc.county) = 'pierce' AND upper(sc.state) = 'WA'
          AND sc.record_type = 'code_violation')
"""


def old_scraper_label(raw: dict) -> str:
    """The party_name the pre-2026-09-14 scraper built from this Tacoma row."""
    case_type = (raw.get("casetype") or "").strip()
    address = (raw.get("address") or "").strip()
    return f"{case_type} - {address}" if address else case_type


def fetch_source_rows(case_numbers: list[str], *, pace_s: float = 1.0) -> dict[str, dict]:
    """{casenumber: raw Tacoma row} for the given cases, in paced batches."""
    from src.api.middleware.security import add_scrape_domain
    from src.config import settings
    from src.utils.safe_http import safe_get

    add_scrape_domain("services3.arcgis.com")
    wanted = sorted({n for n in case_numbers if n and _CASENUMBER_RE.match(n)})
    out: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for i in range(0, len(wanted), _BATCH):
        chunk = wanted[i:i + _BATCH]
        quoted = ",".join(f"'{n}'" for n in chunk)  # validated by _CASENUMBER_RE above
        offset = 0
        for _page in range(_MAX_SOURCE_PAGES):
            resp = safe_get(_FEATURE_URL, params={
                "where": f"casenumber IN ({quoted})",
                "outFields": "objectid,casenumber,casetype,address,parcelnumber",
                "orderByFields": "objectid ASC", "resultOffset": offset,
                "resultRecordCount": _BATCH, "f": "json"},
                headers={"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"},
                timeout=settings.DEFAULT_TIMEOUT)
            resp.raise_for_status()  # a failed batch aborts the run; nothing half-applied
            data = resp.json()
            if not isinstance(data, dict) or "error" in data or "features" not in data:
                raise RuntimeError(f"Tacoma layer returned an error body: {str(data)[:160]}")
            features = data["features"]
            for feat in features:
                attrs = (feat or {}).get("attributes") or {}
                num = str(attrs.get("casenumber") or "")
                if num not in chunk:
                    continue
                prev = out.get(num)
                if prev is not None and (old_scraper_label(prev) != old_scraper_label(attrs)
                                         or prev.get("parcelnumber") != attrs.get("parcelnumber")):
                    # Two different features for one case: neither the label nor the
                    # parcel can be rebuilt with certainty, so its rows are left untouched.
                    ambiguous.add(num)
                out[num] = attrs
            time.sleep(pace_s)
            # Every page of every chunk, so a duplicate on a later page is still seen.
            if not data.get("exceededTransferLimit") and len(features) < _BATCH:
                break
            offset += len(features) or _BATCH
        else:
            raise RuntimeError("Tacoma layer kept paging past the page guard; aborting")
    for num in ambiguous:
        del out[num]
    return out


def _case_number(row) -> str:
    return str(row.ed.get("case_number") or row.legal_description or "").strip()


def run(db, *, apply_writes: bool, owners: bool, retry_owners: bool = False,
        limit: int | None = None, report: Path | None = None,
        source_pace_s: float = 1.0) -> dict:
    from src.scrapers.enrichment.pierce_atip_owner import lookup_parcels, normalize_parcel

    rows = db.execute(text(_CANDIDATES_SQL),
                      {"source": _SOURCE, "retry_owners": retry_owners}).all()
    db.rollback()  # release the read snapshot before any network I/O
    if limit:
        rows = rows[:limit]
    raw_by_num = fetch_source_rows([_case_number(r) for r in rows], pace_s=source_pace_s)

    now = datetime.now(UTC).isoformat()
    counts: Counter = Counter()
    plans: list[dict] = []
    for r in rows:
        raw = raw_by_num.get(_case_number(r))
        if raw is None:
            counts["not_in_source"] += 1  # left untouched, never guessed from the label
            continue
        unnamed = not (r.party_name or "").strip()
        is_label = not unnamed and r.party_name == old_scraper_label(raw)
        if not is_label and not unnamed:
            counts["party_name_not_the_label_left_alone"] += 1
        parcel = normalize_parcel(r.parcel_id)
        # The owner is looked up only for the parcel the Tacoma case itself names: a row
        # whose stored parcel no longer matches its source case is never named (Codex r5).
        same_parcel = (parcel is not None and normalize_parcel(raw.get("parcelnumber")) == parcel
                       # A row that recorded its scrape-time parcel must still be on it
                       # (Codex r10); only legacy rows without the key rely on the source.
                       and ("source_parcel" not in r.ed or r.ed.get("source_parcel") == parcel))
        if (is_label or unnamed) and not same_parcel:
            counts["parcel_differs_from_source_no_owner"] += 1
        # A row the live pass or sweep already decided (owner_status present) still gets
        # its category, but is never re-asked: the write guard would refuse its answer.
        wants_owner = (is_label or unnamed) and same_parcel and "owner_status" not in r.ed
        plans.append({"row": r, "is_label": is_label, "wants_owner": wants_owner,
                      "parcel": parcel,
                      "payload": {"violation_category": (raw.get("casetype") or "").strip() or None}})

    parcels = sorted({p["parcel"] for p in plans if p["wants_owner"]})
    counts["parcels_for_owner"] = len(parcels)
    # Chunks of parcels; rows that need no lookup go in the first chunk's writes.
    chunks = [parcels[i:i + _OWNER_CHUNK] for i in range(0, len(parcels), _OWNER_CHUNK)] or [[]]
    by_chunk: dict[int, list] = {}
    chunk_of = {pid: n for n, chunk in enumerate(chunks) for pid in chunk}
    for p in plans:
        by_chunk.setdefault(chunk_of.get(p["parcel"], 0) if p["wants_owner"] else 0, []).append(p)

    written = skipped = 0
    fh = report.open("w", encoding="utf-8") if report is not None else None
    try:
        for n, chunk in enumerate(chunks):
            fetched: dict = {}
            if owners and chunk:
                lookup_stats: dict = {}
                fetched = lookup_parcels(chunk, source=_SOURCE, stats=lookup_stats)
                counts["lookup_outcome_" + str(lookup_stats.get("outcome"))] += 1
            w, s = _write_plans(db, by_chunk.get(n, []), fetched, counts, fh,
                                apply_writes=apply_writes, now=now)
            written += w
            skipped += s
    finally:
        if fh is not None:
            fh.close()

    stats = {"candidates": len(rows), "planned": len(plans), **dict(counts)}
    if apply_writes:
        stats["writes"] = {"written": written, "skipped_by_write_guard": skipped}
    return stats


def _write_plans(db, plans: list[dict], fetched: dict, counts: Counter, fh, *,
                 apply_writes: bool, now: str) -> tuple[int, int]:
    """Decide and write one chunk's rows; commits the chunk. Returns (written, skipped)."""
    from src.scrapers.enrichment.pierce_atip_owner import MATCHED, decide, owner_payload

    written = skipped = 0
    for p in plans:
        r, payload = p["row"], dict(p["payload"])
        new_party = r.party_name
        f = fetched.get(p["parcel"]) if p["parcel"] else None
        writes_owner_status = False
        if f is not None and p["wants_owner"]:
            d = decide(p["parcel"], f, r.property_address, source=r.ed.get("source"))
            payload.update(owner_payload(p["parcel"], d, now))
            writes_owner_status = True
            counts[f"owner_{d.status}"] += 1
            if d.status == MATCHED:
                new_party = d.name
                counts["named"] += 1
        if new_party == r.party_name and p["is_label"]:
            new_party = None
            counts["label_cleared_no_owner"] += 1
        payload["cv_semantics_repaired_at"] = now
        if fh is not None:
            # Evidence file: the decision, never the taxpayer name.
            fh.write(json.dumps({"result_id": str(r.id), "parcel_id": r.parcel_id,
                                 "was_label": p["is_label"],
                                 "named": new_party is not None and new_party != r.party_name,
                                 **payload}) + "\n")
        if not apply_writes:
            continue
        res = db.execute(text(_UPDATE_SQL), {
            "new_party": new_party, "old_party": r.party_name, "old_parcel": r.parcel_id,
            "old_address": r.property_address, "rid": r.id, "uid": r.user_id, "jid": r.job_id,
            # The source row was fetched for THIS case; a row re-pointed since is skipped.
            "old_legal": r.legal_description, "old_case": _case_number(r),
            "payload": json.dumps(payload), "source": _SOURCE,
            "writes_owner_status": writes_owner_status})
        written += bool(res.rowcount)
        skipped += not res.rowcount
    if apply_writes:
        db.commit()
    return written, skipped


def _refuse_private_redis() -> None:
    host = urlparse(os.environ.get("REDIS_URL", "")).hostname or ""
    if host.endswith(".railway.internal"):
        raise SystemExit("REDIS_URL is Railway's private host; off Railway the Pierce owner "
                         "lease cannot be shared with the workers. Set REDIS_URL to the Redis "
                         "public URL and retry.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--owners", action="store_true", help="look up owners on Pierce ATIP")
    ap.add_argument("--retry-owners", action="store_true",
                    help="also revisit repaired rows that still have no owner or owner_status")
    ap.add_argument("--limit", type=int, help="only the first N candidates")
    ap.add_argument("--report", type=Path,
                    default=Path(f"pierce_cv_owner_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"))
    args = ap.parse_args(argv)
    if args.owners:
        from src.config import settings

        if not settings.PIERCE_CV_OWNER_ENABLED:
            raise SystemExit("--owners needs PIERCE_CV_OWNER_ENABLED=true; nothing was looked up.")
        _refuse_private_redis()
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        stats = run(db, apply_writes=args.apply, owners=args.owners,
                    retry_owners=args.retry_owners, limit=args.limit, report=args.report)
    print(json.dumps(stats, indent=2))
    print(f"evidence -> {args.report}")
    if not args.apply:
        print("dry-run: nothing written (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
