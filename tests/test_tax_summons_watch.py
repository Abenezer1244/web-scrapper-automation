"""The tax-summons watcher (Phase B2): link finding, one county's check, its alerts, and
the watcher's own login.

Real test database (one outer transaction rolled back, as test_tax_summons_review). The
watcher writes as bridgeleads_watcher (SET LOCAL ROLE from the superuser test login,
which migration 173's triggers allow); the page and PDF are passed in, so nothing here
touches the network. The anchors are the live Douglas page's, captured 2026-11-24.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.config import settings
from src.db import session as db_session
from src.db.session import sync_engine
from src.workers.tax_summons import SUMMONS_PAGES
from src.workers.tax_summons_watch import (
    SummonsHoldError,
    alert_for,
    check_county,
    find_summons_link,
)
from tests.test_tax_summons_review import _watcher_role  # noqa: F401 -- module fixture: the watcher role

_PAGE = SUMMONS_PAGES[("douglas", "WA")]
_ORIGIN = "https://www.douglascountywa.gov"
# The live page's document links (2026-11-24): two other PDFs and the summons.
_LIVE_ANCHORS = (
    '<a target="_blank" href="/DocumentCenter/View/12588/Douglas-County---Tax-Title-Properties" '
    'class="fileType pdf">Douglas County - Tax Title Properties</a>'
    '<a target="_blank" href="/DocumentCenter/View/13455/Douglas-County---Tax-foreclosure-Surplus-Funds" '
    'class="fileType pdf">Douglas County - Tax foreclosure Surplus Funds</a>'
    '<a target="_blank" href="/DocumentCenter/View/13454/2026-Notice-and-Summons" '
    'class="fileType pdf">2026 Notice and Summons</a>')
_LIVE_LINK = f"{_ORIGIN}/DocumentCenter/View/13454/2026-Notice-and-Summons"


def _page(*anchors: str) -> str:
    return f"<html><body><div class='fr-view'>{''.join(anchors)}</div></body></html>"


# ── finding the summons on the county page ──

def test_the_live_page_links_the_2026_summons():
    assert find_summons_link(_page(_LIVE_ANCHORS), _PAGE) == (_LIVE_LINK, 2026)


def test_the_highest_year_wins_and_a_repeated_link_is_one_link():
    html = _page('<a href="/DocumentCenter/View/1/2025-Notice-and-Summons">2025</a>', _LIVE_ANCHORS,
                 f'<a href="{_LIVE_LINK}">again, absolute</a>')
    assert find_summons_link(html, _PAGE) == (_LIVE_LINK, 2026)


def test_the_last_accepted_year_is_2100():
    html = _page('<a href="/DocumentCenter/View/7/2100-Notice-and-Summons">x</a>')
    assert find_summons_link(html, _PAGE) == (f"{_ORIGIN}/DocumentCenter/View/7/2100-Notice-and-Summons", 2100)


def test_a_malformed_anchor_beside_the_summons_is_skipped():
    html = _page('<a href="https://[::1/broken">x</a>', '<a href="https://www.douglascountywa.gov:99999/x">y</a>',
                 _LIVE_ANCHORS)
    assert find_summons_link(html, _PAGE) == (_LIVE_LINK, 2026)


@pytest.mark.parametrize("anchors,status", [
    ('<a href="/DocumentCenter/View/12588/Douglas-County---Tax-Title-Properties">sale results</a>', "link_missing"),
    ('<a href="https://evil.example/DocumentCenter/View/1/2026-Notice-and-Summons">x</a>', "link_missing"),
    ('<a href="http://www.douglascountywa.gov/DocumentCenter/View/1/2026-Notice-and-Summons">x</a>', "link_missing"),
    ('<a href="/DocumentCenter/View/1/2026-Notice-and-Summons?x=1">x</a>', "link_missing"),
    (_LIVE_ANCHORS + '<a href="/DocumentCenter/View/99/2026-Notice-and-Summons-Amended">x</a>', "ambiguous"),
    ('<a href="/DocumentCenter/View/1/1999-Notice-and-Summons">x</a>', "link_missing"),
    ('<a href="/DocumentCenter/View/1/2101-Notice-and-Summons">x</a>', "link_missing"),
    ('<a href="/DocumentCenter/View/1/2026-Notice-and-Summons;jsessionid=1">x</a>', "link_missing"),
])
def test_a_page_without_one_clear_summons_link_is_a_hold(anchors, status):
    with pytest.raises(SummonsHoldError) as hold:
        find_summons_link(_page(anchors), _PAGE)
    assert hold.value.status == status


# ── one county's check, against the real store ──

@pytest.fixture
def c():
    with sync_engine.connect() as conn:
        outer = conn.begin()

        @contextmanager
        def as_watcher():
            # SET LOCAL inside the session's savepoint: a failure rolls the savepoint back,
            # which restores the role; a commit keeps it to the end of the test.
            s = Session(bind=conn, join_transaction_mode="create_savepoint")
            s.execute(text("SET LOCAL ROLE bridgeleads_watcher"))
            try:
                yield s
            finally:
                s.close()

        conn.watcher = as_watcher
        try:
            yield conn
        finally:
            outer.rollback()


_PDF_2026 = b"%PDF-1.3 the 2026 summons as first served"


def _check(c, *, page=_page(_LIVE_ANCHORS), pdf=_PDF_2026, page_error=None, pdf_error=None):
    def get_page(url):
        assert url == _PAGE
        if page_error:
            raise SummonsHoldError("page_failed", page_error)
        return page

    def get_pdf(url):
        assert url.startswith(_ORIGIN)
        if pdf_error:
            raise SummonsHoldError("download_failed", pdf_error)
        return pdf

    return check_county("douglas", "WA", session_factory=c.watcher, get_page=get_page, get_pdf=get_pdf)


def _serving(c) -> str:
    return c.execute(text("SELECT status FROM tax_summons_observations WHERE county = 'douglas'")).scalar()


def test_a_first_check_opens_a_version_for_review(c):
    result = _check(c)
    assert (result["status"], result["created"], result["serving"]) == ("ok", True, "unpublished")
    v = c.execute(text("SELECT sha256, source_url, status FROM tax_summons_versions")).one()
    assert (v.sha256, v.source_url, v.status) == (hashlib.sha256(_PDF_2026).hexdigest(), _LIVE_LINK, "pending")
    again = _check(c)
    assert (again["created"], again["version_id"]) == (False, result["version_id"])


def test_a_re_stamped_copy_is_a_new_version(c):
    first = _check(c)["version_id"]
    second = _check(c, pdf=b"%PDF-1.3 the 2026 summons with one more REDEEMED stamp")
    assert second["created"] and second["version_id"] != first
    assert c.execute(text("SELECT count(*) FROM tax_summons_versions")).scalar() == 2


@pytest.mark.parametrize("kw,status", [
    ({"page_error": "page request failed (ConnectionError)"}, "page_failed"),
    ({"pdf_error": "summons answered 404 text/html"}, "download_failed"),
    ({"page": _page('<a href="/DocumentCenter/View/12588/Tax-Title-Properties">x</a>')}, "link_missing"),
])
def test_a_failed_check_records_a_hold_and_keeps_the_last_copy(c, kw, status):
    first = _check(c)["version_id"]
    result = _check(c, **kw)
    assert (result["status"], result["version_id"], result["serving"]) == (status, first, status)
    assert result["reason"]
    assert _serving(c) == status


def test_a_county_site_too_slow_for_the_time_limit_still_holds_the_leads(c):
    from celery.exceptions import SoftTimeLimitExceeded

    first = _check(c)["version_id"]

    def slow_pdf(url):
        raise SoftTimeLimitExceeded()  # Celery's soft limit, fired mid-download

    with pytest.raises(SoftTimeLimitExceeded) as timed_out:
        check_county("douglas", "WA", session_factory=c.watcher, get_page=lambda u: _page(_LIVE_ANCHORS),
                     get_pdf=slow_pdf)
    assert timed_out.value.result["status"] == "download_failed"  # what the task alerts on
    assert alert_for("douglas", "WA", timed_out.value.result, "2026-11-24")[0] == "tax_summons_hold"
    assert _serving(c) == "download_failed"
    assert str(c.execute(text("SELECT current_version_id FROM tax_summons_observations")).scalar()) == first


def test_an_earlier_years_summons_after_a_later_one_is_a_rollback(c):
    _check(c)
    result = _check(c, page=_page('<a href="/DocumentCenter/View/1/2025-Notice-and-Summons">x</a>'),
                    pdf=b"%PDF-1.3 last year's summons")
    assert result["status"] == "rollback"
    assert c.execute(text("SELECT count(*) FROM tax_summons_versions")).scalar() == 1


def test_a_second_run_while_one_holds_the_county_is_skipped(c):
    with c.watcher() as s:
        from src.workers.tax_summons import claim_watch

        assert claim_watch(s, county="douglas", state="WA", lease_seconds=900) is not None
        s.commit()
    assert _check(c)["status"] == "skipped"


def test_a_check_without_the_watcher_login_is_refused(c):
    @contextmanager
    def as_superuser():
        s = Session(bind=c, join_transaction_mode="create_savepoint")
        try:
            yield s
        finally:
            s.close()

    with pytest.raises(Exception, match="only the watcher"):
        check_county("douglas", "WA", session_factory=as_superuser, get_page=lambda u: _page(_LIVE_ANCHORS),
                     get_pdf=lambda u: _PDF_2026)


# ── what ops is told ──

_TODAY = "2026-11-24"


def test_alerts():
    vid = "2b3c4d5e-0000-4000-8000-000000000000"
    assert alert_for("douglas", "WA", {"status": "skipped"}, _TODAY) is None
    new = alert_for("douglas", "WA", {"status": "ok", "serving": "unpublished", "created": True,
                                      "version_id": vid}, _TODAY)
    assert new[0:2] == ("tax_summons_new_copy", vid) and "fetch --version" in new[3]
    reminder = alert_for("douglas", "WA", {"status": "ok", "serving": "unpublished", "created": False,
                                           "version_id": vid}, _TODAY)
    assert reminder[0:2] == ("tax_summons_unpublished", f"{vid}:{_TODAY}")
    hold = alert_for("douglas", "WA", {"status": "link_missing", "version_id": vid,
                                       "reason": "the county page links no Notice and Summons"}, _TODAY)
    assert hold[0:2] == ("tax_summons_hold", "douglas/WA:link_missing") and "held" in hold[2]
    stale_copy = alert_for("douglas", "WA", {"status": "ok", "serving": "superseded", "version_id": vid}, _TODAY)
    assert stale_copy[0:2] == ("tax_summons_hold", "douglas/WA:superseded")
    assert alert_for("douglas", "WA", {"status": "ok", "serving": "ok", "version_id": vid}, _TODAY) is None


# ── the watcher's own login ──

@pytest.fixture
def fresh_watcher_engine(monkeypatch):
    monkeypatch.setattr(db_session, "_watcher_engine", None)
    yield
    if db_session._watcher_engine is not None:
        db_session._watcher_engine.dispose()
    db_session._watcher_engine = None


def test_the_watcher_does_not_run_without_its_own_url(monkeypatch, fresh_watcher_engine):
    monkeypatch.setattr(settings, "DATABASE_URL_WATCHER", "")
    with pytest.raises(db_session.WatcherNotConfiguredError, match="not set"):
        with db_session.watcher_sync_session():
            pass


def test_the_watcher_refuses_a_url_that_logs_in_as_another_role(monkeypatch, fresh_watcher_engine):
    monkeypatch.setattr(settings, "DATABASE_URL_WATCHER", settings.DATABASE_URL_SYNC)  # the test superuser
    with pytest.raises(db_session.WatcherNotConfiguredError, match="does not log in as the watcher"):
        with db_session.watcher_sync_session():
            pass
