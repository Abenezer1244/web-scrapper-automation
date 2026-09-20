"""The survivor is elected before enrichment, so enrichment gets to change it.

`collapse_same_run_siblings` has to run before the export and before the billing
count, which means it ranks on the addresses as SCRAPED. Two addressless rows
sharing a strong PARCEL hash are separated by completeness alone -- and then the
Pierce legal-description repair inside `_run_inline_enrichment` can fill an
address onto the row that LOST. The survivor stays undeliverable, the one
actionable row sits flagged is_duplicate, and no retry ever revisits it because
the collapse SELECT reads `is_duplicate = false` (Codex P2).

Two more things the collapse left behind, covered here:

- whatever a loser ALONE carried (the only heir on a later filing, the only legal
  description) went with it, because the collapse marked the row and stopped;
- the `delivered_records` claim kept pointing at whichever row PostgreSQL reached
  first inside the batched `ON CONFLICT DO NOTHING`, which has nothing to do with
  the actionability ranking -- so the claim could name a row the collapse had
  just flagged is_duplicate.

Real DB (conftest `db` fixture) -- no mocks.
"""
import uuid

from src.db.models import DeliveredRecord, Job, Result, ScraperConfig, User
from src.workers.property_identity import legacy_strong_signature
from src.workers.tasks_helpers.dedup import (
    _merged_survivor_fields,
    collapse_same_run_siblings,
    reconcile_same_run_survivors,
    survivor_sort_key,
)


def _strong(parcel: str, address: str | None) -> str:
    """The REAL hash the worker would store for these values."""
    h = legacy_strong_signature(parcel, address)
    assert h is not None, "test inputs must form a strong identity"
    return h


async def _job(db, user: User, config: ScraperConfig) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status="done", trigger="manual"))
    await db.commit()
    return job_id


async def _row(db, job_id, user_id, dedup_hash, **kw) -> str:
    rid = str(uuid.uuid4())
    kw.setdefault("is_duplicate", False)
    db.add(Result(id=rid, job_id=job_id, user_id=user_id, dedup_hash=dedup_hash, **kw))
    await db.commit()
    return rid


async def _claim(db, user_id, dedup_hash, first_result_id, first_job_id) -> str:
    cid = str(uuid.uuid4())
    db.add(DeliveredRecord(id=cid, user_id=user_id, dedup_hash=dedup_hash,
                           first_result_id=first_result_id, first_job_id=first_job_id))
    await db.commit()
    return cid


async def _collapse(db, job_id, user_id, record_type=None) -> int:
    n = await db.run_sync(
        lambda s: collapse_same_run_siblings(s, job_id, user_id, record_type)
    )
    await db.commit()
    return n


async def _reconcile(db, job_id, user_id, record_type=None) -> int:
    n = await db.run_sync(
        lambda s: reconcile_same_run_survivors(s, job_id, user_id, record_type)
    )
    await db.commit()
    return n


async def _fresh(db, rid) -> Result:
    r = await db.get(Result, rid)
    await db.refresh(r)
    return r


# ── the reported defect ──────────────────────────────────────────────────────


async def test_enrichment_recovering_an_address_moves_the_survivor(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """The whole point. Two addressless filings on one parcel collapse; the
    legal-description repair then gives an address to the row that LOST."""
    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", None)
    # 'a' sorts first today only because neither row is actionable and ids break
    # the tie, so pin the order by party_name completeness instead.
    winner = await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
                        party_name="FIRST FILING")
    loser = await _row(db, job_id, starter_user.id, h, parcel_id="0123456")

    assert await _collapse(db, job_id, starter_user.id) == 1
    assert (await _fresh(db, loser)).is_duplicate is True
    assert (await _fresh(db, winner)).is_duplicate is False

    # Enrichment recovers an address for the COLLAPSED row only.
    collapsed = await _fresh(db, loser)
    collapsed.property_address = "1400 PACIFIC AVE"
    await db.commit()

    assert await _reconcile(db, job_id, starter_user.id) == 1

    # The actionable row is the one the customer now gets.
    assert (await _fresh(db, loser)).is_duplicate is False
    demoted = await _fresh(db, winner)
    assert demoted.is_duplicate is True
    assert demoted.duplicate_reason == "same_run"


