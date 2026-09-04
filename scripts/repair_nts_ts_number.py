"""Repair TS numbers shifted by the pre-header split bug (see PR #195).

`split_notice_blocks` used to cut on a lookahead at the statutory header. North Star
and MTC/Trustee Corps print "TS No <x>" BEFORE that header, so the run was orphaned at
the tail of the PREVIOUS block and every such notice was stored under the FOLLOWING
notice's TS number. This repairs the rows already written that way.

Truth comes from RE-PARSING the source PDFs with the fixed parser, joined on `parcel` —
nothing is hand-typed. Parcel is the safe join key: it was never affected by the bug
(it is printed after the header), and it was verified correct on every audited row.

TWO PHASES, because they have different safety conditions:

  --results   SAFE TO RUN NOW. Rewrites the delivered lead rows' stored TS number
              (enrichment_data + the ts-derived raw_html_hash/source_fingerprint).
              Durable: the beat matcher only writes rows WHERE auction_date IS NULL
              (src/workers/nts_matcher_task.py), so it never rewrites a matched row.

  --notices   MUST WAIT until the fixed parser is DEPLOYED. nts_notices is upserted
              ON CONFLICT (source, ts_number) by the beat crawler. Renaming a row to
              its correct number while the OLD parser is still live means the next
              crawl re-parses the same notice to the OLD (wrong) number and UPDATES
              the row we just renamed — overwriting one notice's data with another's.
              That is strictly worse than the current state, so this phase refuses to
              run without --i-confirm-fixed-parser-is-deployed.

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


def repair_results(db, truth: dict[str, str], apply: bool) -> int:
    """Fix the stored TS number on already-delivered lead rows."""
    rows = db.execute(sa_text("""
        SELECT r.id::text AS id, r.job_id::text AS job_id, r.party_name, r.parcel_id,
               r.raw_html_hash, r.source_fingerprint, r.enrichment_data
          FROM results r
         WHERE r.nts_notice_id IS NOT NULL
           AND r.enrichment_data -> 'nts' ->> 'ts_number' IS NOT NULL
           AND r.parcel_id = ANY(:parcels)
    """), {"parcels": list(truth)}).all()

    # Plan every change first, then order them — see the loop below.
    plan = []
    for row in rows:
        m = dict(row._mapping)
        correct = truth[m["parcel_id"]]
        enr = m["enrichment_data"] or {}
        stored = (enr.get("nts") or {}).get("ts_number")
        if stored == correct:
            continue

        new_enr = dict(enr)
        new_enr["nts"] = {**(enr.get("nts") or {}), "ts_number": correct}
        if isinstance(enr.get("nts_source"), dict):
            new_enr["nts_source"] = {**enr["nts_source"], "ts_number": correct}

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
        stale, new_hash = _ts_hash(stored or ""), _ts_hash(correct)
        if m["raw_html_hash"] == stale:
            sets.append("raw_html_hash = :h")
            params["h"] = new_hash
        if m["source_fingerprint"] == stale:
            sets.append("source_fingerprint = :h")
            params["h"] = new_hash

        plan.append({
            "m": m, "stored": stored, "correct": correct, "sets": sets, "params": params,
            "old_fp": m["source_fingerprint"],
            "new_fp": new_hash if m["source_fingerprint"] == stale else m["source_fingerprint"],
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
        print(f"  {m['party_name'][:26]:28} job={m['job_id'][:8]} parcel={m['parcel_id']:20} "
              f"{item['stored']!r} -> {item['correct']!r}"
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
    if not (args.results or args.notices):
        ap.error("pick --results and/or --notices")
    # The gate is on WRITING, not on planning — a dry run must always be allowed so the
    # rename plan can be reviewed before the deploy that makes it safe.
    if args.notices and args.apply and not args.deployed:
        ap.error(
            "--notices --apply needs --i-confirm-fixed-parser-is-deployed: with the OLD "
            "parser live, the next beat crawl re-upserts the wrong number onto the row you "
            "just renamed, overwriting one notice's data with another's"
        )

    with system_sync_session() as db:
        urls = [r[0] for r in db.execute(sa_text(
            "SELECT DISTINCT source_url FROM nts_notices "
            "WHERE source = :src AND source_url IS NOT NULL AND source_url <> ''"
        ), {"src": SOURCE}).all()]
        print(f"Re-parsing {len(urls)} {SOURCE} source PDFs with the FIXED parser…")
        truth = truth_from_pdfs(urls)
        print(f"  authoritative parcel -> ts_number for {len(truth)} notices\n")

        total = 0
        if args.results:
            print("results:")
            total += repair_results(db, truth, args.apply)
        if args.notices:
            print("nts_notices:")
            total += repair_notices(db, truth, args.apply)

        if args.apply:
            db.commit()
            print(f"\nAPPLIED — {total} row(s) updated.")
        else:
            db.rollback()
            print(f"\nDRY RUN — {total} row(s) would change. Re-run with --apply.")


if __name__ == "__main__":
    main()
