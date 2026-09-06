"""READ-ONLY: measure the blast radius of the city/state pre-submit gate.

build_pending_row_payload now declines a Result whose locality cannot be
resolved (Tracerfy requires address+city+state and silently drops rows missing
them). Before shipping that gate, measure how many REAL Results it would newly
decline -- a guard that looks reasonable and quietly kills good leads is the
failure mode this project has hit before.

Runs the payload builder over real Results and reports the delta. Writes nothing.

Run:
    railway run --service worker python scripts/diag_locality_gate_impact.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LIMIT = int(os.environ.get("LIMIT", "5000"))


def main():
    from sqlalchemy import select

    from src.api.lead_actionability import actionable_condition
    from src.db.models import Result
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.skip_trace import (
        _parse_full_address,
        looks_like_non_personal_party_name,
    )

    with system_sync_session() as db:
        rows = db.execute(
            select(Result)
            .where(
                Result.property_address.isnot(None),
                actionable_condition(),
                Result.is_duplicate.is_(False),
            )
            .order_by(Result.created_at.desc())
            .limit(LIMIT)
        ).scalars().all()

    print(f"== Sampled {len(rows)} actionable, non-duplicate Results ==\n")

    eligible_before = 0
    declined_no_locality = 0
    missing_city_only = 0
    missing_state_only = 0
    missing_both = 0
    examples: list[str] = []

    for rec in rows:
        prop = (rec.property_address or "").strip()
        if not prop or prop == "(enrichment unavailable)":
            continue
        if looks_like_non_personal_party_name(rec.party_name):
            continue

        # Mirror build_pending_row_payload's locality resolution exactly.
        parsed = _parse_full_address(rec.property_address)
        for field, stored in (
            ("city", getattr(rec, "property_city", None)),
            ("state", getattr(rec, "property_state", None)),
            ("zip", getattr(rec, "property_zip", None)),
        ):
            if not parsed[field] and stored:
                parsed[field] = str(stored).strip() or None
        mail_parsed = (
            _parse_full_address(rec.mailing_address) if rec.mailing_address else None
        )
        if not parsed["city"] and mail_parsed and mail_parsed["city"]:
            parsed["city"] = mail_parsed["city"]
            parsed["state"] = mail_parsed["state"]
            parsed["zip"] = mail_parsed["zip"]

        eligible_before += 1
        has_city, has_state = bool(parsed["city"]), bool(parsed["state"])
        if has_city and has_state:
            continue
        declined_no_locality += 1
        if not has_city and not has_state:
            missing_both += 1
        elif not has_city:
            missing_city_only += 1
        else:
            missing_state_only += 1
        if len(examples) < 12:
            examples.append(
                f"    city={parsed['city']!r} state={parsed['state']!r} "
                f"addr={(rec.property_address or '')[:58]!r}"
            )

    print(f"  Eligible under the OLD rule : {eligible_before}")
    print(f"  Newly DECLINED by the gate  : {declined_no_locality}")
    if eligible_before:
        pct = 100.0 * declined_no_locality / eligible_before
        print(f"  Blast radius                : {pct:.2f}%")
    print(f"    missing state only        : {missing_state_only}")
    print(f"    missing city only         : {missing_city_only}")
    print(f"    missing both              : {missing_both}")

    if examples:
        print("\n  Examples of newly-declined rows:")
        for e in examples:
            print(e)

    print(
        "\n  NOTE: a declined row stays skip_trace_status='not_attempted', so a later "
        "\n  GIS/assessor backfill that fills the situs makes it eligible again."
    )


if __name__ == "__main__":
    main()
