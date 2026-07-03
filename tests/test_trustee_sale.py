"""Trustee Sale ("Auction Leads") record type — wiring + record mapping.

Pure Python, no DB: constructs real ``NtsNotice`` instances in-memory and checks
the ScrapedRecord mapping, plan/entitlement wiring, registry allowlist, and the
thin per-county subclasses. The DB read path (``scrape()``) is exercised in CI /
live e2e, not here (no local Postgres).
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.api.entitlements import record_type_violations
from src.config.constants import ALL_RECORD_TYPES, RECORD_TYPES_BY_PLAN
from src.db.models import NtsNotice
from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES
from src.scrapers.trustee_sale import (
    RECORD_TYPE,
    KingWATrusteeSaleScraper,
    PierceWATrusteeSaleScraper,
    SnohomishWATrusteeSaleScraper,
    _record_from_notice,
    _TrusteeSaleScraper,
)


def _notice(**over) -> NtsNotice:
    """A realistic active, future-dated NTS notice row (in-memory, no session)."""
    base = {
        "id": "notice-abc-123",
        "source": "tacoma_daily_index",
        "ts_number": "WA-24-999",
        "county": "pierce",
        "state": "WA",
        "parcel": "0519285029",
        "property_address": "123 MAIN ST, TACOMA, WA 98401",
        "auction_date": date.today() + timedelta(days=30),
        "auction_time": "10:00 AM",
        "auction_location": "County Courthouse",
        "grantor": "JOHN SMITH, A MARRIED MAN, AS HIS SOLE AND SEPARATE PROPERTY",
        "trustee": "Quality Loan Service",
        "beneficiary": "Wells Fargo",
        "principal_owing": Decimal("282345.67"),
        "nod_date": "1/15/2026",
        "source_url": "https://example.com/notice",
    }
    base.update(over)
    return NtsNotice(**base)


class TestPlanWiring:
    def test_trustee_sale_is_a_live_record_type(self):
        assert RECORD_TYPE == "trustee_sale"
        assert "trustee_sale" in ALL_RECORD_TYPES

    def test_pro_business_agency_include_trustee_sale(self):
        assert "trustee_sale" in RECORD_TYPES_BY_PLAN["pro"]
        assert "trustee_sale" in RECORD_TYPES_BY_PLAN["business"]
        assert "trustee_sale" in RECORD_TYPES_BY_PLAN["agency"]

    def test_starter_excludes_trustee_sale(self):
        assert "trustee_sale" not in RECORD_TYPES_BY_PLAN["starter"]

    def test_entitlement_allows_pro_but_not_starter(self):
        # Pro may request it; Starter is a violation (gated).
        assert record_type_violations("pro", ["trustee_sale"]) == set()
        assert record_type_violations("starter", ["trustee_sale"]) == {"trustee_sale"}


class TestRegistryAllowlist:
    def test_module_is_allowlisted(self):
        assert "src.scrapers.trustee_sale" in _ALLOWED_SCRAPER_MODULES


class TestCountySubclasses:
    def test_three_counties_wired(self):
        assert PierceWATrusteeSaleScraper.COUNTY == "pierce"
        assert SnohomishWATrusteeSaleScraper.COUNTY == "snohomish"
        assert KingWATrusteeSaleScraper.COUNTY == "king"
        for cls in (
            PierceWATrusteeSaleScraper,
            SnohomishWATrusteeSaleScraper,
            KingWATrusteeSaleScraper,
        ):
            assert issubclass(cls, _TrusteeSaleScraper)

    def test_subclass_constructs(self):
        s = PierceWATrusteeSaleScraper()
        assert s.COUNTY == "pierce"

    def test_bare_base_refuses_without_county(self):
        # A county-less scraper can't know which notices to read — must fail loud.
        with pytest.raises(ValueError):
            _TrusteeSaleScraper()


class TestRecordMapping:
    def test_core_fields(self):
        rec = _record_from_notice(_notice())
        assert rec.parcel_id == "0519285029"
        assert rec.property_address == "123 MAIN ST, TACOMA, WA 98401"
        assert rec.doc_type == "Notice of Trustee Sale"
        # grantor vesting boilerplate stripped, owner name preserved
        assert "SMITH" in (rec.party_name or "")
        assert "SOLE AND SEPARATE" not in (rec.party_name or "")
        # date_recorded prefers an already-M/D/YYYY nod_date (parseable by the
        # generated date_recorded_parsed column)
        assert rec.date_recorded == "1/15/2026"

    def test_raw_html_hash_is_stable_and_notice_unique(self):
        # Per-notice idempotency key so two active notices on the same property/date
        # with different TS#s don't collide on the insert ON CONFLICT.
        h1 = _record_from_notice(_notice()).raw_html_hash
        h1b = _record_from_notice(_notice()).raw_html_hash
        h2 = _record_from_notice(_notice(ts_number="WA-24-000")).raw_html_hash
        assert h1 and len(h1) <= 32  # fits Result.raw_html_hash String(32)
        assert h1 == h1b  # stable across re-runs (same source+ts_number)
        assert h1 != h2  # distinct notice -> distinct fingerprint

    def test_nts_source_contract(self):
        rec = _record_from_notice(_notice())
        src = rec.enrichment_data["nts_source"]
        # notice_id is the finalizer's REQUIRED key (fail-closed downstream)
        assert src["notice_id"] == "notice-abc-123"
        assert src["default_amount"] == "282345.67"  # Decimal -> str, not float
        assert src["ts_number"] == "WA-24-999"
        assert src["trustee"] == "Quality Loan Service"
        # auction_date carried as ISO string
        assert src["auction_date"] == (date.today() + timedelta(days=30)).isoformat()

    def test_date_recorded_falls_back_to_auction_date_as_mdy(self):
        # No usable nod_date -> the real auction Date, formatted M/D/YYYY (parseable).
        d = date(2026, 8, 5)
        rec = _record_from_notice(_notice(nod_date=None, auction_date=d))
        assert rec.date_recorded == "8/5/2026"

    def test_date_recorded_ignores_non_mdy_nod_date(self):
        # An ISO nod_date is NOT M/D/YYYY -> fall back to auction date (parseable),
        # never emit an unparseable ISO date_recorded.
        d = date(2026, 8, 5)
        rec = _record_from_notice(_notice(nod_date="2026-01-15", auction_date=d))
        assert rec.date_recorded == "8/5/2026"

    def test_null_principal_owing_is_none(self):
        rec = _record_from_notice(_notice(principal_owing=None))
        assert rec.enrichment_data["nts_source"]["default_amount"] is None
