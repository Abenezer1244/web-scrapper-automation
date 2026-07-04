"""Parser tests for the Tacoma Daily Index NTS parser.

Runs against a REAL saved notice (tests/fixtures/nts_tacoma_25-76127.txt — Pierce
County NTS, fetched 2026-06-12), per the no-mocks rule. Pins the investor-critical
field extraction.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.scrapers.sources.nts_tacoma_index import (
    _to_date,
    collect_notice_urls,
    extract_article_text,
    extract_notice_urls,
    is_valid_nts,
    listing_has_entries,
    notice_to_row,
    parse_nts_notice,
    parse_tacoma_notice,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "nts_tacoma_25-76127.txt"


def _load() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


class TestParseRealNotice:
    def setup_method(self):
        self.p = parse_nts_notice(_load())

    def test_ts_number(self):
        assert self.p["ts_number"] == "25-76127"

    def test_auction_date_time_location(self):
        assert self.p["auction_date"] == "7/10/2026"
        assert self.p["auction_time"] == "10:00 AM"
        assert "Pierce County Courthouse" in self.p["auction_location"]

    def test_parties(self):
        assert "SPURLING" in self.p["grantor"]
        assert self.p["trustee"] == "North Star Trustee, LLC"
        assert self.p["beneficiary"] == "Freedom Mortgage Corporation"

    def test_parcel(self):
        assert self.p["parcel"] == "051928-5029"

    def test_property_address(self):
        addr = self.p["property_address"]
        assert "19012 160TH ST EAST" in addr
        assert "BONNEY LAKE" in addr
        assert "98391" in addr
        assert "WA" in addr  # WASHINGTON normalized to WA

    def test_default_and_note_amounts(self):
        assert self.p["principal_owing"] == Decimal("185895.06")  # section IV sum owing
        assert self.p["note_amount"] == Decimal("234533.00")      # original loan

    def test_deed_reference_and_nod_date(self):
        assert self.p["deed_reference"] == "200907160286"
        assert self.p["nod_date"] == "1/20/2026"

    def test_is_valid(self):
        assert is_valid_nts(self.p) is True


class TestValidationGuards:
    def test_empty_text_invalid(self):
        assert is_valid_nts(parse_nts_notice("")) is False

    def test_site_chrome_invalid(self):
        chrome = "Home News Legal Notices Subscribe Weather Contact About Us"
        assert is_valid_nts(parse_nts_notice(chrome)) is False

    def test_missing_auction_date_invalid(self):
        # has a TS# but no auction clause -> not usable
        assert is_valid_nts(parse_nts_notice("TS #: 99-00000\nGrantor: X")) is False

    def test_curly_apostrophe_normalized(self):
        # CMS emits curly quotes in "TRUSTEE’S SALE"; must not break labels
        body = "TS #: 25-0001\nwill on 8/1/2026, at 9:00 AM at Courthouse sell at public auction"
        p = parse_nts_notice(body)
        assert p["ts_number"] == "25-0001" and p["auction_date"] == "8/1/2026"

    def test_money_parsing_handles_commas(self):
        body = "Note Amount: $1,234,567.89"
        assert parse_nts_notice(body)["note_amount"] == Decimal("1234567.89")


class TestTrusteeFormatVariety:
    """Codex review: parser must survive the layout variance across WA trustees."""

    def test_ts_number_prefixed_format(self):
        # Quality Loan / Clear Recon style prefixed sale numbers
        for raw, want in (
            ("TS #: WA-25-123456", "WA-25-123456"),
            ("T.S. No.: 25-76127", "25-76127"),
            ("Trustee Sale No.: F25-1234-WA", "F25-1234-WA"),
        ):
            body = f"{raw}\nwill on 8/1/2026, at 9:00 AM at Courthouse sell at public auction"
            assert parse_nts_notice(body)["ts_number"] == want

    def test_ts_number_not_polluted_by_title_suffix(self):
        body = "TS #: 25-76127-NOTICE OF TRUSTEE'S SALE\nTS #: 25-76127"
        assert parse_nts_notice(body)["ts_number"] == "25-76127"

    def test_dotted_ampm_auction_time(self):
        for t in ("10:00 A.M.", "10:00 a.m.", "9:30 AM"):
            body = f"TS #: 25-1\nwill on 7/1/2026, at {t} at Pierce County Courthouse sell at public auction"
            p = parse_nts_notice(body)
            assert p["auction_date"] == "7/1/2026", t
            assert is_valid_nts(p), t

    def test_trustee_sale_number_line_not_read_as_trustee(self):
        body = (
            "Trustee Sale No.: 25-99999\n"
            "Current trustee of the deed of trust: Quality Loan Service Corp of WA\n"
            "will on 7/1/2026, at 9:00 AM at Courthouse sell at public auction"
        )
        assert parse_nts_notice(body)["trustee"] == "Quality Loan Service Corp of WA"

    def test_address_unit_prefix_preserved(self):
        body = "Commonly known as: UNIT B 123 MAIN ST\nTACOMA, WASHINGTON 98402 which is subject to"
        addr = parse_nts_notice(body)["property_address"]
        assert addr.startswith("UNIT B 123 MAIN ST")
        assert "which is subject" not in addr

    def test_address_same_line_deed_text_excluded(self):
        body = "Commonly known as: 5 OAK AVE, TACOMA, WA 98402 which is subject to that certain Deed of Trust dated 1/1/2020"
        addr = parse_nts_notice(body)["property_address"]
        assert "Deed of Trust" not in addr and "which is subject" not in addr
        assert "5 OAK AVE" in addr


class TestCrawlExtraction:
    def test_extract_notice_urls_filters_and_dedupes(self):
        listing = '''
        <a href="https://www.tacomadailyindex.com/2026/06/05/ts-25-76127-notice-of-trustees-sale/">x</a>
        <a href="https://www.tacomadailyindex.com/2026/06/05/ts-25-76127-notice-of-trustees-sale/">dup</a>
        <a href="https://www.tacomadailyindex.com/2026/06/04/jane-doe-name-change/">not nts</a>
        <a href="https://www.tacomadailyindex.com/2026/06/03/ts-25-99999-notice-of-trustees-sale/">y</a>
        '''
        urls = extract_notice_urls(listing)
        assert len(urls) == 2  # deduped + non-NTS filtered out
        assert urls[0].endswith("ts-25-76127-notice-of-trustees-sale/")
        assert all("notice-of-trustee" in u for u in urls)

    def test_extract_notice_urls_current_idx_slug(self):
        # The CURRENT live Tacoma Daily Index slug — /ts-<wa-NN-NNNNN>-...-idx<N>/ —
        # carries NO 'notice-of-trustee' text. The pre-#36 regex required that text,
        # so this listing yielded ZERO URLs and the prod crawler upserted 0 rows.
        # Lock the current format in so the 0-rows regression can't return.
        listing = '''
        <a href="https://www.tacomadailyindex.com/2026/06/12/ts-wa-26-1033982-rm-idx1031786/">a</a>
        <a href="https://www.tacomadailyindex.com/2026/06/12/no-26-4-04376-8-sea-probate-notice-to-creditors-idx-1032180/">probate</a>
        <a href="https://www.tacomadailyindex.com/2026/06/12/ts-wa-25-1032618-rm-idx1031750/">b</a>
        '''
        urls = extract_notice_urls(listing)
        assert len(urls) == 2  # both ts- notices; the probate notice filtered out
        assert urls[0].endswith("ts-wa-26-1033982-rm-idx1031786/")
        assert all("/ts-wa-" in u for u in urls)

    def test_extract_notice_urls_rejects_offsite_host(self):
        # a syndicated/compromised link with an NTS-shaped path on another host
        # must NOT be crawled (Codex P2 host-pin)
        listing = (
            '<a href="https://evil.example.com/2026/06/05/ts-25-1-notice-of-trustees-sale/">x</a>'
            '<a href="https://www.tacomadailyindex.com/2026/06/05/ts-25-2-notice-of-trustees-sale/">ok</a>'
        )
        urls = extract_notice_urls(listing)
        assert len(urls) == 1
        assert "tacomadailyindex.com" in urls[0]

    def test_extract_article_text_strips_chrome(self):
        html = "<html><nav>Menu</nav><article><p>TS #: 25-1</p><script>junk()</script><p>Body</p></article><footer>f</footer></html>"
        text = extract_article_text(html)
        assert "TS #: 25-1" in text and "Body" in text
        assert "junk()" not in text and "Menu" not in text


def _dated(slug: str) -> str:
    return f'<a href="https://www.tacomadailyindex.com/2026/07/02/{slug}/">x</a>'


def _ts(n: str) -> str:
    # a real current-format trustee-sale slug
    return _dated(f"ts-wa-26-{n}-rm-idx{n}")


def _probate(n: str) -> str:
    return _dated(f"no-26-4-{n}-probate-notice-to-creditors-idx{n}")


class TestListingHasEntries:
    def test_page_with_dated_articles(self):
        assert listing_has_entries(_probate("01") + _ts("100")) is True

    def test_empty_page(self):
        # site chrome only, no dated article links -> past the end of the listing
        assert listing_has_entries("<html><nav>Menu</nav><footer>f</footer></html>") is False

    def test_offsite_dated_link_does_not_count(self):
        assert listing_has_entries('<a href="https://evil.com/2026/07/02/ts-x/">x</a>') is False


class TestCollectNoticeUrls:
    """Bug A regression: NTS notices sit BEHIND probate/bids on later listing pages.
    The old crawler broke at the first page with no NTS, harvesting nothing on the
    (common) days page 1 was all probate. collect_notice_urls must scan through."""

    def _fetcher(self, pages: dict[int, str]):
        # a real in-memory fetcher (no mock framework): page -> (status, html).
        # a page absent from the dict is treated as past-the-end (404-style).
        def fetch(page: int):
            if page in pages:
                return (200, pages[page])
            return (404, "")
        return fetch

    def test_scans_past_pages_with_zero_nts(self):
        # page1 = all probate (0 NTS), page2 = 1 NTS, page3 = 0 NTS, page4 = 5 NTS.
        # Mirrors the live 2026-07-02 listing where the old code harvested 0.
        pages = {
            1: _probate("01") + _probate("02") + _probate("03"),
            2: _probate("04") + _ts("200"),
            3: _probate("05") + _probate("06"),
            4: _ts("400") + _ts("401") + _ts("402") + _ts("403") + _ts("404"),
        }
        urls = collect_notice_urls(self._fetcher(pages), max_pages=10, max_notices=200)
        assert len(urls) == 6  # 1 from page2 + 5 from page4 — NONE lost to the page1 break
        assert any("ts-wa-26-200" in u for u in urls)
        assert sum("ts-wa-26-40" in u for u in urls) == 5

    def test_stops_at_end_of_listing_empty_page(self):
        # page3 has no dated links at all -> genuine end; page4 (would have NTS) not reached.
        pages = {
            1: _ts("100"),
            2: _ts("200"),
            3: "<html><nav>Menu</nav></html>",  # empty listing page (past the end)
            4: _ts("400"),
        }
        urls = collect_notice_urls(self._fetcher(pages), max_pages=10, max_notices=200)
        assert len(urls) == 2
        assert not any("ts-wa-26-400" in u for u in urls)

    def test_stops_on_non_200(self):
        pages = {1: _ts("100"), 2: _ts("200")}  # page 3+ -> 404 via fetcher default
        urls = collect_notice_urls(self._fetcher(pages), max_pages=10, max_notices=200)
        assert len(urls) == 2

    def test_transient_failure_skips_page_but_continues(self):
        # page2 fetch fails (None) -> we must still reach page3's NTS, not abandon.
        pages = {1: _probate("01"), 3: _ts("300")}

        def fetch(page: int):
            if page == 2:
                return None  # transient failure
            if page in pages:
                return (200, pages[page])
            return (404, "")

        urls = collect_notice_urls(fetch, max_pages=10, max_notices=200)
        assert len(urls) == 1
        assert "ts-wa-26-300" in urls[0]

    def test_respects_max_notices_cap(self):
        pages = {1: "".join(_ts(f"1{i:02d}") for i in range(10))}
        urls = collect_notice_urls(self._fetcher(pages), max_pages=10, max_notices=3)
        assert len(urls) == 3

    def test_respects_max_pages(self):
        pages = {p: _ts(f"{p}00") for p in range(1, 20)}
        urls = collect_notice_urls(self._fetcher(pages), max_pages=4, max_notices=200)
        assert len(urls) == 4  # only pages 1-4 walked


class TestSurrogateTsNumberAndWordedDate:
    """Bug B regression (real notices, live 2026-06-26): trustees that print NO
    'Trustee Sale No.' (Clear Recon 'In re …') and older law-firm notices using an
    ORDINAL worded auction date ('17th day of July, 2026') + 'Tax Parcel No' labels
    were dropped whole. parse_tacoma_notice must recover them (surrogate ts# + worded
    date + parcel labels) WITHOUT changing any real-TS# notice."""

    def _fx(self, name: str) -> str:
        return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")

    def test_clearrecon_no_tsnum_recovered_via_ref_surrogate(self):
        # $661,481.84 Clear Recon sale — no TS# label; deed reference -> REF- surrogate.
        p = parse_tacoma_notice(self._fx("nts_tacoma_clearrecon_no_tsnum.txt"))
        assert p["ts_number"] == "REF-202204180696"
        assert p["auction_date"] == "7/31/2026"
        assert p["parcel"] == "9260000622"
        assert p["principal_owing"] == Decimal("661481.84")
        assert is_valid_nts(p) is True
        row = notice_to_row(p, "http://x/", today=date(2026, 7, 2))
        assert row is not None and row["is_active"] is True
        assert row["auction_date"] == date(2026, 7, 31)

    def test_worded_ordinal_date_tax_parcel_nos(self):
        # "will on the 17th day of July, 2026 ... Tax Parcel Nos: 031715-3007, ..."
        p = parse_tacoma_notice(self._fx("nts_tacoma_worded_date_a.txt"))
        assert p["auction_date"] == "July 17, 2026"
        assert _to_date(p["auction_date"]) == date(2026, 7, 17)
        assert p["parcel"] == "031715-3007"       # first of the multi-parcel list
        assert p["ts_number"] == "APN-031715-3007"  # no deed ref -> APN surrogate
        assert is_valid_nts(p) is True

    def test_worded_ordinal_date_assessor_tax_parcel(self):
        # "will on the 24th day of July 2026 ... ASSESSOR'S TAX PARCEL NO.: 031713-2-024"
        p = parse_tacoma_notice(self._fx("nts_tacoma_worded_date_b.txt"))
        assert _to_date(p["auction_date"]) == date(2026, 7, 24)
        assert p["parcel"] == "031713-2-024"
        assert p["ts_number"] == "APN-031713-2-024"
        assert is_valid_nts(p) is True

    def test_tax_parcel_line_not_leaked_into_address(self):
        # Codex P2: "Commonly known as: <addr> Tax Parcel Nos.: <apn> which is subject…"
        # must NOT leak the parcel line into property_address / the normalized match key,
        # or the notice can't attach to a lead by address.
        p = parse_tacoma_notice(self._fx("nts_tacoma_worded_date_b.txt"))
        assert p["property_address"] == "4122 320th Street East, Eatonville, WA 98328"
        assert "Tax Parcel" not in (p["property_address"] or "")
        row = notice_to_row(p, "http://x/", today=date(2026, 7, 2))
        assert "PARCEL" not in (row["property_address_normalized"] or "").upper()

    def test_wrapper_does_not_alter_real_tsnumber_notices(self):
        # The two existing REAL fixtures carry real TS#s — the wrapper must be a no-op
        # (identical dict) so the surrogate path never rewrites a genuine trustee id.
        for fn in ("nts_tacoma_25-76127.txt", "nts_tacoma_quality_loan.txt"):
            text = self._fx(fn)
            assert parse_tacoma_notice(text) == parse_nts_notice(text)
            assert not parse_tacoma_notice(text)["ts_number"].startswith(("REF-", "APN-"))

    def test_tax_parcel_label_does_not_break_numeric_parcel_label(self):
        # The widened _PARCEL alternation must still parse the plain "Parcel Number(s):".
        assert parse_nts_notice(
            "Parcel Number(s): 5005002880\nwill on 7/1/2026, at 9:00 AM at X sell at public auction"
        )["parcel"] == "5005002880"

    def test_worded_date_not_used_when_numeric_present(self):
        # A numeric-format notice must NOT be re-parsed by the worded fallback (gated on
        # am is None) — behavior byte-identical for the dominant Tacoma format.
        p = parse_nts_notice(
            "TS #: 25-1\nwill on 7/10/2026, at 10:00 AM at Courthouse sell at public auction"
        )
        assert p["auction_date"] == "7/10/2026"


