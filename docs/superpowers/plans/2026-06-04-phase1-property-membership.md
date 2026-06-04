# Phase 1 — Property Membership Foundation: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Maintain a per-`(user, record_type, property)` rollup of strong-identity property sightings so Phase 3's "on both lists" overlap is a fast indexed lookup, with zero user-visible change and the billing path untouched.

**Architecture:** A new `property_list_membership` table is upserted at the end of each scrape job, AFTER address enrichment, for every result that resolves to a strong (parcel/address) identity. Identity is a sha256 `property_key` shared with `_compute_dedup_hash` via one helper module so they cannot drift. The upsert is pre-aggregated in Python (no `ON CONFLICT` double-affect), deadlock-ordered, and retried. Forward writes are durable-with-retry; an offline best-effort backfill seeds history.

**Tech Stack:** FastAPI, Celery, SQLAlchemy (sync engine in workers, async in API/tests), Alembic, PostgreSQL (Supabase, RLS), pytest + pytest-asyncio (real Postgres, no mocks).

**Spec:** `docs/superpowers/specs/2026-06-04-lead-targeting-delivery-design.md` (Section 4).

**Branch:** `feature/lead-targeting-delivery` (already created).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/workers/property_identity.py` (new) | Pure functions: normalize parcel/address, decide strong identity, compute `property_key`. Single source of truth shared with `_compute_dedup_hash`. |
| `src/db/models.py` (modify) | Add `PropertyListMembership` model. |
| `alembic/versions/034_property_list_membership.py` (new) | Create table + index + RLS policy. Schema only. |
| `scripts/apply_rls_force.sql`, `scripts/apply_rls_cutover_policies.sql`, `scripts/provision_rls_roles.sql`, `scripts/_cutover_step2_grants_policies.py`, `scripts/_cutover_step3_rehearse.py` (modify) | Register the new table across the RLS cutover machinery (app SELECT + system write/delete), modeled on `results`, so a future FORCE doesn't default-deny it. |
| `src/workers/tasks.py` (modify) | `_upsert_property_membership()` helper + call it after enrichment; refactor `_compute_dedup_hash` strong branch to use `property_identity`. |
| `src/workers/membership_query.py` (new) | `async users_overlap()` read helper (proves the data; Phase 3 consumes it). |
| `src/workers/scheduler.py` (modify) | Extend `purge_old_records` to prune stale membership rows. |
| `scripts/backfill_property_membership.py` (new) | Offline idempotent best-effort backfill. |
| `tests/test_property_identity.py` (new) | Pure unit tests for the helper. |
| `tests/test_property_membership.py` (new) | DB integration: upsert idempotency, two-type overlap, intersection query, purge. |
| `tests/conftest.py` (modify) | Clean up `property_list_membership` between tests. |

---

## Task 1: Property identity helper (pure, no DB)

**Files:**
- Create: `src/workers/property_identity.py`
- Test: `tests/test_property_identity.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_property_identity.py
"""Pure unit tests for the canonical property-identity helper (Phase 1).

No DB. These pin the strong-identity rules so they stay in lockstep with
_compute_dedup_hash's strong branch in src/workers/tasks.py.
"""
import hashlib

from src.workers.property_identity import (
    compute_property_key,
    is_strong_identity,
    normalize_address,
    normalize_parcel,
)


def test_normalize_parcel_strips_separators_and_uppercases():
    assert normalize_parcel(" 1234-56-7890 ") == "1234567890"
    assert normalize_parcel(None) == ""


def test_normalize_address_collapses_punctuation_and_whitespace():
    assert normalize_address("123 Main St., #4  ") == "123 MAIN ST 4"
    assert normalize_address(None) == ""


def test_strong_identity_true_for_valid_parcel():
    assert is_strong_identity("123456", None) is True


def test_strong_identity_true_for_valid_address():
    assert is_strong_identity(None, "123 MAIN STREET") is True


def test_strong_identity_false_for_short_or_empty():
    assert is_strong_identity(None, None) is False
    assert is_strong_identity("12", "x") is False          # parcel too short, addr too short
    assert is_strong_identity("ABCD", None) is False        # no digit in parcel
    assert is_strong_identity(None, "12345678") is False    # no alpha in address


def test_property_key_is_none_for_weak_identity():
    assert compute_property_key(None, None) is None
    assert compute_property_key("12", None) is None


def test_property_key_matches_dedup_hash_strong_key_format():
    # MUST equal the strong-branch key of _compute_dedup_hash:
    #   key = f"{parcel}|{addr}" then sha256 hexdigest.
    parcel, addr = "1234567890", "123 Main St"
    expected = hashlib.sha256(
        f"{normalize_parcel(parcel)}|{normalize_address(addr)}".encode()
    ).hexdigest()
    assert compute_property_key(parcel, addr) == expected


