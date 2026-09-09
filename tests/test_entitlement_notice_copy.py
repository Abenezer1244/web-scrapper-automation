"""The customer-facing copy of an entitlement refusal.

A plan limit is not an application error, and the strings below are the only
part of the entitlement system a customer ever reads. They are asserted here
because every one of them was wrong at some point: a raw ``'starter'`` slug in
quotes, ``distinct counties`` leaking an implementation word, ``1 counties``,
and an em dash the product owner explicitly banned.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.api.entitlements import (
    CODE_COUNTY_LIMIT,
    CODE_PLAN_LIMIT,
    CODE_RECORD_TYPE,
    ConfigRow,
    Violation,
    combine_violations,
    config_run_violation,
    county_cap_violation,
    county_outside_plan_violation,
    disallowed_record_types,
    plan_limit_http,
    record_type_violation,
)
from src.config.constants import COUNTY_LIMIT_BY_PLAN, RECORD_TYPES_BY_PLAN

# Every dash a copywriter might reach for that the owner banned outright, plus
# the two forms that survive a naive find-and-replace.
BANNED_CHARS = ("—", "–", "―")


def _all_strings(v: Violation) -> list[str]:
    return [v.title, v.message]


# ── the exact case in the bug report ─────────────────────────────────────────


def test_starter_two_counties_reads_like_the_agreed_copy():
    v = county_cap_violation("starter", projected=2, cap=1)
    assert v.code == CODE_COUNTY_LIMIT
    assert v.title == "County limit reached"
    assert v.message == (
        "Your Starter plan includes 1 county. "
        "This would put your account at 2 counties."
    )


def test_the_wire_payload_is_structured_and_carries_the_instruction():
    exc = plan_limit_http(county_cap_violation("starter", projected=2, cap=1))
    assert exc.status_code == 402
    assert exc.detail == {
        "code": "county_limit",
        "title": "County limit reached",
        "message": (
            "Your Starter plan includes 1 county. "
            "This would put your account at 2 counties. "
            "Upgrade your plan to continue."
        ),
    }


# ── grammar and naming ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("plan", "projected", "cap", "expected"),
    [
        ("starter", 2, 1, "Your Starter plan includes 1 county. This would put your account at 2 counties."),
        ("pro", 4, 3, "Your Pro plan includes 3 counties. This would put your account at 4 counties."),
        ("business", 11, 10, "Your Business plan includes 10 counties. This would put your account at 11 counties."),
    ],
)
def test_county_counts_are_pluralized_and_plan_names_are_title_cased(plan, projected, cap, expected):
    assert county_cap_violation(plan, projected, cap).message == expected


def test_an_unknown_plan_slug_reads_as_the_entry_tier_not_as_itself():
    # Every gate fails closed to starter; the copy has to agree with the gate,
    # and must never echo an unrecognized slug back at the customer.
    v = county_cap_violation("enterprise", projected=2, cap=1)
    assert "Starter" in v.message
    assert "enterprise" not in v.message.lower()


def test_plan_names_never_appear_quoted_or_lowercase():
    for v in (
        county_cap_violation("starter", 2, 1),
        county_outside_plan_violation("pro", "WA", "King", 3),
        record_type_violation("business", ["divorce"]),
    ):
        for text in _all_strings(v):
            assert "'" not in text
            assert '"' not in text
            for slug in ("starter", "pro", "business", "agency"):
                assert slug not in text


def test_no_banned_dash_in_any_generated_string():
    violations = [
        county_cap_violation("starter", 2, 1),
        county_outside_plan_violation("starter", "wa", "pierce", 1),
        record_type_violation("starter", ["divorce"]),
        record_type_violation("pro", ["divorce", "code_violation", "death_certificate"]),
        combine_violations(
            [record_type_violation("starter", ["divorce"]), county_cap_violation("starter", 2, 1)]
        ),
    ]
    texts = [t for v in violations for t in _all_strings(v)]
    texts.append(plan_limit_http(violations[0]).detail["message"])
    offenders = [t for t in texts if any(c in t for c in BANNED_CHARS)]
    assert offenders == []


def test_implementation_vocabulary_never_reaches_the_customer():
    v = county_cap_violation("starter", 2, 1)
    for word in ("distinct", "config", "record_type", "user_id", "cap"):
        assert word not in v.message.lower()


# ── record types ─────────────────────────────────────────────────────────────


def test_record_type_labels_are_human_and_grammar_agrees_with_the_count():
    assert record_type_violation("starter", ["divorce"]).message == (
        "Divorce is not included in your Starter plan."
    )
    assert record_type_violation("starter", ["pre_foreclosure", "divorce"]).message == (
        "Divorce and Pre-Foreclosure are not included in your Starter plan."
    )
    assert record_type_violation(
        "pro", ["divorce", "code_violation", "death_certificate"]
    ).message == (
        "Code Violation, Death Certificate and Divorce are not included in your Pro plan."
    )


def test_an_unmapped_record_type_slug_degrades_to_words_not_underscores():
    assert "Eviction" in record_type_violation("starter", ["eviction"]).message
    assert "_" not in record_type_violation("starter", ["some_new_type"]).message


def test_the_record_type_message_does_not_enumerate_the_whole_plan():
    # Business allows every live type; listing them buries the one that matters.
    msg = record_type_violation("business", ["eviction"]).message
    assert "Probate" not in msg
    assert len(msg) < 120


# ── both limits broken at once ───────────────────────────────────────────────


def test_a_request_breaking_both_limits_gets_one_notice_naming_both():
    combined = combine_violations(
        [record_type_violation("starter", ["divorce"]), county_cap_violation("starter", 2, 1)]
    )
    assert combined.code == CODE_PLAN_LIMIT
    assert combined.title == "Plan limit reached"
    assert "Divorce is not included" in combined.message
    assert "This would put your account at 2 counties." in combined.message


def test_a_single_violation_keeps_its_own_specific_title():
    only = combine_violations([county_cap_violation("starter", 2, 1)])
    assert only.title == "County limit reached"
    assert only.code == CODE_COUNTY_LIMIT


# ── the run path, and the contract the workers still depend on ───────────────


def _row(county: str, record_type: str = "probate", *, days_old: int = 0) -> ConfigRow:
    return ConfigRow(
        id=f"cfg-{county}-{record_type}",
        state="WA",
        county=county,
        record_type=record_type,
        created_at=datetime(2026, 1, 1 + days_old, tzinfo=UTC),
        active=True,
    )


def test_run_path_returns_a_structured_violation_for_a_county_outside_the_plan():
    v = config_run_violation("starter", "WA", "pierce", "probate", [_row("king"), _row("pierce", days_old=1)])
    assert v is not None
    assert v.code == CODE_COUNTY_LIMIT
    assert v.message == "Your Starter plan includes 1 county. Pierce, WA is outside that limit."


def test_run_path_returns_a_structured_violation_for_a_record_type():
    v = config_run_violation("starter", "WA", "king", "divorce", [_row("king")])
    assert v is not None
    assert v.code == CODE_RECORD_TYPE
    assert v.message == "Divorce is not included in your Starter plan."


def test_run_path_returns_none_when_the_plan_covers_it():
    assert config_run_violation("starter", "WA", "king", "probate", [_row("king")]) is None


def test_str_of_a_violation_is_the_message_so_worker_log_lines_still_work():
    # workers/tasks.py, batch_tasks.py and scheduler_helpers/dispatch.py all
    # interpolate the return value of config_run_violation into a string. If this
    # ever stops being the message, those three surfaces start printing a repr.
    v = config_run_violation("starter", "WA", "pierce", "probate", [_row("king")])
    assert str(v) == v.message
    assert f"{v}" == v.message
    assert bool(v) is True  # `if should_block_run(...)` relies on truthiness


def test_disallowed_record_types_still_returns_the_raw_slugs():
    # The gate works in slugs; only the copy layer works in labels.
    assert disallowed_record_types("starter", ["probate", "divorce"]) == {"divorce"}
    assert disallowed_record_types("agency", ["divorce"]) == set()


# ── the copy must agree with the gate, not with what the gate MEANT ──────────


def test_an_untrimmed_plan_value_reads_as_the_tier_actually_enforced():
    """The copy has to name the tier the gate is actually applying. This test used
    to prove that by asserting the OPPOSITE outcome, and it was right to at the
    time: nothing trimmed, so a stored "pro " missed every `.get(plan, starter)`,
    was enforced on Starter limits, and had to be TOLD "Starter" or the sentence
    would have been false.

    Both halves now normalize through `constants.normalize_plan`, which strips as
    well as lowers, so "pro " is enforced as Pro and reads as Pro. The invariant
    is unchanged and is what this asserts: whatever tier the gate resolves, the
    copy names that same tier. Only the resolved tier moved.
    """
    from src.api.entitlements import _plan_of
    from src.config.constants import normalize_plan
    from src.db.models import User

    for stored, resolved in (("pro ", "pro"), (" Business", "business"), ("AGENCY", "agency")):
        assert _plan_of(User(plan=stored)) == resolved
        assert normalize_plan(stored) == resolved

    padded = _plan_of(User(plan="pro "))
    assert padded in RECORD_TYPES_BY_PLAN  # the gate now recognizes it
    assert "pre_foreclosure" in RECORD_TYPES_BY_PLAN[padded]
    assert record_type_violation(padded, ["divorce"]).message == (
        "Divorce is not included in your Pro plan."
    )
    assert county_cap_violation(padded, 4, COUNTY_LIMIT_BY_PLAN[padded]).message == (
        "Your Pro plan includes 3 counties. This would put your account at 4 counties."
    )


def test_an_unrecognized_plan_is_still_enforced_and_named_as_the_entry_tier():
    """Normalizing is not the same as accepting anything. A plan value that is not
    a catalog id after trimming and lowering still fails closed to Starter in the
    gate, and still reads as Starter in the copy, so the two cannot disagree."""
    from src.api.entitlements import _plan_of
    from src.config.plans import plan_label
    from src.db.models import User

    junk = _plan_of(User(plan="platinum"))
    assert junk not in RECORD_TYPES_BY_PLAN
    assert plan_label(junk) == "Starter"
    assert record_type_violation(junk, ["pre_foreclosure"]).message == (
        "Pre-Foreclosure is not included in your Starter plan."
    )


def test_plan_names_come_from_the_billing_catalog():
    """One source of truth for what a plan is called. plans.py exists because a
    duplicated plan constant once drifted and quoted a price we do not charge;
    a duplicated NAME would drift the same way."""
    from src.config.plans import PLAN_CATALOG, plan_label

    for entry in PLAN_CATALOG:
        assert plan_label(entry["id"]) == entry["name"]
        assert plan_label(entry["id"]) in county_cap_violation(entry["id"], 99, 1).message


def test_record_type_labels_come_from_one_map_shared_with_the_exporters():
    from src.api.routes.segments import _label as segments_label
    from src.config.constants import record_type_label
    from src.workers.batch_export import _label as export_label

    assert segments_label is record_type_label
    assert export_label is record_type_label
    # The behaviour the two private copies had, preserved.
    assert record_type_label("pre_foreclosure") == "Pre-Foreclosure"
    assert record_type_label("tax_delinquent") == "Tax Delinquent"
    assert record_type_label("unknown_x") == "Unknown X"