class TestNoticeToRow:
    def _parsed(self):
        return parse_nts_notice((Path(__file__).parent / "fixtures" / "nts_tacoma_25-76127.txt").read_text(encoding="utf-8"))

    def test_row_fields_and_normalized_address(self):
        # today BEFORE the 7/10/2026 auction -> active
        row = notice_to_row(self._parsed(), "http://x/ts-25-76127/", today=date(2026, 6, 12))
        assert row["source"] == "tacoma_daily_index"
        assert row["ts_number"] == "25-76127"
        assert row["county"] == "pierce" and row["state"] == "WA"
        assert row["auction_date"] == date(2026, 7, 10)
        assert row["is_active"] is True
        assert row["property_address_normalized"] is not None
        assert "98391" in row["property_address_normalized"]
        assert len(row["raw_hash"]) == 64
        assert row["source_url"] == "http://x/ts-25-76127/"

    def test_past_auction_is_inactive(self):
        # today AFTER the auction -> not matched (kept for audit)
        row = notice_to_row(self._parsed(), "http://x/", today=date(2026, 8, 1))
        assert row["is_active"] is False

    def test_invalid_notice_returns_none(self):
        assert notice_to_row(parse_nts_notice("site chrome only"), "http://x/", today=date(2026, 6, 12)) is None

    def test_raw_hash_stable_for_same_content(self):
        p = self._parsed()
        a = notice_to_row(p, "http://x/", today=date(2026, 6, 12))["raw_hash"]
        b = notice_to_row(p, "http://different-url/", today=date(2026, 6, 12))["raw_hash"]
        assert a == b  # hash is over content, not URL


