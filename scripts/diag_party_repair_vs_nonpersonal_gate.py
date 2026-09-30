"""Codex round-7 [P2] verification: can the party repair ever produce a party that
the ENQUEUE would have suppressed entirely?

build_pending_row_payload (src/scrapers/enrichment/skip_trace.py:741) returns None
— no trace at all — when looks_like_non_personal_party_name(party_name) is True.
_refresh_pending_name() has no equivalent branch: it derives (first, last) and
falls back to an ADVANCED (address-only, 2-credit) trace instead of suppressing.

So the question is whether orient_probate_party() can output such a value. Three
heuristics can return True; two of them (case-category prefixes, the
"<category> ? <house number>" shape) are shapes SYNTHESIZED by the code_violation
scrapers, and _PARTY_CANDIDATES is scoped to record_type IN
('probate','death_certificate'), so only the address-shaped-name rule is even
reachable. This runs the real repair derivation over every production candidate
and reports any row where the gate would fire — on the OLD value, the NEW value,
or both.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.scrapers.enrichment.skip_trace import (  # noqa: E402
    looks_like_non_personal_party_name,
    select_traceable_owner,
)
from src.scrapers.probate import orient_probate_party  # noqa: E402

_CANDIDATES = text(
    """
    SELECT r.id, r.party_name, r.heirs, r.doc_type, sc.county, sc.state,
           r.skip_trace_status
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE sc.record_type IN ('probate', 'death_certificate')
      AND (r.party_name IS NOT NULL OR r.heirs IS NOT NULL)
    ORDER BY r.created_at
    """
)


def main() -> None:
    with system_sync_session() as db:
        rows = db.execute(_CANDIDATES).mappings().all()

    would_change = 0
    gate_on_new = []
    gate_on_old = []
    advanced_after = 0

    for row in rows:
        new_party, new_heirs = orient_probate_party(
            row["party_name"], row["heirs"], row["doc_type"]
        )
        if new_party == row["party_name"] and new_heirs == row["heirs"]:
            continue
        would_change += 1
        if new_party is None:
            continue
        if looks_like_non_personal_party_name(new_party):
            gate_on_new.append((row["id"], row["party_name"], new_party))
        if looks_like_non_personal_party_name(row["party_name"]):
            gate_on_old.append((row["id"], row["party_name"], new_party))
        first, last = select_traceable_owner(new_party)
        if not (first and last):
            advanced_after += 1

    print(f"probate/death_certificate rows scanned : {len(rows)}")
    print(f"rows the party repair would still change: {would_change}")
    print(f"  -> new party trips the non-personal gate: {len(gate_on_new)}")
    for rid, old, new in gate_on_new[:20]:
        print(f"       result={rid} old={old!r} new={new!r}")
    print(f"  -> OLD party tripped the gate (so no pending row exists): {len(gate_on_old)}")
    for rid, old, new in gate_on_old[:20]:
        print(f"       result={rid} old={old!r} new={new!r}")
    print(f"  -> would end on ADVANCED (no confident person): {advanced_after}")

    # Independent of the repair: does ANY stored probate party trip the gate today?
    tripping = [r for r in rows if looks_like_non_personal_party_name(r["party_name"])]
    print(f"\nstored probate party_name values tripping the gate right now: {len(tripping)}")
    for r in tripping[:20]:
        print(f"    result={r['id']} party={r['party_name']!r} "
              f"county={r['county']} skip_trace={r['skip_trace_status']}")


if __name__ == "__main__":
    main()
