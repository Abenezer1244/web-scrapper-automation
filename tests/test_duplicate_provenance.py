"""The results page must be able to SHOW who delivered a duplicate, not just assert it.

A Starter account opened a run that reported "0 new, 49 already delivered" and
"All 49 records ... were duplicates of leads you already received", then clicked
the page's own "View previous results" button to check. The button took them to a
run that had happened two months LATER and that had delivered none of those
leads, because `previous_job_id` was picked by "newest DONE sibling job with
visible leads" with no bound requiring it to precede the run being viewed.

The classification was correct. The account really had received those leads, from
its own earlier run. But the one affordance offered to verify that pointed
somewhere unrelated, so a correct duplicate count was reported as a cross-account
data leak.

Two things are asserted here:

  1. "Previous" means previous. The link can never point forward in time.
  2. A duplicate carries provenance stamped when it was classified, and the API
     reports what it does not know as unattributed instead of guessing. That
     matters because `delivered_records` cannot answer the question after the
     fact — it is a mutable CLAIM ledger the request path holds no privilege on,
     and 82% of its production rows already point at a purged job.

Real DB + real endpoints (conftest `db`/`client`/token fixtures) — no mocks.
"""
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

import src.db.session as _db_session
from src.db.models import Job, Result, ScraperConfig, User


async def _job(
    user: User,
    config: ScraperConfig,
    *,
    created_at: datetime,
    status: str = "done",
    record_count: int = 0,
) -> str:
    job_id = str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(Job(
            id=job_id,
            user_id=user.id,
            scraper_config_id=config.id,
            status=status,
            trigger="manual",
            record_count=record_count,
            created_at=created_at,
        ))
        await s.commit()
    return job_id


async def _rows(job_id: str, user_id: str, specs: list[dict]) -> None:
    """specs: {duplicate, source_job, source_at, reason, hash}."""
    async with _db_session.AsyncSessionLocal() as s:
        for i, spec in enumerate(specs):
            s.add(Result(
                id=str(uuid.uuid4()),
                job_id=job_id,
                user_id=user_id,
                party_name=f"OWNER {i}",
                property_address=f"{i} MAIN ST",
                dedup_hash=spec.get("hash") or uuid.uuid4().hex,
                is_duplicate=spec["duplicate"],
                duplicate_source_job_id=spec.get("source_job"),
                duplicate_source_at=spec.get("source_at"),
                duplicate_reason=spec.get("reason"),
            ))
        await s.commit()


