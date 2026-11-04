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


async def test_endpoint_never_serves_cached_records_even_from_a_legacy_row(db, client):
    # A cache row written by the old refresh: a real-looking person with ZIP+4
    # and an exact recording date. The endpoint must ignore its records.
    import json

    from sqlalchemy import text

    legacy = {
        "records": [{
            "date_recorded": "09/01/2026", "party_name": "Legacy P.", "county": "Pierce",
            "property_address": None, "mailing_address": "SUMNER, WA, 98390-1231",
            "has_parcel": True,
        }],
        "total_scraped": "12K+", "counties_active": 7,
        "enrichment_rate": "95%+", "freshness": "Updated daily",
    }
    await db.execute(
        text("""
            INSERT INTO public.public_sample_cache (id, payload, refreshed_at)
            VALUES (1, CAST(:p AS jsonb), now())
            ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, refreshed_at = now()
        """),
        {"p": json.dumps(legacy)},
    )
    await db.commit()
    try:
        resp = await client.get("/scrapers/sample")
        assert resp.status_code == 200
        body = resp.json()
        assert body["records"] == example_sample_records()
        assert "Legacy" not in resp.text and "98390-1231" not in resp.text
        # The aggregate stats still come from the cache.
        assert body["total_scraped"] == "12K+" and body["counties_active"] == 7
    finally:
        await db.execute(text("DELETE FROM public.public_sample_cache WHERE id = 1"))
        await db.commit()


async def test_endpoint_with_an_unwarmed_cache_serves_examples_and_zero_stats(db, client):
    from sqlalchemy import text

    await db.execute(text("DELETE FROM public.public_sample_cache WHERE id = 1"))
    await db.commit()
    resp = await client.get("/scrapers/sample")
    assert resp.status_code == 200
    body = resp.json()
    assert body["records"] == example_sample_records()
    assert body["total_scraped"] == "0+" and body["counties_active"] == 0


def test_refresh_no_longer_reads_individual_result_rows():
    # The refresh may count rows for the aggregate stats, but it must not select
    # whole Result rows (the old path that copied names and addresses out).
    import inspect

    src = inspect.getsource(public_cache._refresh_public_sample_cache_impl)
    assert "select(Result," not in src
    assert "party_name" not in src
