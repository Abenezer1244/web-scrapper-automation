"""Chelan recorded NTS: operator review turns a staged OCR read into a notice (4a-3a).

Real test database, one outer transaction rolled back at the end (the CLI's commits
release savepoints, as in test_privacy_admin_cli). Staged rows come from the real
staging path (stand-in tesseract, Chelan's real detail page, names redacted).
"""
from __future__ import annotations

import importlib.util
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.db.models import NtsNotice
from src.db.models import NtsRecorderDocument as Doc
from src.db.session import sync_engine
from src.utils.crypto import blind_index
from src.workers.nts_crawler import (
    CHELAN_RECORDER_SOURCE,
    ReviewError,
    approve_recorder_document,
    reject_recorder_document,
    stage_recorder_document,
)
from tests.test_nts_chelan_recorder_stage import _DETAIL
from tests.test_nts_recorder_ocr import _OK, _tool, recorder_pdf
from tests.test_privacy_authorize import _keyring  # noqa: F401 -- autouse keyring

_SPEC = importlib.util.spec_from_file_location(
    "nts_review", Path(__file__).resolve().parents[1] / "scripts" / "nts_review.py")
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)

_APN, _DT = "222014846080", "2531058"   # the fixture detail page's own recording facts
_SALE = date.today() + timedelta(days=60)


@pytest.fixture
def c():
    with sync_engine.connect() as conn:
        outer = conn.begin()

        @contextmanager
        def _session():
            s = Session(bind=conn, join_transaction_mode="create_savepoint")
            try:
                yield s
            finally:
                s.close()

        conn.session_factory = _session
        try:
            yield conn
        finally:
            outer.rollback()


@pytest.fixture
def db(c):
    with c.session_factory() as s:
        yield s


def _stage(db, tmp_path, item: str, instrument: str, recorded: str = "09/28/2026") -> str:
    doc = {"item_id": item, "instrument": instrument, "doc_code": "NTS", "record_date": recorded}
    assert stage_recorder_document(db, county="chelan", state="WA", doc=doc, detail_html=_DETAIL,
                                   pdf=recorder_pdf(1), tesseract=_tool(tmp_path, "tess_ok", _OK)) == "pending_review"
    db.flush()
    return str(db.execute(select(Doc.id).where(Doc.transaction_item_id == item)).scalar_one())


def _values(**kw) -> dict:
    return {"auction_date": _SALE, "parcel": _APN, "grantor": "DOE, JANE", **kw}


def _notice(db, instrument: str) -> NtsNotice:
    return db.execute(select(NtsNotice).where(NtsNotice.source == CHELAN_RECORDER_SOURCE,
                                              NtsNotice.ts_number == f"REC-{instrument}")).scalar_one()


def test_approval_writes_the_confirmed_notice_and_drops_the_ocr_text(db, tmp_path):
    doc_id = _stage(db, tmp_path, "9900001", "2627524")
    nid = approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(
        principal_owing=Decimal("300674.39"), principal_owing_basis="sum_owing",
        property_address="123  MAIN ST, WENATCHEE, WA 98801", auction_time="9:00 AM"))
    db.flush()
    n = _notice(db, "2627524")
    assert str(n.id) == nid
    assert (n.county, n.state, n.parcel, n.auction_date, n.grantor) == ("chelan", "WA", _APN, _SALE, "DOE, JANE")
    assert n.is_active and n.cancelled_at is None and n.fetched_at is not None
    assert n.principal_owing == Decimal("300674.39") and n.principal_owing_basis == "sum_owing"
    assert n.source_url.endswith("/details/documentdetails/9900001/1/1/11")
    d = db.get(Doc, doc_id)
    assert (d.status, d.ocr_text, d.reviewed_by, str(d.nts_notice_id)) == ("approved", None, "op-1", nid)
    assert "DOE" not in str(d.fields) and d.fields["approved"]["parcel"] == _APN


@pytest.mark.parametrize("bad,match", [
    ({"parcel": "222014846081"}, "own APNs"),
    ({"auction_date": None}, "sale date"),
    ({"grantor": "  "}, "grantor"),
    ({"principal_owing": Decimal("10")}, "basis"),
    ({"principal_owing": Decimal("-1"), "principal_owing_basis": "principal"}, "positive"),
    ({"principal_owing": Decimal("NaN"), "principal_owing_basis": "principal"}, "positive"),
    ({"principal_owing": Decimal("Infinity"), "principal_owing_basis": "principal"}, "positive"),
    ({"principal_owing": Decimal("10000000000"), "principal_owing_basis": "principal"}, "positive"),
    ({"principal_owing": Decimal("1.005"), "principal_owing_basis": "principal"}, "cents"),
], ids=["foreign-parcel", "no-date", "no-grantor", "amount-without-basis", "negative-amount",
        "nan", "infinity", "overflow", "fraction-of-a-cent"])
