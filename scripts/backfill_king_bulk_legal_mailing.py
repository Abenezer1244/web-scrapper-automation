"""Bulk backfill King tax-delinquent legal_description + mailing_address from the
King County Assessor data downloads — NO per-parcel eRealProperty scraping.

Why: King rate-limits the per-parcel eRealProperty page, so the owner-name backfill
is slow/throttled. But the Assessor publishes FREE bulk extracts that carry the two
OTHER missing fields for every parcel at once (the bulk files redact the taxpayer
NAME — RCW 42.56.070 — so owner name is NOT available here and stays on the
per-parcel / daily-scrape path):

  Parcel.zip  -> EXTR_Parcel.csv         : parcel -> plat/lot/block or section/twn/range
                                           => a REAL legal description (replaces the
                                           parcel-number stand-in this scraper writes)
  Real Property Account.zip -> EXTR_RPAcct_NoName.csv : parcel -> mailing address
                                           (AddrLine/CityState/ZipCode), NO name

Parcel key = Major(6) + Minor(4) = the 10-digit PIN our results.parcel_id stores
(king_wa_tax_delinquent stores acct[:10] = Major+Minor).

Idempotent + re-runnable:
  - legal_description is overwritten ONLY when it is still the parcel stand-in
    (is_parcel_legal_placeholder) and a real legal was constructed — never clobbers
    a real legal.
  - mailing_address is FILL-MISSING (only when currently NULL) — never clobbers a
    mailing already enriched (e.g. from payment.kingcounty.gov).
Scope: results joined to scraper_configs WHERE king/WA/tax_delinquent, parcel_id set.

Usage:
  python scripts/backfill_king_bulk_legal_mailing.py --dry-run
  python scripts/backfill_king_bulk_legal_mailing.py
  python scripts/backfill_king_bulk_legal_mailing.py --batch 2000 --cache /tmp/king_bulk
"""
import argparse
import csv
import io
import logging
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.scrapers.king_wa_tax_delinquent import is_parcel_legal_placeholder

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("backfill_king_bulk")

_BASE = "https://aqua.kingcounty.gov/extranet/assessor"
_PARCEL_ZIP = f"{_BASE}/Parcel.zip"
_RPACCT_ZIP = f"{_BASE}/Real%20Property%20Account.zip"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}

_TARGET_SQL = text(
    """
    SELECT DISTINCT r.parcel_id
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE r.parcel_id IS NOT NULL
      AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'tax_delinquent'
    """
)


def _pin(major: str, minor: str) -> str:
    """Major(6)+Minor(4) = 10-digit PIN, matching results.parcel_id.

    Returns "" for malformed (non-numeric / over-length) parts so a garbage key
    can never match a target parcel.
    """
    m = (major or "").strip()
    n = (minor or "").strip()
    if not (m.isdigit() and n.isdigit()) or len(m) > 6 or len(n) > 4:
        return ""
    return f"{m.zfill(6)}{n.zfill(4)}"


