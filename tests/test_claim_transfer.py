"""A claim whose holder never delivered the lead moves to the run that does.

The cross-run claim is written for every row with a hash, before enrichment
decides whether the row has an address. An address-less row is not a lead, but
it kept the claim, so a later run that found the same property WITH an address
flagged it "already delivered" and hid it. Production held 419 such rows with no
way through: claims never expire.

transfer_undelivered_claims hands the claim over instead of releasing it, so the
old row can never resurface through a later address backfill and the property is
delivered exactly once.

Real DB (conftest `db` fixture), real hashes from legacy_strong_signature.
"""
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, OVER_QUOTA, actionable_condition
from src.db.models import DeliveredRecord, Job, Result, ScraperConfig, User
from src.db.session import system_sync_session
from src.workers.property_identity import legacy_strong_signature
from src.workers.tasks_helpers.dedup import (
    BILLING_STAMP_RELIABLE_SINCE,
    NO_ADDRESS_NOT_BILLED_SINCE,
    release_capped_dedup_claims,
    transfer_undelivered_claims,
)

PARCEL = "0320248026"
AFTER_RULE = NO_ADDRESS_NOT_BILLED_SINCE + timedelta(days=2)
BEFORE_RULE = NO_ADDRESS_NOT_BILLED_SINCE - timedelta(hours=3)
# Created before billing was stamped: a NULL billing_applied_at proves nothing.
LEGACY = BILLING_STAMP_RELIABLE_SINCE - timedelta(days=1)


def _strong(parcel=PARCEL, address=None) -> str:
    h = legacy_strong_signature(parcel, address)
    assert h is not None
    return h


async def _job(db, user: User, config: ScraperConfig, *, status="done",
               billed_at=AFTER_RULE, created_at=None) -> str:
    job_id = str(uuid.uuid4())
    extra = {"created_at": created_at} if created_at is not None else {}
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status=status, trigger="manual", billing_applied_at=billed_at, **extra))
    await db.commit()
    return job_id


async def _row(db, job_id, user_id, dedup_hash, **kw) -> str:
    rid = str(uuid.uuid4())
    kw.setdefault("is_duplicate", False)
    kw.setdefault("parcel_id", PARCEL)
    db.add(Result(id=rid, job_id=job_id, user_id=user_id, dedup_hash=dedup_hash, **kw))
    await db.commit()
    return rid


async def _claim(db, user_id, dedup_hash, first_result_id, first_job_id,
                 parcel=PARCEL, address=None) -> str:
    cid = str(uuid.uuid4())
    db.add(DeliveredRecord(id=cid, user_id=user_id, dedup_hash=dedup_hash,
                           first_result_id=first_result_id, first_job_id=first_job_id,
                           parcel_id=parcel, property_address=address))
    await db.commit()
    return cid


async def _found_again(db, user, config, dedup_hash, source_job, **kw) -> tuple[str, str]:
    """The later run: its row was flagged prior_run, then enrichment found an address."""
    job_id = await _job(db, user, config, status="enriching", billed_at=None)
    kw.setdefault("property_address", "5006 61ST STREET CT E")
    rid = await _row(db, job_id, user.id, dedup_hash, is_duplicate=True,
                     duplicate_reason="prior_run", duplicate_source_job_id=source_job, **kw)
    return job_id, rid


async def _transfer(db, job_id, user_id, record_type=None) -> int:
    n = await db.run_sync(lambda s: transfer_undelivered_claims(s, job_id, user_id, record_type))
    await db.commit()
    return n


async def _fresh(db, model, pk):
    obj = await db.get(model, pk)
    await db.refresh(obj)
    return obj


async def _setup_undelivered(db, user, config, **anchor_job_kw):
    """An earlier run whose only row for the property had no address, holding the claim."""
    h = _strong()
    old_job = await _job(db, user, config, **anchor_job_kw)
    anchor = await _row(db, old_job, user.id, h)
    claim = await _claim(db, user.id, h, anchor, old_job)
    return h, old_job, anchor, claim


# ── the defect ───────────────────────────────────────────────────────────────


