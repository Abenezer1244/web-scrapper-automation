# BridgeLeads Production Launch Checklist

Complete every item in order. Do not skip steps.

---

## Pre-requisites

| Tool | Install |
|------|---------|
| Railway CLI | `npm install -g @railway/cli` then `railway login` |
| Terraform | `winget install HashiCorp.Terraform` |
| GitHub CLI | `winget install GitHub.cli` then `gh auth login` |

---

## Step 1 — Supabase Database

- [x] Go to [supabase.com](https://supabase.com) → your project → **Settings → Database**
- [x] Copy the **Connection string (URI)** — select **Transaction** mode for `DATABASE_URL_SYNC` (psycopg2) and **Session** mode for `DATABASE_URL` (asyncpg)
- [x] Enable **Point-in-Time Recovery** (PITR) under **Database → Backups**
- [x] Set a database password — save it securely

---

## Step 2 — Cloudflare R2

- [x] Cloudflare dashboard → **R2** → Create bucket `bridgeleads-exports` (region: WNAM)
- [x] **R2 → Manage R2 API Tokens** → Create token with **Object Read & Write** on `bridgeleads-exports`
- [x] Copy: Account ID, Access Key ID, Secret Access Key, Endpoint URL
  Format: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

---

## Step 3 — Stripe

- [x] [dashboard.stripe.com](https://dashboard.stripe.com) → **Developers → API keys** → copy Secret key
- [x] **Products** → Create 3 products:
  - Pro ($99/mo) → copy Price ID → `STRIPE_PRICE_PRO`
  - Business ($299/mo) → copy Price ID → `STRIPE_PRICE_BUSINESS`
  - Agency ($799/mo) → copy Price ID → `STRIPE_PRICE_AGENCY`
- [x] **Webhooks** → Add endpoint: `https://api.bridgeleads.io/billing/webhook`
  - Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`
  - Copy Signing secret → `STRIPE_WEBHOOK_SECRET`
- [x] Enable **Stripe Customer Portal** → Settings → Customer portal → Activate

---

## Step 4 — Resend Email

- [x] [resend.com](https://resend.com) → **API Keys** → Create key → `RESEND_API_KEY`
- [x] **Domains** → Add `bridgeleads.io` → verify DNS records in Cloudflare
- [x] Set `EMAIL_FROM=leads@bridgeleads.io`

---

## Step 5 — Upstash Redis

- [x] [upstash.com](https://upstash.com) → Create database → region: `us-west-1`
- [x] Copy **Redis URL** (TLS) → `REDIS_URL`

---

## Step 6 — Railway Setup

- [x] `railway login`
- [x] `railway init` → create project `bridgeleads-production`
- [x] Create 3 services: **api**, **worker**, **beat**
- [x] Set all environment variables on each service:

```bash
# Run for each service: api, worker, beat
railway variables set \
  DATABASE_URL="postgresql+asyncpg://..." \
  DATABASE_URL_SYNC="postgresql+psycopg2://..." \
  REDIS_URL="rediss://..." \
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  R2_ENDPOINT_URL="https://<ACCOUNT_ID>.r2.cloudflarestorage.com" \
  R2_ACCESS_KEY_ID="..." \
  R2_SECRET_ACCESS_KEY="..." \
  R2_BUCKET_NAME="bridgeleads-exports" \
  STRIPE_SECRET_KEY="sk_live_..." \
  STRIPE_WEBHOOK_SECRET="whsec_..." \
  STRIPE_PRICE_PRO="price_..." \
  STRIPE_PRICE_BUSINESS="price_..." \
  STRIPE_PRICE_AGENCY="price_..." \
  RESEND_API_KEY="re_..." \
  EMAIL_FROM="leads@bridgeleads.io" \
  FRONTEND_URL="https://app.bridgeleads.io" \
  ALLOWED_ORIGINS="https://app.bridgeleads.io" \
  ENVIRONMENT="production" \
  DEBUG="false"
```

---

## Step 7 — GitHub Secrets

```bash
# Backend repo
gh secret set RAILWAY_TOKEN_PRODUCTION --body "$(railway whoami --token)"
gh secret set RAILWAY_TOKEN_STAGING    --body "YOUR_STAGING_TOKEN"
gh secret set SLACK_WEBHOOK            --body "https://hooks.slack.com/services/..."
```

---

## Step 8 — Cloudflare DNS (Terraform)

```bash
cd infra/terraform

# Create terraform.tfvars
cat > terraform.tfvars << EOF
cloudflare_api_token  = "your-cloudflare-api-token"
cloudflare_zone_id    = "your-zone-id"
cloudflare_account_id = "your-account-id"
railway_api_ip        = "your-railway-ip"
vercel_ip             = "cname.vercel-dns.com"
EOF

terraform init
terraform plan
terraform apply
```

---

## Step 9 — Run Migrations

```bash
# From your local machine with DATABASE_URL_SYNC set
export DATABASE_URL_SYNC="postgresql+psycopg2://..."
alembic upgrade head
```

Or via Railway:
```bash
railway run --service api alembic upgrade head
```

---

## Step 10 — Vercel (Frontend)

- [x] `cd bridgeleads-web`
- [x] `npx vercel --prod`
- [x] Set environment variables in Vercel dashboard:
  ```
  NEXT_PUBLIC_API_URL=https://api.bridgeleads.io
  NEXTAUTH_URL=https://app.bridgeleads.io
  NEXTAUTH_SECRET=<min 32 char random string>
  ```
- [x] Add `app.bridgeleads.io` as custom domain in Vercel

---

## Step 11 — Deploy

```bash
# Push to staging first
git push origin main:staging

# Watch CI: github.com/your-org/web-scrapper-automation/actions
# All checks green? Then:
git push origin main
```

---

## Step 12 — Verify Production

```bash
# API health check
curl https://api.bridgeleads.io/health
# Expected: {"status":"ok"}

# Auth check
curl -X POST https://api.bridgeleads.io/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"TestPass123!"}'

# Connectors check
curl https://api.bridgeleads.io/scrapers/connectors
# Expected: array with Pierce County entry
```

---

## Step 13 — Onboard Mike (First Customer)

```bash
python scripts/onboard_customer.py \
  --api-url https://api.bridgeleads.io \
  --email mike@MIKE_EMAIL_HERE.com \
  --password "TEMP_PASSWORD_HERE" \
  --plan pro \
  --county pierce \
  --state WA \
  --record-type probate \
  --delivery-email mike@MIKE_EMAIL_HERE.com \
  --run-now
```

After account creation, send Mike the Stripe checkout link:
```bash
# Get checkout URL for Pro plan
curl -X POST https://api.bridgeleads.io/billing/checkout \
  -H "Authorization: Bearer MIKE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"price_id":"STRIPE_PRICE_PRO"}'
```

---

## Step 14 — Monitoring

- [x] Open Grafana: `http://YOUR_SERVER:3001` (or tunnel via Railway)
- [x] Default login: `admin` / `${GRAFANA_PASSWORD}`
- [x] Verify all 4 Prometheus targets are green: fastapi, celery, redis, postgres
- [x] Confirm the BridgeLeads dashboard loaded (auto-provisioned from `infra/grafana/dashboards/`)
- [x] Create Slack channel `#bridgeleads-alerts`
- [x] Set `SLACK_WEBHOOK_URL` in alertmanager environment
- [x] Trigger a test alert: `curl -X POST http://localhost:9093/-/reload`

---

## Step 15 — Supabase Backups

- [x] Supabase dashboard → **Database → Backups**
- [x] Enable **Point-in-Time Recovery**
- [x] Verify daily backups are scheduled
- [x] Test restore procedure on staging before going live

---

## Launch Complete ✓

When all 15 steps are checked:
1. Tweet the launch (optional)
2. Post in WA RE investor groups (Phase 10 GTM begins)
3. Monitor `#bridgeleads-alerts` Slack channel for first 48 hours
