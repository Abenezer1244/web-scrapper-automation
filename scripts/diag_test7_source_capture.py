"""Capture the RAW King LandmarkWeb DataTables JSON for the Test 7 window.

Read-only against the county portal: runs the real scraper's search once and
dumps every raw row (pre-parse) plus the parsed ScrapedRecords, so the stored
Test 7 leads can be diffed field-by-field against the source.

    railway run python scripts/diag_test7_source_capture.py <out_dir>
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATE_FROM = "06/04/2026"
DATE_TO = "09/02/2026"


async def main(out_dir: str):
    from src.scrapers.king_wa_probate import KingCountyLandmarkWebScraper

    raw_batches: list[list] = []
    orig = KingCountyLandmarkWebScraper._parse_json_results

    def capture(self, data_rows):
        raw_batches.append(data_rows)
        return orig(self, data_rows)

    KingCountyLandmarkWebScraper._parse_json_results = capture

    async with KingCountyLandmarkWebScraper(record_type="probate") as s:
        records = await s.scrape(DATE_FROM, DATE_TO)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "king_raw_rows.json"), "w", encoding="utf-8") as fh:
        json.dump(raw_batches, fh, indent=1)
    with open(os.path.join(out_dir, "king_parsed.json"), "w", encoding="utf-8") as fh:
        json.dump([r.to_dict() for r in records], fh, indent=1, default=str)
    print(f"raw batches={len(raw_batches)} rows={sum(len(b) for b in raw_batches)} parsed={len(records)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
