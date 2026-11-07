"""nts_notices readers skip a notice whose grantor a privacy rule withholds (Phase 6 P5e1,
migration 132).

Real database, the matcher's own end-to-end fixture (tests/test_nts_matcher_task.py).
The withheld state is written here by hand, as P5e2's crawler will write it; the rule
row is a REAL (pending) privacy_suppressions row. Each case has its positive control:
the same notice WITHOUT the rule is matched, so the skip is the rule's doing.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.db.models import Result
from src.db.session import SyncSessionLocal
from src.workers.nts_matcher_task import match_nts_notices
from tests.test_nts_matcher_task import _bridge_fixture, _cleanup


def _rule_row() -> str:
    with SyncSessionLocal() as s:
        request_id = s.execute(text(
            "INSERT INTO privacy_requests (kind, channel, deadline_at) "
            "VALUES ('opt_out', 'web', now() + interval '45 days') RETURNING id")).scalar()
        rule = s.execute(text(
            "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, anchor_hmac, "
            "subject_kind, subject_hmac, scope, key_version, normalization_version, state) "
            "VALUES (gen_random_uuid(), :r, 'property', :h, 'person', :h, 'subject', 'v1', "
            "'n3', 'pending') RETURNING id"), {"r": request_id, "h": "c" * 64}).scalar()
        s.commit()
    return str(rule)


def _scenario(*, past: bool, withheld: bool) -> dict:
    tag = uuid.uuid4().hex[:8]
    pin = "".join(c for c in tag if c.isdigit()).ljust(10, "7")[:10]
    with SyncSessionLocal() as s:
        ids = _bridge_fixture(s, county="king", notice_parcel=f"{pin[:6]}-{pin[6:]}-08",
                              result_parcel=pin, tag=tag)
    sets = []
    params: dict = {"n": ids["notice"]}
    if past:  # a sale that already ran: the past pass reads inactive notices too
        sets += ["auction_date = :d", "fetched_at = :f", "is_active = false"]
        params |= {"d": date.today() - timedelta(days=10), "f": datetime.now(UTC)}
    if withheld:
        sets += ["grantor = NULL", "is_active = false", "privacy_suppression_id = :r"]
        params["r"] = _rule_row()
    if sets:
        with SyncSessionLocal() as s:
            s.execute(text(f"UPDATE nts_notices SET {', '.join(dict.fromkeys(sets))} "  # noqa: S608
                           "WHERE id = :n"), params)
            s.commit()
    return ids


def _matched(ids: dict) -> str | None:
    with SyncSessionLocal() as s:
        return s.get(Result, ids["result"]).nts_notice_id


@pytest.mark.parametrize("past", [False, True], ids=["live", "past"])
def test_a_withheld_notice_is_never_attached_but_the_same_notice_otherwise_is(past):
    control = _scenario(past=past, withheld=False)
    withheld = _scenario(past=past, withheld=True)
    try:
        match_nts_notices()
        assert _matched(control) == control["notice"]   # the pass does read this notice
        assert _matched(withheld) is None               # ... unless a rule withholds it
    finally:
        _cleanup(control)
        _cleanup(withheld)


@pytest.mark.parametrize(("grantor", "active"), [("SMITH JOHN", False), (None, True)])
def test_a_notice_carrying_a_rule_has_no_grantor_and_is_inactive(grantor, active):
    ids = _scenario(past=False, withheld=False)
    rule = _rule_row()
    try:
        with pytest.raises(IntegrityError), SyncSessionLocal() as s:
            s.execute(text("UPDATE nts_notices SET grantor = :g, is_active = :a, "
                           "privacy_suppression_id = :r WHERE id = :n"),
                      {"g": grantor, "a": active, "r": rule, "n": ids["notice"]})
            s.commit()
    finally:
        _cleanup(ids)


def _withhold(notice_id: str) -> None:
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE nts_notices SET grantor = NULL, is_active = false, "
                       "privacy_suppression_id = :r WHERE id = :n"),
                  {"r": _rule_row(), "n": notice_id})
        s.commit()


def _point_at_its_notice(ids: dict) -> None:
    """Give the fixture's lead the trustee-sale scraper's nts_source contract."""
    with SyncSessionLocal() as s:
        auction = s.execute(text("SELECT auction_date FROM nts_notices WHERE id = :n"),
                            {"n": ids["notice"]}).scalar_one()
        s.execute(text(
            "UPDATE results SET enrichment_data = CAST(:e AS json) WHERE id = :r"),
            {"r": ids["result"], "e": json.dumps({"nts_source": {
                "notice_id": ids["notice"], "auction_date": auction.isoformat(),
                "ts_number": "T"}})})
        s.commit()


def test_the_finalizer_never_writes_a_withheld_notices_auction_data():
    from src.workers.trustee_sale_finalize import (
        TrusteeSaleFinalizeError,
        finalize_trustee_sale_job,
    )

    control = _scenario(past=False, withheld=False)
    withheld = _scenario(past=False, withheld=False)
    try:
        _point_at_its_notice(control)
        _point_at_its_notice(withheld)
        _withhold(withheld["notice"])
        with SyncSessionLocal() as s:
            finalize_trustee_sale_job(s, control["job"], control["user"])
            s.commit()
        assert _matched(control) == control["notice"]
        with pytest.raises(TrusteeSaleFinalizeError), SyncSessionLocal() as s:
            finalize_trustee_sale_job(s, withheld["job"], withheld["user"])  # fails closed
        assert _matched(withheld) is None
    finally:
        _cleanup(control)
        _cleanup(withheld)


def test_snohomish_leads_built_from_the_pdf_drop_a_withheld_notice():
    from src.db.models import NtsNotice
    from src.scrapers.base_scraper import ScrapedRecord
    from src.scrapers.snohomish_wa_pre_foreclosure import _without_withheld_notices

    tag = uuid.uuid4().hex[:8]
    kept_ts, gone_ts = f"TEST-K-{tag}", f"TEST-W-{tag}"
    ids = []
    with SyncSessionLocal() as s:
        for ts in (kept_ts, gone_ts):
            n = NtsNotice(id=str(uuid.uuid4()), source="snohomish_tribune", ts_number=ts,
                          county="snohomish", state="WA", grantor="DOE JANE",
                          auction_date=date.today() + timedelta(days=20), is_active=True)
            s.add(n)
            ids.append(n.id)
        s.commit()
    try:
        _withhold(ids[1])
        records = [ScrapedRecord(party_name="DOE JANE", enrichment_data={"ts_number": ts})
                   for ts in (kept_ts, gone_ts)]
        kept = _without_withheld_notices(records)
        assert [r.enrichment_data["ts_number"] for r in kept] == [kept_ts]
    finally:
        with SyncSessionLocal() as s:
            s.execute(text("DELETE FROM nts_notices WHERE id = ANY(CAST(:i AS uuid[]))"),
                      {"i": ids})
            s.commit()
