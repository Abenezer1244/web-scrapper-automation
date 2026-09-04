"""READ-ONLY: what the King (queen_anne_news) TS-number repair WOULD change.

Re-parses the archived King legals PDFs with the FIXED parser and compares the result
against the stored nts_notices / results rows. SELECTs only — there is no write path in
this file. Run the real repair with scripts/repair_nts_ts_number.py --source
queen_anne_news (dry-run by default, --apply to write).

    railway run --service worker python scripts/diag_king_ts_repair_plan.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE = "queen_anne_news"


def main():
    from sqlalchemy import text as sa_text

    from src.db.session import system_sync_session
    from src.scrapers.sources import nts_pdf
    from src.scrapers.sources.nts_king_pdf import parse_king_notice
    from src.utils.safe_http import safe_get
    from src.workers.nts_crawler import _PDF_BROWSER_UA

    with system_sync_session() as db:
        urls = [r[0] for r in db.execute(sa_text(
            "SELECT DISTINCT source_url FROM nts_notices "
            "WHERE source = :s AND source_url IS NOT NULL AND source_url <> ''"
        ), {"s": SOURCE}).all()]
        print(f"archived {SOURCE} PDFs on file: {len(urls)}")

        truth = {}          # parcel -> (ts, auction_date, grantor, owing)
        for u in urls:
            try:
                data = safe_get(u, timeout=60, headers={"User-Agent": _PDF_BROWSER_UA}).content
            except Exception as exc:
                print(f"  UNREACHABLE {u.rsplit('/', 1)[-1]}: {str(exc)[:90]}")
                continue
            blocks = nts_pdf.split_notice_blocks(nts_pdf.normalize_pdf_text(
                nts_pdf.extract_pdf_text(data)))
            n = 0
            for b in blocks:
                p = parse_king_notice(b)
                if p.get("parcel") and p.get("ts_number"):
                    truth[p["parcel"]] = (p["ts_number"], p.get("auction_date"),
                                          (p.get("grantor") or "")[:34], p.get("principal_owing"))
                    n += 1
            print(f"  {u.rsplit('/', 1)[-1]}: {n}/{len(blocks)} notices parsed")

        print(f"\nauthoritative parcel -> ts for {len(truth)} notices\n")
        stored = db.execute(sa_text(
            "SELECT id::text, ts_number, parcel, auction_date, is_active, grantor, principal_owing "
            "FROM nts_notices WHERE source = :s ORDER BY auction_date"
        ), {"s": SOURCE}).all()

        print("=== nts_notices: stored vs truth ===")
        for _rid, ts, parcel, auc, act, _grantor, _owing in stored:
            t = truth.get(parcel or "")
            if not t:
                print(f"  {ts:22} parcel={str(parcel):16} auction={auc} active={act}  <no archived source>")
                continue
            ok_ts = (t[0] == ts)
            print(f"  {ts:22} parcel={str(parcel):16} auction={auc} active={act}")
            if not ok_ts:
                print(f"      MISBOUND -> should be {t[0]!r} (grantor {t[2]!r}, auction {t[1]}, owing {t[3]})")
            elif str(t[1] or "") and str(auc) != str(t[1]):
                print(f"      DATE DRIFT -> parser now reads {t[1]}")

        stored_ts = {r[1] for r in stored}
        print("\n=== notices present in the PDFs but MISSING from the cache ===")
        by_ts = {v[0]: (k, v) for k, v in truth.items()}
        for ts, (parcel, v) in sorted(by_ts.items()):
            if ts not in stored_ts:
                print(f"  MISSING {ts:22} parcel={parcel:16} auction={v[1]} grantor={v[2]!r} owing={v[3]}")

        print("\n=== delivered lead rows carrying a King TS number ===")
        rows = db.execute(sa_text("""
            SELECT r.id::text, r.job_id::text, r.parcel_id, r.party_name,
                   r.enrichment_data -> 'nts' ->> 'ts_number' AS ts, r.auction_date
              FROM results r
             WHERE r.nts_notice_id IS NOT NULL
               AND r.enrichment_data -> 'nts' ->> 'source' = :s
        """), {"s": SOURCE}).all()
        for rid, jid, parcel, party, ts, auc in rows:
            t = truth.get(parcel or "")
            flag = "" if (t and t[0] == ts) else f"  <-- stored {ts!r}, truth {t[0]!r}" if t else "  <no source>"
            print(f"  result={rid[:8]} job={jid[:8]} parcel={str(parcel):16} auction={auc} "
                  f"party={str(party)[:30]!r}{flag}")
        if not rows:
            print("  (none)")


if __name__ == "__main__":
    main()
