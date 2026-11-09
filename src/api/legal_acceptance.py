"""Terms / Privacy acceptance for existing accounts (compliance audit Phase 3d).

What it records is an application record that a signed-in person ticked a box for the
versions the backend publishes (src/config/constants.py LEGAL_DOCUMENT_VERSIONS),
written to the append-only legal_acceptances (migration 123) with flow 'reaccept'. It is
not a legal conclusion about what that proves; counsel owns the evidence model (whether to
add document hashes, IP or user agent, or separate boxes).

"Accepted" means a row for EXACTLY the current version of each document. A row for
PREVIOUS_PRIVACY_VERSION (the post-bump grace for stale signup pages) does not count.

The gate (LEGAL_REACCEPT_ENFORCED, off by default) runs inside get_auth_context, the one
dependency every authenticated route resolves, for sessions and API keys alike, so a new
route is gated unless it is listed in _EXEMPT below. Exempt: what a person must always be
able to do: see and accept the terms, secure the account, exercise privacy rights (export,
delete, restore) and cancel (billing portal). Webhooks and Celery tasks never reach it.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.config.constants import LEGAL_DOCUMENT_VERSIONS

REQUIRED_DETAIL = "legal_acceptance_required"
VERSION_CHANGED_DETAIL = "legal_version_changed"

# (method, route template). Matched on the ROUTE, not the raw path, so a path parameter
# cannot be used to reach an exemption.
_EXEMPT = frozenset({
    ("GET", "/auth/me"),
    ("GET", "/auth/legal"),
    ("POST", "/auth/legal/accept"),
    # Where an incomplete profile is sent to finish it (Codex 3d review).
    ("PUT", "/auth/profile"),
    # Securing the account.
    ("GET", "/auth/sessions"),
    ("DELETE", "/auth/sessions/{session_id}"),
    ("POST", "/auth/sessions/revoke-others"),
    ("POST", "/auth/logout-all"),
    ("POST", "/auth/change-password"),
    ("GET", "/auth/mfa/status"),
    ("POST", "/auth/mfa/setup"),
    ("POST", "/auth/mfa/enable"),
    ("POST", "/auth/mfa/disable"),
    ("DELETE", "/auth/api-key"),
    ("GET", "/auth/security-events"),
    # Privacy rights.
    ("POST", "/auth/account/delete"),
    ("POST", "/auth/account/restore"),
    ("POST", "/auth/export"),
    ("GET", "/auth/export"),
    ("GET", "/auth/export/{export_id}/url"),
    ("GET", "/auth/export/{export_id}/download"),
    # Cancelling.
    ("POST", "/billing/portal"),
})


def route_key(request: Request) -> tuple[str, str] | None:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return (request.method, path) if path else None


async def accepted_documents(db: AsyncSession, user_id: str) -> set[str]:
    """Which documents this user has accepted at their CURRENT version."""
    # legal_acceptances is RLS-isolated on app.current_user_id (migration 123).
    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(user_id)})
    rows = await db.execute(
        text("SELECT DISTINCT document FROM legal_acceptances WHERE user_id = :u AND "
             "((document = 'terms' AND version = :t) OR (document = 'privacy' AND version = :p))"),
        {"u": str(user_id), "t": LEGAL_DOCUMENT_VERSIONS["terms"],
         "p": LEGAL_DOCUMENT_VERSIONS["privacy"]})
    return set(rows.scalars())


async def refuse_if_acceptance_required(db: AsyncSession, user, request: Request | None) -> None:
    """403 legal_acceptance_required for an account that has not accepted the current
    versions, on every route but _EXEMPT. Fails closed: no Request, no exemption."""
    if not settings.LEGAL_REACCEPT_ENFORCED:
        return
    if request is not None and route_key(request) in _EXEMPT:
        return
    if await accepted_documents(db, user.id) >= set(LEGAL_DOCUMENT_VERSIONS):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=REQUIRED_DETAIL)


def require_current_versions_at_signup(terms: str | None, privacy: str | None) -> None:
    """With LEGAL_ACCEPTANCE_REQUIRED on, a signup must submit exactly the current pair:
    the previous-version grace (registration.legal_versions_to_record) is for the
    non-enforced path only."""
    if not settings.LEGAL_ACCEPTANCE_REQUIRED:
        return
    if {"terms": terms, "privacy": privacy} != LEGAL_DOCUMENT_VERSIONS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=REQUIRED_DETAIL)
