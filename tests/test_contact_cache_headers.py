"""Every response a contact-bearing route produces carries ``Cache-Control: no-store``.

UX audit item 3, sub-phase 3.8e. The routes below return lead contacts (phones,
emails) or, for /export-url, a bearer download capability for them. Their success
responses set no-store; an HTTPException from the same route used to go out with no
Cache-Control at all, so a cache could keep a stale 404 / 409 for a URL that later
serves PII. Each route now wraps its whole body in ``no_store_errors()``.

The contract is the DIRECTIVE: the header's directive set contains ``no-store``
(the Segments exports keep their ``private, no-store``). Out of contract, by plan:
errors raised by dependencies or request validation before the body runs (auth 401,
plan 402, body 422), and the global 500 handler, whose body is only detail + ref.

Real requests on the guarded test DB. The route-produced 500 (run CSV) and 503
(batch renderer) can only be reached by a fault and the product has no fault hook,
so ``no_store_errors()`` is tested directly and a structural test proves each route
body is one ``with no_store_errors():`` block, which puts those raises inside it.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import _CREDENTIALS_EXCEPTION
from src.api.download_tokens import mint_download_token
from src.api.errors import RunRefusedHTTPException, no_store_errors
from src.api.routes import batches, jobs, segments
from src.db.models import BatchRun, Job, JobLog, Result, ScraperBatch, ScraperConfig, User

pytestmark = pytest.mark.asyncio

PHONE = "2065550147"
EMAIL = "jane.doe@example.com"
SEGMENT_BODY = {"record_types": ["pre_foreclosure", "probate"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _directives(resp: Response) -> set[str]:
    value = resp.headers.get("cache-control")
    assert value, f"{resp.request.method} {resp.request.url.path} -> {resp.status_code}: no Cache-Control"
    return {part.strip().split("=", 1)[0].lower() for part in value.split(",") if part.strip()}


def _assert_no_store(resp: Response, status_code: int) -> None:
    assert resp.status_code == status_code, (resp.request.url.path, resp.status_code, resp.text[:300])
    assert "no-store" in _directives(resp), resp.headers.get("cache-control")


# ─── Fixtures ────────────────────────────────────────────────────────────────

async def _config(db: AsyncSession, user: User, **kw) -> ScraperConfig:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Pierce probate", county="pierce",
        state="WA", record_type="probate", fields=[], enrichment=[], schedule={}, deliver={},
        **kw,
    )
    db.add(cfg)
    await db.commit()
    return cfg


async def _job(db: AsyncSession, user: User, cfg: ScraperConfig, *, status: str = "done",
               export_key: str | None = "exports/contact38.csv") -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(
        id=job_id, user_id=user.id, scraper_config_id=cfg.id, status=status,
        trigger="manual", record_count=1, finished_at=datetime.now(UTC),
        export_key=export_key,
    ))
    await db.commit()
    return job_id


async def _lead(db: AsyncSession, user: User, job_id: str) -> None:
    db.add(Result(
        id=str(uuid.uuid4()), job_id=job_id, user_id=user.id, party_name="DOE, JANE",
        property_address="123 Main St, Tacoma, WA 98402", is_duplicate=False,
        phone=PHONE, email=EMAIL, skip_trace_status="hit",
    ))
    await db.commit()


async def _delivered_job_with_contact(db: AsyncSession, user: User) -> str:
    cfg = await _config(db, user)
    job_id = await _job(db, user, cfg)
    await _lead(db, user, job_id)
    return job_id


async def _batch(db: AsyncSession, user: User, run_status: str = "done") -> tuple[str, str]:
    """A batch with one empty run: 'done' downloads a header-only CSV (the honest
    zero-row case), 'running' is not ready."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()), user_id=user.id, name="Batch", state="WA", fields=[],
        enrichment=[], schedule={}, deliver={}, status="active",
    )
    db.add(batch)
    await db.flush()  # batch_runs carries a composite FK to scraper_batches
    run = BatchRun(
        id=str(uuid.uuid4()), batch_id=batch.id, user_id=user.id, status=run_status,
        child_job_ids=[],
    )
    db.add(run)
    await db.commit()
    return batch.id, run.id


# ─── no_store_errors(), directly (the fault-only 500 / 503) ─────────────────

@pytest.mark.parametrize("code", [500, 503, 404, 429])
async def test_no_store_errors_adds_the_header_to_an_http_exception(code):
    with pytest.raises(HTTPException) as caught:
        with no_store_errors():
            raise HTTPException(status_code=code, detail="x")
    assert caught.value.status_code == code
    assert caught.value.headers == {"Cache-Control": "no-store"}


