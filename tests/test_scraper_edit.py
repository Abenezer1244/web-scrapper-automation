"""Tests for PATCH /scrapers/{id} — editing an existing scraper config in place.

Two layers:

* Pure-unit tests (no DB) cover the secret-preservation merge and the request
  schema. They are safe to run anywhere (`pytest -m "not integration"`).
* Integration tests (`@pytest.mark.integration`) exercise the endpoint end-to-end
  against a real Postgres via the `client` + `db` fixtures. They run in CI against
  a DEDICATED test database — do not run them against a production DATABASE_URL,
  because the conftest `db` teardown deletes all ScraperConfig/Job rows.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.api.routes.scrapers import _is_secret_placeholder, _merge_deliver
from src.api.schemas import DeliverConfig, DeliverUpdate, ScraperConfigUpdate

_REAL_SECRET = "a" * 30          # passes the 24-char minimum
_NEW_SECRET = "b" * 30
_HOOK = "https://example.com/hook"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─── Unit: _is_secret_placeholder ─────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("******", True),
    ("  **** ", True),     # stripped
    ("*", True),
    ("", False),
    ("   ", False),
    ("a" * 30, False),
    ("ab*", False),        # not ALL asterisks
])
def test_is_secret_placeholder(value, expected):
    assert _is_secret_placeholder(value) is expected


# ─── Unit: _merge_deliver secret preservation ─────────────────────────────────

def test_merge_deliver_keeps_secret_when_blank():
    """Omitted/blank secret → the stored secret is preserved, never None-clobbered."""
    stored = {"webhook_url": _HOOK, "webhook_secret": _REAL_SECRET}
    update = DeliverUpdate(webhook_url=_HOOK, webhook_secret="")
    merged = _merge_deliver(stored, update)
    assert merged.webhook_secret == _REAL_SECRET


def test_merge_deliver_keeps_secret_when_omitted():
    stored = {"webhook_url": _HOOK, "webhook_secret": _REAL_SECRET}
    update = DeliverUpdate(webhook_url=_HOOK)  # webhook_secret not provided
    merged = _merge_deliver(stored, update)
    assert merged.webhook_secret == _REAL_SECRET


def test_merge_deliver_replaces_secret_when_nonblank():
    stored = {"webhook_url": _HOOK, "webhook_secret": _REAL_SECRET}
    update = DeliverUpdate(webhook_url=_HOOK, webhook_secret=_NEW_SECRET)
    merged = _merge_deliver(stored, update)
    assert merged.webhook_secret == _NEW_SECRET


def test_merge_deliver_treats_asterisk_placeholder_as_keep():
    """A '******' echo from a UI must never overwrite the real stored secret."""
    stored = {"webhook_url": _HOOK, "webhook_secret": _REAL_SECRET}
    update = DeliverUpdate(webhook_url=_HOOK, webhook_secret="******")
    merged = _merge_deliver(stored, update)
    assert merged.webhook_secret == _REAL_SECRET


def test_merge_deliver_drops_orphan_secret_when_url_removed():
    """Removing the webhook URL drops its now-unreachable secret."""
    stored = {"webhook_url": _HOOK, "webhook_secret": _REAL_SECRET}
    update = DeliverUpdate(webhook_url="")  # destination removed
    merged = _merge_deliver(stored, update)
    assert merged.webhook_url is None
    assert merged.webhook_secret is None


def test_merge_deliver_drops_phoneburner_token_when_dialer_changed():
    stored = {
        "dialer_type": "phoneburner",
        "phoneburner_access_token": _REAL_SECRET,
        "phoneburner_owner_id": "42",
    }
    update = DeliverUpdate()  # dialer_type omitted → defaults None
    merged = _merge_deliver(stored, update)
    assert merged.dialer_type is None
    assert merged.phoneburner_access_token is None
    assert merged.phoneburner_owner_id is None


def test_merge_deliver_runs_full_validation():
    """A replaced secret below the 24-char minimum is rejected (HTTP 422)."""
    from fastapi import HTTPException

    stored = {"webhook_url": _HOOK, "webhook_secret": _REAL_SECRET}
    update = DeliverUpdate(webhook_url=_HOOK, webhook_secret="short")
    with pytest.raises(HTTPException) as exc:
        _merge_deliver(stored, update)
    assert exc.value.status_code == 422


# ─── Unit: schema shape ───────────────────────────────────────────────────────

def test_update_requires_updated_at():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ScraperConfigUpdate(name="x")  # updated_at missing


def test_update_tracks_identity_in_fields_set():
    """Identity fields are accepted only so the route can detect + 422 them."""
    body = ScraperConfigUpdate(updated_at=datetime.now(UTC), county="king")
    assert "county" in body.model_fields_set
    assert "state" not in body.model_fields_set


def test_update_forbids_unknown_keys():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ScraperConfigUpdate(updated_at=datetime.now(UTC), bogus_field=1)


def test_deliverupdate_ignores_readback_set_flags():
    """The GET response carries webhook_secret_set etc.; echoing them back on PATCH
    must not 422 (extra='ignore')."""
    upd = DeliverUpdate(
        emails=[],
        webhook_secret_set=True,
        dialer_webhook_secret_set=False,
        phoneburner_access_token_set=True,
    )
    # The flags are dropped, not stored as real fields.
    assert "webhook_secret_set" not in upd.model_dump()


def test_deliverconfig_and_update_share_field_names():
    """_merge_deliver round-trips DeliverUpdate.model_dump() into DeliverConfig, so
    every DeliverUpdate field must be a real DeliverConfig field."""
    extra = set(DeliverUpdate.model_fields) - set(DeliverConfig.model_fields)
    assert not extra, f"DeliverUpdate has fields DeliverConfig lacks: {extra}"


# ─── Integration: PATCH /scrapers/{id} ────────────────────────────────────────

async def _make_config(db, user, **overrides):
    from src.db.models import ScraperConfig
    data = dict(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name="Edit Test",
        county="pierce",
        state="WA",
        record_type="probate",
        fields={"party_name": True, "parcel_id": True},
        enrichment={"property_lookup": True, "skip_tracing": False},
        schedule={"frequency": "manual"},
        deliver={"formats": ["csv"], "emails": []},
        skip_trace_enabled=False,
    )
    data.update(overrides)
    config = ScraperConfig(**data)
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_requires_auth(client):
    r = await client.patch(f"/scrapers/{uuid.uuid4()}", json={"updated_at": datetime.now(UTC).isoformat()})
    assert r.status_code in (401, 403)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_unknown_id_404(client, starter_user, starter_token):
    r = await client.patch(
        f"/scrapers/{uuid.uuid4()}",
        json={"updated_at": datetime.now(UTC).isoformat(), "name": "x"},
        headers=_auth(starter_token),
    )
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_other_tenant_404(client, db, starter_token, business_user):
    """A config owned by another tenant is invisible (user_id filter + RLS)."""
    config = await _make_config(db, business_user)
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={"updated_at": config.updated_at.isoformat(), "name": "stolen"},
        headers=_auth(starter_token),
    )
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_identity_locked_422(client, db, starter_user, starter_token):
    config = await _make_config(db, starter_user)
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={"updated_at": config.updated_at.isoformat(), "county": "king"},
        headers=_auth(starter_token),
    )
    assert r.status_code == 422
    assert "identity" in r.json()["detail"].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_soft_deleted_404(client, db, starter_user, starter_token):
    config = await _make_config(db, starter_user, active=False)
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={"updated_at": config.updated_at.isoformat(), "name": "x"},
        headers=_auth(starter_token),
    )
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_stale_updated_at_409(client, db, starter_user, starter_token):
    config = await _make_config(db, starter_user)
    stale = (config.updated_at - timedelta(seconds=30)).isoformat()
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={"updated_at": stale, "name": "renamed"},
        headers=_auth(starter_token),
    )
    assert r.status_code == 409


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_active_job_409(client, db, starter_user, starter_token):
    from src.db.models import Job
    config = await _make_config(db, starter_user)
    job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=config.id,
        status="scraping",
        trigger="manual",
    )
    db.add(job)
    await db.commit()
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={"updated_at": config.updated_at.isoformat(), "name": "renamed"},
        headers=_auth(starter_token),
    )
    assert r.status_code == 409


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_rename_succeeds_and_bumps_token(client, db, starter_user, starter_token):
    from src.db.models import ScraperConfig
    config = await _make_config(db, starter_user)
    original = config.updated_at
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={"updated_at": original.isoformat(), "name": "Renamed Scraper"},
        headers=_auth(starter_token),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed Scraper"
    fresh = (await db.execute(
        ScraperConfig.__table__.select().where(ScraperConfig.id == config.id)
    )).fetchone()
    assert fresh.name == "Renamed Scraper"
    assert fresh.updated_at >= original


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_noop_does_not_change_row(client, db, starter_user, starter_token):
    """A PATCH that changes nothing must not bump updated_at (no lost-update churn)."""
    from src.db.models import ScraperConfig
    config = await _make_config(db, starter_user)
    original = config.updated_at
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={"updated_at": original.isoformat()},
        headers=_auth(starter_token),
    )
    assert r.status_code == 200
    fresh = (await db.execute(
        ScraperConfig.__table__.select().where(ScraperConfig.id == config.id)
    )).fetchone()
    assert fresh.updated_at == original


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_starter_enabling_skip_trace_402(client, db, starter_user, starter_token):
    """Edit must not bypass tiers: a Starter turning ON skip_trace gets 402."""
    config = await _make_config(db, starter_user)
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={"updated_at": config.updated_at.isoformat(), "skip_trace_enabled": True},
        headers=_auth(starter_token),
    )
    assert r.status_code == 402


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_keeps_webhook_secret_when_blank(client, db, business_user, business_token):
    """Editing the recipients while leaving the secret blank keeps the stored secret."""
    from src.db.models import ScraperConfig
    config = await _make_config(
        db, business_user,
        deliver={"formats": ["csv"], "emails": [], "webhook_url": _HOOK, "webhook_secret": _REAL_SECRET},
    )
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={
            "updated_at": config.updated_at.isoformat(),
            "deliver": {"formats": ["csv"], "emails": ["a@b.com"], "webhook_url": _HOOK},
        },
        headers=_auth(business_token),
    )
    assert r.status_code == 200
    fresh = (await db.execute(
        ScraperConfig.__table__.select().where(ScraperConfig.id == config.id)
    )).fetchone()
    assert fresh.deliver["webhook_secret"] == _REAL_SECRET   # preserved
    assert fresh.deliver["emails"] == ["a@b.com"]            # updated


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_replaces_webhook_secret_when_provided(client, db, business_user, business_token):
    from src.db.models import ScraperConfig
    config = await _make_config(
        db, business_user,
        deliver={"formats": ["csv"], "emails": [], "webhook_url": _HOOK, "webhook_secret": _REAL_SECRET},
    )
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={
            "updated_at": config.updated_at.isoformat(),
            "deliver": {"formats": ["csv"], "emails": [], "webhook_url": _HOOK, "webhook_secret": _NEW_SECRET},
        },
        headers=_auth(business_token),
    )
    assert r.status_code == 200
    # Secret is write-only in the response.
    assert "webhook_secret" not in r.json()["deliver"]
    fresh = (await db.execute(
        ScraperConfig.__table__.select().where(ScraperConfig.id == config.id)
    )).fetchone()
    assert fresh.deliver["webhook_secret"] == _NEW_SECRET


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_switch_to_since_last_run_persists(client, db, starter_user, starter_token):
    from src.db.models import ScraperConfig
    config = await _make_config(db, starter_user)
    r = await client.patch(
        f"/scrapers/{config.id}",
        json={
            "updated_at": config.updated_at.isoformat(),
            "schedule": {"frequency": "daily", "date_range_mode": "since_last_run"},
        },
        headers=_auth(starter_token),
    )
    assert r.status_code == 200
    fresh = (await db.execute(
        ScraperConfig.__table__.select().where(ScraperConfig.id == config.id)
    )).fetchone()
    assert fresh.schedule["date_range_mode"] == "since_last_run"
    assert fresh.schedule["frequency"] == "daily"
