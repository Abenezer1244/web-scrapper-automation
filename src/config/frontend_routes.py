"""Canonical frontend paths: the single place the API decides where in the app
a link should land.

The Next.js app keeps every signed-in page inside the ``app/(dashboard)/`` route
GROUP. A parenthesised segment contributes NOTHING to the URL, so the live paths
carry no ``/dashboard`` prefix: the create-scraper page is ``/scrapers/new``, and
``/dashboard/scrapers/new`` has never been served by anything. Treating the group
name as a URL segment is exactly what sent the onboarding "New Scraper" CTA to
the 404 page for every brand-new account (2026-09-08).

Three backend surfaces hand a user a link into the app: the ``next_action.route``
of GET /auth/onboarding (rendered as the dashboard onboarding CTA), the lifecycle
emails, and the referral share URL. They all build their paths from here so they
cannot drift apart again. A path added here must correspond to a real ``page.tsx`` in the frontend
repo; ``tests/test_onboarding_routes.py`` guards the onboarding side of that.
"""

from urllib.parse import quote

from src.config.settings import settings

# ─── Pages (paths, as used in-app by next/link) ───────────────────────────────

DASHBOARD = "/dashboard"
SCRAPERS = "/scrapers"
SCRAPERS_NEW = "/scrapers/new"
RESULTS = "/results"
LOGIN = "/login"
REGISTER = "/register"
FORGOT_PASSWORD = "/forgot-password"
BILLING = "/settings?tab=billing"

# Every page path this module hands out. The onboarding tests assert that a
# next_action never points anywhere outside this set, which is what catches a
# route invented from a folder name rather than from a real page.
ALL_PATHS: frozenset[str] = frozenset(
    {
        DASHBOARD,
        SCRAPERS,
        SCRAPERS_NEW,
        RESULTS,
        LOGIN,
        REGISTER,
        FORGOT_PASSWORD,
        BILLING,
    }
)


def job_detail(job_id: str) -> str:
    """The finished-job page: its lead table and the CSV download button.

    The frontend serves this at ``/results/<job id>`` (the page calls getJob on
    the id). There is no ``/jobs/<id>`` page in the app.
    """
    return f"{RESULTS}/{job_id}"


def referral_signup(code: str) -> str:
    """The signup page for a shared referral link, carrying the ref code.

    The page is ``/register`` and it reads ``?ref=`` at mount. ``/signup`` is not
    a page: it is not public either, so a prospect following that link was bounced
    to /login and the referral code was dropped on the floor.
    """
    return f"{REGISTER}?ref={quote(code, safe='')}"


def absolute(path: str) -> str:
    """``path`` as a full URL, for links that leave the app (emails).

    Reads FRONTEND_URL at call time so a settings override applies.
    """
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}"
