"""Golden + divergence-guard test for the two address normalizers.

`property_identity.normalize_address` (feeds compute_property_key — an identity /
dedup / billing-adjacent key) and `skip_trace._normalize_address` (skip-trace cache
key) are SEPARATE functions with byte-identical behavior today. A dead-code audit
flagged the duplication; we deliberately did NOT merge them (touching a function
that feeds a frozen identity key is high blast-radius for ~6 LOC). Instead this test
PINS their output to exact golden values AND asserts they stay in lock-step — so any
future divergence (the real risk Codex flagged) fails CI immediately, without the
risky refactor. If you ever DO merge them, this test must keep passing unchanged.
"""
import pytest

from src.scrapers.enrichment.skip_trace import _normalize_address
from src.workers.property_identity import normalize_address

# (input, expected) — pins the exact contract: uppercase, drop [.,#] to spaces,
# collapse all whitespace to single spaces, strip ends. None/empty -> "".
GOLDEN: list[tuple[str | None, str]] = [
    ("123 Main St., #4  ", "123 MAIN ST 4"),
    (None, ""),
    ("", ""),
    ("   ", ""),
    ("456 N.W. 7th Ave, Apt #2B", "456 N W 7TH AVE APT 2B"),
    ("lowercase road", "LOWERCASE ROAD"),
    ("Tab\tand\nnewline", "TAB AND NEWLINE"),
    ("PO Box #1,234", "PO BOX 1 234"),
    ("already normal", "ALREADY NORMAL"),
]


@pytest.mark.parametrize("raw,expected", GOLDEN)
def test_property_identity_normalize_golden(raw: str | None, expected: str) -> None:
    assert normalize_address(raw) == expected


@pytest.mark.parametrize("raw,expected", GOLDEN)
def test_skip_trace_normalize_golden(raw: str | None, expected: str) -> None:
    assert _normalize_address(raw) == expected


@pytest.mark.parametrize("raw,_expected", GOLDEN)
def test_two_normalizers_stay_in_lockstep(raw: str | None, _expected: str) -> None:
    # The divergence guard: the two keys MUST normalize identically. If this ever
    # fails, the skip-trace cache key and the property identity key have drifted.
    assert _normalize_address(raw) == normalize_address(raw)
