"""The public /scrapers/sample records are illustrative, never real people.

The endpoint is unauthenticated. Its previous build served real owners' first
names, last initials, ZIP+4 and recording dates (compliance audit 2026-11, P1-6).
"""

import re

from src.workers.scheduler_helpers import public_cache
from src.workers.scheduler_helpers.public_cache import example_sample_records

_PUBLIC_KEYS = {
    "record_type", "date_recorded", "party_name", "county",
    "property_address", "mailing_address", "has_parcel", "example",
}


def test_every_sample_record_is_flagged_as_an_example():
    records = example_sample_records()
    assert records, "the landing sample must not be empty"
    assert all(r["example"] is True for r in records)
    assert all(r["party_name"] == "Example owner" for r in records)


def test_sample_records_carry_no_street_zip4_or_real_date():
    for r in example_sample_records():
        assert set(r) == _PUBLIC_KEYS
        assert r["property_address"] is None
        # 5-digit ZIP only: a ZIP+4 narrows a mailing address to a few households.
        assert re.search(r"\b\d{5}$", r["mailing_address"])
        assert not re.search(r"\d{5}-\d{4}", r["mailing_address"])
        assert not re.search(r"\d", r["date_recorded"])


def test_callers_get_copies_not_the_shared_constant():
    first = example_sample_records()
    first[0]["party_name"] = "mutated"
    assert example_sample_records()[0]["party_name"] == "Example owner"


def test_refresh_no_longer_reads_individual_result_rows():
    # The refresh may count rows for the aggregate stats, but it must not select
    # whole Result rows (the old path that copied names and addresses out).
    import inspect

    src = inspect.getsource(public_cache._refresh_public_sample_cache_impl)
    assert "select(Result," not in src
    assert "party_name" not in src
