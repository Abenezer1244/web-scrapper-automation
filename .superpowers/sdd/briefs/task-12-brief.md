### Task 12: Audit-violation report + flip

**Files:**
- Create: `scripts/audit_entitlement_violations.py`

- [ ] **Step 1: Write the measurement script** (read-only; lists who would be blocked under each tier's matrix):

```python
# scripts/audit_entitlement_violations.py
"""Read-only: enumerate users whose ACTIVE configs would be blocked once
ENTITLEMENT_ENFORCEMENT flips. Run against prod DATABASE_URL before flipping.

Usage: python scripts/audit_entitlement_violations.py
"""
from collections import defaultdict
from sqlalchemy import select
from src.db.session import SyncSessionLocal
from src.db.models import ScraperConfig, User
from src.api.entitlements import ConfigRow, plan_reconciliation


def main() -> None:
    with SyncSessionLocal() as db:
        users = db.execute(select(User.id, User.email, User.plan, User.stripe_customer_id)).all()
        by_user = defaultdict(list)
        rows = db.execute(
            select(
                ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
                ScraperConfig.record_type, ScraperConfig.created_at,
                ScraperConfig.active, ScraperConfig.paused_reason,
                ScraperConfig.user_id,
            )
        ).all()
        for r in rows:
            by_user[str(r.user_id)].append(ConfigRow(r.id, r.state, r.county, r.record_type, r.created_at, r.active, r.paused_reason))
        affected = 0
        for uid, email, plan, cust in users:
            pause, _ = plan_reconciliation(by_user.get(str(uid), []), plan or "starter")
            if pause:
                affected += 1
                paid = "PAID" if cust else "free/trial"
                print(f"{email}\tplan={plan}\t{paid}\twould_pause={len(pause)} configs")
        print(f"\nTOTAL affected users: {affected}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run against prod (read-only)**

Run: `railway run python scripts/audit_entitlement_violations.py` (or the project's prod-DSN invocation)
Expected: a list. **Decision gate:** if any `PAID` user appears, add a time-bounded grandfather override before flipping (note it back to the user; do not flip yet). If only free/trial users appear, proceed.

- [ ] **Step 3: Flip the flag (ops, both services)**

Set `ENTITLEMENT_ENFORCEMENT=true` on the **API** and **worker** Railway services. No code change. Document in `docs/BUILD_JOURNAL.md`.

- [ ] **Step 4: Commit the script + journal**

```bash
git add scripts/audit_entitlement_violations.py docs/BUILD_JOURNAL.md
git commit -m "chore(entitlements): audit script + enforcement flip notes (Phase 6)"
```

**PHASE 6 GATE:** audit reviewed with user; flag flipped only after the paid-user decision. User approval before Phase 7.

---