def test_an_approval_the_image_does_not_support_is_refused_and_writes_nothing(db, tmp_path, bad, match):
    doc_id = _stage(db, tmp_path, "9900002", "2627525")
    with pytest.raises(ReviewError, match=match):
        approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(**bad))
    assert db.get(Doc, doc_id).status == "pending_review"
    assert db.execute(select(NtsNotice).where(NtsNotice.ts_number == "REC-2627525")).first() is None


def test_a_decided_document_cannot_be_decided_again(db, tmp_path):
    doc_id = _stage(db, tmp_path, "9900003", "2627526")
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values())
    with pytest.raises(ReviewError, match="not pending review"):
        approve_recorder_document(db, doc_id, reviewer="op-2", values=_values())
    with pytest.raises(ReviewError, match="not pending review"):
        reject_recorder_document(db, doc_id, reviewer="op-2", reason="unreadable")


def test_a_sale_already_past_is_stored_but_never_active(db, tmp_path):
    doc_id = _stage(db, tmp_path, "9900004", "2627527")
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values(auction_date=date.today() - timedelta(days=1)))
    assert _notice(db, "2627527").is_active is False


def test_a_second_notice_on_the_same_deed_of_trust_needs_the_reviewers_call(db, tmp_path):
    first = _stage(db, tmp_path, "9900005", "2627528", "09/01/2026")
    second = _stage(db, tmp_path, "9900006", "2627529", "10/01/2026")
    approve_recorder_document(db, first, reviewer="op-1", values=_values())
    with pytest.raises(ReviewError, match="2627528 share this deed of trust"):
        approve_recorder_document(db, second, reviewer="op-1", values=_values())
    # A new foreclosure on the same loan: both notices stand.
    approve_recorder_document(db, second, reviewer="op-1", values=_values(), independent=True)
    assert _notice(db, "2627528").is_active and _notice(db, "2627529").is_active


def test_a_recording_citing_several_deeds_of_trust_is_checked_against_each(db, tmp_path):
    first = _stage(db, tmp_path, "9900014", "2627537", "09/01/2026")
    second = _stage(db, tmp_path, "9900015", "2627538", "10/01/2026")
    approve_recorder_document(db, first, reviewer="op-1", values=_values())
    db.flush()
    # Staging keeps a single DT column only when there is exactly one; the set is in fields.
    db.execute(text("UPDATE nts_recorder_documents SET related_deed_of_trust = NULL, "
                    "fields = jsonb_set(fields::jsonb, '{related_deeds_of_trust}', CAST(:d AS jsonb)) WHERE id = :i"),
               {"d": f'["2400001", "{_DT}"]', "i": second})
    db.expire_all()
    with pytest.raises(ReviewError, match="share this deed of trust"):
        approve_recorder_document(db, second, reviewer="op-1", values=_values())


def test_an_amended_notice_cancels_the_one_it_replaces_and_frees_its_leads(db, tmp_path):
    first = _stage(db, tmp_path, "9900007", "2627530", "09/01/2026")
    second = _stage(db, tmp_path, "9900008", "2627531", "10/01/2026")
    old_id = approve_recorder_document(db, first, reviewer="op-1", values=_values())
    db.flush()
    # A delivered lead the matcher stamped with the first notice's sale.
    uid, cfg, job, res = (str(uuid.uuid4()) for _ in range(4))
    email = f"chelan-{uid[:8]}@example.test"
    db.execute(text("INSERT INTO users (id, email, password_hash, email_hmac, plan) VALUES (:u, :e, 'x', :h, 'pro')"),
               {"u": uid, "e": email, "h": blind_index(email)})
    db.execute(text("INSERT INTO scraper_configs (id, user_id, name, county, state, record_type, fields, "
                    "enrichment, schedule, deliver) VALUES (:c, :u, 'chelan nts', 'chelan', 'WA', "
                    "'pre_foreclosure', '[]', '[]', '{}', '{}')"), {"c": cfg, "u": uid})
    db.execute(text("INSERT INTO jobs (id, user_id, scraper_config_id, status) VALUES (:j, :u, :c, 'done')"),
               {"j": job, "u": uid, "c": cfg})
    db.execute(text("INSERT INTO results (id, job_id, user_id, parcel_id, auction_date, nts_notice_id, "
                    "enrichment_data) VALUES (:r, :j, :u, :p, :d, :n, '{\"nts\": {}}')"),
               {"r": res, "j": job, "u": uid, "p": _APN, "d": _SALE, "n": old_id})

    approve_recorder_document(db, second, reviewer="op-2", values=_values(auction_date=_SALE + timedelta(days=14)),
                              supersedes="2627530")
    db.flush()
    db.expire_all()
    old = _notice(db, "2627530")
    assert old.is_active is False and old.cancelled_at is not None
    assert _notice(db, "2627531").is_active
    assert db.get(Doc, first).status == "superseded"
    lead = db.execute(text("SELECT auction_date, nts_notice_id, enrichment_data FROM results WHERE id = :r"),
                      {"r": res}).one()
    assert lead.auction_date is None and lead.nts_notice_id is None and "nts" not in lead.enrichment_data


