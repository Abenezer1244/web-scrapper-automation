"""The job lead CSV streams from a server-side cursor in partitions.

The API used to load every row, build the whole CSV in a StringIO and encode a
second copy before replying (~3x the export in memory, unbounded on Agency).
download_export now streams partition by partition; the FILE must be exactly
what it was: one header, every row once, nothing lost or repeated at a
partition boundary. The partition size is shrunk here so a few dozen rows
cross many boundaries.
"""
from __future__ import annotations

import csv
import io
import uuid

import pytest
from httpx import AsyncClient
from starlette.requests import ClientDisconnect

from src.api.routes import jobs as jobs_routes
from src.db.models import Job, Result, ScraperConfig

_PW = "Download-Stream-Test-2026!"
_ROWS = 50


async def _session_with_a_large_run(client: AsyncClient, db, rows: int) -> tuple[dict, str]:
    email = f"stream_{uuid.uuid4().hex[:8]}@test.bridgeleads.io"
    reg = await client.post("/auth/register", json={
        "first_name": "Stream", "last_name": "Test", "email": email, "password": _PW,
    })
    assert reg.status_code == 201, reg.text
    session = reg.json()
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {session['access_token']}"})
    user_id = me.json()["id"]
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user_id, name="Pierce tax", county="pierce",
        state="WA", record_type="tax_delinquent", fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.flush()
    job = Job(
        id=str(uuid.uuid4()), user_id=user_id, scraper_config_id=config.id,
        status="done", trigger="manual", export_key=f"exports/{user_id}/run.csv",
    )
    db.add(job)
    await db.flush()
    for i in range(rows):
        db.add(Result(
            id=str(uuid.uuid4()), job_id=job.id, user_id=user_id,
            party_name=f"STREAM OWNER {i:03d}", parcel_id=f"{1000000000 + i}",
            property_address=f"{100 + i} Main St, Tacoma, WA 98402",
        ))
    await db.commit()
    return session, job.id


@pytest.mark.asyncio
async def test_a_multi_partition_download_is_one_complete_csv(client, db, monkeypatch):
    monkeypatch.setattr(jobs_routes, "_DOWNLOAD_STREAM_ROWS", 7)  # 50 rows -> 8 partitions
    session, job_id = await _session_with_a_large_run(client, db, _ROWS)
    dl = await client.get(
        f"/jobs/{job_id}/download", headers={"Authorization": f"Bearer {session['access_token']}"}
    )
    assert dl.status_code == 200, dl.text
    assert dl.headers["content-type"].startswith("text/csv")
    assert "content-length" not in dl.headers  # streamed, not one buffered body
    assert "attachment" in dl.headers["content-disposition"]

    lines = dl.text.splitlines()
    header = lines[0]
    assert sum(1 for line in lines if line == header) == 1, "header repeated at a partition boundary"
    rows = list(csv.DictReader(io.StringIO(dl.text)))
    owners = sorted(r.get("party_name") or r.get("owner_name") or "" for r in rows)
    assert len(rows) == _ROWS
    assert len(set(owners)) == _ROWS, "a row was lost or repeated at a partition boundary"
    assert all(o.startswith("STREAM OWNER") for o in owners)


@pytest.mark.asyncio
async def test_the_streamed_file_matches_the_single_partition_file(client, db, monkeypatch):
    # Same job, partition size 1 vs one partition holding everything: byte-identical
    # bodies prove the chunking adds nothing (header, quoting, line endings).
    session, job_id = await _session_with_a_large_run(client, db, 12)
    auth = {"Authorization": f"Bearer {session['access_token']}"}
    monkeypatch.setattr(jobs_routes, "_DOWNLOAD_STREAM_ROWS", 1)
    tiny = await client.get(f"/jobs/{job_id}/download", headers=auth)
    monkeypatch.setattr(jobs_routes, "_DOWNLOAD_STREAM_ROWS", 10_000)
    whole = await client.get(f"/jobs/{job_id}/download", headers=auth)
    assert tiny.status_code == whole.status_code == 200
    assert tiny.content == whole.content


async def _drive(response, send):
    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "http_version": "1.1"}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await response(scope, receive, send)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on", ["http.response.start", "http.response.body"])
async def test_the_cursor_closes_even_when_sending_fails(fail_on):
    # A failed send can leave Starlette's body iterator unclosed, and an iterator
    # that never started runs no `finally`: the response must close the cursor itself.
    closed = []

    async def body():
        yield b"header\n"
        yield b"row\n"

    async def on_close():
        closed.append(True)

    async def send(message):
        if message["type"] == fail_on:
            raise OSError("client went away")

    response = jobs_routes._ClosingStreamingResponse(body(), on_close=on_close, media_type="text/csv")
    # Starlette turns a failing send into ClientDisconnect (ASGI spec >= 2.4).
    with pytest.raises((OSError, ClientDisconnect)):
        await _drive(response, send)
    assert closed == [True]


@pytest.mark.asyncio
async def test_the_cursor_closes_after_a_complete_download():
    closed = []

    async def body():
        yield b"header\n"

    async def on_close():
        closed.append(True)

    sent = []

    async def send(message):
        sent.append(message["type"])

    await _drive(jobs_routes._ClosingStreamingResponse(body(), on_close=on_close), send)
    assert closed == [True] and "http.response.body" in sent
