"""Body logic for the registration beat tasks:

  * `_dispatch_pending_verification_emails_impl` — the verification-email
    OUTBOX DISPATCHER. pending_registrations rows are the durable outbox; this
    beat sends each due row's verification email and records the outcome on the
    row, so a signup made while Redis (the Celery broker) is down is drained and
    sent once Redis recovers, never lost on a fire-and-forget enqueue.
  * `_purge_expired_pending_registrations_impl` — batched delete of expired rows.
"""

from datetime import UTC, datetime, timedelta

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")

# Per-address email-bomb guard: at most one REAL verification send per address
# per this window, AND at most _MAX_VERIFY_EMAILS_PER_DAY real sends per address
# per rolling 24h (the window alone would still allow ~720/day to a targeted
# address). A blocked attempt is marked 'suppressed' (dequeued without sending).
# Enforced under a Postgres advisory lock over the 'sent' rows, so it holds even
# during a Redis outage (no once_per dependency). 10/day leaves ample room for a
# legitimate user re-requesting while capping an attacker's inbox flood.
_VERIFY_EMAIL_WINDOW_SECONDS = 120
_MAX_VERIFY_EMAILS_PER_DAY = 10

# Dispatcher batch + backoff. A transient send failure pushes next_email_attempt_at
# forward by an exponential backoff (capped); the beat reclaims the row when due.
# The beat IS the sole retry driver (no Celery self.retry), so retries can't
# double-schedule. After _DISPATCH_MAX_ATTEMPTS transient failures the row is
# marked 'failed' and ops-alerted.
_DISPATCH_BATCH = 100
_DISPATCH_MAX_ATTEMPTS = 10
_DISPATCH_BACKOFF_BASE = 60      # seconds
_DISPATCH_BACKOFF_CAP = 3600     # 1 hour

# Purge deletes in bounded batches so a spray of abandoned signups can't make
# one giant locking DELETE (Codex P3).
_PURGE_BATCH = 1000


