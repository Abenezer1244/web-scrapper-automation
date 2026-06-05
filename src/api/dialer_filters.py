"""Phase 5 — dialer-ready lead selection (the Enzo-independent foundation).

"Dialer-ready" = a lead a user can safely push into a dialer TODAY: a valid
phone number that is KNOWN to be off the Do-Not-Call list. This is a pure,
DB-agnostic predicate builder (same shape as tax_filters.py) so it can be reused
by both the results view/export filter (shipping now) and the eventual Enzo push
task (blocked on Enzo API docs/credentials — see the milestone spec).

TCPA / FTC TSR (Codex-reviewed): a number with an UNKNOWN DNC status
(phone_dnc_flag IS NULL) is intentionally EXCLUDED from `dialer_ready` — you may
not call a number you have not confirmed is off the registry. Only
`phone_dnc_flag IS FALSE` qualifies. A looser "candidate" set that includes
unknown-DNC numbers is a separate, explicitly-named opt-in (not the default).

Provenance-agnostic: a valid phone from the scrape itself qualifies just as a
skip-traced one does. A caller that specifically wants skip-traced leads can AND
in `skip_trace_status = 'hit'` separately — it is NOT baked into "ready".
"""
from sqlalchemy import func

from src.db.models import Result


def dialer_ready_conditions(include_unknown_dnc: bool = False) -> list:
    """Predicates for dialer-ready leads: a valid phone that is not DNC.

    include_unknown_dnc=False (default, TCPA-safe): require phone_dnc_flag IS
    FALSE — unknown/NULL DNC status is excluded.
    include_unknown_dnc=True ("candidate" set): allow NULL DNC (exclude only
    KNOWN-DNC). Caller must opt in deliberately and label it honestly.
    """
    conditions = [
        Result.phone.is_not(None),
        func.trim(Result.phone) != "",
    ]
    if include_unknown_dnc:
        # Exclude only KNOWN DNC; unknown (NULL) is allowed through.
        conditions.append(Result.phone_dnc_flag.isnot(True))
    else:
        # TCPA-safe: only numbers confirmed NOT on the DNC list.
        conditions.append(Result.phone_dnc_flag.is_(False))
    return conditions
