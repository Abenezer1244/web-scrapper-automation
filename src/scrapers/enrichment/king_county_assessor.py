"""King County address enrichment — hybrid HTTP + Playwright.

Step 1 (HTTP, fast): eRealProperty → property address + tax bill URL
Step 2 (Playwright, reliable): payment.kingcounty.gov → mailing address

500 parcels in ~5 min:
- Step 1: 500 × 1s = ~8 min (but can run 5 concurrent HTTP requests = ~2 min)
- Step 2: 500 × 4s / 1 tab = ~33 min → too slow
- Better: use 3 Playwright tabs for step 2 = ~11 min total

Actually: since Step 2 only needs Playwright for JS-rendered content,
we run Step 1 (HTTP) for ALL parcels first (fast), then Step 2 (Playwright)
for the subset that need mailing addresses.
"""

import asyncio
import html
import re
from collections import deque
from dataclasses import dataclass

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper
from src.scrapers.enrichment.source_health import (
    KING_EREALPROPERTY,
    check_source_or_raise,
    record_source_blocked,
)
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.enrichment.king_assessor")

_ERP_URL = "https://blue.kingcounty.com/Assessor/eRealProperty/Dashboard.aspx?ParcelNbr="
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}

add_scrape_domain("blue.kingcounty.com")
add_scrape_domain("payment.kingcounty.gov")

# eRealProperty Dashboard labels the owner/taxpayer cell `<td>Name</td><td>VALUE`
# exactly once per page. The label cell is plain text (no nested tags on the live
# page); the VALUE cell is captured lazily to its closing </td> and tag-stripped,
# so markup inside the value is tolerated. Case- and whitespace-insensitive.
# King joins co-owners with "+"; entity owners (LLC/bank/estate) are valid
# tax-delinquent leads, so no person-vs-agency orientation is applied.
_OWNER_RE = re.compile(
    r"<td[^>]*>\s*Name\s*</td>\s*<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL
)
# Reject placeholders the assessor sometimes serves so we never overwrite a
# labeled lead with junk. Compared after stripping non-alphanumerics, so "N/A",
# "N.A.", and "N / A" all collapse to "NA".
_OWNER_JUNK = frozenset({"NA", "NONE", "NULL", "UNKNOWN"})

# Phase-1 breaker: trip when this many of the last N fetches came back non-200.
# 30/50 is well clear of the normal miss rate (a genuine 200-with-no-data page is
# NOT a failure here — only a non-200 counts), so ordinary sparse parcels can
# never trip it, while a real block trips it within ~50 requests.
_PHASE1_BREAKER_WINDOW = 50
_PHASE1_BREAKER_MIN_FAILURES = 30

# eRealProperty SILENTLY TRUNCATES an over-length ParcelNbr to the first 10 digits
# and serves a DIFFERENT parcel's page with no error (verified live 2026-09-03:
# ParcelNbr=64116000027 returns parcel 641160-0002, owner SNYDER JACOB, site
# 11524 MERIDIAN AVE N — while the lead's decedent was REINKE NORMAN LEONARD,
# whose parcel 6411600027 is 11547 CORLISS AVE N). King's own recorder emits
# malformed PIDs in its legal-description index, so this is reachable from real
# scraped data and it attaches ANOTHER PROPERTY'S address to a lead.
#
# The page states which parcel it actually resolved, so read it back and compare.
# LABEL-ANCHORED on the "Parcel Number" cell (Codex): never "the first 10-digit
# number on the page" — the page is full of unrelated numbers.
_PARCEL_ECHO_RE = re.compile(
    r"<td[^>]*>\s*Parcel\s*(?:Number|Nbr)?\s*</td>\s*<td[^>]*>(.*?)</td>",
    re.IGNORECASE | re.DOTALL,
)

# King PIN = 6-digit major + 4-digit minor. A requested id of exactly this shape
# cannot be truncated, so it is the only case where a page that omits the echo
# (layout change) may still be trusted.
_KING_PIN_DIGITS = 10

# Delay before each parcel-repair candidate lookup. Repair is rare and its
# requests are extra, so they are paced conservatively.
_REPAIR_PACE_S = 0.5


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _extract_parcel_echo(page_html: str) -> str | None:
    """Digits of the parcel the eRealProperty page says it resolved, or None.

    Scans EVERY parcel-labelled cell and returns the first that yields digits
    (Codex P3): the page carries both a "Parcel" and a "Parcel Number" label
    depending on the view, so a blank or "N/A" cell appearing first must not mask
    a real echo further down and must not be mistaken for "no echo on this page".
    """
    for m in _PARCEL_ECHO_RE.finditer(page_html):
        echoed = _digits(BridgeScraper.clean(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))))
        if echoed:
            return echoed
    return None


def _page_has_parcel_cell(page_html: str) -> bool:
    """True if the page carries a parcel-labelled cell at all (even a blank one).

    Distinguishes "this page has no such cell" (a layout change) from "the cell is
    present but says N/A" — the latter is a page that declined to name its parcel,
    which is not evidence that it is ours.
    """
    return _PARCEL_ECHO_RE.search(page_html) is not None


def parcel_page_is_for(page_html: str, requested_pid: str) -> bool:
    """True if this eRealProperty page is really about ``requested_pid``.

    MISMATCH -> False: we asked about parcel X and the county answered about
    parcel Y, so nothing on the page may be attributed to this lead.
    NO PARCEL CELL AT ALL -> trusted only when the requested id is already a
    well-formed 10-digit King PIN (the truncation class cannot apply to it); a
    malformed id with no echo fails CLOSED.
    PARCEL CELL PRESENT BUT UNREADABLE ("N/A", blank) -> always False (Codex P3):
    the page declined to name its parcel, which is not evidence that it is ours.
    """
    want = _digits(requested_pid)
    if not want:
        return False
    echoed = _extract_parcel_echo(page_html)
    if echoed is None:
        if _page_has_parcel_cell(page_html):
            return False
        return len(want) == _KING_PIN_DIGITS
    return echoed == want


class KingOwnerLookupBlockedError(RuntimeError):
    """Raised when eRealProperty appears to be throttling/blocking lookups."""


