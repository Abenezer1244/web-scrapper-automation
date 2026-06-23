### Task 9: Lock the user row in create paths

**Files:**
- Modify: `src/api/routes/scrapers.py` (before `enforce_entitlements` at line 109)
- Modify: `src/api/routes/batches.py` (before `enforce_entitlements` at line 129)

**Interfaces:**
- Consumes: existing `enforce_entitlements`; adds a row lock so concurrent creates serialize.

- [ ] **Step 1: Add the lock in `src/api/routes/scrapers.py`** immediately before the `await enforce_entitlements(` call (line 109):

```python
    # TOCTOU fix: serialize concurrent creates for this user so the distinct-county
    # count in enforce_entitlements can't be raced (entitlements.py:78-81). The lock
    # is released at request-transaction end. Harmless when enforcement is OFF.
    await db.execute(
        select(User.id).where(User.id == current_user.id).with_for_update()
    )
```

(`User` and `select` are already imported in `scrapers.py` — verify with `grep -n "from src.db.models" src/api/routes/scrapers.py`; add `User` to the import if absent.)

- [ ] **Step 2: Add the same lock in `src/api/routes/batches.py`** before line 129's `enforce_entitlements`:

```python
    await db.execute(
        select(User.id).where(User.id == current_user.id).with_for_update()
    )
```

- [ ] **Step 3: Write the concurrency test**

```python
# tests/test_county_cap_race.py
"""Two concurrent creates at the cap must not both succeed when enforcement is ON.
Requires a real DB; skipped if DATABASE_URL is unset."""
import os
import asyncio
import pytest

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DB")


@pytest.mark.asyncio
async def test_concurrent_creates_serialize(monkeypatch):
    from src.config.settings import settings
    monkeypatch.setattr(settings, "ENTITLEMENT_ENFORCEMENT", True)
    # Arrange: a starter user (cap 1) with 0 configs, fire two create requests for
    # two different counties concurrently; exactly one should 402.
    # (Use the project's existing async test client + user factory.)
    ...
```

(Flesh out using the repo's existing async client/user fixtures — see `tests/` for the established pattern. The assertion: exactly one of the two concurrent creates returns 402.)

- [ ] **Step 4: Run + import smoke**

Run: `python -c "import src.api.routes.scrapers, src.api.routes.batches"` then `pytest tests/test_county_cap_race.py -v` (will skip without DB; run against the local dev DB to confirm).

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/scrapers.py src/api/routes/batches.py tests/test_county_cap_race.py
git commit -m "fix(entitlements): lock user row before county-count check, close TOCTOU (Phase 4)"
```

