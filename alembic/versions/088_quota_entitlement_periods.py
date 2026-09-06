"""Entitlement periods — quota stops resetting on the 1st of the calendar month.

Record quota reset on the calendar month while Stripe renewed on the
subscription anniversary. Two unrelated clocks, and every gap between them was a
defect: a first-cycle subscriber crossing a reset received up to 2x their plan
quota on ONE payment, an annual subscriber's "1,000 records/month" would have
meant 1,000 records/year under any naive anniversary fix, and a trial user who
consumed their trial allowance and then PAID received nothing until the 1st.

This migration installs the mechanism; it deliberately changes NOBODY's
behaviour on the way in.

WHAT IT ADDS

``users`` gains a monthly entitlement window and the lifecycle state the window
needs, and ``jobs`` gains the window a quota reservation was charged against.

The window is ``[quota_period_start, quota_period_end)``, always one month long,
on a grid anchored at ``quota_anchor_at``. The anchor is IMMUTABLE except on
three events (first trial->paid conversion, resubscribe after a genuine lapse,
explicit admin action); plan changes, cancellation, dunning and monthly<->annual
switches never move it. That single invariant is what makes upgrade-farming and
cancel/resubscribe-farming worthless.

WHY THE BACKFILL IS A NO-OP

``quota_anchor_at = quota_period_start = records_period_start`` and
``quota_period_end = records_period_start + 1 month``, with ``records_used``
UNTOUCHED. Since every existing ``records_period_start`` is the first of a month,
every user lands on a day-1 grid — which is precisely the calendar behaviour they
already had. The new columns describe the status quo exactly, so the deploy
cannot move anyone's reset date, cannot grant a bucket and cannot take one away.
Existing over-cap accounts stay over cap.

Re-running is idempotent: the values are recomputed from ``records_period_start``
rather than accumulated.

Subscribers are moved onto their real Stripe anniversary LATER and separately, by
``scripts/backfill_quota_anchors.py``, which sets the anchor only and lets each
user's grid shift at their next natural rollover. That ordering is what stops a
user getting both a legacy reset on the 1st and an anchor reset days later.

THE SQL FUNCTIONS

The period rule used to be written out seven times across the API, the worker's
reservation and settlement statements, the release path and the beat, each with
its own ``date_trunc('month', ...)``. Any two drifting is a silent quota bug, and
two of them (``_reservation_is_current`` and ``release_quota_reservation``) were
already comparing calendar MONTHS in a way that becomes wrong the instant an
anchor is not day 1 — a job reserving on the 19th and settling on the 21st with a
20th anchor would have read as "same period" and netted its grant off a counter
that had already been zeroed, delivering records charged to nobody.

So the arithmetic lives in one place per language: these ``public.quota_*``
functions for SQL, ``src/api/quota_window.py`` for Python, with a test that
proves the two agree over a generated date matrix.

Every function re-casts through ``AT TIME ZONE 'UTC'`` because Postgres month
arithmetic on a ``timestamptz`` is evaluated in the SESSION timezone — a worker
connecting under a negative offset would otherwise compute a different day, the
same class of bug that made a healthy user read as stale during the #223 work.

Revision ID: 088
Revises: 087
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op

revision = "088"
down_revision = "087"
branch_labels = None
depends_on = None


# ── Window arithmetic, in SQL ────────────────────────────────────────────────
# STRICT where every argument is required (NULL in => NULL out is correct and
# lets Postgres skip the call); NOT strict for the predicate, whose nullable
# lifecycle columns are meaningful.
_FUNCTIONS = """
CREATE OR REPLACE FUNCTION public.quota_add_months(anchor timestamptz, k integer)
RETURNS timestamptz
LANGUAGE sql IMMUTABLE STRICT
AS $fn$
    SELECT ((anchor AT TIME ZONE 'UTC') + make_interval(months => k)) AT TIME ZONE 'UTC'
$fn$;

COMMENT ON FUNCTION public.quota_add_months(timestamptz, integer) IS
'Anchor plus k whole calendar months, clamping the day (Jan 31 + 1 month = Feb 28).
Always call with the ORIGINAL anchor: adding one month repeatedly to a clamped
result compounds the clamp and walks Jan 31 -> Feb 28 -> Mar 28.';

CREATE OR REPLACE FUNCTION public.quota_grid_index(anchor timestamptz, at timestamptz)
RETURNS integer
LANGUAGE sql IMMUTABLE STRICT
AS $fn$
    SELECT COALESCE((
        SELECT max(k)::int
          FROM generate_series(0, GREATEST(0,
                   (EXTRACT(YEAR  FROM age(at AT TIME ZONE 'UTC', anchor AT TIME ZONE 'UTC'))::int * 12
                  + EXTRACT(MONTH FROM age(at AT TIME ZONE 'UTC', anchor AT TIME ZONE 'UTC'))::int) + 2
               )) AS k
         WHERE public.quota_add_months(anchor, k) <= at
    ), 0)
