#!/usr/bin/env bash
# Run this branch's tests against an ISOLATED local test database.
#
# The shared `bridgeleads_test` on this machine is used by every worktree at
# once, and its conftest teardown deletes EVERY user with a @test.bridgeleads.io
# address. A second session running tests there deletes this one's fixture rows
# mid-request, which surfaces as 401s, "Could not refresh instance", and FK
# violations that look like product bugs and are not. Isolation is the fix, not
# a retry loop. Redis db 1 for the same reason (conftest flushes the whole db).
#
# This includes YOUR OWN runs: two of these going at once (an integration pass
# beside a non-integration one) delete each other's users and produce the same
# scattered, different-every-time failures. Run them one at a time.
set -u
export TEST_DATABASE_URL="postgresql+asyncpg://bridgeleads:testpassword@127.0.0.1:5432/bridgeleads_entaudit_test"
export TEST_DATABASE_URL_SYNC="postgresql+psycopg2://bridgeleads:testpassword@127.0.0.1:5432/bridgeleads_entaudit_test"
export DATABASE_URL="$TEST_DATABASE_URL"
export DATABASE_URL_SYNC="$TEST_DATABASE_URL_SYNC"
export REDIS_URL="redis://127.0.0.1:6379/1"
export SECRET_KEY="test_secret_key_for_local_full_pytest_0123456789"
export STRIPE_SECRET_KEY="sk_test_fake"
# Fake but well-formed plan price ids: _PRICE_TO_PLAN is built from these, and
# an empty map silently SKIPS every checkout/webhook test rather than failing.
export STRIPE_PRICE_PRO="price_test_pro_monthly"
export STRIPE_PRICE_PRO_ANNUAL="price_test_pro_annual"
export STRIPE_PRICE_BUSINESS="price_test_business_monthly"
export STRIPE_PRICE_BUSINESS_ANNUAL="price_test_business_annual"
export STRIPE_PRICE_AGENCY="price_test_agency_monthly"
export STRIPE_PRICE_AGENCY_ANNUAL="price_test_agency_annual"
export ENVIRONMENT="test"
exec python -m pytest -q -p no:cacheprovider -o addopts="" "$@"