class _Phase1Ledger:
    """Outcome accounting for the phase-1 fetch loop.

    Three defects made the 2026-09-04/09-07 King incidents unreadable and worse
    than they had to be, and all three came from the accounting being scattered
    through the loop body rather than centralised here:

    1. THE BREAKER COULD NOT SEE AN EXCEPTION-ONLY OUTAGE. The threshold test sat
       inside the `try`, AFTER a successful `safe_get`. A DNS/TLS/timeout outage
       appended `True` from the `except` and then never evaluated the threshold,
       so fifty straight connection failures ran on to the time budget instead of
       tripping (Codex).
    2. ONE REQUEST COULD RECORD TWO OBSERVATIONS. An exception raised while
       PARSING a response that had already been recorded fell into the same
       `except` and appended a second observation for one request (Codex).
    3. WE KEPT ONLY `last status=`. The persisted reason named the status of the
       50th request and nothing about the other 49, and the per-parcel detail
       logged at DEBUG while production runs at INFO. The real incident is
       therefore unattributable after the fact: `last status=302` is all we have.

    So: one observation per request, recorded in exactly one place, with a
    histogram that survives into the persisted reason.
    """

    def __init__(self, window: int, min_failures: int) -> None:
        self._window: deque[bool] = deque(maxlen=max(1, window))
        self._min_failures = min_failures
        self.statuses: dict[str, int] = {}
        self.redirects: dict[str, int] = {}
        self.attempted = 0
        self.failed = 0

    def record(self, response, exc: BaseException | None) -> bool:
        """Record ONE request. Returns True if this request was a failure."""
        self.attempted += 1
        if exc is not None:
            key = type(exc).__name__
            failed = True
        elif response.status_code != 200:
            key = f"HTTP{response.status_code}"
            failed = True
            hint = _redirect_target(response)
            if hint:
                self.redirects[hint] = self.redirects.get(hint, 0) + 1
        else:
            key = "HTTP200"
            failed = False
        self.statuses[key] = self.statuses.get(key, 0) + 1
        self._window.append(failed)
        self.failed += int(failed)
        return failed

    def should_trip(self) -> bool:
        return (
            len(self._window) == self._window.maxlen
            and self._window.count(True) >= self._min_failures
        )

    def histogram(self) -> str:
        """Compact, ordered outcome breakdown for the persisted reason."""
        parts = [f"{k}x{v}" for k, v in sorted(self.statuses.items(), key=lambda kv: -kv[1])]
        if self.redirects:
            parts += [f"->{k}x{v}" for k, v in sorted(self.redirects.items(), key=lambda kv: -kv[1])]
        return " ".join(parts) or "no requests"

    @property
    def window_failures(self) -> int:
        return self._window.count(True)

    @property
    def window_size(self) -> int:
        return len(self._window)


def _redirect_target(response) -> str:
    """`host/path` a 3xx pointed at, or "". NEVER the query or fragment.

    Where a redirect points is the single most useful fact about it, and it is
    exactly what the last incident could not tell us. The query string is dropped
    because on this endpoint it carries the parcel number, and a Location can
    carry a session token.
    """
    if response is None or response.status_code not in (301, 302, 303, 307, 308):
        return ""
    location = response.headers.get("Location")
    if not location:
        return ""
    from urllib.parse import urljoin, urlparse

    parsed = urlparse(urljoin(response.url or "", location))  # resolve a relative Location
    target = f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
    return target[:120]


@dataclass(frozen=True)
class _OwnerLookupOutcome:
    resolved: bool
    transient: bool

    @property
    def unresolved(self) -> bool:
        return not self.resolved


def _extract_owner_name(page_html: str) -> str | None:
    """Owner/taxpayer name from an eRealProperty Dashboard page, or None."""
    m = _OWNER_RE.search(page_html)
    if not m:
        return None
    # Strip any nested tags, then decode HTML entities (&nbsp;, &amp;, &#160;…).
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    name = BridgeScraper.clean(html.unescape(text))
    if not name or re.sub(r"[^A-Z0-9]", "", name.upper()) in _OWNER_JUNK:
        return None
    return name


async def _fetch_king_owner(pid: str, *, max_attempts: int = 1) -> tuple[str | None, bool]:
    """Resolve one parcel's owner with bounded retry.

    Returns (owner_name_or_None, had_transient_error). A 200 response whose page
    has no owner cell is a GENUINE miss -> (None, False). A persistent non-200
    (429/5xx/4xx) or exception after max_attempts attempts is a TRANSIENT
    failure -> (None, True), so a caller can avoid treating it as "no such owner".
    """
    attempts = max(1, max_attempts)
    for attempt in range(attempts):
        try:
            # S4: safe_get re-validates the (fixed HTTPS) target for SSRF defense
            # in depth — same call the full enricher uses.
            r = safe_get(f"{_ERP_URL}{pid}", headers=_HEADERS, timeout=10)
            if r.status_code == 200:
                # The county may have silently resolved a DIFFERENT parcel (see
                # parcel_page_is_for). A wrong owner is worse than no owner — this
                # path repairs placeholder party_name — so treat it as a genuine
                # miss, not a transient error (retrying would return the same page).
                if not parcel_page_is_for(r.text, pid):
                    _logger.warning(
                        "King owner lookup: eRealProperty resolved a DIFFERENT parcel for "
                        "requested=%s (echoed=%s) — discarding", pid, _extract_parcel_echo(r.text),
                    )
                    return None, False
                return _extract_owner_name(r.text), False  # genuine result (name or miss)
        except Exception as exc:
            _logger.debug(
                "Owner fetch error parcel=%s attempt=%d: %s", pid, attempt + 1, str(exc)[:160]
            )
        if attempt < attempts - 1:
            await asyncio.sleep(0.5 * (2 ** attempt))  # exponential backoff
    return None, True


async def batch_extract_king_owners(parcel_ids: list[str], delay: float = 0.1, **kwargs):
    """Admission-controlled wrapper around the owner-only lookup.

    The lease used to guard only `batch_enrich_king_county`, leaving THIS path
    unguarded even though it hits the very same eRealProperty endpoint (Codex).
    Inline enrichment calls it for every King tax lead that already has a mailing
    address, so an owner pass could run alongside another job's enrichment, or
    alongside the recovery sweep, or alongside another owner pass. The health gate
    answers "is the source available", never "how many of us are on it right now".

    A caller that cannot get in returns what it has rather than queueing: the
    owner-only path is re-runnable by design (the rows still carry a placeholder),
    so deferring costs a later pass, not the data.
    """
    import time as _t

    from src.scrapers.enrichment.source_admission import SourceAdmission

    _t0 = _t.monotonic()
    with SourceAdmission(KING_EREALPROPERTY, max_wait_s=45.0) as admission:
        _waited = _t.monotonic() - _t0
        _budget = kwargs.get("time_budget_s")
        if _waited > 0.5 and _budget is not None:
            # NEVER floor this to a positive number: `max(5.0, budget - waited)`
            # turned a 1-second budget after a 46-second wait into 5 MORE seconds
            # of work, past the caller's own cancellation (Codex). If the wait
            # consumed the budget there is nothing left to spend.
            kwargs["time_budget_s"] = _budget - _waited
            if kwargs["time_budget_s"] <= 0:
                admission.admitted = False
        if not admission.admitted:
            _logger.info(
                "King owner lookup: another pass holds the source lease; skipping "
                "%d parcel(s) this run (re-runnable)", len(parcel_ids),
            )
            out = kwargs.get("out")
            return out if out is not None else {}
        return await _batch_extract_king_owners(
            parcel_ids, delay, _admission=admission, **kwargs)


