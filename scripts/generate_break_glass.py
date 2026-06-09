"""Operator break-glass: issue emergency MFA recovery codes (H2 Phase 5).

Generates N single-use, high-entropy break-glass codes for one user, stores only
their keyed-HMAC hashes, and prints the plaintext ONCE. Hand the codes to the
account holder over a trusted channel; they redeem one at /auth/login/break-glass
if they lose their authenticator. Redemption is RECOVERY-ONLY (clears MFA, revokes
sessions, cannot pass admin step-up).

By default this REVOKES the user's existing unused break-glass codes first, so a
new issuance invalidates an older set (pass --keep-existing to add instead).

Run on Railway against prod:
    railway run --service worker python scripts/generate_break_glass.py \
        --email user@example.com --reason "pre-provision for travel" --count 8

SECURITY: the plaintext codes are printed to stdout, which on Railway lands in
platform logs. Treat that as sensitive — scrub the log line after delivering the
codes, or generate from a trusted shell. Codes are NEVER written to the app
logger, only to stdout under an explicit banner.
"""
import argparse
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update

from src.db.models import MfaBreakGlassCode, User
from src.db.session import SyncSessionLocal
from src.utils.mfa import generate_break_glass_codes


def _operator_identity() -> str:
    """Coarse operator identity for the audit trail (env first, else OS user)."""
    explicit = os.environ.get("BRIDGELEADS_OPERATOR")
    if explicit:
        return explicit[:255]
    import getpass
    try:
        return f"os:{getpass.getuser()}"[:255]
    except Exception:
        return "unknown-operator"


def run(email: str, count: int, reason: str, expires_days: int | None, keep_existing: bool) -> int:
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=expires_days) if expires_days else None
    operator = _operator_identity()
    batch_id = str(uuid.uuid4())

    with SyncSessionLocal() as db:
        user = db.execute(
            select(User).where(User.email == email).with_for_update()
        ).scalar_one_or_none()
        if user is None:
            print(f"ERROR: no user with email {email} — nothing generated.", file=sys.stderr)
            return 2
        user_id = user.id
        db.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(user_id)},
        )

        if not keep_existing:
            # Invalidate any prior unused/unrevoked codes so a fresh issuance
            # supersedes the old set (default — avoids a forgotten old code
            # lingering as a live admin-recovery credential).
            db.execute(
                update(MfaBreakGlassCode)
                .where(
                    MfaBreakGlassCode.user_id == user_id,
                    MfaBreakGlassCode.used_at.is_(None),
                    MfaBreakGlassCode.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )

        plaintext, hashes = generate_break_glass_codes(count)
        for code_hash in hashes:
            db.add(
                MfaBreakGlassCode(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    code_hash=code_hash,
                    batch_id=batch_id,
                    created_by=operator,
                    created_reason=reason[:500],
                    expires_at=expires_at,
                )
            )
        db.commit()

    # Print ONCE to stdout (never the app logger). Explicit banner so the
    # operator knows this is the only time the plaintext is shown.
    print("=" * 64)
    print("BREAK-GLASS CODES — shown ONCE. Store securely, then scrub this log.")
    print(f"  user:    {email}")
    print(f"  batch:   {batch_id}")
    print(f"  by:      {operator}")
    print(f"  expires: {expires_at.isoformat() if expires_at else 'never'}")
    print(f"  count:   {len(plaintext)}")
    print("-" * 64)
    for code in plaintext:
        print(f"  {code}")
    print("=" * 64)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Operator break-glass: issue MFA recovery codes.")
    parser.add_argument("--email", required=True, help="Target user's email.")
    parser.add_argument("--reason", required=True, help="Why these codes are issued (audit).")
    parser.add_argument("--count", type=int, default=8, help="How many codes (1-20, default 8).")
    parser.add_argument(
        "--expires-days",
        type=int,
        default=None,
        help="Optional expiry in days (default: no expiry).",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Do NOT revoke the user's existing unused codes (default: revoke them).",
    )
    args = parser.parse_args()
    if not (1 <= args.count <= 20):
        sys.exit("--count must be between 1 and 20.")
    if args.expires_days is not None and args.expires_days < 1:
        sys.exit("--expires-days must be >= 1 if provided.")
    if len(args.reason.strip()) < 3:
        sys.exit("--reason must be a meaningful explanation (>= 3 chars).")
    sys.exit(
        run(
            args.email.strip(),
            args.count,
            args.reason.strip(),
            args.expires_days,
            args.keep_existing,
        )
    )


if __name__ == "__main__":
    main()
