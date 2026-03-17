# BridgeLeads — DevOps Layer

---

## What Was Built

Full DevOps layer for BridgeLeads — CI/CD, infrastructure as code, monitoring, alerting, runbooks, and scaling strategy. 13 new files on top of the backend.

---

## File Inventory

```
.github/
  workflows/ci-cd.yml        ← Full CI/CD: test → build → staging → production
  dependabot.yml             ← Automated dependency updates
infra/
  nginx/api.proppulse.io.conf ← Reverse proxy + rate limiting + SSE support
  terraform/main.tf          ← Cloudflare DNS + WAF + R2 bucket
monitoring/
  prometheus.yml             ← Metrics scrape config
  alerts.yml                 ← 9 alerting rules with thresholds
docs/
  runbooks.md                ← Incident response + common ops tasks
  scaling.md                 ← Cost model + scaling triggers
scripts/
  bootstrap.sh               ← First-time production setup script
docker-compose.prod.yml      ← Full stack with monitoring (Prometheus, Grafana, Loki)
railway.toml                 ← Railway deployment config
pyproject.toml               ← Ruff linting + pytest + coverage config
```

---

## CI/CD Pipeline

### Trigger Rules

| Event | Action |
|-------|--------|
| PR to `main` | Run tests only |
| Push to `staging` | Test → Build → Deploy to staging |
| Push to `main` | Test → Build → Migrate DB → Deploy to production |

### Pipeline Stages

#### 1. Test (all pushes and PRs)
- Spins up PostgreSQL + Redis via GitHub Actions services
- Installs dependencies with pip cache
- Lints with ruff (fast, Rust-based)
- Runs Alembic migrations against test DB
- Runs pytest with coverage report
- Uploads coverage to Codecov

#### 2. Build (pushes only)
- Builds Docker image with Buildx
- Pushes to GitHub Container Registry (ghcr.io)
- Tags: `{branch}-{sha}`, `latest` (main), `staging`
- Uses GitHub Actions cache for fast rebuilds

#### 3. Deploy Staging
- Deploys API + worker + beat to Railway staging environment
- Runs smoke test: `curl /health`
- Notifies Slack on success/failure

#### 4. Deploy Production
- Runs DB migrations first (before deploying new code)
- Deploys to Railway production
- Health checks with 3 retries, 10s spacing
- Notifies Slack

### Required GitHub Secrets
```
RAILWAY_TOKEN_STAGING       Railway staging deploy token
RAILWAY_TOKEN_PROD          Railway production deploy token
PROD_DATABASE_URL_SYNC      Production DB URL (for migrations)
SLACK_WEBHOOK               Slack notification webhook
```

### Required GitHub Variables
```
STAGING_API_URL             https://staging.api.proppulse.io
PROD_API_URL                https://api.proppulse.io
```

---

## Infrastructure

### Service Topology

```
Users
  │
  ▼
Cloudflare (DNS + WAF + DDoS protection)
  │
  ▼
Railway
  ├── FastAPI (2 replicas, auto-scale to 4)
  ├── Celery workers (2 replicas, auto-scale to 10)
  └── Celery beat (1 replica, singleton)
  │
  ├── Supabase (PostgreSQL)
  ├── Upstash (Redis)
  └── Cloudflare R2 (export storage)

Vercel → Next.js frontend
Resend → Transactional email
```

### Nginx Config Highlights
- Rate limiting by zone: auth (10 req/min), job creation (5 req/min), general (60 req/min)
- SSE endpoint special config: `proxy_buffering off`, `chunked_transfer_encoding on`, 300s timeout
- Security headers: HSTS, X-Frame-Options, X-Content-Type-Options
- TLS 1.2/1.3 only, strong cipher suite

### Cloudflare WAF Rules
- Block known bad bots (not search engines or monitoring)
- Challenge suspicious auth attempts (>10 login attempts in 60s from same IP)
- Block SQL injection (WAF score < 20)