async def test_no_store_errors_keeps_the_exceptions_own_headers():
    with pytest.raises(HTTPException) as caught:
        with no_store_errors():
            raise HTTPException(status_code=429, detail="slow", headers={"Retry-After": "60"})
    assert caught.value.headers == {"Retry-After": "60", "Cache-Control": "no-store"}


async def test_no_store_errors_keeps_an_existing_cache_control():
    with pytest.raises(HTTPException) as caught:
        with no_store_errors():
            raise HTTPException(status_code=404, detail="x",
                                headers={"cache-control": "private, no-store"})
    assert caught.value.headers == {"cache-control": "private, no-store"}


async def test_no_store_errors_raises_a_copy_and_never_touches_the_original():
    """Some exceptions are module-level singletons: the header goes on a copy of
    the same class with the same attributes, never on the shared instance."""
    original = RunRefusedHTTPException("frozen", "Account frozen.", None,
                                       headers={"Retry-After": "60"})
    with pytest.raises(RunRefusedHTTPException) as caught:
        with no_store_errors():
            raise original
    raised = caught.value
    assert raised is not original and raised.__cause__ is original
    assert (raised.status_code, raised.detail, raised.code) == (402, "Account frozen.", "frozen")
    assert raised.headers == {"Retry-After": "60", "Cache-Control": "no-store"}
    assert original.headers == {"Retry-After": "60"}


async def test_no_store_errors_leaves_any_other_exception_untouched():
    original = ValueError("boom")
    with pytest.raises(ValueError) as caught:
        with no_store_errors():
            raise original
    assert caught.value is original
    assert not hasattr(caught.value, "headers")


async def test_no_store_errors_is_silent_on_success():
    with no_store_errors():
        value = 1
    assert value == 1


# ─── Structural: each route body is ONE `with no_store_errors():` block ──────

ROUTES = [
    (jobs, "get_results"),
    (jobs, "get_export_url"),
    (jobs, "download_export"),
    (segments, "intersection_preview"),
    (segments, "intersection_export"),
    (segments, "union_preview"),
    (segments, "union_export"),
    (batches, "download_batch"),
    (batches, "download_batch_run"),
]


def _function_node(module, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(module, name))))
    node = tree.body[0]
    assert isinstance(node, ast.AsyncFunctionDef) and node.name == name
    return node


@pytest.mark.parametrize(("module", "name"), ROUTES, ids=[n for _, n in ROUTES])
async def test_route_body_is_a_single_no_store_block(module, name):
    body = _function_node(module, name).body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # the docstring
    assert len(body) == 1, f"{name}: statements outside the no_store_errors() block"
    block = body[0]
    assert isinstance(block, ast.With) and len(block.items) == 1
    call = block.items[0].context_expr
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    assert call.func.id == "no_store_errors"


async def test_the_fault_only_raises_sit_inside_the_block():
    """The run CSV's route-level 500 and the batch renderer's 503 are the raises
    a real request cannot reach. Prove each is inside a no_store_errors() block:
    the 500 inside download_export's, the 503 inside _stream_run_csv, which only
    the two (wrapped) batch download routes call."""
    dl = _function_node(jobs, "download_export").body[-1]
    assert isinstance(dl, ast.With)
    assert 'status_code=500' in ast.unparse(dl)
    callers = [n for _, n in ROUTES if n.startswith("download_batch")]
    for name in callers:
        assert "_stream_run_csv(" in ast.unparse(_function_node(batches, name).body[-1])
    src = inspect.getsource(batches)
    assert src.count("await _stream_run_csv(") == len(callers)
    assert "HTTP_503_SERVICE_UNAVAILABLE" in inspect.getsource(batches._stream_run_csv)


async def test_every_joblog_query_in_get_results_is_tenant_joined():
    """JobLog has no user_id. Both count queries join Job and filter its owner,
    the shape of _job_logs_select."""
    awaits = [
        ast.unparse(n) for n in ast.walk(_function_node(jobs, "get_results"))
        if isinstance(n, ast.Await) and "JobLog" in ast.unparse(n)
    ]
    assert len(awaits) == 2
    for q in awaits:
        assert "join(Job, JobLog.job_id == Job.id)" in q, q
        assert "Job.user_id == current_user.id" in q, q


# ─── GET /jobs/{id}/results ──────────────────────────────────────────────────

async def test_results_success_carries_contacts_and_no_store(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str
):
    job_id = await _delivered_job_with_contact(db, starter_user)
    resp = await client.get(f"/jobs/{job_id}/results", headers=_auth(starter_token))
    _assert_no_store(resp, 200)
    assert resp.headers["cache-control"] == "no-store"
    row = resp.json()["items"][0]
    assert row["phone"] == PHONE and row["email"] == EMAIL


