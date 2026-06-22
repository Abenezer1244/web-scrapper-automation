"""Fill REAL legal_description on Snohomish + Clark tax_delinquent leads from free,
no-auth county assessor sources — the honest source, mirroring the King bulk pattern
(scripts/backfill_king_bulk_legal_mailing.py).

PR #92 nulled the parcel-/document-number stand-ins these rows used to carry, so
legal_description is now NULL. This fills it with a real legal description pulled
per-parcel from the county's published assessor data:

- Snohomish: ArcGIS Hub "Assessor Roll CSV Collection" ZIP -> LegalDescr.csv. The
  legal is multi-line (one row per line_nr); we concatenate lines per parcel in order.
  Join key `parcel_number` is the 14-digit value we store in results.parcel_id (exact).
  Probed match rate vs our targets: ~99.95%.
- Clark: ArcGIS REST TaxlotsPublic/MapServer/0, field `LegalShort` (the county's own
  short/display legal — "incomplete, not for official documents", fine for a lead
  display). Join key `Prop_id` (int) == our numeric parcel_id. Reused the project's
  established ArcGIS query shape.

Safety (fail-closed, per review):
- DRY-RUN by default; writes require --apply.
- Exact normalized parcel match only — NO fuzzy matching. Snohomish keys must be the
  14-digit parcel; Clark keys must be all-numeric Prop_id.
- A min-match-rate guard ABORTS before any write if the source/target match rate is
  implausibly low (a sign the source schema or parcel format drifted).
- Update is keyed by result `id` AND re-asserts `parcel_id` AND `legal_description IS
  NULL` — fill-missing only, never clobbers a real legal, idempotent.
- Provenance recorded in enrichment_data (source + field + truncated flag) so the
  origin (and Clark's truncation) is self-documenting.
- Scoped to {county}/WA/tax_delinquent (parcel ids can collide across county/type).

Usage:
  python scripts/backfill_tax_legal_from_assessor.py --county snohomish            # dry-run
  python scripts/backfill_tax_legal_from_assessor.py --county snohomish --apply
  python scripts/backfill_tax_legal_from_assessor.py --county clark --apply --batch 2000
"""
import argparse
import csv
import io
import json
import logging
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from sqlalchemy import text

from src.db.session import SyncSessionLocal

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("backfill_tax_legal")

_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}

# Snohomish Assessor Roll CSV Collection (ArcGIS Online item -> Assessor_Roll_csv.zip).
_SNOHO_ZIP_URL = (
    "https://www.arcgis.com/sharing/rest/content/items/"
    "ee76dfa5905947cc9af1605a25cf216a/data"
)
# Clark public taxlots ArcGIS REST layer (no auth).
_CLARK_REST = (
    "https://gis.clark.wa.gov/arcgisfed/rest/services/"
    "ClarkView_Public/TaxlotsPublic/MapServer/0/query"
)

# Per-county minimum acceptable match rate. These are real migrations against sources
# we probed at ~99.95% (Snohomish) / ~97% (Clark); a low rate means the source schema
# or parcel format drifted, so we ABORT before writing rather than half-fill. Override
# with --min-match-rate only deliberately.
_MIN_MATCH_RATE = {"snohomish": 0.99, "clark": 0.95}

_PROVENANCE = {
    "snohomish": {
        "legal_description_source": "snohomish_assessor_roll",
        "legal_description_field": "LegalDescr.legal_desc_line",
    },
    "clark": {
        "legal_description_source": "clark_taxlots_public",
        "legal_description_field": "LegalShort",
        "legal_description_truncated": True,
    },
}


def _norm_ws(s: str | None) -> str:
    """Collapse all runs of whitespace to single spaces; trim."""
    return " ".join((s or "").split())


def _target_parcels(db, county: str) -> set[str]:
    rows = db.execute(
        text(
            """
            SELECT DISTINCT r.parcel_id
            FROM results r
            JOIN jobs j ON j.id = r.job_id
            JOIN scraper_configs sc ON sc.id = j.scraper_config_id
            WHERE lower(sc.county) = :county AND upper(sc.state) = 'WA'
              AND sc.record_type = 'tax_delinquent'
              AND r.parcel_id IS NOT NULL
              AND r.legal_description IS NULL
            """
        ),
        {"county": county},
    )
    return {r.parcel_id.strip() for r in rows if r.parcel_id and r.parcel_id.strip()}


