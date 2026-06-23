### Task 10: `paused_reason` column

**Files:**
- Create: `alembic/versions/070_scraper_config_paused_reason.py`
- Modify: `src/db/models.py:236` (add column)

- [ ] **Step 1: Add the model column** in `ScraperConfig` after `active` (line 237):

```python
    # Why a config is inactive. NULL = active or user-paused; 'entitlement' =
    # auto-paused by downgrade reconciliation (revived on re-upgrade). Distinct
    # from `active` so re-upgrade only revives what the system paused.
    paused_reason = Column(String(32), nullable=True)
```

- [ ] **Step 2: Create the migration** (revises 069):

```python
# alembic/versions/070_scraper_config_paused_reason.py
"""scraper_configs.paused_reason for entitlement reconciliation

Revision ID: 070_paused_reason
Revises: 069_index_unindexed_fks
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = "070_paused_reason"
down_revision = "069_index_unindexed_fks"  # VERIFY against `alembic heads`
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scraper_configs",
        sa.Column("paused_reason", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scraper_configs", "paused_reason")
```

- [ ] **Step 3: Verify the down_revision** matches the current head:

Run: `python -m alembic heads` (or the project's alembic invocation)
Expected: a single head; set `down_revision` to that exact id. Fix if `069_index_unindexed_fks` is not the literal id.

- [ ] **Step 4: Apply + roundtrip on the dev DB**

Run: `python -m alembic upgrade head` then `python -c "import src.db.models"`
Expected: column added; clean import.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/070_scraper_config_paused_reason.py src/db/models.py
git commit -m "feat(db): migration 070 — scraper_configs.paused_reason (Phase 5)"
```

