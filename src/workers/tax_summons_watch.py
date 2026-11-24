"""The tax-summons watcher (Phase B2): once a day, confirm which copy of each county's
tax-foreclosure Notice and Summons the county serves, and hold its tax leads on anything
else (tax_summons.serving_status is what Phase C asks).

One run, per county (check_county):
  1. claim the county (a fencing token) and COMMIT, before anything is fetched;
  2. outside any transaction: GET the county's foreclosure page, find the summons link
     (exact county HTTPS origin, the highest year; two different links in that year is
     'ambiguous'; none is 'link_missing'), GET the PDF (200, application/pdf, %PDF-, size
     cap), sha256 it;
  3. in a NEW transaction record the result: the copy it confirmed (a new digest becomes
     a pending version for review) or the hold; a run whose claim was taken over is
     refused (record_observation);
  4. after that commit, alert ops: a new copy to review, a daily reminder while the
     current copy is unpublished, a hold, or the county serving a superseded copy.

Every write goes through the watcher's own login (watcher_sync_session: the database
must see bridgeleads_watcher as both login and role); migration 173's triggers refuse it
from anyone else. Alerts carry the county, codes and version ids, never names.
"""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse, urlsplit

from bs4 import BeautifulSoup
from celery.exceptions import SoftTimeLimitExceeded

from src.utils.celery_limits import reraise_time_limit
from src.utils.logger import setup_logger
from src.workers import app
from src.workers.tax_summons import (
    HOLD_STATUSES,
    MAX_SUMMONS_BYTES,
    SUMMONS_PAGES,
    TaxReviewError,
    claim_watch,
    record_observation,
    serving_status,
)

_logger = setup_logger("worker.tax_summons_watch")

# A run's claim outlives the task's hard limit, so a live run is never taken over.
LEASE_SECONDS = 900
SOFT_LIMIT_SECONDS, HARD_LIMIT_SECONDS = 600, 720
_MAX_PAGE_BYTES = 2_000_000
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0", "Cache-Control": "no-cache", "Pragma": "no-cache"}
# /DocumentCenter/View/13454/2026-Notice-and-Summons (CivicPlus). The year is the
# summons' own; document ids carry no order.
_SUMMONS_PATH = re.compile(r"/DocumentCenter/View/\d+/(\d{4})-Notice-and-Summons(?:[-_][^/]*)?",
                           re.ASCII | re.IGNORECASE)


class WatchTimedOutError(SoftTimeLimitExceeded):
    """Celery's soft limit, raised after the hold it forced was recorded: carries that
    result so the task can still alert ops before the timeout propagates."""

    def __init__(self, result: dict):
        super().__init__("the county site did not answer in time")
        self.result = result


class SummonsHoldError(Exception):
    """A check that could not confirm the county's copy; ``status`` is one of
    tax_summons.HOLD_STATUSES. The message is a code-level reason, never page text."""

    def __init__(self, status: str, reason: str):
        assert status in HOLD_STATUSES
        super().__init__(reason)
        self.status = status


def _media_type(resp) -> str:
    return resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def find_summons_link(page_html: str, page_url: str) -> tuple[str, int]:
    """The summons PDF the page links: (absolute URL, year). Only links on the page's own
    HTTPS origin count. Raises SummonsHoldError('link_missing') when there is none and
    SummonsHoldError('ambiguous') when the highest year has two different links."""
    from src.utils.safe_http import same_origin

    found: dict[str, int] = {}
    for a in BeautifulSoup(page_html, "html.parser").find_all("a", href=True):
        try:  # one malformed anchor never sinks the check
            url = urljoin(page_url, a["href"].strip()).split("#", 1)[0]
            parts = urlsplit(url)
            m = _SUMMONS_PATH.fullmatch(parts.path)
            if (m and parts.scheme == "https" and not parts.query and same_origin(url, page_url)
                    and 2000 <= int(m.group(1)) <= 2100):
                found[url] = int(m.group(1))
        except ValueError:
            continue
    if not found:
        raise SummonsHoldError("link_missing", "the county page links no Notice and Summons")
    year = max(found.values())
    links = sorted(u for u, y in found.items() if y == year)
    if len(links) > 1:
        raise SummonsHoldError("ambiguous", f"the county page links {len(links)} different {year} summons")
    return links[0], year


