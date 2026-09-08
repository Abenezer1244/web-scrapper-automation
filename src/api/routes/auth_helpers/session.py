"""Body logic for GET /auth/onboarding. Extracted from auth.py: the route
decorator and signature stay in auth.py, this holds the moved handler body.

``next_action.route`` is rendered straight into a next/link href by the dashboard
onboarding card, so every value here comes from ``src.config.frontend_routes``
rather than a literal. The literals it replaced carried a ``/dashboard`` prefix
taken from the frontend's ``app/(dashboard)/`` route GROUP, which contributes
nothing to the URL: four of the five actions pointed at pages that do not exist,
and a brand-new account's "New Scraper" CTA landed on the 404 page.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import frontend_routes
from src.db import User


async def onboarding_status_for_user(
    current_user: User,
    db: AsyncSession,
) -> dict:
    from src.db.models import Job, ScraperConfig

    # Check what the user has done
    configs_result = await db.execute(
        select(ScraperConfig).where(ScraperConfig.user_id == current_user.id)
    )
    configs = configs_result.scalars().all()

    jobs_result = await db.execute(
        select(Job).where(Job.user_id == current_user.id)
    )
    jobs = jobs_result.scalars().all()

    done_jobs = [j for j in jobs if j.status == "done"]

    steps = {
        "account_created": True,
        "scraper_configured": len(configs) > 0,
        "first_scrape_run": len(jobs) > 0,
        "first_scrape_completed": len(done_jobs) > 0,
        # Observed, not inferred (migration 089). export_key is written by the
        # worker when it marks a job DONE, so the old predicate answered "an
        # export exists" and ticked this box for users who never downloaded.
        # The grandfather flag covers accounts that already had a finished
        # export at cutover, whose real downloads were never instrumented.
        "first_export_downloaded": (
            current_user.first_leads_downloaded_at is not None
            or bool(current_user.onboarding_download_grandfathered)
        ),
    }

    completed = sum(1 for v in steps.values() if v)
    total = len(steps)

    # Determine next action
    if not steps["scraper_configured"]:
        next_action = {
            "action": "create_scraper",
            "title": "Set up your first scraper",
            "description": "Choose a county and record type to start pulling leads.",
            "cta": "New Scraper",
            "route": frontend_routes.SCRAPERS_NEW,
        }
    elif not steps["first_scrape_run"]:
        config = configs[0]
        next_action = {
            "action": "run_scrape",
            "title": f"Run your first scrape on {config.county.title()}, {config.state.upper()}",
            "description": "Open your scrapers and click 'Run now' to start pulling records from the county portal.",
            "cta": "Open Scrapers",
            "route": frontend_routes.SCRAPERS,
        }
    elif not steps["first_scrape_completed"]:
        next_action = {
            "action": "wait_for_scrape",
            "title": "Your scrape is running",
            "description": "Records are being pulled from the county portal. This usually takes 2-5 minutes.",
            "cta": "View Progress",
            "route": frontend_routes.DASHBOARD,
        }
    elif not steps["first_export_downloaded"]:
        job = done_jobs[0]
        next_action = {
            "action": "download_export",
            "title": f"Download your {job.record_count or 0} leads",
            "description": "Your records are ready. Download the CSV and start mailing today.",
            "cta": "View Results",
            "route": frontend_routes.job_detail(job.id),
        }
    else:
        next_action = {
            "action": "complete",
            "title": "You're all set!",
            "description": "Set up a daily schedule to get fresh leads automatically, or add more counties.",
            "cta": "Add Another County",
            "route": frontend_routes.SCRAPERS_NEW,
        }

    return {
        "steps": steps,
        "completed": completed,
        "total": total,
        "progress_pct": int(completed / total * 100),
        "next_action": next_action,
        "trial_days_remaining": (
            max(0, (current_user.trial_ends_at - datetime.now(UTC)).days)
            if current_user.trial_ends_at else None
        ),
    }
