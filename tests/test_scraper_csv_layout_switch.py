"""PUT /scrapers/{id}/csv-layout — switch one scraper's CSV layout atomically.

Real DB + real endpoints (conftest `db` / `client` / token fixtures), no mocks. The
contract: only deliver.csv_layout changes (legacy keys and write-only secrets stay
byte-identical), batch children and configs with a running job are refused, and a
repeat of the stored value writes nothing.
"""
import csv
import io
import uuid

import src.db.session as _db_session
from src.db.models import Job, Result, ScraperBatch, ScraperConfig
from src.utils.lead_export import LAYOUT_CRM_V1, LAYOUT_LEGACY_V1, resolve_lead_export_columns

_SECRET = "a" * 30
_LEGACY_DELIVER = {
    "format": "csv",  # singular legacy key DeliverUpdate (extra="forbid") would reject
    "emails": [],
    "webhook_url": "https://example.com/h",
    "webhook_secret": _SECRET,
}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _config(user_id: str, **overrides) -> ScraperConfig:
    data = {
        "id": str(uuid.uuid4()), "user_id": user_id, "name": "Layout Switch",
        "county": "pierce", "state": "WA", "record_type": "pre_foreclosure",
        "fields": {"party_name": True}, "enrichment": {}, "schedule": {"frequency": "manual"},
        "deliver": {"formats": ["csv"], "emails": []},
    }
    data.update(overrides)
    async with _db_session.AsyncSessionLocal() as s:
        cfg = ScraperConfig(**data)
        s.add(cfg)
        await s.commit()
        await s.refresh(cfg)
        return cfg


async def _stored(config_id: str) -> ScraperConfig:
    async with _db_session.AsyncSessionLocal() as s:
        return (await s.get(ScraperConfig, config_id))


async def _put(client, config_id: str, token: str | None, body: dict):
    headers = _auth(token) if token else {}
    return await client.put(f"/scrapers/{config_id}/csv-layout", json=body, headers=headers)


async def test_switch_legacy_to_crm_keeps_every_other_key(client, db, starter_user, starter_token):
    cfg = await _config(starter_user.id, deliver=dict(_LEGACY_DELIVER))
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deliver"]["csv_layout"] == LAYOUT_CRM_V1
    assert "webhook_secret" not in resp.json()["deliver"]  # still redacted
    stored = await _stored(cfg.id)
    assert stored.deliver == {**_LEGACY_DELIVER, "csv_layout": LAYOUT_CRM_V1}
    assert stored.deliver["webhook_secret"] == _SECRET and stored.deliver["format"] == "csv"
    assert stored.updated_at >= cfg.updated_at


async def test_switch_crm_to_legacy(client, db, starter_user, starter_token):
    deliver = {"formats": ["csv", "json"], "emails": ["a@example.com"], "csv_layout": "crm_v1"}
    cfg = await _config(starter_user.id, deliver=deliver)
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "legacy_v1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deliver"]["csv_layout"] == LAYOUT_LEGACY_V1
    assert (await _stored(cfg.id)).deliver == {**deliver, "csv_layout": LAYOUT_LEGACY_V1}


async def test_same_value_is_a_noop(client, db, starter_user, starter_token):
    deliver = {"formats": ["csv"], "emails": [], "csv_layout": "crm_v1"}
    cfg = await _config(starter_user.id, deliver=deliver)
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deliver"]["csv_layout"] == LAYOUT_CRM_V1
    stored = await _stored(cfg.id)
    assert stored.updated_at == cfg.updated_at
    assert stored.deliver == deliver


async def test_invalid_value_422(client, db, starter_user, starter_token):
    cfg = await _config(starter_user.id)
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v9"})
    assert resp.status_code == 422
    assert "csv_layout" not in (await _stored(cfg.id)).deliver


async def test_extra_key_422(client, db, starter_user, starter_token):
    cfg = await _config(starter_user.id)
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v1", "formats": ["json"]})
    assert resp.status_code == 422
    assert (await _stored(cfg.id)).deliver == {"formats": ["csv"], "emails": []}