$fn$;

COMMENT ON FUNCTION public.quota_grid_index(timestamptz, timestamptz) IS
'Largest k >= 0 with anchor + k months <= at. The month difference is only an
estimate because clamping makes a cell shorter than a calendar month, so the
search runs over that estimate +2 and takes the max rather than trusting it.';

CREATE OR REPLACE FUNCTION public.quota_transitional_end(anchor timestamptz, old_end timestamptz)
RETURNS timestamptz
LANGUAGE sql IMMUTABLE STRICT
AS $fn$
    SELECT CASE WHEN e1 > old_end THEN e1 ELSE b_hi END
      FROM (
        SELECT CASE WHEN (b_hi - target) <= (target - b_lo) THEN b_hi ELSE b_lo END AS e1,
               b_hi
          FROM (
            SELECT target,
                   public.quota_add_months(anchor, public.quota_grid_index(anchor, target))     AS b_lo,
                   public.quota_add_months(anchor, public.quota_grid_index(anchor, target) + 1) AS b_hi
              FROM (SELECT public.quota_add_months(old_end, 1) AS target) AS t
          ) AS x
      ) AS y
$fn$;

COMMENT ON FUNCTION public.quota_transitional_end(timestamptz, timestamptz) IS
'End of the window beginning at old_end. In steady state old_end is a grid
boundary so this is exactly old_end + 1 month. After an anchor MOVES it snaps
back to the grid boundary CLOSEST to old_end + 1 month, which keeps the one-off
transitional window within about a fortnight of a month and makes it impossible
to mint two buckets days apart. Ties go to the later boundary.';

CREATE OR REPLACE FUNCTION public.quota_next_start(anchor timestamptz, old_end timestamptz, at timestamptz)
RETURNS timestamptz
LANGUAGE sql IMMUTABLE STRICT
AS $fn$
    SELECT CASE
             WHEN at < public.quota_transitional_end(anchor, old_end) THEN old_end
             ELSE public.quota_add_months(anchor, public.quota_grid_index(anchor, at))
           END
$fn$;

CREATE OR REPLACE FUNCTION public.quota_next_end(anchor timestamptz, old_end timestamptz, at timestamptz)
RETURNS timestamptz
LANGUAGE sql IMMUTABLE STRICT
AS $fn$
    SELECT CASE
             WHEN at < public.quota_transitional_end(anchor, old_end)
               THEN public.quota_transitional_end(anchor, old_end)
             ELSE public.quota_add_months(anchor, public.quota_grid_index(anchor, at) + 1)
           END
$fn$;

COMMENT ON FUNCTION public.quota_next_start(timestamptz, timestamptz, timestamptz) IS
'Windows are contiguous: a new one starts where the old one ended. A user who is
away for three months lands on the cell containing now and is zeroed ONCE, so
unused entitlement never accumulates.';

CREATE OR REPLACE FUNCTION public.quota_should_roll(
    quota_period_end timestamptz,
    subscription_status text,
    entitlement_grace_ends_at timestamptz,
    entitlement_ends_at timestamptz,
    at timestamptz)
RETURNS boolean
LANGUAGE sql IMMUTABLE
AS $fn$
    SELECT quota_period_end <= at
       AND COALESCE(subscription_status, '') NOT IN
             ('unpaid', 'incomplete', 'incomplete_expired', 'paused')
       AND NOT (COALESCE(subscription_status, '') = 'past_due'
                AND entitlement_grace_ends_at IS NOT NULL
                AND at >= entitlement_grace_ends_at)
       AND (entitlement_ends_at IS NULL OR quota_period_end < entitlement_ends_at)
$fn$;

COMMENT ON FUNCTION public.quota_should_roll(timestamptz, text, timestamptz, timestamptz, timestamptz) IS
'Has the window ended AND is the account entitled to a new one? A frozen
(unpaid/incomplete/paused, or past_due beyond its grace) subscription does not
advance, so failing to pay cannot quietly mint a fresh bucket every month. A NULL
status is never frozen: Starter, free and admin-granted accounts have no
subscription to fail. COALESCE is load-bearing — a NULL compared with IN yields
NULL, which would make the whole predicate NULL rather than true.';
"""

_DROP_FUNCTIONS = """
DROP FUNCTION IF EXISTS public.quota_should_roll(timestamptz, text, timestamptz, timestamptz, timestamptz);
DROP FUNCTION IF EXISTS public.quota_next_end(timestamptz, timestamptz, timestamptz);
DROP FUNCTION IF EXISTS public.quota_next_start(timestamptz, timestamptz, timestamptz);
DROP FUNCTION IF EXISTS public.quota_transitional_end(timestamptz, timestamptz);
DROP FUNCTION IF EXISTS public.quota_grid_index(timestamptz, timestamptz);
DROP FUNCTION IF EXISTS public.quota_add_months(timestamptz, integer);
"""


# ── Backfill statements ──────────────────────────────────────────────────────
# Module-level so tests can execute the EXACT SQL the migration runs rather than
# a transcription of it. All three are deterministic and idempotent: every value
# is recomputed from a column that already exists, never accumulated, so a
# re-run is a no-op and a partial run can simply be repeated.

#: Deliberately a NO-OP in behaviour. Every existing records_period_start is the
#: first of a month, so every user lands on a day-1 grid — exactly the calendar
#: rule they already had. records_used is not referenced at all, so an account
#: sitting over its cap stays over its cap and nobody gains or loses a bucket on
#: deploy. Subscribers move to their real Stripe anniversary later and
#: separately, via scripts/backfill_quota_anchors.py.
BACKFILL_WINDOWS = """
    UPDATE users
    SET quota_anchor_at    = records_period_start,
        quota_period_start = records_period_start,
        quota_period_end   = ((records_period_start AT TIME ZONE 'UTC')
                              + interval '1 month') AT TIME ZONE 'UTC'