async def test_results_errors_carry_no_store(
    client: AsyncClient, db: AsyncSession, starter_user: User, business_token: str,
    starter_token: str,
):
    job_id = await _delivered_job_with_contact(db, starter_user)
    _assert_no_store(await client.get(f"/jobs/{job_id}/results", headers=_auth(business_token)), 404)
    _assert_no_store(await client.get("/jobs/not-a-uuid/results", headers=_auth(starter_token)), 404)


@pytest.mark.parametrize(("log", "expected"), [
    ("Enrichment complete: 1 of 1 addresses found.", False),
    ("No records with parcel numbers to enrich.", False),
    ("Looking up property and mailing addresses...", True),
])
async def test_results_enriching_flag_reads_the_owners_logs_through_the_join(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str, log, expected,
):
    """Unchanged for the owner: the joined count still finds the job's own log, so
    a job still in `enriching` whose log says it finished reads not-enriching."""
    cfg = await _config(db, starter_user)
    job_id = await _job(db, starter_user, cfg, status="enriching", export_key=None)
    db.add(Result(
        id=str(uuid.uuid4()), job_id=job_id, user_id=starter_user.id, party_name="DOE JANE",
        parcel_id="00522400008900", property_address="22801 64TH PL W", is_duplicate=False,
    ))
    db.add(JobLog(job_id=job_id, level="info", message=log))
    await db.commit()
    resp = await client.get(f"/jobs/{job_id}/results", headers=_auth(starter_token))
    _assert_no_store(resp, 200)
    assert resp.json()["enriching"] is expected


# ─── GET /jobs/{id}/export-url ───────────────────────────────────────────────

async def test_export_url_success_and_errors_carry_no_store(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str,
    business_token: str,
):
    cfg = await _config(db, starter_user)
    delivered = await _job(db, starter_user, cfg)
    undelivered = await _job(db, starter_user, cfg, status="enriching")
    no_export = await _job(db, starter_user, cfg, export_key=None)

    ok = await client.get(f"/jobs/{delivered}/export-url", headers=_auth(starter_token))
    _assert_no_store(ok, 200)
    assert ok.headers["cache-control"] == "no-store"
    assert "token=" in ok.json()["url"]

    for path, token, code in [
        (f"/jobs/{delivered}/export-url", business_token, 404),   # another tenant's job
        (f"/jobs/{undelivered}/export-url", starter_token, 409),
        (f"/jobs/{no_export}/export-url", starter_token, 404),
        ("/jobs/not-a-uuid/export-url", starter_token, 404),
    ]:
        _assert_no_store(await client.get(path, headers=_auth(token)), code)


async def test_export_url_without_credentials_hands_out_nothing(
    client: AsyncClient, db: AsyncSession, starter_user: User
):
    """The 401 comes from the auth dependency, before the body: out of the header
    contract. It must carry no URL and no token."""
    job_id = await _delivered_job_with_contact(db, starter_user)
    resp = await client.get(f"/jobs/{job_id}/export-url")
    assert resp.status_code == 401
    assert "url" not in resp.json()
    assert "/download" not in resp.text and "token=" not in resp.text


# ─── GET /jobs/{id}/download (the run CSV) ───────────────────────────────────

async def test_download_success_by_bearer_and_by_token_carries_no_store(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str
):
    job_id = await _delivered_job_with_contact(db, starter_user)
    by_bearer = await client.get(f"/jobs/{job_id}/download", headers=_auth(starter_token))
    _assert_no_store(by_bearer, 200)
    assert PHONE in by_bearer.text or "(206) 555-0147" in by_bearer.text

    url = (await client.get(f"/jobs/{job_id}/export-url", headers=_auth(starter_token))).json()["url"]
    _assert_no_store(await client.get(url), 200)


async def test_download_errors_carry_no_store(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str,
    business_token: str,
):
    cfg = await _config(db, starter_user)
    delivered = await _job(db, starter_user, cfg)
    await _lead(db, starter_user, delivered)
    undelivered = await _job(db, starter_user, cfg, status="enriching")
    no_export = await _job(db, starter_user, cfg, export_key=None)
    no_rows = await _job(db, starter_user, cfg)  # export_key set, no addressable row
    other_job_token = mint_download_token(str(starter_user.id), no_rows, ttl_seconds=60)

    for path, headers, code in [
        (f"/jobs/{delivered}/download", {}, 401),                                # no credentials
        (f"/jobs/{delivered}/download?token=not-a-jwt", {}, 401),               # bad token
        (f"/jobs/{delivered}/download?token={other_job_token}", {}, 403),       # token for another run
        (f"/jobs/{delivered}/download", _auth(business_token), 404),            # another tenant's job
        (f"/jobs/{undelivered}/download", _auth(starter_token), 409),
        (f"/jobs/{no_export}/download", _auth(starter_token), 404),
        (f"/jobs/{no_rows}/download", _auth(starter_token), 404),               # no records
        ("/jobs/not-a-uuid/download", {}, 404),                                 # malformed id
    ]:
        _assert_no_store(await client.get(path, headers=headers), code)