class TestNoticeToRowFieldSafety:
    """2026-07-01: a live King notice was LOST to a varchar(512) INSERT error when a
    layout drift made the colon parser capture 810 chars of boilerplate. notice_to_row
    is the shared chokepoint: display fields are clamped to their column widths,
    identity fields (ts_number, parcel) are NEVER truncated — an overlong ts_number
    skips the row, an overlong parcel is nulled (a truncated key could false-match)."""

    def _parsed(self):
        return parse_nts_notice(_FIXTURE.read_text(encoding="utf-8"))

    def test_display_fields_clamped_to_column_widths(self):
        p = dict(self._parsed())
        p["grantor"] = "G" * 900
        p["beneficiary"] = "B" * 900
        p["trustee"] = "T" * 900
        p["auction_location"] = "L" * 900
        p["property_address"] = "A" * 900
        row = notice_to_row(p, "http://x/", today=date(2026, 6, 12))
        assert row is not None  # the notice SURVIVES — that is the whole point
        assert len(row["grantor"]) == 512
        assert len(row["beneficiary"]) == 255
        assert len(row["trustee"]) == 255
        assert len(row["auction_location"]) == 512
        assert len(row["property_address"]) == 512
        assert len(row["property_address_normalized"] or "") <= 512

    def test_overlong_parcel_nulled_not_truncated(self):
        p = dict(self._parsed())
        p["parcel"] = "1" * 80
        row = notice_to_row(p, "http://x/", today=date(2026, 6, 12))
        assert row["parcel"] is None

    def test_overlong_ts_number_skips_row(self):
        p = dict(self._parsed())
        p["ts_number"] = "X" * 65
        assert notice_to_row(p, "http://x/", today=date(2026, 6, 12)) is None

    def test_identical_party_blob_nulled(self):
        # Colon regexes on a no-colon layout scan to the same first colon (inside
        # "10:00 AM") and assign ONE boilerplate blob to grantor/beneficiary/servicer.
        p = dict(self._parsed())
        blob = "00 AM sell at public auction located At the 4th Ave. entrance " * 3
        p["grantor"] = p["beneficiary"] = p["servicer"] = blob
        row = notice_to_row(p, "http://x/", today=date(2026, 6, 12))
        assert row["grantor"] is None
        assert row["beneficiary"] is None

    def test_identical_grantor_beneficiary_without_servicer_nulled(self):
        # Codex Medium: a drifted layout may poison grantor+beneficiary while the
        # servicer regex misses entirely — the detector must still fire.
        p = dict(self._parsed())
        p["grantor"] = p["beneficiary"] = "NOTICE IS HEREBY GIVEN that the Trustee will sell at public auction"
        p["servicer"] = None
        row = notice_to_row(p, "http://x/", today=date(2026, 6, 12))
        assert row["grantor"] is None
        assert row["beneficiary"] is None

    def test_legit_short_identical_parties_survive(self):
        # A short benign coincidence must NOT be nulled by the mis-parse detector.
        p = dict(self._parsed())
        p["grantor"] = p["beneficiary"] = p["servicer"] = "ACME LLC"
        row = notice_to_row(p, "http://x/", today=date(2026, 6, 12))
        assert row["grantor"] == "ACME LLC"
        assert row["beneficiary"] == "ACME LLC"

    def test_raw_hash_reflects_stored_values_not_discarded_garbage(self):
        # Codex Low: hash the sanitized/stored values — a nulled-out overlong parcel
        # must hash the same as a notice that never had one, or every re-crawl of
        # the same garbage churns the drift signal.
        base = dict(self._parsed())
        base["parcel"] = None
        with_garbage = dict(self._parsed())
        with_garbage["parcel"] = "9" * 80  # nulled by the clamp
        a = notice_to_row(base, "http://x/", today=date(2026, 6, 12))["raw_hash"]
        b = notice_to_row(with_garbage, "http://x/", today=date(2026, 6, 12))["raw_hash"]
        assert a == b