def _build_snoho_map(targets: set[str], cache_dir: str) -> dict[str, str]:
    """parcel_number(14) -> concatenated legal, for target parcels only."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "snoho_assessor_roll.zip")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        _log.info("using cached %s (%d bytes)", path, os.path.getsize(path))
        blob = open(path, "rb").read()
    else:
        _log.info("downloading Snohomish assessor roll ...")
        r = requests.get(_SNOHO_ZIP_URL, headers=_HEADERS, timeout=300)
        r.raise_for_status()
        blob = r.content
        open(path, "wb").write(blob)
        _log.info("downloaded %d bytes -> %s", len(blob), path)

    z = zipfile.ZipFile(io.BytesIO(blob))
    member = next((n for n in z.namelist() if n.lower() == "legaldescr.csv"), None)
    if not member:
        raise ValueError(f"LegalDescr.csv not in zip: {z.namelist()}")

    # parcel -> list[(line_nr:int, text)]; only keep target parcels (bounded memory).
    lines: dict[str, list[tuple[int, str]]] = {}
    scanned = 0
    with z.open(member) as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="latin-1")):
            scanned += 1
            pn = (row.get("parcel_number") or "").strip()
            if pn not in targets:
                continue
            txt = row.get("legal_desc_line") or ""
            try:
                ln = int((row.get("line_nr") or "0").strip())
            except ValueError:
                ln = 0
            try:
                rid = int((row.get("ID") or "0").strip())
            except ValueError:
                rid = 0
            # (line_nr, ID) is a deterministic order even if line_nr repeats/malforms.
            lines.setdefault(pn, []).append((ln, rid, txt))
    _log.info("LegalDescr.csv scanned %d rows; %d target parcels have legal lines", scanned, len(lines))

    out: dict[str, str] = {}
    for pn, parts in lines.items():
        legal = _norm_ws(" ".join(t for _, _, t in sorted(parts, key=lambda x: (x[0], x[1]))))
        if legal:
            out[pn] = legal
    return out


def _build_clark_map(targets: set[str]) -> dict[str, str]:
    """Prop_id(int) -> LegalShort, for target parcels only (batched ArcGIS IN query)."""
    # Strict: Clark Prop_id is integer — drop anything non-numeric so a bad key can
    # never match the wrong parcel.
    clean = sorted(t for t in targets if t.isdigit())
    dropped = len(targets) - len(clean)
    if dropped:
        _log.warning("Clark: dropped %d non-numeric target parcel ids", dropped)
    out: dict[str, str] = {}
    chunk = 50
    for i in range(0, len(clean), chunk):
        batch = clean[i:i + chunk]
        inc = ",".join(batch)
        params = {
            "where": f"Prop_id IN ({inc})",
            "outFields": "Prop_id,LegalShort",
            "returnGeometry": "false",
            "f": "json",
        }
        try:
            r = requests.get(_CLARK_REST, params=params, headers=_HEADERS, timeout=30)
            r.raise_for_status()
            feats = r.json().get("features") or []
        except Exception as exc:
            # FATAL: a dropped batch would silently under-fill (and could still pass
            # the match-rate guard). Abort the whole run — no writes have happened yet
            # (map is built fully before _apply). Re-run to retry.
            raise RuntimeError(
                f"Clark REST batch {i // chunk} failed ({str(exc)[:120]}); aborting before any write"
            ) from exc
        for feat in feats:
            a = feat.get("attributes") or {}
            raw = a.get("Prop_id")
            pid = str(raw).strip() if raw is not None else ""
            legal = _norm_ws(a.get("LegalShort"))
            if pid in targets and legal:
                out[pid] = legal
    return out


def _apply(db_factory, county: str, legal_map: dict[str, str], batch: int, apply: bool) -> tuple[int, int]:
    """Page over results rows; fill legal_description where NULL + matched. Returns
    (rows_seen, rows_filled)."""
    prov = json.dumps(_PROVENANCE[county])
    select_sql = text(
        """
        SELECT r.id, r.parcel_id
        FROM results r
        JOIN jobs j ON j.id = r.job_id
        JOIN scraper_configs sc ON sc.id = j.scraper_config_id
        WHERE r.id > CAST(:last_id AS uuid)
          AND lower(sc.county) = :county AND upper(sc.state) = 'WA'
          AND sc.record_type = 'tax_delinquent'
          AND r.parcel_id IS NOT NULL
          AND r.legal_description IS NULL
        ORDER BY r.id LIMIT :batch
        """
    )
    last_id = "00000000-0000-0000-0000-000000000000"
    seen = filled = 0
    while True:
        with db_factory() as db:
            db.execute(text("SET statement_timeout=120000"))
            rows = db.execute(
                select_sql, {"last_id": last_id, "county": county, "batch": batch}
            ).fetchall()
            if not rows:
                break
            last_id = str(rows[-1].id)
            triples: list[tuple[str, str, str]] = []  # (id, pin, legal)
            for row in rows:
                seen += 1
                pin = row.parcel_id.strip()
                legal = legal_map.get(pin)
                if legal:
                    triples.append((str(row.id), pin, legal))
            if triples and apply:
                vsql = ",".join(f"(:id_{i}, :pin_{i}, :lg_{i})" for i in range(len(triples)))
                p: dict = {"prov": prov}
                for i, (rid, pin, lg) in enumerate(triples):
                    p[f"id_{i}"], p[f"pin_{i}"], p[f"lg_{i}"] = rid, pin, lg
                res = db.execute(
                    text(
                        f"""
                        UPDATE results SET
                          legal_description = data.legal,
                          enrichment_data = (
                            COALESCE(results.enrichment_data::jsonb, '{{}}'::jsonb)
                            || CAST(:prov AS jsonb)
                          )::json
                        FROM (VALUES {vsql}) AS data(id, pin, legal)
                        WHERE results.id = data.id::uuid
                          AND results.parcel_id = data.pin
                          AND results.legal_description IS NULL
                        """  # noqa: S608 — vsql is fixed placeholders, values are bound params
                    ),
                    p,
                )
                db.commit()
                applied = res.rowcount or 0
                filled += applied
                if applied < len(triples):
                    # A guard (legal already filled by another run/process) skipped some.
                    _log.info("  (%d of %d matched rows already non-NULL — skipped)",
                              len(triples) - applied, len(triples))
            else:
                filled += len(triples)
            _log.info(
                "%sscanned %d | %s %d — through %s",
                "[DRY-RUN] " if not apply else "",
                seen, "would-fill" if not apply else "filled", filled, last_id,
            )
    return seen, filled


def run(county: str, batch: int, apply: bool, cache_dir: str, min_match_rate: float | None) -> None:
    with SyncSessionLocal() as db:
        db.execute(text("SET statement_timeout=120000"))
        targets = _target_parcels(db, county)
    _log.info("%s: %d distinct target parcels (tax_delinquent, legal NULL)", county, len(targets))
    if not targets:
        _log.info("nothing to do (no NULL-legal target parcels)")
        return

    if county == "snohomish":
        legal_map = _build_snoho_map(targets, cache_dir)
    else:
        legal_map = _build_clark_map(targets)

    matched = len(legal_map)
    rate = matched / len(targets) if targets else 0.0
    unmatched = len(targets) - matched
    threshold = min_match_rate if min_match_rate is not None else _MIN_MATCH_RATE[county]
    _log.info("source legal found for %d/%d target parcels (%.1f%%); unmatched %d (min %.1f%%)",
              matched, len(targets), rate * 100, unmatched, threshold * 100)
    if rate < threshold:
        _log.error(
            "ABORT: match rate %.1f%% < min %.1f%% — likely a source schema/parcel-format "
            "drift. No writes.", rate * 100, threshold * 100,
        )
        sys.exit(2)

    seen, filled = _apply(SyncSessionLocal, county, legal_map, batch, apply)
    _log.info(
        "%sDONE [%s] — rows scanned %d, legal %s %d",
        "[DRY-RUN] " if not apply else "",
        county, seen, "would-fill" if not apply else "filled", filled,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", required=True, choices=("snohomish", "clark"))
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--min-match-rate", type=float, default=None,
                    help="override the per-county abort threshold (0-1); default: "
                         "snohomish 0.99, clark 0.95")
    ap.add_argument("--cache", default=os.path.join(tempfile.gettempdir(), "snoho_bulk"))
    a = ap.parse_args()
    if a.batch < 1:
        ap.error("--batch must be >= 1")
    run(a.county, a.batch, a.apply, a.cache, a.min_match_rate)
