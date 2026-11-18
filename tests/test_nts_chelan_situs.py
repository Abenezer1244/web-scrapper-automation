"""Chelan approved notices take the parcel's current site address (migration 164).

A trustee's printed address can be years out of date (REC-2628915 printed 170 Hill
Street; the assessor and the WA statewide parcel layer give 62 Village Vista Ln for the
same legal description). Approval looks the parcel up once and freezes the answer, keeping
the printed address beside it. The statewide layer is an external API: its query is
replaced with a response of the real shape, as tests/test_county_gis_parse.py does.
"""
# ruff: noqa: F811
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

import src.scrapers.enrichment.county_gis as cg
from src.db.models import NtsRecorderDocument as Doc
from src.scrapers.trustee_sale import _record_from_notice
from src.workers.nts_crawler import (
    ReviewError,
    approve_recorder_document,
    lookup_parcel_situs,
    refresh_notice_address,
)
from tests.test_nts_chelan_approval import (  # noqa: F401 -- fixtures and helpers
    _APN,
    _SALE,
    _notice,
    _operator,
    _run,
    _stage,
    _values,
    c,
    db,
)
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring

_PRINTED = "170 HILL STREET, MANSON, WA 98831"
_SITUS = "62 VILLAGE VISTA LN MANSON, WA 98831"


@pytest.fixture
def layer(monkeypatch):
    """The statewide layer answering for _APN, as _batch_query_wa_statewide returns it:
    the lookup runs the batch path pre-foreclosure enrichment runs (Codex, Cowlitz)."""
    asked: list[tuple[str, str]] = []

    def _answer(parcel_ids, county):
        asked.extend((p, county) for p in parcel_ids)
        return {p: {"property_address": "62 VILLAGE VISTA LN \r\nMANSON, WA 98831".replace("\r\n", " "),
                    "mailing_address": None, "parcel_id": _APN, **cg._situs_parts("MANSON", "98831")}
                for p in parcel_ids if p == _APN}

    monkeypatch.setattr(cg.settings, "GIS_ENRICHMENT_ENABLED", True, raising=False)
    monkeypatch.setattr(cg, "_batch_query_county", lambda *a, **k: {})
    monkeypatch.setattr(cg, "_batch_query_wa_statewide", _answer)
    return asked


def test_the_lookup_returns_the_parcels_site_address_in_enrichments_form(layer):
    assert lookup_parcel_situs("chelan", "WA", _APN) == _SITUS
    assert layer == [(_APN, "chelan")]
    assert lookup_parcel_situs("chelan", "WA", "000000000000") is None


def test_the_lookup_never_sets_off_an_owner_mailing_request(layer, monkeypatch):
    """Situs only (Codex): Chelan has a bulk mailing source the batch path would ask."""
    def _never(*a, **k):
        raise AssertionError("owner-mailing source asked")

    monkeypatch.setattr(cg, "has_bulk_mailing_source", lambda *a, **k: True)
    monkeypatch.setattr(cg, "_resolve_bulk_mailing", _never)
    assert lookup_parcel_situs("chelan", "WA", _APN) == _SITUS


def test_the_lookup_is_off_when_gis_enrichment_is(monkeypatch):
    monkeypatch.setattr(cg.settings, "GIS_ENRICHMENT_ENABLED", False, raising=False)
    assert lookup_parcel_situs("chelan", "WA", _APN) is None


def test_approval_freezes_the_site_address_and_keeps_the_printed_one(db, tmp_path):
    doc_id = _stage(db, tmp_path, "9910001", "2628901")
    approve_recorder_document(db, doc_id, reviewer="op-1", situs=_SITUS,
                              values=_values(property_address=_PRINTED))
    db.flush()
    n = _notice(db, "2628901")
    assert (n.property_address, n.address_source, n.address_on_notice) == (_SITUS, "assessor_situs", _PRINTED)
    assert "VILLAGE VISTA" in n.property_address_normalized.upper()
    approved = db.get(Doc, doc_id).fields["approved"]
    assert (approved["property_address"], approved["address_on_notice"]) == (_SITUS, _PRINTED)

    nts = _record_from_notice(n)
    assert nts.property_address == _SITUS
    assert nts.enrichment_data["nts_source"]["address_on_notice"] == _PRINTED


def test_without_a_site_address_the_printed_one_stands(db, tmp_path):
    doc_id = _stage(db, tmp_path, "9910002", "2628902")
    approve_recorder_document(db, doc_id, reviewer="op-1", situs=None,
                              values=_values(property_address=_PRINTED))
    db.flush()
    n = _notice(db, "2628902")
    assert (n.property_address, n.address_source, n.address_on_notice) == (_PRINTED, "notice", _PRINTED)
    assert _record_from_notice(n).enrichment_data["nts_source"]["address_on_notice"] is None


def test_an_approved_notice_can_be_given_its_site_address_once(db, tmp_path):
    doc_id = _stage(db, tmp_path, "9910003", "2628903")
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(property_address=_PRINTED))
    assert refresh_notice_address(db, doc_id, reviewer="op-2", situs=_SITUS) == (_PRINTED, _SITUS)
    db.flush()
    n = _notice(db, "2628903")
    assert (n.property_address, n.address_source, n.address_on_notice) == (_SITUS, "assessor_situs", _PRINTED)
    assert "site address" in db.get(Doc, doc_id).decision_note
    # Again: nothing to change, and the printed address is not overwritten by the situs.
    assert refresh_notice_address(db, doc_id, reviewer="op-2", situs=_SITUS) == (_SITUS, _SITUS)
    assert _notice(db, "2628903").address_on_notice == _PRINTED


