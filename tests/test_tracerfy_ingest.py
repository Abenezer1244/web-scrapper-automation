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
from src.workers.tracerfy_ingest import (
    _attribution_is_safe,
    ingest_tracerfy_batch,
)

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


def _provider_queues() -> list[dict]:
    """Tracerfy's queue list as the provider would report it: every seeded queue
    complete, at DOWNLOAD_URL, with the row count recorded at submission."""
    with system_sync_session() as db:
        rows = db.execute(
            text("SELECT tracerfy_queue_id, rows_uploaded FROM skip_trace_queues")
        ).all()
    return [
        {"id": q, "pending": False, "download_url": DOWNLOAD_URL,
         "rows_uploaded": n, "credits_deducted": n}
        for q, n in rows
    ]


@pytest.fixture
def _stub_csv(monkeypatch):
    """Serve a canned result CSV instead of fetching one, and Tracerfy's queue
    record (ingest reads its counts and URL from there, audit #5 S3-15). No network."""
    monkeypatch.setattr(
        "src.scrapers.enrichment.skip_trace.fetch_queues", lambda *a, **k: _provider_queues()
    )

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
async def test_two_answers_for_one_waiting_row_are_refused(starter_user, _stub_csv):
    """Codex, round 14 diff review, P1.

    The guard used to return safe as soon as only ONE of our rows waited on an
    address, without ever looking at how many answers came back. Two CSV rows for
    that address then both ran against the same lead and the last owner processed
    won, stamping an arbitrary person's phone and email on it.

    The submission key added in this phase keeps one pending row per address per
    batch, which makes a single waiting row the ORDINARY case, so this path is
    the ordinary path and not a corner. We send a name on a normal trace but the
    parsed CSV drops the echoed names, so there is nothing to disambiguate with:
    refuse, and let the row settle terminally.
    """
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("500 SPLIT ST", "TACOMA", "WA")])
    _stub_csv(_csv(
        "500 SPLIT ST,TACOMA,WA,JANE,DOE,2065550100,Mobile,2065550100,,,"
        "jane@example.com,",
        "500 SPLIT ST,TACOMA,WA,ROBERT,ROE,2065550999,Mobile,2065550999,,,"
        "robert@example.com,",
    ))

    ingest_tracerfy_batch(
        queue_id=qid,
        download_url="https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/x.csv",
        rows_uploaded=1, credits_deducted=1,
    )

    row = _result_row(seed["rows"][0]["result_id"])
    assert (row.phone, row.email) == (None, None), (
        "an arbitrary owner's contacts were stamped on the lead: two answers came "
        "back for one address and the last one processed won"
    )
    assert row.skip_trace_status != "hit"


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
    # The unnamed row terminates instead of hanging on "Processing". Its pending
    # state is 'unmatched' (Tracerfy charged for it; only our reconciliation
    # failed) which is what makes it billable, while the LEAD still honestly
    # shows 'errored' because it carries no contact data.
    assert _pending_status(dropped["pending_id"]) == "unmatched"
    assert _result_row(dropped["result_id"]).skip_trace_status == "errored"


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
async def test_an_attacker_chosen_download_host_is_never_fetched(starter_user, monkeypatch):
    """The webhook body is shared-secret authed, but a leaked secret must not turn
    the worker into an SSRF fetcher for an attacker-chosen host. The body's URL is
    never read (audit #5, S3-15), and even the provider's own record is refused if
    it names a host off Tracerfy's bucket."""
    fetched: list[str] = []
    monkeypatch.setattr(
        "src.scrapers.enrichment.skip_trace.download_tracerfy_csv",
        lambda url: fetched.append(url) or _csv("6 SSRF ST,TACOMA,WA,A,B,,,,,,,"),
    )
    qid = _next_queue_id()
    _seed(starter_user.id, qid, [("6 SSRF ST", "TACOMA", "WA")])
    monkeypatch.setattr(
        "src.scrapers.enrichment.skip_trace.fetch_queues",
        lambda *a, **k: [{"id": qid, "pending": False, "rows_uploaded": 1,
                          "download_url": "https://evil.example.com/x.csv"}],
    )

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url="https://evil.example.com/x.csv",
        rows_uploaded=1, credits_deducted=1,
    )

    assert out["skipped"] == "untrusted_download_host"
    assert fetched == []


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