async def _results(client: AsyncClient, job_id: str, token: str) -> dict:
    resp = await client.get(
        f"/jobs/{job_id}/results", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


NOW = datetime.now(UTC)


# ─── 1. "Previous" must mean previous ───────────────────────────────────────────

async def test_previous_results_link_never_points_at_a_later_run(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """The reported bug, reduced to its shape.

    Three runs on one config: an earlier one with leads, the all-duplicate one
    being viewed, and a LATER one with leads. Ordering by created_at DESC with no
    bound picked the later run, which is what production served.
    """
    earlier = await _job(
        starter_user, scraper_config, created_at=NOW - timedelta(days=60),
        record_count=122,
    )
    await _rows(earlier, starter_user.id, [{"duplicate": False}] * 3)

    viewed = await _job(
        starter_user, scraper_config, created_at=NOW - timedelta(days=30)
    )
    await _rows(viewed, starter_user.id, [{"duplicate": True}] * 4)

    later = await _job(starter_user, scraper_config, created_at=NOW, record_count=32)
    await _rows(later, starter_user.id, [{"duplicate": False}] * 3)

    body = await _results(client, viewed, starter_token)

    assert body["previous_job_id"] == earlier
    assert body["previous_job_id"] != later, (
        "the 'View previous results' link pointed at a run that had not happened "
        "yet when the run being viewed executed"
    )


async def test_previous_link_is_absent_when_no_earlier_run_exists(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """With only a later run to choose from, offer nothing rather than the wrong
    thing — the UI falls back to 'View all records'."""
    viewed = await _job(
        starter_user, scraper_config, created_at=NOW - timedelta(days=30)
    )
    await _rows(viewed, starter_user.id, [{"duplicate": True}] * 2)

    later = await _job(starter_user, scraper_config, created_at=NOW, record_count=5)
    await _rows(later, starter_user.id, [{"duplicate": False}] * 2)

    body = await _results(client, viewed, starter_token)
    assert body["previous_job_id"] is None
    assert body["previous_job_run_at"] is None
    assert later  # the later run exists and was deliberately not offered


async def test_previous_link_carries_its_date_so_the_copy_can_name_it(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    earlier_at = NOW - timedelta(days=60)
    earlier = await _job(
        starter_user, scraper_config, created_at=earlier_at, record_count=9
    )
    await _rows(earlier, starter_user.id, [{"duplicate": False}] * 2)
    viewed = await _job(starter_user, scraper_config, created_at=NOW)
    await _rows(viewed, starter_user.id, [{"duplicate": True}] * 2)

    body = await _results(client, viewed, starter_token)
    assert body["previous_job_id"] == earlier
    assert body["previous_job_run_at"] is not None


# ─── 2. Provenance: say what we know, and mark what we do not ───────────────────

async def test_duplicates_report_the_run_that_actually_delivered_them(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    source_at = NOW - timedelta(days=60)
    source = await _job(
        starter_user, scraper_config, created_at=source_at, record_count=122
    )
    await _rows(source, starter_user.id, [{"duplicate": False}] * 2)

    viewed = await _job(starter_user, scraper_config, created_at=NOW)
    await _rows(viewed, starter_user.id, [
        {"duplicate": True, "source_job": source, "source_at": source_at,
         "reason": "prior_run"}
    ] * 3)

    body = await _results(client, viewed, starter_token)

    assert body["duplicate_count"] == 3
    assert body["unattributed_duplicate_count"] == 0
    assert len(body["duplicate_sources"]) == 1
    src = body["duplicate_sources"][0]
    assert src["job_id"] == source
    assert src["duplicate_count"] == 3
    assert src["job_available"] is True
    assert src["run_at"] is not None


async def test_multiple_source_runs_are_reported_as_groups_not_one_link(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """One link cannot explain a banner whose duplicates came from two runs. The
    API returns groups, descending by count, so the UI names the dominant one and
    says there were others rather than silently picking."""
    a_at = NOW - timedelta(days=90)
    b_at = NOW - timedelta(days=45)
    a = await _job(starter_user, scraper_config, created_at=a_at, record_count=5)
    await _rows(a, starter_user.id, [{"duplicate": False}])
    b = await _job(starter_user, scraper_config, created_at=b_at, record_count=5)
    await _rows(b, starter_user.id, [{"duplicate": False}])

    viewed = await _job(starter_user, scraper_config, created_at=NOW)
    await _rows(viewed, starter_user.id, (
        [{"duplicate": True, "source_job": a, "source_at": a_at,
          "reason": "prior_run"}] * 4
        + [{"duplicate": True, "source_job": b, "source_at": b_at,
            "reason": "prior_run"}] * 2
    ))

    body = await _results(client, viewed, starter_token)
    assert [s["duplicate_count"] for s in body["duplicate_sources"]] == [4, 2]
    assert body["duplicate_sources"][0]["job_id"] == a
    assert sum(s["duplicate_count"] for s in body["duplicate_sources"]) == \
        body["duplicate_count"]


async def test_rows_without_provenance_are_unattributed_not_guessed(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """Every row classified before provenance was recorded carries NULL. The API
    must count those separately so the copy can drop the specific claim, instead
    of attaching them to whichever run happens to be handy."""
    earlier = await _job(
        starter_user, scraper_config, created_at=NOW - timedelta(days=60),
        record_count=9,
    )
    await _rows(earlier, starter_user.id, [{"duplicate": False}] * 2)

    viewed = await _job(starter_user, scraper_config, created_at=NOW)
    await _rows(viewed, starter_user.id, [{"duplicate": True}] * 5)

    body = await _results(client, viewed, starter_token)
    assert body["duplicate_sources"] == []
    assert body["unattributed_duplicate_count"] == 5
    # The earlier-run link is still offered, because it is true that an earlier
    # run exists; it is the sentence that must stop naming it as the source.
    assert body["previous_job_id"] == earlier


async def test_a_purged_source_run_is_reported_but_not_linkable(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """duplicate_source_job_id has no foreign key on purpose, so it outlives the
    job it names. The group still explains the count; job_available says the link
    must not be offered."""
    gone = str(uuid.uuid4())
    viewed = await _job(starter_user, scraper_config, created_at=NOW)
    await _rows(viewed, starter_user.id, [
        {"duplicate": True, "source_job": gone,
         "source_at": NOW - timedelta(days=30), "reason": "prior_run"}
    ] * 2)

    body = await _results(client, viewed, starter_token)
    assert len(body["duplicate_sources"]) == 1
    assert body["duplicate_sources"][0]["job_available"] is False
    assert body["duplicate_sources"][0]["duplicate_count"] == 2


async def test_a_source_run_that_never_finished_is_not_called_a_delivery(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """A claim is written before its job completes, so a claim alone is not
    evidence the customer received anything. A failed source run must not be
    presented as the run that delivered these leads."""
    crashed = await _job(
        starter_user, scraper_config, created_at=NOW - timedelta(days=10),
        status="failed",
    )
    viewed = await _job(starter_user, scraper_config, created_at=NOW)
    await _rows(viewed, starter_user.id, [
        {"duplicate": True, "source_job": crashed,
         "source_at": NOW - timedelta(days=10), "reason": "prior_run"}
    ] * 3)

    body = await _results(client, viewed, starter_token)
    assert body["duplicate_sources"][0]["job_available"] is False


async def test_same_run_collapse_is_not_reported_as_a_prior_delivery(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """trustee_sale collapses two filings on one property so it bills once. Those
    rows carry is_duplicate=true but were never delivered before, and the banner
    must not tell the user they already received them."""
    viewed = await _job(starter_user, scraper_config, created_at=NOW)
    await _rows(viewed, starter_user.id, [
        {"duplicate": True, "source_job": viewed, "reason": "same_run"}
    ] * 3)

    body = await _results(client, viewed, starter_token)
    assert body["same_run_duplicate_count"] == 3
    assert body["duplicate_count"] == 3
    # Not offered as a prior delivery, and not counted as unknown either.
    assert body["duplicate_sources"] == []
    assert body["unattributed_duplicate_count"] == 0


# ─── 3. Tenant isolation on the surfaces this change touches ────────────────────

async def test_another_accounts_run_is_not_readable_by_id(
    client: AsyncClient, starter_user: User, starter_token: str,
    business_user: User, scraper_config: ScraperConfig, db,
):
    """The report's original hypothesis. A results id belonging to another
    account must 404 from the backend, not merely be hidden by the UI."""
    other_config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=business_user.id, name="theirs",
        county="Pierce", state="WA", record_type="probate",
    )
    db.add(other_config)
    await db.commit()
    theirs = await _job(business_user, other_config, created_at=NOW, record_count=5)
    await _rows(theirs, business_user.id, [{"duplicate": False}] * 2)

    resp = await client.get(
        f"/jobs/{theirs}/results", headers={"Authorization": f"Bearer {starter_token}"}
    )
    assert resp.status_code == 404


async def test_provenance_never_names_another_accounts_run(
    client: AsyncClient, starter_user: User, starter_token: str,
    business_user: User, scraper_config: ScraperConfig, db,
):
    """Defense in depth. Even if a source id somehow named a job owned by another
    account, it must degrade to not-linkable rather than confirming that job
    exists."""
    other_config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=business_user.id, name="theirs",
        county="Pierce", state="WA", record_type="probate",
    )
    db.add(other_config)
    await db.commit()
    theirs = await _job(business_user, other_config, created_at=NOW - timedelta(days=5))

    viewed = await _job(starter_user, scraper_config, created_at=NOW)
    await _rows(viewed, starter_user.id, [
        {"duplicate": True, "source_job": theirs,
         "source_at": NOW - timedelta(days=5), "reason": "prior_run"}
    ])

    body = await _results(client, viewed, starter_token)
    assert body["duplicate_sources"][0]["job_available"] is False


# ─── 4. The claim ledger must not outlive, or predecease, a real delivery ───────
#
# `delivered_records` is what makes a lead "already delivered" forever. Two
# release paths were wrong in opposite directions, and both make that sentence a
# lie: one kept a claim for a lead that was never delivered, the other dropped a
# claim for a lead that was. Asserted at the SQL level, because that is where the
# guards live and where a mistake is silent.

from sqlalchemy import text as _text  # noqa: E402

from src.api.lead_actionability import address_actionable_sql  # noqa: E402


async def _claim(user_id: str, job_id: str, dedup_hash: str) -> None:
    async with _db_session.AsyncSessionLocal() as s:
        await s.execute(_text(
            "INSERT INTO delivered_records "
            "(id, user_id, dedup_hash, first_job_id, first_delivered_at) "
            "VALUES (:i, CAST(:u AS uuid), :h, :j, NOW())"
        ), {"i": str(uuid.uuid4()), "u": user_id, "h": dedup_hash, "j": job_id})
        await s.commit()


async def _claim_count(user_id: str, dedup_hash: str) -> int:
    async with _db_session.AsyncSessionLocal() as s:
        return (await s.execute(_text(
            "SELECT count(*) FROM delivered_records "
            "WHERE user_id = CAST(:u AS uuid) AND dedup_hash = :h"
        ), {"u": user_id, "h": dedup_hash})).scalar_one()


# The plan-cap release, verbatim from workers/tasks.py.
_CAP_RELEASE = (
    'DELETE FROM delivered_records dr USING results r '
    'WHERE dr.user_id = CAST(:uid AS uuid) '
    '  AND dr.first_job_id = :jid '
    '  AND dr.dedup_hash = r.dedup_hash '
    '  AND r.id = ANY(CAST(:ids AS uuid[])) '
    '  AND r.user_id = CAST(:uid AS uuid) '
    '  AND r.dedup_hash IS NOT NULL '
    '  AND NOT EXISTS ( '
    '        SELECT 1 FROM results keep '
    '        WHERE keep.job_id = :jid '
    '          AND keep.user_id = CAST(:uid AS uuid) '
    '          AND keep.dedup_hash = dr.dedup_hash '
    '          AND keep.is_duplicate = false '
    '          AND NOT (keep.id = ANY(CAST(:ids AS uuid[]))) '
    '          AND {keep_rule} '
    '  )'.format(keep_rule=address_actionable_sql("keep"))
)


async def _run_cap_release(user_id: str, job_id: str, capped_ids: list[str]) -> None:
    async with _db_session.AsyncSessionLocal() as s:
        await s.execute(
            _text(_CAP_RELEASE),
            {"uid": user_id, "jid": job_id, "ids": capped_ids},
        )
        await s.commit()


async def test_cap_release_keeps_a_claim_a_shipped_sibling_still_needs(
    starter_user: User, scraper_config: ScraperConfig,
):
    """Two rows in one job on the SAME property: one over the plan cap, one
    shipped and billed. Releasing the excluded row's claim also released the
    shipped row's, so the next run delivered and billed that property again."""
    job_id = await _job(starter_user, scraper_config, created_at=NOW)
    shared = uuid.uuid4().hex
    async with _db_session.AsyncSessionLocal() as s:
        shipped_id, capped_id = str(uuid.uuid4()), str(uuid.uuid4())
        for rid in (shipped_id, capped_id):
            s.add(Result(
                id=rid, job_id=job_id, user_id=starter_user.id,
                party_name="OWNER", property_address="1 MAIN ST",
                dedup_hash=shared, is_duplicate=False,
            ))
        await s.commit()
    await _claim(starter_user.id, job_id, shared)

    await _run_cap_release(starter_user.id, job_id, [capped_id])

    assert await _claim_count(starter_user.id, shared) == 1, (
        "the shipped sibling was billed for this property but no longer holds "
        "its dedup claim, so the next run will bill for it again"
    )


async def test_cap_release_still_frees_a_claim_no_shipped_row_needs(
    starter_user: User, scraper_config: ScraperConfig,
):
    """The guard must not become a leak in the other direction: when EVERY row on
    that hash was excluded, the claim has to go, or the lead is suppressed
    forever without ever being delivered."""
    job_id = await _job(starter_user, scraper_config, created_at=NOW)
    lonely = uuid.uuid4().hex
    async with _db_session.AsyncSessionLocal() as s:
        capped_id = str(uuid.uuid4())
        s.add(Result(
            id=capped_id, job_id=job_id, user_id=starter_user.id,
            party_name="OWNER", property_address="2 MAIN ST",
            dedup_hash=lonely, is_duplicate=False,
        ))
        await s.commit()
    await _claim(starter_user.id, job_id, lonely)

    await _run_cap_release(starter_user.id, job_id, [capped_id])

    assert await _claim_count(starter_user.id, lonely) == 0


async def test_cap_release_ignores_a_duplicate_sibling(
    starter_user: User, scraper_config: ScraperConfig,
):
    """A sibling already flagged is_duplicate is not being delivered, so it is not
    a reason to keep the claim."""
    job_id = await _job(starter_user, scraper_config, created_at=NOW)
    shared = uuid.uuid4().hex
    async with _db_session.AsyncSessionLocal() as s:
        dup_id, capped_id = str(uuid.uuid4()), str(uuid.uuid4())
        s.add(Result(
            id=dup_id, job_id=job_id, user_id=starter_user.id, party_name="A",
            property_address="3 MAIN ST", dedup_hash=shared, is_duplicate=True,
        ))
        s.add(Result(
            id=capped_id, job_id=job_id, user_id=starter_user.id, party_name="B",
            property_address="3 MAIN ST", dedup_hash=shared, is_duplicate=False,
        ))
        await s.commit()
    await _claim(starter_user.id, job_id, shared)

    await _run_cap_release(starter_user.id, job_id, [capped_id])

    assert await _claim_count(starter_user.id, shared) == 0


async def test_cap_release_never_touches_another_accounts_claim(
    starter_user: User, business_user: User, scraper_config: ScraperConfig,
):
    """The same parcel is routinely claimed by many accounts. Releasing one
    account's claim must leave every other account's intact."""
    job_id = await _job(starter_user, scraper_config, created_at=NOW)
    shared = uuid.uuid4().hex
    async with _db_session.AsyncSessionLocal() as s:
        capped_id = str(uuid.uuid4())
        s.add(Result(
            id=capped_id, job_id=job_id, user_id=starter_user.id, party_name="A",
            property_address="4 MAIN ST", dedup_hash=shared, is_duplicate=False,
        ))
        await s.commit()
    await _claim(starter_user.id, job_id, shared)
    await _claim(business_user.id, str(uuid.uuid4()), shared)

    await _run_cap_release(starter_user.id, job_id, [capped_id])

    assert await _claim_count(starter_user.id, shared) == 0
    assert await _claim_count(business_user.id, shared) == 1


async def test_cap_release_frees_a_claim_pinned_only_by_an_addressless_row(
    starter_user: User, scraper_config: ScraperConfig,
):
    """An address-less row is never exported and never billed. Treating it as a
    reason to keep the claim would suppress that lead from every future run while
    the user had never received it once."""
    job_id = await _job(starter_user, scraper_config, created_at=NOW)
    shared = uuid.uuid4().hex
    async with _db_session.AsyncSessionLocal() as s:
        capped_id, blank_id = str(uuid.uuid4()), str(uuid.uuid4())
        s.add(Result(
            id=capped_id, job_id=job_id, user_id=starter_user.id, party_name="A",
            property_address="5 MAIN ST", dedup_hash=shared, is_duplicate=False,
        ))
        s.add(Result(
            id=blank_id, job_id=job_id, user_id=starter_user.id, party_name="B",
            property_address=None, mailing_address=None,
            dedup_hash=shared, is_duplicate=False,
        ))
        await s.commit()
    await _claim(starter_user.id, job_id, shared)

    await _run_cap_release(starter_user.id, job_id, [capped_id])

    assert await _claim_count(starter_user.id, shared) == 0
