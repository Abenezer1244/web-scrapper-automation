"""Statewide-layer county scope: Asotin's parcels carry FIPS_NR NULL, not '003'.

Measured live 2026-11-12 on WAGeoservices Current_Parcels: FIPS_NR='003' matches 0
rows, while the 13,629 NULL-FIPS rows are all in Asotin County cities. Scoping Asotin
by '003' made every Asotin situs lookup come back empty.
"""
from src.scrapers.enrichment import county_gis as cg


def test_asotin_is_scoped_by_the_null_bucket():
    assert cg._wa_fips_filter("asotin") == " AND FIPS_NR IS NULL"
    assert cg._wa_fips_filter("Asotin") == " AND FIPS_NR IS NULL"


def test_every_other_county_keeps_its_fips_code():
    assert cg._wa_fips_filter("pierce") == " AND FIPS_NR='053'"
    assert cg._wa_fips_filter("garfield") == " AND FIPS_NR='023'"


def test_an_unknown_county_is_unscoped():
    assert cg._wa_fips_filter("nowhere") == ""
