"""Guard tests for the NTS DB matcher plumbing (scoring itself is in test_nts_matcher).

The candidate-load/write SQL is integration-level (needs a real DB); here we pin the
cheap invariants: empty input is a no-op (no DB touched) and the module imports +
registers its beat task cleanly.
"""
from src.workers.nts_matcher_task import (
    NTS_MATCH_COUNTIES,
    match_results_inline,
)


def test_inline_empty_is_noop_without_db():
    # `db` is never touched when there are no candidate rows
    assert match_results_inline(db=None, result_dicts=[], county="snohomish") == 0


def test_beat_task_registered():
    from src.workers import app
    assert "src.workers.nts_matcher_task.match_nts_notices" in app.tasks


def test_match_counties_cover_every_crawler():
    # the matcher must be wired for every county that has a crawler populating notices
    assert set(NTS_MATCH_COUNTIES) >= {"pierce", "snohomish", "king", "clark"}


def test_clark_columbian_crawler_registered():
    import src.workers.nts_crawler  # noqa: F401 — import registers the @app.task crawlers
    from src.workers import app
    assert "src.workers.nts_crawler.crawl_nts_columbian_clark" in app.tasks


def test_pdf_crawler_tasks_registered():
    import src.workers.nts_crawler  # noqa: F401 — import registers the @app.task crawlers
    from src.workers import app
    assert "src.workers.nts_crawler.crawl_nts_snoho_tribune" in app.tasks
    assert "src.workers.nts_crawler.crawl_nts_king_queenanne" in app.tasks
