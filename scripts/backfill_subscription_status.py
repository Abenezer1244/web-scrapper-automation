"""Backfill users.stripe_subscription_id + subscription_status from Stripe.

Resolves AMBIGUOUS rows (stripe_customer_id set, subscription_status NULL) so the
expire_trials gate has authoritative entitlement state. The gate intentionally
never downgrades an ambiguous row (could be a legacy payer); this script gives it
the positive evidence it needs. For each ambiguous user, query Stripe:
  * entitled sub (active/trialing/past_due) -> store id + status, clear trial_ends_at.
  * otherwise                               -> store latest status, or "canceled"
    if no subscription exists, so the gate can downgrade a genuine non-payer.

Idempotent (only touches rows still NULL). Read-only unless --apply. Uses the
owner role (DATABASE_URL_MIGRATE -> postgres) because UPDATE on users is RLS-scoped
for the app role.

Usage: railway run --service api python scripts/backfill_subscription_status.py [--apply]
"""
import sys

import stripe
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.config import settings

ENTITLED = {"active", "trialing", "past_due"}
apply = "--apply" in sys.argv
stripe.api_key = settings.STRIPE_SECRET_KEY

_owner_url = (settings.DATABASE_URL_MIGRATE or settings.DATABASE_URL_SYNC or "").strip()
if not _owner_url:
    print("No owner DB URL (DATABASE_URL_MIGRATE/_SYNC). Aborting.")
    raise SystemExit(3)
_engine = create_engine(_owner_url.replace(":6543/", ":5432/"), pool_pre_ping=True)


def _resolve_subscription(customer_id: str):
    """Return the most relevant Stripe subscription for a customer, or None."""
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=20)
    data = list(subs.get("data", []))
    if not data:
        return None
    for s in data:  # prefer an entitled one
        if s.get("status") in ENTITLED:
            return s
    return max(data, key=lambda s: s.get("created", 0))  # else most recent


def main() -> int:
    with Session(_engine) as db:
        print("connected as:", db.execute(text("SELECT current_user")).scalar())
        rows = db.execute(text(
            "SELECT id, stripe_customer_id FROM users "
            "WHERE stripe_customer_id IS NOT NULL AND subscription_status IS NULL"
        )).fetchall()
        print(f"ambiguous rows to backfill: {len(rows)}")
        for r in rows:
            sub = _resolve_subscription(r.stripe_customer_id)
            if sub is None:
                sub_id, status, clear_trial = None, "canceled", False
            else:
                sub_id, status = sub["id"], sub["status"]
                clear_trial = status in ENTITLED
            print(f"  user={r.id} customer={r.stripe_customer_id} -> "
                  f"status={status} sub={sub_id} clear_trial={clear_trial}")
            if apply:
                sql = ("UPDATE users SET stripe_subscription_id=:s, subscription_status=:st"
                       + (", trial_ends_at=NULL" if clear_trial else "")
                       + " WHERE id=:id AND subscription_status IS NULL")
                db.execute(text(sql), {"s": sub_id, "st": status, "id": r.id})
        if apply:
            db.commit()
            print("applied.")
        else:
            print("DRY RUN — pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
