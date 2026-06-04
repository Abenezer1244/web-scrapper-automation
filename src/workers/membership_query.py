"""Read-side queries over property_list_membership (Phase 1).

Phase 3 (combine/overlap) builds its export on these. Kept tenant-scoped and
indexed: cost is proportional to one user's membership rows, not the table.

RLS: property_list_membership is a tenant table with a USING policy keyed on
app.current_user_id (migration 034). A plain AsyncSessionLocal() does NOT set
that GUC, so under enforced RLS it would return zero rows (Codex). We bind the
session to the user with set_config, the same contract get_rls_db uses in the
API. Phase 3's API endpoint will call this through its already-RLS-bound
request session instead; this standalone helper sets the GUC itself so it is
correct in worker/script contexts too.
"""
from sqlalchemy import text

from src.db.session import AsyncSessionLocal


async def users_overlap(user_id: str, record_types: list[str]) -> set[str]:
    """Return the set of property_keys this user has on ALL of `record_types`
    (the "on both lists" intersection). Strong-identity rows only — the table
    holds nothing else.
    """
    if len(record_types) < 2:
        return set()
    async with AsyncSessionLocal() as db:
        # Bind RLS context to this user (no-op when RLS_ENFORCE is off; required
        # once FORCE is on). Mirrors the app.current_user_id contract in deps.
        await db.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(user_id)},
        )
        rows = await db.execute(
            text(
                """
                SELECT property_key
                FROM property_list_membership
                WHERE user_id = :uid AND record_type = ANY(:types)
                GROUP BY property_key
                HAVING count(DISTINCT record_type) >= :n
                """
            ),
            {"uid": str(user_id), "types": record_types, "n": len(record_types)},
        )
        return {r.property_key for r in rows.fetchall()}
