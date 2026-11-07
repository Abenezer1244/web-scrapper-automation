"""The privacy write gate in run_scrape_job: a withheld person is never saved (Phase 6 P5d).

The whole production task runs against the real test DB and the real test Redis, as in
tests/test_job_degradations_wiring.py (whose make_job fixture it reuses). Replaced: the
county portal and R2 (external), and the rule snapshot (the database refuses any rule
state but 'pending' until P7). Synthetic people only.
"""
from __future__ import annotations

# make_job is a fixture imported from the wiring tests and taken as a parameter
# name, which ruff reads as redefining the import. Only F811, only this file.
# ruff: noqa: F811
from sqlalchemy import text

import src.privacy.authorize as authorize_mod
from src.db.session import SyncSessionLocal
from src.privacy import keys
from src.privacy.authorize import RuleSnapshotError, build_snapshot
from src.scrapers.base_scraper import ScrapedRecord
from src.workers import tasks
from tests.test_job_degradations_wiring import _apply, make_job  # noqa: F401 -- fixture
from tests.test_privacy_authorize import _history, _keyring  # noqa: F401 -- autouse keyring

_PARCEL, _OTHER = "1234567890", "5555555555"


def _county(job_id: str) -> str:
    with SyncSessionLocal() as s:
        return s.execute(text(
            "SELECT c.county FROM jobs j JOIN scraper_configs c ON c.id = j.scraper_config_id "
            "WHERE j.id = :j"), {"j": job_id}).scalar_one()


def _portal(monkeypatch, *people: tuple[str, str]) -> None:
    async def _records(*_a, **_k):
        return [ScrapedRecord(date_recorded="10/01/2026", party_name=name, doc_type="Probate",
                              parcel_id=parcel, property_address=f"{i} Gate Ave, Seattle, WA",
                              mailing_address=f"{i} Gate Ave, Seattle, WA",
                              raw_html_hash=f"gate{i:028d}")
                for i, (name, parcel) in enumerate(people)]

    monkeypatch.setattr(tasks, "_run_scraper", _records)


def _rules(monkeypatch, snapshot) -> None:
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", lambda db: snapshot)


def _saved(job_id: str):
    with SyncSessionLocal() as s:
        job = s.execute(text("SELECT status, records_found, billed_count, error_message "
                             "FROM jobs WHERE id = :j"), {"j": job_id}).one()
        rows = s.execute(text("SELECT parcel_id, privacy_checked_gen FROM results "
                              "WHERE job_id = :j ORDER BY parcel_id"), {"j": job_id}).all()
        logs = [m for (m,) in s.execute(text(
            "SELECT message FROM job_logs WHERE job_id = :j"), {"j": job_id}).all()]
    return job, rows, logs


_PEOPLE = (("SMITH JOHN", _PARCEL), ("DOE JANE", _PARCEL), ("SMITH JOHN", _OTHER))


def test_a_withheld_or_review_record_is_never_saved(make_job, redis_client, monkeypatch):
    job_id = make_job()
    anchor = keys.property_anchor_value(_PARCEL, _county(job_id), "WA")
    assert anchor  # the rule can be anchored on this test county
    snapshot = build_snapshot(_history(["pending", "active"], anchor=("property", anchor)))
    _portal(monkeypatch, *_PEOPLE)
    _rules(monkeypatch, snapshot)
    _apply(job_id)
    job, rows, logs = _saved(job_id)
    assert job.status == "done" and job.records_found == 3
    # Only DOE JANE: SMITH JOHN on the rule's parcel (withheld) and elsewhere (review)
    # never reached results, and the saved row carries the generation it was checked at.
    assert [tuple(r) for r in rows] == [(_PARCEL, snapshot.generation)]
    assert any("Withheld 2 record(s)" in m for m in logs)
    assert not any("SMITH" in m for m in logs)


def test_with_no_active_rule_every_record_is_saved(make_job, redis_client, monkeypatch):
    job_id = make_job()
    _portal(monkeypatch, *_PEOPLE)
    _rules(monkeypatch, build_snapshot(_history(["pending"])))
    _apply(job_id)
    job, rows, logs = _saved(job_id)
    assert job.status == "done" and len(rows) == 3
    assert {r.privacy_checked_gen for r in rows} == {0}
    assert not any("Withheld" in m for m in logs)


def test_rules_that_cannot_load_stop_the_run_before_anything_is_saved(
    make_job, redis_client, monkeypatch,
):
    job_id = make_job()
    _portal(monkeypatch, *_PEOPLE)

    def _broken(db):
        raise RuleSnapshotError("rules could not be loaded")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    _apply(job_id)
    job, rows, _logs = _saved(job_id)
    assert job.status == "failed" and rows == []
    assert job.records_found == 3          # kept: the failed load rolled back alone
    assert job.billed_count == 0
    assert "privacy requests" in job.error_message


def test_someone_else_on_another_parcel_is_saved(make_job, redis_client, monkeypatch):
    job_id = make_job()
    _portal(monkeypatch, ("DOE JANE", _PARCEL))
    # The rule is SMITH JOHN on a King parcel; DOE JANE is someone else entirely.
    _rules(monkeypatch, build_snapshot(_history(["pending", "active"])))
    _apply(job_id)
    job, rows, _logs = _saved(job_id)
    assert job.status == "done" and len(rows) == 1
