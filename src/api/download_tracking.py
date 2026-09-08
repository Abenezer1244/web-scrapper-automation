"""Records that a user actually received a leads CSV.

Before this existed, three places asked "has this user downloaded their leads?"
and all three answered with ``jobs.export_key IS NOT NULL``. The worker writes
export_key when it marks a job DONE, before anyone downloads anything, so the
question was really "did an export get produced": the onboarding checklist ticked
"Download leads" for users who never downloaded, its download step was unreachable
on a normal success, the day-3 activation email told activated-looking users they
were done, and the admin activation funnel's job_to_download rate read ~100% by
construction, hiding the real dropoff.

The fact all three want is user-level ("did this person ever get a CSV"), not
job-level, so it lives on ``users.first_leads_downloaded_at``. That also sidesteps
an attribution problem: a combined batch or segment CSV is rendered from a
filtered, deduplicated selection across many jobs, so stamping "the jobs it came
from" would be a guess. One user, one timestamp, every CSV path.

Call this from a response BackgroundTask, never inline: Starlette runs those after
the bytes are sent, so a bookkeeping failure cannot cost the user the download they
already have. It is deliberately not retried, so a process dying mid-task loses one
observation. Under-counting an activation metric is the acceptable failure here;
denying someone their leads is not.
"""

from datetime import UTC, datetime

from sqlalchemy import text, update

from src.db.models import User
from src.db.session import AsyncSessionLocal
from src.utils.logger import setup_logger

_logger = setup_logger("api.download_tracking")


async def mark_leads_downloaded(user_id: str) -> None:
    """Stamp this user's FIRST leads download, once.

    Conditional on the column still being NULL, so concurrent downloads keep the
    earliest recorded one rather than racing to overwrite it, and a user's tenth
    download does not move the timestamp.

    Opens its own session: the request's session has already been committed and
    closed by the time a BackgroundTask runs.
    """
    try:
        async with AsyncSessionLocal() as session:
            # RLS belt, matching get_rls_db. The runtime role currently bypasses
            # RLS, but this write must keep working after that cutover.
            session.sync_session.info["rls_user_id"] = str(user_id)
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": str(user_id)},
            )
            await session.execute(
                update(User)
                .where(
                    User.id == user_id,
                    User.first_leads_downloaded_at.is_(None),
                )
                .values(first_leads_downloaded_at=datetime.now(UTC))
            )
            await session.commit()
    except Exception:
        # The CSV has already been sent. Surface the failure in the logs and let
        # it propagate to the ASGI server's error handler rather than swallowing
        # it: a persistently failing write means the activation funnel is going
        # quietly wrong, which is exactly what this module exists to stop.
        _logger.exception("Failed to record leads download for user %s", user_id)
        raise