def fetch_page(page_url: str) -> str:
    """The county's foreclosure page (allowlisted host, 200, HTML)."""
    import requests

    from src.api.middleware.security import add_scrape_domain
    from src.utils.safe_http import safe_get

    add_scrape_domain(_host(page_url))
    try:
        resp = safe_get(page_url, require_allowlisted=True, headers=_HEADERS, timeout=30, max_bytes=_MAX_PAGE_BYTES)
    except (ValueError, requests.RequestException) as exc:
        raise SummonsHoldError("page_failed", f"page request failed ({type(exc).__name__})") from None
    if resp.status_code != 200 or _media_type(resp) != "text/html":
        # Fixed wording: the content type is the remote site's text, never sent to ops.
        kind = "HTML" if _media_type(resp) == "text/html" else "not HTML"
        raise SummonsHoldError("page_failed", f"page answered HTTP {resp.status_code}, {kind}")
    return resp.text


def fetch_summons_pdf(url: str) -> bytes:
    """The linked PDF (same allowlisted host, no redirects, 200, application/pdf, %PDF-,
    at most MAX_SUMMONS_BYTES)."""
    import requests

    from src.utils.safe_http import safe_get

    try:
        resp = safe_get(url, require_allowlisted=True, headers=_HEADERS, timeout=90, max_bytes=MAX_SUMMONS_BYTES)
    except (ValueError, requests.RequestException) as exc:
        raise SummonsHoldError("download_failed", f"summons download failed ({type(exc).__name__})") from None
    if resp.status_code != 200 or _media_type(resp) != "application/pdf":
        kind = "a PDF" if _media_type(resp) == "application/pdf" else "not a PDF"
        raise SummonsHoldError("download_failed", f"summons answered HTTP {resp.status_code}, {kind}")
    if not resp.content.startswith(b"%PDF-"):
        raise SummonsHoldError("download_failed", "the summons link did not return a PDF")
    return resp.content


def check_county(county: str, state: str, *, session_factory, get_page=fetch_page, get_pdf=fetch_summons_pdf,
                 lease_seconds: int = LEASE_SECONDS) -> dict:
    """One watcher run for one county: claim, fetch, record (see the module docstring).
    Returns the recorded result plus ``serving`` (what Phase C would answer now) and
    ``reason`` (a hold's cause), or {"status": "skipped"} while another run holds it."""
    with session_factory() as db:
        token = claim_watch(db, county=county, state=state, lease_seconds=lease_seconds)
        db.commit()
    if token is None:
        return {"status": "skipped", "reason": "another watcher run holds the county"}
    page_url = SUMMONS_PAGES[(county, state)]
    found: dict = {"hold": None}
    reason, out_of_time = None, None
    stage = "page_failed"
    try:
        link, year = find_summons_link(get_page(page_url), page_url)
        stage = "download_failed"
        body = get_pdf(link)
        found.update(source_url=link, link_year=year, sha256=hashlib.sha256(body).hexdigest())
    except SummonsHoldError as hold:
        found, reason = {"hold": hold.status}, str(hold)
    except SoftTimeLimitExceeded as exc:
        # A county site that answers too slowly must not leave the last confirmation
        # servable: record the hold in the time left before the hard limit, then stop.
        found, reason, out_of_time = {"hold": stage}, "the county site did not answer in time", exc
    with session_factory() as db:
        result = record_observation(db, county=county, state=state, token=token, **found)
        serving, _ = serving_status(db, county=county, state=state)
        db.commit()
    outcome = {**result, "serving": serving, "reason": reason}
    if out_of_time is not None:
        raise WatchTimedOutError(outcome) from out_of_time
    return outcome


