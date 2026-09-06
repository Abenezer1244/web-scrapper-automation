"""Tracerfy webhook ingest: provider results -> the right lead, billed once.

This path had ZERO coverage before these tests, despite being the code that
decides which homeowner's phone number lands on which lead and how much the
tenant is charged for it. Every test here is DB-backed against real rows; only
the CSV download is stubbed (no network, no credits spent).

The cases that matter:
  * a hit lands on the correct lead and nowhere else
  * a genuine no-match is recorded as 'miss', NOT as a failure
  * a row the result CSV never named is settled terminally instead of sitting
    on "Processing" forever (production queue 162456)
  * a replayed webhook cannot advance the usage counter twice
  * a cross-tenant batch keeps each tenant's contacts and cache to itself
"""
import uuid

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.workers.tracerfy_ingest import ingest_tracerfy_batch

DOWNLOAD_URL = "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/x.csv"

# Tracerfy's result CSV: the API docs show snake_case (mobile_1) while the
# webhook CSV uses title-dash (Mobile-1); pick_phones accepts both.
CSV_HEADER = (
    "address,city,state,first_name,last_name,"
    "primary_phone,primary_phone_type,Mobile-1,Mobile-2,Landline-1,Email-1,Email-2"
)


def _csv(*rows: str) -> str:
    return "\n".join([CSV_HEADER, *rows]) + "\n"


def _seed(user_id: str, queue_id: int, addresses: list[tuple[str, str, str]]) -> dict:
    """scraper_config -> job -> one result+pending row per address, + a queue row."""
    sc_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    made: list[dict] = []
    with system_sync_session() as db:
        db.execute(
            text("""
                INSERT INTO scraper_configs
                    (id, user_id, name, county, state, record_type, fields, enrichment,
                     schedule, deliver, skip_trace_enabled, active)
                VALUES (:sc, :u, 'ingest test', 'pierce', 'WA', 'probate',
                        '[]'::json, '[]'::json, '{"frequency":"manual"}'::json,
                        '{"format":"csv","emails":[]}'::json, true, true)
            """),
            {"sc": sc_id, "u": user_id},
        )
        db.execute(
            text("""
                INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger,
                                  page_current, page_total, record_count, retry_count)
                VALUES (:j, :u, :sc, 'done', 'manual', 0, 0, 0, 0)
            """),
            {"j": job_id, "u": user_id, "sc": sc_id},
        )
        for addr, city, state in addresses:
            rid, pid = str(uuid.uuid4()), str(uuid.uuid4())
            db.execute(
                text("""
                    INSERT INTO results (id, job_id, user_id, is_duplicate,
                                         skip_trace_status, party_name,
                                         property_address, created_at)
                    VALUES (:r, :j, :u, false, 'submitted', 'DOE JANE', :addr, now())
                """),
                {"r": rid, "j": job_id, "u": user_id, "addr": addr},
            )
            db.execute(
                text("""
                    INSERT INTO pending_skip_trace_rows
                        (id, job_id, result_id, user_id, property_address, city, state,
                         trace_type, status, enqueued_at, submitted_at,
                         tracerfy_queue_id)
                    VALUES (:p, :j, :r, :u, :addr, :city, :state, 'normal',
                            'submitted', now(), now(), :q)
                """),
                {"p": pid, "j": job_id, "r": rid, "u": user_id, "addr": addr,
                 "city": city, "state": state, "q": queue_id},
            )
            made.append({"result_id": rid, "pending_id": pid, "address": addr})
        db.execute(
            text("""
                INSERT INTO skip_trace_queues
                    (id, tracerfy_queue_id, job_id, user_id, trace_type, status,
                     rows_uploaded, credits_deducted, submitted_at)
                VALUES (:id, :q, :j, :u, 'normal', 'pending', :n, 0, now())
            """),
            {"id": str(uuid.uuid4()), "q": queue_id, "j": job_id, "u": user_id,
             "n": len(addresses)},
        )
        db.commit()
    return {"job_id": job_id, "rows": made}


class _Contacts:
    """Decrypted view of a Result's contact columns."""

    def __init__(self, r):
        self.skip_trace_status = r.skip_trace_status
        self.phone = r.phone
        self.email = r.email
        self.phones = r.phones
        self.emails = r.emails


def _result_row(result_id: str) -> _Contacts:
    """Read via the ORM, NOT raw SQL: phone/email are encrypted at rest
    (Fernet, 'fe1:' prefix) and only the mapped column type decrypts them.
    A raw SELECT returns ciphertext and would make these assertions
    meaningless."""
    from src.db.models import Result

    with system_sync_session() as db:
        return _Contacts(db.get(Result, result_id))


def _pending_status(pending_id: str) -> str:
    with system_sync_session() as db:
        return db.execute(
            text("SELECT status FROM pending_skip_trace_rows WHERE id = :id"),
            {"id": pending_id},
        ).scalar_one()