async def test_reconciliation_is_idempotent_when_nothing_moved(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """No address recovery -> no swap, and running it twice changes nothing."""
    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", "5 MAIN ST")
    a = await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
                   property_address="5 MAIN ST", party_name="A")
    b = await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
                   property_address="5 MAIN ST", party_name="B")

    await _collapse(db, job_id, starter_user.id)
    before = {r: (await _fresh(db, r)).is_duplicate for r in (a, b)}

    assert await _reconcile(db, job_id, starter_user.id) == 0
    assert await _reconcile(db, job_id, starter_user.id) == 0
    assert {r: (await _fresh(db, r)).is_duplicate for r in (a, b)} == before


async def test_a_prior_run_duplicate_is_never_un_flagged(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """Reconciliation only ever re-orders rows THIS run collapsed. A lead the
    cross-job dedup suppressed as already delivered must stay suppressed, however
    actionable it looks."""
    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", "7 MAIN ST")
    standing = await _row(db, job_id, starter_user.id, h, parcel_id="0123456")
    prior = await _row(
        db, job_id, starter_user.id, h, parcel_id="0123456",
        property_address="7 MAIN ST", mailing_address="PO BOX 1",
        is_duplicate=True, duplicate_reason="prior_run",
        duplicate_source_job_id=str(uuid.uuid4()),
    )

    assert await _reconcile(db, job_id, starter_user.id) == 0

    kept = await _fresh(db, prior)
    assert kept.is_duplicate is True
    assert kept.duplicate_reason == "prior_run"
    assert (await _fresh(db, standing)).is_duplicate is False


async def test_a_group_with_two_standing_rows_is_skipped_not_repaired(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """Electing one survivor out of TWO standing rows would move the duplicate
    count by one and the charge with it. Malformed groups are left alone."""
    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", "8 MAIN ST")
    s1 = await _row(db, job_id, starter_user.id, h, parcel_id="0123456")
    s2 = await _row(db, job_id, starter_user.id, h, parcel_id="0123456")
    collapsed = await _row(
        db, job_id, starter_user.id, h, parcel_id="0123456",
        property_address="8 MAIN ST", is_duplicate=True,
        duplicate_reason="same_run", duplicate_source_job_id=job_id,
    )

    assert await _reconcile(db, job_id, starter_user.id) == 0
    assert (await _fresh(db, s1)).is_duplicate is False
    assert (await _fresh(db, s2)).is_duplicate is False
    assert (await _fresh(db, collapsed)).is_duplicate is True


# ── the claim anchor ─────────────────────────────────────────────────────────


async def test_the_collapse_repoints_a_claim_that_named_the_loser(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """The claim's first_result_id comes from whichever row PostgreSQL reached
    first in the batched upsert. If the collapse then flags that row, the claim
    names a duplicate -- and _reuse_enrichment_for_duplicates copies enrichment
    FROM it."""
    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", "2 MAIN ST")
    # Both rows must carry the SAME parcel and address -- that is what produced
    # the shared strong hash -- so completeness decides the winner.
    usable = await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
                        property_address="2 MAIN ST", mailing_address="PO BOX 9")
    plain = await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
                       property_address="2 MAIN ST")
    claim_id = await _claim(db, starter_user.id, h, plain, job_id)

    assert await _collapse(db, job_id, starter_user.id) == 1
    assert (await _fresh(db, plain)).is_duplicate is True

    claim = await db.get(DeliveredRecord, claim_id)
    await db.refresh(claim)
    assert claim.first_result_id == usable


async def test_reconciliation_moves_the_claim_with_the_survivor(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", None)
    winner = await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
                        party_name="FIRST FILING")
    loser = await _row(db, job_id, starter_user.id, h, parcel_id="0123456")
    claim_id = await _claim(db, starter_user.id, h, winner, job_id)

    await _collapse(db, job_id, starter_user.id)
    recovered = await _fresh(db, loser)
    recovered.property_address = "1400 PACIFIC AVE"
    await db.commit()

    assert await _reconcile(db, job_id, starter_user.id) == 1

    claim = await db.get(DeliveredRecord, claim_id)
    await db.refresh(claim)
    assert claim.first_result_id == loser


async def test_a_claim_owned_by_another_run_is_not_touched(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """Only an anchor that is one of THIS group's losers may move."""
    job_id = await _job(db, starter_user, scraper_config)
    other_job = await _job(db, starter_user, scraper_config)
    other_row = await _row(db, other_job, starter_user.id, _strong("9999999", "3 OAK ST"),
                           parcel_id="9999999", property_address="3 OAK ST")
    h = _strong("0123456", "4 MAIN ST")
    await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
               property_address="4 MAIN ST", mailing_address="PO BOX 2")
    await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
               property_address="4 MAIN ST")
    claim_id = await _claim(db, starter_user.id, h, other_row, other_job)

    await _collapse(db, job_id, starter_user.id)

    claim = await db.get(DeliveredRecord, claim_id)
    await db.refresh(claim)
    assert claim.first_result_id == other_row


