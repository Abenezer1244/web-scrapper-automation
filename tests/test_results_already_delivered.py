"""The "N already delivered" count must be a set the user can open, page, search and
download, and it must be exactly that set.

Owner report, 2026-09-17: a Pierce pre_foreclosure run read "3 new · 227 already
delivered" and the 227 were a bare number. Nothing on the page could show which
records they were, when they had been delivered or by which run, so the count could
not be checked. ``GET /jobs/{id}/results?category=already_delivered`` lists them,
``already_delivered_count`` counts them with the same predicate, each row says what
can be verified about its original, and the CSV follows the chosen view.

Asserted here, against the real DB and the real endpoints (conftest fixtures, no
mocks):
  - the tab count, the list total and the CSV are the same rows, and only those rows
    (not combined, not superseded, not unactionable, not tax-capped);
  - search, sort and paging run server-side inside the category;
  - provenance claims only what the data supports;
  - another account can never see, count or download these rows, and another
    account's claim can never make a lead "already delivered";
  - reading any of it writes nothing: no quota, no skip trace, no claim change.
"""
import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy import text as sa_text

from src.db.models import Job, Result, ScraperConfig, User

NOW = datetime.now(UTC)


async def _job(
    db,
    user: User,
    config: ScraperConfig,
    *,
    created_at: datetime = NOW,
    status: str = "done",
) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(
        id=job_id,
        user_id=user.id,
        scraper_config_id=config.id,
        status=status,
        trigger="manual",
        record_count=0,
        created_at=created_at,
        # /download requires a finished export to exist.
        export_key=f"exports/{job_id}.csv" if status == "done" else None,
    ))
    await db.commit()
    return job_id