def _dispatch_pending_verification_emails_impl() -> None:
    """Send verification emails for due pending_registrations rows.

    Gated on EMAIL_VERIFICATION_ENABLED (the flag that creates the rows) and on
    RESEND_API_KEY (a missing key is a config error: log loudly and leave rows
    pending — they send once the key is set; do NOT burn attempts, and do NOT
    ops-alert through the same broken Resend channel).

    Per row, in its own short transaction:
      1. Lock the row FOR UPDATE SKIP LOCKED (a concurrent dispatcher skips it).
      2. pg_try_advisory_xact_lock(hashtext(email_hmac)) serializes sends to the
         SAME address across different rows WITHOUT blocking — if another row for
         this address is mid-send, skip and retry next tick.
      3. Bomb guard: if a REAL send to this address happened < the window ago,
         mark this row 'suppressed' (no send). Only 'sent' rows count, so a
         suppressed duplicate can't poison the guard.
      4. Mint the token (exp == the row's expires_at, so the link never out/under-
         lives the row), send (raises on failure), and record the outcome.
    """
    if not settings.EMAIL_VERIFICATION_ENABLED:
        return
    if not settings.RESEND_API_KEY:
        _logger.error(
            "EMAIL_VERIFICATION_ENABLED is on but RESEND_API_KEY is unset — "
            "verification emails cannot be sent; rows left pending until configured"
        )
        return

    from sqlalchemy import select, text

    from src.api.routes.auth_helpers.tokens import (
        _VERIFY_TOKEN_EXPIRE_SECONDS,
        _mint_verify_token,
    )
    from src.db.models import PendingRegistration
    from src.db.session import system_sync_session
    from src.workers.delivery import _is_retryable_email_error
    from src.workers.onboarding_emails import send_verification_email

    sent = suppressed = failed = retried = 0
    with system_sync_session() as db:
        now = datetime.now(UTC)
        candidate_ids = (
            db.execute(
                select(PendingRegistration.id)
                .where(
                    PendingRegistration.email_dispatch_state == "pending",
                    PendingRegistration.expires_at > now,
                    PendingRegistration.next_email_attempt_at <= now,
                )
                .order_by(PendingRegistration.next_email_attempt_at)
                .limit(_DISPATCH_BATCH)
            )
            .scalars()
            .all()
        )
        # End the read transaction before per-row work (don't hold it across sends).
        db.rollback()

        for pending_id in candidate_ids:
            try:
                now = datetime.now(UTC)
                row = (
                    db.execute(
                        select(PendingRegistration)
                        .where(PendingRegistration.id == pending_id)
                        .with_for_update(skip_locked=True)
                    )
                    .scalars()
                    .one_or_none()
                )
                # Gone, locked by another dispatcher, or no longer eligible. The
                # next_email_attempt_at recheck is load-bearing under concurrent
                # dispatchers (Codex): another dispatcher that had this id in its
                # candidate list could lock the row AFTER a peer recorded a backoff
                # and, without this check, send immediately and defeat the backoff.
                if (
                    row is None
                    or row.email_dispatch_state != "pending"
                    or row.expires_at <= now
                    or row.next_email_attempt_at > now
                ):
                    db.rollback()
                    continue

                # Serialize sends to this address (non-blocking) — releases on commit.
                got_lock = db.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtext(:h))"),
                    {"h": row.email_hmac},
                ).scalar()
                if not got_lock:
                    db.rollback()  # another row for this address is mid-send
                    continue

                # Bomb guard: only REAL ('sent') sends count. One query gets both
                # the last send (120s window) and the rolling-24h count (daily cap).
                day_ago = now - timedelta(days=1)
                guard = db.execute(
                    text(
                        "SELECT max(verification_email_sent_at) AS last_sent, "
                        "count(*) FILTER (WHERE verification_email_sent_at > :day_ago) AS day_count "
                        "FROM pending_registrations "
                        "WHERE email_hmac = :h AND email_dispatch_state = 'sent'"
                    ),
                    {"h": row.email_hmac, "day_ago": day_ago},
                ).one()
                last_sent, day_count = guard.last_sent, guard.day_count
                within_window = last_sent is not None and (
                    now - last_sent
                ) < timedelta(seconds=_VERIFY_EMAIL_WINDOW_SECONDS)
                if within_window or day_count >= _MAX_VERIFY_EMAILS_PER_DAY:
                    row.email_dispatch_state = "suppressed"
                    db.commit()
                    suppressed += 1
                    continue

                # Deadline = 24h from THIS send, applied ONLY on success below. A
                # delayed/recovered send (post-outage) thus gives the user a full
                # fresh window instead of the stale remainder of the original
                # signup+24h, and — because purge keys off expires_at — the row is
                # retained for a real 24h after send so the rolling-24h bomb-guard
                # count can't be truncated by an early purge (Codex). The token is
                # minted against this same deadline.
                new_deadline = now + timedelta(seconds=_VERIFY_TOKEN_EXPIRE_SECONDS)
                token = _mint_verify_token(row.id, new_deadline)
                # Token in the URL FRAGMENT (#), not the query (?): a fragment is
                # never sent to a server (RFC 3986 §3.5), so the bearer token can't
                # leak via access logs or the Referer header. The FE reads it from
                # location.hash and scrubs it. (Same change applied to reset links.)
                verify_link = f"{settings.FRONTEND_URL}/verify-email#token={token}"
                # Delivery is at-least-once: the send is committed to the provider
                # BEFORE we record 'sent', so a worker crash (or a provider-success
                # / client-timeout) between the two re-sends next tick. Resend 2.7.0
                # exposes no idempotency key, and a duplicate verification email is
                # benign — both links point at THIS row, redeeming one deletes every
                # pending row for the address, so the second click just 400s. Same
                # accepted bar as deliver_job_email.
                try:
                    send_verification_email(row.email, verify_link)
                except Exception as exc:  # noqa: BLE001 — classify + record, never crash the beat
                    row.email_attempts += 1
                    if (
                        _is_retryable_email_error(exc)
                        and row.email_attempts < _DISPATCH_MAX_ATTEMPTS
                    ):
                        backoff = min(
                            _DISPATCH_BACKOFF_CAP,
                            _DISPATCH_BACKOFF_BASE * (2 ** (row.email_attempts - 1)),
                        )
                        row.next_email_attempt_at = now + timedelta(seconds=backoff)
                        db.commit()
                        retried += 1
                        _logger.warning(
                            "verification email retry %d/%d for pending=%s: %s",
                            row.email_attempts, _DISPATCH_MAX_ATTEMPTS,
                            pending_id, type(exc).__name__,
                        )
                        continue
                    # Permanent error, or retries exhausted: mark failed + alert.
                    row.email_dispatch_state = "failed"
                    db.commit()
                    failed += 1
                    _logger.error(
                        "verification email FAILED permanently for pending=%s after %d attempt(s): %s",
                        pending_id, row.email_attempts, type(exc).__name__,
                    )
                    try:
                        from src.workers.ops_alerts import send_ops_alert
                        send_ops_alert(
                            "verification_email", str(pending_id),
                            "Verification email failed permanently",
                            f"A signup verification email could not be sent after "
                            f"{row.email_attempts} attempt(s): {type(exc).__name__}. "
                            f"The signup is stuck pending until the user retries.",
                        )
                    except Exception:  # noqa: BLE001 — alert is best-effort
                        pass
                    continue

                row.expires_at = new_deadline  # fresh 24h window from the actual send
                row.email_dispatch_state = "sent"
                row.verification_email_sent_at = now
                db.commit()
                sent += 1
            except Exception:  # noqa: BLE001 — isolate one bad row, keep draining
                db.rollback()
                _logger.exception("verification dispatch error for pending=%s", pending_id)

    if sent or suppressed or failed or retried:
        _logger.info(
            "verification dispatch: sent=%d suppressed=%d retried=%d failed=%d",
            sent, suppressed, retried, failed,
        )


def _purge_expired_pending_registrations_impl() -> None:
    """Delete expired pending_registrations rows in bounded batches.

    Each /auth/register attempt under EMAIL_VERIFICATION_ENABLED inserts its own
    pending row, and verify only drops the row(s) for a verified address. Signups
    that are never confirmed (abandoned, or sprayed by an attacker across IPs)
    otherwise linger — the expires_at filter stops them being honored but does not
    remove them. This hourly sweep deletes expired rows (any dispatch state) so
    the table cannot grow unbounded. Batched with a LIMIT so a large backlog can't
    become one giant locking DELETE (Codex P3). Uses the cross-tenant system
    session: pending_registrations is pre-account and not tenant-scoped (no
    user_id), and the expires_at index keeps each batch cheap.
    """
    from sqlalchemy import text

    from src.db.session import system_sync_session

    total = 0
    now = datetime.now(UTC)
    with system_sync_session() as db:
        while True:
            # DELETE ... WHERE id IN (SELECT ... LIMIT n) — Postgres has no
            # DELETE ... LIMIT, so bound the batch via a subselect.
            result = db.execute(
                text(
                    "DELETE FROM pending_registrations WHERE id IN ("
                    "  SELECT id FROM pending_registrations WHERE expires_at < :now "
                    "  LIMIT :batch FOR UPDATE SKIP LOCKED)"
                ),
                {"now": now, "batch": _PURGE_BATCH},
            )
            db.commit()
            count = result.rowcount or 0
            total += count
            if count < _PURGE_BATCH:
                break
    if total:
        _logger.info("Purged %d expired pending registrations", total)