"""

#: A user already paying has by definition converted. Stamping first_paid_at
#: stops the trial->paid handler mistaking their next subscription webhook for a
#: first conversion and zeroing a counter they legitimately owe.
BACKFILL_FIRST_PAID = """
    UPDATE users
    SET first_paid_at = created_at
    WHERE first_paid_at IS NULL
      AND subscription_status IN ('active', 'past_due', 'trialing')
"""

#: Anyone who was ever granted a trial has consumed it. trial_ends_at is CLEARED
#: on conversion, so it cannot answer "did they ever trial" on its own — which is
#: exactly why trial_consumed_at exists. Runs AFTER the first_paid backfill
#: because it reads the column that one writes.
BACKFILL_TRIAL_CONSUMED = """
    UPDATE users
    SET trial_consumed_at = COALESCE(trial_ends_at, created_at)
    WHERE trial_consumed_at IS NULL
      AND (trial_ends_at IS NOT NULL OR first_paid_at IS NOT NULL)
"""


def upgrade() -> None:
    # ── 1. The entitlement window ────────────────────────────────────────────
    # NOT NULL with a server_default equal to today's calendar behaviour, so a
    # row inserted by any path that predates the application change still lands
    # in a valid window rather than a NULL that some later statement has to
    # guess about. Migration 086 learned that lesson the expensive way: a
    # nullable period column with no default let the rollover's IS NULL arm zero
    # a brand-new user's counter inside their own signup month.
    op.add_column(
        "users",
        sa.Column(
            "quota_anchor_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(
                "(date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
            ),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "quota_period_start",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(
                "(date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
            ),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "quota_period_end",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(
                "((date_trunc('month', NOW() AT TIME ZONE 'UTC') + interval '1 month') "
                "AT TIME ZONE 'UTC')"
            ),
        ),
    )

    # ── 2. Subscription lifecycle state the window depends on ────────────────
    # All nullable: every one of them is genuinely absent for a Starter, free or
    # admin-granted account, and inventing a value would make "never subscribed"
    # indistinguishable from "subscribed and then something".
    op.add_column(
        "users",
        sa.Column("entitlement_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "entitlement_grace_ends_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "users",
        sa.Column("trial_consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("first_paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "paid_entitlement_ended_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column("users", sa.Column("pending_plan", sa.String(32), nullable=True))
    op.add_column(
        "users", sa.Column("pending_records_limit", sa.Integer(), nullable=True)
    )

    # ── 3. Which window a job's quota reservation was charged to ─────────────
    # Nullable on purpose. NULL means "never reserved, or reserved before this
    # deploy": in-flight jobs keep the reserved_count-based behaviour they
    # started under, exactly as migration 087 handled its own deploy seam.
    op.add_column(
        "jobs",
        sa.Column("quota_period_start", sa.DateTime(timezone=True), nullable=True),
    )

    # ── 4. Install the shared arithmetic ─────────────────────────────────────
    op.execute(_FUNCTIONS)

    # ── 5. Backfill: describe the CURRENT behaviour, change nothing ──────────
    op.execute(BACKFILL_WINDOWS)
    op.execute(BACKFILL_FIRST_PAID)
    op.execute(BACKFILL_TRIAL_CONSUMED)


def downgrade() -> None:
    op.execute(_DROP_FUNCTIONS)
    op.drop_column("jobs", "quota_period_start")
    op.drop_column("users", "pending_records_limit")
    op.drop_column("users", "pending_plan")
    op.drop_column("users", "paid_entitlement_ended_at")
    op.drop_column("users", "first_paid_at")
    op.drop_column("users", "trial_consumed_at")
    op.drop_column("users", "entitlement_grace_ends_at")
    op.drop_column("users", "entitlement_ends_at")
    op.drop_column("users", "quota_period_end")
    op.drop_column("users", "quota_period_start")
    op.drop_column("users", "quota_anchor_at")