def test_only_a_later_recording_can_supersede(db, tmp_path):
    later = _stage(db, tmp_path, "9900009", "2627533", "10/01/2026")
    earlier = _stage(db, tmp_path, "9900010", "2627532", "09/01/2026")
    approve_recorder_document(db, later, reviewer="op-1", values=_values())
    with pytest.raises(ReviewError, match="earlier recording"):
        approve_recorder_document(db, earlier, reviewer="op-1", values=_values(), supersedes="2627533")
    with pytest.raises(ReviewError, match="no approved, live notice"):
        approve_recorder_document(db, earlier, reviewer="op-1", values=_values(), supersedes="1")


def test_rejection_writes_no_notice_and_keeps_only_the_recordings_facts(db, tmp_path):
    doc_id = _stage(db, tmp_path, "9900011", "2627534")
    # Free text is refused: it could carry the borrower's name past the OCR text's deletion.
    for reason in (" ", "wrong borrower, Jane Doe"):
        with pytest.raises(ReviewError, match="must be one of"):
            reject_recorder_document(db, doc_id, reviewer="op-1", reason=reason)
    reject_recorder_document(db, doc_id, reviewer="op-1", reason="not_a_trustee_sale_notice")
    d = db.get(Doc, doc_id)
    assert (d.status, d.ocr_text, d.decision_note) == ("rejected", None, "not_a_trustee_sale_notice")
    assert set(d.fields) == {"recording_apns", "related_deeds_of_trust"}
    assert db.execute(select(NtsNotice).where(NtsNotice.ts_number == "REC-2627534")).first() is None


# ── privacy and concurrency ──

def _withholding_rule(db, monkeypatch, subject: str) -> str:
    import src.privacy.authorize as authorize_mod
    from src.privacy import keys
    from src.privacy.authorize import build_snapshot
    from tests.test_privacy_authorize import _history

    request_id = db.execute(text(
        "INSERT INTO privacy_requests (kind, channel, deadline_at) "
        "VALUES ('opt_out', 'web', now() + interval '45 days') RETURNING id")).scalar()
    rule = str(db.execute(text(
        "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, anchor_hmac, "
        "subject_kind, subject_hmac, scope, key_version, normalization_version, state) "
        "VALUES (gen_random_uuid(), :r, 'property', :h, 'person', :h, 'subject', 'v1', "
        "'n3', 'pending') RETURNING id"), {"r": request_id, "h": "c" * 64}).scalar())
    rows = _history(["pending", "active"],
                    anchor=("property", keys.property_anchor_value(_APN, "chelan", "WA")),
                    subject=("person", subject))
    rows[1]["id"] = rule
    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", lambda _db: build_snapshot(rows))
    return rule


def test_a_borrower_with_an_opt_out_is_approved_but_never_named_or_active(db, tmp_path, monkeypatch):
    doc_id = _stage(db, tmp_path, "9900016", "2627539")
    rule = _withholding_rule(db, monkeypatch, "doe|jane")
    approve_recorder_document(db, doc_id, reviewer="op-1", values=_values())
    n = _notice(db, "2627539")
    assert (n.grantor, n.is_active, str(n.privacy_suppression_id)) == (None, False, rule)
    assert db.get(Doc, doc_id).status == "approved"


def test_unreadable_privacy_rules_stop_the_approval_before_anything_is_written(db, tmp_path, monkeypatch):
    import src.privacy.authorize as authorize_mod
    from src.privacy.authorize import RuleSnapshotError

    doc_id = _stage(db, tmp_path, "9900017", "2627540")
    db.commit()

    def _broken(_db):
        raise RuleSnapshotError("keys unavailable")

    monkeypatch.setattr(authorize_mod, "load_rule_snapshot", _broken)
    with pytest.raises(RuleSnapshotError):
        approve_recorder_document(db, doc_id, reviewer="op-1", values=_values())
    db.rollback()
    assert db.get(Doc, doc_id).status == "pending_review" and db.get(Doc, doc_id).ocr_text
    assert db.execute(select(NtsNotice).where(NtsNotice.ts_number == "REC-2627540")).first() is None


