"""Owner-location flags for a job's rows, written set-based (King owner Phase 3b).

The job used to set the four flags on each ORM object and flush ~15k single-row
UPDATEs on a King tax re-run, one pooler round trip each (~25 ms), ~6 minutes. This
writes them in chunked UPDATE ... FROM jsonb_to_recordset statements instead.

Each row is written only if every column the flags were computed from, and every
flag column itself, still holds the value that was read. A row anything else wrote
in between is skipped and keeps its value, never overwritten from a stale read.
The loaded ORM objects are brought up to date with set_committed_value from what
the database RETURNED, so later readers of the same objects (plan cap, CSV
re-export, membership) see the written values without a re-query and nothing is
left dirty.
"""
from __future__ import annotations

import json

from sqlalchemy import text as sa_text
from sqlalchemy.orm.attributes import set_committed_value

from src.utils.address_intel import compute_owner_flags

FLAG_COLUMNS = ("property_state", "owner_state", "absentee_owner", "out_of_state_owner")
# ponytail: fixed chunk; a statement of ~1,000 small JSON objects is well under any limit.
_CHUNK = 1000

_UPDATE_SQL = sa_text("""
    UPDATE results r SET
      property_state = v.ps, owner_state = v.os,
      absentee_owner = v.ab, out_of_state_owner = v.oos
    FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS v(
      id uuid, pa text, ma text, pc text, pz text,
      ps_in text, os_in text, ab_in boolean, oos_in boolean,
      ps text, os text, ab boolean, oos boolean)
    WHERE r.id = v.id AND r.job_id = CAST(:jid AS uuid) AND r.user_id = CAST(:uid AS uuid)
      AND r.property_address IS NOT DISTINCT FROM v.pa
      AND r.mailing_address IS NOT DISTINCT FROM v.ma
      AND r.property_city IS NOT DISTINCT FROM v.pc
      AND r.property_zip IS NOT DISTINCT FROM v.pz
      AND r.property_state IS NOT DISTINCT FROM v.ps_in
      AND r.owner_state IS NOT DISTINCT FROM v.os_in
      AND r.absentee_owner IS NOT DISTINCT FROM v.ab_in
      AND r.out_of_state_owner IS NOT DISTINCT FROM v.oos_in
    RETURNING r.id::text AS id, r.property_state, r.owner_state,
              r.absentee_owner, r.out_of_state_owner
""")


def recompute_owner_flags(db, rows, job_id: str, user_id) -> tuple[int, int]:
    """Recompute and store the owner flags of `rows` (this job's Result objects).

    Returns (written, skipped): skipped = rows that changed after they were read.
    Commits. The ORM objects are touched only after the
    commit succeeds, so a failure leaves them, like the rows, as they were.
    """
    # Anything already pending is flushed first, so it cannot land after (and over)
    # these writes at commit.
    db.flush()
    by_id = {}
    payload = []
    for res in rows:
        flags = compute_owner_flags(
            res.property_address, res.mailing_address,
            property_city=res.property_city, property_state=res.property_state,
            property_zip=res.property_zip,
        )
        if all(getattr(res, c) == flags[c] for c in FLAG_COLUMNS):
            continue
        by_id[str(res.id)] = res
        payload.append({
            "id": str(res.id), "pa": res.property_address, "ma": res.mailing_address,
            "pc": res.property_city, "pz": res.property_zip,
            "ps_in": res.property_state, "os_in": res.owner_state,
            "ab_in": res.absentee_owner, "oos_in": res.out_of_state_owner,
            "ps": flags["property_state"], "os": flags["owner_state"],
            "ab": flags["absentee_owner"], "oos": flags["out_of_state_owner"],
        })
    returned = []
    for i in range(0, len(payload), _CHUNK):
        returned += db.execute(_UPDATE_SQL, {
            "rows": json.dumps(payload[i:i + _CHUNK]), "jid": str(job_id), "uid": str(user_id),
        }).all()
    db.commit()
    for row in returned:
        res = by_id.pop(row.id)
        for col in FLAG_COLUMNS:
            set_committed_value(res, col, getattr(row, col))
    # What is left changed under us: reload those (normally none) from the database
    # on next access, so no later reader sees the values this read was based on.
    for res in by_id.values():
        db.expire(res)
    return len(returned), len(by_id)
