# BridgeLeads — Worker Scaling Guide

## Current vs 10x Architecture

| Metric | Current (1x) | 10x | Notes |
|--------|-------------|-----|-------|
| Concurrent scrapes | 1 | 10 | 4 worker replicas × 3 concurrency |
| 35 counties | ~6 hours | ~35 min | 10 parallel + ~5 min per job |
| Monthly cost | ~$20 | ~$80 | 4× worker replicas |
| Memory per replica | 2 GB | 2 GB | 3 Chromium instances × ~500 MB |
| Users supported | 1-5 | 50-100 | Parallel job processing |

## How to Deploy 10x on Railway

### Step 1: Scale Worker Replicas (Railway Dashboard)

Railway supports multiple replicas of the same service:

1. Go to Railway Dashboard → bridgeleads-production → worker service
2. Settings → Scaling → **Replicas: 4**
3. Each replica auto-gets a unique `RAILWAY_REPLICA_ID`

Or via CLI:
```bash
railway service worker
railway up --replicas 4
```

### Step 2: Set Environment Variables

On the **worker** service:
```
WORKER_CONCURRENCY=3      # 3 parallel scrapes per replica (4×3 = 12 total)
WORKER_QUEUES=scrape,enrichment
```

On the **beat** service (stays at 1 replica):
```
WORKER_QUEUES=celery       # scheduler tasks only
```

### Step 3: Upgrade Railway Plan

Each worker replica with 3 Chromium instances needs ~1.5-2 GB RAM:
- **Hobby plan** ($5/mo): 8 GB shared — good for 2-3 replicas
- **Pro plan** ($20/mo): 32 GB shared — good for 4-8 replicas
- **Enterprise**: unlimited

### Step 4: Upgrade Redis (if needed)

Upstash free tier: 10K commands/day → enough for 10x
Upstash Pro ($10/mo): 500K commands/day → enough for 100x

## Queue Architecture

```
Task Type          → Queue        → Worker Pool
─────────────────────────────────────────────────
run_scrape_job     → scrape       → worker replicas (4×3 = 12 slots)
enrich_job_results → enrichment   → same workers (or dedicated)
scheduler tasks    → celery       → beat service (1 replica)
```

Each worker replica consumes from the same Redis queue — Celery handles
distribution automatically. No code changes needed to add replicas.

## Scaling Beyond 10x (50x-100x)

For national scale (3,000+ counties, 1000+ users):

### Dedicated Queue Workers
```
worker-scrape:     WORKER_QUEUES=scrape        WORKER_CONCURRENCY=4  × 8 replicas = 32 scrapes
worker-enrich:     WORKER_QUEUES=enrichment    WORKER_CONCURRENCY=2  × 4 replicas = 8 enrichments
beat:              WORKER_QUEUES=celery        (1 replica, scheduler only)
```

### Auto-Scaling (Railway Pro)
Railway Pro supports auto-scaling based on CPU/memory:
- Min replicas: 2 (always warm)
- Max replicas: 10 (burst capacity)
- Scale trigger: CPU > 70% for 60s

### Priority Queues
For paid tiers, add priority routing:
```python
# Agency plan → high priority queue (processed first)
app.conf.task_queues += (Queue("scrape-priority", ...),)

# In job creation:
if user.plan == "agency":
    run_scrape_job.apply_async(args=[job_id], queue="scrape-priority")
else:
    run_scrape_job.delay(job_id)
```

### Geographic Distribution
For national coverage:
- US-West worker cluster: WA, OR, CA, AZ counties
- US-East worker cluster: NY, FL, GA, NC counties
- US-Central worker cluster: TX, IL, OH, MI counties

Each cluster runs close to the county portals for lower latency.

## Cost Estimates

| Scale | Workers | RAM | Railway Cost | Redis | Total |
|-------|---------|-----|-------------|-------|-------|
| 1x    | 1×1     | 2 GB | $20/mo | Free | ~$25/mo |
| 5x    | 2×3     | 4 GB | $40/mo | Free | ~$45/mo |
| 10x   | 4×3     | 8 GB | $80/mo | $10 | ~$95/mo |
| 25x   | 8×4     | 16 GB | $160/mo | $10 | ~$175/mo |
| 50x   | 12×4    | 24 GB | $240/mo | $30 | ~$280/mo |

## Monitoring

Add to Railway dashboard:
- Worker CPU utilization (target: 60-80%)
- Memory per replica (alert if > 1.8 GB)
- Redis queue depth (alert if scrape queue > 50)
- Job completion rate (alert if < 90%)
