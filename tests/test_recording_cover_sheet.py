"""The parcel a Benton recording cover sheet names (Benton death certificates, run f1277c60).

Each case below is the cover sheet's parcel field exactly as the text layer of a real
Benton recorded image prints it (2026-11-15). Only the field is kept: the certificate
itself carries personal details that have no place in a fixture.
"""
from src.scrapers.sources.recording_cover_sheet import index_prefix, read_cover_sheet_parcel

# 2026-027909: dashed parcel on the line after the label. The page prints its own
# instrument number below it; the scraper passes that number so it is never read as a parcel.
HAY = "2026-027909"
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
    got = read_cover_sheet_parcel(DASHED, instrument=HAY)
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
    got = read_cover_sheet_parcel(DASHED.replace("1-2798-102-0005-003", "1-2798-102-0005-003, 101894020006036"), instrument=HAY)
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
    assert read_cover_sheet_parcel(DASHED, instrument=HAY, prefix="1279810").outcome == "found"
    got = read_cover_sheet_parcel(DASHED, instrument=HAY, prefix="1079810")
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
    assert read_cover_sheet_parcel(DASHED, instrument=HAY).outcome == "found"


def test_digits_glued_to_letters_are_not_a_parcel():
    """Never attached; a Benton-shaped number on the page sends it to review."""
    got = read_cover_sheet_parcel(PLAIN.replace("101894020006036", "ABC101894020006036XYZ"))
    assert (got.outcome, got.parcel) == ("ambiguous", None)


def test_a_parcel_beside_another_long_number_is_ambiguous():
    got = read_cover_sheet_parcel(PLAIN.replace("101894020006036", "101894020006036 12345678"))
    assert (got.outcome, got.parcel) == ("ambiguous", None)


def test_dashed_and_undashed_spellings_are_one_parcel():
    got = read_cover_sheet_parcel(DASHED + "TAX PARCEL NUMBER(S)\n127981020005003\n", instrument=HAY)
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


def test_a_field_written_as_n_a_names_no_parcel():
    """2026-023977's filer wrote "N/A" in the parcel field: blank, not unreadable."""
    for written in ("N/A", "n/a", "NONE", "-"):
        got = read_cover_sheet_parcel(f"ASSESSOR'S PROPERTY TAX PARCEL/ACCOUNT NUMBER:\n{written}\n")
        assert (got.outcome, got.parcel) == ("none", None), written
    # A real parcel beside it is still read.
    assert read_cover_sheet_parcel(DASHED, instrument=HAY).outcome == "found"


def test_a_blank_field_against_an_index_parcel_is_ambiguous_not_none():
    """Codex P2: "none" would allow an owner-name search; the index says there is a parcel."""
    for written in ("", "N/A"):
        got = read_cover_sheet_parcel(f"ASSESSOR'S PROPERTY TAX PARCEL/ACCOUNT NUMBER:\n{written}\n", prefix="1018920")
        assert got.outcome == "ambiguous", written


def test_a_field_of_blank_markers_too_long_to_read_is_never_none():
    """Codex round 2: a parcel past the read limit must not leave the field looking blank."""
    text = "TAX PARCEL NUMBER(S)\n" + "N/A\n" * 20 + "101894020006036\n"
    assert read_cover_sheet_parcel(text).outcome == "ambiguous"


def test_a_parcel_after_a_blank_line_and_an_n_a_is_still_seen():
    """Codex round 3: N/A, a blank line, N/A, then a parcel: the parcel is the field's."""
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n\nN/A\n101894020006036\n")
    assert (got.outcome, got.parcel) == ("found", "101894020006036")


def test_a_number_of_another_shape_after_a_gap_is_ambiguous_not_none():
    """Codex round 4: the field holds a parcel-shaped value; it is not blank."""
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n\nN/A\n201894020006036\n")
    assert got.outcome == "ambiguous"


def test_a_numbered_field_after_the_parcel_is_another_field():
    """Codex round 5: "Reference Number: 2025027909" ends the parcel field."""
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n\nReference Number: 2025027909\n")
    assert (got.outcome, got.parcel) == ("found", "101894020006036")
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\nReference Number: 2025027909\n")
    assert (got.outcome, got.parcel) == ("found", "101894020006036")


