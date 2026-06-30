"""Read-only pre-flip audit: which users would have configs paused when
ENTITLEMENT_ENFORCEMENT is enabled?

Run against the prod DB (read-only DSN recommended) BEFORE flipping the flag
to measure the blast radius and decide grandfathering strategy.

Usage:
    DATABASE_URL_SYNC=postgresql://... python scripts/audit_entitlement_violations.py

Output: per-affected-user summary + final totals. NO rows are mutated.
"""
from __future__ import annotations

import sys
from datetime import datetime

from sqlalchemy import select

from src.api.entitlements import ConfigRow, plan_reconciliation
from src.db.models import ScraperConfig, User
from src.db.session import SyncSessionLocal


def _plan_of(user: User) -> str:
    return (user.plan or "starter").lower()


def main() -> None:
    db = SyncSessionLocal()
    try:
        # --- load all users ---------------------------------------------------
        users: list[User] = db.execute(select(User)).scalars().all()

        total_users_affected = 0
        total_configs_would_pause = 0

        rows_by_user: dict[str, list[ConfigRow]] = {}

        # --- load all scraper configs in one query ----------------------------
        all_configs: list[ScraperConfig] = (
            db.execute(select(ScraperConfig)).scalars().all()
        )
        for c in all_configs:
            uid = str(c.user_id)
            rows_by_user.setdefault(uid, []).append(
                ConfigRow(
                    id=str(c.id),
                    state=c.state or "",
                    county=c.county or "",
                    record_type=c.record_type or "",
                    created_at=(
                        c.created_at
                        if c.created_at is not None
                        else datetime.min.replace(tzinfo=None)
                    ),
                    active=bool(c.active),
                    paused_reason=c.paused_reason,
                )
            )

        # --- evaluate each user (pure, no DB writes) -------------------------
        affected_lines: list[str] = []
        for user in users:
            uid = str(user.id)
            plan = _plan_of(user)
            config_rows = rows_by_user.get(uid, [])

            pause_ids, _revive_ids = plan_reconciliation(config_rows, plan)

            if not pause_ids:
                continue

            paid = bool(user.stripe_customer_id)
            payment_status = "PAID" if paid else "free/trial"
            # email is encrypted at rest; repr() safely shows the ciphertext
            # without decrypting — operators can cross-reference by user_id
            try:
                email_display = str(user.email)  # decrypts if key available
            except Exception:  # noqa: BLE001
                email_display = f"<encrypted uid={uid}>"

            affected_lines.append(
                f"  user_id={uid}  email={email_display}  plan={plan}"
                f"  payment={payment_status}  would_pause={len(pause_ids)}"
            )
            total_users_affected += 1
            total_configs_would_pause += len(pause_ids)

        # --- print report (stdout, no mutations) ------------------------------
        if affected_lines:
            print("=== Entitlement pre-flip audit ===")
            print(f"Users who would have >=1 config paused ({total_users_affected}):\n")
            for line in affected_lines:
                print(line)
        else:
            print("=== Entitlement pre-flip audit ===")
            print("No users would have configs paused — safe to flip.")

        print()
        print(
            f"TOTALS: {total_users_affected} user(s) affected, "
            f"{total_configs_would_pause} config(s) would be paused."
        )

    finally:
        # READ-ONLY: never commit — explicit rollback to be unambiguous.
        db.rollback()
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