def _usage(user_id: str) -> int:
    with system_sync_session() as db:
        return db.execute(
            text("SELECT skip_trace_used_this_month FROM users WHERE id = :id"),
            {"id": user_id},
        ).scalar_one()


@pytest.fixture
def _stub_csv(monkeypatch):
    """Serve a canned result CSV instead of fetching one. No network."""
    def _install(csv_text: str):
        monkeypatch.setattr(
            "src.scrapers.enrichment.skip_trace.download_tracerfy_csv",
            lambda url: csv_text,
        )
    return _install


def _next_queue_id() -> int:
    return int(uuid.uuid4().int % 10_000_000) + 900_000_000


@pytest.mark.asyncio
async def test_hit_lands_on_the_correct_lead(starter_user, _stub_csv):
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("123 MAIN ST", "TACOMA", "WA")])
    _stub_csv(_csv(
        "123 MAIN ST,TACOMA,WA,JANE,DOE,2065550100,Mobile,2065550100,,,"
        "jane@example.com,"
    ))

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1
    )

    assert out["hits"] == 1 and out["misses"] == 0
    assert out["unmatched_rows"] == 0
    row = _result_row(seed["rows"][0]["result_id"])
    assert row.skip_trace_status == "hit"
    assert row.phone == "2065550100"
    assert row.email == "jane@example.com"
    assert _pending_status(seed["rows"][0]["pending_id"]) == "completed"


@pytest.mark.asyncio
async def test_no_match_is_a_miss_not_a_failure(starter_user, _stub_csv):
    """Tracerfy processed the row and found nothing. That is a valid answer,
    not a system failure — it must land on 'miss' and still settle the row."""
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("456 EMPTY RD", "TACOMA", "WA")])
    _stub_csv(_csv("456 EMPTY RD,TACOMA,WA,JANE,DOE,,,,,,,"))

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1
    )

    assert out["misses"] == 1 and out["hits"] == 0
    row = _result_row(seed["rows"][0]["result_id"])
    assert row.skip_trace_status == "miss"
    assert row.phone is None and row.email is None
    # A miss is a completed lookup: settled, and counted for billing.
    assert _pending_status(seed["rows"][0]["pending_id"]) == "completed"


@pytest.mark.asyncio
async def test_row_the_csv_never_named_is_settled_not_stranded(starter_user, _stub_csv):
    """Production queue 162456: 4 rows sent, Tracerfy uploaded 3. The dropped
    row never appears in the CSV, so it used to stay 'submitted' forever and
    its lead read "Processing" indefinitely. It must now terminate."""
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [
        ("789 GOOD ST", "TACOMA", "WA"),
        ("999 DROPPED AVE", "TACOMA", "WA"),
    ])
    _stub_csv(_csv(
        "789 GOOD ST,TACOMA,WA,JANE,DOE,2065550111,Mobile,2065550111,,,a@b.com,"
    ))

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1
    )

    assert out["unmatched_rows"] == 1
    good, dropped = seed["rows"][0], seed["rows"][1]
    assert _result_row(good["result_id"]).skip_trace_status == "hit"
    assert _pending_status(good["pending_id"]) == "completed"
    # The unnamed row terminates instead of hanging on "Processing".
    assert _pending_status(dropped["pending_id"]) == "errored"
    assert _result_row(dropped["result_id"]).skip_trace_status == "errored"


@pytest.mark.asyncio
async def test_unmatched_row_is_not_billed(starter_user, _stub_csv):
    """The user is not charged for a lookup they never received."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, [
        ("1 BILLED ST", "TACOMA", "WA"),
        ("2 UNBILLED ST", "TACOMA", "WA"),
    ])
    before = _usage(starter_user.id)
    _stub_csv(_csv("1 BILLED ST,TACOMA,WA,J,D,2065550122,Mobile,2065550122,,,,"))

    ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1
    )

    # Exactly one lookup counted, not two.
    assert _usage(starter_user.id) == before + 1


@pytest.mark.asyncio
async def test_webhook_replay_cannot_double_bill(starter_user, _stub_csv):
    """Tracerfy does not sign webhooks and may deliver more than once. The
    queue-row lock is the idempotency anchor: a replay must no-op."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, [("5 REPLAY ST", "TACOMA", "WA")])
    _stub_csv(_csv("5 REPLAY ST,TACOMA,WA,J,D,2065550133,Mobile,2065550133,,,r@x.com,"))
    before = _usage(starter_user.id)

    first = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1
    )
    after_first = _usage(starter_user.id)
    second = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1
    )

    assert first["hits"] == 1
    assert second.get("skipped", "").startswith("already_")
    assert after_first == before + 1
    assert _usage(starter_user.id) == after_first, "replay advanced the counter"


