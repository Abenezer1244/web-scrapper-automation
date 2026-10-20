"""src/utils/owner_pending.py: when a King tax lead's blank owner is "pending"."""
import uuid

import pytest
import pytest_asyncio

from src.db.models import BatchRun, Job, Result, ScraperBatch, ScraperConfig
from src.utils.owner_pending import is_king_tax, owner_name_pending

PIN = "1234567890"


def test_blank_name_on_a_king_pin_is_pending():
    assert owner_name_pending(None, PIN, None)
    assert owner_name_pending("  ", f" {PIN} ", {"owner_recovery_outcome": "transient_failure"})
    assert owner_name_pending("", PIN, "not a dict")


def test_named_lead_is_not_pending():
    assert not owner_name_pending("JANE DOE", PIN, None)


@pytest.mark.parametrize("parcel", [None, "", "123456789", "123456789012", "12345-6789", 1234567890])
def test_non_king_pin_is_not_pending(parcel):
    assert not owner_name_pending(None, parcel, None)


def test_king_has_no_record_is_not_pending():
    assert not owner_name_pending(None, PIN, {"owner_lookup_outcome": "not_on_record"})


@pytest.mark.parametrize("outcome", ["found", "not_on_record", "parcel_mismatch", "gave_up"])
def test_settled_recovery_is_not_pending(outcome):
    assert not owner_name_pending(None, PIN, {"owner_recovery_outcome": outcome})


def test_out_of_attempts_is_not_pending():
    assert not owner_name_pending(None, PIN, {"owner_recovery_attempts": 5})
    assert not owner_name_pending(None, PIN, {"owner_recovery_attempts": "7"})
    assert owner_name_pending(None, PIN, {"owner_recovery_attempts": 4})
    assert owner_name_pending(None, PIN, {"owner_recovery_attempts": "junk"})


def test_attempt_ceiling_matches_the_sweep():
    from src.utils import owner_pending
    from src.workers import owner_recovery

    assert owner_pending._MAX_ATTEMPTS == owner_recovery._MAX_ATTEMPTS


def test_is_king_tax():
    assert is_king_tax(" King ", "tax_delinquent")
    assert is_king_tax("king", "tax_delinquent")
    assert not is_king_tax("Snohomish", "tax_delinquent")
    assert not is_king_tax("King", "probate")
    assert not is_king_tax(None, "tax_delinquent")


# ── Through the API: single-run results and batch leads ─────────────────────
# party_name -> (parcel_id, enrichment_data); only BLANK is still pending.
_ROWS = {
    "": ("0000000001", None),
    "NAMED": ("0000000002", None),
    None: ("0000000003", {"owner_recovery_outcome": "gave_up"}),
}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _job(db, user, county, batch_id=None):
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, batch_id=batch_id, name=f"tax-{county}",
        county=county, state="WA", record_type="tax_delinquent",
        fields=[], enrichment=[], schedule={}, deliver={},
    )
    db.add(cfg)
    await db.flush()
    job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
              status="done", trigger="batch" if batch_id else "manual")
    db.add(job)
    await db.flush()
    for party, (pin, enrichment) in _ROWS.items():
        db.add(Result(
            id=str(uuid.uuid4()), user_id=user.id, job_id=job.id, party_name=party,
            parcel_id=pin, property_key=f"WA|{county}|{pin}", enrichment_data=enrichment,
            property_address=f"{pin} MAIN ST",
        ))
    await db.flush()
    return job


@pytest_asyncio.fixture
async def king_tax_job(db, starter_user):
    job = await _job(db, starter_user, "King")
    await db.commit()
    return job


@pytest_asyncio.fixture
async def pierce_tax_job(db, starter_user):
    job = await _job(db, starter_user, "pierce")
    await db.commit()
    return job


def _pending_by_parcel(rows):
    return {r["parcel_id"]: r["owner_name_pending"] for r in rows}


class TestResultsOwnerPending:
    async def test_king_tax_blank_owner_is_pending(self, client, starter_token, king_tax_job):
        resp = await client.get(f"/jobs/{king_tax_job.id}/results", headers=_auth(starter_token))
        assert resp.status_code == 200
        assert _pending_by_parcel(resp.json()["items"]) == {
            "0000000001": True, "0000000002": False, "0000000003": False,
        }

    async def test_other_county_is_never_pending(self, client, starter_token, pierce_tax_job):
        resp = await client.get(f"/jobs/{pierce_tax_job.id}/results", headers=_auth(starter_token))
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3
        assert not any(r["owner_name_pending"] for r in items)


class TestBatchLeadsOwnerPending:
    async def test_king_tax_batch_lead(self, db, client, starter_token, starter_user):
        batch = ScraperBatch(
            id=str(uuid.uuid4()), user_id=starter_user.id, name="King tax",
            state="WA", fields=[], enrichment=[], schedule={}, deliver={},
            status="active", delivery_mode="everything",
        )
        db.add(batch)
        await db.flush()
        king = await _job(db, starter_user, "King", batch_id=batch.id)
        db.add(BatchRun(id=str(uuid.uuid4()), batch_id=batch.id, user_id=starter_user.id,
                        status="done", child_job_ids=[king.id]))
        await db.commit()
        resp = await client.get(f"/batches/{batch.id}/leads", headers=_auth(starter_token))
        assert resp.status_code == 200
        assert _pending_by_parcel(resp.json()["leads"]) == {
            "0000000001": True, "0000000002": False, "0000000003": False,
        }


class TestListsOwnerPending:
    async def test_union_rows_carry_the_flag(self, db, starter_user):
        from src.api.routes import segments

        await _job(db, starter_user, "King")
        await _job(db, starter_user, "pierce")
        await db.commit()
        rows, _ = await segments._fetch_union(
            db, str(starter_user.id), ["tax_delinquent"], None, 100)
        got = {(r.parcel_id, r.county.lower()): r.owner_name_pending
               for r in segments._union_rows(rows)}
        assert got[("0000000001", "king")] is True
        assert got[("0000000002", "king")] is False
        assert got[("0000000003", "king")] is False
        assert not any(v for (_, c), v in got.items() if c == "pierce")