class TestCommonlyKnownAsBoundaries:
    """2026-07-01 live King layouts (real notice text). The Affinia phrasing leaked
    'The above property is' into the address (poisoning the normalized match key);
    the MTC phrasing has NO colon after 'commonly known as' and lost the address."""

    def test_above_property_boilerplate_excluded(self):
        text = (
            "T.S. No. 25-1 NOTICE OF TRUSTEE'S SALE will on 7/10/2026, at 10:00 AM "
            "Main Entrance sell at public auction the following-described real property "
            "Commonly known as: 2416 South 128th Street, Seatac, WA 98168 The above "
            "property is subject to that certain Deed of Trust dated December 12, 2005"
        )
        p = parse_nts_notice(text)
        assert p["property_address"] == "2416 South 128th Street, Seatac, WA 98168"

    def test_colonless_more_commonly_known_as(self):
        text = (
            "T.S. No. 25-2 NOTICE OF TRUSTEE'S SALE will on 7/10/2026, at 10:00 AM "
            "Main Entrance sell at public auction the following described real property "
            "APN: 338390-0045 More commonly known as 1814 FRANKLIN AVE E, SEATTLE, WA "
            "98102 which is subject to that certain Deed of Trust dated July 8, 2020"
        )
        p = parse_nts_notice(text)
        assert p["property_address"] == "1814 FRANKLIN AVE E, SEATTLE, WA 98102"

    def test_bare_colonless_prose_does_not_hijack_address(self):
        # Codex High (2026-07-01 review): the colon must stay REQUIRED for bare
        # "commonly known as" — entity prose like "…commonly known as Fannie Mae"
        # appearing BEFORE the real address label must not capture.
        text = (
            "T.S. No. 25-3 Current Beneficiary of the Deed of Trust: Federal National "
            "Mortgage Association commonly known as Fannie Mae Current Trustee of the "
            "Deed of Trust: X will on 7/10/2026, at 10:00 AM Main Entrance sell at "
            "public auction More commonly known as: 123 MAIN ST, TACOMA, WA 98402 "
            "Subject to that certain Deed of Trust"
        )
        p = parse_nts_notice(text)
        assert p["property_address"] == "123 MAIN ST, TACOMA, WA 98402"


