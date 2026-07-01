"""Pierce ARMS probate party-name parsing + orientation.

Regression guard for the live bug where a name cell carrying only the ARMS
role marker "[E]" (no associated name indexed) became a lead literally named
"[E]". These are pure-function tests (no DB, no network).
"""

from bs4 import BeautifulSoup

from src.scrapers.pierce_wa_probate import (
    PierceWAARMSScraper,
    _clean_arms_name,
)


def _cell(inner_html: str):
    return BeautifulSoup(f"<td>{inner_html}</td>", "html.parser").find("td")


class TestCleanArmsName:
    def test_bare_role_markers_are_not_names(self):
        for marker in ["[E]", "[R]", " [E] ", " [R] [E] ", "[E](+)", "(+)"]:
            assert _clean_arms_name(marker) is None

    def test_empty_and_none(self):
        assert _clean_arms_name(None) is None
        assert _clean_arms_name("") is None
        assert _clean_arms_name("   ") is None

    def test_real_name_preserved(self):
        assert _clean_arms_name("WALKER JOHN C EST OF") == "WALKER JOHN C EST OF"

    def test_leading_marker_stripped_name_kept(self):
        assert _clean_arms_name("[R] HANSON MICHAEL") == "HANSON MICHAEL"

    def test_plus_marker_stripped_but_name_kept(self):
        assert _clean_arms_name("HANSON KATHLEEN(+)") == "HANSON KATHLEEN"


class TestParseNameCell:
    def test_bare_e_marker_yields_no_party(self):
        # The live "[E]" junk lead: a cell that is only the marker must parse to
        # (None, None) so the record is dropped by the party-name guard.
        assert PierceWAARMSScraper._parse_name_cell(_cell("[E]")) == (None, None)

    def test_decedent_and_heir_split(self):
        cell = _cell("[R] WALKER JOHN C EST OF [E] WALKER ZOYA S")
        assert PierceWAARMSScraper._parse_name_cell(cell) == (
            "WALKER JOHN C EST OF",
            "WALKER ZOYA S",
        )

    def test_plus_marker_stripped_from_heir(self):
        cell = _cell("[R] HANSON MICHAEL CHARLES [E] HANSON KATHLEEN(+)")
        assert PierceWAARMSScraper._parse_name_cell(cell) == (
            "HANSON MICHAEL CHARLES",
            "HANSON KATHLEEN",
        )

    def test_heir_only_has_no_party(self):
        # No decedent in the [R] slot -> party_name None -> record later dropped.
        party, heirs = PierceWAARMSScraper._parse_name_cell(_cell("[E] WALKER ZOYA S"))
        assert party is None
        assert heirs == "WALKER ZOYA S"