class _P:
    """Minimal pending-row stand-in for the attribution guard.

    `trace_type` is part of the shape because the guard reads it: a normal answer
    and an advanced answer are different purchases (and, since 098, different
    cache keys), so a mixed group must not be treated as one.
    """

    def __init__(self, first=None, last=None, trace_type="normal"):
        self.first_name = first
        self.last_name = last
        self.trace_type = trace_type


class TestAttributionGuard:
    """Codex round 2: the first version of this guard let two contamination
    shapes through. Both are pinned here."""

    def test_single_waiting_row_with_one_answer_is_safe(self):
        assert _attribution_is_safe([_P("A", "ALPHA")], 1) is True

    def test_single_waiting_row_with_several_answers_is_refused(self):
        """This assertion used to read `..., 3) is True` — "a single waiting row
        is always safe". That was the P1: several answers for one address then
        all ran against that one lead and the last owner processed won. One
        target does not make several answers attributable."""
        assert _attribution_is_safe([_P("A", "ALPHA")], 3) is False

    def test_one_answer_one_owner_many_rows_is_safe(self):
        # The only collision shape production has ever produced.
        assert _attribution_is_safe([_P("JANE", "DOE"), _P("JANE", "DOE")], 1) is True

    def test_one_answer_but_different_owners_is_refused(self):
        # ESCAPED THE FIRST GUARD: a single contact would be stamped on both.
        assert _attribution_is_safe([_P("A", "ALPHA"), _P("B", "BETA")], 1) is False

    def test_several_answers_with_all_null_names_is_refused(self):
        # ESCAPED THE FIRST GUARD: advanced traces send no name, so every row
        # looked like "one owner" while several answers fought to overwrite.
        assert _attribution_is_safe([_P(), _P()], 2) is False

    def test_several_answers_same_owner_is_refused(self):
        assert _attribution_is_safe([_P("JANE", "DOE"), _P("JANE", "DOE")], 2) is False

    def test_case_and_padding_do_not_cause_a_false_refusal(self):
        assert _attribution_is_safe([_P("jane", "doe"), _P("  JANE ", "Doe")], 1) is True

    def test_null_and_named_owner_together_is_refused(self):
        assert _attribution_is_safe([_P(), _P("JANE", "DOE")], 1) is False

    def test_mixed_trace_types_are_refused(self):
        """Round 14. The dispatcher sends one trace_type per batch, so this group
        should be impossible; the guard fails closed if that ever stops holding.
        Same owner, so the name check alone would have waved it through, and the
        two rows key to two different cache entries under 098."""
        assert _attribution_is_safe(
            [_P("JANE", "DOE", "normal"), _P("JANE", "DOE", "advanced")], 1
        ) is False


@pytest.mark.asyncio
async def test_exhausted_retries_mark_the_queue_errored(starter_user, monkeypatch):
    """The failure hook must ACTUALLY fire.

    ingest_tracerfy_batch's docstring always claimed that exhausting retries
    marks the SkipTraceQueue 'errored'; nothing implemented it until now, and
    the reconciler's redrive sweep looks for exactly the state a missing hook
    leaves behind ('pending' + a download_url), so a silently-inert hook means
    the sweep re-enqueues a doomed ingest every five minutes forever.

    Assigning .on_failure onto a Celery task instance is subtle enough that
    reasoning about it is not evidence -- this invokes the real hook the way
    Celery does and checks the database.
    """
    qid = _next_queue_id()
    _seed(starter_user.id, qid, [("9 DOOMED ST", "TACOMA", "WA")])
    with system_sync_session() as db:
        db.execute(
            text("""UPDATE skip_trace_queues SET download_url = :u
                    WHERE tracerfy_queue_id = :q"""),
            {"u": DOWNLOAD_URL, "q": qid},
        )
        db.commit()

    def _status() -> str:
        with system_sync_session() as db:
            return db.execute(
                text("SELECT status FROM skip_trace_queues WHERE tracerfy_queue_id = :q"),
                {"q": qid},
            ).scalar_one()

    assert _status() == "pending"

    # Exactly how Celery invokes it when retries are exhausted.
    ingest_tracerfy_batch.on_failure(
        RuntimeError("download failed"), "task-id", (), {"queue_id": qid}, None
    )

    assert _status() == "errored", "the on_failure hook did not fire"