async def _batch_extract_king_owners(
    parcel_ids: list[str],
    delay: float = 0.1,
    *,
    _admission=None,
    circuit_window: int = 50,
    max_transient_rate: float = 0.10,
    max_unresolved_rate: float = 0.50,
    fetch_attempts: int = 1,
    out: dict[str, str] | None = None,
    time_budget_s: float | None = None,
) -> dict[str, str]:
    """Owner/taxpayer name per parcel from eRealProperty — HTTP only, no Playwright.

    A lean, owner-ONLY companion to batch_enrich_king_county's Phase 1. The full
    enricher also fetches mailing addresses via Playwright (slow, ~5s/parcel);
    callers that only need to repair a placeholder party_name (the King
    tax-delinquent backfill, and the inline owner-only pass for rows that already
    have a mailing address) must not pay that cost. Same eRealProperty endpoint,
    same SSRF-guarded safe_get, same _extract_owner_name parser/junk-rejection —
    so a name produced here is identical to one produced by the full path.

    `delay` is the pause between requests (default 0.1s — fine for a normal
    ~300-parcel job). A bulk caller (the backfill, tens of thousands of parcels)
    should pass a larger value: eRealProperty rate-limits a sustained ~10 req/s
    stream, so too small a delay makes ~half the lookups fail transiently.

    Returns {parcel_id: owner_name} for parcels that yielded a real owner; misses
    are simply absent (never an empty/None value), so a caller can swap
    unconditionally on a present key. Transient failures (counted + logged at
    WARNING) are also absent — but the backfill is re-runnable, so a parcel that
    failed transiently this run is retried on the next run (it is still a
    placeholder), never permanently abandoned.
    """
    # `out`, when the caller supplies it, IS the result dict — names land in it as
    # they resolve, so a caller that wraps this in asyncio.wait_for still keeps
    # every owner already found when the timeout cancels the coroutine. Returning
    # only at the end meant a cancelled run threw away all of its work; that is
    # exactly how a real King job finished with zero owner names.
    # `time_budget_s` is the cooperative version of the same idea: stop cleanly
    # (keeping results) instead of being killed from outside.
    owners: dict[str, str] = out if out is not None else {}
    import time as _time
    _deadline = (_time.monotonic() + time_budget_s) if time_budget_s is not None else None
    # parcel_id comes from our own scraped DB rows (not user input), but require a
    # digit so a malformed value can't generate a noisy external request.
    clean = list(dict.fromkeys(
        pid.strip() for pid in parcel_ids
        if pid and len(pid.strip()) >= 6 and any(c.isdigit() for c in pid)
    ))
    if not clean:
        return owners

    # Shared cross-process gate. The per-run breaker below only stops THIS run;
    # this stops every worker/backfill while King is still refusing us. Raises
    # SourceUnavailableError, which callers degrade on (they must not retry).
    check_source_or_raise(KING_EREALPROPERTY)

    _logger.info("Owner-only lookup for %d parcels...", len(clean))
    failures = 0
    misses = 0
    window: deque[_OwnerLookupOutcome] = deque(maxlen=max(1, circuit_window))
    for i, pid in enumerate(clean):
        if _deadline is not None and _time.monotonic() >= _deadline:
            _logger.warning(
                "Owner-only lookup: time budget exhausted after %d/%d parcels "
                "(%d resolved so far, kept)", i, len(clean), len(owners),
            )
            break
        if i % 100 == 0 and i > 0:
            _logger.info("  owner HTTP: %d / %d ...", i, len(clean))
        if i and i % 50 == 0 and _admission is not None and not _admission.still_held():
            _logger.warning(
                "Owner-only lookup: lost the source lease after %d/%d parcels "
                "(%d resolved, kept)", i, len(clean), len(owners),
            )
            break
        owner, errored = await _fetch_king_owner(pid, max_attempts=fetch_attempts)
        if owner:
            owners[pid] = owner
        elif errored:
            failures += 1
        else:
            misses += 1
        window.append(_OwnerLookupOutcome(resolved=bool(owner), transient=errored))
        if len(window) == window.maxlen:
            transient_rate = sum(o.transient for o in window) / len(window)
            unresolved_rate = sum(o.unresolved for o in window) / len(window)
            if transient_rate > max_transient_rate or unresolved_rate > max_unresolved_rate:
                msg = (
                    "King owner lookup circuit breaker tripped: "
                    f"window={len(window)} transient_rate={transient_rate:.0%} "
                    f"unresolved_rate={unresolved_rate:.0%} resolved={len(owners)}/{i + 1} "
                    f"transient_failures={failures} genuine_misses={misses}. "
                    "Aborting to avoid treating a throttle/block as no-owner."
                )
                _logger.warning(msg)
                # Persist it: the breaker alone would let the next process start
                # hammering the same blocked source seconds later.
                record_source_blocked(KING_EREALPROPERTY, msg)
                raise KingOwnerLookupBlockedError(msg)
        await asyncio.sleep(delay)

    if failures:
        _logger.warning(
            "Owner-only lookup: %d/%d parcels failed after %d retries (transient — "
            "re-run to retry; not abandoned)", failures, len(clean), settings.MAX_RETRIES,
        )
    _logger.info("Owner-only lookup done: %d/%d parcels resolved", len(owners), len(clean))
    return owners


# The rendered tax bill ends the mailing block with its postal line, often glued to the
# next label ("PORTLAND OR 97210Pay by mail"). Shared with scripts that find the rows
# the old 2-line parser truncated, so "complete" means the same thing in both places.
# A real state code is required before a US ZIP so "PO BOX 12345" never ends a block early.
_US_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV "
    "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR GU VI AS MP AA AE AP"
).split()
MAILING_POSTAL_TAIL_RE = re.compile(
    rf"(?:\b(?:{'|'.join(_US_STATE_CODES)}),?\s+\d{{5}}(?:-\d{{4}})?|\b[A-Z]\d[A-Z] ?\d[A-Z]\d)\s*$",
    re.IGNORECASE)
_MAILING_BLOCK_END_RE = re.compile(r"Pay by|Annual statement|Billing Details")
_MAILING_BLOCK_MAX_LINES = 5


