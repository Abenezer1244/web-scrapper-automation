"""Batch child ScraperConfig display names — derive_batch_child_name().

Children used to be named "{County} {record_type} (batch)", ignoring the batch
name the user typed, so every batch over the same county+record_type minted
identical names (41% of production configs collided). Pure tests, no DB.
"""
from src.api.routes.batches import _SCRAPER_NAME_MAX, derive_batch_child_name


class TestDerivesFromBatchName:
    def test_uses_the_batch_name_the_user_typed(self):
        assert (
            derive_batch_child_name("July Skip Trace", "king", "pre_foreclosure")
            == "July Skip Trace - King Pre Foreclosure"
        )

    def test_two_batches_same_county_and_type_no_longer_collide(self):
        # The actual production bug: these were both "King Probate (batch)".
        first = derive_batch_child_name("Q3 Push", "king", "probate")
        second = derive_batch_child_name("Winter List", "king", "probate")
        assert first != second

    def test_record_type_underscores_become_words(self):
        assert derive_batch_child_name("B", "king", "tax_delinquent").endswith(
            "King Tax Delinquent"
        )

    def test_county_is_title_cased(self):
        assert derive_batch_child_name("B", "walla walla", "probate") == (
            "B - Walla Walla Probate"
        )


class TestLegacyFallback:
    def test_no_name_keeps_the_legacy_batch_suffix(self):
        # BatchCreateRequest.name stays Optional for direct-API callers.
        assert derive_batch_child_name(None, "pierce", "probate") == (
            "Pierce Probate (batch)"
        )

    def test_empty_string_is_absent(self):
        assert derive_batch_child_name("", "pierce", "probate").endswith("(batch)")

    def test_whitespace_only_is_absent(self):
        assert derive_batch_child_name("   \t  ", "pierce", "probate").endswith(
            "(batch)"
        )


class TestSanitization:
    def test_newlines_become_spaces_not_deletions(self):
        # config.name lands unescaped in the lead-delivery email SUBJECT
        # (src/workers/delivery.py) — a bare CR/LF must never survive, but the
        # words either side must not be glued together either.
        assert derive_batch_child_name("King\r\nProbate", "king", "probate") == (
            "King Probate - King Probate"
        )

    def test_control_characters_are_removed(self):
        out = derive_batch_child_name("A\x00\x1bB", "king", "probate")
        assert "\x00" not in out and "\x1b" not in out
        assert out == "A B - King Probate"

    def test_runs_of_whitespace_collapse(self):
        assert derive_batch_child_name("  A     B  ", "king", "probate") == (
            "A B - King Probate"
        )

    def test_non_breaking_space_does_not_glue_words(self):
        assert derive_batch_child_name("A\xa0B", "king", "probate") == (
            "A B - King Probate"
        )


class TestLengthCap:
    def test_result_always_fits_the_column(self):
        # ScraperConfig.name is String(255); county is String(128) and the batch
        # name is capped at 120, so the naive join can overflow.
        out = derive_batch_child_name("x" * 120, "y" * 128, "pre_foreclosure")
        assert len(out) <= _SCRAPER_NAME_MAX

    def test_truncation_keeps_the_county_and_type_suffix(self):
        # The suffix says WHAT the child scrapes — it must survive intact.
        out = derive_batch_child_name("x" * 120, "y" * 128, "probate")
        assert out.endswith(f"{'y' * 128}".title() + " Probate")

    def test_short_names_are_untouched(self):
        out = derive_batch_child_name("Short", "king", "probate")
        assert out == "Short - King Probate"
        assert len(out) < _SCRAPER_NAME_MAX

    def test_absurd_county_alone_still_fits(self):
        out = derive_batch_child_name(None, "z" * 250, "probate")
        assert len(out) <= _SCRAPER_NAME_MAX