def test_refresh_needs_an_address_and_an_approved_notice(db, tmp_path):
    doc_id = _stage(db, tmp_path, "9910004", "2628904")
    with pytest.raises(ReviewError, match="not"):
        refresh_notice_address(db, doc_id, reviewer="op-1", situs=_SITUS)  # still pending review
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(property_address=_PRINTED))
    with pytest.raises(ReviewError, match="no current site address"):
        refresh_notice_address(db, doc_id, reviewer="op-1", situs=None)
    assert _notice(db, "2628904").property_address == _PRINTED


def test_a_notice_already_delivered_as_a_trustee_sale_lead_keeps_its_address(db, tmp_path):
    from src.utils.crypto import blind_index

    doc_id = _stage(db, tmp_path, "9910005", "2628905")
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(property_address=_PRINTED))
    db.flush()
    notice_id = str(_notice(db, "2628905").id)
    uid, cfg, job = (str(uuid.uuid4()) for _ in range(3))
    email = f"ts-{uuid.uuid4().hex[:8]}@example.test"
    db.execute(text("INSERT INTO users (id, email, password_hash, email_hmac, plan) VALUES (:u, :e, 'x', :h, 'pro')"),
               {"u": uid, "e": email, "h": blind_index(email)})
    db.execute(text("INSERT INTO scraper_configs (id, user_id, name, county, state, record_type, fields, "
                    "enrichment, schedule, deliver) VALUES (:c, :u, 'ts', 'chelan', 'WA', 'trustee_sale', "
                    "'[]', '[]', '{}', '{}')"), {"c": cfg, "u": uid})
    db.execute(text("INSERT INTO jobs (id, user_id, scraper_config_id, status) VALUES (:j, :u, :c, 'done')"),
               {"j": job, "u": uid, "c": cfg})
    db.execute(text("INSERT INTO results (id, job_id, user_id, enrichment_data) VALUES "
                    "(gen_random_uuid(), :j, :u, CAST(:e AS json))"),
               {"j": job, "u": uid, "e": json.dumps({"nts_source": {"notice_id": notice_id}})})
    with pytest.raises(ReviewError, match="already been delivered"):
        refresh_notice_address(db, doc_id, reviewer="op-1", situs=_SITUS)
    assert _notice(db, "2628905").property_address == _PRINTED


def test_the_cli_looks_the_parcel_up_before_approving_and_says_what_it_used(c, db, tmp_path, capsys, layer):
    doc_id = _stage(db, tmp_path, "9910006", "2628906")
    db.commit()
    email = _operator(c)
    _run(c, email, "approve", "--doc", doc_id, "--sale-date", _SALE.isoformat(), "--parcel", _APN,
         "--grantor", "DOE, JANE", "--address", _PRINTED)
    out = capsys.readouterr().out
    assert f"property address: {_SITUS}" in out and f"the notice printed: {_PRINTED}" in out
    assert layer == [(_APN, "chelan")]
    row = c.execute(text("SELECT property_address, address_source, address_on_notice FROM nts_notices "
                         "WHERE ts_number = 'REC-2628906'")).one()
    assert tuple(row) == (_SITUS, "assessor_situs", _PRINTED)


def test_the_cli_can_keep_the_printed_address_and_refresh_it_later(c, db, tmp_path, capsys, layer):
    doc_id = _stage(db, tmp_path, "9910007", "2628907")
    db.commit()
    email = _operator(c)
    _run(c, email, "approve", "--doc", doc_id, "--sale-date", _SALE.isoformat(), "--parcel", _APN,
         "--grantor", "DOE, JANE", "--address", _PRINTED, "--keep-notice-address")
    assert layer == []  # no lookup when the operator keeps the printed address
    assert "(as printed" in capsys.readouterr().out
    _run(c, email, "refresh-address", "--doc", doc_id)
    assert f"address: {_PRINTED} -> {_SITUS}" in capsys.readouterr().out
    assert c.execute(text("SELECT address_source FROM nts_notices WHERE ts_number = 'REC-2628907'")
                     ).scalar() == "assessor_situs"


@pytest.mark.parametrize("status,refused", [("scraping", True), ("pending", True), ("done", False),
                                            ("failed", False)])
def test_refresh_waits_for_a_running_trustee_sale_job(db, tmp_path, status, refused):
    """A running job may hold the old address and deliver it after the refresh commits;
    the next run would deliver the same sale again under the new one (Codex P1)."""
    from src.utils.crypto import blind_index

    doc_id = _stage(db, tmp_path, "9910008", "2628908")
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(property_address=_PRINTED))
    uid, cfg = str(uuid.uuid4()), str(uuid.uuid4())
    email = f"ts-{uuid.uuid4().hex[:8]}@example.test"
    db.execute(text("INSERT INTO users (id, email, password_hash, email_hmac, plan) VALUES (:u, :e, 'x', :h, 'pro')"),
               {"u": uid, "e": email, "h": blind_index(email)})
    db.execute(text("INSERT INTO scraper_configs (id, user_id, name, county, state, record_type, fields, "
                    "enrichment, schedule, deliver) VALUES (:c, :u, 'ts', 'Chelan', 'WA', 'trustee_sale', "
                    "'[]', '[]', '{}', '{}')"), {"c": cfg, "u": uid})
    db.execute(text("INSERT INTO jobs (id, user_id, scraper_config_id, status) VALUES (gen_random_uuid(), :u, :c, :s)"),
               {"u": uid, "c": cfg, "s": status})
    if refused:
        with pytest.raises(ReviewError, match="trustee-sale run is in progress"):
            refresh_notice_address(db, doc_id, reviewer="op-1", situs=_SITUS)
        assert _notice(db, "2628908").property_address == _PRINTED
    else:
        refresh_notice_address(db, doc_id, reviewer="op-1", situs=_SITUS)
        assert _notice(db, "2628908").property_address == _SITUS