@pytest.mark.asyncio
async def test_failure_hook_reads_queue_id_from_positional_args_too(
    starter_user, monkeypatch
):
    """webhooks.py and _redrive_completed_queue both call .delay(queue_id=...),
    but a positional call must not silently no-op."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, [("10 DOOMED ST", "TACOMA", "WA")])

    ingest_tracerfy_batch.on_failure(
        RuntimeError("boom"), "task-id", (qid, DOWNLOAD_URL), {}, None
    )

    with system_sync_session() as db:
        status = db.execute(
            text("SELECT status FROM skip_trace_queues WHERE tracerfy_queue_id = :q"),
            {"q": qid},
        ).scalar_one()
    assert status == "errored"


@pytest.mark.asyncio
async def test_unmatched_row_IS_billed(starter_user, _stub_csv):
    """Owner decision 2026-09-07: the customer pays for an unmatched row.

    Tracerfy accepted the row and charged a credit for it; only our address
    reconciliation failed. The lookup was really performed, so it counts against
    the customer's quota exactly like a hit or a miss.
    """
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [
        ("1 MATCHED ST", "TACOMA", "WA"),
        ("2 UNMATCHED ST", "TACOMA", "WA"),
    ])
    before = _usage(starter_user.id)
    _stub_csv(_csv("1 MATCHED ST,TACOMA,WA,J,D,2065550301,Mobile,2065550301,,,,"))

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=2, credits_deducted=2
    )

    assert out["unmatched_rows"] == 1
    # BOTH rows bill: one reconciled, one the provider charged us for anyway.
    assert _usage(starter_user.id) == before + 2
    # The pending row records WHY it billed without being answered...
    assert _pending_status(seed["rows"][1]["pending_id"]) == "unmatched"
    # ...while the lead itself honestly shows no contact data.
    assert _result_row(seed["rows"][1]["result_id"]).skip_trace_status == "errored"


@pytest.mark.asyncio
async def test_presubmit_rejection_is_NEVER_billed(starter_user, _stub_csv):
    """The other half of the decision, and the one that protects the customer.

    A row the dispatcher rejected BEFORE the POST (no city/state) was never seen
    by Tracerfy and never charged. It carries status 'errored' with a NULL
    tracerfy_queue_id. Billing it would charge someone for a lookup that was
    never sent -- which is exactly what would happen if ingest had reused
    'errored' for its unmatched rows instead of a distinct state.
    """
    qid = _next_queue_id()
    seed = _seed(starter_user.id, qid, [("3 BILLED ST", "TACOMA", "WA")])
    # A pre-submit rejection: terminal, and never associated with a queue.
    with system_sync_session() as db:
        db.execute(
            text("""INSERT INTO pending_skip_trace_rows
                    (id, job_id, result_id, user_id, property_address, city, state,
                     trace_type, status, enqueued_at, tracerfy_queue_id)
                    VALUES (:p, :j, :r, :u, '4 NO STATE ST', 'TACOMA', NULL,
                            'normal', 'errored', now(), NULL)"""),
            {"p": str(uuid.uuid4()), "j": seed["job_id"],
             "r": seed["rows"][0]["result_id"], "u": starter_user.id},
        )
        db.commit()
    before = _usage(starter_user.id)
    _stub_csv(_csv("3 BILLED ST,TACOMA,WA,J,D,2065550302,Mobile,2065550302,,,,"))

    ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1
    )

    # Exactly ONE lookup billed -- the reconciled one. The pre-submit rejection
    # is not charged, because Tracerfy never ran it.
    assert _usage(starter_user.id) == before + 1


@pytest.mark.asyncio
async def test_replay_does_not_double_bill_an_unmatched_row(starter_user, _stub_csv):
    """Billing unmatched rows must not weaken replay idempotency."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, [
        ("5 REPLAY MATCHED ST", "TACOMA", "WA"),
        ("6 REPLAY UNMATCHED ST", "TACOMA", "WA"),
    ])
    before = _usage(starter_user.id)
    _stub_csv(_csv("5 REPLAY MATCHED ST,TACOMA,WA,J,D,2065550303,Mobile,,,,,"))

    ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL,
                          rows_uploaded=2, credits_deducted=2)
    after_first = _usage(starter_user.id)
    second = ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL,
                                   rows_uploaded=2, credits_deducted=2)

    assert after_first == before + 2
    assert second.get("skipped", "").startswith("already_")
    assert _usage(starter_user.id) == after_first, "replay re-billed"


