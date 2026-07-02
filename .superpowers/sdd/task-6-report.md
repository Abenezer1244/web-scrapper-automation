# Task 6 report — Paginated combined-leads endpoints

Commit: `e52edb5` on `chore/xcheck-session`
(prior branch tip before this task: `3f753fc`)

## Files changed

- `src/api/schemas.py`
  - Appended `BatchLeadRow` (mirrors the combined CSV's columns: id, date_recorded,
    party_name, parcel_id, property_address, mailing_address, phone, phone_type,
    email, matched_record_types, overlap_count, source_counties, lead_subtype)
    and `BatchLeadsPage` (leads/counts/delivery_mode/page/page_size/total),
    inserted immediately after `BatchDetailResponse`, exactly as specified.

- `src/api/routes/batches.py`
  - New top-level imports: `Query`, `Response` (fastapi); `text` (sqlalchemy);
    `UTC`, `datetime`; `BatchDeliveryCounts`, `BatchLeadRow`, `BatchLeadsPage`
    (schemas); `TAX_CAP_BIND`, `tax_cap_min_year` (tax_filters); `decrypt_field`
    (crypto); `resolve_hidden_output_fields` (lead_export); `_COMBINED_SQL`,
    `_DELIVERY_COUNTS_SQL` (**directly from `src.workers.batch_export`, at module
    level** — see "batch_sql.py extraction" section below for why this was NOT
    moved to a new leaf module despite the brief's conditional fallback).
  - New `_leads_page()` helper (shared body): 404-gates on
    `run.status not in _DOWNLOADABLE_STATUSES`, sets `Cache-Control: no-store`,
    runs `_DELIVERY_COUNTS_SQL` for live (uncapped, mode-independent) counts,
    computes `total` as `overlaps_delivered` (overlaps_only) or `leads_total`
    (everything/overlaps_first), runs `_COMBINED_SQL` for the current page,
    decrypts phone/email, blanks `mailing_address` when hidden, and builds
    `BatchLeadRow` per row.
  - New `GET /{batch_id}/leads` (`list_batch_leads`) — latest-run view, rate
    limited, ownership via `_owned_batch` + `_run_for`.
  - New `GET /{batch_id}/runs/{run_id}/leads` (`list_batch_run_leads`) —
    run-scoped view, rate limited, ownership via `_owned_batch` +
    an explicit `BatchRun` lookup scoped to `batch_id` + `user_id` (same
    pattern as the existing `download_batch_run`).
  - Both endpoints appended after `download_batch_run`, matching the brief's
    placement instruction exactly.

- `tests/test_batch_leads_endpoint.py` (new)
  - `overlap_batch` fixture + `TestBatchLeads` with all 5 tests from the brief,
    copied verbatim except for two adaptations (below).
  - `schema/openapi.json` regenerated (below).

## `batch_sql.py` extraction — investigated, NOT needed, and NOT possible as specified

The brief's Step 4 conditional: *"Verify with `python -c "from src.workers.batch_export
import _COMBINED_SQL"` — if that import drags Celery in, move the two SQL
constants + `EXPORT_CAP` to a new leaf module `src/workers/batch_sql.py`."*

**What I found:**

1. Ran the exact verification command with the env recipe. It succeeded cleanly
   (`OK <class 'str'>`, only pre-existing unrelated billing-config warnings —
   no error, no circular-import failure).
2. Read `src/workers/__init__.py`: importing *any* submodule of `src.workers`
   (via `from src.workers.batch_export import ...`) unconditionally executes
   this file first (standard Python package-import semantics — there is no way
   around it). It **does** construct `app = Celery("bridgeleads", broker=...,
   backend=..., include=[9 task modules])`, configure queues/routes, and
   register a `worker_ready` signal handler. So in the literal sense, the
   import factually *does* "drag in the Celery app" as an object — it's just
   cheap (no broker connection opens at construction time) and doesn't error.
3. **Critical finding: the brief's proposed fallback path doesn't fix the
   stated problem.** `src/workers/batch_sql.py` is still a submodule of
   `src.workers` — importing it would *still* execute `src/workers/__init__.py`
   first, for the exact same reason as #2. Moving the constants there would add
   a file and an indirection layer while leaving the Celery-app-construction
   import graph byte-for-byte identical. It's not a working solution, just a
   different filename.
4. To actually keep Celery construction out of the API import graph, the leaf
   module would need to live *outside* `src/workers` (e.g. `src/utils/` or
   `src/api/`) — but the brief pins the path to `src/workers/batch_sql.py`,
   and that path can't achieve the stated goal.
5. Checked for precedent: `grep -rn "^from src\.workers" src/api/` returns
   zero matches — every existing `src.workers.*` import in `src/api/` is
   lazy (inside a function body), with an explicit comment on this file's
   `_stream_run_csv`: *"Lazy import (matches the codebase) — keep the Celery
   app out of the API import graph."* This is a real, deliberate, previously
   zero-exceptions convention that Step 4's top-level import breaks.
6. Checked the reverse direction for asymmetry: `src/workers/batch_export.py`
   already imports `from src.api.tax_filters import ...` **at module level**
   (pre-existing, from Task 2) — meaning workers already reach into
   `src.api.*` submodules at import time. `src/api/__init__.py` is *not*
   trivial either — it eagerly imports every route module including
   `routes.batches` itself. So the codebase already tolerates a heavier
   worker→api coupling than what Step 4 asks for in the opposite direction.

**Decision:** did NOT create `src/workers/batch_sql.py` (it would not solve
the problem it's meant to solve, and would add pure indirection against the
CLAUDE.md simplicity directive). Instead:
- Implemented the top-level import exactly as Step 4's primary instruction
  states (`from src.workers.batch_export import _COMBINED_SQL,
  _DELIVERY_COUNTS_SQL`), since the specified verification command passed
  clean.
- Went one step further than the brief's own check to be certain: imported
  `src.api` (the full package, which eagerly loads every route including the
  edited `batches.py`) and `main` (the actual FastAPI app entrypoint) after
  making the edit. Both imported cleanly with zero errors — full proof the new
  forward-direction coupling (api→workers, on top of the pre-existing
  workers→api coupling) does not create a circular-import failure in the real
  app boot path, not just the isolated smoke test.
- Net effect: the API process will now construct a `Celery()` object (no
  broker connection) at boot instead of only on first request touching a
  batch-download endpoint. No behavioral risk (Celery() construction is
  side-effect-free besides object creation + signal registration), but this
  is a real, documented deviation from the file's own stated "keep Celery out
  of API import graph" intent — flagged here for the Codex review gate.

## Test fixture adaptations

Per the CRITICAL BRIEF NOTES, read `tests/test_batches_read.py` first:

- Confirmed exact fixture names: `client`, `db: AsyncSession`, `starter_user`,
  `starter_token` (all present, same signatures the brief assumed).
- The brief's tenant-isolation test used `other_user_token`, which does not
  exist. The real second-user token fixture is **`business_token`**
  (`tests/conftest.py:200`, backed by `business_user` at line 177) — same
  fixture `test_batches_read.py` uses for its own tenant-isolation tests
  (`test_list_tenant_isolation`, `test_detail_tenant_isolation`,
  `test_download_tenant_isolation`). Used `business_token` verbatim in
  `test_tenant_isolation`.
- Dropped the brief's unused `from datetime import UTC, datetime` import (the
  brief's test snippet imports it but never references `UTC`/`datetime` in
  any test body or fixture) — would have failed ruff `F401`.