# ── the merged source-only fields ────────────────────────────────────────────


async def test_the_only_legal_description_survives_the_collapse(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """A loser carried the only legal description; it used to vanish with it."""
    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", "6 MAIN ST")
    winner = await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
                        property_address="6 MAIN ST", mailing_address="PO BOX 3")
    await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
               property_address="6 MAIN ST", legal_description="LOT 4 BLK 2 PLAT")

    await _collapse(db, job_id, starter_user.id, "probate")

    assert (await _fresh(db, winner)).legal_description == "LOT 4 BLK 2 PLAT"


def test_a_legal_description_the_survivor_already_has_is_never_overwritten():
    """Two filings can carry two genuinely different legals. Concatenating them
    would present an invented string as the property's description."""
    survivor = {"id": "1", "legal_description": "LOT 4 BLK 2"}
    loser = {"id": "2", "legal_description": "LOT 9 BLK 7"}
    updates = _merged_survivor_fields(survivor, [survivor, loser], "probate")
    assert "legal_description" not in updates


def test_heirs_are_unioned_for_probate():
    survivor = {"id": "1", "party_name": "SMITH JOHN", "heirs": "SMITH ANNE"}
    loser = {"id": "2", "party_name": "SMITH JOHN", "heirs": "SMITH ANNE, SMITH BOB"}
    updates = _merged_survivor_fields(survivor, [survivor, loser], "probate")
    assert updates["heirs"] == "SMITH ANNE, SMITH BOB"


def test_the_union_never_makes_the_survivor_their_own_heir():
    """On a reversed-party filing the sibling's heirs field holds the survivor's
    OWN party. Splicing it in would assert they inherit from themselves (Codex)."""
    survivor = {"id": "1", "party_name": "DOE JANE", "heirs": "DOE JOHN"}
    reversed_filing = {"id": "2", "party_name": "DOE JOHN", "heirs": "DOE JANE"}
    updates = _merged_survivor_fields(survivor, [survivor, reversed_filing], "probate")
    assert updates.get("heirs", "DOE JOHN") == "DOE JOHN"


def test_heirs_are_fill_only_for_a_record_type_that_is_not_a_name_list():
    """divorce keeps the OTHER SPOUSE in `heirs`. Unioning two filings would
    invent a multi-party string no source ever asserted."""
    survivor = {"id": "1", "party_name": "A", "heirs": "SPOUSE ONE"}
    loser = {"id": "2", "party_name": "B", "heirs": "SPOUSE TWO"}
    assert "heirs" not in _merged_survivor_fields(survivor, [survivor, loser], "divorce")

    empty = {"id": "1", "party_name": "A", "heirs": None}
    filled = _merged_survivor_fields(empty, [empty, loser], "divorce")
    assert filled["heirs"] == "SPOUSE TWO"


def test_lead_subtype_is_elected_by_the_shared_priority_order():
    """The combined export aggregates subtypes by priority, so the collapse must
    elect by the same order or the two disagree about one property."""
    survivor = {"id": "1", "enrichment_data": {"lead_subtype": "nonprobate_transfer"}}
    loser = {"id": "2", "enrichment_data": {"lead_subtype": "probate_death_inheritance"}}
    updates = _merged_survivor_fields(survivor, [survivor, loser], "probate")
    assert updates["enrichment_data"]["lead_subtype"] == "probate_death_inheritance"