def parse_mailing_block(body: str) -> str | None:
    """The taxpayer mailing address from a rendered King tax-bill page, or None.

    Lines after "Mailing Address" up to and including the first one ending in a US ZIP
    or Canadian postal code, joined as "LINE1, LINE2, CITY ST ZIP". The old parser kept
    at most two lines and stopped only at a line STARTING with "Pay by", so a three-line
    address ("2250 NW FLANDERS ST / SUITE GARDEN 02 / PORTLAND OR 97210") was stored
    without its city, state and ZIP (2,777 King rows, 2026-09-13). No postal line within
    the first few lines means the block is not understood, so nothing is returned.
    """
    if "Mailing Address" not in body:
        return None
    after = body[body.index("Mailing Address") + len("Mailing Address"):]
    end = _MAILING_BLOCK_END_RE.search(after)
    if end:
        after = after[:end.start()]
    lines = [ln.strip() for ln in after.split("\n") if ln.strip()][:_MAILING_BLOCK_MAX_LINES]
    for i, line in enumerate(lines):
        if MAILING_POSTAL_TAIL_RE.search(line):
            return " ".join(", ".join(lines[:i + 1]).split())
    return None


def _read_parcel_page(page_html: str) -> tuple[str | None, str | None, str | None]:
    """(site address, tax-bill URL, owner) from one eRealProperty page.

    Shared by the ordinary path and the malformed-PID recovery path so a page is
    read exactly one way regardless of which PIN fetched it.
    """
    m = re.search(r"Site Address</td>\s*<td[^>]*>([^<]+)", page_html)
    prop = m.group(1).replace("&nbsp;", "").strip() if m else None
    m2 = re.search(r'href="(https://payment\.kingcounty\.gov[^"]+)"', page_html)
    tax_url = m2.group(1).replace("&amp;", "&") if m2 else None
    return (prop or None), tax_url, _extract_owner_name(page_html)


def _gis_exists(candidates: list[str]) -> set[str]:
    """Which candidate PINs King's strict parcel layer actually carries."""
    from src.scrapers.enrichment.county_gis import batch_enrich_parcels_gis

    return set(batch_enrich_parcels_gis(candidates, "king", "WA"))


def _owner_of(pin: str) -> str | None:
    """Assessor owner for one candidate, echo-verified (a page that names another
    parcel yields None, so a truncating answer can never break the tie).

    PACED. Parcel repair tries several candidate PINs, repeated per party, and
    every one of them is a real eRealProperty request that used to be issued with
    no delay at all — an unmetered second stream against the very source the main
    loop is carefully pacing (Codex). This is the cheapest correct bound; the
    requests stay outside the phase-1 ledger, which is recorded as a known gap.
    """
    import time as _t

    _t.sleep(_REPAIR_PACE_S)
    try:
        r = safe_get(f"{_ERP_URL}{pin}", headers=_HEADERS, timeout=10)
    except Exception:  # noqa: BLE001 — a lookup failure must only cost a repair
        return None
    if r.status_code != 200 or not parcel_page_is_for(r.text, pin):
        return None
    return _extract_owner_name(r.text)


def resolve_malformed_parcel(source_pid: str, party_name: str | None,
                             stats: dict | None = None):
    """Recover the real PIN behind a malformed recorder PID, or None.

    Thin binding of king_parcel_repair to the live county sources. The caller
    keeps ``parcel_id`` as the county printed it and uses the result only for
    lookups + provenance — see that module's docstring for why.
    """
    from src.scrapers.enrichment.king_parcel_repair import resolve_king_parcel

    return resolve_king_parcel(
        source_pid, party_name, gis_exists=_gis_exists, owner_of=_owner_of, stats=stats,
    )


async def batch_enrich_king_county(
    parcel_ids: list[str],
    **kwargs,
) -> dict[str, dict[str, str | None]]:
    """Admission-controlled wrapper around the real King enrichment pass.

    The pacing inside the pass bounds ONE pass and knows nothing about any other,
    so concurrent King jobs used to make independent request streams against one
    county server with nothing able to see the total. Two 17,157-parcel jobs
    overlapped for about half an hour on 2026-09-04 and the first circuit-breaker
    trip landed that morning.

    A shared lease admits one pass at a time, so concurrent jobs SERIALISE against
    the county rather than multiplying against it. A caller that cannot get in
    within the wait window defers its parcels instead of queueing until its
    enrichment budget is gone: the background recovery sweep will collect them,
    which is a far better outcome than a job that blocks and then does nothing.

    A thin wrapper rather than a `with` block inside the pass so the lease is
    released on EVERY exit path, including the circuit breaker raising.
    """
    import time as _t

    from src.scrapers.enrichment.source_admission import SourceAdmission

    _t0 = _t.monotonic()
    with SourceAdmission(KING_EREALPROPERTY, max_wait_s=45.0) as admission:
        # The wait for admission spends the CALLER'S wall clock. The inner budget
        # was computed before this call, and the caller's own kill-switch timer is
        # already running, so leaving it unadjusted meant a pass admitted after 40s
        # would plan work past the moment it gets cancelled — and because results
        # are returned only at the end, every lookup it had already paid for would
        # be thrown away (Codex). Charge the wait to the budget instead.
        _waited = _t.monotonic() - _t0
        _budget = kwargs.get("time_budget_s")
        if _waited > 0.5 and _budget is not None:
            # NEVER floor this to a positive number: `max(5.0, budget - waited)`
            # turned a 1-second budget after a 46-second wait into 5 MORE seconds
            # of work, past the caller's own cancellation (Codex). If the wait
            # consumed the budget there is nothing left to spend.
            kwargs["time_budget_s"] = _budget - _waited
            if kwargs["time_budget_s"] <= 0:
                admission.admitted = False
        if not admission.admitted:
            st = kwargs.get("stats")
            owned = list(kwargs.get("tax_urls_in") or parcel_ids or [])
            if st is not None:
                st.update({"requested": len(owned), "property_found": 0,
                           "mailing_candidates": 0, "mailing_attempted": 0,
                           "mailing_found": 0, "deferred": owned, "unreached": owned,
                           "budget_exhausted": True, "parcel_mismatch": 0,
                           "parcel_recovered": 0,
                           "phase1_outcomes": "not admitted (source busy)"})
            _logger.info(
                "King enrichment: another pass holds the source lease; deferring "
                "%d parcel(s) to the background recovery sweep", len(owned),
            )
            return {}
        return await _batch_enrich_king_county(parcel_ids, _admission=admission, **kwargs)


