"""Job-scoped, revocable download tokens (purpose=download).

Minted by GET /jobs/{id}/export-url (60s, in-app) and by worker delivery
(longer TTL, for emailed/webhooked links). Verified by GET /jobs/{id}/download,
which checks aud/iss/job_id, the jti blacklist, and logout-all revocation — so a
delivered link can be revoked, unlike a raw R2 presigned URL.

Kept dependency-light (no FastAPI / redis imports) so Celery workers can mint
tokens without pulling the API route surface.
"""
import time
import uuid

import jwt

from src.config import settings

_ALGORITHM = "HS256"


def mint_download_token(user_id: str, job_id: str, ttl_seconds: int = 60) -> str:
    """Return a signed download JWT scoped to (user_id, job_id) with the given TTL."""
    now = int(time.time())
    return jwt.encode(
        {
            "sub": str(user_id),
            "job_id": job_id,
            "purpose": "download",
            "aud": "bridgeleads-download",
            "iss": "bridgeleads",
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + ttl_seconds,
        },
        settings.SECRET_KEY,
        algorithm=_ALGORITHM,
    )


def mint_account_export_token(user_id: str, export_id: str, ttl_seconds: int) -> str:
    """Return a signed token for one account data export (GET /auth/export/{id}/download).

    Its own purpose and claim (``purpose=account_export``, ``export_id``), so a job
    download token can never open an export, nor an export token a job's file. Same
    audience/issuer/jti/iat as a job token, so the same revocation checks apply."""
    now = int(time.time())
    return jwt.encode(
        {
            "sub": str(user_id),
            "export_id": export_id,
            "purpose": "account_export",
            "aud": "bridgeleads-download",
            "iss": "bridgeleads",
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + ttl_seconds,
        },
        settings.SECRET_KEY,
        algorithm=_ALGORITHM,
    )