async def test_an_undelivered_claim_moves_to_the_run_that_delivers_it(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    h, old_job, anchor, claim = await _setup_undelivered(db, starter_user, scraper_config)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 1

    promoted = await _fresh(db, Result, row)
    assert promoted.is_duplicate is False
    assert promoted.duplicate_reason is None
    assert promoted.duplicate_source_job_id is None

    held = await _fresh(db, DeliveredRecord, claim)
    assert str(held.first_result_id) == row
    assert str(held.first_job_id) == new_job

    old = await _fresh(db, Result, anchor)
    assert old.is_duplicate is True
    assert old.duplicate_reason == "superseded"
    assert str(old.duplicate_source_job_id) == new_job

    # The billing count's own predicate now includes it.
    billable = (await db.execute(
        select(func.count()).select_from(Result).where(
            Result.job_id == new_job, Result.user_id == starter_user.id,
            Result.is_duplicate.is_(False), actionable_condition(),
        )
    )).scalar_one()
    assert billable == 1


async def test_a_later_address_backfill_cannot_resurface_the_old_row(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """Why the claim is MOVED and not released: a mailing backfill on the old row
    would otherwise put the property in two runs' downloads."""
    h, old_job, anchor, _ = await _setup_undelivered(db, starter_user, scraper_config)
    new_job, _row_id = await _found_again(db, starter_user, scraper_config, h, old_job)
    await _transfer(db, new_job, starter_user.id)

    old = await _fresh(db, Result, anchor)
    old.mailing_address = "PO BOX 156, SOUTH PRAIRIE, WA 98385"
    await db.commit()

    # The download's predicate, across every run this user has.
    deliverable = (await db.execute(
        select(func.count()).select_from(Result).where(
            Result.user_id == starter_user.id, Result.dedup_hash == h,
            Result.is_duplicate.is_(False), actionable_condition(),
        )
    )).scalar_one()
    assert deliverable == 1


# ── what must keep suppressing ───────────────────────────────────────────────


async def test_an_anchor_billed_before_the_rule_was_a_real_delivery(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """Before the merge of #191 address-less rows were exported and billed."""
    h, old_job, _, _ = await _setup_undelivered(
        db, starter_user, scraper_config, billed_at=BEFORE_RULE)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 0
    assert (await _fresh(db, Result, row)).is_duplicate is True


async def test_a_done_run_with_no_billing_stamp_is_not_trusted(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    h, old_job, _, _ = await _setup_undelivered(
        db, starter_user, scraper_config, billed_at=None)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 0
    assert (await _fresh(db, Result, row)).is_duplicate is True


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_a_run_that_never_finished_delivered_nothing(
    db, starter_user: User, scraper_config: ScraperConfig, status,
):
    h, old_job, _, claim = await _setup_undelivered(
        db, starter_user, scraper_config, status=status, billed_at=None)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 1
    assert (await _fresh(db, Result, row)).is_duplicate is False
    assert str((await _fresh(db, DeliveredRecord, claim)).first_job_id) == new_job


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_a_run_that_billed_before_it_was_marked_failed_keeps_its_claim(
    db, starter_user: User, scraper_config: ScraperConfig, status,
):
    """A watchdog retry can mark a job failed after it billed. Billed before the
    no-address rule means the address-less row was charged and exported, so the
    status alone must not let the claim move (Codex review round 5)."""
    h, old_job, _, _ = await _setup_undelivered(
        db, starter_user, scraper_config, status=status, billed_at=BEFORE_RULE)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 0
    assert (await _fresh(db, Result, row)).is_duplicate is True


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_an_addressed_row_on_a_run_that_never_billed_still_delivered_nothing(
    db, starter_user: User, scraper_config: ScraperConfig, status,
):
    """Actionable is not delivered. A run that ended without billing never wrote an
    export (export_key comes only from the done-CAS), so an address the row got,
    from its own enrichment or a later mailing backfill, reached nobody. The
    claim must still move, and the old row must be hidden (Codex review round 6)."""
    h = _strong()
    old_job = await _job(db, starter_user, scraper_config, status=status, billed_at=None)
    anchor = await _row(db, old_job, starter_user.id, h,
                        property_address="5006 61ST ST CT E",
                        mailing_address="PO BOX 156, SOUTH PRAIRIE, WA 98385")
    claim = await _claim(db, starter_user.id, h, anchor, old_job)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 1
    assert (await _fresh(db, Result, row)).is_duplicate is False
    assert str((await _fresh(db, DeliveredRecord, claim)).first_job_id) == new_job
    old = await _fresh(db, Result, anchor)
    assert old.is_duplicate is True
    assert old.duplicate_reason == "superseded"


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_a_run_from_before_billing_was_stamped_keeps_its_claim(
    db, starter_user: User, scraper_config: ScraperConfig, status,
):
    """Before migration 063 a job could charge and still end failed or cancelled
    with no billing stamp. For those runs NULL does not mean unbilled, so the
    claim must keep suppressing (Codex review round 7)."""
    h, old_job, _, _ = await _setup_undelivered(
        db, starter_user, scraper_config, status=status, billed_at=None, created_at=LEGACY)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 0
    assert (await _fresh(db, Result, row)).is_duplicate is True


async def test_a_sibling_the_old_run_delivered_keeps_the_claim(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """Before same-run collapse existed, one run could hold two unflagged rows for a
    property. The claim may name the one without an address while the other was
    exported and billed; the property was delivered (Codex review round 7)."""
    h, old_job, _, _ = await _setup_undelivered(db, starter_user, scraper_config)
    await _row(db, old_job, starter_user.id, h, property_address="5006 61ST ST CT E")
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 0
    assert (await _fresh(db, Result, row)).is_duplicate is True


async def test_every_undelivered_row_of_the_old_run_is_hidden_not_only_the_anchor(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """An unflagged sibling with no address was not delivered either, but a later
    mailing backfill would put it in the old run's live download next to the
    promoted row. It is superseded with the anchor."""
    h, old_job, anchor, _ = await _setup_undelivered(db, starter_user, scraper_config)
    sibling = await _row(db, old_job, starter_user.id, h)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 1

    for rid in (anchor, sibling):
        hidden = await _fresh(db, Result, rid)
        assert hidden.is_duplicate is True
        assert hidden.duplicate_reason == "superseded"
    assert (await _fresh(db, Result, row)).is_duplicate is False


@pytest.mark.parametrize("hash_kind", ["strong", "weak"])
async def test_a_claim_released_after_this_run_flagged_the_row_is_taken_by_this_run(
    db, starter_user: User, scraper_config: ScraperConfig, hash_kind,
):
    """The stranded-claim sweep released a failed run's claim after this run's
    dedup step had already flagged the row "already delivered". Nobody holds the
    property now, so this run claims it exactly as its dedup step would have,
    weak hash included; otherwise a one-off run silently loses the lead
    (Codex review round 9)."""
    h = _strong() if hash_kind == "strong" else "weak-name-date-" + uuid.uuid4().hex
    dead = await _job(db, starter_user, scraper_config, status="failed", billed_at=None)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, dead)

    assert await _transfer(db, new_job, starter_user.id) == 1

    assert (await _fresh(db, Result, row)).is_duplicate is False
    held = (await db.execute(
        select(DeliveredRecord).where(
            DeliveredRecord.user_id == starter_user.id, DeliveredRecord.dedup_hash == h)
    )).scalar_one()
    assert str(held.first_result_id) == row
    assert str(held.first_job_id) == new_job


async def test_a_run_still_in_flight_is_never_robbed(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """Its row may still get an address from its own enrichment."""
    h, old_job, _, _ = await _setup_undelivered(
        db, starter_user, scraper_config, status="enriching", billed_at=None)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 0
    assert (await _fresh(db, Result, row)).is_duplicate is True


async def test_an_actionable_anchor_is_a_real_delivery(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    h = _strong()
    old_job = await _job(db, starter_user, scraper_config)
    anchor = await _row(db, old_job, starter_user.id, h, property_address="5006 61ST ST CT E")
    await _claim(db, starter_user.id, h, anchor, old_job)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 0
    assert (await _fresh(db, Result, row)).is_duplicate is True
    assert (await _fresh(db, Result, anchor)).is_duplicate is False


async def test_an_anchor_the_plan_cap_excluded_was_never_delivered(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    h = _strong()
    old_job = await _job(db, starter_user, scraper_config)
    anchor = await _row(db, old_job, starter_user.id, h, property_address="5006 61ST ST CT E",
                        enrichment_data={DELIVERY_EXCLUDED_KEY: OVER_QUOTA})
    await _claim(db, starter_user.id, h, anchor, old_job)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 1
    assert (await _fresh(db, Result, row)).is_duplicate is False


async def test_a_purged_source_run_keeps_suppressing(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """first_result_id is SET NULL when the source run is purged. Delivery cannot
    be disproved, so nothing moves."""
    h = _strong()
    gone_job = str(uuid.uuid4())
    await _claim(db, starter_user.id, h, None, gone_job)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, gone_job)

    assert await _transfer(db, new_job, starter_user.id) == 0
    assert (await _fresh(db, Result, row)).is_duplicate is True


async def test_a_weak_name_date_claim_is_never_transferred(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """A NAME|DATE hash identifies a filing, not a property. Strength is proven
    from the values stored on the CLAIM, not the row enrichment rewrote."""
    import hashlib

    weak = hashlib.sha256(b"NAME:SMITH JOHN|DATE:09/01/2026").hexdigest()
    old_job = await _job(db, starter_user, scraper_config)
    anchor = await _row(db, old_job, starter_user.id, weak, parcel_id=None,
                        party_name="SMITH JOHN", date_recorded="09/01/2026")
    await _claim(db, starter_user.id, weak, anchor, old_job, parcel=None, address=None)
    new_job, row = await _found_again(db, starter_user, scraper_config, weak, old_job,
                                      parcel_id=None)

    assert await _transfer(db, new_job, starter_user.id) == 0
    assert (await _fresh(db, Result, row)).is_duplicate is True


async def test_strength_comes_from_the_claim_even_after_enrichment_rewrote_the_row(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """The later row's address no longer hashes to dedup_hash (enrichment filled
    it). The claim's stored parcel still proves the hash strong."""
    h, old_job, _, _ = await _setup_undelivered(db, starter_user, scraper_config)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job,
                                      property_address="5006 TO 5008 61ST STREET CT E")
    assert legacy_strong_signature(PARCEL, "5006 TO 5008 61ST STREET CT E") != h

    assert await _transfer(db, new_job, starter_user.id) == 1


# ── shape of the handover ────────────────────────────────────────────────────


async def test_same_run_rows_for_the_property_become_combined_under_one_lead(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    h, old_job, _, _ = await _setup_undelivered(db, starter_user, scraper_config)
    new_job, first = await _found_again(db, starter_user, scraper_config, h, old_job,
                                        mailing_address="PO BOX 1, TACOMA, WA",
                                        party_name="OWNER A")
    second = await _row(db, new_job, starter_user.id, h, is_duplicate=True,
                        duplicate_reason="prior_run", duplicate_source_job_id=old_job,
                        property_address="5006 61ST STREET CT E")

    assert await _transfer(db, new_job, starter_user.id) == 1

    # survivor_sort_key: the row with a mailing address and a party name wins.
    shipped, sibling = await _fresh(db, Result, first), await _fresh(db, Result, second)
    assert shipped.is_duplicate is False
    assert sibling.is_duplicate is True
    assert sibling.duplicate_reason == "same_run"
    assert str(sibling.duplicate_source_job_id) == new_job


async def test_running_it_again_changes_nothing(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """A watchdog re-run reaches this pass again."""
    h, old_job, _, _ = await _setup_undelivered(db, starter_user, scraper_config)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)

    assert await _transfer(db, new_job, starter_user.id) == 1
    assert await _transfer(db, new_job, starter_user.id) == 0
    assert (await _fresh(db, Result, row)).is_duplicate is False


async def test_a_promoted_row_the_plan_cap_then_excludes_releases_the_claim(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """Another run of this user took the remaining quota first. The promoted row
    is capped, the cap's own release frees the claim, the old row stays hidden,
    and the next run delivers the property once."""
    h, old_job, anchor, _ = await _setup_undelivered(db, starter_user, scraper_config)
    new_job, row = await _found_again(db, starter_user, scraper_config, h, old_job)
    await _transfer(db, new_job, starter_user.id)

    capped = await _fresh(db, Result, row)
    capped.enrichment_data = {DELIVERY_EXCLUDED_KEY: OVER_QUOTA}
    await db.commit()
    await db.run_sync(lambda s: release_capped_dedup_claims(s, str(starter_user.id), new_job, [row]))
    await db.commit()

    remaining = (await db.execute(
        select(func.count()).select_from(DeliveredRecord).where(
            DeliveredRecord.user_id == starter_user.id, DeliveredRecord.dedup_hash == h)
    )).scalar_one()
    assert remaining == 0
    assert (await _fresh(db, Result, anchor)).duplicate_reason == "superseded"


async def test_another_accounts_claim_on_the_same_property_is_untouched(
    db, starter_user: User, business_user: User, scraper_config: ScraperConfig,
):
    h, a_job, a_anchor, a_claim = await _setup_undelivered(db, starter_user, scraper_config)

    b_config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=business_user.id, name="b", county="pierce",
        state="WA", record_type="probate", fields=[], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"formats": ["csv"], "emails": []},
    )
    db.add(b_config)
    await db.commit()
    b_job = await _job(db, business_user, b_config)
    b_anchor = await _row(db, b_job, business_user.id, h)
    await _claim(db, business_user.id, h, b_anchor, b_job)
    b_new, b_row = await _found_again(db, business_user, b_config, h, b_job)

    assert await _transfer(db, b_new, business_user.id) == 1

    assert str((await _fresh(db, DeliveredRecord, a_claim)).first_result_id) == a_anchor
    assert (await _fresh(db, Result, a_anchor)).is_duplicate is False
    assert (await _fresh(db, Result, b_row)).is_duplicate is False


async def test_two_runs_racing_for_one_claim_deliver_it_once(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """Both later runs found the property with an address. The row and claim locks
    serialise them; the loser finds the claim already moved and stops, so exactly
    one row ships."""
    h, old_job, _, claim = await _setup_undelivered(db, starter_user, scraper_config)
    job_a, row_a = await _found_again(db, starter_user, scraper_config, h, old_job)
    job_b, row_b = await _found_again(db, starter_user, scraper_config, h, old_job)

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}
    errors: list[BaseException] = []

    def _run(job_id):
        try:
            with system_sync_session() as s:
                barrier.wait(timeout=10)
                results[job_id] = transfer_undelivered_claims(s, job_id, starter_user.id)
        except BaseException as exc:  # surfaced below, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_run, args=(j,)) for j in (job_a, job_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert sorted(results.values()) == [0, 1]
    shipped = [r for r in (row_a, row_b) if (await _fresh(db, Result, r)).is_duplicate is False]
    assert len(shipped) == 1
    held = await _fresh(db, DeliveredRecord, claim)
    assert str(held.first_result_id) == shipped[0]



async def test_a_run_cancelled_after_taking_a_claim_hands_it_back(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """Force-finalize can cancel the run between the transfer and billing. That
    run delivers nothing, so it must not keep the claim it took over, or every
    later run hides the lead again. The earlier run's row stays superseded."""
    from src.workers.tasks import _release_claims_of_cancelled_job

    h, old_job, anchor, _ = await _setup_undelivered(db, starter_user, scraper_config)
    other_hash = _strong("7003054290")
    other_job = await _job(db, starter_user, scraper_config)
    other_row = await _row(db, other_job, starter_user.id, other_hash, parcel_id="7003054290",
                           property_address="12967 190TH AVE E")
    other_claim = await _claim(db, starter_user.id, other_hash, other_row, other_job,
                               parcel="7003054290", address="12967 190TH AVE E")
    new_job, _row_id = await _found_again(db, starter_user, scraper_config, h, old_job)
    assert await _transfer(db, new_job, starter_user.id) == 1

    cancelled = await _fresh(db, Job, new_job)
    cancelled.status = "cancelled"
    await db.commit()
    await db.run_sync(lambda s: _release_claims_of_cancelled_job(s, new_job, starter_user.id))
    await db.commit()

    held = (await db.execute(
        select(func.count()).select_from(DeliveredRecord).where(
            DeliveredRecord.user_id == starter_user.id, DeliveredRecord.dedup_hash == h)
    )).scalar_one()
    assert held == 0
    assert (await _fresh(db, Result, anchor)).duplicate_reason == "superseded"
    # Only the cancelled run's claims go.
    assert (await _fresh(db, DeliveredRecord, other_claim)) is not None


@pytest.mark.parametrize("status,billed", [("done", True), ("enriching", False), ("failed", False)])
async def test_only_a_cancelled_unbilled_run_gives_its_claims_up(
    db, starter_user: User, scraper_config: ScraperConfig, status, billed,
):
    """Both callers only know the job is terminal, and 'done' is terminal. A stale
    attempt reaching the guard after a watchdog retry already finished must not
    strip that finished run's claims (Codex)."""
    from src.workers.tasks import _release_claims_of_cancelled_job

    h = _strong()
    job_id = await _job(db, starter_user, scraper_config, status=status,
                        billed_at=AFTER_RULE if billed else None)
    row = await _row(db, job_id, starter_user.id, h, property_address="5006 61ST ST CT E")
    claim = await _claim(db, starter_user.id, h, row, job_id)

    await db.run_sync(lambda s: _release_claims_of_cancelled_job(s, job_id, starter_user.id))
    await db.commit()

    assert (await _fresh(db, DeliveredRecord, claim)) is not None


async def test_a_cancelled_run_from_before_billing_was_stamped_keeps_its_claims(
    db, starter_user: User, scraper_config: ScraperConfig,
):
    """A requeued pre-063 job can reach this exit; its NULL stamp proves nothing."""
    from src.workers.tasks import _release_claims_of_cancelled_job

    h = _strong()
    job_id = await _job(db, starter_user, scraper_config, status="cancelled",
                        billed_at=None, created_at=LEGACY)
    row = await _row(db, job_id, starter_user.id, h, property_address="5006 61ST ST CT E")
    claim = await _claim(db, starter_user.id, h, row, job_id)

    await db.run_sync(lambda s: _release_claims_of_cancelled_job(s, job_id, starter_user.id))
    await db.commit()

    assert (await _fresh(db, DeliveredRecord, claim)) is not None


# ── the sweep: claims of a job that ended while no worker ran it ─────────────


@pytest.mark.parametrize("status,billed,legacy,released", [
    ("cancelled", False, False, True),   # worker died, then the job was cancelled
    ("failed", False, False, True),      # watchdog permanent-fail writes only status
    ("done", True, False, False),        # a real delivery
    ("done", False, False, False),       # done before billing was stamped (pre-063): delivered
    ("cancelled", True, False, False),   # billed: something was charged, keep it
    ("enriching", False, False, False),  # still running
    ("failed", False, True, False),      # pre-063: a NULL stamp does not prove unbilled
    ("cancelled", False, True, False),   # pre-063, same
])
async def test_the_sweep_releases_only_claims_nothing_was_delivered_for(
    db, starter_user: User, scraper_config: ScraperConfig, status, billed, legacy, released,
):
    from src.workers.tasks_helpers.status import sweep_stranded_dedup_claims

    h = _strong()
    job_id = await _job(db, starter_user, scraper_config, status=status,
                        billed_at=AFTER_RULE if billed else None,
                        created_at=LEGACY if legacy else None)
    row = await _row(db, job_id, starter_user.id, h, property_address="5006 61ST ST CT E")
    claim = await _claim(db, starter_user.id, h, row, job_id)

    sweep_stranded_dedup_claims()

    gone = (await db.execute(
        select(func.count()).select_from(DeliveredRecord).where(DeliveredRecord.id == claim)
    )).scalar_one() == 0
    assert gone is released


async def test_the_sweep_never_touches_another_accounts_claim_on_the_same_property(
    db, starter_user: User, business_user: User, scraper_config: ScraperConfig,
):
    from src.workers.tasks_helpers.status import sweep_stranded_dedup_claims

    h = _strong()
    dead = await _job(db, starter_user, scraper_config, status="cancelled", billed_at=None)
    dead_row = await _row(db, dead, starter_user.id, h)
    await _claim(db, starter_user.id, h, dead_row, dead)

    b_config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=business_user.id, name="b", county="pierce",
        state="WA", record_type="probate", fields=[], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"formats": ["csv"], "emails": []},
    )
    db.add(b_config)
    await db.commit()
    live = await _job(db, business_user, b_config)
    live_row = await _row(db, live, business_user.id, h, property_address="5006 61ST ST CT E")
    b_claim = await _claim(db, business_user.id, h, live_row, live)

    sweep_stranded_dedup_claims()

    assert (await _fresh(db, DeliveredRecord, b_claim)) is not None


async def test_the_claim_sweep_is_scheduled_under_its_registered_name():
    from src.workers.scheduler import app

    entry = app.conf.beat_schedule["sweep-stranded-dedup-claims"]["task"]
    assert entry in app.tasks, f"{entry} is scheduled but not registered"
