"""Is a King tax lead's blank owner name still being looked up?

King's bulk tax extract redacts owner names, so a King tax lead is named later,
one eRealProperty page per parcel, by the job or by the owner-recovery sweep
(src/workers/owner_recovery.py). Until that settles, a blank owner cell means
"not looked up yet", not "no owner", and the UI says so.

Pure and derived at serialize time, never stored: it reads only the row's own
fields. The settled outcomes mirror owner_recovery._ELIGIBLE_ROW (keys from
src/workers/tasks_helpers/enrich.py), so a lead the sweep has given up on, or
King has no record for, is never shown as pending forever. Display only: the
CSV keeps a blank owner cell.
"""
from __future__ import annotations

import re
from typing import Any

_KING_PIN = re.compile(r"[0-9]{10}")
_SETTLED_RECOVERY = frozenset({"found", "not_on_record", "parcel_mismatch", "gave_up"})
# owner_recovery._MAX_ATTEMPTS: the sweep stops asking at this many attempts.
_MAX_ATTEMPTS = 5


def owner_name_pending(party_name: Any, parcel_id: Any, enrichment_data: Any) -> bool:
    """True when this KING TAX lead has no owner name yet and one is still expected.

    The caller decides the row is a King tax_delinquent lead; this only reads the row.
    """
    if isinstance(party_name, str) and party_name.strip():
        return False
    if not isinstance(parcel_id, str) or not _KING_PIN.fullmatch(parcel_id.strip()):
        return False
    data = enrichment_data if isinstance(enrichment_data, dict) else {}
    if data.get("owner_lookup_outcome") == "not_on_record":
        return False
    attempts = str(data.get("owner_recovery_attempts") or "")
    if re.fullmatch(r"[0-9]{1,6}", attempts) and int(attempts) >= _MAX_ATTEMPTS:
        return False
    return data.get("owner_recovery_outcome") not in _SETTLED_RECOVERY


def is_king_tax(county: Any, record_type: Any) -> bool:
    return (isinstance(county, str) and county.strip().lower() == "king"
            and record_type == "tax_delinquent")