- Fixed 3x ruff `E741` (ambiguous variable name `l` in list comprehensions,
  e.g. `[l["party_name"] for l in body["leads"]]`) by renaming to `lead`.
- Everything else (the `overlap_batch` fixture body, all 5 test methods'
  logic/assertions) is copied verbatim from the brief — `Result`/`Job`/
  `ScraperConfig`/`ScraperBatch`/`BatchRun` field names all verified against
  `src/db/models.py` (property_key, date_recorded, party_name, delivery_mode
  check constraint, etc.) before writing the file.

## Bind inventory — the two SQL queries `_leads_page` executes

**`_DELIVERY_COUNTS_SQL`** (live, uncapped, mode-independent counts):
| bind | value |
|---|---|
| `uid` | `run.user_id` (verified owner, not the caller's raw input) |
| `job_ids` | `[str(j) for j in run.child_job_ids]` |
| `tax_cap_min_year` (via `TAX_CAP_BIND`) | `tax_cap_min_year(datetime.now(UTC).date())` |

Skipped entirely (no execute call) when `job_ids` is empty — `counts` falls
back to all-zero `BatchDeliveryCounts()`.

**`_COMBINED_SQL`** (paginated, mode-filtered, ordered rows):
| bind | value |
|---|---|
| `uid` | `run.user_id` |
| `job_ids` | same list |
| `limit` | `page_size` (Query, `ge=1, le=100`, default 50) |
| `offset` | `(page - 1) * page_size` (`page` Query `ge=1`, default 1) |
| `overlaps_only` | `delivery_mode == "overlaps_only"` (Python bool → asyncpg boolean bind, no cast needed — no failure observed in the smoke tests, and this exact bind shape is already proven working by `_combined_pairs` in `batch_export.py`, which passes the identical dict shape through a *sync* psycopg2 session) |
| `tax_cap_min_year` | same value as above |

Skipped entirely when `job_ids` is empty — `rows = []`, `leads = []`.

Both queries are tenant-scoped via `:uid` (matching `run.user_id`, which is
only reachable after `_owned_batch`/`_run_for` or the explicit
`batch_id == ... AND user_id == ...` run lookup have already verified
ownership) — no query here trusts caller-supplied identifiers beyond
`batch_id`/`run_id` used for the ownership joins themselves.

## OpenAPI regeneration

```
"C:\...\web-scrapper-automation\.venv-schema\Scripts\python.exe" scripts/export_openapi.py
# -> wrote .../schema/openapi.json (194070 bytes, 61 paths)

"C:\...\web-scrapper-automation\.venv-schema\Scripts\python.exe" scripts/export_openapi.py --check
# -> OK: schema/openapi.json is up to date.
```

Note: `.venv-schema` lives in the main repo root
(`C:\Users\Windows\OneDrive - Seattle Colleges\Desktop\web-scrapper-automation\.venv-schema`),
not inside the `xcheck-session` worktree — used the absolute path as the
brief specified. `schema/openapi.json` is included in the commit.

## Verification (pytest cannot run locally — no Postgres; guarded by `tests/_db_safety.py`)

```
python -m py_compile src/api/routes/batches.py src/api/schemas.py tests/test_batch_leads_endpoint.py
# -> exit 0, no output

ruff check src/api/routes/batches.py src/api/schemas.py tests/test_batch_leads_endpoint.py
# -> All checks passed!

python -c "from src.workers.batch_export import _COMBINED_SQL; print('OK', type(_COMBINED_SQL))"
# -> OK <class 'str'>  (with the env recipe)

python -c "import src.api as api; from src.api.routes.batches import list_batch_leads, list_batch_run_leads, BatchLeadsPage, _leads_page; print('endpoints OK')"
# -> src.api import OK; all 9 routers present; endpoints OK

python -c "import main; print('main import OK, routes:', len(main.app.routes))"
# -> main import OK, routes: 10   (proves the real FastAPI boot path, not just an isolated import)
```

Every edited region was re-read after editing to confirm the change applied
(`schemas.py` new block after `BatchDetailResponse`; `batches.py` full new
import block + the ~150-line appended section).

## Deviations from the brief (all called out above too)

1. **Did not create `src/workers/batch_sql.py`.** The brief's own fallback
   path is architecturally ineffective (still triggers `src/workers/__init__.py`
   / Celery-app construction, since it's still inside the `src.workers`
   package) — implementing it would add indirection without solving anything.
   Used the brief's primary Step-4 top-level-import instruction instead, since
   the specified verification command passed clean, and additionally verified
   the real app-boot path (`import src.api`, `import main`) has no circular
   import failure.
2. Test: `other_user_token` → `business_token` (real fixture name in
   `tests/conftest.py`).
3. Test: dropped the brief's unused `from datetime import UTC, datetime`
   import (ruff F401).
4. Test: renamed comprehension variable `l` → `lead` (ruff E741) in 2 spots.

No other deviations. Schema field names, SQL bind names, and endpoint
signatures match the brief's code block verbatim.

---

## Fix: lazy worker-SQL import (boot import graph)

**Commit:** `17fbb7e` on `chore/xcheck-session`

### Issue addressed
The module-level import of `_COMBINED_SQL` and `_DELIVERY_COUNTS_SQL` from
`src.workers.batch_export` (added in Step 4 of Task 6) violated the established
convention in `src/api/routes/batches.py` — documented by the existing lazy-import
comment on `_stream_run_csv`: *"Lazy import (matches the codebase) — keep the Celery
app out of the API import graph."* That import executes `src/workers/__init__.py`,
which constructs the Celery app object at boot time, adding unnecessary coupling to
the API's startup path.

### Fix applied
1. Removed the module-level import at line 48 of `batches.py`
   (`from src.workers.batch_export import _COMBINED_SQL, _DELIVERY_COUNTS_SQL`)
2. Added a lazy import at the top of the `_leads_page()` function body (line 584),
   immediately after the docstring and before first usage of either constant,
   with the same comment pattern as `_stream_run_csv` and `create_batch`

### Verification
```bash
DATABASE_URL="postgresql+asyncpg://x:x@localhost:5432/x_test" \
DATABASE_URL_SYNC="postgresql+psycopg2://x:x@localhost:5432/x_test" \
REDIS_URL="redis://localhost:6379/0" \
SECRET_KEY="ci-test-secret-key-minimum-32-characters-long" \
R2_ENDPOINT_URL="https://fake.r2.cloudflarestorage.com" \
R2_ACCESS_KEY_ID=fake R2_SECRET_ACCESS_KEY=fake R2_BUCKET_NAME=b \
STRIPE_SECRET_KEY=sk_test_fake STRIPE_WEBHOOK_SECRET=whsec_fake \
STRIPE_PRICE_PRO=p STRIPE_PRICE_BUSINESS=p STRIPE_PRICE_AGENCY=p \
RESEND_API_KEY=re_fake EMAIL_FROM=noreply@bridgeleads.io \
FRONTEND_URL=http://localhost:3000 ENVIRONMENT=test \
python -c "import sys; import src.api.routes.batches; assert 'src.workers' not in sys.modules, 'celery leaked into boot import'; print('BOOT_CLEAN')"
# Output: BOOT_CLEAN
```

Additional checks:
- `python -m py_compile src/api/routes/batches.py` — OK
- `ruff check src/api/routes/batches.py --quiet` — OK (no violations)

### Net effect
The API process will no longer construct the Celery app at boot time.
The constants are now imported on first request to either leads endpoint
(`GET /batches/{batch_id}/leads` or `GET /batches/{batch_id}/runs/{run_id}/leads`),
restoring the zero-exceptions lazy-import convention for `src.workers.*` in the API layer.

---

## Fix: review minors (no-store on all paths + run-scoped tenant test)

**Commit:** (pending in this session, `chore/xcheck-session`)

### Issues addressed
1. **Uniform no-store header**: The `Cache-Control: no-store` header was set inside
   `_leads_page()` (line 591), which meant 404 short-circuit paths
   (`_owned_batch` raising for batch ownership, `_leads_page` raising for run status
   not in `_DOWNLOADABLE_STATUSES`) returned responses WITHOUT the header. The header
   must be set UNCONDITIONALLY as the first statement (after `rate_limit`) in BOTH
   endpoint handlers (`list_batch_leads` and `list_batch_run_leads`) so 404 responses
   also carry the PII-protection header.

2. **Missing run-scoped tenant-isolation test**: The test suite covered
   `test_tenant_isolation` for the batch-scoped `/batches/{id}/leads` endpoint
   (line 132–137 of `test_batch_leads_endpoint.py`), but had no equivalent test
   for the run-scoped `/batches/{batch_id}/runs/{run_id}/leads` endpoint.

### Fixes applied

**`src/api/routes/batches.py`:**
1. Line 665: Added `response.headers["Cache-Control"] = "no-store"` immediately
   after `rate_limit` in `list_batch_leads`, with comment `# decrypted-PII route
   family — never cacheable, on every path`.
2. Line 685: Added the same header+comment in `list_batch_run_leads` immediately
   after `rate_limit`.
3. Line 591: Removed the redundant line from `_leads_page` (it now lives in both
   endpoint handlers, eliminating the short-circuit-path issue).

**`tests/test_batch_leads_endpoint.py`:**
1. Added `test_run_scoped_tenant_isolation` method (lines 139–144) to the
   `TestBatchLeads` class, mirroring the exact style of the existing
   `test_tenant_isolation` but for the run-scoped endpoint. Uses `business_token`
   (the second-user fixture) to verify that a cross-tenant request to
   `/batches/{batch_id}/runs/{run_id}/leads` returns 404.

### Verification
```bash
python -m py_compile src/api/routes/batches.py tests/test_batch_leads_endpoint.py
# -> exit 0, no output

ruff check src/api/routes/batches.py tests/test_batch_leads_endpoint.py --quiet
# -> exit 0, no violations

# Boot-clean check with env recipe:
DATABASE_URL="postgresql+asyncpg://x:x@localhost:5432/x_test" \
DATABASE_URL_SYNC="postgresql+psycopg2://x:x@localhost:5432/x_test" \
REDIS_URL="redis://localhost:6379/0" \
SECRET_KEY="ci-test-secret-key-minimum-32-characters-long" \
R2_ENDPOINT_URL="https://fake.r2.cloudflarestorage.com" \
R2_ACCESS_KEY_ID=fake R2_SECRET_ACCESS_KEY=fake R2_BUCKET_NAME=b \
STRIPE_SECRET_KEY=sk_test_fake STRIPE_WEBHOOK_SECRET=whsec_fake \
STRIPE_PRICE_PRO=p STRIPE_PRICE_BUSINESS=p STRIPE_PRICE_AGENCY=p \
RESEND_API_KEY=re_fake EMAIL_FROM=noreply@bridgeleads.io \
FRONTEND_URL=http://localhost:3000 ENVIRONMENT=test \
python -c "import sys; import src.api.routes.batches; assert 'src.workers' not in sys.modules; print('BOOT_CLEAN')"
# Output: BOOT_CLEAN
```

### Net effect
1. All paths through both leads endpoints (200-OK, 404s from `_owned_batch`,
   404s from `_leads_page` ready-gate) now unconditionally set the no-store header,
   protecting decrypted PII from caching at any proxy/browser layer.
2. The run-scoped endpoint's tenant boundary is now explicitly tested, preventing
   regression if the ownership check is accidentally weakened or skipped in
   future refactors.

## Fix: Codex P2 — no-store on exception paths

**Commit:** (pending in this session, `chore/xcheck-session`)

### Issue addressed
The previous fix (above) set `response.headers["Cache-Control"] = "no-store"` on
the **injected** `Response` object as the first statement in both endpoint
handlers. That covers the 200-OK path, but FastAPI builds the response for a
raised `HTTPException` **separately** from the injected `Response` — the
default `http_exception_handler` constructs a brand-new `JSONResponse`/`Response`
from `exc.headers`, never touching the `response` param the route mutated. So
when `_owned_batch`, the run lookup, or `_leads_page`'s ready-gate raised
`HTTPException` (the 404 "not ready yet" / "not found" cases), the resulting
error response carried **no** `Cache-Control` header — a private/browser cache
could retain that "not ready" 404 and keep serving it after the run finished
and data became available (Codex P2).

Also fixed (Claude final-review Minor): `_leads_page(..., response: Response)`
took a `response` param that no callsite used — it was dead until this fix.

### Fixes applied

**`src/api/routes/batches.py`:**
1. `_leads_page`: now uses the `response` param — sets
   `response.headers["Cache-Control"] = "no-store"` as the first statement in
   the function body, with comment `# decrypted-PII route family — never
   cacheable (200 path; endpoints stamp exception paths)`. This covers the
   200-OK path (the param is no longer dead).
2. `list_batch_leads` and `list_batch_run_leads`: removed the unconditional
   `response.headers["Cache-Control"] = "no-store"` line from each handler.
   Wrapped the entire handler body (from the `rate_limit` await through the
   `return`) in `try: ... except HTTPException as exc: exc.headers =
   {**(exc.headers or {}), "Cache-Control": "no-store"}; raise`. This stamps
   `no-store` onto the exception object itself — verified against FastAPI's
   `fastapi.exception_handlers.http_exception_handler` source
   (`headers = getattr(exc, "headers", None)` then passed straight into the
   `Response`/`JSONResponse` constructor), and confirmed `main.py` has no
   custom `HTTPException` handler that would bypass this (only a catch-all for
   unhandled `Exception`) — so mutating `exc.headers` before re-raising is the
   correct, verified mechanism to get `no-store` onto 404/402/429/etc.
   responses built from this exception.

**`tests/test_batch_leads_endpoint.py`:**
1. `test_not_ready_while_running_404`: added
   `assert resp.headers["cache-control"] == "no-store"` after the 404 assert.
2. `test_tenant_isolation` and `test_run_scoped_tenant_isolation`: added the
   same one-line `no-store` header assertion after each 404 assert.

**`src/api/schemas.py`:**
1. `BatchCreateRequest.delivery_mode` comment: appended a clarifying line —
   `overlaps_first` and `everything` currently produce identical output
   (the SQL sort already ranks overlaps first); the distinct mode is reserved
   for a future sectioned-export format. Comment-only change; does not alter
   the OpenAPI schema (confirmed below).

### Verification
```bash
python -m py_compile src/api/routes/batches.py src/api/schemas.py tests/test_batch_leads_endpoint.py
# -> exit 0, no output

python -m ruff check src/api/routes/batches.py src/api/schemas.py tests/test_batch_leads_endpoint.py
# -> All checks passed!

# Boot-clean check with env recipe:
DATABASE_URL="postgresql+asyncpg://x:x@localhost:5432/x_test" \
DATABASE_URL_SYNC="postgresql+psycopg2://x:x@localhost:5432/x_test" \
REDIS_URL="redis://localhost:6379/0" \
SECRET_KEY="ci-test-secret-key-minimum-32-characters-long" \
R2_ENDPOINT_URL="https://fake.r2.cloudflarestorage.com" \
R2_ACCESS_KEY_ID=fake R2_SECRET_ACCESS_KEY=fake R2_BUCKET_NAME=b \
STRIPE_SECRET_KEY=sk_test_fake STRIPE_WEBHOOK_SECRET=whsec_fake \
STRIPE_PRICE_PRO=p STRIPE_PRICE_BUSINESS=p STRIPE_PRICE_AGENCY=p \
RESEND_API_KEY=re_fake EMAIL_FROM=noreply@bridgeleads.io \
FRONTEND_URL=http://localhost:3000 ENVIRONMENT=test \
python -c "import sys; import src.api.routes.batches; assert 'src.workers' not in sys.modules; print('BOOT_CLEAN')"
# Output: BOOT_CLEAN

# OpenAPI staleness check (pinned venv, per scripts/export_openapi.py's own
# WARNING that output is dependency-version-sensitive):
"C:\...\web-scrapper-automation\.venv-schema\Scripts\python.exe" scripts/export_openapi.py --check
# -> OK: schema/openapi.json is up to date.  (comment-only schemas.py change,
#    as expected — no commit to schema/openapi.json needed)
```

### Net effect
1. Every response from either leads endpoint — 200-OK **and** every
   `HTTPException` path (404 not-ready, 404 not-found, 404 tenant-mismatch,
   and any future 402/429 raised inside the `try` block) — now carries
   `Cache-Control: no-store`. A cache can no longer retain a stale "not ready"
   404 past the point where the run actually finishes and data is available.
2. `_leads_page`'s `response` parameter is live code, not dead weight.
3. The `delivery_mode` docstring now tells the next reader why
   `overlaps_first` and `everything` look identical today, instead of leaving
   that as a silent surprise.
