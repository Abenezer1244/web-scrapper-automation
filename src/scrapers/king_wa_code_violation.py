"""King County (WA) code violations: one connector over every jurisdiction we can collect.

Each jurisdiction is a source adapter in src/scrapers/king_cv_sources/ (Seattle SDCI,
Bellevue, Burien, and unincorporated King County through the county's Accela portal).
This connector runs them in order and merges their records; every record keeps its own
enrichment_data.source, case number and per-case raw_html_hash.

PARTIAL FAILURE. An adapter retries its own transient failures and raises when one
survives them. A failed source does not fail the job while another source succeeded:
the job ships the records it has, the failure is logged with the source key, and a
customer-visible warning naming the missing jurisdiction(s) is published to the job log
(`scrape_warnings`, published by the worker's _run_scraper). Per-source outcomes are in
`source_status`. When EVERY source fails, the scrape raises as it always has.

Seattle rows carry no parcel or owner (enrichment locates the parcel from coordinates
into enrichment_data.kc_pin). Bellevue, Burien and King County Accela print the King
PIN, which is stored as parcel_id at scrape; their owner is read from King eRealProperty
during enrichment. Accela runs last: it is the slowest source (a paced browser).
"""
from __future__ import annotations

from collections.abc import Sequence

from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.king_cv_sources.base import CodeViolationSource, DateRangeTooLargeError
from src.scrapers.king_cv_sources.bellevue import BellevueSource
from src.scrapers.king_cv_sources.burien import BurienSource
from src.scrapers.king_cv_sources.kingco_accela import KingCountyAccelaSource
from src.scrapers.king_cv_sources.seattle_sdci import SeattleSDCISource
from src.scrapers.reliability import TransientScrapeError, is_transient_scrape_error
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.king_wa_code_violation")

# The registration list: every adapter the King code_violation connector runs, in order.
SOURCES: tuple[type[CodeViolationSource], ...] = (
    SeattleSDCISource, BellevueSource, BurienSource, KingCountyAccelaSource)

SOURCE_OK = "ok"
SOURCE_FAILED = "failed"


def _join_names(names: Sequence[str]) -> str:
    """"Seattle", "Seattle and Bellevue", "Seattle, Bellevue, and Burien"."""
    names = list(names)
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def scope_note(sources: Sequence[type[CodeViolationSource]] = SOURCES) -> str:
    return (f"Collected from the code enforcement records of "
            f"{_join_names([s.jurisdiction for s in sources])}.")


def partial_failure_warning(failed: Sequence[str], succeeded: Sequence[str],
                            too_large: Sequence[str] = ()) -> str:
    """Customer-facing job log line for a run that shipped without some jurisdictions.

    ``failed`` are jurisdictions that may work on a later run; ``too_large`` had more
    cases in the date range than one run can collect, which a later run would not fix.
    """
    missing = [*failed, *too_large]
    msg = (f"Code violation records from {_join_names(missing)} could not be collected this "
           f"run, so these leads cover {_join_names(succeeded)} only.")
    if failed:
        msg += f" Run this scraper again later to include {_join_names(failed)}."
    if too_large:
        msg += (f" This date range has more {_join_names(too_large)} cases than one run can "
                f"collect, so use a shorter date range to include them.")
    return msg


class KingWACodeViolationScraper(BridgeScraper):
    """King County code violations merged from every registered jurisdiction source."""

    @classmethod
    def collection_scope(cls, record_type: str):
        """SHOW descriptor: King code violations come from city datasets, not documents."""
        from src.scrapers.doc_scope import dataset

        if record_type != "code_violation":
            return None
        return dataset(scope_note())

    def __init__(self, record_type: str = "code_violation", *,
                 sources: Sequence[CodeViolationSource] | None = None):
        super().__init__()
        self.sources: list[CodeViolationSource] = (
            list(sources) if sources is not None else [cls() for cls in SOURCES])
        if not self.sources:
            raise ValueError("King code violations need at least one source")
        #: {source key: "ok" | "failed"} for the last scrape.
        self.source_status: dict[str, str] = {}
        #: Customer-facing warnings from the last scrape, published to the job log.
        self.scrape_warnings: list[str] = []

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        self.source_status = {}
        self.scrape_warnings = []
        records: list[ScrapedRecord] = []
        failures: list[tuple[CodeViolationSource, Exception]] = []

        for source in self.sources:
            # Set on every scrape, so a callback from an earlier scrape never outlives it.
            # The record count is cumulative across sources; pages are per source.
            source.on_progress = (
                (lambda pages, total, count, _done=len(records), _cb=self.on_progress:
                 _cb(pages, total, _done + count))
                if self.on_progress is not None else None)
            try:
                got = await source.fetch(date_from, date_to)
            except Exception as exc:
                source.on_progress = None
                # A Celery time limit is the job's deadline, not this source's failure.
                if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
                    raise
                self.source_status[source.key] = SOURCE_FAILED
                failures.append((source, exc))
                _logger.error("King code violation source %s failed for %s to %s: %s: %s",
                              source.key, date_from, date_to, type(exc).__name__,
                              str(exc)[:300])
                continue
            source.on_progress = None  # never outlives this source's fetch
            self.source_status[source.key] = SOURCE_OK
            records.extend(got)
            _logger.info("King code violation source %s: %d records", source.key, len(got))

        if failures and len(failures) == len(self.sources):
            names = ", ".join(s.key for s, _ in failures)
            first = failures[0][1]
            msg = f"King code violation: every source failed ({names}): {str(first)[:200]}"
            # Keep the worker's retry decision: all-transient failures are retried.
            if all(is_transient_scrape_error(e) for _, e in failures):
                raise TransientScrapeError(msg) from first
            raise RuntimeError(msg) from first
        if failures:
            failed = [s.jurisdiction for s, e in failures if not isinstance(e, DateRangeTooLargeError)]
            too_large = [s.jurisdiction for s, e in failures if isinstance(e, DateRangeTooLargeError)]
            succeeded = [s.jurisdiction for s in self.sources
                         if self.source_status.get(s.key) == SOURCE_OK]
            self.scrape_warnings.append(partial_failure_warning(failed, succeeded, too_large))
            _logger.warning("King code violation partial scrape: source_status=%s", self.source_status)

        _logger.info("King WA code violations complete: %d records, source_status=%s",
                     len(records), self.source_status)
        return records

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass
