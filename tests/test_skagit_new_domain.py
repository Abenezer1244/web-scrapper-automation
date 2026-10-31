"""Skagit's recorder moved to classic.skagitcountywa.gov (the old host 301s there).

Registry routes both hosts, exactly, to SkagitRecordingScraper; the new host becomes an
approved scrape target (never its parent domain); migration 122 moves only Skagit's old
URL. Real registry, real SSRF allowlist, real migration SQL on the test DB.
"""
import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from src.scrapers.registry import _detect_template
from src.scrapers.templates.skagit_recording import SkagitRecordingScraper

_MIG_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "122_skagit_recorder_new_domain.py"


def _mig():
    spec = importlib.util.spec_from_file_location("_mig122", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("url", [
    "https://classic.skagitcountywa.gov/search/Recording/",
    "https://www.skagitcounty.net/Search/Recording/",
])
def test_both_hosts_route_to_the_skagit_template(url):
    assert _detect_template(url) is SkagitRecordingScraper


@pytest.mark.parametrize("url", [
    "https://classic.skagitcountywa.gov.evil.example/search/Recording/",   # suffix spoof
    "https://evil.example/search/Recording/?next=www.skagitcounty.net",    # host in the query
    "https://www.skagitcountywa.gov/search/Recording/",                     # a sibling host
    "https://classic.skagitcountywa.gov/",                                  # not the recorder
])
def test_lookalike_urls_are_not_skagit(url):
    assert _detect_template(url) is not SkagitRecordingScraper


def test_the_new_host_is_allowlisted_exactly():
    from src.api.middleware.security import validate_scraping_target

    SkagitRecordingScraper(base_url=_mig().NEW_URL, county="skagit", state="WA",
                           record_types=["probate"])
    validate_scraping_target(_mig().NEW_URL, resolve=False)
    with pytest.raises(ValueError):
        validate_scraping_target("https://www.skagitcountywa.gov/", resolve=False)


def test_migration_chains_onto_121():
    mig = _mig()
    assert (mig.revision, mig.down_revision) == ("122", "121")


def test_migration_moves_only_skagits_old_url():
    from src.db.session import sync_engine

    mig = _mig()
    rows = {
        "skagit_old": ("skagit", "WA", "https://www.skagitcounty.net/Search/Recording/"),
        "skagit_old_no_slash": ("Skagit", "wa", "https://WWW.skagitcounty.net/search/recording"),
        "skagit_other": ("skagit", "WA", "https://www.skagitcounty.net/Search/Other/"),
        "other_county": ("whatcom", "WA", "https://www.skagitcounty.net/Search/Recording/"),
        "other_state": ("skagit", "OR", "https://www.skagitcounty.net/Search/Recording/"),
    }
    ids = {k: str(uuid.uuid4()) for k in rows}
    with sync_engine.connect() as conn:
        tx = conn.begin()
        try:
            for key, (county, state, url) in rows.items():
                conn.execute(text(
                    "INSERT INTO county_connectors (id, county, state, record_types, scraper_class, "
                    "scraper_mode, render_mode, base_url, health_status, active) VALUES "
                    "(:id, :c, :s, '[\"probate\"]', 'x', 'template', 'playwright', :u, 'down', true)"),
                    {"id": ids[key], "c": county, "s": state, "u": url})

            def urls():
                return {k: conn.execute(text("SELECT base_url FROM county_connectors WHERE id = :i"),
                                        {"i": ids[k]}).scalar() for k in rows}

            conn.execute(text(mig.UPGRADE_SQL))
            after = urls()
            assert after["skagit_old"] == mig.NEW_URL
            assert after["skagit_old_no_slash"] == mig.NEW_URL
            for k in ("skagit_other", "other_county", "other_state"):
                assert after[k] == rows[k][2], k
            conn.execute(text(mig.UPGRADE_SQL))          # idempotent
            assert urls() == after

            conn.execute(text(mig.DOWNGRADE_SQL))
            back = urls()
            assert back["skagit_old"] == mig.OLD_URL and back["skagit_old_no_slash"] == mig.OLD_URL
            for k in ("skagit_other", "other_county", "other_state"):
                assert back[k] == rows[k][2], k
        finally:
            tx.rollback()