class TestQualityLoanFormat:
    """Second REAL fixture: Quality Loan layout (whole header on one line, 'More
    commonly known as', 'Subject to' stop) — the CURRENT Tacoma Daily Index format."""

    def setup_method(self):
        self.p = parse_nts_notice(
            (Path(__file__).parent / "fixtures" / "nts_tacoma_quality_loan.txt").read_text(encoding="utf-8")
        )

    def test_ts_number_prefixed(self):
        assert self.p["ts_number"] == "WA-25-1032618-RM"

    def test_auction_date(self):
        assert self.p["auction_date"] == "7/17/2026"

    def test_trustee_not_ts_or_prose(self):
        # must be the company, NOT the TS# (one-line-layout trap) nor "undersigned Trustee,"
        assert self.p["trustee"] == "QUALITY LOAN SERVICE CORPORATION"

    def test_beneficiary_and_grantor(self):
        assert self.p["beneficiary"] == "Lakeview Loan Servicing, LLC"
        assert "TORYIAN M CARTER" in self.p["grantor"]

    def test_parcel(self):
        assert self.p["parcel"] == "5005002880"

    def test_default_amount_principal_sum_phrasing(self):
        # Quality Loan phrases section IV as "...is: The principal sum of $X" (not the
        # North Star "Principal $X"). The default amount feeds Result.default_amount —
        # the whole point of Tier 1 — so it must parse for this dominant live format.
        assert self.p["principal_owing"] == Decimal("170667.37")

    def test_address_clean_no_deed_overrun(self):
        addr = self.p["property_address"]
        assert "9016-9018" in addr and "LAKEWOOD" in addr and "98498" in addr
        assert "Subject to" not in addr and "Deed of Trust" not in addr
        # 'WASHINGTON BLVD' is a STREET name — must NOT be abbreviated to 'WA BLVD'
        # (Codex: a global WASHINGTON->WA rewrite corrupted the match key).
        assert "WASHINGTON BLVD" in addr.upper()

    def test_match_key_uses_full_street_name(self):
        from datetime import date as _d

        from src.scrapers.sources.nts_tacoma_index import notice_to_row
        row = notice_to_row(self.p, "x", _d(2026, 6, 12))
        # the key the matcher joins on must carry the real street, not 'WA BLVD'
        assert "WASHINGTON BLVD" in row["property_address_normalized"]

    def test_is_valid(self):
        assert is_valid_nts(self.p)


