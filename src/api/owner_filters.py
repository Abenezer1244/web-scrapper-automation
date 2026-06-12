"""Owner-location view/export filters (absentee / out-of-state).

Same shape as src/api/tax_filters.py: pure translation of user-facing filter
flags into Result column predicates, applied to the paginated view + export so
`total`/`items`/CSV agree. VIEW filter only — never changes scraping or billing.

The columns are TRI-STATE (True / False / NULL-unknown), so predicates use
`.is_(True)` / `.is_(False)` — never Python truthiness (Codex): a `True` filter
must exclude BOTH known-False and unknown-NULL rows, and a `False` filter must
exclude unknown-NULL too (the user asked for a definite answer, not "maybe").
"""
from src.db.models import Result


def build_owner_conditions(
    absentee: bool | None,
    out_of_state: bool | None,
) -> list:
    """SQLAlchemy predicates on Result for the active owner-location filters.

    Empty list when neither is set. Each flag, when set, matches ONLY rows where
    the stored boolean equals it exactly (IS TRUE / IS FALSE) — NULL/unknown rows
    are excluded from a definite filter, which is the intended behavior: "show me
    absentee owners" should not include rows we couldn't determine.
    """
    conditions: list = []
    if absentee is not None:
        conditions.append(Result.absentee_owner.is_(absentee))
    if out_of_state is not None:
        conditions.append(Result.out_of_state_owner.is_(out_of_state))
    return conditions