def test_a_weaker_subtype_never_displaces_a_stronger_one():
    survivor = {"id": "1", "enrichment_data": {"lead_subtype": "probate_death_inheritance"}}
    loser = {"id": "2", "enrichment_data": {"lead_subtype": "tod_living_owner_estate_planning"}}
    assert "enrichment_data" not in _merged_survivor_fields(
        survivor, [survivor, loser], "probate"
    )


def test_no_other_enrichment_data_key_travels_between_rows():
    """Copying keys individually across two filings manufactures an object no
    single source produced, and the blob carries per-ROW state such as the
    plan-cap exclusion key (Codex)."""
    survivor = {"id": "1", "enrichment_data": {"lead_subtype": "nonprobate_transfer"}}
    loser = {
        "id": "2",
        "enrichment_data": {
            "lead_subtype": "probate_death_inheritance",
            "billed_amount": "500",
            "delivery_excluded": "over_quota",
            "assessor_current_owner": "SOMEONE ELSE",
        },
    }
    merged = _merged_survivor_fields(survivor, [survivor, loser], "probate")["enrichment_data"]
    assert merged == {"lead_subtype": "probate_death_inheritance"}


# ── the idempotency guard ────────────────────────────────────────────────────


def test_merged_fields_are_not_ranking_inputs():
    """If a merged field ever became a ranking input, a retry would rank on
    values the previous pass merged in and the election would stop being
    idempotent (Codex). Pins survivor_sort_key against exactly that drift."""
    base = {
        "id": "00000000-0000-0000-0000-000000000001",
        "parcel_id": "0123456",
        "property_address": "1 MAIN ST",
        "mailing_address": "PO BOX 1",
        "party_name": "OWNER",
        "date_recorded": "2026-01-01",
    }
    enriched = dict(base)
    enriched.update(
        heirs="A, B, C",
        legal_description="LOT 4 BLK 2",
        enrichment_data={"lead_subtype": "probate_death_inheritance"},
    )
    assert survivor_sort_key(base) == survivor_sort_key(enriched)


# ── the two defects the diff review caught ───────────────────────────────────


def test_an_empty_heir_union_never_falls_back_to_the_excluded_name():
    """The exclusion removed the survivor's own party from the union, leaving it
    empty -- and the fill-only fallback then copied that exact name back in,
    making them their own heir again (Codex diff review)."""
    survivor = {"id": "1", "party_name": "ALICE ROE", "heirs": None}
    loser = {"id": "2", "party_name": "BOB ROE", "heirs": "ALICE ROE"}
    updates = _merged_survivor_fields(survivor, [survivor, loser], "probate")
    assert updates.get("heirs") is None


def test_heirs_naming_only_the_survivor_are_cleared_not_kept():
    """The only way to reach an empty union with a value already present is that
    every name in it was the survivor's own party, which is not an heir."""
    survivor = {"id": "1", "party_name": "ALICE ROE", "heirs": "ALICE ROE"}
    loser = {"id": "2", "party_name": "BOB ROE", "heirs": "ALICE ROE"}
    updates = _merged_survivor_fields(survivor, [survivor, loser], "probate")
    assert "heirs" in updates and updates["heirs"] is None


def test_trustee_sale_is_re_ranked_by_auction_date_not_actionability():
    """The reconciliation must re-order a group by the rule that FORMED it.
    Ranking Auction Leads by actionability would replace the soonest-auction
    survivor with a later one (Codex diff review)."""
    from datetime import date as _date

    from src.workers.tasks_helpers.dedup import sort_key_for

    soonest = {"id": "b", "auction_date": _date(2026, 10, 1),
               "property_address": None, "mailing_address": None}
    later_but_actionable = {"id": "a", "auction_date": _date(2026, 12, 1),
                            "property_address": "1 MAIN ST", "mailing_address": "PO BOX 1"}
    rows = [later_but_actionable, soonest]

    assert sorted(rows, key=sort_key_for("trustee_sale"))[0]["id"] == "b"
    # Every other record type still ranks the deliverable row first.
    assert sorted(rows, key=sort_key_for("probate"))[0]["id"] == "a"


