# Pre-foreclosure audit + multi-tenant hardening campaign

Branch: `chore/preforeclosure-audit` (worktree off origin/main @ 8e306a3).
Live verification: via `railway run --service api -- python <script>` (prod env injected; NON-persisting — registry → scrape(), no Celery/DB/billing).
Codex reviews every fix. Agents cross-verify each other's findings. Any P1 (Claude or Codex) = NO-GO.

## Key insight
Pre_foreclosure volume is LOW; most WA non-judicial NTS go through legal newspapers, not the
recorder. A recorder-based scrape returning 0 is OFTEN LEGITIMATE. Audit = (a) scraper runs clean,
(b) doc-type filter is correct (real NTS/LisPendens/NOD keywords, no junk, right party orientation),
(c) where records exist they're legit, (d) the NTS legal-notice pipeline (pierce/snoho/king) is solid.

## Scope (active pre_foreclosure connectors only; skip health=down placeholders + spokane/Cloudflare)
EagleWeb: benton, clallam, grant, island, jefferson, kitsap, lewis, pacific
AcclaimWeb: chelan, douglas · Laserfiche: cowlitz · Tyler: okanogan · Skagit · iDocMarket: columbia
LandmarkWeb: king, clark · ARMS: pierce · NTS legal-notice: pierce, snohomish, king

## Phases
- [x] P0 Recon — DONE (code map + prod connector list).
- [ ] P1 Code audit (orchestrated parallel agents, one per template family) → doc-type filters,
      party orientation, exclude lists, parcel policy. Cross-verify + Codex.
- [ ] P2 Live verify pre_foreclosure on active counties (railway run). Accept legit 0s; flag junk/errors.
- [ ] P3 Multi-tenant hardening review of the pre_foreclosure + NTS pipeline (RLS, user_id filters,
      concurrency, nts_matcher cross-tenant task). Codex.
- [ ] P4 Fix issues (live-verify + Codex each; agents cross-verify). Phased, ≤5 files/phase.
- [ ] P5 PR to main; clean up worktree after merge.

## Review
Shipped as **PR #70** (branch `chore/preforeclosure-audit` → main). 2 commits, Codex-reviewed ×3.

**Findings (5-agent orchestrated audit, cross-corroborated):**
- THEME 1 (P1): cancelled/cured/admin docs counted as active foreclosures — no template had a
  pre_foreclosure exclude; "TRUSTEE SALE"/"FORECLOSURE"/"NOTICE OF DEFAULT" substring-matched
  Discontinuance/Rescission; acclaim/ava/landmarkweb listed danger keywords as INCLUDEs; King had
  NO post-extraction filter.
- THEME 2 (P1): company-as-party — laserfiche/ava/tyler/skagit/King put the trustee company in
  party_name instead of the borrower.
- THEME 3 (P2/P3): require_parcel_id could drop parcel-less Lis Pendens (NOT changed — accepted;
  most templates already enrich downstream / default OFF).
- THEME 4: multi-tenant pipeline SAFE; NTS cross-tenant matcher SAFE (public data). Hardened anyway.

**Fixes:** shared `src/scrapers/preforeclosure.py` (is_cancellation_or_admin + orient_pre_foreclosure_party)
applied to 9 scraper files; NTS `best_match_group` multi-tenant coverage; 2 user_id suspenders +
inline belt; enrich logging.

**Verified:** live probe (kitsap/cowlitz/skagit/okanogan, 180d) company_as_party=0 danger_doctype=0,
healthy totals; unit tests (helpers + best_match_group); ruff clean; smoke-imports OK; Codex clean.

**Deferred (noted, not in scope):** Clark pre_foreclosure uses "all categories" (over-broad, pre-existing);
okanogan doc_type has a cosmetic leading mojibake glyph; spaced-out "L L C" entity names evade the
org-token check (rare); the require_parcel_id parcel-less-NTS drop on laserfiche/skagit.