def test_none_needs_proof_that_no_parcel_follows_the_field():
    """Codex round 6: a wrapped "Additional parcels:" heading must not hide a parcel."""
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n\nAdditional parcels:\n101894020006036\n")
    assert got.outcome == "ambiguous"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\nAdditional parcels:\n101894020006036\n")
    assert got.outcome == "ambiguous"
    # The real blank sheet (no number after the field) is still "none".
    assert read_cover_sheet_parcel(BLANK).outcome == "none"


def test_a_value_glued_to_its_label_is_read():
    """Codex round 7: the text layer can join label and value with no separator."""
    got = read_cover_sheet_parcel("Assessor's15-DigitParcelNumber101894020006036\nGrantor: X\n")
    assert (got.outcome, got.parcel) == ("found", "101894020006036")


def test_a_parcel_wrapped_over_two_lines_never_reads_as_none():
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n\n1-2798-102-\n0005-003\n")
    assert got.outcome == "ambiguous"


def test_a_glued_continuation_parcel_never_reads_as_none():
    """Codex round 8."""
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n\nAdditionalParcelNumber101894020006036\n")
    assert got.outcome == "ambiguous"


def test_unicode_dashes_are_parcel_hyphens():
    """Codex round 9: en dashes from the text layer."""
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n1\u20132798\u2013102\u20130005\u2013003\n")
    assert (got.outcome, got.parcel) == ("found", "127981020005003")
    assert read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n\n1\u20132798\u2013102\u20130005\u2013003\n").outcome != "none"


# Codex final review: a parcel anywhere else on the page decides "exactly one" and "none".

def test_a_second_parcel_after_a_continuation_heading_is_never_dropped():
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\nAdditional parcels:\n101894020006037\n")
    assert (got.outcome, got.parcel) == ("ambiguous", None)


def test_a_parcel_far_below_a_blank_field_still_blocks_none():
    text = "TAX PARCEL NUMBER(S)\nN/A\n\nAdditional parcels:\n\n\n\n\n101894020006036\n"
    assert read_cover_sheet_parcel(text).outcome == "ambiguous"


def test_the_instrument_number_on_the_page_is_not_a_parcel():
    """The real 2026-027909 sheet prints its instrument number below the parcel."""
    assert read_cover_sheet_parcel(DASHED, instrument=HAY).outcome == "found"


def test_a_parcel_wrapped_over_two_lines_elsewhere_on_the_page_is_seen():
    """Codex final review: found must not survive a wrapped second parcel; none must not either."""
    wrapped = "Legal Description: LOT 1\nAdditional parcels:\n1-2798-102-\n0005-003\n"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n" + wrapped)
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    blank = "TAX PARCEL NUMBER(S)\nN/A\n" + "\n" * 8 + wrapped
    assert read_cover_sheet_parcel(blank).outcome == "ambiguous"


def test_a_wrapped_parcel_beside_another_number_line_is_still_seen():
    """Codex: '1-2798-102-' / '0005-003' / '2026-027909' must not merge into one bad run."""
    tail = "Legal Description: LOT 1\nAdditional parcels:\n1-2798-102-\n0005-003\n2026-027909\n"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n" + tail)
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    assert read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n" + "\n" * 8 + tail).outcome == "ambiguous"


def test_a_parcel_wrapped_over_four_lines_is_still_seen():
    """Codex: '1-2798-' / '102-' / '0005-' / '003' after the legal description."""
    tail = "Legal Description: LOT 1\n" + "\n" * 7 + "Additional parcels:\n1-2798-\n102-\n0005-\n003\n"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n" + tail)
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    assert read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n" + tail).outcome == "ambiguous"


def test_a_wrapped_parcel_with_spaces_and_blank_lines_is_still_seen():
    """Codex: 'Additional parcels: 1-2798- 102-' / blank / '0005-' / '-' / '003'."""
    tail = "Legal Description: LOT 1\n" + "\n" * 7 + "Additional parcels: 1-2798- 102-\n\n0005-\n-\n003\n"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n" + tail)
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    assert read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n" + tail).outcome == "ambiguous"


