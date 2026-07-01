"""Shared scraper reliability primitives — FAIL LOUD, never silent-empty.

A Celery scrape job is marked DONE the moment the scraper returns a list. So when
a scraper swallows a captcha / bot-block / transient navigation failure and returns
``[]``, a paying tenant receives an empty lead list that looks legitimate. These
helpers let every template distinguish a GENUINE zero-result window (return ``[]``)
from a FAILURE (raise → the worker marks the job FAILED, see
``src/workers/tasks.py`` which wraps ``_run_scraper`` in ``except Exception →
_fail_job``), and reject a soft-block page that masquerades as an empty result set.

Design notes (pressure-tested with Codex):
  - Block detection is deliberately HIGH-CONFIDENCE only (captcha / cloudflare /
    rate-limit / bot-challenge). Over-broad fingerprints ("forbidden", "403",
    "please log in") risk false-failing a legitimate results/help page. The
    primary safety rule is structural: NEITHER rows NOR an explicit empty-marker
    → raise (``classify_results_page`` returns "ambiguous"); block detection only
    adds a clearer reason when a block wall also happens to contain "no results".
  - The canary is STRICT (header promised N>0 but extracted 0 RAW rows → raise),
    compared on the PRE-dedup raw count so page caps / dedup don't false-trip it.
"""
from __future__ import annotations

import re


class ScraperExecutionError(RuntimeError):
    """A scrape failed in a way that MUST mark the job FAILED (never DONE-with-0).

    Subclasses ``RuntimeError`` so the worker's broad ``except Exception``
    terminalizes the job, and so existing ``raise RuntimeError(...)`` call sites
    (eagleweb / laserfiche) remain behaviourally compatible.
    """

    def __init__(
        self,
        county: str | None,
        platform: str,
        reason: str,
        *,
        record_type: str | None = None,
        doc_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int | None = None,
        context: object | None = None,
    ) -> None:
        self.county = county
        self.platform = platform
        self.reason = reason
        self.record_type = record_type
        self.doc_type = doc_type
        self.date_from = date_from
        self.date_to = date_to
        self.page = page
        meta: list[str] = []
        if record_type:
            meta.append(f"rt={record_type}")
        if doc_type:
            meta.append(f"doc_type={doc_type}")
        if page is not None:
            meta.append(f"page={page}")
        if date_from or date_to:
            meta.append(f"window={date_from}..{date_to}")
        if context is not None:
            meta.append(f"ctx={str(context)[:120]}")
        msg = f"{county or '?'}: {platform} — {reason}"
        if meta:
            msg += " [" + " ".join(meta) + "]"
        super().__init__(msg)


class TransientScrapeError(ScraperExecutionError):
    """A scrape failure that is LIKELY TRANSIENT and worth RETRYING.

    Page that never rendered, a pagination step that flaked, a momentary block
    wall, a portal timeout — conditions that usually clear on a re-run minutes
    later. The worker re-queues the job a bounded number of times (see
    ``SCRAPE_TRANSIENT_MAX_RETRIES``) before giving up. It still subclasses
    ``ScraperExecutionError``, so once retries are exhausted the job fails LOUD
    exactly like any other execution error — never scored DONE-with-0. Raising
    this instead of a bare ``RuntimeError`` is what tells the worker "retry me".
    """


class ScraperBlockedError(TransientScrapeError):
    """Captcha / bot-block / login / session-timeout wall.

    Transient by default — a bot-challenge / rate-limit wall frequently clears on
    a spaced re-run, so it is retried (bounded) before the job is failed.
    """


# Substrings that mark a Playwright base ``Error`` (non-timeout) as INFRA/transient:
# browser/page/context teardown or a network drop, which usually clear on a re-run.
# Deterministic bugs (strict-mode violations, bad selectors, non-actionable
# elements) do NOT match, so they fail fast instead of burning the retry budget.
_TRANSIENT_PW_MARKERS: tuple[str, ...] = (
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser has been disconnected",
    "context or browser has been closed",
    "page has been closed",
    "connection closed",
    "websocket",
    "net::err",
    "crashed",
)


