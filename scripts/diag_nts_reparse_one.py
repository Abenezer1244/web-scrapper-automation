"""Read-only: re-parse ONE cached notice from its own source_url and show every field.

    railway run --service worker python scripts/diag_nts_reparse_one.py <ts_number> [--source S]
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ts_number")
    ap.add_argument("--source", default="tacoma_daily_index")
    args = ap.parse_args()

    from sqlalchemy import text as t

    from src.db.session import system_sync_session
    from src.scrapers.sources import nts_tacoma_index as nts
    from src.utils.safe_http import safe_get
    from src.workers.nts_crawler import _PDF_BROWSER_UA

    with system_sync_session() as db:
        row = db.execute(t(
            "SELECT * FROM nts_notices WHERE source=:s AND ts_number=:ts"),
            {"s": args.source, "ts": args.ts_number}).fetchone()
    if row is None:
        print("not found")
        return
    m = dict(row._mapping)
    print("=== STORED ===")
    for k in ("ts_number", "parcel", "auction_date", "principal_owing", "note_amount",
              "grantor", "trustee", "property_address", "is_active", "fetched_at",
              "source_url"):
        print(f"  {k:28s} {m.get(k)!r}")

    url = m.get("source_url")
    if not url:
        print("\nno source_url — cannot re-parse")
        return
    print(f"\n=== RE-FETCH {url} ===")
    try:
        resp = safe_get(url, timeout=45, headers={"User-Agent": _PDF_BROWSER_UA})
        print(f"  HTTP {resp.status_code}  bytes={len(resp.content)}")
    except Exception as e:  # noqa: BLE001
        print(f"  FETCH FAILED: {type(e).__name__}: {str(e)[:200]}")
        return

    html = resp.text
    try:
        parsed = nts.parse_notice_page(html) if hasattr(nts, "parse_notice_page") else None
    except Exception as e:  # noqa: BLE001
        parsed = None
        print(f"  parse_notice_page raised: {e}")
    if parsed is None:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        parsed = nts.parse_nts_notice(text)
    print("\n=== RE-PARSED NOW ===")
    for k in ("ts_number", "parcel", "auction_date", "principal_owing", "note_amount",
              "grantor", "trustee"):
        print(f"  {k:28s} {parsed.get(k)!r}")

    print("\n=== amount-shaped strings in the page text ===")
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    for mm in re.finditer(r"[^.]{0,90}\$\s?[\d,]+\.\d{2}[^.]{0,60}", text):
        print("   ...", mm.group(0).strip()[:190])


if __name__ == "__main__":
    main()
