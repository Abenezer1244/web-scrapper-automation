"""Coherence tests for the value-metric entitlement matrix (constants only — no
DB/env needed). Guards against the exact drift Codex flagged: gating a record
type that has no live connector, or omitting one that does.
"""
from src.config.constants import (
    ALL_RECORD_TYPES,
    COUNTY_LIMIT_BY_PLAN,
    RECORD_TYPES_BY_PLAN,
    Plan,
)

# The record-type slugs that actually exist in county_connectors.record_types
# (verified live 2026-06-21). If a connector adds a new type, update both this
# test and ALL_RECORD_TYPES together.
LIVE_RECORD_TYPE_SLUGS = {
    "probate",
    "pre_foreclosure",
    "tax_delinquent",
    "code_violation",
    "divorce",
    "death_certificate",
    "trustee_sale",
}


def test_every_plan_has_a_county_limit_and_record_types():
    for p in Plan:
        assert p.value in COUNTY_LIMIT_BY_PLAN, p.value
        assert p.value in RECORD_TYPES_BY_PLAN, p.value


def test_all_record_types_matches_live_registry_slugs():
    # No CLAUDE.md wishlist slugs that have no live connector.
    assert "eviction" not in ALL_RECORD_TYPES
    assert "death_cert" not in ALL_RECORD_TYPES
    assert "death_certificate" in ALL_RECORD_TYPES
    assert set(ALL_RECORD_TYPES) == LIVE_RECORD_TYPE_SLUGS


def test_per_plan_record_types_are_a_subset_of_live_types():
    for plan, types in RECORD_TYPES_BY_PLAN.items():
        assert types <= ALL_RECORD_TYPES, f"{plan} allows a non-live type"


def test_tiers_are_monotonic():
    # Record-type access only widens as you go up tiers.
    assert RECORD_TYPES_BY_PLAN["starter"] <= RECORD_TYPES_BY_PLAN["pro"]
    assert RECORD_TYPES_BY_PLAN["pro"] <= RECORD_TYPES_BY_PLAN["business"]
    assert RECORD_TYPES_BY_PLAN["business"] == RECORD_TYPES_BY_PLAN["agency"]
    # County cap widens, agency unlimited.
    assert COUNTY_LIMIT_BY_PLAN["starter"] < COUNTY_LIMIT_BY_PLAN["pro"]
    assert COUNTY_LIMIT_BY_PLAN["pro"] < COUNTY_LIMIT_BY_PLAN["business"]
    assert COUNTY_LIMIT_BY_PLAN["agency"] == -1


def test_starter_is_probate_only_and_pro_core_lists():
    assert RECORD_TYPES_BY_PLAN["starter"] == {"probate"}
    # Pro = the core distress lists incl. trustee_sale (Auction Leads); NOT all types.
    assert RECORD_TYPES_BY_PLAN["pro"] == {
        "probate", "pre_foreclosure", "tax_delinquent", "trustee_sale"
    }
    assert RECORD_TYPES_BY_PLAN["pro"] != ALL_RECORD_TYPES
