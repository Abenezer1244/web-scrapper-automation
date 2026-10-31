# Skagit's new domain; worker DB connections across fork and canary runs

Seen in worker logs 2026-10-31 17:18-17:28Z. Diagnosed read-only.

## PR 1 - Skagit recorder moved (fix/skagit-new-domain)
Root cause: `www.skagitcounty.net/Search/Recording/` now 301s to
`https://classic.skagitcountywa.gov/search/Recording/` (cert `*.skagitcountywa.gov`, O=Skagit
County; same ASP.NET form). The SSRF allowlist only holds hosts of connector base_urls, so the
redirect is refused and every Skagit job and canary fails.
- [x] Migration 122: county_connectors skagit/WA base_url old -> new (guarded on the old value;
      downgrade reverses). Exact FQDN, never the parent domain (subdomain matching).
- [x] registry: route both hosts to SkagitRecordingScraper (old rows keep working until migrated).
- [x] template docstring; tests for both URLs.
- [x] Live: 14-day probate window at the new URL, through the normal SSRF path: 30 records (4 doc-type searches).

## PR 2 - worker DB resilience (fix/worker-db-fork-and-canary)
A. Prefork children (max-tasks-per-child=3) inherit sync_engine's pooled TLS connection opened by
   worker_ready in the parent -> "SSL error: wrong version number". Fix: worker_process_init ->
   sync_engine.dispose(close=False).
B. canary holds one session/transaction across all scrape units (minutes); the pooler drops it and
   the final commit fails ("SSL SYSCALL error: EOF"), losing every unit's health_status. Fix: read
   connectors in a short session (detached objects, expire_on_commit=False), probe each unit with
   no session open, write each unit's health_status/last_checked in its own short session; queue a
   unit's alerts only after its write commits; dispatch committed alerts even if a later write raises.

## Not fixed in code
Spokane pre_foreclosure: the portal now sits behind a Cloudflare browser challenge. Bypassing bot
detection is off the table; connector stays down (already hidden). Owner: ask the county for access.