### Terraform Manages
- Cloudflare DNS records (api, app subdomains)
- Cloudflare WAF ruleset
- Cloudflare R2 export bucket (WNAM region — closest to WA state targets)
- State stored in R2 backend

---

## Monitoring Stack

### Components

| Tool | Purpose | Port |
|------|---------|------|
| Prometheus | Metrics collection | :9090 |
| Grafana | Dashboards + alerting | :3001 |
| Loki | Log aggregation | internal |
| Promtail | Log shipping | internal |
| Flower | Celery monitoring | :5555 |
| redis-exporter | Redis metrics | :9121 |
| postgres-exporter | PostgreSQL metrics | :9187 |

### Alert Rules (9 total)

| Alert | Threshold | Severity |
|-------|-----------|---------|
| APIDown | Unreachable 2+ min | Critical |
| CeleryWorkersDown | 0 workers 5+ min | Critical |
| QueueDepthHigh | >50 jobs in queue 10+ min | Warning |
| HighJobFailureRate | >20% failure rate 15+ min | Warning |
| DBPoolExhausted | >80 active connections | Warning |
| RedisMemoryHigh | >85% of max memory | Warning |
| DiskSpaceLow | <15% free disk | Warning |
| APIHighLatency | p95 > 2s for 10+ min | Warning |
| CountyConnectorDown | Canary returns 0 records | Warning |

---

## Runbook Index

### Incident Response (P0–P2)
- **P0 — API completely down:** Check logs → restart → rollback
- **P1 — Jobs stuck:** Check Celery → trigger watchdog → force-fail
- **P1 — County portal changed:** Manual test → pause county → fix selector → re-enable
- **P2 — DB pool exhausted:** Kill idle connections → restart API
- **P2 — Redis memory high:** Clear old Celery results + log channels

### Common Ops
- Adding a new county scraper (5 steps)
- Rolling back a deploy
- Manually running a scrape job via curl
- Resetting user's monthly record count
- Re-generating a download URL
- Manual database backup + restore

---

## Scaling Triggers

### Worker Scaling (Most Common)

Scale 2 → N workers when:
- Redis queue depth > 20 consistently
- Job wait time > 5 minutes
- Rule of thumb: 1 worker per 10 concurrent users

Each worker = 1 Playwright browser = ~500MB RAM

### API Scaling

Scale 2 → N replicas when:
- p95 latency > 1s
- CPU > 70% sustained

### Cost Model

| Phase | Users | Revenue | Infra Cost | Ratio |
|-------|-------|---------|------------|-------|
| Phase 1 | 0–100 | $0–$10K | $20/mo | <1% |
| Phase 2 | 100–500 | $10K–$50K | $170/mo | <1% |
| Phase 3 | 500–2000 | $50K–$200K | $1,254/mo | 2.5% |

### Playwright Scaling Path
- **Phase 1–2:** Each worker runs its own browser (simple)
- **Phase 3:** `browserless.io` shared pool ($50/mo for 20 concurrent)
- **Phase 4:** Dedicated scraping cluster, workers become stateless dispatchers

---

## First Production Deploy Checklist

- [ ] Set all required env vars in Railway
- [ ] Run `scripts/bootstrap.sh` to verify env + run migrations
- [ ] Set up Cloudflare DNS via Terraform
- [ ] Configure GitHub secrets (Railway tokens, Slack webhook)
- [ ] Push to `staging` branch → verify CI passes
- [ ] Push to `main` branch → verify production deploy
- [ ] Verify `/health` endpoint returns 200
- [ ] Create Mike's account via API
- [ ] Create Pierce County scraper config
- [ ] Run first job manually
- [ ] Verify CSV arrives in Mike's email
- [ ] Set up daily schedule
- [ ] Set up Grafana dashboards
- [ ] Configure Slack alert channel
- [ ] Test canary health check runs
- [ ] Set up automated DB backups
