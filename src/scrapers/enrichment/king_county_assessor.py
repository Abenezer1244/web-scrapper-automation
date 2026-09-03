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
    owners: dict[str, str] = {}
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


async def batch_enrich_king_county(
    parcel_ids: list[str],
    *,
    time_budget_s: float | None = None,
    stats: dict | None = None,
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
    """
    import time as _time

    results: dict[str, dict[str, str | None]] = {}
    clean = list(dict.fromkeys(pid.strip() for pid in parcel_ids if pid and len(pid.strip()) >= 6))
    st = stats if stats is not None else {}
    st.update({"requested": len(clean), "property_found": 0, "mailing_candidates": 0,
               "mailing_attempted": 0, "mailing_found": 0, "deferred": [], "budget_exhausted": False})
    deadline = (_time.monotonic() + time_budget_s) if time_budget_s is not None else None

    def _over_budget() -> bool:
        return deadline is not None and _time.monotonic() >= deadline

    if not clean:
        return results

    # ── Phase 1: HTTP requests for property address + tax URLs (fast) ─────
    # Same shared gate as the owner-only path — this one also hits eRealProperty.
    check_source_or_raise(KING_EREALPROPERTY)

    _logger.info("Phase 1: HTTP lookup for %d parcels...", len(clean))
    tax_urls: dict[str, str] = {}  # pid → payment.kingcounty.gov URL

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
            if r.status_code != 200:
                continue

            # Extract Site Address
            m = re.search(r"Site Address</td>\s*<td[^>]*>([^<]+)", r.text)
            prop = m.group(1).replace("&nbsp;", "").strip() if m else None
            if not prop:
                prop = None

            # Extract Tax Bill URL (has correct tax account number)
            m2 = re.search(
                r'href="(https://payment\.kingcounty\.gov[^"]+)"', r.text
            )
            tax_url = m2.group(1).replace("&amp;", "&") if m2 else None

            # Owner/taxpayer name — same page, no extra request. Fills the
            # placeholder party_name on King tax-delinquent leads downstream.
            owner = _extract_owner_name(r.text)

            if prop or tax_url or owner:
                results[pid] = {
                    "property_address": prop,
                    "mailing_address": None,
                    "owner_name": owner,
                }
                if tax_url:
                    tax_urls[pid] = tax_url

        except Exception as exc:
            _logger.debug(
                "Property URL fetch failed for parcel=%s: %s",
                pid, str(exc)[:200],
            )

        await asyncio.sleep(0.1)  # minimal delay for HTTP

    st["property_found"] = sum(1 for r in results.values() if r.get("property_address"))
    _logger.info("Phase 1 done: %d/%d property addresses, %d tax URLs",
                 st["property_found"], len(clean), len(tax_urls))

    # ── Phase 2: Playwright for mailing addresses ──────────────────────────
    st["mailing_candidates"] = len(tax_urls)
    if not tax_urls:
        return results

    # Cap at 200 parcels to avoid job timeout (~5-10s per lookup)
    _MAX_MAILING_LOOKUPS = 200
    pids_to_lookup = list(tax_urls.keys())
    if len(pids_to_lookup) > _MAX_MAILING_LOOKUPS:
        _logger.info("Capping mailing lookups: %d → %d (to avoid timeout)", len(pids_to_lookup), _MAX_MAILING_LOOKUPS)
        pids_to_lookup = pids_to_lookup[:_MAX_MAILING_LOOKUPS]

    _logger.info("Phase 2: Playwright lookup for %d mailing addresses...", len(pids_to_lookup))

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

                except Exception:
                    pass

                await asyncio.sleep(0.2)


    found_mail = sum(1 for r in results.values() if r.get("mailing_address"))
    found_prop = sum(1 for r in results.values() if r.get("property_address"))
    st["mailing_found"] = found_mail
    # Parcels beyond the per-call mailing cap were never attempted either.
    st["deferred"].extend(p for p in tax_urls if p not in pids_to_lookup)
    _logger.info("Enrichment done: %d/%d property, %d/%d mailing",
                 found_prop, len(clean), found_mail, len(clean))
    return results
