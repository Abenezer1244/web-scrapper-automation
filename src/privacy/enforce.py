"""Write the privacy flag from the rules (design section 6: "Storage flag", "Write paths").

``enforce_rows(db, snapshot, result_ids)`` authorizes each given result and, in the
CALLER's transaction (it never commits):
  - flags every row that is not ALLOWED (WITHHELD, and REVIEW, which callers treat the
    same until an operator decides): results.privacy_suppressed_at + the rule row id;
  - stamps every row it decided with ``snapshot.generation`` (results.privacy_checked_gen).
Read paths (src/api/lead_actionability.py) then hide a flagged row everywhere.

Two callers:
  - a WRITER that just added a subject or contact to rows (a name, heirs, a phone): it
    passes the ids it touched, so the row is checked in the same transaction as the
    write. Every given id is re-checked whatever its stamp says: the write may have
    added the very subject a rule names.
  - the enforcer sweep (src/workers/privacy_enforcer.py), the backstop: rows whose
    stamp is behind the current generation.

Never touched:
  - a row already flagged (the flag is never cleared here; lifting is P7);
  - a row whose owner's account is purging or deleted: the purge owns those rows
    (migration 113's fence), and nothing is delivered from them.

Every other row is decided. Its record type, county and state come from its job's
config (jobs.scraper_config_id is NOT NULL), read even when a corrupt job points at
another tenant's config: here the config only selects how to read the row, and the
outcome can only add a suppression, so no row is ever left undecided (Codex P5c).

Locks: plain row UPDATEs, no users lock (the account-deletion fence's order is users ->
rows). ``RuleSnapshotError`` from authorize() propagates: the caller's transaction must
roll back and retry, never commit unchecked.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, or_, select, update

from src.db.models import Job, Result, ScraperConfig, User
from src.privacy.authorize import ALLOWED, ROW_COLUMNS, RuleSnapshot, authorize

_OWNER_STATES_SKIPPED = ("purging", "deleted")


@dataclass
class EnforceStats:
    checked: int = 0     # rows authorized and stamped
    flagged: int = 0     # of those, newly flagged

    def add(self, other: EnforceStats) -> None:
        self.checked += other.checked
        self.flagged += other.flagged


def enforce_rows(db, snapshot: RuleSnapshot, result_ids) -> EnforceStats:
    stats = EnforceStats()
    ids = list(dict.fromkeys(str(i) for i in result_ids))
    if not ids or snapshot.generation == 0:
        return stats  # no rule has ever been active: every stamp (0) is current
    gen = snapshot.generation
    rows = db.execute(
        select(Result.id, *(getattr(Result, c) for c in ROW_COLUMNS),
               ScraperConfig.record_type, ScraperConfig.county, ScraperConfig.state)
        .join(User, User.id == Result.user_id)
        .join(Job, Job.id == Result.job_id)
        .join(ScraperConfig, ScraperConfig.id == Job.scraper_config_id)
        .where(Result.id.in_(ids), Result.privacy_suppressed_at.is_(None),
               or_(User.deletion_state.is_(None),
                   User.deletion_state.not_in(_OWNER_STATES_SKIPPED)))
    ).mappings().all()

    allowed: list[str] = []
    withheld: dict[str, list[str]] = defaultdict(list)  # suppression id -> result ids
    for m in rows:
        decision = authorize(snapshot, m, record_type=m["record_type"],
                             county=m["county"], state=m["state"])
        if decision.outcome == ALLOWED:
            allowed.append(str(m["id"]))
        else:
            withheld[decision.suppression_ids[0]].append(str(m["id"]))

    stamp = func.greatest(Result.privacy_checked_gen, gen)
    if allowed:
        stats.checked += db.execute(
            update(Result)
            .where(Result.id.in_(allowed), Result.privacy_suppressed_at.is_(None))
            .values(privacy_checked_gen=stamp)
            .execution_options(synchronize_session=False)
        ).rowcount
    for suppression_id, rids in withheld.items():
        n = db.execute(
            update(Result)
            .where(Result.id.in_(rids), Result.privacy_suppressed_at.is_(None))
            .values(privacy_suppressed_at=func.now(), privacy_suppression_id=suppression_id,
                    privacy_checked_gen=stamp)
            .execution_options(synchronize_session=False)
        ).rowcount
        stats.checked += n
        stats.flagged += n
    return stats