@pytest.mark.asyncio
async def test_provider_dropped_row_is_NOT_billed(starter_user, _stub_csv):
    """The guard on the owner's decision.

    Tracerfy DROPS rows it cannot use and DEDUPLICATES identical addresses, and
    such a row never reaches the result CSV -- so it lands on 'unmatched'
    looking exactly like a row that WAS accepted, charged, and merely failed to
    reconcile. Billing it would charge the customer for a lookup the provider
    never performed, which is not what was decided.

    rows_uploaded < rows submitted is the certain signal that something was
    dropped. When it fires, this batch bills reconciled rows only.
    """
    qid = _next_queue_id()
    _seed(starter_user.id, qid, [
        ("1 KEPT ST", "TACOMA", "WA"),
        ("2 DROPPED BY PROVIDER ST", "TACOMA", "WA"),
    ])
    with system_sync_session() as db:   # provider accepted 1 of the 2 we sent
        db.execute(text("""UPDATE skip_trace_queues SET rows_uploaded = 1
                           WHERE tracerfy_queue_id = :q"""), {"q": qid})
        db.commit()
    before = _usage(starter_user.id)
    _stub_csv(_csv("1 KEPT ST,TACOMA,WA,J,D,2065550401,Mobile,2065550401,,,,"))

    ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1
    )

    # ONE lookup billed, not two: the dropped row is not the customer's cost.
    assert _usage(starter_user.id) == before + 1