def test_property_key_stable_across_formatting():
    a = compute_property_key("1234-56-7890", "123 Main St.")
    b = compute_property_key("1234567890", "123 MAIN ST")
    assert a == b and a is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_property_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.workers.property_identity'`

- [ ] **Step 3: Write the implementation**

```python
# src/workers/property_identity.py
"""Canonical property identity for cross-list overlap (Phase 1).

A property's identity for the "on both lists" feature is its normalized
parcel/address, hashed. This MUST stay in lockstep with the *strong* branch
of `_compute_dedup_hash` in src/workers/tasks.py — that function imports the
helpers below so the two cannot drift.

Weak (name+date) identities are intentionally NOT representable here:
different record-type lists key the same property differently by name, so a
name/date match is unsafe for cross-list overlap. `compute_property_key`
returns None for weak rows, and they are excluded from membership.
"""
import hashlib
import re


def normalize_parcel(parcel_id: str | None) -> str:
    return (parcel_id or "").strip().upper().replace("-", "").replace(" ", "")


def normalize_address(property_address: str | None) -> str:
    addr = (property_address or "").strip().upper()
    addr = re.sub(r"[\.,#]", " ", addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    return addr


def is_strong_identity(parcel_id: str | None, property_address: str | None) -> bool:
    """True when parcel OR address is specific enough to identify a property.

    Mirrors the parcel_ok/addr_ok thresholds in _compute_dedup_hash.
    """
    parcel = normalize_parcel(parcel_id)
    addr = normalize_address(property_address)
    parcel_ok = len(parcel) >= 4 and any(c.isdigit() for c in parcel)
    addr_ok = len(addr) >= 8 and any(c.isalpha() for c in addr)
    return parcel_ok or addr_ok


def compute_property_key(parcel_id: str | None, property_address: str | None) -> str | None:
    """sha256 of normalized `parcel|address`, or None for weak identity.

    Equal to the strong-branch key of _compute_dedup_hash for the same inputs.
    """
    if not is_strong_identity(parcel_id, property_address):
        return None
    key = f"{normalize_parcel(parcel_id)}|{normalize_address(property_address)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_property_identity.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/workers/property_identity.py tests/test_property_identity.py
git commit -m "feat(membership): canonical property-identity helper (Phase 1)

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 2: Refactor `_compute_dedup_hash` strong branch to share the helper

This guarantees `property_key` and the billing `dedup_hash` use identical normalization (Codex: they must not drift). Behavior is unchanged — only the strong branch is re-expressed via the helper.

**Files:**
- Modify: `src/workers/tasks.py` (the nested `_compute_dedup_hash`, ~lines 345-380)

- [ ] **Step 1: Add a characterization test for the existing strong key**

```python
# tests/test_property_identity.py  (append)
def test_strong_key_equals_legacy_inline_formula():
    """Lockstep guard: the helper key must equal the legacy inline strong key
    that _compute_dedup_hash produced, so the refactor in Task 2 is behavior-
    preserving for strong rows."""
    import hashlib, re
    parcel_in, addr_in = "1234-56-7890", "123 Main St., #4"
    # legacy inline formula copied verbatim from _compute_dedup_hash strong branch
    parcel = (parcel_in or "").strip().upper().replace("-", "").replace(" ", "")
    addr = (addr_in or "").strip().upper()
    addr = re.sub(r"[\.,#]", " ", addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    legacy = hashlib.sha256(f"{parcel}|{addr}".encode()).hexdigest()
    from src.workers.property_identity import compute_property_key
    assert compute_property_key(parcel_in, addr_in) == legacy
```

- [ ] **Step 2: Run it (passes already — it characterizes current behavior)**

Run: `pytest tests/test_property_identity.py::test_strong_key_equals_legacy_inline_formula -v`
Expected: PASS

- [ ] **Step 3: Refactor `_compute_dedup_hash`**

In `src/workers/tasks.py`, add these to the MODULE-TOP imports (Codex: import only what's used; `sa_text`, `time`, `OperationalError` are currently function-local or absent and Task 5 needs them at module level):

```python
import time
from sqlalchemy import text as sa_text          # hoist: currently only inside run_scrape_job
from sqlalchemy.exc import OperationalError
from src.workers.property_identity import compute_property_key as _compute_property_key
```

> Verify `sa_text` is not re-imported function-locally in a way that shadows; if `run_scrape_job` has `from sqlalchemy import text as sa_text` inside it, remove that local line so the module-level one is used everywhere.

Replace the body of the nested `_compute_dedup_hash` strong branch so it reuses the helpers (keep the `name_date` fallback exactly as-is):

```python
        def _compute_dedup_hash(
            parcel_id: str | None,
            property_address: str | None,
            party_name: str | None = None,
            date_recorded: str | None = None,
        ) -> str | None:
            """Sprint 6.4 dedup key. Strong branch shares normalization with
            src/workers/property_identity (Phase 1) so the billing dedup_hash
            and the overlap property_key cannot drift. Fallback unchanged."""
            strong = _compute_property_key(parcel_id, property_address)
            if strong is not None:
                return strong
            # Fallback: party_name + date_recorded (unchanged)
            name = (party_name or "").strip().upper()
            name = _re.sub(r"\s+", " ", name).strip()
            date = (date_recorded or "").strip()
            if len(name) >= 3 and len(date) >= 6:
                key = f"NAME:{name}|DATE:{date}"
                return hashlib.sha256(key.encode("utf-8")).hexdigest()
            return None
```

- [ ] **Step 4: Run the full worker + identity tests to prove behavior preserved**

Run: `pytest tests/test_property_identity.py tests/test_workers.py -v`
Expected: PASS (no regressions in existing dedup/worker tests)

- [ ] **Step 5: Commit**

```bash
git add src/workers/tasks.py tests/test_property_identity.py
git commit -m "refactor(dedup): share strong-key normalization with property_identity

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 3: `PropertyListMembership` model + test cleanup

**Files:**
- Modify: `src/db/models.py` (add model after `DeliveredRecord`, ~line 241)
- Modify: `tests/conftest.py` (add cleanup)

- [ ] **Step 1: Add the model**

```python
# src/db/models.py  (after DeliveredRecord)
class PropertyListMembership(Base):
    """Phase 1: per-(user, record_type, property) rollup that powers Phase 3's
    cross-list overlap ("on both lists") as an indexed lookup instead of a
    self-join over results history.

    One row per (user_id, record_type, property_key). STRONG-IDENTITY ONLY:
    property_key is the post-enrichment sha256(parcel|address) from
    property_identity.compute_property_key; weak name/date rows are excluded.

    sighting_count is ADVISORY (cumulative scrape observations, not idempotent
    across job re-runs, never used for billing or correctness). Maintained by
    _upsert_property_membership in workers/tasks.py; pruned by purge_old_records.
    """

    __tablename__ = "property_list_membership"
    __table_args__ = (
        Index(
            "ix_property_list_membership_user_key",
            "user_id",
            "property_key",
        ),
    )

    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    record_type = Column(String(64), primary_key=True)
    property_key = Column(String(64), primary_key=True)
    parcel_id = Column(String(64), nullable=True)
    property_address = Column(String(512), nullable=True)
    sighting_count = Column(Integer, nullable=False, default=1)
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
```

- [ ] **Step 2: Add cleanup to conftest**

In `tests/conftest.py`, import the model and delete its rows in the `db` fixture teardown (before `User` delete):

```python
from src.db.models import Job, JobLog, PropertyListMembership, Result, ScraperConfig, User
```
```python
        await session.execute(delete(JobLog))
        await session.execute(delete(Result))
        await session.execute(delete(PropertyListMembership))   # add
        await session.execute(delete(Job))
        await session.execute(delete(ScraperConfig))
        await session.execute(delete(User).where(User.email.like("%@test.bridgeleads.io")))
```

- [ ] **Step 3: Verify the model imports cleanly**

Run: `python -c "from src.db.models import PropertyListMembership; print(PropertyListMembership.__tablename__)"`
Expected: `property_list_membership`

- [ ] **Step 4: Commit**

```bash
git add src/db/models.py tests/conftest.py
git commit -m "feat(membership): PropertyListMembership model + test cleanup

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 4: Migration 034 (schema only) + operator RLS registration

**Files:**
- Create: `alembic/versions/034_property_list_membership.py`
- Modify: `scripts/apply_rls_force.sql` (add table to both arrays)
- Modify: `scripts/apply_rls_cutover_policies.sql` (give the table a `bridgeleads_system` policy — grep it for the existing per-table loop and add `property_list_membership` to that list, mirroring `delivered_records`)

- [ ] **Step 1: Write the migration**

```python
# alembic/versions/034_property_list_membership.py
"""Add property_list_membership (034) — Phase 1 cross-list overlap rollup.

Schema only. NO data backfill here: scripts/migrate.py runs migrations under a
~900s advisory lock on API boot, and results can be ~hundreds of millions of
rows; a backfill in-migration would brick the deploy. Historical seeding lives
in scripts/backfill_property_membership.py (offline, best-effort, idempotent).

RLS: ENABLE + a per-tenant USING policy (migration 018 pattern) — reads isolate
by app.current_user_id. This table is APP-READABLE (Phase 3 reads overlap from
the API), so role GRANTs are modeled on `results`, not worker-only
delivered_records; those grants live in the operator RLS scripts (Task 4 Step 2),
not here. FORCE is applied out-of-band by scripts/apply_rls_force.sql.

Revision ID: 034
Revises: 033
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "property_list_membership",
        sa.Column("user_id", UUID(as_uuid=False), nullable=False),
        sa.Column("record_type", sa.String(length=64), nullable=False),
        sa.Column("property_key", sa.String(length=64), nullable=False),
        sa.Column("parcel_id", sa.String(length=64), nullable=True),
        sa.Column("property_address", sa.String(length=512), nullable=True),
        sa.Column("sighting_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "record_type", "property_key"),
    )
    op.create_index(
        "ix_property_list_membership_user_key",
        "property_list_membership",
        ["user_id", "property_key"],
    )
    # RLS — mirror delivered_records (migration 018): USING-only isolation.
    op.execute("ALTER TABLE property_list_membership ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY property_list_membership_user_isolation
        ON property_list_membership
        USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS property_list_membership_user_isolation "
        "ON property_list_membership"
    )
    op.drop_index("ix_property_list_membership_user_key", table_name="property_list_membership")
    op.drop_table("property_list_membership")
```

- [ ] **Step 2: Register the table across ALL RLS cutover scripts (Codex: migration policy alone is not enough)**

This table is **app-readable** (Phase 3 reads the overlap from the API), so model its grants on **`results`** — NOT on `delivered_records` (which is worker-only). Concretely, for each file below, find how `results` is registered and add `property_list_membership` the same way; where worker-write + app-read + system-delete differ, follow `results` for the app SELECT and `county_records` for the system DELETE:

- `scripts/apply_rls_force.sql` — add `'property_list_membership'` to the `tbls` array (~line 30) and the commented rollback array (~line 83).
- `scripts/apply_rls_cutover_policies.sql` — give the table both a `bridgeleads_system` FOR ALL policy (worker writes) and a `bridgeleads_app` SELECT policy (API reads), mirroring `results`.
- `scripts/provision_rls_roles.sql` — grant `bridgeleads_app` SELECT, `bridgeleads_system` SELECT/INSERT/UPDATE **and DELETE** (DELETE is needed for Task 7 retention; today only `county_records` has system DELETE). Add the table to any worker-only/app-only revoke/verification arrays consistently with `results`.
- `scripts/_cutover_step2_grants_policies.py` and `scripts/_cutover_step3_rehearse.py` — add `property_list_membership` to whatever table lists they assert/iterate, or the rehearsal will flag it as drifted.

> This is the highest-risk part of Phase 1 for the RLS cutover (which is staged, out-of-band, and currently gated by `RLS_ENFORCE=False` — see RLS landmine memory). Have Codex review these script diffs specifically (Task 9 Step 3) and do NOT run the cutover scripts against prod here.

- [ ] **Step 3: Apply the migration locally and verify**

Run:
```bash
python scripts/migrate.py
python -c "from sqlalchemy import create_engine, text; import os; e=create_engine(os.environ['DATABASE_URL_SYNC']); \
print(e.connect().execute(text(\"SELECT column_name FROM information_schema.columns WHERE table_name='property_list_membership' ORDER BY 1\")).fetchall())"
```
Expected: lists `first_seen_at, last_seen_at, parcel_id, property_address, property_key, record_type, sighting_count, user_id`.

Verify RLS policy exists:
```bash
python -c "from sqlalchemy import create_engine, text; import os; e=create_engine(os.environ['DATABASE_URL_SYNC']); \
print(e.connect().execute(text(\"SELECT polname FROM pg_policies WHERE tablename='property_list_membership'\")).fetchall())"
```
Expected: `property_list_membership_user_isolation`.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/034_property_list_membership.py scripts/apply_rls_force.sql scripts/apply_rls_cutover_policies.sql scripts/provision_rls_roles.sql scripts/_cutover_step2_grants_policies.py scripts/_cutover_step3_rehearse.py
git commit -m "feat(db): migration 034 property_list_membership + RLS registration

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

> ⚠️ Do NOT run this migration against production until the branch is merged to `main` (branch-only migration on prod = api crash-loop — see incident memory). Local/test DB only for now.

---

## Task 5: Membership upsert + wire into the scrape job

**Files:**
- Modify: `src/workers/tasks.py` (add `_upsert_property_membership`; call after enrichment, before "mark done", ~line 630)
- Test: `tests/test_property_membership.py`

- [ ] **Step 1: Write the failing integration test (upsert semantics)**

```python
# tests/test_property_membership.py
"""DB integration tests for property_list_membership (Phase 1).

Real Postgres via SyncSessionLocal (mirrors how workers/tasks.py writes).
"""
import uuid

import pytest
from sqlalchemy import text

from src.db.models import User
from src.db.session import SyncSessionLocal
from src.workers.tasks import _upsert_property_membership


class _Row:
    """Minimal stand-in for a Result row (only fields the upsert reads)."""
    def __init__(self, parcel_id, property_address):
        self.parcel_id = parcel_id
        self.property_address = property_address


@pytest.fixture
def membership_user():
    uid = str(uuid.uuid4())
    with SyncSessionLocal() as db:
        db.execute(text(
            "INSERT INTO users (id, email, password_hash, plan, records_used, records_limit) "
            "VALUES (:id, :email, 'x', 'business', 0, 5000)"
        ), {"id": uid, "email": f"test_{uid[:8]}@test.bridgeleads.io"})
        db.commit()
    yield uid
    with SyncSessionLocal() as db:
        db.execute(text("DELETE FROM property_list_membership WHERE user_id = :u"), {"u": uid})
        db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        db.commit()


def _count(db, uid):
    return db.execute(
        text("SELECT count(*) FROM property_list_membership WHERE user_id = :u"), {"u": uid}
    ).scalar()


def test_upsert_inserts_strong_rows_only(membership_user):
    rows = [
        _Row("1234567890", "123 MAIN ST"),       # strong (parcel)
        _Row(None, "456 OAK AVENUE"),            # strong (address)
        _Row(None, None),                        # weak -> excluded
        _Row("12", "x"),                         # weak -> excluded
    ]
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, rows, membership_user, "probate")
        assert _count(db, membership_user) == 2


def test_upsert_repeated_key_in_one_job_no_double_affect(membership_user):
    rows = [_Row("1234567890", "123 MAIN ST"), _Row("1234-56-7890", "123 Main St.")]  # same property
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, rows, membership_user, "probate")
        row = db.execute(text(
            "SELECT sighting_count FROM property_list_membership WHERE user_id = :u"
        ), {"u": membership_user}).fetchone()
        assert _count(db, membership_user) == 1
        assert row.sighting_count == 2


def test_upsert_rerun_keeps_first_seen_advances_last_seen(membership_user):
    rows = [_Row("1234567890", "123 MAIN ST")]
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, rows, membership_user, "probate")
        first = db.execute(text(
            "SELECT first_seen_at, last_seen_at FROM property_list_membership WHERE user_id=:u"
        ), {"u": membership_user}).fetchone()
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, rows, membership_user, "probate")
        second = db.execute(text(
            "SELECT first_seen_at, last_seen_at, sighting_count "
            "FROM property_list_membership WHERE user_id=:u"
        ), {"u": membership_user}).fetchone()
    assert second.first_seen_at == first.first_seen_at      # LEAST keeps original
    assert second.last_seen_at >= first.last_seen_at        # GREATEST advances
    assert second.sighting_count == 2                       # advisory cumulative


def test_same_property_two_record_types_two_rows(membership_user):
    row = [_Row("1234567890", "123 MAIN ST")]
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, row, membership_user, "probate")
        _upsert_property_membership(db, row, membership_user, "pre_foreclosure")
        assert _count(db, membership_user) == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_property_membership.py -v`
Expected: FAIL with `ImportError: cannot import name '_upsert_property_membership'`

- [ ] **Step 3: Implement `_upsert_property_membership`**

Add at module level in `src/workers/tasks.py` (top-level function, not nested). Add `import time` and the OperationalError import at the top if not present:

```python
from sqlalchemy.exc import OperationalError


def _upsert_property_membership(db, rows, user_id: str, record_type: str) -> int:
    """Phase 1: roll up strong-identity property sightings for cross-list overlap.

    `rows` = post-enrichment Result objects (only .parcel_id / .property_address
    are read). Pre-aggregates by property_key in Python so a single multi-row
    INSERT never hits the same conflict key twice ("cannot affect row a second
    time"). Deadlock-ordered by property_key; retried on serialization/deadlock.
    Returns the number of distinct strong properties upserted.

    Advisory only: sighting_count is not idempotent across job re-runs. Failures
    are caller-handled — this never participates in billing.
    """
    agg: dict[str, dict] = {}
    for res in rows:
        key = _compute_property_key(res.parcel_id, res.property_address)
        if not key:
            continue
        cur = agg.get(key)
        if cur is None:
            agg[key] = {
                "parcel_id": (res.parcel_id or None),
                "property_address": (res.property_address or None),
                "count": 1,
            }
        else:
            cur["count"] += 1
            cur["parcel_id"] = cur["parcel_id"] or res.parcel_id
            cur["property_address"] = cur["property_address"] or res.property_address
    if not agg:
        return 0

    items = sorted(agg.items())  # deterministic lock order (deadlock guard)
    for i in range(0, len(items), 500):
        chunk = items[i:i + 500]
        values_sql = ",".join(
            f"(:uid_{k}, :rt_{k}, :pk_{k}, :pid_{k}, :addr_{k}, :cnt_{k}, NOW(), NOW())"
            for k in range(len(chunk))
        )
        params: dict = {}
        for k, (key, v) in enumerate(chunk):
            params[f"uid_{k}"] = user_id
            params[f"rt_{k}"] = record_type
            params[f"pk_{k}"] = key
            params[f"pid_{k}"] = (v["parcel_id"] or None)
            params[f"addr_{k}"] = (v["property_address"] or None)
            params[f"cnt_{k}"] = v["count"]
        stmt = sa_text(f"""
            INSERT INTO property_list_membership
                (user_id, record_type, property_key, parcel_id,
                 property_address, sighting_count, first_seen_at, last_seen_at)
            VALUES {values_sql}
            ON CONFLICT (user_id, record_type, property_key) DO UPDATE SET
                sighting_count   = property_list_membership.sighting_count + EXCLUDED.sighting_count,
                first_seen_at    = LEAST(property_list_membership.first_seen_at, EXCLUDED.first_seen_at),
                last_seen_at     = GREATEST(property_list_membership.last_seen_at, EXCLUDED.last_seen_at),
                parcel_id        = COALESCE(property_list_membership.parcel_id, EXCLUDED.parcel_id),
                property_address = COALESCE(property_list_membership.property_address, EXCLUDED.property_address)
        """)
        for attempt in range(3):
            try:
                db.execute(stmt, params)
                db.commit()
                break
            except OperationalError as exc:
                db.rollback()
                # psycopg2 (this stack) exposes SQLSTATE as .pgcode, NOT .sqlstate.
                pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
                if pgcode not in ("40001", "40P01") or attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
    return len(agg)
```

> Partial-write note: each chunk commits independently, so if a later chunk raises after retries, earlier chunks are already persisted. That is acceptable — `sighting_count` is advisory and the backfill script (Task 8) is idempotent and heals any gap. The caller (Task 5 Step 5) logs the failure and does NOT fail the delivered job.

(`sa_text`, `time`, and `OperationalError` are now module-top imports from Task 2 Step 3.)

- [ ] **Step 4: Run the upsert tests**

Run: `pytest tests/test_property_membership.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Wire the call into `run_scrape_job`**

In `src/workers/tasks.py`, immediately AFTER the enriched re-export `try/finally` block (after ~line 629) and BEFORE the "NOW mark done" block (~line 631), insert:

```python
        # ── PHASE 1: PROPERTY MEMBERSHIP (cross-list overlap rollup) ─────────
        # Strong-identity rollup keyed (user_id, record_type, property_key),
        # computed AFTER enrichment so a probate owner resolved to a parcel
        # overlaps a pre-foreclosure record on the same parcel. Reuses the
        # `refreshed` post-enrichment rows fetched above. Additive + isolated
        # from the billing/dedup path. Durable-with-retry: on hard failure we
        # log and let scripts/backfill_property_membership.py heal the gap
        # rather than fail an already-delivered job (which would re-email).
        try:
            _mcount = _upsert_property_membership(
                db, refreshed, str(job.user_id), config.record_type
            )
            _logger.info("Job %s: property membership upserted %d properties", job_id, _mcount)
        except Exception as exc:
            _logger.error(
                "Job %s: property membership upsert FAILED (heal via backfill): %s",
                job_id, str(exc)[:200],
            )
```

**`refreshed` must be fetched in its own block** (Codex: today it is defined *inside* the re-export `try`, so a re-export failure would leave it missing/empty and membership would silently skip). Restructure so the post-enrichment SELECT happens once, before the re-export, and both re-export and membership reuse it:

```python
        # Fetch post-enrichment rows ONCE; reused by re-export AND membership.
        try:
            refreshed = db.execute(
                select(Result).where(Result.job_id == job_id, Result.user_id == job.user_id)
            ).scalars().all()
        except Exception as exc:
            db.rollback()
            _logger.warning("Job %s: post-enrichment refetch failed: %s", job_id, str(exc)[:120])
            refreshed = []
```
Then the existing re-export block builds `record_dicts` from `refreshed` (remove its own inner SELECT), and the membership block below also uses `refreshed`.

- [ ] **Step 6: Manual smoke (local)**

Trigger a real local scrape for a configured county/record_type, then:
```bash
python -c "from sqlalchemy import create_engine, text; import os; e=create_engine(os.environ['DATABASE_URL_SYNC']); \
print(e.connect().execute(text('SELECT record_type, count(*) FROM property_list_membership GROUP BY 1')).fetchall())"
```
Expected: a row for the scraped `record_type` with count > 0. Confirm the results page, billing, and exports are unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/workers/tasks.py tests/test_property_membership.py
git commit -m "feat(membership): upsert strong-identity rollup after enrichment

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 6: Intersection read helper (proves the data; Phase 3 consumes it)

**Files:**
- Create: `src/workers/membership_query.py`
- Test: `tests/test_property_membership.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_property_membership.py  (append)
import pytest


@pytest.mark.asyncio
async def test_overlap_returns_properties_on_both_lists(membership_user):
    shared = _Row("1234567890", "123 MAIN ST")
    only_probate = _Row("9990001112", "1 LONE LN")
    with SyncSessionLocal() as db:
        _upsert_property_membership(db, [shared, only_probate], membership_user, "probate")
        _upsert_property_membership(db, [shared], membership_user, "pre_foreclosure")

    from src.workers.membership_query import users_overlap
    keys = await users_overlap(membership_user, ["probate", "pre_foreclosure"])
    from src.workers.property_identity import compute_property_key
    assert keys == {compute_property_key("1234567890", "123 MAIN ST")}
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_property_membership.py::test_overlap_returns_properties_on_both_lists -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.workers.membership_query'`

- [ ] **Step 3: Implement the read helper**

```python
# src/workers/membership_query.py
"""Read-side queries over property_list_membership (Phase 1).

Phase 3 (combine/overlap) builds its export on these. Kept tenant-scoped and
indexed: cost is proportional to one user's membership rows, not the table.

RLS: property_list_membership is a tenant table with a USING policy keyed on
app.current_user_id (migration 034). A plain AsyncSessionLocal() does NOT set
that GUC, so under enforced RLS it would return zero rows (Codex). We bind the
session to the user with set_config, the same contract get_rls_db uses in the
API. Phase 3's API endpoint will call this through its already-RLS-bound
request session instead; this standalone helper sets the GUC itself so it is
correct in worker/script contexts too.
"""
from sqlalchemy import text

from src.db.session import AsyncSessionLocal


async def users_overlap(user_id: str, record_types: list[str]) -> set[str]:
    """Return the set of property_keys this user has on ALL of `record_types`
    (the "on both lists" intersection). Strong-identity rows only — the table
    holds nothing else.
    """
    if len(record_types) < 2:
        return set()
    async with AsyncSessionLocal() as db:
        # Bind RLS context to this user (no-op when RLS_ENFORCE is off; required
        # once FORCE is on). Mirrors the app.current_user_id contract in deps.
        await db.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(user_id)},
        )
        rows = await db.execute(
            text(
                """
                SELECT property_key
                FROM property_list_membership
                WHERE user_id = :uid AND record_type = ANY(:types)
                GROUP BY property_key
                HAVING count(DISTINCT record_type) >= :n
                """
            ),
            {"uid": str(user_id), "types": record_types, "n": len(record_types)},
        )
        return {r.property_key for r in rows.fetchall()}
```

> Executor: confirm the exact GUC-binding call against `src/api/deps.py` `get_rls_db` (it may use `SET LOCAL app.current_user_id = ...` rather than `set_config(...)`). Match whatever that helper does so the contract is identical.

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_property_membership.py::test_overlap_returns_properties_on_both_lists -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/workers/membership_query.py tests/test_property_membership.py
git commit -m "feat(membership): tenant-scoped overlap (on-both-lists) query helper

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 7: Retention — prune membership in `purge_old_records`

**Files:**
- Modify: `src/workers/scheduler.py` (`purge_old_records`, ~line 518)

- [ ] **Step 1: Extend the purge**

In `purge_old_records`, after the `county_records` delete and before the log line, add:

```python
        mem = db.execute(
            text("DELETE FROM property_list_membership WHERE last_seen_at < :cutoff"),
            {"cutoff": cutoff},
        )
```
and include `mem.rowcount` in the log message:
```python
        _logger.info(
            "Purged %d county_records and %d membership rows older than %d days",
            result.rowcount, mem.rowcount, settings.RECORD_RETENTION_DAYS,
        )
```

- [ ] **Step 2: Verify it runs without error**

Run: `python -c "from src.workers.scheduler import purge_old_records; purge_old_records()"`
Expected: logs `Purged N county_records and M membership rows ...` with no exception.

- [ ] **Step 3: Commit**

```bash
git add src/workers/scheduler.py
git commit -m "feat(membership): prune stale membership rows in purge_old_records

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 8: Offline best-effort backfill script

**Files:**
- Create: `scripts/backfill_property_membership.py`

- [ ] **Step 1: Implement the script**

```python
# scripts/backfill_property_membership.py
"""Best-effort historical backfill for property_list_membership (Phase 1).

Run MANUALLY after migration 034 is applied — never on API boot. Idempotent
(re-runnable), batched, small commits. Best-effort by design: record_type was
never snapshotted on results/jobs, so it joins results -> jobs -> scraper_configs
and uses the config's CURRENT record_type. Properties whose config changed type
or was deleted are approximate or skipped. Forward accrual (workers/tasks.py) is
the source of truth; this only seeds pre-launch history.

Usage:  python scripts/backfill_property_membership.py [--batch 5000]
"""
import argparse
import logging

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.workers.property_identity import compute_property_key

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("backfill_membership")


def run(batch: int) -> None:
    last_id = ""
    total = 0
    while True:
        with SyncSessionLocal() as db:
            rows = db.execute(
                text(
                    """
                    SELECT r.id, r.user_id, r.parcel_id, r.property_address,
                           sc.record_type
                    FROM results r
                    JOIN jobs j ON j.id = r.job_id
                    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
                    WHERE r.id > :last_id
                    ORDER BY r.id
                    LIMIT :batch
                    """
                ),
                {"last_id": last_id, "batch": batch},
            ).fetchall()
            if not rows:
                break
            last_id = rows[-1].id
            agg: dict[tuple, dict] = {}
            for row in rows:
                key = compute_property_key(row.parcel_id, row.property_address)
                if not key or not row.record_type:
                    continue
                k = (str(row.user_id), row.record_type, key)
                cur = agg.get(k)
                if cur is None:
                    agg[k] = {"parcel_id": row.parcel_id, "property_address": row.property_address, "count": 1}
                else:
                    cur["count"] += 1
            for (uid, rt, key), v in sorted(agg.items()):
                db.execute(
                    text(
                        """
                        INSERT INTO property_list_membership
                            (user_id, record_type, property_key, parcel_id,
                             property_address, sighting_count, first_seen_at, last_seen_at)
                        VALUES (:uid, :rt, :pk, :pid, :addr, :cnt, NOW(), NOW())
                        ON CONFLICT (user_id, record_type, property_key) DO UPDATE SET
                            sighting_count = property_list_membership.sighting_count + EXCLUDED.sighting_count,
                            parcel_id = COALESCE(property_list_membership.parcel_id, EXCLUDED.parcel_id),
                            property_address = COALESCE(property_list_membership.property_address, EXCLUDED.property_address)
                        """
                    ),
                    {"uid": uid, "rt": rt, "pk": key,
                     "pid": v["parcel_id"], "addr": v["property_address"], "cnt": v["count"]},
                )
            db.commit()
            total += len(rows)
            _log.info("backfilled through result id %s (%d rows scanned)", last_id, total)
    _log.info("done — %d result rows scanned", total)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=5000)
    run(ap.parse_args().batch)
```

> Note: re-running this script double-counts `sighting_count` (advisory — acceptable). It will never create duplicate membership rows (PK + ON CONFLICT).

- [ ] **Step 2: Smoke test against local data**

Run: `python scripts/backfill_property_membership.py --batch 1000`
Expected: logs `done — N result rows scanned` with no exception; membership row count is ≥ what forward accrual produced.

- [ ] **Step 3: Commit**

```bash
git add scripts/backfill_property_membership.py
git commit -m "feat(membership): offline best-effort historical backfill script

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Task 9: Full verification + Codex diff review (gate)

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: all green (new tests + no regressions). Fix any failure before proceeding.

- [ ] **Step 2: Type/lint per project rules**

Run: `ruff check src/workers/property_identity.py src/workers/membership_query.py src/workers/tasks.py scripts/backfill_property_membership.py` (or the project's configured linter). Fix all findings.

- [ ] **Step 3: Codex diff review (NO-GO gate)**

Use the `codex` skill in review mode against the base branch:
`codex review --base main`
Resolve every `[P1]`. Per project doctrine: any Critical/High from Claude or Codex = NO-GO until fixed. Re-run until the gate passes.

- [ ] **Step 4: Run the Master Security Review (§14)**

Per `.claude/rules/security.md`: focus on the new write path + RLS policy on the new table + the read helper's tenant scoping. Confirm `user_id` is always bound and the RLS policy mirrors `delivered_records`.

- [ ] **Step 5: Update BUILD_JOURNAL + memory**

Append a `docs/BUILD_JOURNAL.md` entry (built/tried/failed/succeeded + decisions). Add/update a project memory pointing at this plan + the converged design decisions.

- [ ] **Step 6: Final commit**

```bash
git add docs/BUILD_JOURNAL.md
git commit -m "docs(journal): Phase 1 property membership foundation

Co-Authored-By: claude-flow <ruv@ruv.net>"
```

---

## Spec Coverage Check

| Spec requirement (Section 4) | Task |
|---|---|
| `property_list_membership` normalized table, PK (user_id, record_type, property_key) | 3, 4 |
| strong-identity-only (`property_key`, weak excluded) | 1, 5 |
| identity shared with `_compute_dedup_hash` (no drift) | 1, 2 |
| upsert AFTER enrichment, all enriched rows, billing untouched | 5 |
| pre-aggregated upsert (no double-affect) | 5 |
| `first_seen` LEAST / `last_seen` GREATEST / advisory `sighting_count` | 5 |
| deadlock ordering + retry on 40001/40P01 | 5 |
| durable-with-retry forward write; backfill heals | 5, 8 |
| schema-only migration 034 + RLS policy + operator FORCE registration | 4 |
| separate offline best-effort backfill | 8 |
| retention prune in `purge_old_records` | 7 |
| "has both" intersection query (tenant-scoped, indexed) | 6 |
| zero user-visible change; billing/quota/export unchanged | 5 (smoke), 9 |
| Codex diff review gate; merge before prod migration | 9, Task 4 note |

## Deferred to later phases (NOT in this plan)
- Post-enrichment identity refresh / improving enrichment hit-rate for probate (Phase 3).
- Moving billing claim to post-filter (Phase 4).
- Any UI (first UI is Phase 2).