def test_two_approvals_at_once_are_serialized_not_both_decided_blind(tmp_path):
    """Separate connections and real commits: while one approval holds the decision,
    a second (another operator, same deed of trust) waits instead of reading stale state."""
    from sqlalchemy.exc import OperationalError

    from src.db.session import system_sync_session

    items = {"9900018": "2627541", "9900019": "2627542"}
    try:
        with system_sync_session() as s:
            first = _stage(s, tmp_path, "9900018", "2627541", "09/01/2026")
            second = _stage(s, tmp_path, "9900019", "2627542", "10/01/2026")
            s.commit()
        with system_sync_session() as a, system_sync_session() as b:
            approve_recorder_document(a, first, reviewer="op-a", values=_values())   # not committed
            b.execute(text("SET LOCAL lock_timeout = '1s'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                approve_recorder_document(b, second, reviewer="op-b", values=_values())
            b.rollback()
            a.commit()
            # Once the first is decided, the second sees it and must say which it is.
            with pytest.raises(ReviewError, match="2627541 share this deed of trust"):
                approve_recorder_document(b, second, reviewer="op-b", values=_values())
            b.rollback()
    finally:
        with system_sync_session() as s:
            s.execute(text("DELETE FROM nts_recorder_documents WHERE transaction_item_id = ANY(:i)"),
                      {"i": list(items)})
            s.execute(text("DELETE FROM nts_notices WHERE source = :s AND ts_number = ANY(:k)"),
                      {"s": CHELAN_RECORDER_SOURCE, "k": [f"REC-{v}" for v in items.values()]})
            s.commit()


# ── the CLI ──

def _operator(c, *, mfa: bool = True) -> str:
    email = f"op-{uuid.uuid4().hex[:8]}@example.test"
    c.execute(text("INSERT INTO users (id, email, password_hash, email_hmac, is_admin, mfa_enabled) "
                   "VALUES (gen_random_uuid(), :e, 'x', :h, true, :m)"),
              {"e": email, "h": blind_index(email), "m": mfa})
    return email


def _run(c, email, *argv):
    cli.main(["--operator-email", email, *argv], auth_factory=c.session_factory,
             session_factory=c.session_factory)


def test_the_cli_approves_as_the_checked_operator(c, db, tmp_path, capsys):
    doc_id = _stage(db, tmp_path, "9900012", "2627535")
    db.commit()
    email = _operator(c)
    _run(c, email, "list")
    assert "2627535" in capsys.readouterr().out
    _run(c, email, "show", "--doc", doc_id)
    assert "/Image/DocumentPdfAllPages/9900012" in capsys.readouterr().out
    _run(c, email, "approve", "--doc", doc_id, "--sale-date", _SALE.isoformat(), "--parcel", _APN,
         "--grantor", "DOE, JANE", "--amount", "$300,674.39", "--amount-basis", "sum_owing")
    assert "approved: notice" in capsys.readouterr().out
    uid = c.execute(text("SELECT id FROM users WHERE email_hmac = :h"), {"h": blind_index(email)}).scalar()
    d = c.execute(text("SELECT status, reviewed_by FROM nts_recorder_documents WHERE id = :i"), {"i": doc_id}).one()
    assert (d.status, d.reviewed_by) == ("approved", str(uid))


def test_the_cli_refuses_an_operator_without_mfa(c, db, tmp_path):
    from sqlalchemy.exc import DBAPIError

    email = _operator(c, mfa=False)
    with pytest.raises(DBAPIError, match="admin with MFA"):
        _run(c, email, "list")


def test_the_cli_reports_a_refused_approval_without_writing(c, db, tmp_path):
    doc_id = _stage(db, tmp_path, "9900013", "2627536")
    db.commit()
    email = _operator(c)
    with pytest.raises(SystemExit, match="refused: the parcel"):
        _run(c, email, "approve", "--doc", doc_id, "--sale-date", _SALE.isoformat(),
             "--parcel", "000000000000", "--grantor", "DOE, JANE")
    assert c.execute(text("SELECT status FROM nts_recorder_documents WHERE id = :i"),
                     {"i": doc_id}).scalar() == "pending_review"