@pytest.mark.asyncio
async def test_dedup_shrinking_the_upload_also_suppresses_unmatched_billing(
    starter_user, _stub_csv
):
    """Same guard, reached the other way: Tracerfy de-duplicates identical
    addresses (prod: 25 sent -> 24 uploaded), which shrinks rows_uploaded
    without anything being wrong. Erring toward the customer is correct."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, [
        ("9 SAME ST", "TACOMA", "WA"),
        ("9 SAME ST", "TACOMA", "WA"),
        ("10 OTHER ST", "TACOMA", "WA"),
    ])
    with system_sync_session() as db:   # 3 sent, deduped to 2
        db.execute(text("""UPDATE skip_trace_queues SET rows_uploaded = 2
                           WHERE tracerfy_queue_id = :q"""), {"q": qid})
        db.commit()
    before = _usage(starter_user.id)
    _stub_csv(_csv("9 SAME ST,TACOMA,WA,J,D,2065550402,Mobile,2065550402,,,,"))

    ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=2, credits_deducted=2
    )

    # The two rows at 9 SAME ST reconciled from one CSV row and bill; the
    # unmatched 10 OTHER ST does not, because the upload shrank.
    assert _usage(starter_user.id) == before + 2


@pytest.mark.asyncio
async def test_unmatched_settlement_cannot_touch_another_tenants_lead(
    starter_user, business_user, _stub_csv
):
    """A Tracerfy batch is grouped by trace_type, so it spans tenants. The
    terminal 'errored' update on unmatched rows must be pinned to (id, user_id),
    not id alone -- the project rule is that every query filters by user_id, with
    RLS as belt and the query filter as suspenders (Codex, 2026-09-07).
    """
    qid = _next_queue_id()
    mine = _seed(starter_user.id, qid, [("40 MINE ST", "TACOMA", "WA")])
    # Second tenant joins the SAME batch. _seed cannot be reused here because it
    # inserts a skip_trace_queues row and tracerfy_queue_id is UNIQUE -- which is
    # itself the point: one queue, several tenants.
    theirs_rid, theirs_pid = str(uuid.uuid4()), str(uuid.uuid4())
    theirs_sc, theirs_job = str(uuid.uuid4()), str(uuid.uuid4())
    with system_sync_session() as db:
        db.execute(text("""
            INSERT INTO scraper_configs (id, user_id, name, county, state, record_type,
                fields, enrichment, schedule, deliver, skip_trace_enabled, active)
            VALUES (:sc, :u, 'tenant2', 'pierce', 'WA', 'probate', '[]'::json,
                    '[]'::json, '{"frequency":"manual"}'::json,
                    '{"format":"csv","emails":[]}'::json, true, true)"""),
            {"sc": theirs_sc, "u": business_user.id})
        db.execute(text("""
            INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger,
                page_current, page_total, record_count, retry_count)
            VALUES (:j, :u, :sc, 'done', 'manual', 0, 0, 0, 0)"""),
            {"j": theirs_job, "u": business_user.id, "sc": theirs_sc})
        db.execute(text("""
            INSERT INTO results (id, job_id, user_id, is_duplicate, skip_trace_status,
                party_name, property_address, created_at)
            VALUES (:r, :j, :u, false, 'submitted', 'DOE JOHN', '41 THEIRS ST', now())"""),
            {"r": theirs_rid, "j": theirs_job, "u": business_user.id})
        db.execute(text("""
            INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id,
                property_address, city, state, trace_type, status, enqueued_at,
                submitted_at, tracerfy_queue_id)
            VALUES (:p, :j, :r, :u, '41 THEIRS ST', 'TACOMA', 'WA', 'normal',
                    'submitted', now(), now(), :q)"""),
            {"p": theirs_pid, "j": theirs_job, "r": theirs_rid,
             "u": business_user.id, "q": qid})
        db.commit()
    theirs = {"rows": [{"result_id": theirs_rid, "pending_id": theirs_pid}]}
    # Nothing matches: both tenants' rows go unmatched in the same batch.
    _stub_csv(_csv("99 NOBODY ST,TACOMA,WA,J,D,,,,,,,"))

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=2, credits_deducted=2
    )

    assert out["unmatched_rows"] == 2
    # Each tenant's own lead is settled, and settled as its OWN row.
    for seed, uid in ((mine, starter_user.id), (theirs, business_user.id)):
        rid = seed["rows"][0]["result_id"]
        assert _result_row(rid).skip_trace_status == "errored"
        with system_sync_session() as db:
            owner = db.execute(
                text("SELECT user_id FROM results WHERE id = :r"), {"r": rid}
            ).scalar_one()
        assert str(owner) == str(uid), "a lead was settled under the wrong tenant"


# ── queue_accepted_all: ONE rule for billing and the contact-lookup reconciler ─


@pytest.mark.parametrize(("uploaded", "expected"), [
    (3, True),     # every row we sent was accepted
    (4, True),     # more than we sent still covers it
    (2, False),    # dropped or de-duplicated: unmatched is NOT billed
    (0, False),    # the provider hid the count (an adopted queue records 0)
])
async def test_queue_accepted_all_compares_the_upload_with_the_stamped_rows(
    starter_user, uploaded, expected,
):
    from src.api.billing.skip_trace_usage import queue_accepted_all

    qid = _next_queue_id()
    _seed(starter_user.id, qid, [("1 A ST", "SEATTLE", "WA"), ("2 B ST", "SEATTLE", "WA"),
                                 ("3 C ST", "SEATTLE", "WA")])
    with system_sync_session() as db:
        db.execute(text("UPDATE skip_trace_queues SET rows_uploaded = :n "
                        "WHERE tracerfy_queue_id = :q"), {"n": uploaded, "q": qid})
        db.commit()
        assert queue_accepted_all(db, qid) is expected


async def test_queue_accepted_all_is_false_for_a_queue_with_no_rows(starter_user):
    from src.api.billing.skip_trace_usage import queue_accepted_all

    with system_sync_session() as db:
        assert queue_accepted_all(db, 999_999_001) is False
