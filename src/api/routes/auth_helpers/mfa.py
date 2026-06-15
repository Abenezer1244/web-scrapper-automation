"""Body logic for the MFA enrollment routes: POST /auth/mfa/setup, /auth/mfa/enable,
/auth/mfa/disable. Extracted VERBATIM from auth.py — the route decorators +
signatures stay in auth.py; these hold the moved bodies.
"""

from datetime import UTC, datetime

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import verify_password
from src.api.middleware import audit_log, rate_limit
from src.api.schemas import (
    MfaDisableRequest,
    MfaEnableRequest,
    MfaEnableResponse,
    MfaSetupResponse,
)
from src.db import User


async def mfa_setup_secret(
    request: Request,
    current_user: User,
    db: AsyncSession,
) -> MfaSetupResponse:
    await rate_limit(request, zone="auth")
    from src.utils.crypto import encrypt_field
    from src.utils.mfa import generate_totp_secret, totp_provisioning_uri

    # FOR UPDATE: serialize concurrent setup/enable/disable for this user so a
    # race can't leave mfa_enabled=true with a mismatched secret or duplicate
    # backup-code sets (Codex H2-P2 review).
    user = (
        await db.execute(select(User).where(User.id == current_user.id).with_for_update())
    ).scalar_one()
    if user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MFA is already enabled. Disable it first to re-enroll.",
        )
    secret = generate_totp_secret()
    user.mfa_secret_encrypted = encrypt_field(secret)
    await db.commit()
    audit_log(request, "mfa_setup", current_user.id)
    return MfaSetupResponse(
        secret=secret,
        provisioning_uri=totp_provisioning_uri(secret, user.email),
    )


async def mfa_enable_for_user(
    body: MfaEnableRequest,
    request: Request,
    current_user: User,
    db: AsyncSession,
) -> MfaEnableResponse:
    await rate_limit(request, zone="auth")
    # Per-user throttle (not just per-IP): caps TOTP guessing across rotating IPs.
    await rate_limit(request, zone="auth", identifier=f"mfa-user:{current_user.id}")
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503
    from src.db.models import MfaBackupCode
    from src.utils.crypto import decrypt_field
    from src.utils.mfa import generate_backup_codes, verify_totp_counter

    user = (
        await db.execute(select(User).where(User.id == current_user.id).with_for_update())
    ).scalar_one()
    if user.mfa_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="MFA is already enabled.")
    if not user.mfa_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending MFA setup. Call /auth/mfa/setup first.",
        )
    try:
        secret = decrypt_field(user.mfa_secret_encrypted)
    except Exception:
        user.mfa_secret_encrypted = None
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA setup is invalid; please restart enrollment.",
        )
    enroll_counter = verify_totp_counter(secret, body.code)
    if enroll_counter is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid MFA code.")
    # Seed the replay counter with the enrollment code's timestep (H2-P4) so the
    # very code used to enable MFA cannot be replayed for the first login — the
    # forced re-login (sessions are revoked just below) will use a fresh code.
    user.mfa_last_totp_counter = enroll_counter

    plaintext_codes, hashes = generate_backup_codes()
    # Replace any prior codes (explicit user_id filter; RLS context also set).
    await db.execute(delete(MfaBackupCode).where(MfaBackupCode.user_id == current_user.id))
    for h in hashes:
        db.add(MfaBackupCode(user_id=current_user.id, code_hash=h))
    user.mfa_enabled = True
    user.mfa_enrolled_at = datetime.now(UTC)

    # Revoke ALL sessions so pre-MFA tokens can't survive enrollment. This row is
    # FOR UPDATE-locked on THIS request's connection, so we must NOT call
    # revoke_all_for_user here — its separate NullPool connection would block on
    # our held lock and deadlock the request (Codex HIGH). Instead stamp
    # revoked_at IN this transaction (same connection, no contention) and update
    # the Redis cache BEFORE commit: a cache failure 503s and rolls back, leaving
    # MFA NOT enabled (fail-safe, mirrors the prior revoke-before-commit intent).
    now = datetime.now(UTC)
    user.revoked_at = now
    user.api_key_hash = None
    try:
        await TokenBlacklist.update_revoke_cache(current_user.id, now)
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()
    await db.commit()
    audit_log(request, "mfa_enabled", current_user.id)
    return MfaEnableResponse(backup_codes=plaintext_codes)


async def mfa_disable_for_user(
    body: MfaDisableRequest,
    request: Request,
    current_user: User,
    db: AsyncSession,
) -> None:
    await rate_limit(request, zone="auth")
    # Per-user throttle (not just per-IP): caps 2nd-factor guessing across IPs
    # even after the password check passes.
    await rate_limit(request, zone="auth", identifier=f"mfa-user:{current_user.id}")
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503
    from src.db.models import MfaBackupCode
    from src.utils.crypto import decrypt_field
    from src.utils.mfa import verify_backup_code_hash, verify_totp_counter

    user = (
        await db.execute(select(User).where(User.id == current_user.id).with_for_update())
    ).scalar_one()
    if not user.mfa_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is not enabled.")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password is incorrect.")

    # Second factor: TOTP, else an unused backup code. The TOTP must be REPLAY-
    # FRESH (counter strictly newer than the last consumed) — disabling MFA is a
    # sensitive 2nd-factor check, so a TOTP already used at login can't be
    # replayed to tear MFA down (Codex H2-P4). The row is FOR UPDATE-locked, so
    # the in-Python counter compare is race-safe (no concurrent writer).
    ok = False
    if user.mfa_secret_encrypted:
        try:
            counter = verify_totp_counter(decrypt_field(user.mfa_secret_encrypted), body.code)
        except Exception:
            counter = None
        if counter is not None and counter > (user.mfa_last_totp_counter or -1):
            user.mfa_last_totp_counter = counter
            ok = True
    if not ok:
        unused = (await db.execute(
            select(MfaBackupCode).where(
                MfaBackupCode.user_id == current_user.id,
                MfaBackupCode.used_at.is_(None),
            )
        )).scalars().all()
        ok = any(verify_backup_code_hash(body.code, c.code_hash) for c in unused)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid MFA code.")

    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.mfa_enrolled_at = None
    # Clear the replay counter so a future re-enrollment starts clean (it would
    # be re-seeded by /mfa/enable anyway, but don't leave stale state).
    user.mfa_last_totp_counter = None
    await db.execute(delete(MfaBackupCode).where(MfaBackupCode.user_id == current_user.id))
    # In-session revoke (this users row is FOR UPDATE-locked on our connection —
    # calling revoke_all_for_user would deadlock on a second NullPool connection;
    # Codex HIGH). Stamp revoked_at in this txn + update the cache before commit,
    # 503-and-rollback on cache failure (MFA stays enabled — fail-safe). Also
    # clear the API key: revoke_all stamps revoked_at which the JWT path checks,
    # but the API-key path never consults revoked_at, so a disable that did not
    # clear it would leave one live credential behind (Codex). Mirrors mfa_enable
    # + logout-all + change/reset-password: a sensitive MFA state change kills
    # every credential; the user re-issues via POST /api-key.
    now = datetime.now(UTC)
    user.revoked_at = now
    user.api_key_hash = None
    try:
        await TokenBlacklist.update_revoke_cache(current_user.id, now)
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()
    await db.commit()
    audit_log(request, "mfa_disabled", current_user.id)