async def test_download_bad_bearer_401_keeps_the_auth_singleton_clean(
    client: AsyncClient, db: AsyncSession, starter_user: User
):
    """The in-body bearer check raises auth's module-level _CREDENTIALS_EXCEPTION.
    Its 401 carries no-store AND its WWW-Authenticate, and the shared instance is
    left exactly as it was, so no other route's 401 inherits the header."""
    job_id = await _delivered_job_with_contact(db, starter_user)
    before = dict(_CREDENTIALS_EXCEPTION.headers)
    resp = await client.get(f"/jobs/{job_id}/download", headers=_auth("not-a-jwt"))
    _assert_no_store(resp, 401)
    assert resp.headers["www-authenticate"] == "Bearer"
    assert _CREDENTIALS_EXCEPTION.headers == before == {"WWW-Authenticate": "Bearer"}


# ─── POST /segments/* ────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/segments/intersection", "/segments/union"])
async def test_segment_previews_carry_no_store(
    client: AsyncClient, business_user: User, business_token: str, path: str
):
    resp = await client.post(path, json=SEGMENT_BODY, headers=_auth(business_token))
    _assert_no_store(resp, 200)
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/segments/intersection/export", "/segments/union/export"])
async def test_segment_exports_keep_private_no_store(
    client: AsyncClient, business_user: User, business_token: str, path: str
):
    resp = await client.post(path, json=SEGMENT_BODY, headers=_auth(business_token))
    _assert_no_store(resp, 200)
    assert _directives(resp) == {"private", "no-store"}


# ─── GET /batches/{id}/download, /batches/{id}/runs/{run}/download ───────────

async def test_batch_downloads_success_carries_no_store(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str
):
    batch_id, run_id = await _batch(db, starter_user)
    for path in (f"/batches/{batch_id}/download", f"/batches/{batch_id}/runs/{run_id}/download"):
        resp = await client.get(path, headers=_auth(starter_token))
        _assert_no_store(resp, 200)
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["content-type"].startswith("text/csv")


async def test_batch_download_errors_carry_no_store(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str,
    business_token: str,
):
    batch_id, run_id = await _batch(db, starter_user)
    running_batch, running_run = await _batch(db, starter_user, run_status="running")
    for path, token in [
        (f"/batches/{batch_id}/download", business_token),                       # another tenant
        (f"/batches/{batch_id}/runs/{run_id}/download", business_token),
        (f"/batches/{running_batch}/download", starter_token),                    # not ready
        (f"/batches/{running_batch}/runs/{running_run}/download", starter_token),
        (f"/batches/{batch_id}/runs/{uuid.uuid4()}/download", starter_token),     # no such run
    ]:
        _assert_no_store(await client.get(path, headers=_auth(token)), 404)


# ─── 429: exhaust the per-user limits with real requests ─────────────────────

async def test_export_zone_429_carries_no_store_on_every_export_route(
    client: AsyncClient, business_user: User, business_token: str
):
    """`export` is one bucket per user (20/min) shared by every route here. Spend
    it with real requests, then every route's 429 carries no-store."""
    h = _auth(business_token)
    for _ in range(20):
        _assert_no_store(await client.get(f"/jobs/{uuid.uuid4()}/export-url", headers=h), 404)
    some = uuid.uuid4()
    for method, path in [
        ("GET", f"/jobs/{some}/export-url"),
        ("GET", f"/jobs/{some}/download"),
        ("POST", "/segments/intersection/export"),
        ("POST", "/segments/union/export"),
        ("GET", f"/batches/{some}/download"),
        ("GET", f"/batches/{some}/runs/{uuid.uuid4()}/download"),
    ]:
        kw = {"json": SEGMENT_BODY} if method == "POST" else {}
        resp = await client.request(method, path, headers=h, **kw)
        _assert_no_store(resp, 429)
        assert resp.headers["retry-after"] == "60"


async def test_general_zone_429_carries_no_store_on_results_and_previews(
    client: AsyncClient, business_user: User, business_token: str
):
    """`general` is 60/min per user. Spend it on real Results 404s, then the
    Results page and both Segments previews answer 429 with no-store."""
    h = _auth(business_token)
    for _ in range(60):
        _assert_no_store(await client.get(f"/jobs/{uuid.uuid4()}/results", headers=h), 404)
    for method, path in [
        ("GET", f"/jobs/{uuid.uuid4()}/results"),
        ("POST", "/segments/intersection"),
        ("POST", "/segments/union"),
    ]:
        kw = {"json": SEGMENT_BODY} if method == "POST" else {}
        resp = await client.request(method, path, headers=h, **kw)
        _assert_no_store(resp, 429)
        assert resp.headers["retry-after"] == "60"