def test_a_wrapped_parcel_joined_to_a_neighbouring_digit_is_still_seen():
    """Codex: 'Lot 7 12798102' / '0005003, Block A' joins to a 16-digit run."""
    tail = "Legal Description: Lot 7 12798102\n0005003, Block A\n"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n" + tail)
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    assert read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n" + "\n" * 8 + tail).outcome == "ambiguous"


def test_an_instrument_shaped_piece_of_a_parcel_is_not_stripped():
    """Codex: '1-' / '27981020-005003' holds '1020-005003', which is not an instrument here."""
    tail = "Legal Description: LOT 1\n1-\n27981020-005003\n"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n" + tail)
    assert (got.outcome, got.parcel) == ("ambiguous", None)


def test_a_wrap_whose_second_line_looks_like_an_instrument_is_still_seen():
    """Codex: '12798' / '1020-005003' is a parcel, not an instrument number."""
    tail = "Legal Description: LOT 1\n12798\n1020-005003\n"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n" + tail)
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    assert read_cover_sheet_parcel("Tax Parcel Number:\n" + "\n" * 8 + tail).outcome == "ambiguous"


def test_a_space_grouped_parcel_elsewhere_on_the_page_is_seen():
    """Codex: '1018 9402 0006 036' under the legal description."""
    page = "Assessor's Parcel Number:\nLegal Description:\n1018 9402 0006 036\n"
    assert read_cover_sheet_parcel(page).outcome == "ambiguous"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n127981020005003\n" + page)
    assert (got.outcome, got.parcel) == ("ambiguous", None)


def test_the_page_is_read_as_one_digit_stream():
    """Codex round 12: a second parcel sharing a line with the first, and a '--' wrap."""
    got = read_cover_sheet_parcel(
        "TAX PARCEL NUMBER(S)\n127981020005003\nAdditional parcels: 127981020005003, 1018\n9402 0006 036\n")
    assert (got.outcome, got.parcel) == ("ambiguous", None)
    tail = "Legal Description: LOT 1\n" + "\n" * 8 + "Additional parcels: 1-2798-102-\n-0005-003\n"
    assert read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n" + tail).outcome == "ambiguous"


def test_the_own_instrument_number_inside_a_parcel_is_kept():
    """Codex: '1-2026-027909-7890' contains the instrument but is a parcel-shaped number."""
    tail = "Legal Description: LOT 1\n" + "\n" * 8 + "Additional parcel: 1-2026-027909-7890\n"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n" + tail, instrument=HAY)
    assert got.outcome == "ambiguous"
    got = read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\n101894020006036\n" + tail, instrument=HAY)
    assert (got.outcome, got.parcel) == ("ambiguous", None)


def test_the_own_instrument_number_is_removed_only_when_it_fills_a_line():
    """Codex: '1- 2026-027909 -7890' keeps its digits; a line holding only the number is dropped."""
    tail = "Legal Description: LOT 1\n" + "\n" * 8 + "Additional parcel: 1- 2026-027909 -7890\n"
    assert read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n" + tail, instrument=HAY).outcome == "ambiguous"
    assert read_cover_sheet_parcel(DASHED, instrument=HAY).outcome == "found"


def test_an_ocr_garbled_parcel_elsewhere_on_the_page_is_not_no_parcel():
    """Codex: '1-2798-1O2-0005-003' (letter O) below the legal description."""
    tail = "Legal Description: LOT 1\n" + "\n" * 8 + "Additional parcel: 1-2798-1O2-0005-003\n"
    assert read_cover_sheet_parcel("TAX PARCEL NUMBER(S)\nN/A\n" + tail).outcome == "ambiguous"



def test_n_a_after_separator_punctuation_is_no_parcel():
    """2026-028804's filer wrote 'Assessor's Tax Parcel No .: N/A'."""
    got = read_cover_sheet_parcel("Assessor's Tax Parcel No .: N/A\nGrantor: X\n")
    assert (got.outcome, got.parcel) == ("none", None)
