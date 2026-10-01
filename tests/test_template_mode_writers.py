"""Every writer stores scraper_mode 'template' (AI-mode removal 2b, migration 108),
and the old name 'ai' is refused (2c): by the API, and by the database CHECK that
migration 109 adds after sweeping any straggler.

Real DB rows, the real admin step-up gate, and the real migration module.
"""
import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import AsyncClient
from sqlalchemy import delete, select, text

from src.api.auth import create_secure_token, hash_password
from src.db.models import CountyConnector, User
from src.db.session import sync_engine

# A real EagleWeb portal (Thurston, live in production), so the route's DNS-resolving
# SSRF check passes and has_template() matches. Each test uses a throwaway county name.
_EAGLEWEB_URL = "https://eagleweb.co.thurston.wa.us/thurstonrecorder/web/"


@pytest.fixture
async def admin_headers(db) -> dict[str, str]:
    """An admin with MFA enrolled and a fresh MFA-backed session (the step-up gate)."""
    admin = User(
        id=str(uuid.uuid4()), email=f"tmplw_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"), plan="business",
        records_used=0, records_limit=5000, is_admin=True, mfa_enabled=True,
    )
    db.add(admin)
    await db.commit()
    return {"Authorization": f"Bearer {create_secure_token(admin.id, amr=['pwd', 'mfa'])}"}


@pytest.fixture
async def counties(db):
    """Throwaway county names; every connector made under one is deleted afterwards."""
    made: list[str] = []

    def name() -> str:
        made.append(f"tmplw{uuid.uuid4().hex[:8]}")
        return made[-1]

    yield name
    await db.execute(delete(CountyConnector).where(CountyConnector.county.in_(made)))
    await db.commit()


async def _create(client, headers, county, **mode):
    return await client.post(
        "/scrapers/connectors", headers=headers,
        json={"county": county, "state": "WA", "record_types": ["probate"],
              "base_url": _EAGLEWEB_URL, **mode},
    )


async def _stored_mode(db, county) -> str:
    db.expire_all()
    return (await db.execute(
        select(CountyConnector.scraper_mode).where(CountyConnector.county == county)
    )).scalar_one()


# ─── POST /scrapers/connectors ────────────────────────────────────────────────

async def test_a_connector_created_without_a_mode_is_stored_as_template(
    client: AsyncClient, db, admin_headers, counties,
):
    """REGRESSION: main stored the default 'ai'."""
    county = counties()
    r = await _create(client, admin_headers, county)
    assert r.status_code == 201, r.text
    assert r.json()["scraper_mode"] == "template"
    assert await _stored_mode(db, county) == "template"


async def test_an_explicit_template_input_is_stored(
    client: AsyncClient, db, admin_headers, counties,
):
    county = counties()
    r = await _create(client, admin_headers, county, scraper_mode="template")
    assert r.status_code == 201, r.text
    assert await _stored_mode(db, county) == "template"


@pytest.mark.parametrize("mode", ["ai", "manual", "AI", " ai", "Template", "bogus"])
async def test_any_other_mode_is_refused(client: AsyncClient, db, admin_headers, counties, mode):
    """REGRESSION for 'ai' (2b normalized it to 'template' and stored the row); CONTROL
    for the rest: the route creates template connectors only."""
    county = counties()
    r = await _create(client, admin_headers, county, scraper_mode=mode)
    assert r.status_code in (400, 422), r.text
    assert (await db.execute(
        select(CountyConnector).where(CountyConnector.county == county)
    )).scalar_one_or_none() is None


def test_the_openapi_contract_advertises_template_not_ai():
    from src.api.schemas import ConnectorCreate

    field = ConnectorCreate.model_json_schema()["properties"]["scraper_mode"]
    assert field["default"] == "template"
    assert "ai" not in field.get("description", "").lower().split()


# ─── defaults below the API ───────────────────────────────────────────────────

