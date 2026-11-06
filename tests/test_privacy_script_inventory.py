"""Operator scripts that print or export lead subjects must honour suppression first.

docs/legal/audit-2026-11/08-PII-EMISSION-INVENTORY.md lists the scripts that print or
write subject data with no privacy filter. Today no rule can be active (migration
125's guard, src/privacy/activation.py), so they expose nobody who opted out. This
test turns the inventory into a hard gate: the change that raises
IMPLEMENTED_ENFORCEMENT_VERSION (P7) cannot pass CI while any listed script still
exists without filtering on the privacy flag. Deleting a script (and its entry
below) satisfies it too.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.privacy import activation

_ROOT = Path(__file__).resolve().parent.parent

# Section A of the inventory: High (bulk or contact-level) and cross-tenant stdout.
HIGH_SCRIPTS = (
    "scripts/verify_tracerfy_provenance.py",
    "scripts/verify_entitlement_deploy.py",
    "scripts/diag_test11_dq.py",
    "scripts/diag_test7_source_capture.py",
    "scripts/diag_king_tax_owner_results.py",
    "scripts/purge_test_batch_configs.py",
    "scripts/diag_test7_rows.py",
    "scripts/backfill_preforeclosure_party_names.py",
    "scripts/backfill_king_code_violation_owner.py",
    "scripts/repair_stale_absentee_owner.py",
    "scripts/backfill_assumed_mailing.py",
    "scripts/backfill_pierce_statewide_mailing.py",
    "scripts/king_rpacct_mailing.py",
    "scripts/king_taxbill_mailing_check.py",
    "scripts/repair_king_property_situs.py",
    "scripts/backfill_property_situs_parts.py",
    "scripts/repair_placeholder_absentee.py",
    "scripts/backfill_king_code_violation_mailing.py",
    "scripts/release_stranded_dedup_claims.py",
    "scripts/diag_king_bad_parcel_rows.py",
    "scripts/diag_probate_party_scan.py",
    "scripts/diag_preforeclosure_party_names.py",
    "scripts/diag_xcheck_followup.py",
    "scripts/diag_xcheck_king_and_canary.py",
    "scripts/diag_nts_pointer_integrity.py",
    "scripts/diag_nts_recent_matches.py",
    "scripts/diag_nts_null_downgrade.py",
    "scripts/diag_king_ts_repair_plan.py",
    "scripts/repair_nts_ts_number.py",
    "scripts/diag_nts_stored_vs_source.py",
    "scripts/diag_skip_trace_match_audit.py",
    "scripts/diag_match_key_collisions.py",
    "scripts/rerun_pierce_address_recovery.py",
)

# Any of these in a script's source means it reads through the privacy flag.
_FILTER_MARKERS = ("privacy_suppressed_at", "not_privacy_suppressed", "actionable_condition",
                   "actionable_sql")


def _unfiltered() -> list[str]:
    left = []
    for rel in HIGH_SCRIPTS:
        path = _ROOT / rel
        if path.exists() and not any(m in path.read_text(encoding="utf-8")
                                     for m in _FILTER_MARKERS):
            left.append(rel)
    return left


def test_activation_is_blocked_while_a_listed_script_ignores_suppression():
    if activation.IMPLEMENTED_ENFORCEMENT_VERSION < activation.REQUIRED_ENFORCEMENT_VERSION:
        pytest.skip("suppression cannot be activated yet; the gate arms with P7")
    assert _unfiltered() == [], (
        "These scripts print or export lead subjects without the privacy filter. "
        "Filter them (src/api/lead_actionability.py) or delete them before activation.")


def test_every_listed_script_exists():
    """A typo would silently exempt a script, so every entry must be a real file.
    Retiring a script means deleting it AND its entry here in the same change."""
    assert [rel for rel in HIGH_SCRIPTS if not (_ROOT / rel).is_file()] == []


def test_the_gate_would_fail_if_armed_today(monkeypatch):
    """Positive control: with enforcement claimed, today's scripts trip the gate."""
    monkeypatch.setattr(activation, "IMPLEMENTED_ENFORCEMENT_VERSION",
                        activation.REQUIRED_ENFORCEMENT_VERSION)
    with pytest.raises(AssertionError):
        test_activation_is_blocked_while_a_listed_script_ignores_suppression()


def test_the_list_is_exactly_the_inventorys_high_scripts():
    """An omission would let activation pass with a documented High script still live:
    the list must name every script in the inventory's section A High tables and
    cross-tenant list, and nothing else."""
    doc = (_ROOT / "docs/legal/audit-2026-11/08-PII-EMISSION-INVENTORY.md").read_text(
        encoding="utf-8")
    high = doc[doc.index("## A. Scripts"):doc.index("**Customer emails to stdout")]
    documented = {f"scripts/{n}" for n in re.findall(r"\b([a-z0-9_]+\.py)\b", high)}
    assert documented == set(HIGH_SCRIPTS)