async def test_other_tenant_404(client, db, business_user, starter_token):
    cfg = await _config(business_user.id)
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v1"})
    assert resp.status_code == 404
    assert "csv_layout" not in (await _stored(cfg.id)).deliver


async def test_inactive_config_404(client, db, starter_user, starter_token):
    cfg = await _config(starter_user.id, active=False)
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v1"})
    assert resp.status_code == 404
    assert "csv_layout" not in (await _stored(cfg.id)).deliver


async def test_batch_child_409(client, db, starter_user, starter_token):
    batch_id = str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(ScraperBatch(
            id=batch_id, user_id=starter_user.id, name="Layout Batch", state="WA",
            fields=[], enrichment=[], schedule={}, deliver={}, status="active",
            delivery_mode="everything",
        ))
        await s.commit()
    cfg = await _config(starter_user.id, batch_id=batch_id)
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v1"})
    assert resp.status_code == 409
    assert "batch" in resp.json()["detail"].lower()
    assert "csv_layout" not in (await _stored(cfg.id)).deliver


async def test_active_job_409(client, db, starter_user, starter_token):
    cfg = await _config(starter_user.id)
    async with _db_session.AsyncSessionLocal() as s:
        s.add(Job(id=str(uuid.uuid4()), user_id=starter_user.id, scraper_config_id=cfg.id,
                  status="scraping", trigger="manual"))
        await s.commit()
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v1"})
    assert resp.status_code == 409
    assert "running" in resp.json()["detail"].lower()
    assert "csv_layout" not in (await _stored(cfg.id)).deliver


async def test_same_value_while_job_runs_is_a_noop_not_409(client, db, starter_user, starter_token):
    cfg = await _config(starter_user.id, deliver={"formats": ["csv"], "csv_layout": "crm_v1"})
    async with _db_session.AsyncSessionLocal() as s:
        s.add(Job(id=str(uuid.uuid4()), user_id=starter_user.id, scraper_config_id=cfg.id,
                  status="scraping", trigger="manual"))
        await s.commit()
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v1"})
    assert resp.status_code == 200
    assert resp.json()["deliver"]["csv_layout"] == "crm_v1"


async def test_malformed_stored_deliver_is_refused_without_writing(client, db, starter_user, starter_token):
    cfg = await _config(starter_user.id, deliver=["csv"])
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v1"})
    assert resp.status_code == 409
    assert (await _stored(cfg.id)).deliver == ["csv"]


async def test_requires_auth(client, db, starter_user):
    cfg = await _config(starter_user.id)
    resp = await _put(client, cfg.id, None, {"csv_layout": "crm_v1"})
    assert resp.status_code in (401, 403)
    assert "csv_layout" not in (await _stored(cfg.id)).deliver


async def test_download_after_switch_uses_crm_headers(client, db, starter_user, starter_token):
    cfg = await _config(starter_user.id)
    job_id = str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(Job(id=job_id, user_id=starter_user.id, scraper_config_id=cfg.id, status="done",
                  trigger="manual", export_key=f"exports/{job_id}.csv"))
        await s.flush()
        s.add(Result(id=str(uuid.uuid4()), job_id=job_id, user_id=starter_user.id,
                     party_name="HALL MARVIN WAYNE", property_address="510 106TH ST S"))
        await s.commit()

    async def _header() -> list[str]:
        url = await client.get(f"/jobs/{job_id}/export-url", headers=_auth(starter_token))
        assert url.status_code == 200, url.text
        file = await client.get(url.json()["url"])
        assert file.status_code == 200
        # The file is live: a browser-cacheable response served the OLD headers after
        # a switch for an hour (found by a real-browser check), so it must be no-store.
        assert file.headers["cache-control"] == "no-store"
        return list(csv.reader(io.StringIO(file.text)))[0]

    assert await _header() == resolve_lead_export_columns("pre_foreclosure")
    resp = await _put(client, cfg.id, starter_token, {"csv_layout": "crm_v1"})
    assert resp.status_code == 200, resp.text
    assert (await _header())[0] == "First Name"