def is_transient_scrape_error(exc: BaseException) -> bool:
    """True when ``exc`` is a transient scrape failure the worker should RETRY.

    Transient =
      - an explicit ``TransientScrapeError`` raised by a scraper, OR
      - a Playwright ``TimeoutError`` (navigation/action timeout — the classic
        momentary portal hiccup), OR
      - a Playwright base ``Error`` whose message matches an INFRA/network marker
        (browser/page/context closed, connection dropped, net::ERR, crash).

    Everything else — config errors, data-invariant violations, and DETERMINISTIC
    Playwright bugs (strict-mode / bad selector / not actionable) — is PERMANENT:
    retrying only burns worker time + backoff before hitting the same wall (Codex).

    Playwright is imported lazily so this stays importable in non-scrape contexts
    (and returns False there, since a non-Playwright exception can't be one).
    """
    if isinstance(exc, TransientScrapeError):
        return True
    try:
        from playwright.async_api import Error as _PWError
        from playwright.async_api import TimeoutError as _PWTimeout
    except Exception:  # playwright unavailable — exc can't be a Playwright error
        return False
    if isinstance(exc, _PWTimeout):
        return True
    if isinstance(exc, _PWError):
        msg = str(exc).lower()
        return any(m in msg for m in _TRANSIENT_PW_MARKERS)
    return False


# HIGH-CONFIDENCE block fingerprints only. Each is unlikely to appear on a normal
# county recorder results page, so a hit is a strong block signal. Kept tight on
# purpose — see module docstring.
_BLOCK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"re-?captcha|hcaptcha", "captcha"),
    (r"\bcaptcha\b", "captcha"),
    (r"cloudflare|cf-ray|checking your browser", "cloudflare"),
    (r"verify (?:you are|you're)(?: a)? human|are you a human|unusual traffic from",
     "bot-challenge"),
    (r"too many requests|rate limit exceeded", "rate-limited"),
)
_BLOCK_RE: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), label) for p, label in _BLOCK_PATTERNS
)


def detect_block(page_text: str | None) -> str | None:
    """Return a short block-reason label if the page looks like a captcha / bot /
    rate-limit wall, else ``None``. High-confidence only — see module docstring."""
    if not page_text:
        return None
    for rx, label in _BLOCK_RE:
        if rx.search(page_text):
            return label
    return None


def classify_results_page(
    *,
    row_count: int,
    page_text: str | None,
    empty_markers: tuple[str, ...],
) -> str:
    """Classify a results page into one of four verdicts the caller acts on:

    - ``"rows"``      — ``row_count > 0``; proceed.
    - ``"block"``     — a high-confidence block fingerprint is present; caller
                        should ``raise ScraperBlockedError``.
    - ``"empty"``     — no rows but an EXPLICIT empty-marker is present (and no
                        block); a genuine zero-result window — caller returns [].
    - ``"ambiguous"`` — no rows, no marker, no block; the search may have silently
                        failed — caller should ``raise ScraperExecutionError``
                        (we cannot prove a genuine empty).

    Block is checked before the empty-marker so a block wall that also renders a
    generic "no results" string is not misread as a genuine empty.
    """
    if row_count > 0:
        return "rows"
    if detect_block(page_text):
        return "block"
    low = (page_text or "").lower()
    # WORD-BOUNDARY match, not substring (Codex P1): a numeric marker like
    # "0 records" is a substring of "10 records" / "100 records". A plain
    # substring check would misread a parse-drift page that says "10 records"
    # (but yielded 0 rows) as a genuine empty and complete the job with 0 leads —
    # the exact false-empty this module exists to prevent. \b before the leading
    # "0" does not match between the "1" and "0" of "10", so "10 records" no
    # longer counts as empty.
    for m in empty_markers:
        if re.search(r"\b" + re.escape(m.lower()) + r"\b", low):
            return "empty"
    return "ambiguous"


def check_extraction_canary(
    *,
    expected_total: int | None,
    raw_extracted: int,
    county: str | None,
    platform: str,
    **kw: object,
) -> None:
    """Raise when the results header promised ``expected_total > 0`` but extraction
    produced 0 RAW rows — a parse-drift / silent-block signal.

    Compare against the PRE-dedup raw count (``raw_extracted``), not the final
    emitted/deduped count, so legitimate dedup or page caps never false-trip it.
    """
    if expected_total and expected_total > 0 and raw_extracted == 0:
        raise ScraperExecutionError(
            county,
            platform,
            f"results header reported {expected_total} record(s) but extracted 0 "
            "raw rows (parse drift or silent block)",
            **kw,  # type: ignore[arg-type]
        )
