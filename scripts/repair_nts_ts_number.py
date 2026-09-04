"""Repair TS numbers shifted by the pre-header split bug (see PR #195).

`split_notice_blocks` used to cut on a lookahead at the statutory header. North Star
and MTC/Trustee Corps print "TS No <x>" BEFORE that header, so the run was orphaned at
the tail of the PREVIOUS block and every such notice was stored under the FOLLOWING
notice's TS number. This repairs the rows already written that way.

Truth comes from RE-PARSING the source PDFs with the fixed parser, joined on `parcel` —
nothing is hand-typed. Parcel is the safe join key: it was never affected by the bug
(it is printed after the header), and it was verified correct on every audited row.

TWO PHASES, because they have different safety conditions:

  --results   SAFE TO RUN NOW. Rewrites the delivered lead rows' stored TS number —
              BOTH copies in enrichment_data (the crawler's nested `nts` blob and the
              pre_foreclosure scraper's own top-level `ts_number`) plus the ts-derived
              raw_html_hash/source_fingerprint.
              Durable: the beat matcher only writes rows WHERE auction_date IS NULL
              (src/workers/nts_matcher_task.py), so it never rewrites a matched row.

  --notices   MUST WAIT until the fixed parser is DEPLOYED. nts_notices is upserted
              ON CONFLICT (source, ts_number) by the beat crawler. Renaming a row to
              its correct number while the OLD parser is still live means the next
              crawl re-parses the same notice to the OLD (wrong) number and UPDATES
              the row we just renamed — overwriting one notice's data with another's.
              That is strictly worse than the current state, so this phase refuses to
              run without --i-confirm-fixed-parser-is-deployed.

  --retire-wrong-key
              The alternative to --notices when the corrections do NOT form an orderable
              rename chain. On King they do not: an already-inactive twin still occupies
              a number a live row needs, and --notices refuses (correctly) to invent a
              rename-parking scheme. This retires the wrongly-keyed rows instead and
              lets the crawler's archive sweep insert the correctly-keyed ones. Same
              deploy gate as --notices. Mutually exclusive with it.

  --fields    Corrects nts_notices auction_date / principal_owing, which the two phases
              above never touched. The same split bug also captured a NEIGHBOURING
              notice's auction date onto a row, and a wrong date is worse than a wrong
              key: it is the product's urgency clock, and a live sale mis-stored as a
              past one is flipped is_active=false by the crawler's expiry pass and
              vanishes from matching. Measured on King 2026-09-04: 12 of 14 cached rows
              had a wrong ts_number and 2 also had a wrong auction_date, one of them
              hiding a live 2026-09-18 sale. Corrects each row against a re-parse of ITS
              OWN source PDF (not a cross-issue map — a postponed sale legitimately
              prints two dates), and re-activates a row whose corrected auction is still
              ahead, since nothing else ever sets is_active back to true.

Usage:
    railway run --service worker python scripts/repair_nts_ts_number.py --results
    # a different paper (King):  --source queen_anne_news
    railway run --service worker python scripts/repair_nts_ts_number.py --results --apply
    # only after PR #195 is deployed:
    railway run --service worker python scripts/repair_nts_ts_number.py --notices \
        --i-confirm-fixed-parser-is-deployed --apply

Dry-run by default: prints every intended change and writes nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text as sa_text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.scrapers.sources import nts_pdf  # noqa: E402
from src.scrapers.sources import nts_tacoma_index as nts  # noqa: E402
from src.utils.logger import setup_logger  # noqa: E402
from src.utils.safe_http import safe_get  # noqa: E402
from src.workers.nts_crawler import _PDF_BROWSER_UA  # noqa: E402

_logger = setup_logger("scripts.repair_nts_ts_number")

# Which paper this run repairs. Set from --source in main(); every query, the ts hash
# and the re-parse all key off these, so a run can never mix two sources' rows.
# Each source needs the SAME parser its crawler uses — King's layouts (no-colon Affinia
# fields, surrogate REF-/APN- keys) come out garbage under the shared colon parser, and
# a garbage truth map is exactly what this script must never write from.
SOURCE = "snohomish_tribune"
PARSE_FN = None  # resolved in main() from _SOURCE_PARSERS

# notice_to_row stamps the county onto every row it builds, and the matcher scopes by
# county — so the re-parse must use the SAME county its crawler task passes.
_SOURCE_COUNTY = {"snohomish_tribune": "snohomish", "queen_anne_news": "king"}


def _source_parsers() -> dict:
    """source -> the parse function its crawler passes to the shared PDF pipeline."""
    from src.scrapers.sources.nts_king_pdf import parse_king_notice

    return {
        "snohomish_tribune": nts.parse_nts_notice,   # shared colon parser
        "queen_anne_news": parse_king_notice,        # King: no-colon + surrogate keys
    }


def _fetch(url: str) -> bytes:
    """Fetch a legals PDF exactly the way the crawler does.

    Reuses `safe_get` (the SSRF guard every outbound fetch must pass) AND the crawler's
    browser UA — the Pacific Publishing CDN answers 403 to a default library agent, so
    a hand-rolled urllib fetch silently returned nothing for every issue.
    """
    resp = safe_get(url, timeout=60, headers={"User-Agent": _PDF_BROWSER_UA})
    resp.raise_for_status()
    return resp.content


def truth_from_pdfs(urls: list[str]) -> dict[str, str]:
    """parcel -> the TS number that notice actually prints, per the FIXED parser."""
    truth: dict[str, str] = {}
    for url in urls:
        # FAIL CLOSED on a fetch failure. Skipping used to be a warning, which made the
        # whole run quietly under-repair while still printing "APPLIED — N rows" (Codex).
        # Worse, an incomplete truth map disarms the cross-issue disagreement check
        # below: if a parcel legitimately carries two different TS numbers in two issues
        # and only one issue fetched, the map looks unanimous and the notices phase would
        # retire a live sale as a duplicate. A partial map is not safe to write from.
        try:
            data = _fetch(url)
        except Exception as exc:
            raise SystemExit(
                f"ABORT: could not fetch {url} ({str(exc)[:120]}). Refusing to repair "
                "from an incomplete source set — re-run when every issue is reachable."
            ) from exc
        blocks = nts_pdf.split_notice_blocks(nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(data)))
        hits = 0
        for block in blocks:
            parsed = PARSE_FN(block)
            parcel, ts = parsed.get("parcel"), parsed.get("ts_number")
            if not parcel or not ts:
                continue
            if parcel in truth and truth[parcel] != ts:
                # Two issues disagreeing about the same parcel means the re-parse is not
                # authoritative; refuse rather than pick one (this would be the bug again).
                raise SystemExit(
                    f"ABORT: parcel {parcel} parses to {truth[parcel]!r} and {ts!r} across issues"
                )
            truth[parcel] = ts
            hits += 1
        print(f"  parsed {hits}/{len(blocks)} notices from {url.rsplit('/', 1)[-1]}")
    return truth


def truth_by_issue(urls: list[str]) -> dict[str, dict[str, dict]]:
    """source_url -> parcel -> the auction fields that issue actually prints.

    Deliberately NOT keyed on parcel alone, unlike `truth_from_pdfs`. A TS number is a
    stable property of the sale, so one map across every issue is right for it — but an
    auction DATE is not: a postponed sale legitimately prints two different dates in two
    issues, and folding those together would either abort or pick one at random. Each
    stored row is therefore corrected against a re-parse of ITS OWN source PDF, which is
    the only text that row was ever supposed to represent.
    """
    from datetime import date as _date

    today = _date.today()
    county = _SOURCE_COUNTY[SOURCE]
    out: dict[str, dict[str, dict]] = {}
    for url in urls:
        try:
            data = _fetch(url)
        except Exception as exc:
            raise SystemExit(
                f"ABORT: could not fetch {url} ({str(exc)[:120]}). Refusing to repair "
                "from an incomplete source set — re-run when every issue is reachable."
            ) from exc
        blocks = nts_pdf.split_notice_blocks(
            nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(data)))
        per: dict[str, dict] = {}
        for block in blocks:
            # Go through notice_to_row, not the raw parser: it is what the crawler
            # writes through, so the dates/decimals compared here are normalized the
            # same way as the stored values (the raw parser returns strings). It
            # returns None for a notice it could not date — nothing to correct from.
            row = nts.notice_to_row(PARSE_FN(block), source_url=url, today=today,
                                    source=SOURCE, county=county)
            if row is None or not row.get("parcel"):
                continue
            parcel = row["parcel"]
            if parcel in per:
                # One issue printing a parcel twice with conflicting fields means the
                # re-parse is not authoritative for it; refuse rather than choose.
                raise SystemExit(
                    f"ABORT: parcel {parcel} appears twice in {url.rsplit('/', 1)[-1]}")
            per[parcel] = {
                "ts_number": row.get("ts_number"),
                "auction_date": row.get("auction_date"),
                "principal_owing": row.get("principal_owing"),
            }
        out[url] = per
        print(f"  re-read {len(per)} notices from {url.rsplit('/', 1)[-1]}")
    return out


def repair_notice_fields(db, by_issue: dict[str, dict[str, dict]], apply: bool) -> int:
    """Correct auction_date / principal_owing on nts_notices from each row's own PDF.

    The TS repair (--notices) only ever rewrote the natural key, so rows whose auction
    DATE was captured from a neighbouring notice by the same split bug kept the wrong
    date indefinitely — and a wrong date is the more damaging of the two: it is the
    product's urgency clock, and a live sale mis-stored as a past one is flipped
    is_active=false by the crawler's expiry pass and disappears from matching entirely.

    Reactivates a row whose corrected auction is still in the future, because nothing
    else ever sets is_active back to true.
    """
    from datetime import date as _date

    today = _date.today()
    rows = [dict(r._mapping) for r in db.execute(sa_text("""
        SELECT id::text AS id, ts_number, parcel, source_url, auction_date,
               principal_owing, is_active
          FROM nts_notices
         WHERE source = :src AND source_url IS NOT NULL AND source_url <> ''
         ORDER BY source_url, parcel
    """), {"src": SOURCE}).all()]

    changed = 0
    for row in rows:
        per = by_issue.get(row["source_url"]) or {}
        tr = per.get(row["parcel"])
        if tr is None:
            # Never guess. A parcel absent from its own issue means the row's provenance
            # is not reproducible; leave it alone and say so.
            print(f"  SKIP  parcel={row['parcel']!r} not found in "
                  f"{row['source_url'].rsplit('/', 1)[-1]} — left untouched")
            continue
        sets, notes = {}, []
        if tr["auction_date"] is not None and tr["auction_date"] != row["auction_date"]:
            sets["auction_date"] = tr["auction_date"]
            notes.append(f"auction_date {row['auction_date']} -> {tr['auction_date']}")
        if tr["principal_owing"] is not None and (
            row["principal_owing"] is None
            or Decimal(str(tr["principal_owing"])) != Decimal(str(row["principal_owing"]))
        ):
            sets["principal_owing"] = tr["principal_owing"]
            notes.append(f"principal_owing {row['principal_owing']} -> {tr['principal_owing']}")
        if not sets:
            continue
        new_auction = sets.get("auction_date", row["auction_date"])
        # Reactivation is the one destructive thing this phase can do, so it is gated on
        # the row ALREADY carrying the correct TS number — i.e. --notices has run and
        # this row is the survivor, not a twin it deliberately retired. Un-retiring a
        # twin would surface one sale twice, which is the bug --notices exists to kill.
        # Run order is therefore --notices, then --fields; run alone, --fields corrects
        # the stored values and reactivates nothing (fail closed).
        if (
            new_auction and new_auction >= today and not row["is_active"]
            and tr["ts_number"] and row["ts_number"] == tr["ts_number"]
        ):
            sets["is_active"] = True
            notes.append("is_active false -> true (auction is still ahead)")
        changed += 1
        print(f"  parcel={row['parcel']!r} ts={row['ts_number']!r}: {'; '.join(notes)}")
        if apply:
            assign = ", ".join(f"{k} = :{k}" for k in sets)
            res = db.execute(
                sa_text(f"UPDATE nts_notices SET {assign} WHERE id = CAST(:id AS uuid)"),  # noqa: S608
                {**sets, "id": row["id"]},
            )
            _require_one(res, "nts_notices", row["id"])
    return changed


def retire_wrong_key(db, by_issue: dict[str, dict[str, dict]], apply: bool) -> int:
    """Retire rows whose stored ts_number is not the one their own issue prints.

    An alternative to --notices for a source whose back issues are all re-fetchable.
    --notices RENAMES rows onto the right natural key, which needs the corrections to
    form an orderable chain; on King they do not — an already-inactive twin still
    occupies a number a live row needs, and freeing it would need a rename-parking
    scheme the script deliberately refuses to invent. Retiring instead is strictly
    simpler and loses nothing here: the crawler's archive sweep re-reads those issues
    and INSERTS the correctly-keyed rows itself.

    Retire, never delete: results.nts_notice_id points at these rows (and the app role
    has no DELETE). A retired row keeps serving as the audit target of the lead it
    matched; is_active=false only removes it from future matching, so the correct row
    the sweep inserts cannot end up competing with a stale twin for the same sale.
    """
    rows = [dict(r._mapping) for r in db.execute(sa_text("""
        SELECT n.id::text AS id, n.ts_number, n.parcel, n.source_url, n.is_active,
               n.auction_date, count(r.id) AS results
          FROM nts_notices n
          LEFT JOIN results r ON r.nts_notice_id = n.id
         WHERE n.source = :src AND n.source_url IS NOT NULL AND n.source_url <> ''
         GROUP BY n.id, n.ts_number, n.parcel, n.source_url, n.is_active, n.auction_date
         ORDER BY n.source_url, n.parcel
    """), {"src": SOURCE}).all()]

    n = 0
    for row in rows:
        tr = (by_issue.get(row["source_url"]) or {}).get(row["parcel"])
        if tr is None or not tr["ts_number"]:
            continue
        if row["ts_number"] == tr["ts_number"]:
            continue
        if not row["is_active"]:
            # Already out of the matching set; nothing to do. Reported so a repaired
            # database dry-runs as "0 rows would change".
            continue
        n += 1
        print(f"  RETIRE parcel={row['parcel']!r} ts={row['ts_number']!r} "
              f"(issue prints {tr['ts_number']!r}) results={row['results']}")
        if apply:
            res = db.execute(
                sa_text("UPDATE nts_notices SET is_active = false WHERE id = CAST(:id AS uuid)"),
                {"id": row["id"]},
            )
            _require_one(res, "nts_notices", row["id"])
    return n


def _require_one(result, table: str, row_id: str) -> None:
    """Every UPDATE here targets one row by primary key, so anything else means the row
    moved under us (the crawler also writes nts_notices). Abort so the whole transaction
    rolls back rather than report a repair that did not land (Codex)."""
    if result.rowcount != 1:
        raise SystemExit(
            f"ABORT: UPDATE {table} id={row_id} touched {result.rowcount} rows, expected 1 "
            "— the row changed underneath this run; nothing has been committed."
        )


def _ts_hash(ts_number: str) -> str:
    """The raw_html_hash trustee_sale.py derives — MUST stay in step with that code."""
    return hashlib.sha256(f"nts|{SOURCE}|{ts_number}".encode()).hexdigest()[:32]


def _normalized_truth(truth: dict[str, str]) -> dict[str, str]:
    """truth re-keyed the way the MATCHER compares parcels (alphanumerics, uppercased).

    A lead's parcel_id is the recorder's verbatim spelling and the notice's is the
    newspaper's; King prints "111263-0120" for a lead stored as "1112630120", so an
    exact-string join silently skips exactly the rows the matcher had no trouble
    pairing. Reuses the matcher's own _norm_parcel so the repair joins on the same key
    the match was made on. Aborts on a genuine collision rather than pick a side.
    """
    from src.scrapers.sources.nts_matcher import _norm_parcel

    out: dict[str, str] = {}
    for parcel, ts in truth.items():
        k = _norm_parcel(parcel)
        if not k:
            continue
        if k in out and out[k] != ts:
            raise SystemExit(
                f"ABORT: parcels normalizing to {k!r} carry two TS numbers "
                f"({out[k]!r}, {ts!r}) — resolve by hand; nothing written."
            )
        out[k] = ts
    return out


def repair_results(db, truth: dict[str, str], apply: bool) -> int:
    """Fix the stored TS number on already-delivered lead rows."""
    norm_truth = _normalized_truth(truth)
    # JOIN through to the notice so only rows matched from THIS paper are touched.
    # Parcel numbers are not globally unique across counties and normalizing widens the
    # collision surface, so parcel alone must never decide which row gets rewritten
    # (Codex P1). The pre-existing exact-string join had the same hole; it was simply
    # harder to hit.
    rows = db.execute(sa_text("""
        SELECT r.id::text AS id, r.job_id::text AS job_id, r.party_name, r.parcel_id,
               r.raw_html_hash, r.source_fingerprint, r.enrichment_data
          FROM results r
          JOIN nts_notices n ON n.id = r.nts_notice_id
         WHERE n.source = :src
           AND r.enrichment_data -> 'nts' ->> 'ts_number' IS NOT NULL
           AND upper(regexp_replace(coalesce(r.parcel_id, ''), '[^A-Za-z0-9]', '', 'g'))
               = ANY(:parcels)
    """), {"parcels": list(norm_truth), "src": SOURCE}).all()

    from src.scrapers.sources.nts_matcher import _norm_parcel

    # Plan every change first, then order them — see the loop below.
    plan = []
    for row in rows:
        m = dict(row._mapping)
        correct = norm_truth[_norm_parcel(m["parcel_id"])]
        enr = m["enrichment_data"] or {}
        stored = (enr.get("nts") or {}).get("ts_number")
        # The TOP-LEVEL ts_number is a SECOND, independent copy: the pre_foreclosure
        # scraper writes it from its own parse of the same PDF
        # (snohomish_wa_pre_foreclosure._record_from_notice), so it carried the SAME
        # shift and needs the SAME correction. The first pass of this repair modelled
        # only the nested `nts` blob — which is why 6 rows across 3 jobs were left with
        # a corrected nested value sitting next to a top-level one still naming the
        # FOLLOWING notice. Present-only: never ADD the key to a trustee_sale row that
        # never had one, or the two writers would start disagreeing in the other
        # direction.
        top_stored = enr.get("ts_number")
        top_wrong = top_stored is not None and top_stored != correct
        if stored == correct and not top_wrong:
            continue

        new_enr = dict(enr)
        new_enr["nts"] = {**(enr.get("nts") or {}), "ts_number": correct}
        if isinstance(enr.get("nts_source"), dict):
            new_enr["nts_source"] = {**enr["nts_source"], "ts_number": correct}
        if top_stored is not None:
            new_enr["ts_number"] = correct

        params = {"id": m["id"], "enr": json.dumps(new_enr)}
        sets = ["enrichment_data = CAST(:enr AS json)"]
        # raw_html_hash IS the ON CONFLICT source_fingerprint for trustee_sale rows
        # (tasks.py: `_fingerprint = rec.raw_html_hash or _source_fingerprint(rec)`), so
        # the two must move together — and ONLY when the stored hash really is the
        # ts-derived one. pre_foreclosure rows carry raw_html_hash NULL and a fingerprint
        # built from parcel/party instead; those must not be touched.
        # Each column is tested on its OWN value rather than assuming the two agree:
        # a row whose source_fingerprint had drifted from raw_html_hash used to get only
        # the hash rewritten, leaving the real ON CONFLICT key stale (Codex). Measured
        # 0 such rows in production, but the coupling was an assumption, not a fact.
        # Only the NESTED value feeds these hashes, so a row whose nested number was
        # already correct (top-level-only repair) must not touch them — otherwise
        # `stale` equals `new_hash` and the row rewrites its own fingerprint to the
        # value it already holds, churning the uq_results_job_fingerprint bookkeeping
        # below for no reason.
        stale, new_hash = _ts_hash(stored or ""), _ts_hash(correct)
        if stored != correct:
            if m["raw_html_hash"] == stale:
                sets.append("raw_html_hash = :h")
                params["h"] = new_hash
            if m["source_fingerprint"] == stale:
                sets.append("source_fingerprint = :h")
                params["h"] = new_hash

        plan.append({
            "m": m, "stored": stored, "correct": correct, "sets": sets, "params": params,
            "top_stored": top_stored, "top_wrong": top_wrong,
            "old_fp": m["source_fingerprint"],
            "new_fp": (
                new_hash
                if stored != correct and m["source_fingerprint"] == stale
                else m["source_fingerprint"]
            ),
        })

    if not plan:
        return 0

    # results has a UNIQUE index on (job_id, source_fingerprint) WHERE fingerprint IS NOT
    # NULL (uq_results_job_fingerprint). The corrections form a CHAIN inside a single job:
    # Cate's CORRECTED fingerprint is exactly the one Weintraub still holds, because
    # Weintraub's row is squatting on Cate's TS number. Updating in query order therefore
    # trips the constraint. Free a fingerprint before claiming it, same as the notices
    # phase. (Unique INDEXES cannot be DEFERRABLE, so ordering is the only option.)
    # Model EVERY fingerprint in the affected jobs, not only the rows being changed: the
    # unique index spans the whole job, so a row this script never selected can hold the
    # key a rename wants and the pre-check would miss it (Codex).
    held: dict[tuple[str, str], str] = {}
    for row in db.execute(sa_text("""
        SELECT id::text AS id, job_id::text AS job_id, source_fingerprint
          FROM results
         WHERE job_id = ANY(CAST(:jobs AS uuid[])) AND source_fingerprint IS NOT NULL
    """), {"jobs": sorted({item["m"]["job_id"] for item in plan})}).all():
        m = dict(row._mapping)
        held[(m["job_id"], m["source_fingerprint"])] = m["id"]

    changed, queue, guard = 0, list(plan), 0
    while queue and guard <= len(plan) * len(plan) + 1:
        guard += 1
        item = queue.pop(0)
        m, key = item["m"], (item["m"]["job_id"], item["new_fp"])
        if item["new_fp"] and held.get(key, m["id"]) != m["id"]:
            queue.append(item)  # another row in this job still holds it
            continue
        nested_note = (
            f"nts {item['stored']!r} -> {item['correct']!r}"
            if item["stored"] != item["correct"] else "nts ok"
        )
        top_note = (
            f"  top {item['top_stored']!r} -> {item['correct']!r}" if item["top_wrong"] else ""
        )
        print(f"  {m['party_name'][:26]:28} job={m['job_id'][:8]} parcel={m['parcel_id']:20} "
              f"{nested_note}{top_note}"
              + (f"  fp {item['old_fp'][:8]}->{item['new_fp'][:8]}"
                 if item["new_fp"] and item["new_fp"] != item["old_fp"] else ""))
        if apply:
            # noqa justification: `sets` is assembled ONLY from the fixed literal
            # fragments a few lines above ("enrichment_data = CAST(:enr AS json)",
            # "raw_html_hash = :h", "source_fingerprint = :h"). No value reaches the SQL
            # text — every value is a bound parameter. Which fragments apply varies per
            # row, which is why the statement is composed rather than written out.
            columns = ", ".join(item["sets"])
            res = db.execute(
                sa_text(f"UPDATE results SET {columns} WHERE id = CAST(:id AS uuid)"),  # noqa: S608
                item["params"],
            )
            _require_one(res, "results", m["id"])
        if item["old_fp"]:
            held.pop((m["job_id"], item["old_fp"]), None)
        if item["new_fp"]:
            held[key] = m["id"]
        changed += 1
    if queue:
        raise SystemExit(
            f"ABORT: {len(queue)} result update(s) could not be ordered without colliding "
            "on uq_results_job_fingerprint — inspect before forcing"
        )
    return changed


def repair_notices(db, truth: dict[str, str], apply: bool) -> int:
    """Rename nts_notices rows onto their real TS number.

    Ordered, because the corrections form a CHAIN: each notice wants the number the
    next one is currently squatting on, and (source, ts_number) is unique. Freeing a
    number before claiming it is the whole reason this is not a bulk UPDATE.
    """
    rows = [dict(r._mapping) for r in db.execute(sa_text("""
        SELECT n.id::text AS id, n.ts_number, n.parcel, n.grantor, n.auction_date,
               n.is_active, n.created_at, count(r.id) AS results
          FROM nts_notices n
          LEFT JOIN results r ON r.nts_notice_id = n.id
         WHERE n.source = :src AND n.parcel = ANY(:parcels)
         GROUP BY n.id, n.ts_number, n.parcel, n.grantor, n.auction_date, n.is_active, n.created_at
    """), {"src": SOURCE, "parcels": list(truth)}).all()]

    # The bug could give the SAME notice two different wrong numbers in two issues, so a
    # parcel can hold two active rows for one real sale — the same "one sale surfacing
    # twice" the crawler's trailing-dash twin retirement guards against. Keep the row the
    # delivered leads already reference (ties: the oldest), retire the rest. Retire, not
    # delete: results.nts_notice_id points at these, and the app role has no DELETE.
    by_parcel: dict[str, list[dict]] = {}
    for m in rows:
        by_parcel.setdefault(m["parcel"], []).append(m)

    retire: list[dict] = []
    keep_rows: list[dict] = []
    for parcel, group in by_parcel.items():
        if len(group) == 1:
            keep_rows.append(group[0])
            continue
        # "Same parcel" is NOT the same thing as "same sale" (Codex): a parcel can carry
        # two genuinely distinct trustee sales. Same parcel AND same auction date is the
        # duplicate this bug manufactures — one real notice split across two rows because
        # two issues gave it two different wrong numbers. Anything else is refused rather
        # than guessed at, because retiring a live sale hides it from every lead list.
        dates = {m["auction_date"] for m in group}
        if len(dates) != 1:
            raise SystemExit(
                f"ABORT: parcel {parcel} has {len(group)} notices across "
                f"{len(dates)} auction dates ({sorted(str(d) for d in dates)}) — these may "
                "be DISTINCT sales, not duplicates. Resolve by hand; nothing written."
            )
        group.sort(key=lambda g: (-int(g["results"]), g["created_at"]))
        keep_rows.append(group[0])
        # Already-inactive losers are ALREADY retired — excluded so a fully repaired
        # database dry-runs as "0 rows would change" instead of perpetually reporting
        # a retirement it would not actually perform.
        retire.extend(m for m in group[1:] if m["is_active"])

    for m in retire:
        print(f"  RETIRE duplicate {(m['grantor'] or '')[:30]!r:32} parcel={m['parcel']:20} "
              f"ts={m['ts_number']!r} results={m['results']} active={m['is_active']}")
        if apply:
            res = db.execute(
                sa_text("UPDATE nts_notices SET is_active = false WHERE id = CAST(:id AS uuid)"),
                {"id": m["id"]},
            )
            _require_one(res, "nts_notices", m["id"])

    pending = []
    for m in keep_rows:
        correct = truth[m["parcel"]]
        if m["ts_number"] != correct:
            pending.append((m, correct))
    if not pending:
        return len(retire)

    # A retired duplicate still occupies its ts_number (retiring does not free the natural
    # key), so it can still block a rename. That needs a scheme for moving a retired row
    # out of the way, which is not needed for any row seen so far — refuse rather than
    # invent one untested.
    blockers = {m["ts_number"]: m for m in retire}
    for _row, correct in pending:
        if correct in blockers:
            raise SystemExit(
                f"ABORT: {correct!r} is held by RETIRED duplicate id={blockers[correct]['id']}; "
                "moving a retired row out of the way is unimplemented — inspect manually"
            )

    # Model EVERY ts_number in this source, not just the rows being changed: the unique
    # key spans the source, so a notice whose parcel is absent from `truth` can still be
    # holding the number a rename wants and the pre-check would miss it (Codex).
    held = {
        r[0]: r[1]
        for r in db.execute(
            sa_text("SELECT ts_number, id::text FROM nts_notices WHERE source = :src"),
            {"src": SOURCE},
        ).all()
    }
    # Bound on the INITIAL size: `pending` shrinks as renames land, so recomputing the
    # limit from the live list made it fall below `guard` and abandon the last rename.
    done, guard, budget = 0, 0, len(pending) ** 2 + 1
    while pending and guard <= budget:
        guard += 1
        m, correct = pending.pop(0)
        if correct in held and held[correct] != m["id"]:
            pending.append((m, correct))  # still occupied — come back to it
            continue
        print(f"  {(m['grantor'] or '')[:34]:36} parcel={m['parcel']:20} "
              f"{m['ts_number']!r} -> {correct!r}")
        if apply:
            res = db.execute(
                sa_text("UPDATE nts_notices SET ts_number = :new WHERE id = CAST(:id AS uuid)"),
                {"new": correct, "id": m["id"]},
            )
            _require_one(res, "nts_notices", m["id"])
        held.pop(m["ts_number"], None)
        held[correct] = m["id"]
        done += 1
    if pending:
        for m, correct in pending:
            holder = held.get(correct)
            print(f"  STUCK: {(m['grantor'] or '')[:34]!r} wants {correct!r}, held by id={holder}")
        raise SystemExit(
            f"ABORT: {len(pending)} rename(s) could not be ordered — see STUCK lines above"
        )
    return done + len(retire)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", action="store_true", help="repair delivered lead rows (safe now)")
    ap.add_argument("--notices", action="store_true", help="rename nts_notices (needs the fix deployed)")
    ap.add_argument("--retire-wrong-key", action="store_true", dest="retire_wrong_key",
                    help="retire nts_notices whose ts_number is not the one their own "
                         "issue prints (alternative to --notices; needs the fix deployed)")
    ap.add_argument("--fields", action="store_true",
                    help="correct nts_notices auction_date/principal_owing from each row's own PDF")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry-run")
    ap.add_argument("--i-confirm-fixed-parser-is-deployed", action="store_true", dest="deployed")
    ap.add_argument(
        "--source", default="snohomish_tribune", choices=sorted(_source_parsers()),
        help="which paper's rows to repair (default: snohomish_tribune, the original run)",
    )
    args = ap.parse_args()
    global SOURCE, PARSE_FN
    SOURCE = args.source
    PARSE_FN = _source_parsers()[SOURCE]
    if not (args.results or args.notices or args.fields or args.retire_wrong_key):
        ap.error("pick --results, --notices, --retire-wrong-key and/or --fields")
    if args.notices and args.retire_wrong_key:
        ap.error("--notices and --retire-wrong-key are two ways to resolve the SAME wrong "
                 "keys; pick one")
    # The gate is on WRITING, not on planning — a dry run must always be allowed so the
    # rename plan can be reviewed before the deploy that makes it safe.
    # --fields is gated for the same reason (Codex P2): it rewrites product-facing
    # auction_date / principal_owing from a LOCAL re-parse. If the deployed crawler runs
    # a different parser, the next beat overwrites what this just wrote — or worse, a
    # locally-stale parser writes values the deployed one would never produce.
    if (args.notices or args.retire_wrong_key or args.fields) and args.apply             and not args.deployed:
        ap.error(
            "--apply on --notices/--retire-wrong-key/--fields needs "
            "--i-confirm-fixed-parser-is-deployed: with the OLD "
            "parser live, the next beat crawl re-upserts the wrong number onto the row you "
            "just renamed, overwriting one notice's data with another's"
        )

    with system_sync_session() as db:
        urls = [r[0] for r in db.execute(sa_text(
            "SELECT DISTINCT source_url FROM nts_notices "
            "WHERE source = :src AND source_url IS NOT NULL AND source_url <> ''"
        ), {"src": SOURCE}).all()]
        print(f"Re-parsing {len(urls)} {SOURCE} source PDFs with the FIXED parser…")
        truth: dict[str, str] = {}
        if args.results or args.notices:
            truth = truth_from_pdfs(urls)
            print(f"  authoritative parcel -> ts_number for {len(truth)} notices\n")
        by_issue = (truth_by_issue(urls)
                    if (args.fields or args.retire_wrong_key) else {})

        total = 0
        if args.results:
            print("results:")
            total += repair_results(db, truth, args.apply)
        if args.notices:
            print("nts_notices:")
            total += repair_notices(db, truth, args.apply)
        if args.retire_wrong_key:
            print("nts_notices wrong-key retirement:")
            total += retire_wrong_key(db, by_issue, args.apply)
        if args.fields:
            print("nts_notices auction fields:")
            total += repair_notice_fields(db, by_issue, args.apply)

        if args.apply:
            db.commit()
            print(f"\nAPPLIED — {total} row(s) updated.")
        else:
            db.rollback()
            print(f"\nDRY RUN — {total} row(s) would change. Re-run with --apply.")


if __name__ == "__main__":
    main()
