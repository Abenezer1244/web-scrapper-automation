"""Audit: does every cached nts_notice still say what its OWN source PDF says?

Re-parses each stored notice's `source_url` with the parser that source's crawler uses
and diffs field-by-field against the row. Read-only — writes nothing.

This is how the 2026-09-04 "Test 8" audit found that 12 of 14 cached King rows carried a
ts_number belonging to a DIFFERENT notice in the same PDF and 2 carried a wrong
auction_date (one hiding a live 2026-09-18 sale as an expired 2026-06-26 one) — residue
of the split bug fixed in #195/#199, whose data repair covered Pierce and Snohomish but
never King. Run it after any parser change; the fix is
`scripts/repair_nts_ts_number.py --source <source> --retire-wrong-key --fields`.

Also reports notices PRESENT in a cached issue that were never stored at all — the other
half of the same bug (the old splitter dropped a PDF's last notice).

    railway run --service worker python scripts/diag_nts_stored_vs_source.py --source queen_anne_news
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COMPARED = ("ts_number", "auction_date", "principal_owing", "property_address_normalized")


def _parsers() -> dict:
    from src.scrapers.sources import nts_tacoma_index as nts
    from src.scrapers.sources.nts_king_pdf import parse_king_notice

    return {"queen_anne_news": parse_king_notice, "snohomish_tribune": nts.parse_nts_notice}


def _counties() -> dict:
    from src.scrapers.sources.nts_pdf_archive import ARCHIVE_SOURCES

    return {name: cfg["county"] for name, cfg in ARCHIVE_SOURCES.items()}


def _reparse(url: str, parse_fn, source: str, county: str) -> list[dict]:
    from datetime import UTC, datetime

    from src.scrapers.sources import nts_pdf
    from src.scrapers.sources import nts_tacoma_index as nts
    from src.utils.safe_http import safe_download_to_file
    from src.workers.nts_crawler import _MAX_PDF_BYTES, _PDF_BROWSER_UA

    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        safe_download_to_file(url, path, max_bytes=_MAX_PDF_BYTES, require_https=True,
                              headers={"User-Agent": _PDF_BROWSER_UA})
        with open(path, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    today = datetime.now(UTC).date()
    out = []
    norm = nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(data))
    for block in nts_pdf.split_notice_blocks(norm):
        row = nts.notice_to_row(parse_fn(block), source_url=url, today=today,
                                source=source, county=county)
        if row:
            out.append(row)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, choices=sorted(_parsers()))
    args = ap.parse_args()

    from sqlalchemy import text as t

    from src.db.session import system_sync_session
    from src.scrapers.sources.nts_matcher import _norm_parcel

    parse_fn = _parsers()[args.source]
    county = _counties()[args.source]

    with system_sync_session() as db:
        stored = [dict(r._mapping) for r in db.execute(t(
            """
            SELECT ts_number, parcel, auction_date, principal_owing,
                   property_address_normalized, is_active, source_url
            FROM nts_notices
            WHERE source = :src AND source_url IS NOT NULL AND source_url <> ''
            ORDER BY source_url, parcel
            """), {"src": args.source}).fetchall()]

    by_url: dict[str, list[dict]] = defaultdict(list)
    for row in stored:
        by_url[row["source_url"]].append(row)
    print(f"{len(stored)} cached {args.source} notices across {len(by_url)} source PDFs\n")

    mismatched = uncached = 0
    for url, rows in sorted(by_url.items()):
        print(f"=== {url.rsplit('/', 1)[-1]} ===")
        try:
            truth = _reparse(url, parse_fn, args.source, county)
        except Exception as exc:  # noqa: BLE001
            print(f"  re-parse FAILED: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        truth_by_parcel = {_norm_parcel(r.get("parcel")): r for r in truth}
        print(f"  stored={len(rows)} reparsed_now={len(truth)}")
        for row in rows:
            tr = truth_by_parcel.get(_norm_parcel(row["parcel"]))
            if tr is None:
                mismatched += 1
                print(f"  [{row['parcel']!r}] NOT FOUND in a fresh parse of its own issue")
                continue
            diffs = [f"{f}: stored={row.get(f)!r} truth={tr.get(f)!r}"
                     for f in COMPARED if str(row.get(f)) != str(tr.get(f))]
            if diffs:
                mismatched += 1
                print(f"  [{row['parcel']!r}] MISMATCH")
                for d in diffs:
                    print(f"        {d}")
        have = {_norm_parcel(r["parcel"]) for r in rows}
        for tr in truth:
            if _norm_parcel(tr.get("parcel")) not in have:
                uncached += 1
                print(f"  [{tr.get('parcel')!r}] IN THE PDF BUT NEVER CACHED "
                      f"ts={tr.get('ts_number')!r} auction={tr.get('auction_date')} "
                      f"owed={tr.get('principal_owing')}")
    print(f"\n>>> {mismatched}/{len(stored)} stored rows disagree with their own source; "
          f"{uncached} published notices were never cached")


if __name__ == "__main__":
    main()
