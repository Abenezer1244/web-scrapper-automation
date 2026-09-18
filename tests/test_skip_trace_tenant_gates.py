"""No request can start a lookup on, or move contact data out of, another account's leads,
and no request can write skip-trace state.

Skip-trace state (status, source, phones, emails) is written only by workers. The HTTP
paths that can LEAD to a paid lookup or move contact data carry an id, so each is tried
here with another account's id, asserting both the 404 and that nothing changed behind
it (Codex 2026-09-18: a 404 alone does not prove no side effect).

  POST   /jobs                                   starts a run (lookups follow)
  PATCH  /scrapers/{id}                          can turn skip trace on
  DELETE /jobs/{id}                              cancels a run
  POST   /scrapers/{cfg}/jobs/{job}/dialer-replay pushes contacts to a dialer

Real DB and real endpoints (conftest). starter_user owns the target; business_user
attacks.
"""
import uuid

from sqlalchemy import func, select, text

from src.db.models import DialerDelivery, Job, PendingSkipTraceRow, Result, ScraperConfig


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _count(db, model, **where) -> int:
    q = select(func.count()).select_from(model)
    for k, v in where.items():
        q = q.where(getattr(model, k) == v)
    return (await db.execute(q)).scalar_one()


async def test_another_account_cannot_start_a_run_of_my_scraper(
    client, db, scraper_config, business_token, business_user,
):
    before = await _count(db, Job, scraper_config_id=scraper_config.id)

    resp = await client.post("/jobs", json={"scraper_config_id": scraper_config.id},
                             headers=_auth(business_token))

    assert resp.status_code == 404
    assert await _count(db, Job, scraper_config_id=scraper_config.id) == before
    assert await _count(db, Job, user_id=business_user.id) == 0
    assert await _count(db, PendingSkipTraceRow, user_id=business_user.id) == 0


async def test_another_account_cannot_turn_skip_trace_on_for_my_scraper(
    client, db, scraper_config, business_token,
):
    resp = await client.patch(f"/scrapers/{scraper_config.id}",
                              # A valid updated_at, so the request gets past body
                              # validation and meets the ownership check itself.
                              json={"skip_trace_enabled": True,
                                    "updated_at": scraper_config.updated_at.isoformat()},
                              headers=_auth(business_token))

    assert resp.status_code == 404, resp.text
    await db.refresh(scraper_config)
    assert scraper_config.skip_trace_enabled is False


async def test_another_account_cannot_cancel_my_run(client, db, pending_job, business_token):
    resp = await client.delete(f"/jobs/{pending_job.id}", headers=_auth(business_token))

    assert resp.status_code == 404
    await db.refresh(pending_job)
    assert pending_job.status == "pending"


async def test_another_account_cannot_replay_my_contacts_to_a_dialer(
    client, db, starter_user, scraper_config, pending_job, business_token,
):
    """The most sensitive of the four: it would push MY leads' phone numbers to a dialer."""
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, job_id=pending_job.id, user_id=starter_user.id,
                  party_name="DOE JANE", property_address="1 MAIN ST"))
    await db.flush()
    delivery = DialerDelivery(job_id=pending_job.id, result_id=rid, user_id=starter_user.id,
                              scraper_config_id=scraper_config.id, vendor_id="phoneburner",
                              status="failed", attempts=3)
    db.add(delivery)
    await db.commit()

    resp = await client.post(
        f"/scrapers/{scraper_config.id}/jobs/{pending_job.id}/dialer-replay",
        headers=_auth(business_token),
    )

    assert resp.status_code == 404
    await db.refresh(delivery)
    assert delivery.status == "failed"


async def test_skip_trace_state_in_a_request_body_changes_nothing(
    client, db, starter_user, scraper_config, pending_job, starter_token,
):
    """The owner's own requests cannot write skip-trace state either. Scraper updates
    REJECT the worker-owned fields outright (extra="forbid", 422 naming each one); a job
    create ignores them (JobCreate reads two fields; API-key callers are not broken by a
    stricter model for no gain). Either way nothing reaches the row."""
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, job_id=pending_job.id, user_id=starter_user.id,
                  party_name="DOE JANE", property_address="1 MAIN ST"))
    await db.commit()
    injected = {
        "skip_trace_status": "hit", "skip_trace_source": "lookup",
        "phones": [{"number": "2065550000", "type": "Mobile"}], "phone": "2065550000",
        "is_duplicate": True, "skip_trace_attempted_at": "2026-01-01T00:00:00Z",
    }

    patched = await client.patch(
        f"/scrapers/{scraper_config.id}",
        json={"name": "renamed", "updated_at": scraper_config.updated_at.isoformat(),
              **injected},
        headers=_auth(starter_token))
    assert patched.status_code == 422
    rejected = {e["loc"][-1] for e in patched.json()["detail"]
                if e["type"] == "extra_forbidden"}
    assert rejected == set(injected)
    await client.post("/jobs", json={"scraper_config_id": scraper_config.id, **injected},
                      headers=_auth(starter_token))

    row = (await db.execute(
        text("SELECT skip_trace_status, skip_trace_source, skip_trace_attempted_at, "
             "is_duplicate, phone IS NULL AS no_phone FROM results WHERE user_id = :u"),
        {"u": starter_user.id},
    )).mappings().all()
    assert [dict(r) for r in row] == [{
        "skip_trace_status": "not_attempted", "skip_trace_source": None,
        "skip_trace_attempted_at": None, "is_duplicate": False, "no_phone": True,
    }]
    cfg = (await db.execute(select(ScraperConfig).where(
        ScraperConfig.id == scraper_config.id))).scalar_one()
    await db.refresh(cfg)
    assert cfg.skip_trace_enabled is False