class TestDualLabelTrustee:
    """Live 2026-07-04 (Clark/MTC): a layout that prints BOTH 'Original Trustee of the
    Deed of Trust:' and 'Current Trustee of the Deed of Trust:'. The single
    '(?:Current\\s+)?Trustee…' regex matched the FIRST (Original) label, so the trustee
    field carried the original title company and the beneficiary value bled into the
    Original-Trustee text. The fix: prefer the Current label, never capture Original, and
    stop the beneficiary at 'Original Trustee'."""

    def test_prefers_current_trustee_over_original(self):
        text = (
            "Current Beneficiary of the Deed of Trust: Idaho Housing and Finance Association "
            "Original Trustee of the Deed of Trust: FIDELITY NATIONAL TITLE COMPANY OF WASHINGTON INC "
            "Current Trustee of the Deed of Trust: MTC Financial Inc. dba Trustee Corps "
            "Current Mortgage Servicer of the Deed of Trust: Idaho Housing "
            "I. NOTICE IS HEREBY GIVEN that on July 17, 2026, 09:00 AM, X will sell at public auction"
        )
        p = parse_nts_notice(text)
        assert p["trustee"] == "MTC Financial Inc. dba Trustee Corps"

    def test_beneficiary_stops_before_original_trustee(self):
        text = (
            "Current Beneficiary of the Deed of Trust: Idaho Housing and Finance Association "
            "Original Trustee of the Deed of Trust: FIDELITY NATIONAL TITLE COMPANY OF WASHINGTON INC "
            "Current Trustee of the Deed of Trust: MTC Financial Inc. dba Trustee Corps "
            "will on 7/1/2026, at 9:00 AM at X sell at public auction"
        )
        p = parse_nts_notice(text)
        assert p["beneficiary"] == "Idaho Housing and Finance Association"
        assert "Original Trustee" not in p["beneficiary"]

    def test_bare_single_trustee_label_unchanged(self):
        # A single-label Pierce/Snoho notice still resolves via the bare path. Real
        # notices follow the trustee with another labeled field (here the servicer),
        # which is what stops the value — mirror that so the test reflects real structure.
        text = (
            "Trustee of the Deed of Trust: North Star Trustee, LLC "
            "Current Mortgage Servicer of the Deed of Trust: Freedom Mortgage "
            "will on 7/1/2026, at 9:00 AM at X sell at public auction"
        )
        assert parse_nts_notice(text)["trustee"] == "North Star Trustee, LLC"

    def test_only_original_trustee_yields_none(self):
        # An original title company is not the acting trustee; None beats a wrong name.
        text = (
            "Original Trustee of the Deed of Trust: FIDELITY NATIONAL TITLE "
            "will on 7/1/2026, at 9:00 AM at X sell at public auction"
        )
        assert parse_nts_notice(text)["trustee"] is None
