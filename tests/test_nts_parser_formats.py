"""REAL Snohomish County Tribune trustee sales the parser dropped (issue 2026-09-09).

The "Legals 9-9-26" PDF split into 10 blocks but only 7 were valid. Three real sales
were lost, for three different reasons:

1. Affinia Default Services, no-colon header, NO trustee sale number. The Tribune was
   parsed with the plain colon parser, which has no surrogate identity, so the notice
   had no ts_number. King already handled this layout (parse_king_notice).
2. Burns Law, PLLC "NOTICE OF TRUSTEE'S SALE OF COMMERCIAL LOAN". The splitter cut the
   ONE notice in two at its own section title "I. NOTICE OF TRUSTEE'S SALE", leaving a
   header half with the identity (no sale sentence) and a body half with the sale
   sentence (no identity).
3. That body half's sale sentence reads "will on September 18, 2026, at the hour of
   10:00 o'clock a.m." -- the "o'clock" defeated every auction pattern.

Fixtures are the real published PDFs (tests/fixtures/nts_snoho_tribune_*.pdf) and
verbatim excerpts from them. No mocks.
"""
from decimal import Decimal
from functools import cache
from pathlib import Path

from src.scrapers.sources import nts_pdf
from src.scrapers.sources.nts_king_pdf import parse_king_notice
from src.scrapers.sources.nts_tacoma_index import is_valid_nts, parse_nts_notice

_FX = Path(__file__).parent / "fixtures"
_ISSUE_0909 = _FX / "nts_snoho_tribune_2026-09-09.pdf"
_ISSUE_1217 = _FX / "nts_snoho_tribune_2025-12-17.pdf"


@cache
def _blocks(pdf: Path) -> tuple[str, ...]:
    text = nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(pdf.read_bytes()))
    return tuple(nts_pdf.split_notice_blocks(text))


def _block_with(pdf: Path, needle: str) -> str:
    hits = [b for b in _blocks(pdf) if needle in b]
    assert len(hits) == 1, f"expected exactly one block containing {needle!r}, got {len(hits)}"
    return hits[0]


# ── Issue-level coverage ──────────────────────────────────────────────────────

def test_every_notice_in_the_0909_issue_is_valid():
    blocks = _blocks(_ISSUE_0909)
    # 10 before the fix: the commercial notice was counted twice (header + body halves).
    assert len(blocks) == 9
    parsed = [parse_king_notice(b) for b in blocks]
    assert all(is_valid_nts(p) for p in parsed)
    ts_numbers = [p["ts_number"] for p in parsed]
    assert len(set(ts_numbers)) == len(ts_numbers)  # no two notices share an identity


def test_section_title_does_not_split_the_commercial_notice():
    for b in _blocks(_ISSUE_0909):
        assert not b.startswith("NOTICE OF TRUSTEE'S SALE NOTICE IS HEREBY GIVEN")
    block = _block_with(_ISSUE_0909, "OF COMMERCIAL LOAN")
    # The identity header and the sale sentence are back in ONE block.
    assert "REFERENCE NO. (DOT): 202211100430" in block
    assert "I. NOTICE OF TRUSTEE'S SALE NOTICE IS HEREBY GIVEN" in block
    assert "will on September 18, 2026, at the hour of 10:00 o'clock a.m." in block


# ── 1. Affinia, no TS number ──────────────────────────────────────────────────

def test_affinia_block_parses_with_deed_reference_identity():
    block = _block_with(_ISSUE_0909, "Affinia Default Services, LLC Current Mortgage Servicer")
    # The plain colon parser (what the Tribune used before) cannot identify it.
    assert not is_valid_nts(parse_nts_notice(block))

    p = parse_king_notice(block)
    assert is_valid_nts(p)
    assert p["ts_number"] == "REF-202411260448"  # "Deed of Trust Recording Number (Ref. #)"
    assert p["auction_date"] == "10/09/2026"
    assert p["auction_time"] == "10:00 AM"
    assert p["parcel"] == "00623700006200"
    assert p["property_address"] == "12725 218th Pl. SE, SNOHOMISH, WA 98296"
    assert p["grantor"] == "VIRGINIA TURNER AND JORDAN MURRAY"
    assert p["beneficiary"] == "Freedom Mortgage Corporation"
    assert p["trustee"] == "Affinia Default Services, LLC"
    assert p["principal_owing"] == Decimal("521130.51")


# ── 2. Commercial loan (Burns Law), only what the notice prints ─────────────