def alert_for(county: str, state: str, result: dict, today: str) -> tuple[str, str, str, str] | None:
    """The ops alert a result calls for, as (kind, key, subject, body), or None. Keys make
    a new copy alert once per version, a reminder once per version a day, and a hold once
    per county and cause (send_ops_alert's cooldown)."""
    place = f"{county.title()} County, {state}"
    status, serving, vid = result.get("status"), result.get("serving"), result.get("version_id")
    review = ("Review it: railway run --service worker python scripts/tax_summons_review.py "
              f"--operator-email <you> fetch --version {vid} --out summons.pdf, then manifest, draft, load, "
              "show, publish (see the script's help).")
    if status == "skipped":
        return None
    if status in HOLD_STATUSES:
        return ("tax_summons_hold", f"{county}/{state}:{status}", f"{place} tax summons: {status}; tax leads held",
                f"The daily summons check could not confirm the county's copy ({status}: {result.get('reason')}). "
                f"{place} tax leads are held until a check confirms it again. Last confirmed version: {vid}.")
    if serving == "superseded":
        return ("tax_summons_hold", f"{county}/{state}:superseded",
                f"{place} tax summons: the county serves an older copy; tax leads held",
                f"The county serves version {vid}, which a later publication superseded. Tax leads are held "
                f"until the county serves a copy that can be published.")
    if serving == "unpublished" and result.get("created"):
        return ("tax_summons_new_copy", str(vid), f"{place} tax summons: a new copy to review",
                f"The county now serves a new copy of its tax-foreclosure summons (version {vid}). "
                f"{place} tax leads are held until it is reviewed and published. {review}")
    if serving == "unpublished":
        return ("tax_summons_unpublished", f"{vid}:{today}", f"{place} tax summons: still awaiting review",
                f"Version {vid} is the copy the county serves and it is not published yet; {place} tax leads "
                f"stay held. {review}")
    return None


@app.task(name="src.workers.tax_summons_watch.watch_tax_summons",
          soft_time_limit=SOFT_LIMIT_SECONDS, time_limit=HARD_LIMIT_SECONDS)
def watch_tax_summons() -> dict:
    """Daily: check every county with a summons page. A failure in one county never
    stops the next; each is logged and alerted by code, never by page text."""
    from src.db.session import WatcherNotConfiguredError, watcher_sync_session
    from src.workers.ops_alerts import send_ops_alert

    today = datetime.now(UTC).date().isoformat()
    outcomes: dict[str, str] = {}
    for county, state in SUMMONS_PAGES:
        where = f"{county}/{state}"
        try:
            result = check_county(county, state, session_factory=watcher_sync_session)
        except WatcherNotConfiguredError as exc:
            outcomes[where] = "unconfigured"
            _logger.error("tax summons watcher %s: not configured (%s)", where, exc)
            send_ops_alert("tax_summons_watcher_unconfigured", where,
                           f"Tax summons watcher cannot run for {where}",
                           f"The watcher has no working login of its own ({exc}). Tax leads stay held. Set "
                           "DATABASE_URL_WATCHER on the worker to the bridgeleads_watcher login "
                           "(scripts/provision_rls_roles.sql -v watch_pw).")
            continue
        except WatchTimedOutError as exc:  # the hold is recorded: say so, then stop as Celery asked
            alert = alert_for(county, state, exc.result, today)
            if alert:
                send_ops_alert(*alert)
            raise
        except TaxReviewError as exc:
            outcomes[where] = "refused"
            _logger.warning("tax summons watcher %s: result refused (%s)", where, exc)
            continue
        except Exception as exc:  # noqa: BLE001 -- one county's failure is reported, never silent
            reraise_time_limit(exc)
            outcomes[where] = "failed"
            _logger.error("tax summons watcher %s failed: %s", where, type(exc).__name__)
            send_ops_alert("tax_summons_watch_failed", where, f"Tax summons watcher failed for {where}",
                           f"The daily summons check raised {type(exc).__name__}. Tax leads keep their last "
                           "state until a check succeeds; they go stale (held) after a day and a half.")
            continue
        outcomes[where] = f"{result['status']}/{result.get('serving')}"
        _logger.info("tax summons watcher %s: %s", where, outcomes[where])
        alert = alert_for(county, state, result, today)
        if alert:
            send_ops_alert(*alert)
    return outcomes
