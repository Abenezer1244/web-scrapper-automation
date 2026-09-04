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


async def batch_extract_king_owners(
    parcel_ids: list[str],
    delay: float = 0.1,
    *,
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
    parcel yields None, so a truncating answer can never break the tie)."""
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
    *,
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
        return await _king_mailing_phase(_seeded, dict(tax_urls_in), st, _over_budget, pace_s)

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
    _p1_window: deque[bool] = deque(maxlen=_PHASE1_BREAKER_WINDOW)  # True = failed

    for i, pid in enumerate(clean):
        if _over_budget():
            _logger.warning("King phase 1: time budget exhausted after %d/%d parcels", i, len(clean))
            st["budget_exhausted"] = True
            st["deferred"].extend(clean[i:])
            break
        if i % 100 == 0 and i > 0:
            _logger.info("  HTTP: %d / %d ...", i, len(clean))

        try:
            # S4: safe_http (SSRF defense-in-depth). Fixed HTTPS eRealProperty
            # endpoint, but safe_get re-validates (resolve=True), disables
            # ambient proxy, and refuses redirect-to-internal. Same Response API.
            r = safe_get(
                f"{_ERP_URL}{pid}", headers=_HEADERS, timeout=10
            )
            _p1_window.append(r.status_code != 200)
            if (len(_p1_window) == _PHASE1_BREAKER_WINDOW
                    and _p1_window.count(True) >= _PHASE1_BREAKER_MIN_FAILURES):
                msg = (
                    "King phase-1 circuit breaker tripped: "
                    f"{_p1_window.count(True)}/{len(_p1_window)} recent eRealProperty "
                    f"fetches failed (last status={r.status_code}) after {i} of "
                    f"{len(clean)} parcels. Aborting so a block is never recorded as "
                    "'this parcel has no data'."
                )
                _logger.warning(msg)
                record_source_blocked(KING_EREALPROPERTY, msg)
                st["budget_exhausted"] = True
                st["deferred"].extend(clean[i:])
                break
            if r.status_code != 200:
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
                    rr = safe_get(f"{_ERP_URL}{resolved.parcel_id}", headers=_HEADERS, timeout=10)
                    if rr.status_code == 200 and parcel_page_is_for(rr.text, resolved.parcel_id):
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
                        await asyncio.sleep(0.1 if pace_s <= 0.2 else pace_s)
                        continue
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
            # A hard block / DNS or TLS failure RAISES rather than returning a
            # non-200, so the breaker must see these too — otherwise a total
            # outage looks like a run of parcels that merely "had no data".
            _p1_window.append(True)
            _logger.debug(
                "Property URL fetch failed for parcel=%s: %s",
                pid, str(exc)[:200],
            )

        await asyncio.sleep(0.1 if pace_s <= 0.2 else pace_s)  # job: 0.1 s; backfill: slow

    st["property_found"] = sum(1 for r in results.values() if r.get("property_address"))
    _logger.info("Phase 1 done: %d/%d property addresses, %d tax URLs, %d parcel mismatches "
                 "(%d recovered)", st["property_found"], len(clean), len(tax_urls),
                 st["parcel_mismatch"], st["parcel_recovered"])

    if tax_urls_out is not None:
        tax_urls_out.update(tax_urls)
    if not do_mailing:
        # Phase-1-only pass: the caller drives mailing itself, afterwards, with
        # the budget that is actually left over.
        st["mailing_candidates"] = len(tax_urls)
        return results

    return await _king_mailing_phase(results, tax_urls, st, _over_budget, pace_s)


async def _king_mailing_phase(results, tax_urls, st, _over_budget, pace_s):
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
                    break
                if i % 25 == 0:
                    _logger.info("  Mailing: %d / %d ...", i, len(pids_to_lookup))
                st["mailing_attempted"] += 1
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

                    try:
                        await scraper.page.wait_for_function(
                            "() => document.body.innerText.includes('Mailing Address') || document.body.innerText.includes('No accounts')",
                            timeout=4_000,
                        )
                    except Exception:
                        pass

                    body = await scraper.page.inner_text("body")
                    # "none" ONLY when the rendered page is provably this parcel's
                    # tax-bill page (its number is on the page, or the explicit
                    # "No accounts" answer) and the Mailing Address block is absent —
                    # partial renders / wrong pages stay "error" (Codex P1).
                    # Validate against the pid whose tax URL this is: for a
                    # RECOVERED parcel the page names the resolved PIN, not the
                    # malformed one we key results by (Codex P2).
                    _probe = (results.get(pid, {}).get("resolved_parcel_id") or pid)
                    if "No accounts" in body or _probe.replace("-", "") in body.replace("-", ""):
                        results[pid]["mailing_lookup"] = "none"
                    if "Mailing Address" in body:
                        idx = body.index("Mailing Address") + len("Mailing Address")
                        after = body[idx:idx + 200]
                        lines = [ln.strip() for ln in after.split("\n") if ln.strip()]
                        addr_lines = []
                        for line in lines:
                            if line.startswith("Pay by") or line.startswith("Annual") or line.startswith("Billing"):
                                break
                            if len(line) > 3:
                                addr_lines.append(line)
                            if len(addr_lines) >= 2:
                                break
                        if addr_lines:
                            mailing = " ".join(", ".join(addr_lines).strip().split())
                            results[pid]["mailing_address"] = mailing
                            results[pid]["mailing_lookup"] = "found"

                except Exception:
                    pass

                await asyncio.sleep(pace_s)


    found_mail = sum(1 for r in results.values() if r.get("mailing_address"))
    found_prop = sum(1 for r in results.values() if r.get("property_address"))
    st["mailing_found"] = found_mail
    # Parcels beyond the per-call mailing cap were never attempted either.
    st["deferred"].extend(p for p in tax_urls if p not in pids_to_lookup)
    # `requested` (not the enclosing scope's parcel list — this phase is also
    # reachable standalone via tax_urls_in, where no such list exists).
    _n = st.get("requested") or len(tax_urls)
    _logger.info("Enrichment done: %d/%d property, %d/%d mailing",
                 found_prop, _n, found_mail, _n)
    return results