async def test_an_orm_insert_without_a_mode_defaults_to_template(db, counties):
    """REGRESSION: the model default was 'ai'."""
    county = counties()
    db.add(CountyConnector(
        id=str(uuid.uuid4()), county=county, state="WA", record_types=["probate"],
        scraper_class="", base_url=_EAGLEWEB_URL, health_status="unknown", active=False,
    ))
    await db.commit()
    assert await _stored_mode(db, county) == "template"


async def test_a_raw_sql_insert_without_a_mode_defaults_to_template(db, counties):
    """REGRESSION: the column's server default was 'ai' (migration 002)."""
    county = counties()
    await db.execute(text(
        "INSERT INTO county_connectors "
        "(id, county, state, record_types, scraper_class, render_mode, base_url, "
        " health_status, active) "
        "VALUES (:id, :c, 'WA', '[\"probate\"]', '', 'playwright', :u, 'unknown', false)"
    ), {"id": str(uuid.uuid4()), "c": county, "u": _EAGLEWEB_URL})
    await db.commit()
    assert await _stored_mode(db, county) == "template"


# ─── migrations 108 and 109 ───────────────────────────────────────────────────

def _mig(rev: str):
    path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions")
                .glob(f"{rev}_*.py"))
    spec = importlib.util.spec_from_file_location(f"_mig{rev}", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def _mig108():
    return _mig("108")


def _column_default(conn) -> str:
    return conn.execute(text(
        "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
        "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
        "WHERE d.adrelid = 'public.county_connectors'::regclass "
        "AND a.attname = 'scraper_mode'"
    )).scalar()


def _seed(conn, county, mode):
    conn.execute(text(
        "INSERT INTO county_connectors "
        "(id, county, state, record_types, scraper_class, scraper_mode, render_mode, "
        " base_url, health_status, active) "
        "VALUES (:id, :c, 'WA', '[\"probate\"]', :cls, :m, 'playwright', :u, 'healthy', true)"
    ), {"id": str(uuid.uuid4()), "c": county, "m": mode, "u": _EAGLEWEB_URL,
        "cls": "" if mode == "ai" else "src.scrapers.base_scraper.BridgeScraper"})


def _modes(conn, counties_) -> dict[str, str]:
    rows = conn.execute(text(
        "SELECT county, scraper_mode FROM county_connectors WHERE county = ANY(:c)"
    ), {"c": list(counties_)}).all()
    return {r.county: r.scraper_mode for r in rows}


def test_108_chains_on_107():
    mig = _mig108()
    assert (mig.revision, mig.down_revision) == ("108", "107")


def test_108_moves_ai_rows_to_template_and_leaves_manual_alone():
    """Inside ONE rolled-back transaction: the shared test DB is never changed."""
    mig = _mig108()
    ai_county, manual_county = f"m108a{uuid.uuid4().hex[:6]}", f"m108m{uuid.uuid4().hex[:6]}"
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                _mig("109").downgrade()  # drop 2c's CHECK so an 'ai' row can be seeded
                mig.downgrade()  # back to the 2a schema: server default 'ai'
                assert _column_default(conn) == "'ai'::character varying"
                _seed(conn, ai_county, "ai")
                _seed(conn, manual_county, "manual")
                mig.upgrade()
            assert _modes(conn, [ai_county, manual_county]) == {
                ai_county: "template", manual_county: "manual",
            }
            assert conn.execute(text(
                "SELECT count(*) FROM county_connectors WHERE scraper_mode = 'ai'"
            )).scalar() == 0
            assert _column_default(conn) == "'template'::character varying"
        finally:
            trans.rollback()


def test_108_aborts_on_an_unknown_mode():
    """An unknown mode is data no reader understands: the migration raises, so the
    API boot fails (start.sh) and Postgres rolls back its transactional DDL."""
    mig = _mig108()
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                _mig("109").downgrade()
            _seed(conn, f"m108x{uuid.uuid4().hex[:6]}", "bogus")
            with Operations.context(MigrationContext.configure(conn)), \
                    pytest.raises(RuntimeError, match="unknown scraper_mode"):
                mig.upgrade()
        finally:
            trans.rollback()


def test_after_108_every_template_url_shape_still_resolves():
    """Production base_url shapes stored as 'template' still resolve: EagleWeb on its
    own host and on tylerhost.net, AcclaimWeb, and Tyler SelfService. (Every
    production template connector's URL is pinned in test_doc_type_select_wiring.)"""
    from src.scrapers.registry import connector_scraper_class
    from src.scrapers.templates.acclaimweb import AcclaimWebScraper
    from src.scrapers.templates.eagleweb import EagleWebScraper
    from src.scrapers.templates.tyler_selfservice import TylerSelfServiceScraper

    def row(base_url):  # transient, never added to a session
        return CountyConnector(scraper_mode="template", scraper_class="", base_url=base_url)

    assert connector_scraper_class(row(_EAGLEWEB_URL)) is EagleWebScraper
    assert connector_scraper_class(
        row("https://grantcountywa-recorder.tylerhost.net/grantrecorder/web/")
    ) is EagleWebScraper
    assert connector_scraper_class(
        row("https://acclaim.co.chelan.wa.us/acclaimweb")
    ) is AcclaimWebScraper
    assert connector_scraper_class(
        row("https://okanogancountywa-web.tylerhost.net/Web")
    ) is TylerSelfServiceScraper

# ─── 2c: 'ai' is refused by the database (migration 109) ──────────────────────

def _check_state(conn):
    return conn.execute(text(
        "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
        "WHERE conname = 'ck_county_connectors_scraper_mode' "
        "AND conrelid = 'public.county_connectors'::regclass"
    )).one_or_none()


def test_109_chains_on_108():
    mig = _mig("109")
    assert (mig.revision, mig.down_revision) == ("109", "108")


@pytest.mark.parametrize("mode", ["ai", "bogus"])
def test_the_database_refuses_a_mode_other_than_template_or_manual(mode):
    """REGRESSION for 'ai': before 109 nothing below the API stopped a writer storing it."""
    from sqlalchemy.exc import IntegrityError

    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(IntegrityError, match="ck_county_connectors_scraper_mode"):
                _seed(conn, f"m109r{uuid.uuid4().hex[:6]}", mode)
        finally:
            trans.rollback()


def test_109_sweeps_a_straggler_then_validates_the_check():
    """An 'ai' row an old 2a API wrote during 2b's rolling deploy becomes 'template'."""
    mig = _mig("109")
    straggler = f"m109s{uuid.uuid4().hex[:6]}"
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                mig.downgrade()
                assert _check_state(conn) is None
                _seed(conn, straggler, "ai")
                mig.upgrade()
            assert _modes(conn, [straggler]) == {straggler: "template"}
            definition, validated = _check_state(conn)
            assert definition == mig._CHECK_DEF and validated is True
        finally:
            trans.rollback()


def test_109_is_replay_safe():
    """Run again on a database that already has the right CHECK: a no-op."""
    mig = _mig("109")
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            assert _check_state(conn) is not None  # head already has it
            with Operations.context(MigrationContext.configure(conn)):
                mig.upgrade()
            definition, validated = _check_state(conn)
            assert definition == mig._CHECK_DEF and validated is True
        finally:
            trans.rollback()


def test_109_aborts_on_an_unknown_mode():
    mig = _mig("109")
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                mig.downgrade()
            _seed(conn, f"m109x{uuid.uuid4().hex[:6]}", "bogus")
            with Operations.context(MigrationContext.configure(conn)), \
                    pytest.raises(RuntimeError, match="unknown scraper_mode"):
                mig.upgrade()
        finally:
            trans.rollback()


def test_109_refuses_an_impostor_constraint_of_the_same_name():
    """A same-named constraint with another definition is never silently replaced."""
    mig = _mig("109")
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                mig.downgrade()
            conn.execute(text(
                "ALTER TABLE county_connectors ADD CONSTRAINT ck_county_connectors_scraper_mode "
                "CHECK (scraper_mode <> '')"
            ))
            with Operations.context(MigrationContext.configure(conn)), \
                    pytest.raises(RuntimeError, match="Inspect it by hand"):
                mig.upgrade()
        finally:
            trans.rollback()
