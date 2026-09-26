"""Two small access gates from the 2026-09-25 security audit.

C-1: GET /scrapers/connectors?include_all=true named every broken county
connector (with its portal URLs and health) to any anonymous caller. The
default picker view stays public; include_all is admin-only.

D-1: the emailed download link (a 48h token) loaded its user without checking
is_active, so a deactivated account kept downloading through old links.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from src.api.auth import create_secure_token, hash_password
from src.api.download_tokens import mint_download_token
from src.db.models import CountyConnector, Job, Result, ScraperConfig, User


async def _user(db, **extra) -> User:
    u = User(
        id=str(uuid.uuid4()), email=f"p3_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"), plan="business",
        records_used=0, records_limit=5000, **extra,
    )
    db.add(u)
    await db.commit()
    return u


@pytest.fixture
async def down_connector(db):
    c = CountyConnector(
        id=str(uuid.uuid4()), county=f"brokenco{uuid.uuid4().hex[:4]}", state="WA",
        record_types=["probate"], scraper_class="x.Broken", base_url="https://broken.example.gov",
        health_status="down", active=True,
    )
    db.add(c)
    await db.commit()
    return c


async def test_the_public_picker_view_still_needs_no_login(client: AsyncClient, down_connector):
    r = await client.get("/scrapers/connectors")
    assert r.status_code == 200
    assert down_connector.county not in r.text  # down connectors were never in it


async def test_include_all_is_refused_to_anonymous_callers(client: AsyncClient, down_connector):
    r = await client.get("/scrapers/connectors?include_all=true")
    assert r.status_code == 401
    assert down_connector.county not in r.text


async def test_include_all_is_hidden_from_non_admins(client: AsyncClient, db, down_connector):
    user = await _user(db)
    r = await client.get(
        "/scrapers/connectors?include_all=true",
        headers={"Authorization": f"Bearer {create_secure_token(user.id)}"},
    )
    assert r.status_code == 404
    assert down_connector.county not in r.text


async def test_include_all_still_works_for_admins(client: AsyncClient, db, down_connector):
    admin = await _user(db, is_admin=True, mfa_enabled=True)
    r = await client.get(
        "/scrapers/connectors?include_all=true",
        headers={"Authorization": f"Bearer {create_secure_token(admin.id)}"},
    )
    assert r.status_code == 200
    assert down_connector.county in r.text


async def test_a_deactivated_account_cannot_use_an_emailed_download_link(client: AsyncClient, db):
    user = await _user(db)
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="cfg", county="pierce", state="WA",
        record_type="probate", fields=["party_name"], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.flush()
    job = Job(
        id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=config.id,
        status="done", trigger="manual", export_key=f"exports/{user.id}/x.csv",
    )
    db.add(job)
    await db.flush()
    db.add(Result(
        id=str(uuid.uuid4()), job_id=job.id, user_id=user.id,
        party_name="LINK OWNER", property_address="1 Main St, Tacoma, WA 98402",
    ))
    await db.commit()

    link = mint_download_token(str(user.id), job.id, ttl_seconds=172800)
    ok = await client.get(f"/jobs/{job.id}/download?token={link}")
    assert ok.status_code == 200 and "LINK OWNER" in ok.text  # positive control

    await db.execute(update(User).where(User.id == user.id).values(is_active=False))
    await db.commit()
    link = mint_download_token(str(user.id), job.id, ttl_seconds=172800)
    refused = await client.get(f"/jobs/{job.id}/download?token={link}")
    assert refused.status_code == 401
    assert "LINK OWNER" not in refused.text
