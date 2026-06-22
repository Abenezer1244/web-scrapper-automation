"""Product invariant: a ``tax_delinquent`` record set may only be persisted if
every row comes from a qualified tax source AND carries both delinquent_amount
and bill_year. Guards against the Clark 2026-04 incident (deeds written as tax
leads with no amount/year). See ``validate_tax_delinquent_records`` in
``src/workers/tasks_helpers/dedup.py`` and BACKLOG §9.

Real ScrapedRecord objects, no mocks (per .claude/rules/testing.md).
"""

import pytest

from src.scrapers.base_scraper import ScrapedRecord
from src.workers.tasks_helpers.dedup import (
    TaxDelinquentInvariantError,
    validate_tax_delinquent_records,
)


def _king_tax_record(**overrides):
    """A well-formed King tax_delinquent record (the shape the live scraper emits)."""
    enrichment = {
        "source": "king_county_delinquent_taxes",
        "delinquent_amount": "5123.45",
        "bill_year": 2023,
    }
    enrichment.update(overrides.pop("enrichment_data", {}))
    return ScrapedRecord(
        date_recorded="2024-01-15",
        party_name="DOE JANE",
        parcel_id="1234567890",
        property_address="123 MAIN ST, SEATTLE WA",
        enrichment_data=enrichment,
        **overrides,
    )


def test_valid_king_record_passes():
    validate_tax_delinquent_records([_king_tax_record()], "tax_delinquent")


def test_valid_snohomish_record_passes():
    """Independent Snohomish-shaped record (not a King fixture with the source
    swapped) — proves the guard accepts a genuinely different trusted source."""
    rec = ScrapedRecord(
        date_recorded="2024-03-02",
        party_name="SMITH JOHN",
        parcel_id="00500000100",
        property_address="9 PINE AVE, EVERETT WA",
        enrichment_data={
            "source": "snohomish_county_delinquent_taxes",
            "delinquent_amount": "812.00",
            "bill_year": 2022,
        },
    )
    validate_tax_delinquent_records([rec], "tax_delinquent")


def test_untrusted_source_fails():
    """The Clark failure mode: a recorder-source row labeled tax_delinquent."""
    bad = ScrapedRecord(
        parcel_id="98765",
        doc_type="DEED OF TRUST",
        enrichment_data={"source": "clark_county_recorder"},
    )
    with pytest.raises(TaxDelinquentInvariantError) as exc:
        validate_tax_delinquent_records([bad], "tax_delinquent")
    assert "untrusted source" in str(exc.value)
    assert "clark_county_recorder" in str(exc.value)


def test_missing_source_fails():
    bad = ScrapedRecord(parcel_id="1", enrichment_data={})
    with pytest.raises(TaxDelinquentInvariantError):
        validate_tax_delinquent_records([bad], "tax_delinquent")


def test_trusted_source_missing_amount_fails():
    rec = _king_tax_record()
    rec.enrichment_data.pop("delinquent_amount")
    with pytest.raises(TaxDelinquentInvariantError) as exc:
        validate_tax_delinquent_records([rec], "tax_delinquent")
    assert "structured tax fields" in str(exc.value)


def test_trusted_source_missing_year_fails():
    rec = _king_tax_record()
    rec.enrichment_data.pop("bill_year")
    with pytest.raises(TaxDelinquentInvariantError):
        validate_tax_delinquent_records([rec], "tax_delinquent")


def test_trusted_source_malformed_amount_fails():
    """A present-but-uncoercible amount is as invalid as a missing one
    (_extract_tax_fields bounds-checks; the guard reuses it)."""
    rec = _king_tax_record()
    rec.enrichment_data["delinquent_amount"] = "not-a-number"
    with pytest.raises(TaxDelinquentInvariantError):
        validate_tax_delinquent_records([rec], "tax_delinquent")


def test_out_of_range_year_fails():
    rec = _king_tax_record()
    rec.enrichment_data["bill_year"] = 1800  # below the 1900 floor
    with pytest.raises(TaxDelinquentInvariantError):
        validate_tax_delinquent_records([rec], "tax_delinquent")


def test_first_bad_row_in_a_good_batch_fails():
    """One bad row anywhere fails the whole set (threshold > 0)."""
    good = _king_tax_record()
    bad = _king_tax_record()
    bad.enrichment_data["source"] = "clark_county_recorder"
    with pytest.raises(TaxDelinquentInvariantError):
        validate_tax_delinquent_records([good, good, bad, good], "tax_delinquent")


def test_non_tax_record_type_is_noop():
    """A deed-shaped record under probate/pre_foreclosure must NOT be touched —
    the invariant is scoped strictly to tax_delinquent."""
    rec = ScrapedRecord(
        parcel_id="1",
        doc_type="DEED OF TRUST",
        enrichment_data={"source": "clark_county_recorder"},
    )
    for rt in ("probate", "pre_foreclosure", "code_violation", "divorce", ""):
        validate_tax_delinquent_records([rec], rt)


def test_empty_record_set_is_noop():
    validate_tax_delinquent_records([], "tax_delinquent")
