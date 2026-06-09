"""Operator break-glass: fully reset a user's MFA (H2 Phase 5).

The HEAVIEST recovery lever: clears MFA entirely, destroys every backup and
break-glass code, and revokes all sessions + the API key. Use when an account
holder is fully locked out (lost authenticator AND has no usable break-glass
code). Authentication is by operator access to the production DB/env — there is
no in-app trigger for this.

After this runs the user logs in with their PASSWORD only (MFA is gone) and is
forced to re-enroll: admin routes return admin_mfa_enrollment_required until they
do.

Run on Railway against prod:
    railway run --service worker python scripts/reset_user_mfa.py \
        --email user@example.com --reason "lost device, verified by phone"

Idempotent: re-running on an already-reset user just re-applies the same NULLs
and re-revokes (harmless).
"""
import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

from sqlalchemy import delete, select, text, update

from src.db.models import MfaBackupCode, MfaBreakGlassCode, User
from src.db.session import SyncSessionLocal

logging.basicConfig(level=logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("reset_user_mfa")


def run(email: str, reason: str) -> int:
    now = datetime.now(UTC)
    # Resolve the user (no lock) just to confirm existence + get the id.
    with SyncSessionLocal() as db:
        user_id = db.execute(
            select(User.id).where(User.email == email)
        ).scalar_one_or_none()
    if user_id is None:
        _log.error("No user with email %s — nothing reset.", email)
        return 2

    # FAIL-SAFE ORDERING (mirrors change_password): revoke ALL sessions FIRST,
    # before clearing MFA. revoke_all_for_user writes the durable users.revoked_at
    # and fails CLOSED on a datastore error (incl. the Redis SETEX+DEL double
    # failure, where a stale positive cache could otherwise mask the revocation —
    # Codex P1). We therefore treat ANY failure as fatal: nothing has been
    # cleared yet, so we exit non-zero with MFA + sessions UNCHANGED (safe). The
    # operator re-runs once the datastore recovers; the whole script is
    # idempotent. Doing this before any users-row mutation also keeps the
    # revoke_all_for_user transaction from contending with our row lock.
    from src.api.middleware.auth_hardening import TokenBlacklist
    try:
        asyncio.run(TokenBlacklist.revoke_all_for_user(user_id))
    except Exception as exc:  # noqa: BLE001 — operator must see the reason
        _log.error(
            "Session revocation FAILED (%s) — nothing was changed. RE-RUN this "
            "script once the datastore recovers (it is idempotent).", exc,
        )
        return 3

    # Sessions are now revoked. Clear MFA + destroy codes in one atomic txn.
    with SyncSessionLocal() as db:
        # Lock the row so a concurrent generate/redeem can't race this reset.
        locked = db.execute(
            select(User.id).where(User.id == user_id).with_for_update()
        ).scalar_one_or_none()
        if locked is None:
            # Deleted between the revoke and now — sessions are already revoked
            # (and the row is gone), so there is nothing left to clear.
            _log.warning("User %s vanished after revoke; sessions already revoked.", user_id)
            return 0
        # Forward-safe under an RLS-enforce cutover (advisory today): bind the
        # tenant context before touching the per-user code tables.
        db.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(user_id)},
        )
        # Targeted Core UPDATE (NOT ORM writeback) so we set only these columns
        # and never clobber the revoked_at that revoke_all_for_user just wrote.
        db.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                mfa_enabled=False,
                mfa_secret_encrypted=None,
                mfa_last_totp_counter=None,
                mfa_enrolled_at=None,
                api_key_hash=None,
            )
        )
        db.execute(delete(MfaBackupCode).where(MfaBackupCode.user_id == user_id))
        db.execute(delete(MfaBreakGlassCode).where(MfaBreakGlassCode.user_id == user_id))
        db.commit()

    # LOUD audit to the operator log. Never logs secrets — reason is operator text.
    _log.info(
        "MFA RESET COMPLETE for user_id=%s email=%s at %s — reason=%r. "
        "User must re-enroll MFA on next login.",
        user_id, email, now.isoformat(), reason,
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Operator break-glass: reset a user's MFA.")
    parser.add_argument("--email", required=True, help="Target user's email.")
    parser.add_argument(
        "--reason",
        required=True,
        help="Why this reset is being performed (for the audit log).",
    )
    args = parser.parse_args()
    if len(args.reason.strip()) < 3:
        sys.exit("--reason must be a meaningful explanation (>= 3 chars).")
    sys.exit(run(args.email.strip(), args.reason.strip()[:500]))


if __name__ == "__main__":
    main()
