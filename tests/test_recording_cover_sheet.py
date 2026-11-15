"""The parcel a Benton recording cover sheet names (Benton death certificates, run f1277c60).

Each case below is the cover sheet's parcel field exactly as the text layer of a real
Benton recorded image prints it (2026-11-15). Only the field is kept: the certificate
itself carries personal details that have no place in a fixture.
"""
from src.scrapers.sources.recording_cover_sheet import index_prefix, read_cover_sheet_parcel

# 2026-027909: dashed parcel on the line after the label.
DASHED = ("RECORDING COVER SHEET\nASSESSOR'S PROPERTY TAX PARCEL/ACCOUNT NUMBER:\n"
          "1-2798-102-0005-003\n\n2026-027909\n")
# 2026-027396: plain 15 digits under a "TAX PARCEL NUMBER(S)" label.
PLAIN = ("ABBREVIATED LEGAL DESCRIPTION\nLot36,Block6,Amended PlatofBlocks5,6,7 and8,AndersonAddition\n"
         "Completelegaldescriptionisonpage ofdocument\nTAX PARCEL NUMBER(S)\n101894020006036\n"
         "AdditionalTaxAccountsareonpage ofdocument\n")
# 2026-028271: compact label, the text layer having dropped its spaces.
COMPACT = ("Assessor'sPropertyTax Parcel/AccountNumber:\n1-0798-107-0000-001\n"
           "The Auditor/Recorderwillrelyon theinformationprovidedon theform.\n")
# 2026-026845: the field left blank; the next line is the form's own checkbox text.
BLANK = ("Assessor's15-DigitParcelNumber:\nPropertyTaxParcelIDnumber isnotyetassigned.\n"
         "Additionalnumber(s)canbefoundon page ofdocument.\n")
# 2026-026343: a handwritten value the text layer turned into noise.
NOISE = ("Assessor's15-DigitParcelNumber:\n\\MR30110®0\nPropertyTaxParcelIDnumber isnotyetassigned.\n"
         "Additionalnumber(s)canbefoundon page ofdocument.\n")


def test_a_dashed_parcel_on_the_next_line_is_found():
    got = read_cover_sheet_parcel(DASHED)
    assert (got.outcome, got.parcel) == ("found", "127981020005003")
    assert "1-2798-102-0005-003" in got.evidence


def test_a_plain_parcel_under_a_tax_parcel_label_is_found():
    assert read_cover_sheet_parcel(PLAIN).parcel == "101894020006036"


def test_a_label_without_spaces_is_still_a_label():
    assert read_cover_sheet_parcel(COMPACT).parcel == "107981070000001"


def test_a_blank_field_means_the_recording_names_no_parcel():
    """The checkbox text "Property Tax Parcel ID number is not yet assigned" is the form's,
    not a value: the field is empty, so a later owner-name lookup is allowed."""
    got = read_cover_sheet_parcel(BLANK)
    assert (got.outcome, got.parcel, got.candidates) == ("none", None, [])


def test_a_written_but_unreadable_value_is_never_guessed():
    got = read_cover_sheet_parcel(NOISE)
    assert (got.outcome, got.parcel) == ("unreadable", None)
    assert "MR30110" in got.evidence


def test_no_parcel_field_at_all_is_unreadable_not_none():
    """A page with no cover-sheet field proves nothing about the recording."""
    assert read_cover_sheet_parcel("CERTIFICATE OF DEATH\nsome text\n").outcome == "unreadable"


def test_two_distinct_parcels_are_ambiguous():
    got = read_cover_sheet_parcel(DASHED.replace("1-2798-102-0005-003", "1-2798-102-0005-003, 101894020006036"))
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    assert got.candidates == ["127981020005003", "101894020006036"]


def test_the_same_parcel_twice_is_one_parcel():
    got = read_cover_sheet_parcel(PLAIN + "TAX PARCEL NUMBER(S)\n101894020006036\n")
    assert (got.outcome, got.parcel) == ("found", "101894020006036")


def test_a_number_of_another_shape_is_ambiguous_not_trimmed():
    got = read_cover_sheet_parcel(PLAIN.replace("101894020006036", "24173-0001-22"))
    assert (got.outcome, got.parcel) == ("ambiguous", None)


def test_numbers_on_one_line_are_never_joined_into_a_parcel():
    """Two short numbers separated by a space must not merge into one 15-digit value."""
    got = read_cover_sheet_parcel(PLAIN.replace("101894020006036", "1018940200 06036"))
    assert got.parcel is None


def test_the_parcel_must_agree_with_the_index_prefix():
    assert read_cover_sheet_parcel(DASHED, prefix="1279810").outcome == "found"
    got = read_cover_sheet_parcel(DASHED, prefix="1079810")
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    assert "disagrees" in got.evidence


def test_the_index_prefix_is_read_from_the_truncated_legal():
    assert index_prefix("Subdivision APPLEWOOD EST Lot 3 Block 5   Parcel: 1279810...") == "1279810"
    # From the raw summary too, with no legal keyword before it (Codex P1).
    assert index_prefix("HAY, JAMES LEROY 11/03/2026 Parcel: 1279810...") == "1279810"
    assert index_prefix("Lot 2   Parcel: 115802011127002 SHORT PLAT #1127") is None
    assert index_prefix(None) is None


# Codex adversarial review, round 1.

def test_a_second_parcel_on_an_additional_line_is_ambiguous():
    text = PLAIN.replace("AdditionalTaxAccountsareonpage ofdocument\n",
                         "Additional parcels: 127981020005003\n")
    got = read_cover_sheet_parcel(text)
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    assert got.candidates == ["101894020006036", "127981020005003"]


def test_a_second_parcel_after_a_blank_line_is_ambiguous():
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n\n127981020005003\n")
    assert got.outcome == "ambiguous"


def test_a_blank_line_then_the_instrument_number_still_ends_the_field():
    """The real 2026-027909 sheet prints its instrument number after a blank line."""
    assert read_cover_sheet_parcel(DASHED).outcome == "found"


def test_digits_glued_to_letters_are_not_a_parcel():
    got = read_cover_sheet_parcel(PLAIN.replace("101894020006036", "ABC101894020006036XYZ"))
    assert (got.outcome, got.parcel) == ("unreadable", None)


def test_a_parcel_beside_another_long_number_is_ambiguous():
    got = read_cover_sheet_parcel(PLAIN.replace("101894020006036", "101894020006036 12345678"))
    assert (got.outcome, got.parcel) == ("ambiguous", None)


def test_dashed_and_undashed_spellings_are_one_parcel():
    got = read_cover_sheet_parcel(DASHED + "TAX PARCEL NUMBER(S)\n127981020005003\n")
    assert (got.outcome, got.parcel) == ("found", "127981020005003")


# Codex adversarial review, round 2: a field is read to its end, never part of it.

def test_a_different_parcel_after_repeats_of_the_first_is_ambiguous():
    text = "TAX PARCEL NUMBER(S)\n" + "101894020006036\n" * 3 + "127981020005003\n"
    got = read_cover_sheet_parcel(text)
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    assert got.candidates == ["101894020006036", "127981020005003"]


def test_a_second_parcel_after_several_blank_lines_is_ambiguous():
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n\n\n\n\n127981020005003\n")
    assert got.outcome == "ambiguous"


def test_a_field_too_long_to_read_whole_never_yields_a_parcel():
    text = "TAX PARCEL NUMBER(S)\n101894020006036\n" + "continued value text\n" * 20
    got = read_cover_sheet_parcel(text)
    assert (got.outcome, got.parcel) == ("ambiguous", None)
