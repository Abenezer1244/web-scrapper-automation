"""Stale pre-foreclosure document-type selections (#625 follow-up).

A saved selection that names a type its county no longer offers must fail with a
message the customer can act on (never broaden, never retry), and migration 162 must
repair the Chelan selections #625 left stale without touching anything else.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from src.db.session import sync_engine
from src.scrapers.doc_types import (
    _AVAILABILITY,
    _EAGLEWEB_COUNTIES,
    StaleDocTypeSelectionError,
    availability_for,
    canonical_tokens_or_raise,
    validate_selection,
)
from src.utils.crypto import blind_index

_MIG = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "162_repair_chelan_doc_type_selections.py"
_OLD_FOUR = ["notice_of_default", "notice_of_trustee_sale", "lis_pendens", "foreclosure"]


def _mig():
    spec = importlib.util.spec_from_file_location("_mig162", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- the strict check --------------------------------------------------------------

def test_stale_types_raise_a_typed_error_naming_every_one():
    with pytest.raises(StaleDocTypeSelectionError) as exc:
        canonical_tokens_or_raise("chelan", "wa", _OLD_FOUR)
    err = exc.value
    assert err.stale == ["notice_of_default", "foreclosure"]
    assert isinstance(err, ValueError)  # callers guarding ValueError keep their behaviour
    assert err.public_message.startswith(
        "Chelan County no longer offers Notice of Default or Foreclosure as a document type.")
    assert err.public_message.endswith("Edit this scraper to choose from the current list, or contact support.")
    assert "—" not in err.public_message  # no em dash in customer copy


def test_a_valid_selection_still_maps_and_unrelated_failures_stay_plain():
    assert canonical_tokens_or_raise("chelan", "wa", ["notice_of_trustee_sale", "lis_pendens"]) == [
        "NTS", "NTSCOM", "LP"]
    for county, sel in (("chelan", []), ("nowhere", ["lis_pendens"])):
        with pytest.raises(ValueError) as exc:
            canonical_tokens_or_raise(county, "wa", sel)
        assert not isinstance(exc.value, StaleDocTypeSelectionError)


def test_every_type_a_customer_can_save_is_one_the_scraper_can_run():
    """Codex: the API validates against 'available', execution maps 'tokens'; a type in
    the first but not the second would save fine and then fail every run."""
    keys = list(_AVAILABILITY) + [(c, "wa") for c in _EAGLEWEB_COUNTIES]
    for county, state in keys:
        a = availability_for(county, state)
        if not a or not a.get("supported_for_selection"):
            continue
        for d in a["available"]:
            assert validate_selection(county, state, [d]) == (True, None)
            canonical_tokens_or_raise(county, state, [d])


def test_the_worker_fails_a_stale_selection_without_retrying():
    """Handled beside SourceUnavailableError, before the transient-retry branch, with the
    customer copy and still reported (a registry change shipped without a repair)."""
    import src.workers.tasks as tasks

    src = inspect.getsource(tasks)
    branch = src.index("isinstance(exc, StaleDocTypeSelectionError)")
    assert src.index("isinstance(exc, SourceUnavailableError)") < branch
    assert branch < src.index("Transient portal hiccup")
    assert "_fail_attempt(exc.public_message, report=True)\n                return" in src[branch:]


# -- migration 162 -----------------------------------------------------------------

@pytest.fixture
def conn():
    with sync_engine.connect() as c:
        outer = c.begin()
        try:
            yield c
        finally:
            outer.rollback()


def _user(c) -> str:
    uid, email = str(uuid.uuid4()), f"dt-{uuid.uuid4().hex[:10]}@example.test"
    c.execute(text("INSERT INTO users (id, email, password_hash, email_hmac, plan) "
                   "VALUES (:u, :e, 'x', :h, 'pro')"), {"u": uid, "e": email, "h": blind_index(email)})
    return uid


def _config(c, uid, county, record_type, doc_types) -> str:
    cid = str(uuid.uuid4())
    c.execute(text("INSERT INTO scraper_configs (id, user_id, name, county, state, record_type, fields, "
                   "enrichment, schedule, deliver, doc_types, updated_at) VALUES (:c, :u, 'dt', :co, 'WA', "
                   ":rt, '[]', '[]', '{}', '{}', CAST(:dt AS json), now() - interval '1 day')"),
              {"c": cid, "u": uid, "co": county, "rt": record_type,
               "dt": None if doc_types is None else json.dumps(doc_types)})
    return cid


def _run_162(c):
    with Operations.context(MigrationContext.configure(c)):
        _mig().upgrade()


def _row(c, cid):
    return c.execute(text("SELECT doc_types::text AS dt, updated_at > now() - interval '1 hour' AS bumped "
                          "FROM scraper_configs WHERE id = :c"), {"c": cid}).one()


def test_162_is_frozen_and_chained():
    mig = _mig()
    assert (mig.revision, mig.down_revision) == ("162", "161")
    assert set(mig._ALLOWED) == set(availability_for("chelan", "wa")["available"])
    assert "from src." not in _MIG.read_text(encoding="utf-8")  # no live-registry import


def test_162_repairs_stale_chelan_selections_and_nothing_else(conn):
    uid = _user(conn)
    stale = _config(conn, uid, "chelan", "pre_foreclosure", _OLD_FOUR)
    reordered = _config(conn, uid, "Chelan", "pre_foreclosure", ["lis_pendens", "foreclosure"])
    nothing_valid = _config(conn, uid, "chelan", "pre_foreclosure", ["notice_of_default"])
    already_ok = _config(conn, uid, "chelan", "pre_foreclosure", ["notice_of_trustee_sale"])
    sql_null = _config(conn, uid, "chelan", "pre_foreclosure", None)
    json_null = _config(conn, uid, "chelan", "pre_foreclosure", None)
    conn.execute(text("UPDATE scraper_configs SET doc_types = 'null'::json WHERE id = :c"), {"c": json_null})
    other_county = _config(conn, uid, "douglas", "pre_foreclosure", _OLD_FOUR)
    other_type = _config(conn, uid, "chelan", "probate", None)

    _run_162(conn)

    assert json.loads(_row(conn, stale).dt) == ["notice_of_trustee_sale", "lis_pendens"]
    assert _row(conn, stale).bumped
    assert json.loads(_row(conn, reordered).dt) == ["lis_pendens"]
    for cid, want in ((nothing_valid, ["notice_of_default"]), (already_ok, ["notice_of_trustee_sale"]),
                      (other_county, _OLD_FOUR)):
        r = _row(conn, cid)
        assert json.loads(r.dt) == want and not r.bumped
    assert _row(conn, sql_null).dt is None
    assert _row(conn, json_null).dt == "null"
    assert not _row(conn, other_type).bumped

    audits = conn.execute(text("SELECT user_id::text, path, detail FROM audit_events "
                               "WHERE event = 'scraper_doc_types_repaired' AND user_id = :u"),
                          {"u": uid}).all()
    assert {a.detail.split()[0] for a in audits} == {f"config_id={stale}", f"config_id={reordered}"}
    assert all(a.path == "alembic:162" and "system_repair" in a.detail for a in audits)

    _run_162(conn)  # a replay changes nothing and audits nothing new
    assert conn.execute(text("SELECT count(*) FROM audit_events WHERE event = 'scraper_doc_types_repaired' "
                             "AND user_id = :u"), {"u": uid}).scalar_one() == 2


# -- GET /scrapers/{id} carries the county's current choice ----------------------

async def _saved_config(user_id: str, county: str, record_type: str, doc_types):
    import src.db.session as _db_session
    from src.db.models import ScraperConfig

    async with _db_session.AsyncSessionLocal() as s:
        cfg = ScraperConfig(
            id=str(uuid.uuid4()), user_id=user_id, name="dt", county=county, state="WA",
            record_type=record_type, fields={"party_name": True}, enrichment={},
            schedule={"frequency": "manual"}, deliver={"formats": ["csv"], "emails": []},
            doc_types=doc_types,
        )
        s.add(cfg)
        await s.commit()
        return cfg.id


async def test_get_scraper_returns_the_countys_current_options(client, db, starter_user, starter_token):
    """The Edit page reads the options from the scraper itself, so it can repair a
    stale selection even when the county's connector is hidden from the connectors
    list (down or unknown health)."""
    auth = {"Authorization": f"Bearer {starter_token}"}
    pf = await _saved_config(starter_user.id, "chelan", "pre_foreclosure", _OLD_FOUR)
    body = (await client.get(f"/scrapers/{pf}", headers=auth)).json()
    assert body["doc_type_options"] == {"notice_of_trustee_sale": "Notice of Trustee Sale",
                                        "lis_pendens": "Lis Pendens"}
    assert body["doc_type_confidence"] == "keyword"
    assert body["doc_types"] == _OLD_FOUR  # the stored selection is reported as is

    probate = await _saved_config(starter_user.id, "chelan", "probate", None)
    body = (await client.get(f"/scrapers/{probate}", headers=auth)).json()
    assert body["doc_type_options"] is None and body["doc_type_confidence"] is None
