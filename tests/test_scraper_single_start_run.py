"""Tests for the single "Start run" simplification.

Two guarantees this change introduces:

1. Scraper creation can no longer produce an INACTIVE (dashboard-invisible) config.
   The old ``POST /scrapers/preview`` one-off path persisted ``active=False`` snapshots
   that ran + billed real records but never showed on the dashboard — the root cause
   of the "I scraped and nothing shows" bug. That path is deleted.
2. Visibility (``active``) and recurrence (``schedule.frequency``) are independent:
   a ``frequency="manual"`` config is active (visible/usable) but is never
   auto-dispatched. That invariant is covered by
   ``test_dispatch_due_jobs.py::test_manual_config_never_dispatched`` and
   ``test_should_run_now.py::test_manual_and_unknown_frequency_never_fire``; here we
   only lock the create side.

Both tests avoid the ``db`` fixture on purpose, so neither writes rows nor triggers
the destructive conftest teardown — they are safe to run anywhere.
"""
import inspect

import pytest


def test_build_scraper_config_has_no_active_param():
    """Regression guard: creation must ALWAYS yield an active (visible) config.

    ``_build_scraper_config`` used to take ``active: bool`` so the preview endpoint
    could persist an inactive snapshot. Reintroducing an inactive-create path (the
    disappearing-run footgun) would re-add this parameter and fail here.
    """
    from src.api.routes.scrapers import _build_scraper_config

    params = inspect.signature(_build_scraper_config).parameters
    assert "active" not in params, (
        "_build_scraper_config must not take an `active` flag — scraper creation is "
        "always active; recurrence is governed solely by schedule.frequency."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preview_endpoint_removed(client):
    """``POST /scrapers/preview`` no longer exists.

    The one-off preview route is gone, so a run can't create an invisible inactive
    config + billed job behind the dashboard's back. The path now only matches the
    parametrized ``/scrapers/{id}`` GET/DELETE/PATCH routes, so POST returns 405
    (method not allowed) — or 404 if unmatched. Never a 2xx "created".
    """
    r = await client.post(
        "/scrapers/preview",
        json={"name": "x", "county": "pierce", "state": "WA", "record_type": "probate"},
    )
    assert r.status_code in (404, 405)
    assert r.status_code not in (200, 201)