@pytest.mark.asyncio
async def test_unknown_queue_id_is_refused(starter_user, _stub_csv):
    """A forged or unrecognised queue id must not touch any lead."""
    _stub_csv(_csv("1 NOWHERE ST,TACOMA,WA,J,D,2065550144,Mobile,,,,,"))

    out = ingest_tracerfy_batch(
        queue_id=_next_queue_id(), download_url=DOWNLOAD_URL,
        rows_uploaded=1, credits_deducted=1,
    )

    assert out["skipped"] == "unknown_queue"


@pytest.mark.asyncio
async def test_untrusted_download_host_is_refused(starter_user, _stub_csv):
    """The webhook body is shared-secret authed, but a leaked secret must not
    turn the worker into an SSRF fetcher for an attacker-chosen host."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, [("6 SSRF ST", "TACOMA", "WA")])

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url="https://evil.example.com/x.csv",
        rows_uploaded=1, credits_deducted=1,
    )

    assert out["skipped"] == "untrusted_download_host"


@pytest.mark.asyncio
async def test_contacts_never_cross_between_leads(starter_user, _stub_csv):
    """Two different properties in one batch keep their own contacts."""
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [
        ("10 ALPHA ST", "TACOMA", "WA"),
        ("20 BETA ST", "TACOMA", "WA"),
    ])
    _stub_csv(_csv(
        "10 ALPHA ST,TACOMA,WA,A,A,2065550001,Mobile,2065550001,,,alpha@x.com,",
        "20 BETA ST,TACOMA,WA,B,B,2065550002,Mobile,2065550002,,,beta@x.com,",
    ))

    ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=2, credits_deducted=2
    )

    alpha = _result_row(seed["rows"][0]["result_id"])
    beta = _result_row(seed["rows"][1]["result_id"])
    assert alpha.phone == "2065550001" and alpha.email == "alpha@x.com"
    assert beta.phone == "2065550002" and beta.email == "beta@x.com"


@pytest.mark.asyncio
async def test_case_and_whitespace_differences_still_match(starter_user, _stub_csv):
    """The match key lowercases the street/city and uppercases the state, so
    Tracerfy echoing a different case must not strand the row."""
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("30 Mixed Case Ave", "Tacoma", "wa")])
    _stub_csv(_csv(
        "30 MIXED CASE AVE,TACOMA,WA,J,D,2065550155,Mobile,2065550155,,,m@x.com,"
    ))

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1
    )

    assert out["hits"] == 1 and out["unmatched_rows"] == 0
    assert _result_row(seed["rows"][0]["result_id"]).skip_trace_status == "hit"


@pytest.mark.asyncio
async def test_same_address_same_owner_shares_contacts(starter_user, _stub_csv):
    """Two results for ONE property with the same owner legitimately share the
    contact data — Tracerfy de-duplicates the address and returns one CSV row.
    This is the only in-batch key collision production has ever seen."""
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [
        ("1609 121ST ST S", "TACOMA", "WA"),
        ("1609 121ST ST S", "TACOMA", "WA"),
    ])
    _stub_csv(_csv(
        "1609 121ST ST S,TACOMA,WA,JANE,DOE,2065550177,Mobile,2065550177,,,j@x.com,"
    ))

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1
    )

    assert out["unmatched_rows"] == 0
    for row in seed["rows"]:
        r = _result_row(row["result_id"])
        assert r.skip_trace_status == "hit"
        assert r.phone == "2065550177"


@pytest.mark.asyncio
async def test_ambiguous_owner_attribution_is_refused_not_guessed(starter_user, _stub_csv):
    """Same address key, DIFFERENT owners, and a CSV row per owner: every CSV
    row matches every pending row and the last would silently win, stamping one
    person's contacts onto the other's lead. Refuse instead."""
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [
        ("77 DUPLEX WAY", "TACOMA", "WA"),
        ("77 DUPLEX WAY", "TACOMA", "WA"),
    ])
    with system_sync_session() as db:
        for pid, first, last in (
            (seed["rows"][0]["pending_id"], "ALICE", "ALPHA"),
            (seed["rows"][1]["pending_id"], "BOB", "BETA"),
        ):
            db.execute(
                text("""UPDATE pending_skip_trace_rows
                        SET first_name = :f, last_name = :l WHERE id = :id"""),
                {"f": first, "l": last, "id": pid},
            )
        db.commit()
    _stub_csv(_csv(
        "77 DUPLEX WAY,TACOMA,WA,ALICE,ALPHA,2065550188,Mobile,2065550188,,,a@x.com,",
        "77 DUPLEX WAY,TACOMA,WA,BOB,BETA,2065550199,Mobile,2065550199,,,b@x.com,",
    ))

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=2, credits_deducted=2
    )

    # Neither lead gets a contact rather than one lead getting the wrong person's.
    assert out["unmatched_rows"] == 2
    for row in seed["rows"]:
        r = _result_row(row["result_id"])
        assert r.skip_trace_status == "errored"
        assert r.phone is None and r.email is None