async def _batch_enrich_king_county(
    parcel_ids: list[str],
    *,
    _admission=None,
    time_budget_s: float | None = None,
    stats: dict | None = None,
    party_names: dict[str, list[str]] | None = None,
    pace_s: float = 0.2,
    do_mailing: bool = True,
    tax_urls_out: dict[str, str] | None = None,
    tax_urls_in: dict[str, str] | None = None,
    results_seed: dict[str, dict] | None = None,
) -> dict[str, dict[str, str | None]]:
    """Two-phase enrichment: HTTP for property, Playwright for mailing.

    ``time_budget_s`` (2026-09-02): a monotonic deadline checked BEFORE every
    lookup (each HTTP fetch and each Playwright navigation). On exhaustion the
    remaining parcels are skipped and the PARTIAL results are returned — never
    cancelled from outside and lost. Evidence: every King tax_delinquent job with
    a large mailing pass (172 / 7,542 / 8,626 parcels) died in the caller's
    ``asyncio.wait_for(240)`` and lost everything incl. skip-trace enqueue, while
    jobs with <= 42 parcels succeeded. ``stats`` (optional dict) is filled with
    requested / property_found / mailing_candidates / mailing_attempted /
    mailing_found / deferred (parcel ids never attempted) / budget_exhausted.
    ``pace_s`` is the delay between Playwright page loads (0.2 s for a job; a
    one-off backfill passes several seconds — King has IP-rate-blocked us).
    """
    import time as _time

    results: dict[str, dict[str, str | None]] = {}
    clean = list(dict.fromkeys(pid.strip() for pid in parcel_ids if pid and len(pid.strip()) >= 6))
    st = stats if stats is not None else {}
    st.update({"requested": len(clean), "property_found": 0, "mailing_candidates": 0,
               "mailing_attempted": 0, "mailing_found": 0, "deferred": [],
               # `deferred` is the DURABLE MARKER set: every parcel that still has
               # no mailing address, whether we tried it or not. `unreached` is the
               # strict subset we never issued a request for. A retrying caller
               # needs the difference: charging a retry attempt to a parcel that
               # was never tried would burn its ceiling on work that never
               # happened, and NOT charging one that was tried and failed lets a
               # permanently unanswerable parcel retry forever (Codex).
               "unreached": [],
               # POSITIVE evidence that we issued a request for a parcel.
               # NAMED `requested_pids`, not `attempted`: king_parcel_repair is
               # handed THIS SAME dict and bumps an int counter it calls
               # "attempted", so reusing that name made the repair path raise
               # `can only concatenate list (not "int") to list` and swallowed the
               # whole parcel. Caught by an existing test. A
               # retrying caller must charge an attempt on evidence that the work
               # HAPPENED, not on the absence of it from `unreached`: an exception
               # anywhere (a mid-run health raise, a browser that fails to start,
               # a cancellation) leaves `unreached` empty and every selected parcel
               # then looks attempted. `stats` is caller-owned and mutated in
               # place, so whatever lands here survives even a cancelled coroutine
               # (Codex).
               "requested_pids": [],
               "budget_exhausted": False, "parcel_mismatch": 0, "parcel_recovered": 0})
    deadline = (_time.monotonic() + time_budget_s) if time_budget_s is not None else None

    def _over_budget() -> bool:
        return deadline is not None and _time.monotonic() >= deadline

    # Mailing-only pass (see PHASE SELECTION below). Must come BEFORE the
    # empty-`clean` early return: this mode is driven by tax_urls_in and is
    # legitimately called with an EMPTY parcel_ids list.
    if tax_urls_in is not None:
        st["requested"] = len(tax_urls_in)
        check_source_or_raise(KING_EREALPROPERTY)
        # Seeded with phase 1's own rows for these parcels. Phase 2 is not
        # standalone-pure: it validates the rendered tax page against
        # `resolved_parcel_id` (a RECOVERED parcel's page names the resolved PIN,
        # not the malformed one we key by), so starting from an empty dict would
        # make every recovered parcel fail that check and silently drop its
        # mailing address. Copied per pid so the caller's dict is never mutated.
        _seeded = {pid: dict((results_seed or {}).get(pid) or {}) for pid in tax_urls_in}
        return await _king_mailing_phase(
            _seeded, dict(tax_urls_in), st, _over_budget, pace_s, _admission=_admission)

    if not clean:
        return results

    # PHASE SELECTION (2026-09-04). The two phases have wildly different costs:
    # phase 1 is one cheap HTTP GET per parcel (~0.5s) and yields property +
    # OWNER; phase 2 drives Playwright against the tax-bill page (~5-10s) for the
    # mailing address. A caller that slices its parcel list into chunks and calls
    # this function per chunk silently INVERTS their priority — chunk 1's phase 2
    # eats the whole shared budget and later chunks never get phase 1 at all.
    # (Observed in prod: a 17,157-parcel job reached 173 parcels, not ~1,200.)
    # `do_mailing=False` + `tax_urls_out` let such a caller run phase 1 across
    # EVERY parcel first and collect the tax-bill URLs; `tax_urls_in` then drives
    # phase 2 alone with whatever budget is left. Defaults preserve the original
    # single-call behaviour exactly.
    # ── Phase 1: HTTP requests for property address + tax URLs (fast) ─────
    # Same shared gate as the owner-only path — this one also hits eRealProperty.
    check_source_or_raise(KING_EREALPROPERTY)

    _logger.info("Phase 1: HTTP lookup for %d parcels...", len(clean))
    tax_urls: dict[str, str] = {}  # pid → payment.kingcounty.gov URL

    # Per-run circuit breaker for phase 1. The one-shot check_source_or_raise gate
    # above only asks "was King refusing us BEFORE this run started"; it cannot
    # notice the source starting to refuse us DURING a run. That was tolerable
    # while the parcel list was hard-capped, but this loop is now bounded by time
    # rather than count, so an unnoticed block could mean a long run of failed
    # fetches recorded as "this parcel simply has no data" — the exact shape of the
    # eRealProperty IP rate-block incident. Mirrors the owner-only path's breaker:
    # a sustained failure rate aborts the run AND is persisted, so the next worker
    # does not immediately start hammering a source that is still refusing us.
    _p1 = _Phase1Ledger(_PHASE1_BREAKER_WINDOW, _PHASE1_BREAKER_MIN_FAILURES)
    _p1_pace = 0.1 if pace_s <= 0.2 else pace_s

    for i, pid in enumerate(clean):
        if _over_budget():
            _logger.warning("King phase 1: time budget exhausted after %d/%d parcels", i, len(clean))
            st["budget_exhausted"] = True
            st["deferred"].extend(clean[i:])
            st["unreached"].extend(clean[i:])
            break
        if i % 100 == 0 and i > 0:
            _logger.info("  HTTP: %d / %d ...", i, len(clean))
        # Renew the shared lease as we go and STOP if we no longer hold it: a
        # fixed TTL with no renewal lets a long pass outlive its lease while a
        # second worker starts on the same source (Codex).
        if i and i % 50 == 0 and _admission is not None and not _admission.still_held():
            st["budget_exhausted"] = True
            st["deferred"].extend(clean[i:])
            st["unreached"].extend(clean[i:])
            break

        # EVERY path through this body pays the pace, including a failed fetch.
        # It used to `continue` straight past the sleep at the bottom, so the
        # instant King started refusing us the loop stopped pacing altogether: a
        # 302 comes back in ~50 ms where a real page takes ~290 ms, so we sped UP
        # by roughly 6x exactly when the source was asking us to slow down. That
        # is a positive feedback loop, and it is part of why a blip became a
        # 50/50 wipeout rather than a handful of retries (Codex).
        _tripped = False
        try:
            r = None
            exc: BaseException | None = None
            try:
                # S4: safe_http (SSRF defense-in-depth). Fixed HTTPS eRealProperty
                # endpoint, but safe_get re-validates (resolve=True), disables
                # ambient proxy, and refuses redirect-to-internal. Same Response API.
                # allow_redirects stays FALSE: parcel_page_is_for() trusts a page
                # with no parcel cell when the requested id is a well-formed 10-digit
                # King PIN, so following a 302 to a block page would hand us a 200
                # we would then record as "this parcel has no data" — precisely what
                # this breaker exists to prevent. We record where it pointed instead.
                r = safe_get(f"{_ERP_URL}{pid}", headers=_HEADERS, timeout=10)
            except Exception as fetch_exc:  # noqa: BLE001
                exc = fetch_exc
                _logger.debug(
                    "Property URL fetch failed for parcel=%s: %s", pid, str(fetch_exc)[:200]
                )
            # ONE observation per request, recorded before anything can raise.
            st["requested_pids"].append(pid)
            failed = _p1.record(r, exc)

            if _p1.should_trip():
                msg = (
                    "King phase-1 circuit breaker tripped: "
                    f"{_p1.window_failures}/{_p1.window_size} recent eRealProperty "
                    f"fetches failed after {i} of {len(clean)} parcels. "
                    f"Outcomes this run: {_p1.histogram()}. "
                    "Aborting so a block is never recorded as 'this parcel has no data'."
                )
                _logger.warning(msg)
                record_source_blocked(KING_EREALPROPERTY, msg)
                st["budget_exhausted"] = True
                # clean[i:] covers this parcel and everything after it. The parcels
                # BEFORE it that already failed were deferred as they failed (below),
                # so the marker now covers every unresolved parcel rather than only
                # the tail — the old shape left the 49 failures with no durable
                # marker at all, which is why no later sweep could ever find them.
                st["deferred"].extend(clean[i:])
                # clean[i] was attempted (its failure is what tripped the breaker);
                # only the tail after it was never reached.
                st["unreached"].extend(clean[i + 1:])
                _tripped = True
                break  # runs the `finally` below, which skips the pace and exits
            if failed:
                # A failed lookup is UNKNOWN, not "no data". Mark it deferred so the
                # recovery sweep can come back to it.
                st["deferred"].append(pid)
                continue

            # The county may have silently truncated our id and served ANOTHER
            # parcel's page (see parcel_page_is_for). Everything below — site
            # address, tax-bill URL, owner — would then belong to a different
            # property, so discard the whole page rather than attach any of it.
            # A lead with no address is honest; a lead with someone else's address
            # is a wrong mailing AND a paid skip-trace on a stranger's house.
            if not parcel_page_is_for(r.text, pid):
                st["parcel_mismatch"] += 1
                _logger.warning(
                    "King enrichment: eRealProperty resolved a DIFFERENT parcel for "
                    "requested=%s (echoed=%s) — discarding page",
                    pid, _extract_parcel_echo(r.text),
                )
                # The county's own PID is malformed. Try to recover the REAL
                # parcel under strict guards (king_parcel_repair) and, if it
                # resolves, redo THIS lookup against the recovered PIN. The
                # stored parcel_id still stays exactly as the county printed it —
                # only the lookup target and the provenance change.
                # Try EVERY distinct party on this PID, not just the first: two
                # leads can share one malformed PID, and picking one party meant a
                # lead whose party did not resolve silently lost its recovery
                # (Codex P1). The first party that resolves wins; the write-back
                # then applies it only to rows naming that same person.
                resolved = None
                for _party in ((party_names or {}).get(pid) or [None]):
                    resolved = resolve_malformed_parcel(pid, _party, st)
                    if resolved is not None:
                        break
                if resolved is not None:
                    # A second real eRealProperty request. It must be paced and
                    # counted like any other, or the repair path becomes an
                    # unmetered second stream against a source we are rate-limiting.
                    await asyncio.sleep(_p1_pace)
                    st["requested_pids"].append(pid)
                    try:
                        rr = safe_get(
                            f"{_ERP_URL}{resolved.parcel_id}", headers=_HEADERS, timeout=10
                        )
                        _p1.record(rr, None)
                    except Exception as _rexc:  # noqa: BLE001
                        # A repair fetch that RAISES is still a request King saw,
                        # and it used to skip the ledger entirely: two requests
                        # could leave a ledger reading HTTP200x1, so a repair-heavy
                        # run could hide a developing outage from the breaker
                        # (Codex).
                        _p1.record(None, _rexc)
                        rr = None
                    # Evaluate the breaker on this observation too, or a run whose
                    # failures are concentrated in the repair path never trips.
                    if _p1.should_trip():
                        msg = (
                            "King phase-1 circuit breaker tripped during parcel repair: "
                            f"{_p1.window_failures}/{_p1.window_size} recent eRealProperty "
                            f"fetches failed after {i} of {len(clean)} parcels. "
                            f"Outcomes this run: {_p1.histogram()}."
                        )
                        _logger.warning(msg)
                        record_source_blocked(KING_EREALPROPERTY, msg)
                        st["budget_exhausted"] = True
                        st["deferred"].extend(clean[i:])
                        st["unreached"].extend(clean[i + 1:])
                        _tripped = True
                        break
                    if rr is not None and rr.status_code == 200 and parcel_page_is_for(rr.text, resolved.parcel_id):
                        _logger.info(
                            "King enrichment: recovered %s -> %s via %s",
                            pid, resolved.parcel_id, resolved.method,
                        )
                        prop, tax_url, owner = _read_parcel_page(rr.text)
                        results[pid] = {
                            "property_address": prop,
                            "mailing_address": None,
                            "owner_name": owner,
                            "parcel_lookup": "recovered",
                            **resolved.provenance(pid),
                        }
                        if tax_url:
                            # Keyed by the SOURCE pid so phase 2 still writes the
                            # mailing address back onto the right lead.
                            tax_urls[pid] = tax_url
                        st["parcel_recovered"] += 1
                        continue  # the `finally` below still paces this iteration
                results[pid] = {
                    "property_address": None,
                    "mailing_address": None,
                    "owner_name": None,
                    "parcel_lookup": "mismatch",
                }
                continue

            # Site address + tax-bill URL + owner/taxpayer name, all from the
            # page already fetched (no extra request). The owner fills the
            # placeholder party_name on King tax-delinquent leads downstream.
            prop, tax_url, owner = _read_parcel_page(r.text)

            if prop or tax_url or owner:
                results[pid] = {
                    "property_address": prop,
                    "mailing_address": None,
                    "owner_name": owner,
                    # Provenance (Codex): "verified" = the page echoed the parcel we
                    # asked for; "echo_absent" = the page carried no Parcel Number
                    # cell but our id was a well-formed 10-digit King PIN, so the
                    # truncation class could not apply.
                    "parcel_lookup": (
                        "verified" if _extract_parcel_echo(r.text) else "echo_absent"
                    ),
                }
                if tax_url:
                    tax_urls[pid] = tax_url

        except Exception as exc:
            # Reached only when PARSING or the malformed-PID recovery raises, never
            # for the fetch itself (that is handled above). Deliberately does NOT
            # touch the ledger: the request was already recorded, and appending here
            # is what let one request count twice (Codex). The parcel is unresolved,
            # so it is deferred like any other unknown.
            st["deferred"].append(pid)
            _logger.warning(
                "King phase 1: parcel=%s failed after a successful fetch: %s: %s",
                pid, type(exc).__name__, str(exc)[:200],
            )
        finally:
            # job: 0.1 s; backfill: slow. In a `finally` so `continue` cannot skip it.
            if not _tripped:
                await asyncio.sleep(_p1_pace)

    # FINALIZE across every requested parcel. Two paths used to return a parcel
    # with no tax URL and no marker: an unrepaired mismatch, and a 200 that parsed
    # nothing. Phase 2 only ever looks at `tax_urls`, so such a parcel had neither
    # a mailing address nor anything a later sweep could find, and the job could
    # still announce enrichment complete (Codex). Anything we asked about that did
    # not produce a tax-bill URL is unresolved, full stop.
    _resolved = set(tax_urls)
    _already = set(st["deferred"])
    for _pid in clean:
        if _pid not in _resolved and _pid not in _already:
            st["deferred"].append(_pid)

    st["property_found"] = sum(1 for r in results.values() if r.get("property_address"))
    st["phase1_outcomes"] = _p1.histogram()
    _logger.info(
        "Phase 1 done: %d/%d property addresses, %d tax URLs, %d parcel mismatches "
        "(%d recovered); %d/%d fetches failed [%s]",
        st["property_found"], len(clean), len(tax_urls), st["parcel_mismatch"],
        st["parcel_recovered"], _p1.failed, _p1.attempted, _p1.histogram(),
    )

    if tax_urls_out is not None:
        tax_urls_out.update(tax_urls)
    if not do_mailing:
        # Phase-1-only pass: the caller drives mailing itself, afterwards, with
        # the budget that is actually left over.
        st["mailing_candidates"] = len(tax_urls)
        return results

    if _admission is not None and not getattr(_admission, "admitted", True):
        # The lease was lost during phase 1. Its branch set budget_exhausted, but
        # `_over_budget()` only reads the DEADLINE, so phase 2 used to run anyway
        # and issue its mailing navigations with no lease at all (Codex).
        _logger.warning("King phase 2: skipped, the source lease was lost in phase 1")
        st["deferred"].extend(tax_urls)
        st.setdefault("unreached", []).extend(tax_urls)
        return results
    return await _king_mailing_phase(
        results, tax_urls, st, _over_budget, pace_s, _admission=_admission)


