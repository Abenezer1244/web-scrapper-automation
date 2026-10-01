"""AI mode removal, Phase 2b: every writer stores scraper_mode 'template', and
migration 108 moves the existing 'ai' rows.

2a (live) taught every reader both names. 2b switches the writers and migrates
the rows; an incoming 'ai' is still accepted (the admin page sends it until
Phase 4) but is stored as 'template' and logged, so 2c can retire it once the
logs and the table both show zero.

Real DB rows, the real admin step-up gate, and the real migration module.
"""
import importlib.util
import logging
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


async def test_a_legacy_ai_input_is_stored_as_template_and_logged(
    client: AsyncClient, db, admin_headers, counties, caplog,
):
    """REGRESSION: main stored 'ai' and logged nothing. The admin page sends 'ai'
    until Phase 4; 2c retires the alias only after this log line stays at zero."""
    county = counties()
    with caplog.at_level(logging.INFO, logger="api.scrapers"):
        r = await _create(client, admin_headers, county, scraper_mode="ai")
    assert r.status_code == 201, r.text
    assert await _stored_mode(db, county) == "template"
    legacy = [rec for rec in caplog.records
              if rec.name == "api.scrapers" and "legacy scraper_mode 'ai'" in rec.getMessage()]
    assert len(legacy) == 1 and county in legacy[0].getMessage()


async def test_an_explicit_template_input_is_stored_and_not_logged_as_legacy(
    client: AsyncClient, db, admin_headers, counties, caplog,
):
    """REGRESSION: main refused 'template' with 400 (it accepted only 'ai')."""
    county = counties()
    with caplog.at_level(logging.INFO, logger="api.scrapers"):
        r = await _create(client, admin_headers, county, scraper_mode="template")
    assert r.status_code == 201, r.text
    assert await _stored_mode(db, county) == "template"
    assert not [rec for rec in caplog.records if "legacy scraper_mode" in rec.getMessage()]


@pytest.mark.parametrize("mode", ["manual", "AI", " ai", "Template", "bogus"])
async def test_any_other_mode_is_refused(client: AsyncClient, db, admin_headers, counties, mode):
    """CONTROL: the route creates template connectors only, and the alias is exactly 'ai'."""
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


# ─── migration 108 ────────────────────────────────────────────────────────────

def _mig108():
    path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions")
                .glob("108_*.py"))
    spec = importlib.util.spec_from_file_location("_mig108", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


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
    """An unknown mode is data no reader understands: fail the boot, change nothing."""
    mig = _mig108()
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed(conn, f"m108x{uuid.uuid4().hex[:6]}", "bogus")
            with Operations.context(MigrationContext.configure(conn)), \
                    pytest.raises(RuntimeError, match="unknown scraper_mode"):
                mig.upgrade()
        finally:
            trans.rollback()


def test_after_108_every_template_url_shape_still_resolves():
    """The 17 production template connectors keep their scraper after the rename:
    one base_url per platform family they use, stored as 'template'."""
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
