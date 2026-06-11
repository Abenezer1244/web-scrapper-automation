"""SPIKE (read-only): measure how parseable Result.date_recorded is.

The date-framed overlap/combine feature filters leads by their COUNTY FILING
DATE. That date is stored in the free-form text column results.date_recorded.
This script measures, across all leads and broken down by record_type, how many
of those text values we can reliably parse into a real DATE — the single biggest
risk to retire before designing the feature (per the Codex consult).

Reports:
  - overall: total rows, null/empty, parseable, unparseable (+ %)
  - per record_type breakdown (joined via jobs -> scraper_configs)
  - the most common raw "shapes" of the text (digits->9, letters->a) so we can
    see format diversity and which formats a parser must cover
  - a sample of UNPARSEABLE non-empty values (so we can eyeball the garbage)

READ-ONLY. No writes. Run on prod env:
    railway run --service worker python scripts/spike_date_recorded_coverage.py
  or locally if .env points at the prod DB:
    PYTHONPATH=. python scripts/spike_date_recorded_coverage.py
"""

import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Bound the per-row parse scan so a huge table can't blow up memory/time.
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "60000"))
SANE_MIN_YEAR = 1990
SANE_MAX_YEAR = 2031


def _make_parser():
    """Return parse(value) -> date|None using dateutil if present, else formats."""
    try:
        from dateutil import parser as _dp

        def _parse(v):
            try:
                dt = _dp.parse(v, fuzzy=False, default=None)
            except (ValueError, OverflowError, TypeError):
                return None
            if dt is None:
                return None
            if not (SANE_MIN_YEAR <= dt.year <= SANE_MAX_YEAR):
                return None
            return dt.date()

        return _parse, "dateutil"
    except ImportError:
        from datetime import datetime

        fmts = [
            "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%d-%m-%Y",
            "%b %d, %Y", "%B %d, %Y", "%Y%m%d", "%m/%d/%Y %H:%M:%S",
            "%Y-%m-%d %H:%M:%S", "%d %b %Y", "%m.%d.%Y",
        ]

        def _parse(v):
            for f in fmts:
                try:
                    dt = datetime.strptime(v.strip(), f)
                except ValueError:
                    continue
                if SANE_MIN_YEAR <= dt.year <= SANE_MAX_YEAR:
                    return dt.date()
            return None

        return _parse, "strptime-fallback"


def _shape(v):
    s = re.sub(r"\d", "9", v.strip())
    s = re.sub(r"[A-Za-z]", "a", s)
    return s[:24]


def main():
    from sqlalchemy import func, select

    from src.db.models import Job, Result, ScraperConfig
    from src.db.session import system_sync_session

    parse, engine_name = _make_parser()
    print(f"== date_recorded coverage spike (parser: {engine_name}, scan<= {SCAN_LIMIT}) ==\n")

    with system_sync_session() as db:
        total = db.execute(select(func.count()).select_from(Result)).scalar_one()
        nulls = db.execute(
            select(func.count()).select_from(Result).where(
                (Result.date_recorded.is_(None)) | (Result.date_recorded == "")
            )
        ).scalar_one()
        print(f"total result rows         : {total}")
        print(f"date_recorded null/empty  : {nulls} ({100*nulls/total:.1f}%)" if total else "no rows")
        print()

        # Pull a bounded sample of non-empty values, newest first (most relevant).
        rows = db.execute(
            select(Result.date_recorded)
            .where(Result.date_recorded.isnot(None), Result.date_recorded != "")
            .order_by(Result.created_at.desc())
            .limit(SCAN_LIMIT)
        ).scalars().all()

        parseable = 0
        unparseable = 0
        shapes = Counter()
        bad_samples = []
        for v in rows:
            d = parse(v)
            shapes[_shape(v)] += 1
            if d is not None:
                parseable += 1
            else:
                unparseable += 1
                if len(bad_samples) < 40:
                    bad_samples.append(v)

        scanned = len(rows)
        print(f"== Parse coverage over {scanned} non-empty sampled values ==")
        if scanned:
            print(f"  parseable    : {parseable} ({100*parseable/scanned:.1f}%)")
            print(f"  UNPARSEABLE  : {unparseable} ({100*unparseable/scanned:.1f}%)")
        print()

        print("== Top 25 raw shapes (9=digit, a=letter) ==")
        for shape, n in shapes.most_common(25):
            print(f"  {n:>7}  {shape!r}")
        print()

        print("== Sample UNPARSEABLE non-empty values (up to 40) ==")
        for v in bad_samples:
            print(f"  {v!r}")
        if not bad_samples:
            print("  <none — everything non-empty parsed>")
        print()

        # Per record_type coverage (join results -> jobs -> scraper_configs).
        print("== Per record_type: null/empty rate + parse rate on a sample ==")
        types = db.execute(
            select(ScraperConfig.record_type, func.count())
            .select_from(Result)
            .join(Job, Job.id == Result.job_id)
            .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
            .group_by(ScraperConfig.record_type)
            .order_by(func.count().desc())
        ).all()
        for rtype, cnt in types:
            n_null = db.execute(
                select(func.count())
                .select_from(Result)
                .join(Job, Job.id == Result.job_id)
                .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
                .where(
                    ScraperConfig.record_type == rtype,
                    (Result.date_recorded.is_(None)) | (Result.date_recorded == ""),
                )
            ).scalar_one()
            sample = db.execute(
                select(Result.date_recorded)
                .join(Job, Job.id == Result.job_id)
                .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
                .where(
                    ScraperConfig.record_type == rtype,
                    Result.date_recorded.isnot(None), Result.date_recorded != "",
                )
                .order_by(Result.created_at.desc())
                .limit(5000)
            ).scalars().all()
            ok = sum(1 for v in sample if parse(v) is not None)
            srate = (100 * ok / len(sample)) if sample else 0.0
            nrate = (100 * n_null / cnt) if cnt else 0.0
            print(f"  {rtype:>16} : rows={cnt:>7}  null/empty={nrate:5.1f}%  "
                  f"parse_ok(sample {len(sample)})={srate:5.1f}%")


if __name__ == "__main__":
    main()