def test_commercial_loan_notice_parses_only_printed_fields():
    block = _block_with(_ISSUE_0909, "OF COMMERCIAL LOAN")
    p = parse_king_notice(block)
    assert is_valid_nts(p)
    # No trustee sale number is printed ("BL #32692" is the firm's file number, not
    # one), so identity is the deed of trust recording number.
    assert p["ts_number"] == "REF-202211100430"
    assert p["auction_date"] == "September 18, 2026"
    assert p["auction_time"] == "10:00 AM"
    assert p["parcel"] == "31043400401900"  # "PARCEL NO(S).: 31043400401900"
    assert p["property_address"] == "14510 S Lake Crabapple Rd, Marysville, WA 98271-7921"
    assert p["grantor"] == "Julie Anderson, a single woman as her separate estate"
    assert p["beneficiary"] == "Viet A. Betts, a single person"
    assert p["trustee"] == "Burns Law, PLLC"
    # Section IV: "The sum owing on the obligation secured by the Deed of Trust is:
    # $438,117.67 in principal, interest and late fees".
    assert p["principal_owing"] == Decimal("438117.67")


def test_commercial_override_does_not_fire_on_other_layouts():
    for b in _blocks(_ISSUE_0909):
        if "OF COMMERCIAL LOAN" in b:
            continue
        shared, pdf = parse_nts_notice(b), parse_king_notice(b)
        if shared["ts_number"]:  # notices with their own TS number are untouched
            assert pdf == shared


# ── 3. Month-name date with "o'clock", no ordinal ─────────────────────────────

_OCLOCK_SENTENCE = (
    "NOTICE IS HEREBY GIVEN that the undersigned trustee will on September 18, 2026, at "
    "the hour of 10:00 o'clock a.m., outside the main entrance of the Snohomish County "
    "Courthouse, 3000 Rockefeller Avenue, Everett, WA 98201 to sell at public auction to "
    "the highest and best bidder, payable at time of sale"
)


def test_month_name_oclock_sale_date_is_read():
    p = parse_nts_notice(_OCLOCK_SENTENCE)
    assert p["auction_date"] == "September 18, 2026"
    assert p["auction_time"] == "10:00 AM"


def test_non_sale_dates_are_not_read_as_the_auction():
    # Verbatim sentences from the same notice: a recording date, the section V sale
    # restatement and the cure-by date. None hangs off "will on", so none is the sale.
    text = (
        "which is subject to that certain Deed of Trust and Promissory Note dated November "
        "7, 2022, and recorded on November 10, 2022 under Snohomish County Auditor No. "
        "202211100430, from Julie Anderson, as Grantor, to Viet A. Betts, a single person, "
        "Beneficiary. The sale will be made without warranty, express or implied, regarding "
        "title, possession, or encumbrances on the 18th day of September, 2026. The "
        "default(s) referred to in paragraph III must be cured by the 7th day of September, "
        "2026 (11 days before the sale date) to cause a discontinuance of the sale. "
        "to sell at public auction to the highest and best bidder"
    )
    assert parse_nts_notice(text)["auction_date"] is None


def test_real_block_without_its_sale_sentence_has_no_auction_date():
    block = _block_with(_ISSUE_0909, "OF COMMERCIAL LOAN")
    sale = "will on September 18, 2026, at the hour of 10:00 o'clock a.m.,"
    assert sale in block
    # The rest of the 45k-char block is full of other dates; none may stand in.
    assert parse_nts_notice(block.replace(sale, "will,"))["auction_date"] is None


# ── Surrogate identity: stable, never colliding ──────────────────────────────

def test_surrogate_identity_is_stable_across_reparses():
    _blocks.cache_clear()
    first = {parse_king_notice(b)["ts_number"] for b in _blocks(_ISSUE_0909)}
    _blocks.cache_clear()
    second = {parse_king_notice(b)["ts_number"] for b in _blocks(_ISSUE_0909)}
    assert first == second
    assert {"REF-202411260448", "REF-202211100430"} <= first


def test_different_recording_numbers_never_collide():
    affinia = parse_king_notice(_block_with(_ISSUE_0909, "Affinia Default Services, LLC Current"))
    commercial = parse_king_notice(_block_with(_ISSUE_0909, "OF COMMERCIAL LOAN"))
    assert affinia["ts_number"] != commercial["ts_number"]


def test_word_captured_as_deed_reference_is_never_a_key():
    # "Reference number of the deed of trust: Auditor's File No. 202303210039." The
    # shared regex reads the reference as the word "Auditor"; REF-Auditor would merge
    # every notice printed this way under one key.
    block = _block_with(_ISSUE_1217, "Randy A. Lindquist")
    p = parse_king_notice(block)
    assert parse_nts_notice(block)["deed_reference"] == "Auditor"
    assert p["ts_number"] == "REF-202303210039"
    # Both colon regexes ran to the same first colon; the misread is blanked, not kept.
    assert p["grantor"] is None and p["beneficiary"] is None