def _download(url: str, cache_dir: str) -> bytes:
    """Download a zip, caching to disk so re-runs don't re-fetch (~50 MB total)."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, os.path.basename(url).replace("%20", "_"))
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        _log.info("using cached %s (%d bytes)", path, os.path.getsize(path))
        return open(path, "rb").read()
    _log.info("downloading %s ...", url)
    r = requests.get(url, headers=_HEADERS, timeout=300)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    _log.info("downloaded %d bytes -> %s", len(r.content), path)
    return r.content


def _csv_rows(zip_bytes: bytes):
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    csvs = [n for n in z.namelist() if n.lower().endswith(".csv")]
    if not csvs:
        raise ValueError(f"no .csv member in zip: {z.namelist()}")
    with z.open(csvs[0]) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="latin-1"))


def _build_legal(row: dict) -> str | None:
    """Construct a real legal description from Parcel.csv components.

    Platted: "<PlatName> LOT <n> BLK <n>". Unplatted: quarter/section/township/range.
    Returns None when no components are present (nothing better than the stand-in).
    """
    plat = (row.get("PlatName") or "").strip()
    lot = (row.get("PlatLot") or "").strip()
    block = (row.get("PlatBlock") or "").strip()
    if plat:
        parts = [plat]
        if lot:
            parts.append(f"LOT {lot}")
        if block:
            parts.append(f"BLK {block}")
        return " ".join(parts)
    qtr = (row.get("QuarterSection") or "").strip()
    sec = (row.get("Section") or "").strip()
    twn = (row.get("Township") or "").strip()
    rng = (row.get("Range") or "").strip()
    seg = []
    if qtr:
        seg.append(qtr)
    if sec:
        seg.append(f"SEC {sec}")
    if twn:
        seg.append(f"TWN {twn}")
    if rng:
        seg.append(f"RNG {rng}")
    return " ".join(seg) if seg else None


def _build_mailing(row: dict) -> str | None:
    """Construct mailing address from RPAcct.csv (AddrLine, CityState, ZipCode)."""
    addr = " ".join((row.get("AddrLine") or "").split())
    if not addr:
        return None
    citystate = " ".join((row.get("CityState") or "").split())
    zipc = (row.get("ZipCode") or "").strip()
    tail = " ".join(f"{citystate} {zipc}".split())
    return f"{addr}, {tail}" if tail else addr


def run(batch: int, dry_run: bool, cache_dir: str) -> None:
    # 1) target parcels (only keep CSV rows we actually need -> small maps)
    with SyncSessionLocal() as db:
        targets = {row.parcel_id.strip() for row in db.execute(_TARGET_SQL) if row.parcel_id}
    _log.info("target King tax_delinquent parcels: %d", len(targets))
    if not targets:
        return

    # 2) build parcel -> legal from Parcel.zip
    legal_map: dict[str, str] = {}
    scanned = 0
    for row in _csv_rows(_download(_PARCEL_ZIP, cache_dir)):
        scanned += 1
        pin = _pin(row.get("Major", ""), row.get("Minor", ""))
        if pin in targets:
            lg = _build_legal(row)
            if lg:
                legal_map[pin] = lg
    _log.info("Parcel.csv scanned %d rows; legal for %d/%d targets", scanned, len(legal_map), len(targets))

    # 3) build parcel -> mailing from Real Property Account.zip (first non-empty wins)
    mail_map: dict[str, str] = {}
    scanned = 0
    for row in _csv_rows(_download(_RPACCT_ZIP, cache_dir)):
        scanned += 1
        pin = _pin(row.get("Major", ""), row.get("Minor", ""))
        if pin in targets and pin not in mail_map:
            ml = _build_mailing(row)
            if ml:
                mail_map[pin] = ml
    _log.info("RPAcct.csv scanned %d rows; mailing for %d/%d targets", scanned, len(mail_map), len(targets))

    # 4) apply, paged over results rows. legal = swap stand-in only; mailing =
    #    fill-missing only. Both guarded in the UPDATE WHERE for idempotency.
    last_id = "00000000-0000-0000-0000-000000000000"
    seen = legal_upd = mail_upd = 0
    select_sql = text(
        """
        SELECT r.id, r.parcel_id, r.party_name, r.legal_description,
               (r.mailing_address IS NULL) AS need_mail
        FROM results r
        JOIN jobs j ON j.id = r.job_id
        JOIN scraper_configs sc ON sc.id = j.scraper_config_id
        WHERE r.id > CAST(:last_id AS uuid)
          AND r.parcel_id IS NOT NULL
          AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
          AND sc.record_type = 'tax_delinquent'
        ORDER BY r.id LIMIT :batch
        """
    )
    while True:
        with SyncSessionLocal() as db:
            rows = db.execute(select_sql, {"last_id": last_id, "batch": batch}).fetchall()
            if not rows:
                break
            last_id = str(rows[-1].id)
            legal_triples: list[tuple[str, str, str]] = []   # (id, new_legal, pin)
            mail_pairs: list[tuple[str, str]] = []           # (id, mailing)
            for row in rows:
                seen += 1
                pin = row.parcel_id.strip()
                new_legal = legal_map.get(pin)
                if new_legal and is_parcel_legal_placeholder(row.legal_description, pin):
                    legal_triples.append((str(row.id), new_legal, pin))
                if row.need_mail and pin in mail_map:
                    mail_pairs.append((str(row.id), mail_map[pin]))

            if not dry_run and legal_triples:
                vsql = ",".join(f"(:id_{i}, :new_{i}, :pin_{i})" for i in range(len(legal_triples)))
                p: dict = {}
                for i, (rid, new, pin) in enumerate(legal_triples):
                    p[f"id_{i}"], p[f"new_{i}"], p[f"pin_{i}"] = rid, new, pin
                # Re-assert the stand-in shape in SQL (legal_description == parcel_id
                # == pin) so the swap is idempotent and self-evidently only replaces
                # the placeholder, never a real legal description.
                res = db.execute(
                    text(
                        f"""
                        UPDATE results SET legal_description = data.new
                        FROM (VALUES {vsql}) AS data(id, new, pin)
                        WHERE results.id = data.id::uuid
                          AND results.parcel_id = data.pin
                          AND results.legal_description = data.pin
                        """
                    ),
                    p,
                )
                legal_upd += res.rowcount or 0
            else:
                legal_upd += len(legal_triples)

            if not dry_run and mail_pairs:
                vsql = ",".join(f"(:id_{i}, :m_{i})" for i in range(len(mail_pairs)))
                p = {}
                for i, (rid, m) in enumerate(mail_pairs):
                    p[f"id_{i}"], p[f"m_{i}"] = rid, m
                res = db.execute(
                    text(
                        f"""
                        UPDATE results SET mailing_address = data.m
                        FROM (VALUES {vsql}) AS data(id, m)
                        WHERE results.id = data.id::uuid
                          AND results.mailing_address IS NULL
                        """
                    ),
                    p,
                )
                mail_upd += res.rowcount or 0
            else:
                mail_upd += len(mail_pairs)

            if not dry_run:
                db.commit()
            _log.info(
                "%sscanned %d | legal %s %d | mailing %s %d — through %s",
                "[DRY-RUN] " if dry_run else "",
                seen,
                "would-set" if dry_run else "set", legal_upd,
                "would-set" if dry_run else "set", mail_upd,
                last_id,
            )

    _log.info(
        "%sDONE — rows scanned %d, legal %s %d, mailing %s %d",
        "[DRY-RUN] " if dry_run else "",
        seen,
        "would-set" if dry_run else "set", legal_upd,
        "would-set" if dry_run else "set", mail_upd,
    )
    if not dry_run and mail_upd:
        _log.warning(
            "Mailing addresses changed — run `python scripts/backfill_owner_flags.py` "
            "next to refresh owner-location flags (absentee_owner / out_of_state_owner / "
            "owner_state), which lead filters depend on and which this script does not touch."
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cache", default=os.path.join(tempfile.gettempdir(), "king_bulk"))
    a = ap.parse_args()
    run(a.batch, a.dry_run, a.cache)