async def test_reconciliation_keeps_the_soonest_auction_survivor(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """End to end: a trustee_sale group must not have its survivor moved just
    because the later-auction sibling got an address."""
    from datetime import date as _date

    job_id = await _job(db, starter_user, scraper_config)
    h = _strong("0123456", None)
    soonest = await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
                         auction_date=_date(2026, 10, 1))
    later = await _row(db, job_id, starter_user.id, h, parcel_id="0123456",
                       auction_date=_date(2026, 12, 1), is_duplicate=True,
                       duplicate_reason="same_run", duplicate_source_job_id=job_id)

    # Enrichment makes the LATER notice the actionable one.
    recovered = await _fresh(db, later)
    recovered.property_address = "1400 PACIFIC AVE"
    await db.commit()

    assert await _reconcile(db, job_id, starter_user.id, "trustee_sale") == 0
    assert (await _fresh(db, soonest)).is_duplicate is False
    assert (await _fresh(db, later)).is_duplicate is True


# ── the ATIP paid-use line (legal cleared NAMING only) ───────────────────────


async def _reuse(db, job_id, user_id) -> int:
    from src.db.models import Job as _Job
    from src.workers.tasks_helpers.enrich import _reuse_enrichment_for_duplicates

    def _go(sync_session):
        job = sync_session.get(_Job, job_id)
        return _reuse_enrichment_for_duplicates(sync_session, job, job_id)

    n = await db.run_sync(_go)
    await db.commit()
    return n


async def test_a_duplicate_reuse_never_copies_contacts_onto_an_atip_named_tacoma_lead(
    db, starter_user: User, scraper_config: ScraperConfig, monkeypatch,
):
    """No new credit is spent here, but copying a settled phone/email onto a lead named
    from Pierce ATIP would still take that name past "owner naming only"."""
    from datetime import UTC, datetime

    from src.config import settings
    from src.scrapers.enrichment.skip_trace import lookup_subject_key

    atip = {"source": "tacoma_code_violations", "owner_source": "pierce_atip",
            "owner_pin": "2021110133", "owner_status": "matched"}
    first_job = await _job(db, starter_user, scraper_config)
    rerun = await _job(db, starter_user, scraper_config)
    h = _strong("2021110133", "2117 AVE S")
    # An entity name routes to an ADVANCED trace (no name is sent), and a row is
    # only traceable at all with a resolvable city/state, so both rows carry one.
    # Since 098 the source also carries the subject its answer was bought for:
    # without it the source is a pre-098 row that donates to nobody, and this
    # test would go green without ever reaching the ATIP policy.
    subject = lookup_subject_key(
        starter_user.id, "2117 AVE S", "TACOMA", "WA", "advanced", None, None,
    )
    traced = await _row(db, first_job, starter_user.id, h, parcel_id="2021110133",
                        property_address="2117 AVE S", party_name="TACOMA TOWN CENTER LLC",
                        property_city="TACOMA", property_state="WA",
                        enrichment_data=atip, phone="2535550100", email="a@b.test",
                        skip_trace_status="hit", skip_trace_attempted_at=datetime.now(UTC),
                        skip_trace_subject_hash=subject)
    duplicate = await _row(db, rerun, starter_user.id, h, parcel_id="2021110133",
                           property_address="2117 AVE S", party_name="TACOMA TOWN CENTER LLC",
                           property_city="TACOMA", property_state="WA",
                           enrichment_data=atip, skip_trace_status="not_attempted",
                           is_duplicate=True)
    await _claim(db, starter_user.id, h, traced, first_job)

    # Two independent gates now refuse this while the switch is off: the SQL ATIP
    # predicate, and the subject gate (code_violation_skip_trace_allowed makes
    # build_pending_row_payload decline, so the target has no subject to match).
    # The switch-on half below is what proves the copy is otherwise possible, so
    # neither half passes for want of a reachable code path.
    assert await _reuse(db, rerun, starter_user.id) == 1
    row = await _fresh(db, duplicate)
    assert (row.phone, row.email) == (None, None)
    assert row.skip_trace_status == "not_attempted"

    # With the paid switch on, the same copy is allowed (no new credit either way).
    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", True)
    assert await _reuse(db, rerun, starter_user.id) == 1
    row = await _fresh(db, duplicate)
    assert (row.phone, row.email, row.skip_trace_status) == ("2535550100", "a@b.test", "hit")
