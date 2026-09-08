"""Manually clear an enrichment source's cooldown, after PROVING it is serving.

    railway run --service worker python scripts/ops_clear_source_health.py <source_key> --apply

This is the human override for a cooldown that outlived the outage it recorded.
It exists because the canary (`src.workers.scheduler.enrichment_source_canary`)
only clears a source once it is DEPLOYED and running; before that, and after any
incident where a block should be lifted immediately, this is the safe path.

It refuses to clear a source it cannot verify. The probe it runs is the same one
the canary uses, so "cleared by hand" and "cleared by canary" mean exactly the
same thing about the source. Without `--apply` it only reports.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_change = "--apply" in sys.argv
    if not args:
        print("usage: ops_clear_source_health.py <source_key> [--apply]")
        return 2
    source_key = args[0]

    from src.db.session import system_sync_session
    from src.scrapers.enrichment.source_health import (
        get_source_state,
        mark_source_healthy,
    )
    from src.scrapers.enrichment.source_probe import PROBES

    with system_sync_session() as db:
        before = get_source_state(db, source_key)
        if before is None:
            print(f"{source_key}: no health row — already healthy, nothing to do")
            return 0
        print(f"BEFORE  status={before['status']} cooldown_until={before['cooldown_until']}")
        print(f"        first_seen_at={before['first_seen_at']} "
              f"probe_failures={before['consecutive_probe_failures']}")
        print(f"        reason={before['reason']}")

        probe = PROBES.get(source_key)
        if probe is None:
            print(f"\nREFUSING: no probe is registered for {source_key}, so this script "
                  "cannot verify it is serving. Clearing blind would put real traffic "
                  "back onto a source nobody has checked.")
            return 1

        print(f"\nProbing {source_key} ...")
        healthy, detail = probe(db)
        print(f"PROBE   healthy={healthy}  {detail}")
        if not healthy:
            print("\nREFUSING to clear: the source did not answer its probe. The cooldown "
                  "is doing its job.")
            return 1

        if not apply_change:
            print("\nDRY RUN. Re-run with --apply to clear the cooldown.")
            return 0

        recovered = mark_source_healthy(db, source_key)
        after = get_source_state(db, source_key)
        print(f"\nAPPLIED recovered={recovered}")
        print(f"AFTER   status={after['status']} cooldown_until={after['cooldown_until']} "
              f"probe_failures={after['consecutive_probe_failures']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