async def _king_mailing_phase(results, tax_urls, st, _over_budget, pace_s,
                              _admission=None):
    """Phase 2 — Playwright mailing lookups for parcels with a tax-bill URL.

    Split out of batch_enrich_king_county so a chunking caller can run it AFTER
    phase 1 has covered every parcel, instead of once per chunk (see PHASE
    SELECTION above).
    """
    st["mailing_candidates"] = len(tax_urls)
    if not tax_urls:
        return results

    # Cap at 200 parcels to avoid job timeout (~5-10s per lookup). The mailing
    # pass really is expensive (Playwright), so unlike phase 1 this cap stays.
    # What must NOT stay is dropping the overflow SILENTLY: the truncated parcels
    # used to vanish without entering `deferred`, so they got no durable marker
    # and no later sweep could find them — a gap invisible to both the logs and
    # the caller. Record them like any other unreached parcel.
    _MAX_MAILING_LOOKUPS = 200
    pids_to_lookup = list(tax_urls.keys())
    if len(pids_to_lookup) > _MAX_MAILING_LOOKUPS:
        _logger.info("Capping mailing lookups: %d → %d (to avoid timeout)", len(pids_to_lookup), _MAX_MAILING_LOOKUPS)
        st["deferred"].extend(pids_to_lookup[_MAX_MAILING_LOOKUPS:])
        st.setdefault("unreached", []).extend(pids_to_lookup[_MAX_MAILING_LOOKUPS:])
        pids_to_lookup = pids_to_lookup[:_MAX_MAILING_LOOKUPS]

    _logger.info("Phase 2: Playwright lookup for %d mailing addresses...", len(pids_to_lookup))
    # Provenance for the mailing lookup (2026-09-02): callers must be able to tell
    # "the tax-bill page was read and shows no mailing address" (a real source
    # outcome) from "the lookup never happened / failed" (unknown). An earlier
    # situs-copy fallback masked exactly this gap for every King lead.
    for _pid in results:
        results[_pid]["mailing_lookup"] = "not_attempted"

    # Never even launch the browser once the budget is gone (Codex): phase 1 may
    # have used it all, and a Playwright start-up would eat the caller's kill-switch.
    if _over_budget():
        _logger.warning("King phase 2: budget exhausted before mailing lookups; %d deferred", len(pids_to_lookup))
        st["budget_exhausted"] = True
        st["deferred"].extend(pids_to_lookup)
        st.setdefault("unreached", []).extend(pids_to_lookup)
        pids_to_lookup = []
    if pids_to_lookup:
        async with BridgeScraper() as scraper:

            for i, pid in enumerate(pids_to_lookup):
                if _over_budget():
                    # Checked before EVERY navigation so one slow page can't burn the
                    # caller's outer kill-switch timeout (Codex).
                    _logger.warning("King phase 2: time budget exhausted after %d/%d mailing lookups",
                                    i, len(pids_to_lookup))
                    st["budget_exhausted"] = True
                    st["deferred"].extend(pids_to_lookup[i:])
                    st.setdefault("unreached", []).extend(pids_to_lookup[i:])
                    break
                if i % 25 == 0:
                    _logger.info("  Mailing: %d / %d ...", i, len(pids_to_lookup))
                # Mailing is the SLOW phase (5-10 s per parcel), so an unrenewed
                # 200-parcel pass can outlive the lease on its own and overlap
                # another worker. Renew here too, and stop the moment it is gone.
                if (i and i % 10 == 0 and _admission is not None
                        and not _admission.still_held()):
                    _logger.warning(
                        "King phase 2: lost the source lease after %d/%d lookups",
                        i, len(pids_to_lookup),
                    )
                    st["budget_exhausted"] = True
                    st["deferred"].extend(pids_to_lookup[i:])
                    st.setdefault("unreached", []).extend(pids_to_lookup[i:])
                    break
                st["mailing_attempted"] += 1
                st.setdefault("requested_pids", []).append(pid)
                # A MAILING attempt specifically. The recovery sweep charges its
                # retry ceiling off this list, not off all-phase `attempted`: a
                # parcel whose phase 1 succeeded but which phase 2 never reached
                # would otherwise spend a mailing retry without a mailing request
                # ever being made, and five such ticks would terminalise it
                # unlooked-at (Codex).
                st.setdefault("mailing_attempted_pids", []).append(pid)
                # setdefault, not results[pid]: in mailing-only mode a pid may have
                # no phase-1 row at all, and a KeyError here is swallowed upstream
                # as a whole-chunk failure (so phase 2 would appear to do nothing).
                results.setdefault(pid, {})["mailing_lookup"] = "error"

                try:
                    url = tax_urls[pid]
                    # safe_goto (not raw page.goto): fail-CLOSED pre-flight SSRF
                    # validation + landing-URL re-check after redirects.
                    await scraper.safe_goto(
                        url, wait_until="domcontentloaded", timeout_ms=8_000
                    )

                    # Did the page actually SETTLE? The 4s wait can time out and its
                    # exception was swallowed, so a page that had rendered the parcel
                    # number but not yet the mailing section satisfied "our parcel,
                    # no Mailing Address block" and became a TERMINAL "none" — the
                    # sweep then cleared the marker after that single attempt. That
                    # is permanent silent loss, the exact class of defect this whole
                    # change exists to remove (Codex).
                    _rendered = False
                    try:
                        await scraper.page.wait_for_function(
                            "() => document.body.innerText.includes('Mailing Address') || document.body.innerText.includes('No accounts')",
                            timeout=4_000,
                        )
                        _rendered = True
                    except Exception:
                        _logger.debug("King mailing: parcel=%s never settled in 4s", pid)

                    body = await scraper.page.inner_text("body")
                    # "none" ONLY when the rendered page is provably this parcel's
                    # tax-bill page (its number is on the page, or the explicit
                    # "No accounts" answer) and the Mailing Address block is absent —
                    # partial renders / wrong pages stay "error" (Codex P1).
                    # Validate against the pid whose tax URL this is: for a
                    # RECOVERED parcel the page names the resolved PIN, not the
                    # malformed one we key results by (Codex P2).
                    _probe = (results.get(pid, {}).get("resolved_parcel_id") or pid)
                    _page_is_ours = _probe.replace("-", "") in body.replace("-", "")
                    # "none" needs AFFIRMATIVE evidence that there is no mailing
                    # address: either the county's explicit "No accounts", or this
                    # parcel's own page with no Mailing Address block at all. A
                    # block that IS present but yields no address lines (a partial
                    # render) is UNKNOWN, not empty — treating it as a real answer
                    # let the sweep clear the marker permanently on the first
                    # attempt (Codex).
                    # `_rendered` is load-bearing: the absence of a section on a page
                    # we never saw finish is not evidence that the section is empty.
                    if "No accounts" in body or (
                        _rendered and _page_is_ours and "Mailing Address" not in body
                    ):
                        results[pid]["mailing_lookup"] = "none"
                    # IDENTITY GATE (Codex). The extraction below used to be
                    # independent of the check above: any rendered page carrying a
                    # "Mailing Address" block wrote its address onto THIS pid, even
                    # when the page never named this parcel. A stale tab, a wrong tax
                    # URL, or a redirect to another account would then attach a
                    # stranger's mailing address to this lead — the same class of
                    # defect as the eRealProperty truncation, and one that a paid
                    # skip trace would then bill against. A page that does not name
                    # our parcel is not evidence about our parcel, so it stays
                    # "error" (unknown) rather than becoming a wrong "found".
                    if "Mailing Address" in body and not _page_is_ours:
                        results[pid]["mailing_lookup"] = "identity_unverified"
                        _logger.warning(
                            "King mailing: rendered tax page never named parcel %s — "
                            "discarding its Mailing Address block", _probe,
                        )
                    elif "Mailing Address" in body:
                        # A block that does not end in a postal code is a partial or
                        # unexpected render: it stays "error" (unknown, retried),
                        # never a truncated "found".
                        mailing = parse_mailing_block(body)
                        if mailing:
                            results[pid]["mailing_address"] = mailing
                            results[pid]["mailing_lookup"] = "found"

                except Exception as exc:  # noqa: BLE001 -- best-effort per parcel
                    _logger.debug(
                        "King mailing: parcel=%s lookup failed: %s: %s",
                        pid, type(exc).__name__, str(exc)[:160],
                    )

                # An attempted parcel whose outcome is UNKNOWN ("error", or a page
                # that never named it) still has no mailing address, so it needs the
                # durable marker like any other unresolved parcel. Without this a
                # navigation timeout left the parcel with neither an address nor a
                # marker, so no later sweep could find it and the job could still
                # report enrichment complete (Codex). Only "found" and "none" are
                # real answers; everything else is unknown.
                if results.get(pid, {}).get("mailing_lookup") not in ("found", "none"):
                    st["deferred"].append(pid)

                await asyncio.sleep(pace_s)


    found_mail = sum(1 for r in results.values() if r.get("mailing_address"))
    found_prop = sum(1 for r in results.values() if r.get("property_address"))
    st["mailing_found"] = found_mail
    # Parcels beyond the per-call mailing cap were never attempted either.
    _never = [p for p in tax_urls if p not in pids_to_lookup]
    st["deferred"].extend(_never)
    st.setdefault("unreached", []).extend(_never)
    # `requested` (not the enclosing scope's parcel list — this phase is also
    # reachable standalone via tax_urls_in, where no such list exists).
    _n = st.get("requested") or len(tax_urls)
    _logger.info("Enrichment done: %d/%d property, %d/%d mailing",
                 found_prop, _n, found_mail, _n)
    return results
