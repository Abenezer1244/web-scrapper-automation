"""Cached-records (county_records) response hygiene — src/api/routes/scrapers.py.

Pure unit tests on the two helpers the endpoint uses. Pinned from prod on
2026-09-02: a probate config's "View" page served the entire 3,305-row county
cache (every row doc_type NULL) because the type filter had a
`doc_type IS NULL OR …` escape hatch, and 308 of those rows carried the literal
"(enrichment unavailable)" as an address. The SQL filter mirrors the scraper's
own matcher (Codex review): word-boundary for short codes, substring for
phrases, and the per-type exclude list.
"""
import re

from src.api.routes.scrapers import _cache_address_or_none, _cached_doc_type_filter
from src.scrapers.templates.eagleweb import _DOC_TYPE_EXCLUDE, _DOC_TYPE_MAP, _doc_type_matches


def _sql_matches(clause: str, params: dict, doc_type: str | None) -> bool:
    """Evaluate the generated fragment in Python with PostgreSQL semantics
    (ILIKE = case-insensitive substring; `~*` with \\m…\\M = word boundaries)."""
    if doc_type is None:
        return False  # NULL never satisfies ILIKE / ~* (and there is no IS NULL hatch)
    up = doc_type.upper()

    def cond(c: str) -> bool:
        m = re.fullmatch(r"doc_type (ILIKE|~\*) :(\w+)", c)
        assert m, c
        op, name = m.groups()
        val = params[name]
        if op == "ILIKE":
            return val.strip("%").upper() in up
        pat = val.replace(r"\m", r"\b").replace(r"\M", r"\b")
        return re.search(pat, up, re.IGNORECASE) is not None

    include, _, exclude = clause.partition(" AND NOT ")
    inc = any(cond(c) for c in include[1:-1].split(" OR "))
    if not inc:
        return False
    if exclude:
        return not any(cond(c) for c in exclude[1:-1].split(" OR "))
    return True


class TestDocTypeFilter:
    def test_probate_filter_has_no_null_escape_hatch(self):
        clause, _ = _cached_doc_type_filter("probate")
        assert clause is not None
        assert "IS NULL" not in clause
        assert not _sql_matches(clause, {}, None)

    def test_values_are_bound_not_interpolated(self):
        clause, params = _cached_doc_type_filter("probate")
        assert "PROBATE" not in clause  # values live in params only
        assert clause.count(":") == len(params)
        assert set(re.findall(r":(\w+)", clause)) == set(params)

    def test_short_code_cannot_bleed_into_successor_trustee(self):
        # "SUCC" is a Clallam probate code; "SUCCESSOR TRUSTEE" is a deed-of-trust
        # instrument. Word-boundary + the probate TRUSTEE exclude keep it out.
        clause, params = _cached_doc_type_filter("probate")
        assert not _sql_matches(clause, params, "APPOINTMENT OF SUCCESSOR TRUSTEE")
        assert _sql_matches(clause, params, "SUCC")
        assert _sql_matches(clause, params, "LETTERS TESTAMENTARY")

    def test_probate_excludes_apply(self):
        clause, params = _cached_doc_type_filter("probate")
        assert not _sql_matches(clause, params, "LACK OF PROBATE AFFIDAVIT")
        assert not _sql_matches(clause, params, "RESIGN/APPT/SUB SUCC TRUSTEE")

    def test_pre_foreclosure_short_codes(self):
        clause, params = _cached_doc_type_filter("pre_foreclosure")
        assert _sql_matches(clause, params, "NTS")
        assert _sql_matches(clause, params, "NTSCL")
        assert _sql_matches(clause, params, "NOTICE OF TRUSTEE SALE")
        assert not _sql_matches(clause, params, "WARRANTY DEED")

    def test_sql_filter_agrees_with_scraper_matcher(self):
        samples = [
            "PROBATE", "LETTERS OF ADMINISTRATION", "SUCC", "SUCCESSOR TRUSTEE",
            "APPOINTMENT OF SUCCESSOR TRUSTEE", "LACK OF PROBATE", "TOD", "TODAY",
            "DEATH CERTIFICATE", "NTS", "NTSCL", "LIS PENDENS", "DEED OF TRUST",
            "DISSOLUTION", "DISS", "TAX LIEN", "CERTIFICATE OF SALE", "WARRANTY DEED",
        ]
        for record_type, keywords in _DOC_TYPE_MAP.items():
            clause, params = _cached_doc_type_filter(record_type)
            excludes = _DOC_TYPE_EXCLUDE.get(record_type, [])
            for doc in samples:
                expected = _doc_type_matches(doc, keywords) and not any(x in doc for x in excludes)
                assert _sql_matches(clause, params, doc) is expected, (record_type, doc)

    def test_unknown_record_type_has_no_filter(self):
        assert _cached_doc_type_filter("not_a_record_type") == (None, {})


class TestAddressPlaceholder:
    def test_placeholder_becomes_null(self):
        assert _cache_address_or_none("(enrichment unavailable)") is None
        assert _cache_address_or_none("  (enrichment unavailable) ") is None

    def test_blank_becomes_null(self):
        assert _cache_address_or_none("") is None
        assert _cache_address_or_none(None) is None

    def test_real_address_passes_through_unchanged(self):
        assert _cache_address_or_none("20508 ISLAND PKWY E, LAKE TAPPS, WA, 98391-9081") == (
            "20508 ISLAND PKWY E, LAKE TAPPS, WA, 98391-9081"
        )