async def _row(
    db,
    job_id: str,
    user_id: str,
    *,
    tag: str,
    duplicate: bool = False,
    reason: str | None = None,
    source_job: str | None = None,
    source_at: datetime | None = None,
    dedup_hash: str | None = None,
    address: str | None = "ADDR",
    date_recorded: str | None = None,
    bill_year: int | None = None,
    **extra,
) -> str:
    row_id = str(uuid.uuid4())
    db.add(Result(
        id=row_id,
        job_id=job_id,
        user_id=user_id,
        party_name=f"OWNER {tag}",
        parcel_id=f"PARCEL-{tag}",
        property_address=None if address is None else f"{tag} MAIN ST",
        date_recorded=date_recorded,
        dedup_hash=dedup_hash or uuid.uuid4().hex,
        is_duplicate=duplicate,
        duplicate_reason=reason,
        duplicate_source_job_id=source_job,
        duplicate_source_at=source_at,
        delinquent_bill_year=bill_year,
        **extra,
    ))
    await db.commit()
    return row_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _results(client: AsyncClient, job_id: str, token: str, **params) -> dict:
    resp = await client.get(f"/jobs/{job_id}/results", params=params, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _delivered(client, job_id, token, **params) -> dict:
    return await _results(client, job_id, token, category="already_delivered", **params)


async def _csv_parcels(client: AsyncClient, job_id: str, token: str, **params):
    resp = await client.get(f"/jobs/{job_id}/download", params=params, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    rows = list(csv.reader(io.StringIO(resp.text)))
    parcels = {cell for row in rows[1:] for cell in row if cell.startswith("PARCEL-")}
    return resp, parcels


async def _mixed_run(db, user: User, config: ScraperConfig) -> dict:
    """An earlier run that delivered two leads, then the viewed run holding every
    kind of row the page distinguishes."""
    earlier = await _job(db, user, config, created_at=NOW - timedelta(days=5))
    h_a, h_b = uuid.uuid4().hex, uuid.uuid4().hex
    await _row(db, earlier, user.id, tag="ORIG-A", dedup_hash=h_a)
    await _row(db, earlier, user.id, tag="ORIG-B", dedup_hash=h_b)

    viewed = await _job(db, user, config)
    at = NOW - timedelta(days=5)
    ids = {
        "new": [await _row(db, viewed, user.id, tag=f"NEW-{i}") for i in range(3)],
        "delivered": [
            await _row(db, viewed, user.id, tag="DLV-A", duplicate=True, reason="prior_run",
                       source_job=earlier, source_at=at, dedup_hash=h_a),
            await _row(db, viewed, user.id, tag="DLV-B", duplicate=True, reason="prior_run",
                       source_job=earlier, source_at=at, dedup_hash=h_b),
            # Classified before migration 089: no reason, no source. Still a prior
            # claim (the only way a pre-089 row could be flagged).
            await _row(db, viewed, user.id, tag="DLV-LEGACY", duplicate=True),
        ],
        "combined": [await _row(db, viewed, user.id, tag="SAME", duplicate=True,
                                reason="same_run", source_job=viewed)],
        "superseded": [await _row(db, viewed, user.id, tag="SUPER", duplicate=True,
                                  reason="superseded", source_job=earlier)],
        # Not a lead at all (no property or mailing address): never listed or counted.
        "unactionable": [await _row(db, viewed, user.id, tag="NOADDR", duplicate=True,
                                    reason="prior_run", source_job=earlier, address=None)],
        # Oldest unpaid tax year far outside the cap: never shown anywhere.
        "tax_capped": [await _row(db, viewed, user.id, tag="OLDTAX", duplicate=True,
                                  reason="prior_run", source_job=earlier, bill_year=2000)],
    }
    return {"earlier": earlier, "viewed": viewed, "ids": ids}


# ─── 1. Count, list and CSV are one set ─────────────────────────────────────────

async def test_the_tab_count_the_list_and_the_csv_are_exactly_the_delivered_rows(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    run = await _mixed_run(db, starter_user, scraper_config)
    viewed, ids = run["viewed"], run["ids"]

    new_view = await _results(client, viewed, starter_token)
    dlv_view = await _delivered(client, viewed, starter_token)

    assert new_view["new_count"] == 3
    assert new_view["already_delivered_count"] == 3
    assert {r["id"] for r in new_view["items"]} == set(ids["new"])
    assert new_view["total"] == 3

    assert {r["id"] for r in dlv_view["items"]} == set(ids["delivered"])
    assert dlv_view["total"] == dlv_view["already_delivered_count"] == 3
    assert all(r["is_duplicate"] for r in dlv_view["items"])
    # Why the page must read already_delivered_count and stop deriving it: the old
    # header arithmetic (duplicate_count - same_run) also counts a duplicate tax row
    # past the 18-month cap, which no view ever lists. It said 4 for these 3 rows.
    assert (new_view["duplicate_count"] - new_view["same_run_duplicate_count"]
            == new_view["already_delivered_count"] + len(ids["tax_capped"]))

    _, new_csv = await _csv_parcels(client, viewed, starter_token)
    resp, dlv_csv = await _csv_parcels(
        client, viewed, starter_token, category="already_delivered")
    assert new_csv == {"PARCEL-NEW-0", "PARCEL-NEW-1", "PARCEL-NEW-2"}
    assert dlv_csv == {"PARCEL-DLV-A", "PARCEL-DLV-B", "PARCEL-DLV-LEGACY"}
    assert "_already_delivered.csv" in resp.headers["content-disposition"]


async def test_a_run_with_only_new_leads_has_an_empty_delivered_view(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    # An earlier run with leads exists, so the new view's "previous results" hint
    # machinery has something to find; the delivered view must not borrow it.
    earlier = await _job(db, starter_user, scraper_config, created_at=NOW - timedelta(days=9))
    await _row(db, earlier, starter_user.id, tag="OLD")
    viewed = await _job(db, starter_user, scraper_config)
    for i in range(2):
        await _row(db, viewed, starter_user.id, tag=f"N{i}")

    body = await _delivered(client, viewed, starter_token)
    assert body["already_delivered_count"] == 0
    assert body["total"] == 0 and body["items"] == []
    assert body["previous_job_id"] is None
    assert (await _results(client, viewed, starter_token))["new_count"] == 2


async def test_a_run_with_only_delivered_leads_lists_them_all(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    viewed = await _job(db, starter_user, scraper_config)
    ids = {await _row(db, viewed, starter_user.id, tag=f"D{i}", duplicate=True,
                      reason="prior_run") for i in range(4)}

    assert (await _results(client, viewed, starter_token))["new_count"] == 0
    body = await _delivered(client, viewed, starter_token)
    assert body["already_delivered_count"] == body["total"] == 4
    assert {r["id"] for r in body["items"]} == ids


# ─── 2. Search, sort and paging stay server-side inside the category ────────────

async def test_paging_walks_the_whole_delivered_set_exactly_once(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    viewed = await _job(db, starter_user, scraper_config)
    await _row(db, viewed, starter_user.id, tag="NEWONE")
    ids = {await _row(db, viewed, starter_user.id, tag=f"P{i}", duplicate=True,
                      reason="prior_run") for i in range(7)}

    seen: list[str] = []
    for page in (1, 2, 3):
        body = await _delivered(client, viewed, starter_token, page=page, page_size=3)
        assert body["total"] == 7
        seen += [r["id"] for r in body["items"]]
    assert len(seen) == 7 and set(seen) == ids


async def test_search_matches_only_inside_the_delivered_set(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    viewed = await _job(db, starter_user, scraper_config)
    # Same searchable text on a NEW row: it must not leak into the delivered view.
    await _row(db, viewed, starter_user.id, tag="ZEBRA-NEW")
    hit = await _row(db, viewed, starter_user.id, tag="ZEBRA-OLD", duplicate=True,
                     reason="prior_run")
    await _row(db, viewed, starter_user.id, tag="OTHER", duplicate=True, reason="prior_run")

    by_name = await _delivered(client, viewed, starter_token, q="ZEBRA")
    assert [r["id"] for r in by_name["items"]] == [hit] and by_name["total"] == 1
    by_parcel = await _delivered(client, viewed, starter_token, q="PARCEL-ZEBRA-OLD")
    assert [r["id"] for r in by_parcel["items"]] == [hit]
    # The tab number describes the run, not the search.
    assert by_name["already_delivered_count"] == 2


async def test_sort_orders_the_delivered_set_before_paging(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    viewed = await _job(db, starter_user, scraper_config)
    early = await _row(db, viewed, starter_user.id, tag="E", duplicate=True,
                       reason="prior_run", date_recorded="01/05/2026")
    late = await _row(db, viewed, starter_user.id, tag="L", duplicate=True,
                      reason="prior_run", date_recorded="08/20/2026")

    desc = await _delivered(client, viewed, starter_token, sort="date_desc", page_size=1)
    asc = await _delivered(client, viewed, starter_token, sort="date_asc", page_size=1)
    assert desc["items"][0]["id"] == late
    assert asc["items"][0]["id"] == early


async def test_an_unknown_category_is_rejected_not_ignored(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    viewed = await _job(db, starter_user, scraper_config)
    for path in ("results", "export-url", "download"):
        resp = await client.get(
            f"/jobs/{viewed}/{path}", params={"category": "combined"},
            headers=_auth(starter_token),
        )
        assert resp.status_code == 422, (path, resp.text)


# ─── 3. Provenance says only what the data supports ─────────────────────────────

async def test_provenance_distinguishes_proven_matched_gone_and_unrecorded(
    client: AsyncClient, starter_user: User, starter_token: str,
    business_user: User, scraper_config: ScraperConfig, db,
):
    at = NOW - timedelta(days=3)
    source = await _job(db, starter_user, scraper_config, created_at=at)
    h_proven, h_matched = uuid.uuid4().hex, uuid.uuid4().hex
    await _row(db, source, starter_user.id, tag="ORIG", dedup_hash=h_proven)
    # The source run's own row for this property never became a lead there.
    await _row(db, source, starter_user.id, tag="ORIGNOADDR", dedup_hash=h_matched,
               address=None)
    unfinished = await _job(db, starter_user, scraper_config, status="scraping",
                            created_at=at)

    other_cfg = ScraperConfig(id=str(uuid.uuid4()), user_id=business_user.id, name="theirs",
                              county="Pierce", state="WA", record_type="probate")
    db.add(other_cfg)
    await db.commit()
    foreign = await _job(db, business_user, other_cfg, created_at=at)
    await _row(db, foreign, business_user.id, tag="THEIRS", dedup_hash=h_proven)

    viewed = await _job(db, starter_user, scraper_config)
    rid = {
        "proven": await _row(db, viewed, starter_user.id, tag="R1", duplicate=True,
                             reason="prior_run", source_job=source, source_at=at,
                             dedup_hash=h_proven),
        "matched": await _row(db, viewed, starter_user.id, tag="R2", duplicate=True,
                              reason="prior_run", source_job=source, source_at=at,
                              dedup_hash=h_matched),
        "purged": await _row(db, viewed, starter_user.id, tag="R3", duplicate=True,
                             reason="prior_run", source_job=str(uuid.uuid4()),
                             source_at=at),
        "unfinished": await _row(db, viewed, starter_user.id, tag="R4", duplicate=True,
                                 reason="prior_run", source_job=unfinished, source_at=at),
        "foreign": await _row(db, viewed, starter_user.id, tag="R5", duplicate=True,
                              reason="prior_run", source_job=foreign, source_at=at,
                              dedup_hash=h_proven),
        "unrecorded": await _row(db, viewed, starter_user.id, tag="R6", duplicate=True),
    }

    by_id = {r["id"]: r for r in (await _delivered(client, viewed, starter_token))["items"]}
    got = {k: (by_id[v]["duplicate_source_available"], by_id[v]["duplicate_original_visible"])
           for k, v in rid.items()}
    assert got == {
        "proven": (True, True),
        "matched": (True, False),
        "purged": (False, False),
        "unfinished": (False, False),
        # Never confirms another account's run exists, even when the key matches.
        "foreign": (False, False),
        "unrecorded": (False, False),
    }
    assert by_id[rid["proven"]]["duplicate_source_job_id"] == source
    assert by_id[rid["proven"]]["duplicate_source_at"] is not None
    # A run the account cannot open is never named, only dated.
    for k in ("purged", "unfinished", "foreign"):
        assert by_id[rid[k]]["duplicate_source_job_id"] is None, k
        assert by_id[rid[k]]["duplicate_source_at"] is not None, k
    assert by_id[rid["unrecorded"]]["duplicate_source_at"] is None

    # The new view carries no provenance claims at all.
    new_items = (await _results(client, viewed, starter_token))["items"]
    assert all(r["duplicate_source_available"] is None for r in new_items)


async def test_the_original_run_link_is_openable_by_its_owner_only(
    client: AsyncClient, starter_user: User, starter_token: str,
    business_token: str, scraper_config: ScraperConfig, db,
):
    at = NOW - timedelta(days=2)
    source = await _job(db, starter_user, scraper_config, created_at=at)
    h = uuid.uuid4().hex
    await _row(db, source, starter_user.id, tag="ORIG", dedup_hash=h)
    viewed = await _job(db, starter_user, scraper_config)
    await _row(db, viewed, starter_user.id, tag="DUP", duplicate=True, reason="prior_run",
               source_job=source, source_at=at, dedup_hash=h)

    item = (await _delivered(client, viewed, starter_token))["items"][0]
    link = item["duplicate_source_job_id"]
    opened = await _results(client, link, starter_token)
    assert [r["parcel_id"] for r in opened["items"]] == ["PARCEL-ORIG"]

    for path in ("", "/results"):
        resp = await client.get(f"/jobs/{link}{path}", headers=_auth(business_token))
        assert resp.status_code == 404, path


# ─── 4. Tenant isolation ────────────────────────────────────────────────────────

async def test_another_account_cannot_list_count_or_download_my_delivered_rows(
    client: AsyncClient, starter_user: User, starter_token: str,
    business_token: str, scraper_config: ScraperConfig, db,
):
    run = await _mixed_run(db, starter_user, scraper_config)
    mine = run["viewed"]

    for path, params in (
        ("results", {"category": "already_delivered"}),
        ("results", {"category": "already_delivered", "q": "DLV"}),
        ("export-url", {"category": "already_delivered"}),
        ("download", {"category": "already_delivered"}),
    ):
        resp = await client.get(f"/jobs/{mine}/{path}", params=params,
                                headers=_auth(business_token))
        assert resp.status_code == 404, (path, resp.status_code)


async def test_a_download_token_for_one_run_cannot_open_another(
    client: AsyncClient, starter_user: User, starter_token: str,
    business_user: User, scraper_config: ScraperConfig, db,
):
    from src.api.download_tokens import mint_download_token

    run = await _mixed_run(db, starter_user, scraper_config)
    other_cfg = ScraperConfig(id=str(uuid.uuid4()), user_id=business_user.id, name="theirs",
                              county="Pierce", state="WA", record_type="probate")
    db.add(other_cfg)
    await db.commit()
    theirs = await _job(db, business_user, other_cfg)
    await _row(db, theirs, business_user.id, tag="THEIRS", duplicate=True, reason="prior_run")

    # My valid token, replayed against their run and against my own other run.
    token = mint_download_token(str(starter_user.id), run["viewed"], ttl_seconds=60)
    for target in (theirs, run["earlier"]):
        resp = await client.get(f"/jobs/{target}/download",
                                params={"token": token, "category": "already_delivered"})
        assert resp.status_code == 403, target

    # The export-url flow carries the category into a token bound to MY run.
    url = (await client.get(f"/jobs/{run['viewed']}/export-url",
                            params={"category": "already_delivered"},
                            headers=_auth(starter_token))).json()["url"]
    assert "category=already_delivered" in url
    resp = await client.get(url)
    assert resp.status_code == 200
    assert "PARCEL-DLV-A" in resp.text and "PARCEL-NEW-0" not in resp.text


async def test_another_accounts_claim_never_makes_my_lead_already_delivered(db, starter_user,
                                                                             business_user):
    """The mechanism behind 'already delivered' is the claim ledger's uniqueness,
    which is per ACCOUNT. Account A holding a property's claim must leave account
    B free to claim, and therefore deliver and be billed for, the same property.
    Production, 2026-09-17: 210 of the 227 screenshot hashes are also held by other
    accounts, and every one of the 227 has its own account's claim."""
    shared = uuid.uuid4().hex
    claim = sa_text(
        "INSERT INTO delivered_records (id, user_id, dedup_hash, first_delivered_at) "
        "VALUES (CAST(:i AS uuid), CAST(:u AS uuid), :h, NOW()) "
        "ON CONFLICT (user_id, dedup_hash) DO NOTHING RETURNING dedup_hash"
    )
    a = await db.execute(claim, {"i": str(uuid.uuid4()), "u": business_user.id, "h": shared})
    assert a.scalar_one_or_none() == shared
    b = await db.execute(claim, {"i": str(uuid.uuid4()), "u": starter_user.id, "h": shared})
    assert b.scalar_one_or_none() == shared, "A's claim blocked B's first delivery"
    again = await db.execute(claim, {"i": str(uuid.uuid4()), "u": starter_user.id, "h": shared})
    assert again.scalar_one_or_none() is None, "B's own second run must lose to B's claim"
    await db.rollback()


# ─── 5. Reading history is free: no quota, no skip trace, no claim change ───────

async def _snapshot(db, user_id: str, job_id: str) -> tuple:
    await db.rollback()  # a fresh snapshot, not the session's cached view
    used = (await db.execute(select(User.records_used).where(User.id == user_id))).scalar_one()
    counts = (await db.execute(sa_text(
        "SELECT (SELECT count(*) FROM skip_trace_queues WHERE user_id = CAST(:u AS uuid)), "
        "       (SELECT count(*) FROM skip_trace_meter_events), "
        "       (SELECT count(*) FROM delivered_records WHERE user_id = CAST(:u AS uuid))"
    ), {"u": user_id})).one()
    rows = (await db.execute(sa_text(
        "SELECT id::text, skip_trace_status, skip_trace_attempted_at, is_duplicate, "
        "       duplicate_reason FROM results WHERE job_id = CAST(:j AS uuid) ORDER BY id"
    ), {"j": job_id})).all()
    return used, tuple(counts), tuple(tuple(r) for r in rows)


async def test_viewing_searching_and_downloading_history_writes_nothing(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    viewed = await _job(db, starter_user, scraper_config)
    await _row(db, viewed, starter_user.id, tag="NEW")
    # Previously enriched and traced: its contact data was copied from the original.
    traced = await _row(
        db, viewed, starter_user.id, tag="TRACED", duplicate=True, reason="prior_run",
        phone="2535550100", phones=[{"number": "2535550100", "type": "Mobile"}],
        email="owner@example.com", skip_trace_status="hit",
        skip_trace_attempted_at=NOW - timedelta(days=4),
    )
    # Never traced: viewing it must not queue a paid lookup.
    await _row(db, viewed, starter_user.id, tag="UNTRACED", duplicate=True,
               reason="prior_run")

    before = await _snapshot(db, starter_user.id, viewed)

    body = await _delivered(client, viewed, starter_token)
    await _delivered(client, viewed, starter_token, q="TRACED")
    await _delivered(client, viewed, starter_token, page=2, page_size=1)
    await _results(client, viewed, starter_token)
    await _csv_parcels(client, viewed, starter_token, category="already_delivered")
    await client.get(f"/jobs/{viewed}/export-url", params={"category": "already_delivered"},
                     headers=_auth(starter_token))

    assert await _snapshot(db, starter_user.id, viewed) == before

    row = next(r for r in body["items"] if r["id"] == traced)
    assert row["phone"] == "2535550100" and row["skip_trace_status"] == "hit"
    assert row["email"] == "owner@example.com"
